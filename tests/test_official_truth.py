from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from jobagent import db, official_truth, observation
from jobagent.targets import OBSERVATION_SOURCES


def _factory(handler):
    transport = httpx.MockTransport(handler)

    def create(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    return create


def test_tencent_official_snapshot_paginates_and_returns_only_ids() -> None:
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["pageIndex"]
        seen_pages.append(page)
        rows = [{"postId": f"T-{index}"} for index in range(200)]
        if page == 2:
            rows = [{"postId": "T-200"}]
        return httpx.Response(
            200,
            json={"status": 0, "data": {"count": 201, "positionList": rows}},
        )

    ids = official_truth.fetch_official_ids(
        OBSERVATION_SOURCES[0],
        client_factory=_factory(handler),
        sleep=lambda _seconds: None,
    )

    assert seen_pages == [1, 2]
    assert ids[0] == "T-0"
    assert "T-200" in ids
    assert len(ids) == 201


def test_feishu_official_snapshot_uses_target_host_and_portal() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"count": 2, "job_post_list": [{"id": "N2"}, {"id": "N1"}]},
            },
        )

    ids = official_truth.fetch_official_ids(
        OBSERVATION_SOURCES[1], client_factory=_factory(handler)
    )

    assert ids == ["N1", "N2"]
    assert requests[0].url == "https://nio.jobs.feishu.cn/api/v1/search/job/posts"
    assert requests[0].headers["website-path"] == "campus"


def test_official_snapshot_rejects_a_truncated_response() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        rows = [{"id": "one"}] if calls == 1 else []
        return httpx.Response(
            200,
            json={"code": 0, "data": {"count": 2, "job_post_list": rows}},
        )

    with pytest.raises(RuntimeError, match="半截"):
        official_truth.fetch_official_ids(
            OBSERVATION_SOURCES[1], client_factory=_factory(handler)
        )


def test_official_snapshot_retries_one_timeout_only() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"count": 1, "job_post_list": [{"id": "N1"}] }},
        )

    ids = official_truth.fetch_official_ids(
        OBSERVATION_SOURCES[1],
        client_factory=_factory(handler),
        sleep=sleeps.append,
    )

    assert ids == ["N1"]
    assert calls == 2
    assert sleeps == [1.0]


def test_official_truth_module_does_not_import_the_product_pipeline() -> None:
    source = Path(official_truth.__file__).read_text(encoding="utf-8")
    for forbidden in ("adapters", "ingest", "normalize", "match", "queries"):
        assert f"import {forbidden}" not in source
        assert f"from .{forbidden}" not in source


def _seed_observation(conn, observation_id: int = 7) -> dict:
    conn.execute(
        """INSERT INTO observation_batches(
               id, started_at, finished_at, observed_date, trigger, slot,
               is_workday, on_time, status)
           VALUES(?, '2026-08-21T09:30:00+08:00', '2026-08-21T09:31:00+08:00',
                  '2026-08-21', 'scheduled', '09:30', 1, 1, 'ok')""",
        (observation_id,),
    )
    for spec in OBSERVATION_SOURCES:
        conn.execute(
            """INSERT INTO observation_sources(
                   observation_id, source_key, company, status)
               VALUES(?,?,?,'ok')""",
            (observation_id, spec["source_key"], spec["company"]),
        )
    conn.commit()
    return {
        "id": observation_id,
        "status": "ok",
        "results": [
            {"source_key": spec["source_key"], "status": "ok"}
            for spec in OBSERVATION_SOURCES
        ],
    }


def test_candidate_capture_is_append_only(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    report = _seed_observation(conn)

    official_truth.capture_candidates(
        conn,
        report,
        OBSERVATION_SOURCES,
        fetcher=lambda spec: [f"{spec['source_key']}:job-1"],
        captured_at="2026-08-21T09:32:00+08:00",
    )
    with pytest.raises(official_truth.CandidateExistsError):
        official_truth.capture_candidates(
            conn,
            report,
            OBSERVATION_SOURCES,
            fetcher=lambda _spec: ["replacement"],
            captured_at="2026-08-21T09:33:00+08:00",
        )

    rows = conn.execute(
        "SELECT source_key, official_ids_json FROM observation_truth_candidates"
    ).fetchall()
    assert len(rows) == 5
    assert json.loads(rows[0]["official_ids_json"])[0].endswith(":job-1")
    conn.close()


def _seed_review_day(conn) -> None:
    for spec in OBSERVATION_SOURCES:
        db.register_source(
            conn,
            spec["source_key"],
            spec["company"],
            spec["system"],
            spec["entry_url"],
            tenant=spec.get("tenant"),
        )
    for batch_number, slot in enumerate(observation.SCHEDULE_SLOTS, start=1):
        started = f"2026-08-21T{slot}:00+08:00"
        batch_id = conn.execute(
            """INSERT INTO observation_batches(
                   started_at, finished_at, observed_date, trigger, slot,
                   is_workday, on_time, status)
               VALUES(?,?, '2026-08-21', 'scheduled', ?, 1, 1, 'ok')""",
            (started, started, slot),
        ).lastrowid
        conn.execute(
            """INSERT INTO observation_slot_claims(observed_date, slot, observation_id)
               VALUES('2026-08-21', ?, ?)""",
            (slot, batch_id),
        )
        conn.execute(
            """INSERT INTO observation_notifications(
                   observation_id, policy, status, attempted_at)
               VALUES(?, ?, ?, ?)""",
            (
                batch_id,
                "daily-complete" if slot == "20:30" else "no-change",
                "sent" if slot == "20:30" else "skipped",
                started,
            ),
        )
        for source_number, spec in enumerate(OBSERVATION_SOURCES, start=1):
            run_id = conn.execute(
                """INSERT INTO runs(source_key, started_at, finished_at, status)
                   VALUES(?,?,?,'ok')""",
                (spec["source_key"], started, started),
            ).lastrowid
            external_id = f"{spec['source_key']}:job-{batch_number}-{source_number}"
            conn.execute(
                """INSERT INTO snapshots(
                       run_id, source_key, external_id, fingerprint, raw_json, captured_at)
                   VALUES(?,?,?,?,?,?)""",
                (run_id, spec["source_key"], external_id, "fp", "{}", started),
            )
            conn.execute(
                """INSERT INTO observation_sources(
                       observation_id, source_key, company, run_id, status, bootstrap)
                   VALUES(?,?,?,?, 'ok', 0)""",
                (batch_id, spec["source_key"], spec["company"], run_id),
            )
            ids_json = json.dumps([external_id], ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(ids_json.encode("utf-8")).hexdigest()
            conn.execute(
                """INSERT INTO observation_truth_candidates(
                       observation_id, source_key, status, official_url, captured_at,
                       official_ids_json, official_ids_sha256)
                   VALUES(?,?,'captured',?,?,?,?)""",
                (batch_id, spec["source_key"], spec["entry_url"], started, ids_json, digest),
            )
    conn.commit()


def test_daily_review_rejects_equal_count_but_different_ids(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_review_day(conn)
    different = ["different"]
    conn.execute(
        """UPDATE observation_truth_candidates
           SET official_ids_json=?, official_ids_sha256=?
           WHERE observation_id=(SELECT MIN(id) FROM observation_batches)
             AND source_key='tencent_join'""",
        (
            json.dumps(different, separators=(",", ":")),
            hashlib.sha256(json.dumps(different, separators=(",", ":")).encode()).hexdigest(),
        ),
    )
    conn.commit()

    review = official_truth.review_day(conn, "2026-08-21")

    assert review["ready"] is False
    assert any("岗位编号不一致" in problem for problem in review["problems"])
    conn.close()


def test_daily_review_rejects_failed_notification(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_review_day(conn)
    conn.execute(
        """UPDATE observation_notifications SET status='failed', error='denied'
           WHERE observation_id=(SELECT MIN(id) FROM observation_batches)"""
    )
    conn.commit()

    review = official_truth.review_day(conn, "2026-08-21")

    assert review["ready"] is False
    assert any("通知失败" in problem for problem in review["problems"])
    conn.close()


def test_daily_review_rejects_a_valid_status_with_the_wrong_policy(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_review_day(conn)
    conn.execute(
        """UPDATE observation_notifications SET policy='changes', status='sent'
           WHERE observation_id=(SELECT MIN(id) FROM observation_batches)"""
    )
    conn.commit()

    review = official_truth.review_day(conn, "2026-08-21")

    assert review["ready"] is False
    assert any("通知结果不符合策略" in problem for problem in review["problems"])
    conn.close()


def test_accept_day_commits_all_fifteen_items_atomically(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_review_day(conn)

    result = observation.accept_day(
        conn,
        "2026-08-21",
        reviewer="product-owner",
        note="已逐项查看三轮五家公司清单",
        checked_at="2026-08-21T21:00:00+08:00",
    )

    assert result["accepted"] == 15
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_truth_evidence"
    ).fetchone()[0] == 15
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_sources WHERE truth_status='verified'"
    ).fetchone()[0] == 15
    conn.close()


def test_accept_day_rolls_back_all_items_when_one_write_fails(
    tmp_path, monkeypatch
) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_review_day(conn)
    real_record_truth = observation.record_truth
    calls = 0

    def fail_midway(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 8:
            raise RuntimeError("simulated transaction failure")
        return real_record_truth(*args, **kwargs)

    monkeypatch.setattr(observation, "record_truth", fail_midway)

    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        observation.accept_day(
            conn,
            "2026-08-21",
            reviewer="product-owner",
            note="已逐项查看三轮五家公司清单",
            checked_at="2026-08-21T21:00:00+08:00",
        )

    assert calls == 8
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_truth_evidence"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_sources WHERE truth_status!='pending'"
    ).fetchone()[0] == 0
    conn.close()
