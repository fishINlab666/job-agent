"""自动观察的最小 macOS 通知通道。"""
from __future__ import annotations

import subprocess
from typing import Callable

from . import db
from .targets import OBSERVATION_SLOTS, OBSERVATION_SOURCES


Runner = Callable[..., subprocess.CompletedProcess]


def _message(
    report: dict, slot: str, *, daily_complete: bool
) -> tuple[str, str, str] | None:
    if report["status"] != "ok":
        return (
            "failure",
            "Job Agent 自动观察异常",
            f"{slot} 观察未完整，请查看记录。",
        )
    changes = sum(int(result.get("change_count", 0)) for result in report["results"])
    if changes:
        return (
            "changes",
            "Job Agent 发现岗位变化",
            f"{slot} 观察发现 {changes} 条岗位变化。",
        )
    if slot == "20:30":
        if not daily_complete:
            return (
                "daily-incomplete",
                "Job Agent 今日观察不完整",
                "今日三轮观察不完整，请查看记录。",
            )
        return (
            "daily-complete",
            "Job Agent 今日观察完成",
            "今日三轮观察已完成，最后一轮没有岗位变化。",
        )
    return None


def _day_is_complete(conn, observation_id: int, slot: str) -> bool:
    batch = conn.execute(
        "SELECT observed_date, slot FROM observation_batches WHERE id=?",
        (observation_id,),
    ).fetchone()
    if batch is None or batch["slot"] != slot:
        return False
    rows = conn.execute(
        """SELECT b.id, b.slot, b.status, b.on_time
           FROM observation_batches AS b
           JOIN observation_slot_claims AS c ON c.observation_id=b.id
           WHERE b.observed_date=? AND b.trigger='scheduled'""",
        (batch["observed_date"],),
    ).fetchall()
    by_slot = {row["slot"]: row for row in rows}
    if set(by_slot) != set(OBSERVATION_SLOTS) or not all(
        row["status"] == "ok" and row["on_time"] for row in by_slot.values()
    ):
        return False
    expected_sources = {spec["source_key"] for spec in OBSERVATION_SOURCES}
    for row in by_slot.values():
        sources = conn.execute(
            """SELECT source_key, status FROM observation_sources
               WHERE observation_id=?""",
            (row["id"],),
        ).fetchall()
        if (
            {source["source_key"] for source in sources} != expected_sources
            or any(source["status"] != "ok" for source in sources)
        ):
            return False
    return True


def deliver_observation(
    conn,
    report: dict,
    *,
    slot: str,
    runner: Runner = subprocess.run,
    attempted_at: str | None = None,
) -> dict:
    """按固定策略发系统通知，并把成功、跳过或失败都写入数据库。"""
    message = _message(
        report,
        slot,
        daily_complete=(
            slot != "20:30" or _day_is_complete(conn, report["id"], slot)
        ),
    )
    policy = message[0] if message else "no-change"
    status = "skipped" if message is None else "sent"
    error = None
    attempted = attempted_at or db.now()

    if message is not None:
        _policy, title, body = message
        argv = [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            body,
        ]
        try:
            runner(
                argv,
                check=True,
                capture_output=True,
                text=True,
                shell=False,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

    conn.execute(
        """INSERT INTO observation_notifications(
               observation_id, policy, status, attempted_at, error)
           VALUES(?,?,?,?,?)""",
        (report["id"], policy, status, attempted, error),
    )
    conn.commit()
    return {"policy": policy, "status": status, "error": error}
