"""五家目标公司的连续观察编排。

底层事实仍由 ``runs`` / ``snapshots`` / ``events`` 持有。本模块只把五次采集收成
一轮，记录有没有跑全以及官网真值是否已经核对，不复制岗位数据。
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

from . import db, ingest, routing
from .targets import OBSERVATION_SOURCES


Syncer = Callable[[object, dict], dict]
SCHEDULE_SLOTS = ("09:30", "14:30", "20:30")
SCHEDULE_GRACE_MINUTES = 60
TRUTH_CAPTURE_GRACE_MINUTES = 120


class AlreadyRunningError(RuntimeError):
    pass


class DuplicateObservationError(RuntimeError):
    pass


@contextmanager
def exclusive_run(db_path: Path):
    """同一数据库同一时刻只允许一轮观察；不等待第二轮。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.observe.lock")
    handle = lock_path.open("a", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("已有一轮观察正在运行") from exc
        yield
    finally:
        handle.close()


def _last_run_id(conn, source_key: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM runs WHERE source_key=? ORDER BY id DESC LIMIT 1",
        (source_key,),
    ).fetchone()
    return int(row["id"]) if row else None


def sync_source(conn, spec: dict) -> dict:
    """登记并同步一个已批准目标源，返回带底层 run_id 的摘要。"""
    db.register_source(
        conn,
        spec["source_key"],
        spec["company"],
        spec["system"],
        spec["entry_url"],
        notes="自动观察目标",
        tenant=spec.get("tenant"),
    )
    source = conn.execute(
        "SELECT * FROM sources WHERE source_key=?", (spec["source_key"],)
    ).fetchone()
    adapter = routing.get_adapter(
        {"source_key": spec["source_key"]}, dict(source) if source else None
    )
    stats = ingest.sync(conn, adapter)
    run_id = _last_run_id(conn, spec["source_key"])
    real_change_count = conn.execute(
        """SELECT COUNT(*) AS count FROM events
           WHERE run_id=?
             AND kind IN ('job_opened', 'job_updated', 'job_reopened', 'job_closed')""",
        (run_id,),
    ).fetchone()["count"]
    return {
        **stats,
        "run_id": run_id,
        "real_change_count": int(real_change_count),
    }


def _is_on_time(started: datetime, *, trigger: str, slot: str) -> bool:
    if trigger != "scheduled":
        return False
    if slot not in SCHEDULE_SLOTS:
        raise ValueError(f"不认识的观察时段：{slot}")
    hour, minute = (int(part) for part in slot.split(":", 1))
    scheduled = started.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delay = started - scheduled
    return timedelta(0) <= delay <= timedelta(minutes=SCHEDULE_GRACE_MINUTES)


def run(
    conn,
    *,
    specs: Iterable[dict],
    trigger: str,
    slot: str,
    started_at: str | None = None,
    syncer: Syncer = sync_source,
) -> dict:
    """顺序跑完目标源；单源失败不阻断其余源，整轮则明确标为不完整。"""
    started = started_at or db.now()
    started_datetime = datetime.fromisoformat(started)
    day = started_datetime.date()
    on_time = _is_on_time(started_datetime, trigger=trigger, slot=slot)
    try:
        cur = conn.execute(
            """INSERT INTO observation_batches(
                   started_at, observed_date, trigger, slot, is_workday, on_time, status)
               VALUES(?,?,?,?,?,?,'running')""",
            (
                started,
                day.isoformat(),
                trigger,
                slot,
                int(day.weekday() < 5),
                int(on_time),
            ),
        )
        observation_id = int(cur.lastrowid)
        if trigger == "scheduled":
            conn.execute(
                """INSERT INTO observation_slot_claims(
                       observed_date, slot, observation_id) VALUES(?,?,?)""",
                (day.isoformat(), slot, observation_id),
            )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if trigger == "scheduled":
            raise DuplicateObservationError(
                f"{day.isoformat()} {slot} 已有观察记录"
            ) from exc
        raise
    conn.commit()

    results: list[dict] = []
    for spec in specs:
        previous_run_id = _last_run_id(conn, spec["source_key"])
        try:
            stats = syncer(conn, spec)
        except Exception as exc:
            latest_run_id = _last_run_id(conn, spec["source_key"])
            result = {
                "source_key": spec["source_key"],
                "company": spec["company"],
                "run_id": (
                    latest_run_id if latest_run_id != previous_run_id else None
                ),
                "status": "failed",
                "bootstrap": False,
                "fetched": 0,
                "opened": 0,
                "updated": 0,
                "closed": 0,
                "change_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            opened = int(stats.get("opened", 0))
            updated = int(stats.get("updated", 0))
            closed = int(stats.get("closed", 0))
            real_change_count = int(
                stats.get(
                    "real_change_count",
                    opened
                    + max(0, updated - int(stats.get("fingerprint_desync", 0)))
                    + closed,
                )
            )
            result = {
                "source_key": spec["source_key"],
                "company": spec["company"],
                "run_id": stats.get("run_id"),
                "status": "partial" if stats.get("guard_tripped") else "ok",
                "bootstrap": bool(stats.get("bootstrap")),
                "fetched": int(stats.get("fetched", 0)),
                "opened": opened,
                "updated": updated,
                "closed": closed,
                "change_count": real_change_count,
                "error": None,
            }

        conn.execute(
            """INSERT INTO observation_sources(
                   observation_id, source_key, company, run_id, status, bootstrap,
                   fetched, opened, updated, closed, change_count, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id,
                result["source_key"],
                result["company"],
                result["run_id"],
                result["status"],
                int(result["bootstrap"]),
                result["fetched"],
                result["opened"],
                result["updated"],
                result["closed"],
                result["change_count"],
                result["error"],
            ),
        )
        conn.commit()
        results.append(result)

    failures = sum(result["status"] == "failed" for result in results)
    if failures == len(results):
        overall = "failed"
    elif any(result["status"] != "ok" for result in results):
        overall = "partial"
    else:
        overall = "ok"
    conn.execute(
        """UPDATE observation_batches
           SET finished_at=?, status=? WHERE id=?""",
        (db.now(), overall, observation_id),
    )
    conn.commit()
    return {
        "id": observation_id,
        "status": overall,
        "on_time": on_time,
        "results": results,
    }


def record_truth(
    conn,
    observation_id: int,
    source_key: str,
    *,
    official_url: str,
    captured_at: str,
    reviewer: str,
    official_job_ids: Iterable[str],
    verified_event_ids: Iterable[int],
    note: str,
    checked_at: str | None = None,
) -> None:
    """保存可复查的官网清单，并由程序计算漏报、误报和未核对变化。"""
    row = conn.execute(
        """SELECT s.status, s.run_id, s.truth_status,
                  b.status AS batch_status, b.started_at, b.finished_at
           FROM observation_sources AS s
           JOIN observation_batches AS b ON b.id=s.observation_id
           WHERE s.observation_id=? AND s.source_key=?""",
        (observation_id, source_key),
    ).fetchone()
    if row is None:
        raise ValueError("这轮观察里没有该数据源")
    if row["status"] != "ok":
        raise ValueError("采集未成功，不能把官网核对记成通过")
    if row["batch_status"] != "ok" or row["finished_at"] is None:
        raise ValueError("整轮观察尚未成功结束，不能提前记录官网核对")
    if row["truth_status"] != "pending":
        raise ValueError("这家公司本轮已有官网证据，不能覆盖历史核对")
    if row["run_id"] is None:
        raise ValueError("本轮缺少底层采集 run_id，不能建立官网对照")

    target = next(
        (spec for spec in OBSERVATION_SOURCES if spec["source_key"] == source_key),
        None,
    )
    if target is None or official_url != target["entry_url"]:
        raise ValueError("官网证据网址与固定目标入口不一致")
    if not reviewer.strip() or not note.strip():
        raise ValueError("官网核对者和说明不能为空")
    try:
        captured = datetime.fromisoformat(captured_at)
        started = datetime.fromisoformat(row["started_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("官网证据时间必须是 ISO 8601") from exc
    if captured < started:
        raise ValueError("官网证据时间早于本轮观察开始")
    if captured > started + timedelta(minutes=TRUTH_CAPTURE_GRACE_MINUTES):
        raise ValueError("官网证据超出本轮观察的同期窗口")

    official_ids = list(official_job_ids)
    if any(not isinstance(value, str) or not value for value in official_ids):
        raise ValueError("官网岗位编号必须是非空字符串")
    if len(official_ids) != len(set(official_ids)):
        raise ValueError("官网岗位编号清单不能有重复项")
    verified_ids = list(verified_event_ids)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in verified_ids):
        raise ValueError("变化事件编号必须是整数")
    if len(verified_ids) != len(set(verified_ids)):
        raise ValueError("变化事件编号不能重复")

    system_ids = [
        item["external_id"]
        for item in conn.execute(
            "SELECT external_id FROM snapshots WHERE run_id=? ORDER BY external_id",
            (row["run_id"],),
        ).fetchall()
    ]
    change_event_ids = [
        int(item["id"])
        for item in conn.execute(
            """SELECT id FROM events WHERE run_id=?
               AND kind IN ('job_opened','job_updated','job_reopened','job_closed')
               ORDER BY id""",
            (row["run_id"],),
        ).fetchall()
    ]
    official_ids = sorted(official_ids)
    system_ids = sorted(system_ids)
    verified_ids = sorted(verified_ids)
    missed_ids = sorted(set(official_ids) - set(system_ids))
    false_positive_ids = sorted(set(system_ids) - set(official_ids))
    unverified_event_ids = sorted(set(change_event_ids) - set(verified_ids))
    unexpected_event_ids = sorted(set(verified_ids) - set(change_event_ids))
    truth_status = (
        "verified"
        if not (
            missed_ids
            or false_positive_ids
            or unverified_event_ids
            or unexpected_event_ids
        )
        else "mismatch"
    )

    def canonical(value) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(value) -> str:
        return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

    checked = checked_at or db.now()
    try:
        checked_datetime = datetime.fromisoformat(checked)
    except (TypeError, ValueError) as exc:
        raise ValueError("官网核对时间必须是 ISO 8601") from exc
    if checked_datetime < captured:
        raise ValueError("官网核对时间不能早于证据采集时间")
    evidence = {
        "observation_id": observation_id,
        "source_key": source_key,
        "official_url": official_url,
        "captured_at": captured_at,
        "reviewer": reviewer.strip(),
        "official_job_ids": official_ids,
        "official_ids_sha256": digest(official_ids),
        "system_ids_sha256": digest(system_ids),
        "missed_ids": missed_ids,
        "false_positive_ids": false_positive_ids,
        "change_event_ids": change_event_ids,
        "verified_event_ids": verified_ids,
        "unverified_event_ids": unverified_event_ids,
        "unexpected_event_ids": unexpected_event_ids,
        "checked_at": checked,
        "note": note.strip(),
    }
    evidence_sha256 = digest(evidence)
    try:
        conn.execute(
            """INSERT INTO observation_truth_evidence(
                   observation_id, source_key, official_url, captured_at, reviewer,
                   official_ids_json, official_ids_sha256, system_ids_sha256,
                   missed_ids_json, false_positive_ids_json,
                   change_event_ids_json, verified_event_ids_json,
                   unverified_event_ids_json, unexpected_event_ids_json,
                   checked_at, note, evidence_sha256)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id,
                source_key,
                official_url,
                captured_at,
                evidence["reviewer"],
                canonical(official_ids),
                evidence["official_ids_sha256"],
                evidence["system_ids_sha256"],
                canonical(missed_ids),
                canonical(false_positive_ids),
                canonical(change_event_ids),
                canonical(verified_ids),
                canonical(unverified_event_ids),
                canonical(unexpected_event_ids),
                checked,
                evidence["note"],
                evidence_sha256,
            ),
        )
        conn.execute(
            """UPDATE observation_sources
               SET truth_status=?, missed_count=?, false_positive_count=?,
                   unverified_change_count=?, checked_at=?, truth_note=?
               WHERE observation_id=? AND source_key=?""",
            (
                truth_status,
                len(missed_ids),
                len(false_positive_ids),
                len(unverified_event_ids) + len(unexpected_event_ids),
                checked,
                evidence["note"],
                observation_id,
                source_key,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _next_workday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def progress(conn) -> dict:
    """返回当前最长连续工作日验收窗口，不把周末当有效样本。"""
    batches = [
        dict(row)
        for row in conn.execute(
            """SELECT b.* FROM observation_batches AS b
               JOIN observation_slot_claims AS c ON c.observation_id=b.id
               WHERE b.trigger='scheduled' ORDER BY b.id"""
        ).fetchall()
    ]
    by_date_slot: dict[tuple[str, str], dict] = {}
    for batch in batches:
        if batch["is_workday"] and batch["on_time"]:
            by_date_slot[(batch["observed_date"], batch["slot"])] = batch

    results_by_batch: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM observation_sources").fetchall():
        results_by_batch[int(row["observation_id"])].append(dict(row))
    evidence_keys = {
        (int(row["observation_id"]), row["source_key"])
        for row in conn.execute(
            "SELECT observation_id, source_key FROM observation_truth_evidence"
        ).fetchall()
    }

    expected_keys = {spec["source_key"] for spec in OBSERVATION_SOURCES}
    qualified: list[str] = []
    verified_change_dates: set[str] = set()
    candidate_dates = sorted({day for day, _slot in by_date_slot})
    for day in candidate_dates:
        slot_batches = [by_date_slot.get((day, slot)) for slot in SCHEDULE_SLOTS]
        if any(batch is None for batch in slot_batches):
            continue
        complete = True
        day_has_change = False
        for batch in slot_batches:
            assert batch is not None
            rows = results_by_batch[int(batch["id"])]
            if (
                batch["status"] != "ok"
                or {row["source_key"] for row in rows} != expected_keys
                or any(
                    row["status"] != "ok"
                    or row["truth_status"] != "verified"
                    or row["bootstrap"]
                    or (int(batch["id"]), row["source_key"]) not in evidence_keys
                    for row in rows
                )
            ):
                complete = False
                break
            day_has_change = day_has_change or any(
                row["change_count"] > 0 for row in rows
            )
        if complete:
            qualified.append(day)
            if day_has_change:
                verified_change_dates.add(day)

    longest: list[str] = []
    current: list[str] = []
    for day_text in qualified:
        day = date.fromisoformat(day_text)
        if current and day != _next_workday(date.fromisoformat(current[-1])):
            current = []
        current.append(day_text)
        if len(current) >= len(longest):
            longest = list(current)

    has_change = any(day in verified_change_dates for day in longest)
    if len(longest) >= 3 and has_change:
        status = "passed"
    elif len(longest) >= 3:
        status = "stability_only"
    else:
        status = "collecting"
    return {
        "status": status,
        "qualified_workdays": len(longest),
        "qualified_dates": longest,
        "has_verified_change": has_change,
    }
