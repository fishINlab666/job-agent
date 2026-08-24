from __future__ import annotations

import asyncio
from pathlib import Path

from cloud.collector.src.collector import Collector
from cloud.collector.src.repository import D1Repository
from cloud.collector.tests.fakes import FakeD1
from cloud.collector.tests.test_repository import _window
from jobagent.adapters.base import RawJob
from jobagent.targets import OBSERVATION_SOURCES


MIGRATION = Path(__file__).parents[1] / "migrations" / "0001_init.sql"


class FakeAdapter:
    def __init__(self, source_key: str, company: str, error: Exception | None = None) -> None:
        self.source_key = source_key
        self.company = company
        self.error = error

    async def fetch_async(self) -> list[RawJob]:
        if self.error:
            raise self.error
        return [
            RawJob(
                external_id=f"{self.source_key}-J1",
                title="工程师",
                raw_json={},
                apply_url=f"https://example.test/{self.source_key}/J1",
            )
        ]


def _collector(failures: set[str] | None = None):
    database = FakeD1(MIGRATION)
    calls: list[str] = []
    failures = failures or set()

    def build(spec):
        calls.append(spec["source_key"])
        error = RuntimeError("upstream failed") if spec["source_key"] in failures else None
        return FakeAdapter(spec["source_key"], spec["company"], error)

    return Collector(D1Repository(database), adapter_builder=build), database, calls


def test_fixed_source_pool_is_the_existing_five_public_sources() -> None:
    assert len(OBSERVATION_SOURCES) == 5
    assert {source["company"] for source in OBSERVATION_SOURCES} == {
        "腾讯", "蔚来", "小鹏汽车", "字节跳动", "商汤科技"
    }


def test_five_successful_sources_complete_the_window_and_replay_is_noop() -> None:
    async def scenario() -> None:
        collector, database, calls = _collector()
        result = await collector.run_window(_window(), trigger="cron")
        assert result["status"] == "complete"
        assert len(result["sources"]) == 5
        assert all(item["status"] == "success" for item in result["sources"])
        assert calls == [source["source_key"] for source in OBSERVATION_SOURCES]

        changes = database.conn.execute("SELECT COUNT(*) FROM job_changes").fetchone()[0]
        replay = await collector.run_window(_window(), trigger="catch-up")
        assert replay["status"] == "complete"
        assert len(calls) == 5
        assert database.conn.execute("SELECT COUNT(*) FROM job_changes").fetchone()[0] == changes

    asyncio.run(scenario())


def test_partial_window_retries_only_failed_source() -> None:
    async def scenario() -> None:
        failed_key = OBSERVATION_SOURCES[2]["source_key"]
        collector, database, calls = _collector({failed_key})
        first = await collector.run_window(_window(), trigger="cron")
        assert first["status"] == "partial"
        assert [item["source_key"] for item in first["sources"] if item["status"] == "failed"] == [failed_key]

        retry_calls: list[str] = []

        def healthy(spec):
            retry_calls.append(spec["source_key"])
            return FakeAdapter(spec["source_key"], spec["company"])

        retry = Collector(D1Repository(database), adapter_builder=healthy)
        second = await retry.run_window(_window(), trigger="catch-up")
        assert second["status"] == "complete"
        assert retry_calls == [failed_key]
        assert len(calls) == 5

    asyncio.run(scenario())


def test_invalid_or_dropped_source_identity_cannot_publish_success() -> None:
    async def scenario() -> None:
        for invalid_kind in ("empty-id", "skipped-id"):
            database = FakeD1(MIGRATION)

            class InvalidAdapter(FakeAdapter):
                async def fetch_async(self) -> list[RawJob]:
                    if invalid_kind == "skipped-id":
                        self.skipped_no_id = 1
                        return await super().fetch_async()
                    return [RawJob(external_id="", title="坏岗位", raw_json={})]

            def build(spec):
                return InvalidAdapter(spec["source_key"], spec["company"])

            result = await Collector(
                D1Repository(database), adapter_builder=build
            ).run_window(_window(), trigger="cron")
            assert result["status"] == "partial"
            assert all(item["status"] == "failed" for item in result["sources"])
            assert database.conn.execute(
                "SELECT COUNT(*) FROM cloud_jobs"
            ).fetchone()[0] == 0

    asyncio.run(scenario())
