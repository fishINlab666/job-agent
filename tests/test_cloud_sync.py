from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import httpx

from jobagent import db
from jobagent.cloud_sync import CloudConfig, CloudSync
from jobagent.cloud_sync import CloudAPI


def _payload(title: str = "产品运营") -> dict:
    return {
        "source_key": "tencent_join",
        "external_id": "J1",
        "company": "腾讯",
        "title": title,
        "job_family": "operations",
        "raw_category": None,
        "cities": ["深圳"],
        "raw_location": "深圳",
        "country": "中国",
        "department": "平台",
        "recruit_type": "campus",
        "grad_year": "27",
        "apply_url": "https://join.qq.com/J1",
        "apply_system": "tencent_join",
        "description": "公开岗位描述",
    }


class FakeAPI:
    def __init__(self) -> None:
        self.status_payload = {
            "workday": "2026-08-24",
            "active_window": "morning",
            "windows": [],
        }
        self.pages: dict[int, dict] = {}
        self.catch_up_calls = 0
        self.acks: list[int] = []

    def status(self) -> dict:
        return self.status_payload

    def catch_up(self) -> dict:
        self.catch_up_calls += 1
        return {"status": "complete"}

    def changes(self, after: int) -> dict:
        return self.pages.get(
            after,
            {"changes": [], "next_cursor": after, "has_more": False},
        )

    def ack(self, cursor: int) -> int:
        self.acks.append(cursor)
        return cursor


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "jobagent.db")
    db.init(conn)
    return conn


def _change(cursor: int, kind: str, payload: dict) -> dict:
    return {
        "cursor": cursor,
        "source_key": payload["source_key"],
        "external_id": payload["external_id"],
        "kind": kind,
        "job": payload,
        "occurred_at": f"2026-08-24T0{cursor}:00:00+00:00",
    }


def test_sync_replays_cloud_changes_once_and_updates_existing_job_state(tmp_path) -> None:
    conn = _conn(tmp_path)
    api = FakeAPI()
    api.pages[0] = {
        "changes": [_change(1, "opened", _payload())],
        "next_cursor": 1,
        "has_more": False,
    }
    syncer = CloudSync(conn, api, client_id="local-mac")

    first = syncer.sync()
    assert first == {"applied": 1, "cursor": 1, "affected_sources": 1}
    row = conn.execute(
        "SELECT title, closed_at FROM jobs WHERE source_key='tencent_join' AND external_id='J1'"
    ).fetchone()
    assert tuple(row) == ("产品运营", None)
    assert api.acks == [1]

    replay = syncer.sync()
    assert replay == {"applied": 0, "cursor": 1, "affected_sources": 0}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert api.acks == [1, 1]

    changed = _payload("高级产品运营")
    api.pages[1] = {
        "changes": [_change(2, "updated", changed)],
        "next_cursor": 2,
        "has_more": False,
    }
    assert syncer.sync()["cursor"] == 2
    assert conn.execute("SELECT title FROM jobs").fetchone()[0] == "高级产品运营"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='job_updated'"
    ).fetchone()[0] == 1

    api.pages[2] = {
        "changes": [_change(3, "closed", changed)],
        "next_cursor": 3,
        "has_more": False,
    }
    assert syncer.sync()["cursor"] == 3
    assert conn.execute("SELECT closed_at IS NOT NULL FROM jobs").fetchone()[0] == 1


def test_check_only_requests_catch_up_when_active_window_has_a_gap(tmp_path) -> None:
    conn = _conn(tmp_path)
    api = FakeAPI()
    syncer = CloudSync(conn, api, client_id="local-mac")

    first = syncer.check()
    assert first["catch_up_requested"] is True
    assert api.catch_up_calls == 1

    api.status_payload["windows"] = [
        {"window_key": "morning", "status": "complete", "sources": []}
    ]
    second = syncer.check()
    assert second["catch_up_requested"] is False
    assert api.catch_up_calls == 1


def test_cloud_tables_only_store_public_jobs_and_cursor(tmp_path) -> None:
    conn = _conn(tmp_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cloud_%'"
        )
    }
    assert tables == {"cloud_job_cache", "cloud_sync_state"}
    columns = {
        row[1]
        for table in tables
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    assert not columns & {
        "profile", "resume", "cookie", "session", "application", "submission"
    }


def test_cloud_config_requires_private_file_and_https(tmp_path) -> None:
    path = tmp_path / "cloud.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "https://collector.example.test",
                "token": "x" * 32,
                "client_id": "local-mac",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        CloudConfig.load(path)
    path.chmod(0o600)
    assert CloudConfig.load(path).client_id == "local-mac"

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["base_url"] = "http://collector.example.test"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="HTTPS"):
        CloudConfig.load(path)


def test_cloud_api_sends_bearer_only_to_the_fixed_https_origin() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"workday": "2026-08-24", "windows": []})

    config = CloudConfig(
        "https://collector.example.test", "private-" + "x" * 32, "local-mac"
    )
    api = CloudAPI(config, transport=httpx.MockTransport(handler))
    try:
        assert api.status()["workday"] == "2026-08-24"
    finally:
        api.close()

    assert seen == {
        "url": "https://collector.example.test/v1/status/today",
        "authorization": f"Bearer {config.token}",
    }


def test_sync_rejects_non_advancing_paginated_response(tmp_path) -> None:
    conn = _conn(tmp_path)
    api = FakeAPI()
    api.pages[0] = {"changes": [], "next_cursor": 0, "has_more": True}

    with pytest.raises(ValueError, match="没有推进"):
        CloudSync(conn, api, client_id="local-mac").sync()

    assert api.acks == []
