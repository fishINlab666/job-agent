from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jobagent import cli, db, observation
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
        """SELECT s.run_id, b.started_at
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
