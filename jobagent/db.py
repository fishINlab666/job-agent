"""SQLite 连接与 schema 初始化。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "jobagent.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# 老库要补的列。ALTER TABLE ADD COLUMN 的默认值必须是常量，
# 所以 created_at 这里不带 datetime('now')，由写入方自己填。
APPLICATION_COLUMNS: list[tuple[str, str]] = [
    ("source_key", "TEXT"),
    ("external_id", "TEXT"),
    ("company", "TEXT"),
    ("error", "TEXT"),
    ("screenshot_path", "TEXT"),
    ("confirm_token", "TEXT"),
    ("prepared_at", "TEXT"),
    ("created_at", "TEXT"),
]

# sources 要补的列。CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，
# 所以老库不会自己长出新列，得在这儿补。
SOURCE_COLUMNS: list[tuple[str, str]] = [
    ("tenant", "TEXT"),
]


def now() -> str:
    """统一用本地时区的 ISO 字符串，便于直接读。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """建表 + 迁移老库。两者都幂等，可以反复跑。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate(conn)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """把老库对齐到当前 schema，返回做过的动作，便于 CLI 打出来。

    这里主要处理一件事：M6 当时另起了一张 submissions 表，而 schema.sql
    里早就有 applications（prefilled/submitted/failed/abandoned + filled_fields
    /skipped_fields），本来就是为「先填好、再确认、才提交」设计的。两张表记
    同一件事必然对不上账，所以合回 applications，submissions 里的数据搬过去。
    """
    done: list[str] = []
    for table, cols in (("applications", APPLICATION_COLUMNS), ("sources", SOURCE_COLUMNS)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                done.append(f"{table} += {col}")

    # 索引建在补列之后：老库缺列时 schema.sql 里的索引语句会失败
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_apps_status "
        "ON applications(status, created_at DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_token "
        "ON applications(confirm_token) WHERE confirm_token IS NOT NULL"
    )

    done += _absorb_submissions(conn)
    conn.commit()
    return done


def _absorb_submissions(conn: sqlite3.Connection) -> list[str]:
    """把遗留的 submissions 行搬进 applications。空表就什么都不做。

    不删旧表：删表是不可逆动作，留着也不碍事，只在 CLI 里提示一句。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submissions'"
    ).fetchone()
    if not exists:
        return []

    rows = conn.execute("SELECT * FROM submissions").fetchall()
    if not rows:
        return []

    # 老表的 success 对应新表的 submitted，其余状态名一致
    status_map = {"success": "submitted"}
    moved = 0
    for r in rows:
        d = dict(r)
        already = conn.execute(
            "SELECT 1 FROM applications WHERE job_id=? AND submitted_at=?",
            (d.get("job_id"), d.get("submitted_at")),
        ).fetchone()
        if already:
            continue
        conn.execute(
            """INSERT INTO applications(
                   job_id, source_key, external_id, company, status,
                   submitted_at, error, screenshot_path, note, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                d.get("job_id"), d.get("source_key"), d.get("external_id"),
                d.get("company"),
                status_map.get(d.get("status"), d.get("status") or "failed"),
                d.get("submitted_at"), d.get("error"), d.get("screenshot_path"),
                "从 submissions 表迁移", d.get("created_at") or now(),
            ),
        )
        moved += 1
    return [f"submissions → applications 迁移 {moved} 条"] if moved else []


def register_source(
    conn: sqlite3.Connection,
    source_key: str,
    company: str,
    system: str,
    entry_url: str,
    notes: str = "",
    tenant: str | None = None,
) -> None:
    """登记/更新一个源。**自己提交**，调用方不再控制提交时机。

    提交是必须的，不是可选的：`runs.source_key REFERENCES sources(source_key)`
    （schema.sql:28），源那行没落盘的话，紧接着 `start_run` 走侧连接会撞
    `FOREIGN KEY constraint failed`（主连接还持着写锁时先撞 `database is locked`）。

    dry-run 不许落盘，靠调用方传一个吞掉 commit 的包装连接（见 `ingest._NoCommit`），
    不靠这里的形参 —— 形参得让每个调用方都记着传，忘了就静默落盘。
    """
    conn.execute(
        """INSERT INTO sources(source_key, company, system, entry_url, notes, tenant)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(source_key) DO UPDATE SET
             company=excluded.company, system=excluded.system,
             entry_url=excluded.entry_url, notes=excluded.notes,
             -- COALESCE 而不是直接覆盖：tenant 有一部分是人工配的（北森/Moka
             -- 的租户取不到，只能手填）。适配器没声明 tenant 时传进来是 NULL，
             -- 直接覆盖就把人配好的值擦掉了——下一轮 sync 静默清空，
             -- 表现是「本来能投的公司忽然投不了」。适配器给了值才盖。
             tenant=COALESCE(excluded.tenant, sources.tenant)""",
        (source_key, company, system, entry_url, notes, tenant),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, source_key: str) -> int:
    """开一条 run，返回 id。**自己提交**，调用方不再控制提交时机。

    这一行必须**先**落盘，进程中途被杀也留得下「这轮开过、没收尾」的痕迹，
    否则崩一次就查不到崩在哪。`cli status` 每个源只读最近一条 run
    （`ORDER BY id DESC LIMIT 1`），没有这行 `running`，崩掉的一轮会显示成
    上一次的 `ok` —— 等于报假账。

    所以真跑时这里走的是**侧连接**（见 `ingest.sync`）：主事务异常回滚业务数据，
    这一行痕迹不跟着回滚。dry-run 走吞掉 commit 的包装连接，什么都不落盘。
    """
    cur = conn.execute(
        "INSERT INTO runs(source_key, started_at) VALUES(?,?)",
        (source_key, now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    fetched: int = 0,
    error: str | None = None,
) -> None:
    """给 run 收尾。**自己提交**，调用方不再控制提交时机。

    必须在主事务 `commit`/`rollback` **之后**调用：SQLite 单写者，主连接还持着
    写事务时侧连接写 `runs` 会撞 `database is locked`（见 `ingest.sync` 的顺序）。

    dry-run 下这一句是空操作，不是 bug：`rollback()` 已经把 `start_run` 那行
    INSERT 退掉了，这条 UPDATE 命中 0 行。
    """
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, fetched=?, error=? WHERE id=?",
        (now(), status, fetched, error, run_id),
    )
    conn.commit()


def record_prefill(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    source_key: str,
    external_id: str,
    company: str,
    confirm_token: str,
    filled_fields: list[dict],
    skipped_fields: list[dict],
    screenshot_path: str | None = None,
) -> int:
    """prepare 成功后先落一条 prefilled。

    为什么确认之前就写库：如果只在提交成功后才记，那么「填好了但用户没确认」
    这段就是黑的——排查不了、也看不出有多少投递卡在确认环节。
    """
    cur = conn.execute(
        """INSERT INTO applications(
               job_id, source_key, external_id, company, status,
               filled_fields, skipped_fields, screenshot_path,
               confirm_token, prepared_at, created_at)
           VALUES(?,?,?,?,'prefilled',?,?,?,?,?,?)""",
        (
            job_id, source_key, external_id, company,
            json.dumps(filled_fields, ensure_ascii=False),
            json.dumps(skipped_fields, ensure_ascii=False),
            screenshot_path, confirm_token or None, now(), now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_blocked(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    source_key: str,
    external_id: str,
    company: str,
    error: str | None,
    screenshot_path: str | None = None,
) -> int:
    """prepare 阶段就没走通（岗位关了 / 要登录 / 找不到申请按钮），直接落终态。

    confirm_token 显式写 NULL 而不是空串：applications 上那条唯一索引的条件是
    `WHERE confirm_token IS NOT NULL`，空串是非 NULL，会参与去重，于是第二条
    blocked 就撞唯一约束了。
    """
    cur = conn.execute(
        """INSERT INTO applications(
               job_id, source_key, external_id, company, status,
               error, screenshot_path, confirm_token, prepared_at, created_at)
           VALUES(?,?,?,?,'blocked',?,?,NULL,?,?)""",
        (job_id, source_key, external_id, company, error,
         screenshot_path, now(), now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finalize_application(
    conn: sqlite3.Connection,
    app_id: int,
    *,
    status: str,
    submitted_at: str | None = None,
    error: str | None = None,
    screenshot_path: str | None = None,
    filled_fields: list[dict] | None = None,
    skipped_fields: list[dict] | None = None,
    note: str | None = None,
) -> None:
    """把 prefilled 那一条更新成终态。同一次确认只有一条记录。"""
    conn.execute(
        """UPDATE applications SET
               status=?, submitted_at=COALESCE(?, submitted_at),
               error=?, screenshot_path=COALESCE(?, screenshot_path),
               filled_fields=COALESCE(?, filled_fields),
               skipped_fields=COALESCE(?, skipped_fields),
               note=? WHERE id=?""",
        (
            status, submitted_at, error, screenshot_path,
            json.dumps(filled_fields, ensure_ascii=False) if filled_fields is not None else None,
            json.dumps(skipped_fields, ensure_ascii=False) if skipped_fields is not None else None,
            note, app_id,
        ),
    )
    conn.commit()


def add_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    source_key: str | None = None,
    company: str | None = None,
    job_id: int | None = None,
    payload: dict | None = None,
    run_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO events(kind, source_key, company, job_id, payload, occurred_at, run_id)
           VALUES(?,?,?,?,?,?,?)""",
        (
            kind,
            source_key,
            company,
            job_id,
            json.dumps(payload or {}, ensure_ascii=False),
            now(),
            run_id,
        ),
    )
