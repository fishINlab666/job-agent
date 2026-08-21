from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from typer.testing import CliRunner

from jobagent import cli, db, network, observation
from jobagent.adapters.base import RawJob
from jobagent.targets import OBSERVATION_SOURCES
from scripts import run_five


runner = CliRunner()


def test_real_run_script_reuses_the_production_target_pool() -> None:
    assert run_five.SOURCES is OBSERVATION_SOURCES


def test_observation_lock_rejects_an_overlapping_run(tmp_path) -> None:
    db_path = tmp_path / "jobagent.db"

    with observation.exclusive_run(db_path):
        with pytest.raises(observation.AlreadyRunningError):
            with observation.exclusive_run(db_path):
                pass


def test_scheduled_slot_can_only_create_one_batch(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    kwargs = {
        "specs": OBSERVATION_SOURCES,
        "trigger": "scheduled",
        "slot": "09:30",
        "started_at": "2026-08-21T09:30:00+08:00",
        "syncer": lambda _conn, spec: _stats(spec["source_key"]),
    }

    first = observation.run(conn, **kwargs)
    with pytest.raises(observation.DuplicateObservationError):
        observation.run(conn, **kwargs)

    rows = conn.execute(
        """SELECT id FROM observation_batches
           WHERE observed_date='2026-08-21' AND slot='09:30'"""
    ).fetchall()
    assert [row["id"] for row in rows] == [first["id"]]
    conn.close()


def test_observation_retry_stays_inside_one_sync_run(tmp_path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    calls = 0

    class FlakyAdapter:
        source_key = "test:retry"
        company = "重试公司"
        system = "test"
        entry_url = "https://example.com/jobs"

        def fetch(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout("temporary")
            return [
                RawJob(
                    external_id="job-1",
                    title="测试岗位",
                    raw_json={"id": "job-1"},
                )
            ]

    monkeypatch.setattr(observation.routing, "get_adapter", lambda *_args: FlakyAdapter())
    monkeypatch.setattr(network.time, "sleep", lambda _seconds: None)
    spec = {
        "source_key": "test:retry",
        "company": "重试公司",
        "system": "test",
        "entry_url": "https://example.com/jobs",
        "tenant": None,
    }

    result = observation.sync_source(conn, spec)

    runs = conn.execute(
        "SELECT id, status FROM runs WHERE source_key='test:retry'"
    ).fetchall()
    assert calls == 2
    assert [(row["id"], row["status"]) for row in runs] == [
        (result["run_id"], "ok")
    ]
    conn.close()


def test_duplicate_legacy_slots_are_kept_but_invalidated_during_migration(
    tmp_path,
) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    conn.execute("DROP INDEX IF EXISTS idx_observation_scheduled_slot")
    conn.execute("DELETE FROM observation_slot_claims")
    for minute in (30, 40):
        conn.execute(
            """INSERT INTO observation_batches(
                   started_at, observed_date, trigger, slot,
                   is_workday, on_time, status)
               VALUES(?, '2026-08-21', 'scheduled', '09:30', 1, 1, 'ok')""",
            (f"2026-08-21T09:{minute}:00+08:00",),
        )
    conn.commit()

    actions = db.migrate(conn)

    rows = conn.execute(
        """SELECT on_time FROM observation_batches
           WHERE observed_date='2026-08-21' AND slot='09:30'"""
    ).fetchall()
    claims = conn.execute("SELECT COUNT(*) FROM observation_slot_claims").fetchone()[0]
    assert [row["on_time"] for row in rows] == [0, 0]
    assert claims == 0
    assert "重复自动观察时段 1 组已保守失效" in actions
    assert db.migrate(conn) == []
    conn.close()


def test_observe_cli_stops_before_sync_when_another_run_holds_the_lock(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "jobagent.db"
    monkeypatch.setattr(
        cli,
        "_run_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("重叠任务不得进入采集")
        ),
    )

    with observation.exclusive_run(db_path):
        result = runner.invoke(cli.app, ["observe", "--db", str(db_path)])

    assert result.exit_code == 1
    assert "已有一轮观察正在运行" in result.output
    assert not isinstance(result.exception, AssertionError)


def test_observe_cli_runs_the_five_approved_sources(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "observation.db")
    captured: dict = {}

    def fake_run(conn, *, specs, trigger, slot):
        captured["db"] = conn.execute("PRAGMA database_list").fetchone()[2]
        captured["keys"] = [spec["source_key"] for spec in specs]
        captured["trigger"] = trigger
        captured["slot"] = slot
        return {
            "id": 1,
            "status": "ok",
            "results": [],
        }

    monkeypatch.setattr(cli, "_run_observation", fake_run, raising=False)
    monkeypatch.setattr(
        cli,
        "_capture_official_candidates",
        lambda *_args, **_kwargs: {"status": "ok", "results": []},
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_deliver_observation_notification",
        lambda *_args, **_kwargs: {"policy": "no-change", "status": "skipped", "error": None},
        raising=False,
    )

    result = runner.invoke(
        cli.app,
        [
            "observe",
            "--db",
            str(tmp_path / "scheduled.db"),
            "--trigger",
            "scheduled",
            "--slot",
            "09:30",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "db": str(tmp_path / "scheduled.db"),
        "keys": [
            "tencent_join",
            "feishu:nio:campus",
            "feishu:xiaopeng:campus",
            "feishu:bytedance:campus",
            "feishu:sensetime:edu",
        ],
        "trigger": "scheduled",
        "slot": "09:30",
    }


def test_scheduled_observe_captures_official_candidates_and_notifies(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[str, object]] = []
    report = {
        "id": 11,
        "status": "ok",
        "results": [
            {
                "company": "腾讯",
                "source_key": "tencent_join",
                "status": "ok",
                "fetched": 1,
                "opened": 0,
                "updated": 0,
                "closed": 0,
                "change_count": 0,
                "bootstrap": False,
                "error": None,
            }
        ],
    }
    monkeypatch.setattr(cli, "_run_observation", lambda *_args, **_kwargs: report)

    def capture(conn, observed, specs):
        calls.append(("capture", observed["id"]))
        return {"status": "ok", "results": []}

    def notify(conn, observed, *, slot):
        calls.append(("notify", (observed["status"], slot)))
        return {"policy": "no-change", "status": "skipped", "error": None}

    monkeypatch.setattr(cli, "_capture_official_candidates", capture, raising=False)
    monkeypatch.setattr(cli, "_deliver_observation_notification", notify, raising=False)

    result = runner.invoke(
        cli.app,
        [
            "observe",
            "--db",
            str(tmp_path / "observation.db"),
            "--trigger",
            "scheduled",
            "--slot",
            "09:30",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("capture", 11), ("notify", ("ok", "09:30"))]


def test_scheduled_candidate_failure_is_visible_and_returns_nonzero(
    tmp_path, monkeypatch
) -> None:
    report = {"id": 12, "status": "ok", "results": []}
    monkeypatch.setattr(cli, "_run_observation", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        cli,
        "_capture_official_candidates",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "results": [
                {
                    "source_key": "feishu:bytedance:campus",
                    "status": "failed",
                    "error": "ReadTimeout: official snapshot",
                }
            ],
        },
        raising=False,
    )
    observed_notification: dict = {}

    def notify(_conn, observed, *, slot):
        observed_notification.update(status=observed["status"], slot=slot)
        return {"policy": "failure", "status": "sent", "error": None}

    monkeypatch.setattr(cli, "_deliver_observation_notification", notify, raising=False)

    result = runner.invoke(
        cli.app,
        [
            "observe",
            "--db",
            str(tmp_path / "observation.db"),
            "--trigger",
            "scheduled",
            "--slot",
            "14:30",
        ],
    )

    assert result.exit_code == 1
    assert "官网候选失败" in result.output
    assert "ReadTimeout" in result.output
    assert observed_notification == {"status": "partial", "slot": "14:30"}


def test_manual_observe_does_not_capture_or_notify(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_run_observation",
        lambda *_args, **_kwargs: {"id": 13, "status": "ok", "results": []},
    )
    monkeypatch.setattr(
        cli,
        "_capture_official_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("手工观察不生成自动验收候选")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_deliver_observation_notification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("手工观察不发定时通知")
        ),
        raising=False,
    )

    result = runner.invoke(
        cli.app,
        ["observe", "--db", str(tmp_path / "observation.db")],
    )

    assert result.exit_code == 0, result.output


def test_schedule_install_cli_delegates_to_fixed_installer(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    def fake_install(*, project_root, python_executable, db_path, home):
        captured.update(
            project_root=project_root,
            python_executable=python_executable,
            db_path=db_path,
            home=home,
        )
        return ["09:30", "14:30", "20:30"]

    monkeypatch.setattr(cli, "_install_observation_schedule", fake_install, raising=False)
    result = runner.invoke(
        cli.app,
        [
            "schedule-install",
            "--project-root",
            str(tmp_path / "repo"),
            "--python",
            str(tmp_path / "venv/bin/python"),
            "--db",
            str(tmp_path / "real.db"),
            "--home",
            str(tmp_path / "home"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "project_root": tmp_path / "repo",
        "python_executable": tmp_path / "venv/bin/python",
        "db_path": tmp_path / "real.db",
        "home": tmp_path / "home",
    }


def test_schedule_install_preserves_the_virtualenv_python_symlink(
    tmp_path, monkeypatch
) -> None:
    base_python = tmp_path / "runtime/python3.13"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("python", encoding="utf-8")
    base_python.chmod(0o755)
    venv_python = tmp_path / "venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)
    project_root = tmp_path / "stable"
    project_root.mkdir()
    captured: dict = {}

    def fake_install(*, project_root, python_executable, db_path, home):
        captured["python"] = python_executable
        return list(observation.SCHEDULE_SLOTS)

    monkeypatch.setattr(cli, "_install_observation_schedule", fake_install)
    result = runner.invoke(
        cli.app,
        [
            "schedule-install",
            "--project-root",
            str(project_root),
            "--python",
            str(venv_python),
            "--db",
            str(tmp_path / "jobagent.db"),
            "--home",
            str(tmp_path / "home"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["python"] == venv_python.absolute()
    assert captured["python"] != base_python.resolve()


def test_observe_cli_shows_each_failure_and_returns_nonzero(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_run_observation",
        lambda *args, **kwargs: {
            "id": 9,
            "status": "partial",
            "results": [
                {
                    "company": "腾讯",
                    "source_key": "tencent_join",
                    "status": "ok",
                    "fetched": 8,
                    "opened": 1,
                    "updated": 0,
                    "closed": 0,
                    "bootstrap": False,
                    "error": None,
                },
                {
                    "company": "蔚来",
                    "source_key": "feishu:nio:campus",
                    "status": "failed",
                    "fetched": 0,
                    "opened": 0,
                    "updated": 0,
                    "closed": 0,
                    "bootstrap": False,
                    "error": "RuntimeError: upstream unavailable",
                },
            ],
        },
    )

    result = runner.invoke(
        cli.app,
        ["observe", "--db", str(tmp_path / "observation.db")],
    )

    assert result.exit_code == 1
    assert "腾讯" in result.output and "新增 1" in result.output
    assert "蔚来" in result.output and "upstream unavailable" in result.output


def test_observe_review_cli_writes_official_check(tmp_path) -> None:
    path = tmp_path / "observation.db"
    conn = db.connect(path)
    db.init(conn)
    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES,
        trigger="manual",
        slot="manual",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=_evidenced_sync,
    )
    conn.close()
    evidence = tmp_path / "tencent-official.json"
    evidence.write_text(
        json.dumps(
            {
                "source_key": "tencent_join",
                "official_url": OBSERVATION_SOURCES[0]["entry_url"],
                "captured_at": "2026-08-21T09:35:00+08:00",
                "reviewer": "test-reviewer",
                "external_ids": ["tencent_join:job-1"],
                "verified_event_ids": [],
                "note": "官网清单逐项一致",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        [
            "observe-review",
            str(report["id"]),
            "tencent_join",
            "--db",
            str(path),
            "--evidence",
            str(evidence),
        ],
    )

    assert result.exit_code == 0, result.output
    check = db.connect_readonly(path).execute(
        """SELECT truth_status FROM observation_sources
           WHERE observation_id=? AND source_key='tencent_join'""",
        (report["id"],),
    ).fetchone()
    assert check["truth_status"] == "verified"


def test_observe_review_cli_cannot_bypass_scheduled_daily_review(tmp_path) -> None:
    path = tmp_path / "observation.db"
    conn = db.connect(path)
    db.init(conn)
    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES,
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=_evidenced_sync,
    )
    conn.close()
    evidence = tmp_path / "tencent-official.json"
    evidence.write_text(
        json.dumps(
            {
                "source_key": "tencent_join",
                "official_url": OBSERVATION_SOURCES[0]["entry_url"],
                "captured_at": "2026-08-21T09:35:00+08:00",
                "reviewer": "test-reviewer",
                "external_ids": ["tencent_join:job-1"],
                "verified_event_ids": [],
                "note": "试图逐公司确认定时记录",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        [
            "observe-review",
            str(report["id"]),
            "tencent_join",
            "--db",
            str(path),
            "--evidence",
            str(evidence),
        ],
    )

    assert result.exit_code == 1
    assert "定时观察不能逐公司确认" in result.output
    conn = db.connect_readonly(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_truth_evidence"
    ).fetchone()[0] == 0
    conn.close()


def test_scheduled_observe_rejects_an_unapproved_slot_before_running(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        cli,
        "_run_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("非法时段不得开始采集")
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "observe",
            "--db",
            str(tmp_path / "observation.db"),
            "--trigger",
            "scheduled",
            "--slot",
            "18:00",
        ],
    )

    assert result.exit_code == 2
    assert "09:30" in result.output and "20:30" in result.output
    assert not isinstance(result.exception, AssertionError)


def test_observation_status_cli_translates_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_observation_progress",
        lambda conn: {
            "status": "stability_only",
            "qualified_workdays": 3,
            "qualified_dates": ["2026-08-21", "2026-08-24", "2026-08-25"],
            "has_verified_change": False,
        },
        raising=False,
    )

    result = runner.invoke(
        cli.app,
        ["observation-status", "--db", str(tmp_path / "observation.db")],
    )

    assert result.exit_code == 0, result.output
    assert "3 个有效工作日" in result.output
    assert "还没有捕获并核对真实变化" in result.output


def test_review_day_defaults_to_read_only_preview(tmp_path, monkeypatch) -> None:
    path = tmp_path / "observation.db"
    conn = db.connect(path)
    db.init(conn)
    conn.close()
    called = {"accept": 0}
    monkeypatch.setattr(
        cli,
        "_review_observation_day",
        lambda _conn, day: {
            "date": day,
            "ready": True,
            "problems": [],
            "items": [
                {
                    "slot": "09:30",
                    "company": "腾讯",
                    "change_count": 1,
                }
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_accept_observation_day",
        lambda *_args, **_kwargs: called.update(accept=called["accept"] + 1),
        raising=False,
    )

    result = runner.invoke(
        cli.app,
        ["observation-review-day", "2026-08-21", "--db", str(path)],
    )

    assert result.exit_code == 0, result.output
    assert "可以确认" in result.output
    assert "尚未写入最终验收" in result.output
    assert called["accept"] == 0


def test_review_day_accepts_all_evidence_only_with_explicit_flag(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "observation.db"
    conn = db.connect(path)
    db.init(conn)
    conn.close()
    captured: dict = {}
    monkeypatch.setattr(
        cli,
        "_review_observation_day",
        lambda _conn, day: {
            "date": day,
            "ready": True,
            "problems": [],
            "items": [],
        },
        raising=False,
    )

    def accept(_conn, day, *, reviewer, note):
        captured.update(day=day, reviewer=reviewer, note=note)
        return {"accepted": 15}

    monkeypatch.setattr(cli, "_accept_observation_day", accept, raising=False)

    result = runner.invoke(
        cli.app,
        [
            "observation-review-day",
            "2026-08-21",
            "--db",
            str(path),
            "--accept",
            "--reviewer",
            "owner",
            "--note",
            "已查看三轮五家公司",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "day": "2026-08-21",
        "reviewer": "owner",
        "note": "已查看三轮五家公司",
    }
    assert "15 份" in result.output


def test_schedule_uninstall_cli_delegates(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    def fake_uninstall(*, home):
        captured["home"] = home

    monkeypatch.setattr(
        cli, "_uninstall_observation_schedule", fake_uninstall, raising=False
    )
    result = runner.invoke(
        cli.app,
        ["schedule-uninstall", "--home", str(tmp_path / "home")],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"home": tmp_path / "home"}


def _stats(
    source_key: str,
    *,
    opened: int = 0,
    updated: int = 0,
    closed: int = 0,
    fingerprint_desync: int = 0,
    bootstrap: bool = False,
) -> dict:
    return {
        "source": source_key,
        "bootstrap": bootstrap,
        "fetched": 10,
        "opened": opened,
        "updated": updated,
        "closed": closed,
        "guard_tripped": False,
        "family_unknown": 0,
        "fingerprint_desync": fingerprint_desync,
        "run_id": None,
    }


def _evidenced_sync(conn, spec, *, real_change: bool = False) -> dict:
    db.register_source(
        conn,
        spec["source_key"],
        spec["company"],
        spec["system"],
        spec["entry_url"],
        tenant=spec.get("tenant"),
    )
    started = db.now()
    run_id = conn.execute(
        """INSERT INTO runs(source_key, started_at, finished_at, status, fetched)
           VALUES(?,?,?,?,1)""",
        (spec["source_key"], started, started, "ok"),
    ).lastrowid
    external_id = f"{spec['source_key']}:job-1"
    conn.execute(
        """INSERT INTO snapshots(
               run_id, source_key, external_id, fingerprint, raw_json, captured_at)
           VALUES(?,?,?,?,?,?)""",
        (run_id, spec["source_key"], external_id, "fp", "{}", started),
    )
    if real_change:
        conn.execute(
            """INSERT INTO events(kind, source_key, company, occurred_at, run_id)
               VALUES('job_opened',?,?,?,?)""",
            (spec["source_key"], spec["company"], started, run_id),
        )
    conn.commit()
    return {
        **_stats(spec["source_key"], opened=int(real_change)),
        "run_id": int(run_id),
        "real_change_count": int(real_change),
    }


def _record_matching_truth(conn, observation_id: int, spec: dict) -> None:
    row = conn.execute(
        """SELECT s.run_id, b.started_at, b.slot
           FROM observation_sources AS s
           JOIN observation_batches AS b ON b.id=s.observation_id
           WHERE s.observation_id=? AND s.source_key=?""",
        (observation_id, spec["source_key"]),
    ).fetchone()
    external_ids = [
        item["external_id"]
        for item in conn.execute(
            "SELECT external_id FROM snapshots WHERE run_id=? ORDER BY external_id",
            (row["run_id"],),
        ).fetchall()
    ]
    event_ids = [
        int(item["id"])
        for item in conn.execute(
            """SELECT id FROM events WHERE run_id=?
               AND kind IN ('job_opened','job_updated','job_reopened','job_closed')
               ORDER BY id""",
            (row["run_id"],),
        ).fetchall()
    ]
    official_json = json.dumps(
        external_ids,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    official_sha = hashlib.sha256(official_json.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO observation_truth_candidates(
               observation_id, source_key, status, official_url, captured_at,
               official_ids_json, official_ids_sha256)
           VALUES(?,?,'captured',?,?,?,?)""",
        (
            observation_id,
            spec["source_key"],
            spec["entry_url"],
            row["started_at"],
            official_json,
            official_sha,
        ),
    )
    if event_ids:
        notification_policy, notification_status = "changes", "sent"
    elif row["slot"] == "20:30":
        notification_policy, notification_status = "daily-complete", "sent"
    else:
        notification_policy, notification_status = "no-change", "skipped"
    conn.execute(
        """INSERT OR IGNORE INTO observation_notifications(
               observation_id, policy, status, attempted_at)
           VALUES(?, ?, ?, ?)""",
        (
            observation_id,
            notification_policy,
            notification_status,
            row["started_at"],
        ),
    )
    observation.record_truth(
        conn,
        observation_id,
        spec["source_key"],
        official_url=spec["entry_url"],
        captured_at=row["started_at"],
        reviewer="test-reviewer",
        official_job_ids=external_ids,
        verified_event_ids=event_ids,
        note="官网清单与变化逐项一致",
        checked_at=row["started_at"],
    )


def test_observation_records_each_source_and_workday(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)

    def fake_sync(_conn, spec):
        return _stats(
            spec["source_key"],
            opened=3 if spec["source_key"] == "tencent_join" else 0,
            updated=2,
            closed=1,
            bootstrap=spec["source_key"] == "feishu:nio:campus",
        )

    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES,
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=fake_sync,
    )

    assert report["status"] == "ok"
    batch = dict(conn.execute("SELECT * FROM observation_batches").fetchone())
    assert (batch["trigger"], batch["slot"], batch["is_workday"]) == (
        "scheduled",
        "09:30",
        1,
    )
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM observation_sources ORDER BY source_key"
        ).fetchall()
    ]
    assert len(rows) == 5
    assert {row["status"] for row in rows} == {"ok"}
    assert {row["truth_status"] for row in rows} == {"pending"}
    assert next(row for row in rows if row["source_key"] == "tencent_join")[
        "change_count"
    ] == 6
    assert next(
        row for row in rows if row["source_key"] == "feishu:nio:campus"
    )["bootstrap"] == 1
    conn.close()


def test_fingerprint_repairs_do_not_count_as_real_job_changes(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)

    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES[:1],
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=lambda _conn, spec: _stats(
            spec["source_key"], updated=2, fingerprint_desync=2
        ),
    )

    row = conn.execute(
        "SELECT change_count FROM observation_sources WHERE observation_id=?",
        (report["id"],),
    ).fetchone()
    assert row["change_count"] == 0
    conn.close()


def test_observation_continues_after_one_source_fails(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    called: list[str] = []

    def fake_sync(_conn, spec):
        called.append(spec["source_key"])
        if spec["source_key"] == "feishu:nio:campus":
            raise RuntimeError("upstream unavailable")
        return _stats(spec["source_key"])

    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES,
        trigger="scheduled",
        slot="14:30",
        started_at="2026-08-22T14:30:00+08:00",
        syncer=fake_sync,
    )

    assert called == [spec["source_key"] for spec in OBSERVATION_SOURCES]
    assert report["status"] == "partial"
    assert conn.execute(
        "SELECT status FROM observation_batches WHERE id=?", (report["id"],)
    ).fetchone()["status"] == "partial"
    failed = conn.execute(
        """SELECT status, error FROM observation_sources
           WHERE observation_id=? AND source_key='feishu:nio:campus'""",
        (report["id"],),
    ).fetchone()
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError: upstream unavailable"
    assert conn.execute(
        "SELECT is_workday FROM observation_batches WHERE id=?", (report["id"],)
    ).fetchone()["is_workday"] == 0
    conn.close()


def test_failure_before_a_new_run_does_not_link_the_previous_run(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    spec = OBSERVATION_SOURCES[0]
    db.register_source(
        conn,
        spec["source_key"],
        spec["company"],
        spec["system"],
        spec["entry_url"],
    )
    previous_run = conn.execute(
        """INSERT INTO runs(source_key, started_at, finished_at, status)
           VALUES(?, ?, ?, 'ok')""",
        (spec["source_key"], db.now(), db.now()),
    ).lastrowid
    conn.commit()

    report = observation.run(
        conn,
        specs=(spec,),
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=lambda _conn, _spec: (_ for _ in ()).throw(
            RuntimeError("adapter construction failed")
        ),
    )
    linked = conn.execute(
        "SELECT run_id FROM observation_sources WHERE observation_id=?",
        (report["id"],),
    ).fetchone()["run_id"]

    assert previous_run is not None
    assert linked is None
    conn.close()


def test_truth_review_records_verified_or_mismatch(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES,
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=_evidenced_sync,
    )

    observation.record_truth(
        conn,
        report["id"],
        "tencent_join",
        official_url=OBSERVATION_SOURCES[0]["entry_url"],
        captured_at="2026-08-21T09:35:00+08:00",
        reviewer="test-reviewer",
        official_job_ids=["tencent_join:job-1"],
        verified_event_ids=[],
        note="官网岗位清单逐项一致",
        checked_at="2026-08-21T10:00:00+08:00",
    )
    observation.record_truth(
        conn,
        report["id"],
        "feishu:nio:campus",
        official_url=OBSERVATION_SOURCES[1]["entry_url"],
        captured_at="2026-08-21T09:35:00+08:00",
        reviewer="test-reviewer",
        official_job_ids=["feishu:nio:campus:job-1", "official-only"],
        verified_event_ids=[],
        note="官网多一条",
        checked_at="2026-08-21T10:05:00+08:00",
    )

    rows = {
        row["source_key"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM observation_sources WHERE observation_id=?",
            (report["id"],),
        ).fetchall()
    }
    assert rows["tencent_join"]["truth_status"] == "verified"
    assert rows["feishu:nio:campus"]["truth_status"] == "mismatch"
    assert rows["feishu:nio:campus"]["missed_count"] == 1
    evidence = conn.execute(
        """SELECT reviewer, official_ids_sha256, missed_ids_json
           FROM observation_truth_evidence
           WHERE observation_id=? AND source_key='feishu:nio:campus'""",
        (report["id"],),
    ).fetchone()
    assert evidence["reviewer"] == "test-reviewer"
    assert len(evidence["official_ids_sha256"]) == 64
    assert json.loads(evidence["missed_ids_json"]) == ["official-only"]
    conn.close()


def test_truth_evidence_is_append_only_and_unverified_changes_fail(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES[:1],
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=lambda inner, spec: _evidenced_sync(inner, spec, real_change=True),
    )

    observation.record_truth(
        conn,
        report["id"],
        "tencent_join",
        official_url=OBSERVATION_SOURCES[0]["entry_url"],
        captured_at="2026-08-21T09:35:00+08:00",
        reviewer="test-reviewer",
        official_job_ids=["tencent_join:job-1"],
        verified_event_ids=[],
        note="岗位清单一致，但变化事件尚未逐项核对",
    )
    row = conn.execute(
        """SELECT truth_status, unverified_change_count
           FROM observation_sources WHERE observation_id=?""",
        (report["id"],),
    ).fetchone()
    assert (row["truth_status"], row["unverified_change_count"]) == ("mismatch", 1)

    with pytest.raises(ValueError, match="不能覆盖"):
        _record_matching_truth(conn, report["id"], OBSERVATION_SOURCES[0])

    evidence_count = conn.execute(
        "SELECT COUNT(*) FROM observation_truth_evidence WHERE observation_id=?",
        (report["id"],),
    ).fetchone()[0]
    assert evidence_count == 1
    conn.close()


def test_truth_evidence_must_be_captured_within_the_same_observation_window(
    tmp_path,
) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES[:1],
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T09:30:00+08:00",
        syncer=_evidenced_sync,
    )

    with pytest.raises(ValueError, match="同期窗口"):
        observation.record_truth(
            conn,
            report["id"],
            "tencent_join",
            official_url=OBSERVATION_SOURCES[0]["entry_url"],
            captured_at="2026-08-22T09:30:00+08:00",
            reviewer="test-reviewer",
            official_job_ids=["tencent_join:job-1"],
            verified_event_ids=[],
            note="不是同期证据",
        )

    conn.close()


def test_progress_ignores_legacy_verified_rows_without_evidence(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    for day in ("2026-08-21", "2026-08-24", "2026-08-25"):
        for slot in observation.SCHEDULE_SLOTS:
            batch_id = conn.execute(
                """INSERT INTO observation_batches(
                       started_at, finished_at, observed_date, trigger, slot,
                       is_workday, on_time, status)
                   VALUES(?,?,?,?,?,1,1,'ok')""",
                (
                    f"{day}T{slot}:00+08:00",
                    f"{day}T{slot}:30+08:00",
                    day,
                    "scheduled",
                    slot,
                ),
            ).lastrowid
            conn.execute(
                """INSERT INTO observation_slot_claims(
                       observed_date, slot, observation_id) VALUES(?,?,?)""",
                (day, slot, batch_id),
            )
            conn.executemany(
                """INSERT INTO observation_sources(
                       observation_id, source_key, company, status, bootstrap,
                       change_count, truth_status)
                   VALUES(?,?,?,?,0,1,'verified')""",
                [
                    (batch_id, spec["source_key"], spec["company"], "ok")
                    for spec in OBSERVATION_SOURCES
                ],
            )
    conn.commit()

    state = observation.progress(conn)

    assert state["status"] == "collecting"
    assert state["qualified_workdays"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_truth_evidence"
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("official_url", "https://example.com/wrong"),
        ("captured_at", "2026-08-21T09:31:00+08:00"),
        ("official_ids_sha256", "0" * 64),
    ],
)
def test_candidate_must_match_final_evidence_exactly(field, wrong_value) -> None:
    candidate = {
        "status": "captured",
        "official_url": "https://join.qq.com/post.html",
        "captured_at": "2026-08-21T09:30:00+08:00",
        "official_ids_sha256": "a" * 64,
    }
    evidence = dict(candidate)
    evidence.pop("status")

    assert observation._candidate_matches_evidence(candidate, evidence) is True
    evidence[field] = wrong_value
    assert observation._candidate_matches_evidence(candidate, evidence) is False


def test_truth_review_rejects_a_batch_that_has_not_finished(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)
    batch_id = conn.execute(
        """INSERT INTO observation_batches(
               started_at, observed_date, trigger, slot, is_workday, on_time, status)
           VALUES('2026-08-21T09:30:00+08:00', '2026-08-21',
                  'scheduled', '09:30', 1, 1, 'running')"""
    ).lastrowid
    conn.execute(
        """INSERT INTO observation_sources(
               observation_id, source_key, company, status)
           VALUES(?, 'tencent_join', '腾讯', 'ok')""",
        (batch_id,),
    )
    conn.commit()

    with pytest.raises(ValueError, match="整轮观察尚未成功结束"):
        observation.record_truth(
            conn,
            int(batch_id),
            "tencent_join",
            official_url=OBSERVATION_SOURCES[0]["entry_url"],
            captured_at="2026-08-21T09:35:00+08:00",
            reviewer="test-reviewer",
            official_job_ids=["J1"],
            verified_event_ids=[],
            note="过早核对",
        )

    conn.close()


def test_delayed_launch_does_not_count_as_the_approved_slot(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)

    report = observation.run(
        conn,
        specs=OBSERVATION_SOURCES,
        trigger="scheduled",
        slot="09:30",
        started_at="2026-08-21T11:15:00+08:00",
        syncer=lambda _conn, spec: _stats(spec["source_key"]),
    )
    batch = conn.execute(
        "SELECT on_time FROM observation_batches WHERE id=?", (report["id"],)
    ).fetchone()

    assert batch["on_time"] == 0
    conn.close()


def test_progress_counts_three_complete_workdays_but_not_weekend(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)

    for day in ("2026-08-21", "2026-08-22", "2026-08-24", "2026-08-25"):
        for slot in observation.SCHEDULE_SLOTS:
            def fake_sync(_conn, spec, *, _day=day, _slot=slot):
                return _evidenced_sync(
                    _conn,
                    spec,
                    real_change=(
                        _day == "2026-08-24"
                        and _slot == "14:30"
                        and spec["source_key"] == "tencent_join"
                    ),
                )

            report = observation.run(
                conn,
                specs=OBSERVATION_SOURCES,
                trigger="scheduled",
                slot=slot,
                started_at=f"{day}T{slot}:00+08:00",
                syncer=fake_sync,
            )
            for spec in OBSERVATION_SOURCES:
                _record_matching_truth(conn, report["id"], spec)

    progress = observation.progress(conn)

    assert progress == {
        "status": "passed",
        "qualified_workdays": 3,
        "qualified_dates": ["2026-08-21", "2026-08-24", "2026-08-25"],
        "has_verified_change": True,
    }

    first_batch = conn.execute(
        "SELECT MIN(id) FROM observation_batches WHERE is_workday=1"
    ).fetchone()[0]
    candidate = dict(
        conn.execute(
            """SELECT * FROM observation_truth_candidates
               WHERE observation_id=? AND source_key='tencent_join'""",
            (first_batch,),
        ).fetchone()
    )
    conn.execute(
        """DELETE FROM observation_truth_candidates
           WHERE observation_id=? AND source_key='tencent_join'""",
        (first_batch,),
    )
    conn.commit()
    without_candidate = observation.progress(conn)
    assert without_candidate["status"] == "collecting"
    assert without_candidate["qualified_workdays"] == 2
    conn.execute(
        """INSERT INTO observation_truth_candidates(
               observation_id, source_key, status, official_url, captured_at,
               official_ids_json, official_ids_sha256, error)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            candidate["observation_id"],
            candidate["source_key"],
            candidate["status"],
            candidate["official_url"],
            candidate["captured_at"],
            candidate["official_ids_json"],
            candidate["official_ids_sha256"],
            candidate["error"],
        ),
    )
    conn.commit()

    conn.execute(
        """UPDATE observation_notifications
           SET status='failed', error='Notification Center unavailable'
           WHERE observation_id=?""",
        (first_batch,),
    )
    conn.commit()
    degraded = observation.progress(conn)
    assert degraded["status"] == "collecting"
    assert degraded["qualified_workdays"] == 2

    conn.execute(
        """UPDATE observation_notifications
           SET status='skipped', error=NULL WHERE observation_id=?""",
        (first_batch,),
    )
    missing_batch = conn.execute(
        """SELECT MIN(id) FROM observation_batches
           WHERE observed_date='2026-08-24'"""
    ).fetchone()[0]
    conn.execute(
        "DELETE FROM observation_notifications WHERE observation_id=?",
        (missing_batch,),
    )
    conn.commit()
    missing = observation.progress(conn)
    assert missing["status"] == "collecting"
    assert missing["qualified_workdays"] == 1
    conn.close()


def test_progress_rejects_a_late_catch_up_run(tmp_path) -> None:
    conn = db.connect(tmp_path / "observation.db")
    db.init(conn)

    for slot, actual in (
        ("09:30", "11:15"),
        ("14:30", "14:30"),
        ("20:30", "20:30"),
    ):
        report = observation.run(
            conn,
            specs=OBSERVATION_SOURCES,
            trigger="scheduled",
            slot=slot,
            started_at=f"2026-08-21T{actual}:00+08:00",
            syncer=_evidenced_sync,
        )
        for spec in OBSERVATION_SOURCES:
            _record_matching_truth(conn, report["id"], spec)

    state = observation.progress(conn)

    assert state["qualified_workdays"] == 0
    assert state["qualified_dates"] == []
    conn.close()
