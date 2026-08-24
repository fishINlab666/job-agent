from __future__ import annotations

import asyncio
import json
import sqlite3
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


async def _finalize(
    repo: D1Repository,
    run_id: int,
    window_id: int,
    source_key: str,
    digest: str,
    count: int,
    now: str,
):
    owner = f"test-owner-{window_id}"
    generation = await repo.acquire_window(
        window_id,
        owner,
        now,
        "2099-01-01T00:00:00+00:00",
    )
    assert generation is not None
    return await repo.finalize_snapshot(
        run_id,
        window_id,
        source_key,
        digest,
        count,
        now,
        lease_owner=owner,
        lease_generation=generation,
        fence_now=now,
    )


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


def test_expired_owner_cannot_publish_after_a_new_owner_reclaims_window() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        window_id = await repo.get_or_create_window(
            _window(), "2026-08-24T00:00:00Z"
        )
        old_generation = await repo.acquire_window(
            window_id,
            "owner-old",
            "2026-08-24T00:01:00+00:00",
            "2026-08-24T00:10:00+00:00",
        )
        assert old_generation == 1
        run_id = await repo.start_run(
            window_id, "source", "2026-08-24T00:02:00Z"
        )
        jobs = [_job("J-old")]
        await repo.stage_jobs(run_id, "source", jobs)

        new_generation = await repo.acquire_window(
            window_id,
            "owner-new",
            "2026-08-24T00:11:00+00:00",
            "2026-08-24T00:30:00+00:00",
        )
        assert new_generation == 2
        with pytest.raises(RuntimeError, match="lease is stale"):
            await repo.finalize_snapshot(
                run_id,
                window_id,
                "source",
                _digest(jobs),
                1,
                "2026-08-24T00:12:00Z",
                lease_owner="owner-old",
                lease_generation=old_generation,
                fence_now="2026-08-24T00:12:00+00:00",
            )

        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 0
        assert database.conn.execute(
            "SELECT status FROM source_runs WHERE id=?", (run_id,)
        ).fetchone()[0] == "running"

    asyncio.run(scenario())


def test_older_window_cannot_overwrite_a_newer_published_snapshot() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        old_window = await repo.get_or_create_window(
            _window("morning"), "2026-08-24T00:00:00Z"
        )
        old_run = await repo.start_run(
            old_window, "source", "2026-08-24T00:01:00Z"
        )
        old_jobs = [_job("J1", "旧标题")]
        await repo.stage_jobs(old_run, "source", old_jobs)

        new_window = await repo.get_or_create_window(
            _window("afternoon"), "2026-08-24T04:00:00Z"
        )
        new_run = await repo.start_run(
            new_window, "source", "2026-08-24T04:01:00Z"
        )
        new_jobs = [_job("J1", "新标题")]
        await repo.stage_jobs(new_run, "source", new_jobs)
        await _finalize(
            repo,
            new_run,
            new_window,
            "source",
            _digest(new_jobs),
            1,
            "2026-08-24T04:02:00Z",
        )

        with pytest.raises(RuntimeError, match="lease is stale"):
            await _finalize(
                repo,
                old_run,
                old_window,
                "source",
                _digest(old_jobs),
                1,
                "2026-08-24T04:03:00Z",
            )

        payload = database.conn.execute(
            "SELECT payload_json FROM cloud_jobs WHERE source_key='source' AND external_id='J1'"
        ).fetchone()[0]
        assert '"title":"新标题"' in payload
        assert database.conn.execute(
            "SELECT COUNT(*) FROM job_changes WHERE run_id=?", (old_run,)
        ).fetchone()[0] == 0

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


def test_incomplete_window_is_marked_missed_after_grace() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        status = await repo.mark_missed(
            _window(), {"source-a", "source-b"}, "2026-08-24T05:00:00+00:00"
        )
        assert status == "missed"
        row = database.conn.execute(
            "SELECT status, last_error FROM collection_windows"
        ).fetchone()
        assert tuple(row) == (
            "missed",
            "collection grace elapsed before all sources succeeded",
        )

    asyncio.run(scenario())


def test_complete_snapshot_publishes_once_and_replay_is_idempotent() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        first_window = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        first_run = await repo.start_run(first_window, "source", "2026-08-24T00:01:00Z")
        jobs = [_job("J1"), _job("J2")]
        await repo.stage_jobs(first_run, "source", jobs)
        result = await _finalize(
            repo, first_run, first_window, "source", _digest(jobs), 2, "2026-08-24T00:02:00Z"
        )
        assert (result.opened, result.updated, result.closed) == (2, 0, 0)

        second_window = await repo.get_or_create_window(_window("afternoon"), "2026-08-24T04:00:00Z")
        second_run = await repo.start_run(second_window, "source", "2026-08-24T04:01:00Z")
        await repo.stage_jobs(second_run, "source", jobs)
        replay = await _finalize(
            repo, second_run, second_window, "source", _digest(jobs), 2, "2026-08-24T04:02:00Z"
        )
        assert (replay.opened, replay.updated, replay.closed) == (0, 0, 0)
        assert database.conn.execute("SELECT COUNT(*) FROM job_changes").fetchone()[0] == 2

    asyncio.run(scenario())


def test_database_rejects_a_second_success_for_the_same_window_and_source() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        window_id = await repo.get_or_create_window(
            _window(), "2026-08-24T00:00:00Z"
        )
        first_run = await repo.start_run(
            window_id, "source", "2026-08-24T00:01:00Z"
        )
        first = [_job("J1")]
        await repo.stage_jobs(first_run, "source", first)
        await _finalize(
            repo,
            first_run,
            window_id,
            "source",
            _digest(first),
            1,
            "2026-08-24T00:02:00Z",
        )

        second_run = await repo.start_run(
            window_id, "source", "2026-08-24T00:03:00Z"
        )
        second = [_job("J2")]
        await repo.stage_jobs(second_run, "source", second)
        with pytest.raises(sqlite3.IntegrityError):
            await _finalize(
                repo,
                second_run,
                window_id,
                "source",
                _digest(second),
                1,
                "2026-08-24T00:04:00Z",
            )

        assert database.conn.execute(
            "SELECT COUNT(*) FROM source_runs WHERE window_id=? AND source_key=? "
            "AND status='success'",
            (window_id, "source"),
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_changed_and_missing_jobs_publish_updated_and_closed() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        first_window = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        first_run = await repo.start_run(first_window, "source", "2026-08-24T00:01:00Z")
        first_jobs = [_job("J1"), _job("J2")]
        await repo.stage_jobs(first_run, "source", first_jobs)
        await _finalize(
            repo, first_run, first_window, "source", _digest(first_jobs), 2, "2026-08-24T00:02:00Z"
        )

        second_window = await repo.get_or_create_window(_window("afternoon"), "2026-08-24T04:00:00Z")
        second_run = await repo.start_run(second_window, "source", "2026-08-24T04:01:00Z")
        second_jobs = [_job("J1", "高级工程师")]
        await repo.stage_jobs(second_run, "source", second_jobs)
        result = await _finalize(
            repo, second_run, second_window, "source", _digest(second_jobs), 1, "2026-08-24T04:02:00Z"
        )

        assert (result.opened, result.updated, result.closed) == (0, 1, 1)
        events = database.conn.execute(
            "SELECT kind, external_id FROM job_changes WHERE run_id=? ORDER BY kind",
            (second_run,),
        ).fetchall()
        assert [(row[0], row[1]) for row in events] == [("closed", "J2"), ("updated", "J1")]

    asyncio.run(scenario())


def test_large_snapshot_shrink_is_rejected_before_cloud_closures_publish() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        first_window = await repo.get_or_create_window(
            _window(), "2026-08-24T00:00:00Z"
        )
        first_run = await repo.start_run(
            first_window, "source", "2026-08-24T00:01:00Z"
        )
        initial = [_job(f"J{index}") for index in range(10)]
        await repo.stage_jobs(first_run, "source", initial)
        await _finalize(
            repo,
            first_run,
            first_window,
            "source",
            _digest(initial),
            len(initial),
            "2026-08-24T00:02:00Z",
        )

        second_window = await repo.get_or_create_window(
            _window("afternoon"), "2026-08-24T04:00:00Z"
        )
        second_run = await repo.start_run(
            second_window, "source", "2026-08-24T04:01:00Z"
        )
        truncated = initial[:2]
        await repo.stage_jobs(second_run, "source", truncated)
        with pytest.raises(RuntimeError, match="关闭守卫触发"):
            await _finalize(
                repo,
                second_run,
                second_window,
                "source",
                _digest(truncated),
                len(truncated),
                "2026-08-24T04:02:00Z",
            )

        assert database.conn.execute(
            "SELECT COUNT(*) FROM cloud_jobs WHERE closed_at IS NULL"
        ).fetchone()[0] == 10
        assert database.conn.execute(
            "SELECT COUNT(*) FROM job_changes WHERE kind='closed'"
        ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_close_guard_uses_the_same_transaction_snapshot_as_publication() -> None:
    class InterleavingD1(FakeD1):
        inject_before_publication = False

        async def batch(self, statements):
            if self.inject_before_publication and any(
                "INSERT INTO source_heads" in statement.sql
                for statement in statements
            ):
                self.inject_before_publication = False
                for index in range(4, 14):
                    job = _job(f"J{index}")
                    self.conn.execute(
                        """INSERT INTO cloud_jobs(
                               source_key, external_id, company, fingerprint,
                               payload_json, first_seen_at, last_seen_at, closed_at
                           ) VALUES('source', ?, '公司', ?, ?, 't0', 't0', NULL)""",
                        (
                            job.external_id,
                            job.fingerprint,
                            json.dumps(job.payload, ensure_ascii=False),
                        ),
                    )
                self.conn.commit()
            return await super().batch(statements)

    async def scenario() -> None:
        database = InterleavingD1(MIGRATION)
        repo = D1Repository(database)
        for index in range(4):
            job = _job(f"J{index}")
            database.conn.execute(
                """INSERT INTO cloud_jobs(
                       source_key, external_id, company, fingerprint,
                       payload_json, first_seen_at, last_seen_at, closed_at
                   ) VALUES('source', ?, '公司', ?, ?, 't0', 't0', NULL)""",
                (
                    job.external_id,
                    job.fingerprint,
                    json.dumps(job.payload, ensure_ascii=False),
                ),
            )
        database.conn.commit()
        window_id = await repo.get_or_create_window(
            _window("afternoon"), "2026-08-24T04:00:00Z"
        )
        run_id = await repo.start_run(
            window_id, "source", "2026-08-24T04:01:00Z"
        )
        staged = [_job("J0"), _job("J1")]
        await repo.stage_jobs(run_id, "source", staged)
        database.inject_before_publication = True

        with pytest.raises(RuntimeError, match="关闭守卫触发：12/14"):
            await _finalize(
                repo,
                run_id,
                window_id,
                "source",
                _digest(staged),
                2,
                "2026-08-24T04:02:00Z",
            )

        assert database.conn.execute(
            "SELECT COUNT(*) FROM cloud_jobs WHERE closed_at IS NULL"
        ).fetchone()[0] == 14
        assert database.conn.execute(
            "SELECT COUNT(*) FROM job_changes WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0
        assert database.conn.execute(
            "SELECT COUNT(*) FROM source_heads WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_incomplete_staging_cannot_finalize() -> None:
    async def scenario() -> None:
        repo, database = _repo()
        window_id = await repo.get_or_create_window(_window(), "2026-08-24T00:00:00Z")
        run_id = await repo.start_run(window_id, "source", "2026-08-24T00:01:00Z")
        await repo.stage_jobs(run_id, "source", [_job("J1")])

        with pytest.raises(ValueError, match="staged 1.*expected 2"):
            await _finalize(
                repo, run_id, window_id, "source", _digest([_job("J1")]), 2, "2026-08-24T00:02:00Z"
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
        await _finalize(
            repo, run_id, window_id, "source", _digest(jobs), 2, "2026-08-24T00:02:00Z"
        )

        page = await repo.changes_after(0, limit=1)
        assert [item["cursor"] for item in page["changes"]] == [1]
        assert page["next_cursor"] == 1
        assert page["has_more"] is True
        assert "raw_json" not in page["changes"][0]["job"]

        assert await repo.ack_client("local-mac", 1, "2026-08-24T00:03:00Z") == 1
        assert await repo.ack_client("local-mac", 0, "2026-08-24T00:04:00Z") == 1

    asyncio.run(scenario())
