"""只读查询层。CLI 和 MCP server 共用的那一半。

为什么单独一个模块：`cli.py:1` 早就写了「后面在外面包一层 MCP server，逻辑复用
这里的函数」，但查询逻辑长在 `cli.jobs` / `cli.status` 的函数体里，和 Rich 表格
混在一起、返回 `None`。MCP 要的是数据，不是打印出来的表格，复用不了。

这一层的三条硬规矩，任一条破了就是把 CLI 的毛病带进 MCP：

1. **只读。** 不 INSERT / UPDATE / DELETE，不 commit。调用方可以传一个
   `mode=ro` 的连接进来（见 `db.connect_readonly`），破了规矩会当场报错。
2. **不打印、不 raise typer.\\*。** 参数不合法抛 `ValueError`，由调用方翻译成
   自己的报错形式（CLI 翻成 `typer.BadParameter`，MCP 翻成工具报错）。
   这里 import typer 就等于把 MCP 绑在 CLI 上。
3. **不碰 `identity`。** 匹配要读 `profile.yaml`，那个文件里有姓名/手机/身份证。
   这一层只接受 `intent` 字典，不接受整份 profile，也不自己去 load ——
   「谁把敏感数据读进内存」这件事要留在调用方一侧看得见。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from . import db, match


class AmbiguousJobError(ValueError):
    """同一个 external_id 对应多个来源，调用方必须补全 source_key。"""

    def __init__(self, external_id: str, source_keys: list[str]) -> None:
        self.external_id = external_id
        self.source_keys = source_keys
        super().__init__(
            f"岗位编号 {external_id!r} 出现在多个来源：{', '.join(source_keys)}"
        )


def validate_allow_missing(allow_missing: Iterable[str] | None) -> set[str]:
    """校验并归一化匹配放宽维度，供 CLI/MCP 在读取画像前先行拒错。"""
    allowed = set(allow_missing or ())
    if bad := allowed - set(match.MISSING_DIMS):
        raise ValueError(
            f"不认识的维度 {sorted(bad)}，可选：{'/'.join(match.MISSING_DIMS)}"
        )
    return allowed


def validate_positive_limit(limit: int) -> int:
    """只接受正整数上限，不暴露 SQLite 对 0/负数 LIMIT 的特殊解释。"""
    if limit <= 0:
        raise ValueError("limit 必须是正整数")
    return limit


def validate_since(since: str | None) -> str | None:
    """只接受带时区的 ISO 时间，避免非法文本被当成“没有变动”。"""
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("since 必须是带时区的 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("since 必须是带时区的 ISO 时间")
    return since


def _row_to_job(row: sqlite3.Row) -> dict:
    """把 jobs 行转成 dict，`cities` 解成 list。

    库里 `cities` 存的是 JSON 字符串，而 `match` 和适配器两边都可能直接给 list。
    这里统一解开，下游就不用每处都判一次类型 —— 判漏一处的表现是城市筛选静默
    命中 0 条（`"北京" in '["北京"]'` 恰好为真，反过来 `in` 一个 list 才是对的，
    所以这类错不一定会炸，可能只是答案不对）。
    """
    job = dict(row)
    job["cities"] = match.city_list(job)
    return job


def open_jobs(
    conn: sqlite3.Connection,
    *,
    family: str | None = None,
    city: str | None = None,
    recruit_type: str | None = None,
    company: str | None = None,
    matched: bool = False,
    allow_missing: Iterable[str] | None = None,
    intent: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """当前开放岗位，按筛选条件过滤。返回 (岗位, 提醒)。

    **不截断**：`limit` 留给调用方，因为 CLI 要在标题里打「共 N 条、显示前 M 条」，
    截断之后就数不出 N 了。

    第二个返回值是「给人看的提醒」，不是错误：`--allow-missing` 在没有 `matched`
    时不生效，这件事必须说出来而不是静默忽略 —— 用户以为放宽了，实际没放宽，
    看到的条数偏少，而少掉的那些恰好是他刚要求要看的。

    `allow_missing` 里有不认识的维度时抛 `ValueError`。不静默丢：写错一个维度名
    的后果是「以为放宽了某一维、实际一维都没放宽」，和上面那条是同一类故障。
    """
    rows = [
        _row_to_job(r)
        for r in conn.execute(
            "SELECT * FROM jobs WHERE closed_at IS NULL ORDER BY first_seen_at DESC"
        ).fetchall()
    ]

    allowed = validate_allow_missing(allow_missing)

    notes: list[str] = []
    if allowed and not matched:
        notes.append("--allow-missing / --loose 只在 --matched 下生效，已忽略")

    if matched:
        rows = match.filter_jobs(rows, intent or {}, allow_missing=allowed)
    if family:
        rows = [r for r in rows if r["job_family"] == family]
    if recruit_type:
        rows = [r for r in rows if r["recruit_type"] == recruit_type]
    if city:
        rows = [r for r in rows if city in r["cities"]]
    if company:
        rows = [r for r in rows if r["company"] == company]

    return rows, notes


def find_job(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    source_key: str | None = None,
) -> dict | None:
    """按完整岗位身份查找；编号重号时拒绝猜测。"""
    if source_key is not None:
        row = conn.execute(
            "SELECT * FROM jobs WHERE source_key=? AND external_id=?",
            (source_key, external_id),
        ).fetchone()
        return _row_to_job(row) if row else None

    rows = conn.execute(
        "SELECT * FROM jobs WHERE external_id=? ORDER BY source_key",
        (external_id,),
    ).fetchall()
    if len(rows) > 1:
        raise AmbiguousJobError(
            external_id, [str(row["source_key"]) for row in rows]
        )
    return _row_to_job(rows[0]) if rows else None


def explain_match(
    conn: sqlite3.Connection,
    external_id: str,
    intent: dict | None = None,
    *,
    source_key: str | None = None,
) -> dict | None:
    """一条岗位为什么命中／不命中。岗位不存在返回 `None`。

    返回里 `state` 是三态（hit / unknown / miss），不是布尔。把 unknown 折进
    「不命中」会丢掉最要紧的那一类：信息不全的岗位既没被排除也没被确认，
    该由人看一眼，而不是被系统当成不合格悄悄扣掉。
    """
    job = find_job(conn, external_id, source_key=source_key)
    if job is None:
        return None

    intent = intent or {}
    verdict = match.classify(job, intent)
    return {
        "source_key": job["source_key"],
        "external_id": job["external_id"],
        "company": job["company"],
        "title": job["title"],
        "job_family": job["job_family"],
        "recruit_type": job["recruit_type"],
        "grad_year": job["grad_year"],
        "cities": job["cities"],
        "closed_at": job["closed_at"],
        "state": verdict.state,
        "reason": verdict.reason,
        "unknowns": list(verdict.unknowns),
        "missing": list(verdict.missing),
        "score": match.score(job, intent),
    }


def source_health(conn: sqlite3.Connection) -> list[dict]:
    """每个源：开放岗位数 + 最近一条 run + 租户/限投 + 配额已用。

    最近一条 run 取 `ORDER BY id DESC LIMIT 1`，和 `cli status` 一致。一次都没跑过
    的源 `last_run` 是 `None`，不是一个假的 "-" 字符串 —— 「没跑过」和「跑过但状态
    未知」得分得开，前者该去 sync，后者该去查错。

    配额走 `db.quota_state`，按 **company** 算而不是按 source_key（同一家公司在库里
    可以有多行，按 source_key 数会把用量拆开，每份都不到上限，于是投穿）。
    """
    out: list[dict] = []
    for s in conn.execute("SELECT * FROM sources ORDER BY source_key").fetchall():
        src = dict(s)
        n = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE source_key=? AND closed_at IS NULL",
            (src["source_key"],),
        ).fetchone()["n"]
        r = conn.execute(
            """SELECT id, started_at, finished_at, status, fetched, error FROM runs
               WHERE source_key=? ORDER BY id DESC LIMIT 1""",
            (src["source_key"],),
        ).fetchone()
        used, limit = db.quota_state(conn, src["company"])
        out.append({
            "source_key": src["source_key"],
            "company": src["company"],
            "system": src["system"],
            "entry_url": src["entry_url"],
            "tenant": src.get("tenant"),
            "open_jobs": int(n),
            "last_run": dict(r) if r else None,
            "apply_limit": limit,
            "apply_used": used,
            "apply_remaining": None if limit is None else max(limit - used, 0),
        })
    return out


def sync_runs(
    conn: sqlite3.Connection, *, source_key: str | None = None, limit: int = 20
) -> list[dict]:
    """采集批次，最近的在前。

    `finished_at` 为空 = 这一轮没收尾（进程被杀，或者正在跑）。留成 `None` 而不是
    补一个时间：`runs` 那行是**先**落盘的，就是为了让崩掉的一轮留得下痕迹
    （见 `db.start_run`），补值等于把痕迹擦掉。
    """
    sql = "SELECT * FROM runs"
    args: list[Any] = []
    if source_key:
        sql += " WHERE source_key=?"
        args.append(source_key)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def job_changes(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """岗位变动事件（新开/关闭/改动/某族首现/批次启动），最近的在前。

    `payload` 解成 dict 再交出去。解不开时留 `{}` 并把原文放进 `payload_raw`：
    脏数据不该让整个查询炸掉，但也不能装作没有过 —— 悄悄吞掉会让「diff 里什么都
    没有」和「diff 存坏了」看起来一样。
    """
    sql = "SELECT * FROM events WHERE 1=1"
    args: list[Any] = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if since:
        sql += " AND occurred_at >= ?"
        args.append(since)
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    args.append(int(limit))

    out: list[dict] = []
    for r in conn.execute(sql, args).fetchall():
        e = dict(r)
        raw = e.pop("payload", None)
        try:
            e["payload"] = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            e["payload"] = {}
            e["payload_raw"] = raw
        out.append(e)
    return out
