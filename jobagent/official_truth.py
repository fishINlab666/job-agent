"""独立读取官方招聘接口的岗位编号，生成待人工确认的同期证据。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from urllib.parse import urlparse

import httpx

from . import db, network
from .targets import OBSERVATION_SLOTS, OBSERVATION_SOURCES


TENCENT_API = "https://join.qq.com/api/v1/position/searchPosition"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PAGE_SIZE = 200
MAX_PAGES = 100


class CandidateExistsError(RuntimeError):
    pass


class ReviewNotReadyError(RuntimeError):
    pass


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _validated_ids(values: Iterable[object], *, expected: int) -> list[str]:
    ids = [str(value).strip() if value is not None else "" for value in values]
    if any(not value for value in ids):
        raise RuntimeError("官方岗位返回了空编号，拒绝生成不完整证据")
    if len(ids) != expected:
        raise RuntimeError(f"官方接口 count={expected}，实际拿到 {len(ids)} 条，拒绝半截证据")
    if len(ids) != len(set(ids)):
        raise RuntimeError("官方岗位编号重复，拒绝生成含糊证据")
    return sorted(ids)


def _fetch_tencent(client_factory) -> list[str]:
    rows: list[dict] = []
    total: int | None = None
    headers = {
        "User-Agent": UA,
        "Referer": "https://join.qq.com/post.html?query=p_2",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    with client_factory(timeout=20.0, headers=headers) as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.post(
                TENCENT_API,
                json={
                    "projectId": 2,
                    "pageIndex": page,
                    "pageSize": PAGE_SIZE,
                    "keyword": "",
                },
            )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != 0:
                raise RuntimeError(f"腾讯官方接口异常：{body.get('message')!r}")
            data = body.get("data") or {}
            if total is None:
                total = int(data.get("count") or 0)
            if len(rows) >= total:
                break
            batch = data.get("positionList") or []
            if not batch:
                raise RuntimeError(
                    f"腾讯官方接口返回半截数据：count={total}，只拿到 {len(rows)} 条"
                )
            rows.extend(batch)
            if len(rows) >= total:
                break
        else:
            raise RuntimeError("腾讯官方接口分页超过安全上限")
    assert total is not None
    return _validated_ids(
        (row.get("postId") or row.get("id") for row in rows), expected=total
    )


def _fetch_feishu(spec: dict, client_factory) -> list[str]:
    parsed = urlparse(spec["entry_url"])
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("飞书官方入口必须是固定 HTTPS 地址")
    portal = parsed.path.strip("/").split("/", 1)[0] or None
    base = f"https://{parsed.hostname}"
    headers = {
        "User-Agent": UA,
        "Referer": f"{base}/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    if portal:
        headers["website-path"] = portal

    rows: list[dict] = []
    total: int | None = None
    with client_factory(timeout=20.0, headers=headers) as client:
        for page in range(MAX_PAGES):
            offset = page * PAGE_SIZE
            response = client.post(
                f"{base}/api/v1/search/job/posts",
                json={"keyword": "", "limit": PAGE_SIZE, "offset": offset},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise RuntimeError(
                    f"{spec['source_key']} 官方接口异常："
                    f"code={body.get('code')!r} msg={body.get('msg')!r}"
                )
            data = body.get("data") or {}
            if total is None:
                total = int(data.get("count") or 0)
                if total == 0:
                    return []
            batch = data.get("job_post_list") or []
            if not batch:
                raise RuntimeError(
                    f"{spec['source_key']} 官方接口返回半截数据："
                    f"count={total}，只拿到 {len(rows)} 条"
                )
            rows.extend(batch)
            if len(rows) >= total:
                break
        else:
            raise RuntimeError(f"{spec['source_key']} 官方接口分页超过安全上限")
    assert total is not None
    return _validated_ids((row.get("id") for row in rows), expected=total)


def fetch_official_ids(
    spec: dict,
    *,
    client_factory=httpx.Client,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """走独立、只取 ID 的官方 API 路径；只对 timeout 多试一次。"""

    def operation() -> list[str]:
        if spec["system"] == "tencent_join":
            return _fetch_tencent(client_factory)
        if spec["system"] == "feishu":
            return _fetch_feishu(spec, client_factory)
        raise ValueError(f"不支持的官方证据源：{spec['system']}")

    return network.retry_timeouts(operation, sleep=sleep)


def capture_candidates(
    conn,
    report: dict,
    specs: Iterable[dict],
    *,
    fetcher: Callable[[dict], list[str]] = fetch_official_ids,
    captured_at: str | None = None,
) -> dict:
    """每轮每源只写一个候选；失败也落证，绝不覆盖或自动代签。"""
    existing = conn.execute(
        "SELECT COUNT(*) FROM observation_truth_candidates WHERE observation_id=?",
        (report["id"],),
    ).fetchone()[0]
    if existing:
        raise CandidateExistsError("该观察轮次已有官网候选，不能覆盖或重抓")

    attempted = captured_at or db.now()
    results_by_key = {result["source_key"]: result for result in report["results"]}
    summary: list[dict] = []
    candidate_rows: list[tuple] = []
    try:
        for spec in specs:
            source_result = results_by_key.get(spec["source_key"])
            status = "skipped"
            ids = None
            error = "本轮数据同步未成功，官网候选不参与确认"
            if source_result and source_result["status"] == "ok":
                try:
                    ids = sorted(fetcher(spec))
                    if len(ids) != len(set(ids)) or any(not item for item in ids):
                        raise RuntimeError("官网候选岗位编号为空或重复")
                except Exception as exc:
                    status = "failed"
                    ids = None
                    error = f"{type(exc).__name__}: {exc}"
                else:
                    status = "captured"
                    error = None
            candidate_rows.append(
                (
                    report["id"],
                    spec["source_key"],
                    status,
                    spec["entry_url"],
                    attempted,
                    _canonical(ids) if ids is not None else None,
                    _digest(ids) if ids is not None else None,
                    error,
                ),
            )
            summary.append(
                {"source_key": spec["source_key"], "status": status, "error": error}
            )
        conn.executemany(
            """INSERT INTO observation_truth_candidates(
                   observation_id, source_key, status, official_url, captured_at,
                   official_ids_json, official_ids_sha256, error)
               VALUES(?,?,?,?,?,?,?,?)""",
            candidate_rows,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CandidateExistsError("官网候选必须 append-only") from exc
    except Exception:
        conn.rollback()
        raise
    return {"status": "ok" if all(x["status"] == "captured" for x in summary) else "partial", "results": summary}


def review_day(conn, observed_date: str) -> dict:
    """汇总一个工作日的 15 格证据；不写最终真值。"""
    batches = conn.execute(
        """SELECT b.* FROM observation_batches AS b
           JOIN observation_slot_claims AS c ON c.observation_id=b.id
           WHERE b.observed_date=? AND b.trigger='scheduled'
           ORDER BY b.started_at""",
        (observed_date,),
    ).fetchall()
    by_slot = {row["slot"]: row for row in batches}
    problems: list[str] = []
    items: list[dict] = []
    if set(by_slot) != set(OBSERVATION_SLOTS):
        problems.append("当天三个固定时段尚未全部完成")

    expected_sources = {spec["source_key"]: spec for spec in OBSERVATION_SOURCES}
    for slot in OBSERVATION_SLOTS:
        batch = by_slot.get(slot)
        if batch is None:
            continue
        batch_id = int(batch["id"])
        if not batch["is_workday"]:
            problems.append(f"{slot} 不是工作日记录")
        if not batch["on_time"] or batch["status"] != "ok":
            problems.append(f"{slot} 数据同步未完整或未准点")
        notification = conn.execute(
            """SELECT policy, status, error FROM observation_notifications
               WHERE observation_id=?""",
            (batch_id,),
        ).fetchone()
        sources = {
            row["source_key"]: row
            for row in conn.execute(
                "SELECT * FROM observation_sources WHERE observation_id=?",
                (batch_id,),
            ).fetchall()
        }
        candidates = {
            row["source_key"]: row
            for row in conn.execute(
                "SELECT * FROM observation_truth_candidates WHERE observation_id=?",
                (batch_id,),
            ).fetchall()
        }
        if set(sources) != set(expected_sources):
            problems.append(f"{slot} 目标公司记录不完整")
        operational_failed = (
            batch["status"] != "ok"
            or set(sources) != set(expected_sources)
            or any(row["status"] != "ok" for row in sources.values())
            or set(candidates) != set(expected_sources)
            or any(row["status"] != "captured" for row in candidates.values())
        )
        total_changes = sum(int(row["change_count"]) for row in sources.values())
        day_complete = set(by_slot) == set(OBSERVATION_SLOTS) and all(
            row["status"] == "ok" and row["on_time"] for row in by_slot.values()
        )
        if operational_failed:
            expected_notification = ("failure", "sent")
        elif total_changes:
            expected_notification = ("changes", "sent")
        elif slot == "20:30" and day_complete:
            expected_notification = ("daily-complete", "sent")
        elif slot == "20:30":
            expected_notification = ("daily-incomplete", "sent")
        else:
            expected_notification = ("no-change", "skipped")
        if notification is None:
            problems.append(f"{slot} 缺少通知结果")
        elif notification["status"] == "failed":
            problems.append(f"{slot} 通知失败：{notification['error']}")
        elif (notification["policy"], notification["status"]) != expected_notification:
            problems.append(
                f"{slot} 通知结果不符合策略："
                f"实际 {notification['policy']}/{notification['status']}，"
                f"预期 {expected_notification[0]}/{expected_notification[1]}"
            )
        for source_key, spec in expected_sources.items():
            source = sources.get(source_key)
            if source is None:
                continue
            if source["status"] != "ok" or source["run_id"] is None:
                problems.append(f"{slot} {spec['company']} 数据同步失败")
                continue
            if source["bootstrap"]:
                problems.append(f"{slot} {spec['company']} 仍是首轮基线，不能签正式真值")
            candidate = candidates.get(source_key)
            if candidate is None:
                problems.append(f"{slot} {spec['company']} 缺少官网候选")
                continue
            if candidate["status"] != "captured":
                problems.append(
                    f"{slot} {spec['company']} 官网候选失败：{candidate['error']}"
                )
                continue
            if candidate["official_url"] != spec["entry_url"]:
                problems.append(f"{slot} {spec['company']} 官网入口与固定目标不一致")
                continue
            try:
                captured = datetime.fromisoformat(candidate["captured_at"])
                started = datetime.fromisoformat(batch["started_at"])
            except (TypeError, ValueError):
                problems.append(f"{slot} {spec['company']} 官网候选时间格式错误")
                continue
            if captured < started or captured > started + timedelta(minutes=120):
                problems.append(f"{slot} {spec['company']} 官网候选不在同期窗口")
                continue
            try:
                official_ids = json.loads(candidate["official_ids_json"])
            except (TypeError, json.JSONDecodeError):
                problems.append(f"{slot} {spec['company']} 官网候选清单格式错误")
                continue
            if (
                not isinstance(official_ids, list)
                or any(not isinstance(value, str) or not value for value in official_ids)
                or official_ids != sorted(set(official_ids))
            ):
                problems.append(f"{slot} {spec['company']} 官网候选清单无序、重复或含空编号")
                continue
            if _digest(official_ids) != candidate["official_ids_sha256"]:
                problems.append(f"{slot} {spec['company']} 官网候选哈希不一致")
                continue
            system_ids = [
                row["external_id"]
                for row in conn.execute(
                    "SELECT external_id FROM snapshots WHERE run_id=? ORDER BY external_id",
                    (source["run_id"],),
                ).fetchall()
            ]
            if official_ids != system_ids:
                problems.append(f"{slot} {spec['company']} 岗位编号不一致")
            event_rows = conn.execute(
                """SELECT e.id, e.kind, j.external_id, j.title
                   FROM events AS e LEFT JOIN jobs AS j ON j.id=e.job_id
                   WHERE e.run_id=?
                     AND e.kind IN ('job_opened','job_updated','job_reopened','job_closed')
                   ORDER BY e.id""",
                (source["run_id"],),
            ).fetchall()
            event_ids = [int(row["id"]) for row in event_rows]
            items.append(
                {
                    "observation_id": batch_id,
                    "slot": slot,
                    "source_key": source_key,
                    "company": spec["company"],
                    "official_url": candidate["official_url"],
                    "captured_at": candidate["captured_at"],
                    "official_ids": official_ids,
                    "official_ids_sha256": candidate["official_ids_sha256"],
                    "official_count": len(official_ids),
                    "system_count": len(system_ids),
                    "verified_event_ids": event_ids,
                    "change_count": len(event_ids),
                    "events": [dict(row) for row in event_rows],
                }
            )
    if len(items) != len(OBSERVATION_SLOTS) * len(expected_sources):
        problems.append("当天官网证据未形成完整 15 格")
    return {"date": observed_date, "ready": not problems, "problems": problems, "items": items}
