from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from cloud.collector.src.main import route_request
from cloud.collector.src.repository import D1Repository
from cloud.collector.tests.fakes import FakeD1
from cloud.collector.tests.test_main import FakeEnv, FakeRequest
from jobagent.adapters.base import RawJob
from jobagent.collection import payload_fingerprint, snapshot_digest, to_public_payload
from jobagent.targets import OBSERVATION_SOURCES


MIGRATION_1 = Path(__file__).parents[1] / "migrations" / "0001_init.sql"
MIGRATION_2 = Path(__file__).parents[1] / "migrations" / "0002_remote_ingest.sql"
NOW = datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc)


def _database() -> FakeD1:
    database = FakeD1(MIGRATION_1)
    database.conn.executescript(MIGRATION_2.read_text(encoding="utf-8"))
    return database


def _payload(external_id: str = "J1") -> dict:
    spec = OBSERVATION_SOURCES[0]
    return to_public_payload(
        str(spec["source_key"]),
        str(spec["company"]),
        RawJob(
            external_id=external_id,
            title="工程师",
            raw_json={},
            apply_url=f"https://example.test/{external_id}",
        ),
    )


def _begin_body(
    payloads: list[dict],
    *,
    session_id: str = "a" * 64,
    collection_id: str = "c" * 64,
    source_index: int = 0,
) -> dict:
    return {
        "session_id": session_id,
        "collection_id": collection_id,
        "source_key": str(OBSERVATION_SOURCES[source_index]["source_key"]),
        "mode": "official",
        "expected_count": len(payloads),
        "expected_bytes": sum(
            len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for payload in payloads
        ),
        "snapshot_sha256": snapshot_digest(payloads),
    }


def test_ingest_and_sync_credentials_are_not_interchangeable() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        body = _begin_body([_payload()])

        _payload1, status1 = await route_request(
            FakeRequest("POST", "/v1/ingest/sessions", token="correct-token", body=body),
            env,
            now=NOW,
        )
        assert status1 == 401

        _payload2, status2 = await route_request(
            FakeRequest("GET", "/v1/status/today", token="ingest-token"),
            env,
            now=NOW,
        )
        assert status2 == 401

    asyncio.run(scenario())


def test_complete_chunked_snapshot_can_publish_once() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        jobs = [_payload("J1"), _payload("J2")]
        begin_body = _begin_body(jobs)

        begin, status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=begin_body
            ),
            env,
            now=NOW,
        )
        assert status == 201
        assert begin["status"] == "pending"

        chunk_body = {"chunk_sha256": snapshot_digest(jobs), "jobs": jobs}
        chunk, status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{begin_body['session_id']}/chunks/0",
                token="ingest-token",
                body=chunk_body,
            ),
            env,
            now=NOW,
        )
        assert status == 200
        assert chunk == {"chunk_index": 0, "row_count": 2, "status": "stored"}

        committed, status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{begin_body['session_id']}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=NOW,
        )
        assert status == 200
        assert committed["status"] == "committed"
        assert committed["fetched_count"] == 2
        assert database.conn.execute(
            "SELECT COUNT(*) FROM cloud_jobs WHERE closed_at IS NULL"
        ).fetchone()[0] == 2
        assert database.conn.execute(
            "SELECT COUNT(*) FROM source_runs WHERE status='success'"
        ).fetchone()[0] == 1

        later_jobs = [_payload("J3")]
        later_body = _begin_body(later_jobs, session_id="b" * 64)
        already, replay_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=later_body
            ),
            env,
            now=NOW,
        )
        assert replay_status == 200
        assert already["status"] == "committed"
        assert already["expected_count"] == 2
        assert already["snapshot_sha256"] == begin_body["snapshot_sha256"]
        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 2
        assert tuple(
            database.conn.execute(
                "SELECT status, lease_owner FROM collection_windows"
            ).fetchone()
        ) == ("partial", None)

    asyncio.run(scenario())


def test_each_technical_trial_collection_gets_its_own_window() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        jobs = [_payload("OLD")]
        first_body = _begin_body(
            jobs, session_id="1" * 64, collection_id="a" * 64
        )
        first_body["mode"] = "technical-trial"
        first, status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=first_body
            ),
            env,
            now=NOW,
        )
        assert status == 201
        await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{first_body['session_id']}/chunks/0",
                token="ingest-token",
                body={"chunk_sha256": snapshot_digest(jobs), "jobs": jobs},
            ),
            env,
            now=NOW,
        )
        committed, status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{first_body['session_id']}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=NOW,
        )
        assert status == 200
        assert committed["snapshot_sha256"] == snapshot_digest(jobs)

        fresh_jobs = [_payload("NEW1"), _payload("NEW2")]
        second_body = _begin_body(
            fresh_jobs, session_id="2" * 64, collection_id="b" * 64
        )
        second_body["mode"] = "technical-trial"
        second, second_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=second_body
            ),
            env,
            now=NOW,
        )

        assert second_status == 201
        assert second["status"] == "pending"
        assert second["window_id"] != first["window_id"]
        assert database.conn.execute(
            "SELECT COUNT(DISTINCT window_id) FROM remote_collection_rounds"
        ).fetchone()[0] == 2

    asyncio.run(scenario())


def test_one_collection_keeps_one_server_window_across_noon() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        first_jobs = [_payload("J1")]
        first, first_status = await route_request(
            FakeRequest(
                "POST",
                "/v1/ingest/sessions",
                token="ingest-token",
                body=_begin_body(first_jobs, session_id="1" * 64),
            ),
            env,
            now=datetime(2026, 8, 24, 3, 59, tzinfo=timezone.utc),
        )
        assert first_status == 201
        chunk_body = {
            "chunk_sha256": snapshot_digest(first_jobs),
            "jobs": first_jobs,
        }
        _chunk, chunk_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{'1' * 64}/chunks/0",
                token="ingest-token",
                body=chunk_body,
            ),
            env,
            now=datetime(2026, 8, 24, 3, 59, tzinfo=timezone.utc),
        )
        assert chunk_status == 200
        _commit, commit_status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{'1' * 64}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=datetime(2026, 8, 24, 3, 59, tzinfo=timezone.utc),
        )
        assert commit_status == 200

        second_payload = to_public_payload(
            str(OBSERVATION_SOURCES[1]["source_key"]),
            str(OBSERVATION_SOURCES[1]["company"]),
            RawJob(
                external_id="J2",
                title="工程师",
                raw_json={},
                apply_url="https://example.test/J2",
            ),
        )
        second, second_status = await route_request(
            FakeRequest(
                "POST",
                "/v1/ingest/sessions",
                token="ingest-token",
                body=_begin_body(
                    [second_payload],
                    session_id="2" * 64,
                    source_index=1,
                ),
            ),
            env,
            now=datetime(2026, 8, 24, 4, 1, tzinfo=timezone.utc),
        )

        assert second_status == 201
        assert second["window_id"] == first["window_id"]
        assert second["window_key"] == first["window_key"] == "morning"
        assert database.conn.execute(
            "SELECT COUNT(DISTINCT window_id) FROM remote_ingest_sessions"
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_repeated_begin_is_idempotent_and_creates_one_run() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        body = _begin_body([_payload()])

        (first, first_status), (second, second_status) = await asyncio.gather(
            route_request(
                FakeRequest(
                    "POST", "/v1/ingest/sessions", token="ingest-token", body=body
                ),
                env,
                now=NOW,
            ),
            route_request(
                FakeRequest(
                    "POST", "/v1/ingest/sessions", token="ingest-token", body=body
                ),
                env,
                now=NOW,
            ),
        )

        assert sorted((first_status, second_status)) == [200, 201]
        assert second == first
        assert database.conn.execute(
            "SELECT COUNT(*) FROM source_runs"
        ).fetchone()[0] == 1
        assert database.conn.execute(
            "SELECT COUNT(*) FROM remote_ingest_sessions"
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_mode_drift_is_rejected_before_mutating_the_window_lease() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        first_body = _begin_body([_payload()])
        _first, first_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=first_body
            ),
            env,
            now=NOW,
        )
        assert first_status == 201
        before = tuple(
            database.conn.execute(
                "SELECT lease_owner, lease_generation FROM collection_windows"
            ).fetchone()
        )

        drift = _begin_body(
            [_payload()], session_id="b" * 64, collection_id=first_body["collection_id"]
        )
        drift["mode"] = "technical-trial"
        payload, status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=drift
            ),
            env,
            now=NOW,
        )

        assert status == 409
        assert "不同模式" in payload["error"]
        assert tuple(
            database.conn.execute(
                "SELECT lease_owner, lease_generation FROM collection_windows"
            ).fetchone()
        ) == before
        assert database.conn.execute(
            "SELECT COUNT(*) FROM remote_ingest_sessions"
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_commit_is_atomic_and_replay_recovers_after_in_batch_failure() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        jobs = [_payload()]
        body = _begin_body(jobs)
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=body
            ),
            env,
            now=NOW,
        )
        assert begin_status == 201
        _chunk, chunk_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{body['session_id']}/chunks/0",
                token="ingest-token",
                body={"chunk_sha256": snapshot_digest(jobs), "jobs": jobs},
            ),
            env,
            now=NOW,
        )
        assert chunk_status == 200

        original_batch = database.batch

        async def fail_inside_atomic_commit(statements):
            if any("UPDATE remote_ingest_sessions" in item.sql for item in statements):
                statements = [*statements]
                statements.insert(-1, database.prepare("SELECT no_such_function()"))
            return await original_batch(statements)

        database.batch = fail_inside_atomic_commit
        with pytest.raises(Exception, match="no such function"):
            await route_request(
                FakeRequest(
                    "POST",
                    f"/v1/ingest/sessions/{body['session_id']}/commit",
                    token="ingest-token",
                    body={},
                ),
                env,
                now=NOW,
            )
        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 0
        assert database.conn.execute(
            "SELECT status FROM remote_ingest_sessions"
        ).fetchone()[0] == "committing"
        assert database.conn.execute(
            "SELECT status FROM source_runs"
        ).fetchone()[0] == "running"

        database.batch = original_batch
        committed, status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{body['session_id']}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=NOW,
        )
        assert status == 200
        assert committed["status"] == "committed"
        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 1

    asyncio.run(scenario())


def test_server_enforces_five_source_total_limit() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        first_body = _begin_body([_payload()], session_id="1" * 64)
        first_body["expected_count"] = 20_000
        first_body["snapshot_sha256"] = "1" * 64
        _first, first_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=first_body
            ),
            env,
            now=NOW,
        )
        assert first_status == 201

        # 模拟第一来源已发布，随后同一 collection 再声明超过 30,000 的总量。
        database.conn.execute(
            "UPDATE source_runs SET status='success', fetched_count=20000 WHERE id=1"
        )
        database.conn.execute(
            "UPDATE remote_ingest_sessions SET status='committed' WHERE id=?",
            (first_body["session_id"],),
        )
        database.conn.execute(
            "UPDATE collection_windows SET lease_owner=NULL, lease_expires_at=NULL"
        )
        database.conn.commit()
        second_payload = to_public_payload(
            str(OBSERVATION_SOURCES[1]["source_key"]),
            str(OBSERVATION_SOURCES[1]["company"]),
            RawJob(
                external_id="J2",
                title="工程师",
                raw_json={},
                apply_url="https://example.test/J2",
            ),
        )
        second_body = _begin_body(
            [second_payload],
            session_id="2" * 64,
            collection_id="d" * 64,
            source_index=1,
        )
        second_body["expected_count"] = 10_001
        second_body["snapshot_sha256"] = "2" * 64
        payload, status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=second_body
            ),
            env,
            now=NOW,
        )

        assert status == 409
        assert "安全上限" in payload["error"]
        assert database.conn.execute(
            "SELECT COUNT(*) FROM remote_ingest_sessions"
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_single_job_larger_than_d1_row_limit_is_rejected_before_staging() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        job = _payload()
        job["description"] = "x" * 1_000_001
        begin_body = _begin_body([job])
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=begin_body
            ),
            env,
            now=NOW,
        )
        assert begin_status == 201

        payload, status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{begin_body['session_id']}/chunks/0",
                token="ingest-token",
                body={"chunk_sha256": snapshot_digest([job]), "jobs": [job]},
            ),
            env,
            now=NOW,
        )

        assert status == 400
        assert "单岗位" in payload["error"]
        assert database.conn.execute("SELECT COUNT(*) FROM staged_jobs").fetchone()[0] == 0

    asyncio.run(scenario())


def test_maximum_chunk_stays_within_free_d1_batch_limit() -> None:
    class CountingD1(FakeD1):
        max_batch = 0

        async def batch(self, statements):
            self.max_batch = max(self.max_batch, len(statements))
            return await super().batch(statements)

    async def scenario() -> None:
        database = CountingD1(MIGRATION_1)
        database.conn.executescript(MIGRATION_2.read_text(encoding="utf-8"))
        jobs = [_payload(f"J{index}") for index in range(40)]
        body = _begin_body(jobs)
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=body
            ),
            FakeEnv(database),
            now=NOW,
        )
        assert begin_status == 201
        _chunk, chunk_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{body['session_id']}/chunks/0",
                token="ingest-token",
                body={"chunk_sha256": snapshot_digest(jobs), "jobs": jobs},
            ),
            FakeEnv(database),
            now=NOW,
        )

        assert chunk_status == 200
        assert database.max_batch == 42
        assert database.max_batch <= 50

    asyncio.run(scenario())


def test_expired_session_is_failed_and_staged_rows_are_cleaned() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        jobs = [_payload()]
        body = _begin_body(jobs)
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=body
            ),
            env,
            now=NOW,
        )
        assert begin_status == 201
        _chunk, chunk_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{body['session_id']}/chunks/0",
                token="ingest-token",
                body={"chunk_sha256": snapshot_digest(jobs), "jobs": jobs},
            ),
            env,
            now=NOW,
        )
        assert chunk_status == 200

        database.conn.execute(
            "UPDATE remote_ingest_sessions SET expires_at='2026-08-24T01:00:00+00:00'"
        )
        database.conn.commit()
        next_body = _begin_body(
            jobs, session_id="e" * 64, collection_id="f" * 64
        )
        _next, _status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=next_body
            ),
            env,
            now=datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc),
        )

        assert database.conn.execute(
            "SELECT status FROM remote_ingest_sessions WHERE id=?",
            (body["session_id"],),
        ).fetchone()[0] == "failed"
        assert database.conn.execute(
            "SELECT status FROM source_runs WHERE remote_session_id=?",
            (body["session_id"],),
        ).fetchone()[0] == "failed"
        assert database.conn.execute(
            "SELECT COUNT(*) FROM staged_jobs WHERE run_id=1"
        ).fetchone()[0] == 0
        assert database.conn.execute(
            "SELECT COUNT(*) FROM remote_ingest_chunks WHERE session_id=?",
            (body["session_id"],),
        ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_expired_failed_session_does_not_consume_replacement_capacity() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        first = _begin_body([_payload()], session_id="1" * 64, collection_id="a" * 64)
        first["expected_count"] = 20_000
        first["snapshot_sha256"] = "1" * 64
        _payload1, first_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=first
            ),
            env,
            now=NOW,
        )
        assert first_status == 201
        database.conn.execute(
            "UPDATE remote_ingest_sessions SET expires_at='2026-08-24T01:31:00+00:00'"
        )
        database.conn.execute(
            "UPDATE remote_collection_rounds SET expires_at='2026-08-24T01:31:00+00:00'"
        )
        database.conn.commit()

        replacement = _begin_body(
            [_payload("NEW")], session_id="2" * 64, collection_id="b" * 64
        )
        replacement["expected_count"] = 11_000
        replacement["snapshot_sha256"] = "2" * 64
        created, created_status = await route_request(
            FakeRequest(
                "POST",
                "/v1/ingest/sessions",
                token="ingest-token",
                body=replacement,
            ),
            env,
            now=datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc),
        )

        assert created_status == 201
        assert created["status"] == "pending"
        assert database.conn.execute(
            "SELECT status FROM remote_ingest_sessions WHERE id=?",
            (first["session_id"],),
        ).fetchone()[0] == "failed"

    asyncio.run(scenario())


def test_round_expiry_invalidates_a_later_source_session() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        first_jobs = [_payload("J1")]
        first = _begin_body(first_jobs, session_id="1" * 64)
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=first
            ),
            env,
            now=NOW,
        )
        assert begin_status == 201
        await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{first['session_id']}/chunks/0",
                token="ingest-token",
                body={
                    "chunk_sha256": snapshot_digest(first_jobs),
                    "jobs": first_jobs,
                },
            ),
            env,
            now=NOW,
        )
        _committed, commit_status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{first['session_id']}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=NOW,
        )
        assert commit_status == 200

        spec = OBSERVATION_SOURCES[1]
        second_job = to_public_payload(
            str(spec["source_key"]),
            str(spec["company"]),
            RawJob(
                external_id="J2",
                title="工程师",
                raw_json={},
                apply_url="https://example.test/J2",
            ),
        )
        second = _begin_body(
            [second_job],
            session_id="2" * 64,
            collection_id=first["collection_id"],
            source_index=1,
        )
        late_now = datetime(2026, 8, 24, 1, 54, tzinfo=timezone.utc)
        _late, late_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=second
            ),
            env,
            now=late_now,
        )
        assert late_status == 201
        assert database.conn.execute(
            "SELECT expires_at FROM remote_ingest_sessions WHERE id=?",
            (second["session_id"],),
        ).fetchone()[0] == "2026-08-24T01:55:00+00:00"

        expired_now = datetime(2026, 8, 24, 1, 56, tzinfo=timezone.utc)
        await D1Repository(database).expire_remote_sessions(expired_now.isoformat())
        assert database.conn.execute(
            "SELECT status FROM remote_collection_rounds"
        ).fetchone()[0] == "failed"
        assert database.conn.execute(
            "SELECT status FROM remote_ingest_sessions WHERE id=?",
            (second["session_id"],),
        ).fetchone()[0] == "failed"

        chunk, chunk_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{second['session_id']}/chunks/0",
                token="ingest-token",
                body={
                    "chunk_sha256": snapshot_digest([second_job]),
                    "jobs": [second_job],
                },
            ),
            env,
            now=expired_now,
        )
        assert chunk_status == 409
        assert "pending" in chunk["error"]
        assert database.conn.execute(
            "SELECT COUNT(*) FROM remote_ingest_chunks WHERE session_id=?",
            (second["session_id"],),
        ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_half_snapshot_and_chunk_drift_never_publish() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        jobs = [_payload("J1"), _payload("J2")]
        begin_body = _begin_body(jobs)
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=begin_body
            ),
            env,
            now=NOW,
        )
        assert begin_status == 201

        first = [jobs[0]]
        chunk_body = {"chunk_sha256": snapshot_digest(first), "jobs": first}
        path = f"/v1/ingest/sessions/{begin_body['session_id']}/chunks/0"
        _chunk, chunk_status = await route_request(
            FakeRequest("PUT", path, token="ingest-token", body=chunk_body),
            env,
            now=NOW,
        )
        assert chunk_status == 200

        replay, replay_status = await route_request(
            FakeRequest("PUT", path, token="ingest-token", body=chunk_body),
            env,
            now=NOW,
        )
        assert replay_status == 200
        assert replay["status"] == "already-stored"

        drift = {"chunk_sha256": snapshot_digest([jobs[1]]), "jobs": [jobs[1]]}
        _drift, drift_status = await route_request(
            FakeRequest("PUT", path, token="ingest-token", body=drift),
            env,
            now=NOW,
        )
        assert drift_status == 409

        _commit, commit_status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{begin_body['session_id']}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=NOW,
        )
        assert commit_status == 409
        assert database.conn.execute("SELECT COUNT(*) FROM cloud_jobs").fetchone()[0] == 0
        assert database.conn.execute(
            "SELECT COUNT(*) FROM source_runs WHERE status='success'"
        ).fetchone()[0] == 0
        late, late_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{begin_body['session_id']}/chunks/1",
                token="ingest-token",
                body={
                    "chunk_sha256": snapshot_digest([jobs[1]]),
                    "jobs": [jobs[1]],
                },
            ),
            env,
            now=NOW,
        )
        assert late_status == 409
        assert "pending" in late["error"]
        assert database.conn.execute(
            "SELECT COUNT(*) FROM remote_ingest_chunks"
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_close_guard_cannot_mark_remote_session_committed() -> None:
    async def scenario() -> None:
        database = _database()
        env = FakeEnv(database)
        existing = [_payload(f"J{index}") for index in range(10)]
        for job in existing:
            encoded = json.dumps(
                job, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            database.conn.execute(
                """INSERT INTO cloud_jobs(
                       source_key, external_id, company, fingerprint, payload_json,
                       first_seen_at, last_seen_at, closed_at
                   ) VALUES(?,?,?,?,?,'t0','t0',NULL)""",
                (
                    job["source_key"],
                    job["external_id"],
                    job["company"],
                    payload_fingerprint(job),
                    encoded,
                ),
            )
        database.conn.commit()

        truncated = [existing[0]]
        body = _begin_body(truncated)
        _begin, begin_status = await route_request(
            FakeRequest(
                "POST", "/v1/ingest/sessions", token="ingest-token", body=body
            ),
            env,
            now=NOW,
        )
        assert begin_status == 201
        _chunk, chunk_status = await route_request(
            FakeRequest(
                "PUT",
                f"/v1/ingest/sessions/{body['session_id']}/chunks/0",
                token="ingest-token",
                body={
                    "chunk_sha256": snapshot_digest(truncated),
                    "jobs": truncated,
                },
            ),
            env,
            now=NOW,
        )
        assert chunk_status == 200

        result, commit_status = await route_request(
            FakeRequest(
                "POST",
                f"/v1/ingest/sessions/{body['session_id']}/commit",
                token="ingest-token",
                body={},
            ),
            env,
            now=NOW,
        )

        assert commit_status == 409
        assert "关闭守卫" in result["error"]
        assert database.conn.execute(
            "SELECT status FROM remote_ingest_sessions"
        ).fetchone()[0] == "committing"
        assert database.conn.execute(
            "SELECT status FROM source_runs"
        ).fetchone()[0] == "running"
        assert database.conn.execute(
            "SELECT COUNT(*) FROM cloud_jobs WHERE closed_at IS NULL"
        ).fetchone()[0] == 10
        assert database.conn.execute("SELECT COUNT(*) FROM job_changes").fetchone()[0] == 0

    asyncio.run(scenario())


def test_remote_payload_extra_fields_and_wrong_identity_are_rejected() -> None:
    async def scenario() -> None:
        for broken in (
            {**_payload(), "private": "must-not-enter-d1"},
            {**_payload(), "source_key": "unapproved"},
            "not-an-object",
        ):
            database = _database()
            env = FakeEnv(database)
            begin_body = _begin_body([_payload()])
            await route_request(
                FakeRequest(
                    "POST", "/v1/ingest/sessions", token="ingest-token", body=begin_body
                ),
                env,
                now=NOW,
            )
            _result, status = await route_request(
                FakeRequest(
                    "PUT",
                    f"/v1/ingest/sessions/{begin_body['session_id']}/chunks/0",
                    token="ingest-token",
                    body={
                        "chunk_sha256": (
                            snapshot_digest([broken])
                            if isinstance(broken, dict)
                            else "f" * 64
                        ),
                        "jobs": [broken],
                    },
                ),
                env,
                now=NOW,
            )
            assert status == 400
            assert database.conn.execute("SELECT COUNT(*) FROM staged_jobs").fetchone()[0] == 0

    asyncio.run(scenario())


def test_official_ingest_is_rejected_outside_a_workday_window() -> None:
    async def scenario() -> None:
        database = _database()
        payload, status = await route_request(
            FakeRequest(
                "POST",
                "/v1/ingest/sessions",
                token="ingest-token",
                body=_begin_body([_payload()]),
            ),
            FakeEnv(database),
            now=datetime(2026, 8, 23, 1, 30, tzinfo=timezone.utc),
        )
        assert status == 409
        assert payload == {"error": "no active or just-ended collection window"}
        assert database.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0

    asyncio.run(scenario())


def test_next_workday_marks_missing_evening_window_as_missed() -> None:
    async def scenario() -> None:
        database = _database()
        database.conn.execute(
            """INSERT INTO collection_windows(
                   workday, window_key, opens_at, closes_at, status, created_at,
                   completed_at
               ) VALUES(
                   '2026-08-24','morning',
                   '2026-08-24T08:00:00+08:00','2026-08-24T12:00:00+08:00',
                   'complete','2026-08-24T01:00:00+00:00','2026-08-24T04:00:00+00:00'
               )"""
        )
        database.conn.commit()

        _created, status = await route_request(
            FakeRequest(
                "POST",
                "/v1/ingest/sessions",
                token="ingest-token",
                body=_begin_body([_payload()], collection_id="d" * 64),
            ),
            FakeEnv(database),
            now=datetime(2026, 8, 25, 1, 30, tzinfo=timezone.utc),
        )

        assert status == 201
        rows = database.conn.execute(
            """SELECT window_key, status FROM collection_windows
               WHERE workday='2026-08-24' ORDER BY opens_at"""
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            ("morning", "complete"),
            ("afternoon", "missed"),
            ("evening", "missed"),
        ]

    asyncio.run(scenario())
