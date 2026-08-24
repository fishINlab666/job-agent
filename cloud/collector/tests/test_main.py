from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from pathlib import Path

from cloud.collector.src.main import Default, route_request
from cloud.collector.src.repository import D1Repository
from cloud.collector.tests.fakes import FakeD1


MIGRATION = Path(__file__).parents[1] / "migrations" / "0001_init.sql"


def test_python_cron_handler_uses_the_required_runtime_signature() -> None:
    assert list(inspect.signature(Default.scheduled).parameters) == [
        "self", "controller", "env", "ctx"
    ]


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class FakeRequest:
    def __init__(self, method: str, path: str, *, token: str | None = None, body=None) -> None:
        self.method = method
        self.url = f"https://collector.test{path}"
        self.headers = FakeHeaders()
        if token is not None:
            self.headers["authorization"] = f"Bearer {token}"
        self._body = {} if body is None else body

    async def json(self):
        return self._body


class FakeEnv:
    def __init__(self, database) -> None:
        self.DB = database
        self.JOBAGENT_SYNC_TOKEN = "correct-token"


def test_unauthenticated_requests_are_rejected() -> None:
    async def scenario() -> None:
        database = FakeD1(MIGRATION)
        payload, status = await route_request(
            FakeRequest("GET", "/v1/status/today"),
            FakeEnv(database),
            now=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
        )
        assert status == 401
        assert payload == {"error": "unauthorized"}

        for method, path, body in (
            ("GET", "/v1/changes?after=0", None),
            ("POST", "/v1/clients/local-mac/ack", {"cursor": 0}),
            ("POST", "/v1/catch-up", {}),
        ):
            payload, status = await route_request(
                FakeRequest(method, path, body=body), FakeEnv(database)
            )
            assert status == 401

    asyncio.run(scenario())


def test_catch_up_rejects_job_payloads() -> None:
    async def scenario() -> None:
        database = FakeD1(MIGRATION)
        payload, status = await route_request(
            FakeRequest(
                "POST",
                "/v1/catch-up",
                token="correct-token",
                body={"jobs": [{"external_id": "must-not-be-accepted"}]},
            ),
            FakeEnv(database),
            now=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
        )
        assert status == 400
        assert payload == {"error": "catch-up body must be an empty object"}
        assert database.conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 0

    asyncio.run(scenario())


def test_status_is_read_only() -> None:
    async def scenario() -> None:
        database = FakeD1(MIGRATION)
        repo = D1Repository(database)
        before = database.conn.total_changes
        payload, status = await route_request(
            FakeRequest("GET", "/v1/status/today", token="correct-token"),
            FakeEnv(database),
            now=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
            repository=repo,
        )
        assert status == 200
        assert payload == {"workday": "2026-08-24", "windows": []}
        assert database.conn.total_changes == before

    asyncio.run(scenario())


def test_changes_and_ack_have_strict_shapes() -> None:
    async def scenario() -> None:
        database = FakeD1(MIGRATION)
        env = FakeEnv(database)
        payload, status = await route_request(
            FakeRequest("GET", "/v1/changes?after=0", token="correct-token"), env
        )
        assert status == 200
        assert payload == {"changes": [], "next_cursor": 0, "has_more": False}

        payload, status = await route_request(
            FakeRequest(
                "POST",
                "/v1/clients/local-mac/ack",
                token="correct-token",
                body={"cursor": 0},
            ),
            env,
        )
        assert status == 200
        assert payload == {"client_id": "local-mac", "acknowledged_cursor": 0}

        payload, status = await route_request(
            FakeRequest(
                "POST",
                "/v1/clients/../../escape/ack",
                token="correct-token",
                body={"cursor": 0},
            ),
            env,
        )
        assert status == 404

    asyncio.run(scenario())


def test_technical_trial_is_explicit_and_does_not_backfill_a_real_window(monkeypatch) -> None:
    class FakeCollector:
        def __init__(self, repository) -> None:
            self.repository = repository

        async def run_window(self, window, *, trigger):
            return {"window_key": window.key, "trigger": trigger}

    async def scenario() -> None:
        database = FakeD1(MIGRATION)
        monkeypatch.setattr("cloud.collector.src.main.Collector", FakeCollector)
        payload, status = await route_request(
            FakeRequest(
                "POST", "/v1/technical-trial", token="correct-token", body={}
            ),
            FakeEnv(database),
            now=datetime(2026, 8, 23, 1, tzinfo=timezone.utc),
        )
        assert status == 200
        assert payload == {"window_key": "technical-trial", "trigger": "technical-trial"}

    asyncio.run(scenario())
