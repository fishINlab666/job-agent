from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cloud.collector.src.repository import D1Repository, StagedJob
from cloud.collector.src.windowing import Window
from cloud.collector.tests.fakes import FakeD1
from jobagent.collection import snapshot_digest


MIGRATION = Path(__file__).parents[1] / "migrations" / "0001_init.sql"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _window(key: str = "morning") -> Window:
    hours = {"morning": (8, 12), "afternoon": (12, 17), "evening": (17, 22)}
    start, end = hours[key]
    return Window(
        "2026-08-24",
        key,
        datetime(2026, 8, 24, start, tzinfo=SHANGHAI),
        datetime(2026, 8, 24, end, tzinfo=SHANGHAI),
    )


def _job(external_id: str, title: str = "工程师") -> StagedJob:
    return StagedJob(
        external_id=external_id,
        fingerprint=f"fp-{title}",
        payload={
            "source_key": "source",
            "external_id": external_id,
            "company": "公司",
            "title": title,
        },
    )


def _digest(jobs: list[StagedJob]) -> str:
    return snapshot_digest([job.payload for job in jobs])


def _repo() -> tuple[D1Repository, FakeD1]:
    database = FakeD1(MIGRATION)
    return D1Repository(database), database


def test_get_or_create_window_is_unique() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        first = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        second = await repo.get_or_create_window(_window(), "2026-08-24T00:01:00Z")
        assert first == second
        assert database.conn.execute("SELECT COUNT(*) FROM collection_windows").fetchone()[0] == 1

    asyncio.run(scenario())


def test_unexpired_window_lease_rejects_second_owner_and_can_be_reclaimed() -> None:
    async def scenario() -> None:
        repo, _ = _repo()
        window_id = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        assert await repo.acquire_window(
            window_id,
            "owner-a",
            "2026-08-24T00:01:00+00:00",
            "2026-08-24T00:16:00+00:00",
        )
        assert not await repo.acquire_window(
            window_id,
            "owner-b",
            "2026-08-24T00:02:00+00:00",
            "2026-08-24T00:17:00+00:00",
        )
        assert await repo.acquire_window(
            window_id,
            "owner-b",
            "2026-08-24T00:17:00+00:00",
            "2026-08-24T00:32:00+00:00",
        )

    asyncio.run(scenario())


def test_staging_rows_are_not_publication_facts() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        window_id = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        run_id = await repo.start_run(window_id, "source", "2026-08-24T00:01:00Z")
        await repo.stage_jobs(run_id, "source", [_job("J1")])

        assert database.conn.execute("SELECT COUNT(*) FROM staged_jobs").fetchone()[0] == 1
        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 0
        assert database.conn.execute("SELECT COUNT(*) FROM job_changes").fetchone()[0] == 0

    asyncio.run(scenario())


def test_complete_snapshot_publishes_once_and_replay_is_idempotent() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        first_window = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        first_run = await repo.start_run(first_window, "source", "2026-08-24T00:01:00Z")
        jobs = [_job("J1"), _job("J2")]
        await repo.stage_jobs(first_run, "source", jobs)
        result = await repo.finalize_snapshot(
            first_run, first_window, "source", _digest(jobs), 2, "2026-08-24T00:02:00Z"
        )
        assert (result.opened, result.updated, result.closed) == (2, 0, 0)

        second_window = await repo.get_or_create_window(_window("afternoon"), "2026-08-24T04:00:00Z")
        second_run = await repo.start_run(second_window, "source", "2026-08-24T04:01:00Z")
        await repo.stage_jobs(second_run, "source", jobs)
        replay = await repo.finalize_snapshot(
            second_run, second_window, "source", _digest(jobs), 2, "2026-08-24T04:02:00Z"
        )
        assert (replay.opened, replay.updated, replay.closed) == (0, 0, 0)
        assert database.conn.execute("SELECT COUNT(*) FROM job_changes").fetchone()[0] == 2

    asyncio.run(scenario())


def test_changed_and_missing_jobs_publish_updated_and_closed() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        first_window = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        first_run = await repo.start_run(first_window, "source", "2026-08-24T00:01:00Z")
        first_jobs = [_job("J1"), _job("J2")]
        await repo.stage_jobs(first_run, "source", first_jobs)
        await repo.finalize_snapshot(
            first_run, first_window, "source", _digest(first_jobs), 2, "2026-08-24T00:02:00Z"
        )

        second_window = await repo.get_or_create_window(_window("afternoon"), "2026-08-24T04:00:00Z")
        second_run = await repo.start_run(second_window, "source", "2026-08-24T04:01:00Z")
        second_jobs = [_job("J1", "高级工程师")]
        await repo.stage_jobs(second_run, "source", second_jobs)
        result = await repo.finalize_snapshot(
            second_run, second_window, "source", _digest(second_jobs), 1, "2026-08-24T04:02:00Z"
        )

        assert (result.opened, result.updated, result.closed) == (0, 1, 1)
        events = database.conn.execute(
            "SELECT kind, external_id FROM job_changes WHERE run_id=? ORDER BY kind",
            (second_run,),
        ).fetchall()
        assert [(row[0], row[1]) for row in events] == [("closed", "J2"), ("updated", "J1")]

    asyncio.run(scenario())


def test_incomplete_staging_cannot_finalize() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        window_id = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        run_id = await repo.start_run(window_id, "source", "2026-08-24T00:01:00Z")
        await repo.stage_jobs(run_id, "source", [_job("J1")])

        with pytest.raises(ValueError, match="staged 1.*expected 2"):
            await repo.finalize_snapshot(
                run_id, window_id, "source", _digest([_job("J1")]), 2, "2026-08-24T00:02:00Z"
            )

        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 0
        assert database.conn.execute("SELECT status FROM source_runs").fetchone()[0] == "running"

    asyncio.run(scenario())


def test_changes_are_cursor_ordered_and_ack_only_moves_forward() -> None:
    async def scenario() -> None:
        repo, _ = _repo()
        window_id = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        run_id = await repo.start_run(window_id, "source", "2026-08-24T00:01:00Z")
        jobs = [_job("J1"), _job("J2")]
        await repo.stage_jobs(run_id, "source", jobs)
        await repo.finalize_snapshot(
            run_id, window_id, "source", _digest(jobs), 2, "2026-08-24T00:02:00Z"
        )

        page = await repo.changes_after(0, limit=1)
        assert [item["cursor"] for item in page["changes"]] == [1]
        assert page["next_cursor"] == 1
        assert page["has_more"] is True
        assert "raw_json" not in page["changes"][0]["job"]

        assert await repo.ack_client("local-mac", 1, "2026-08-24T00:03:00Z") == 1
        assert await repo.ack_client("local-mac", 0, "2026-08-24T00:04:00Z") == 1

    asyncio.run(scenario())
