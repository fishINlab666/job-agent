from __future__ import annotations

import subprocess

from jobagent import db, notifications
from jobagent.targets import OBSERVATION_SOURCES


def _report(*, observation_id: int = 7, status: str = "ok", changes: int = 0) -> dict:
    return {
        "id": observation_id,
        "status": status,
        "results": [
            {
                "company": "腾讯",
                "status": "ok" if status == "ok" else status,
                "change_count": changes,
            }
        ],
    }


def _seed_batch(conn, *, observation_id: int = 7, slot: str = "09:30") -> None:
    conn.execute(
        """INSERT INTO observation_batches(
               id, started_at, observed_date, trigger, slot,
               is_workday, on_time, status)
           VALUES(?, ?, '2026-08-21', 'scheduled', ?, 1, 1, 'ok')""",
        (observation_id, f"2026-08-21T{slot}:00+08:00", slot),
    )
    conn.execute(
        """INSERT INTO observation_slot_claims(observed_date, slot, observation_id)
           VALUES('2026-08-21', ?, ?)""",
        (slot, observation_id),
    )
    conn.commit()


def _seed_complete_day(conn, *, include_sources: bool = True) -> None:
    for observation_id, slot in enumerate(("09:30", "14:30", "20:30"), start=7):
        _seed_batch(conn, observation_id=observation_id, slot=slot)
        if include_sources:
            conn.executemany(
                """INSERT INTO observation_sources(
                       observation_id, source_key, company, status)
                   VALUES(?,?,?,'ok')""",
                [
                    (observation_id, spec["source_key"], spec["company"])
                    for spec in OBSERVATION_SOURCES
                ],
            )
    conn.commit()


def test_failure_notification_uses_fixed_osascript_argv_without_shell(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_batch(conn, slot="14:30")
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = notifications.deliver_observation(
        conn,
        _report(status="partial"),
        slot="14:30",
        runner=runner,
        attempted_at="2026-08-21T14:31:00+08:00",
    )

    assert result["status"] == "sent"
    assert result["policy"] == "failure"
    argv, kwargs = calls[0]
    assert argv[:2] == ["/usr/bin/osascript", "-e"]
    assert argv[-2:] == ["Job Agent 自动观察异常", "14:30 观察未完整，请查看记录。"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 10
    row = conn.execute(
        "SELECT * FROM observation_notifications WHERE observation_id=7"
    ).fetchone()
    assert (row["policy"], row["status"], row["attempted_at"]) == (
        "failure",
        "sent",
        "2026-08-21T14:31:00+08:00",
    )
    conn.close()


def test_change_notification_reports_total_change_count(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_batch(conn, slot="09:30")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = notifications.deliver_observation(
        conn, _report(changes=3), slot="09:30", runner=runner
    )

    assert result == {"policy": "changes", "status": "sent", "error": None}
    assert calls[0][-1] == "09:30 观察发现 3 条岗位变化。"
    conn.close()


def test_last_no_change_slot_sends_daily_completion(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_complete_day(conn)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = notifications.deliver_observation(
        conn, _report(observation_id=9), slot="20:30", runner=runner
    )

    assert result["policy"] == "daily-complete"
    assert result["status"] == "sent"
    assert calls[0][-1] == "今日三轮观察已完成，最后一轮没有岗位变化。"
    conn.close()


def test_last_slot_warns_when_the_day_is_incomplete(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_batch(conn, slot="20:30")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = notifications.deliver_observation(
        conn, _report(), slot="20:30", runner=runner
    )

    assert result["policy"] == "daily-incomplete"
    assert result["status"] == "sent"
    assert calls[0][-1] == "今日三轮观察不完整，请查看记录。"
    conn.close()


def test_last_slot_warns_when_company_results_are_missing(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_complete_day(conn, include_sources=False)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = notifications.deliver_observation(
        conn, _report(observation_id=9), slot="20:30", runner=runner
    )

    assert result["policy"] == "daily-incomplete"
    assert result["status"] == "sent"
    assert calls[0][-1] == "今日三轮观察不完整，请查看记录。"
    conn.close()


def test_ordinary_no_change_slot_is_persisted_as_skipped(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_batch(conn, slot="14:30")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("普通无变化轮次不应调用系统通知")

    result = notifications.deliver_observation(
        conn, _report(), slot="14:30", runner=should_not_run
    )

    assert result == {"policy": "no-change", "status": "skipped", "error": None}
    row = conn.execute(
        "SELECT status FROM observation_notifications WHERE observation_id=7"
    ).fetchone()
    assert row["status"] == "skipped"
    conn.close()


def test_notification_failure_is_persisted_and_returned(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    _seed_batch(conn, slot="09:30")

    def runner(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="not allowed")

    result = notifications.deliver_observation(
        conn, _report(changes=1), slot="09:30", runner=runner
    )

    assert result["policy"] == "changes"
    assert result["status"] == "failed"
    assert "CalledProcessError" in result["error"]
    row = conn.execute(
        "SELECT status, error FROM observation_notifications WHERE observation_id=7"
    ).fetchone()
    assert row["status"] == "failed"
    assert "CalledProcessError" in row["error"]
    conn.close()
