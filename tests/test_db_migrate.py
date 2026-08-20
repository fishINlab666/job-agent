"""迁移测试。

老库不会自己长出新列：CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做。
所以每加一列都得在 migrate 里补，而 migrate 会被反复调用（每次 db.init 都跑），
必须幂等。

这里另外钉死一条 register_source 的行为：sources.tenant 有一部分是人工配的
（北森/Moka 的租户从 URL 取不到），upsert 时不能被适配器传来的 NULL 覆盖。
那种覆盖不报错，只是下一轮 sync 之后「本来能投的公司忽然投不了」。
"""
from __future__ import annotations

import sqlite3

import pytest

from jobagent import db

# 加 tenant 列之前的 sources 表。手写而不是靠 DROP COLUMN，
# 这样测的是真实的老库形状，不依赖 SQLite 版本。
OLD_SOURCES_DDL = """
CREATE TABLE sources (
    source_key  TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    system      TEXT,
    entry_url   TEXT,
    notes       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1
);
"""


def _old_db(tmp_path) -> sqlite3.Connection:
    """建一个缺 tenant 列的库，并塞一行进去。"""
    conn = db.connect(tmp_path / "old.db")
    conn.executescript(OLD_SOURCES_DDL)
    conn.execute(
        "INSERT INTO sources(source_key, company, system, entry_url) VALUES(?,?,?,?)",
        ("tencent_join", "腾讯", "self_built", "https://join.qq.com/post.html"),
    )
    conn.commit()
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class TestMigrate:
    def test_adds_tenant_to_old_sources(self, tmp_path):
        conn = _old_db(tmp_path)
        assert "tenant" not in _cols(conn, "sources")

        db.init(conn)

        assert "tenant" in _cols(conn, "sources")

    def test_existing_rows_survive(self, tmp_path):
        """补列不能动数据。ALTER TABLE ADD COLUMN 本身安全，
        但如果哪天有人改成重建表，这条会立刻叫。"""
        conn = _old_db(tmp_path)
        db.init(conn)

        row = conn.execute("SELECT * FROM sources WHERE source_key='tencent_join'").fetchone()
        assert row["company"] == "腾讯"
        assert row["tenant"] is None      # 新列，老行取到 NULL

    def test_idempotent(self, tmp_path):
        """第二次跑必须什么都不做。db.init 每次连库都调 migrate。"""
        conn = _old_db(tmp_path)
        db.init(conn)

        assert db.migrate(conn) == []

    def test_fresh_db_needs_no_migration(self, tmp_path):
        """新库由 schema.sql 直接建全，migrate 不该有活干。
        有活干说明 schema.sql 和 SOURCE_COLUMNS 对不上了。"""
        conn = db.connect(tmp_path / "fresh.db")
        db.init(conn)

        assert db.migrate(conn) == []

    def test_reports_what_it_did(self, tmp_path):
        """返回值会被 CLI 打出来，得说清楚动了哪张表的哪一列。"""
        conn = _old_db(tmp_path)
        conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))

        assert "sources += tenant" in db.migrate(conn)


class TestRegisterSourceTenant:
    def test_manual_tenant_survives_a_sync(self, tmp_path):
        """人工配好的租户，不能被适配器传来的 None 擦掉。

        自建适配器没有 tenant 属性，ingest 那边取到 None。直接覆盖的话，
        每轮 sync 都会静默清空手配的值。
        """
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)
        db.register_source(conn, "nio_campus", "蔚来", "feishu", "https://x", tenant="nio")

        # 模拟下一轮 sync：适配器没声明 tenant
        db.register_source(conn, "nio_campus", "蔚来", "feishu", "https://x", tenant=None)

        row = conn.execute("SELECT tenant FROM sources WHERE source_key='nio_campus'").fetchone()
        assert row["tenant"] == "nio"

    def test_adapter_tenant_overwrites(self, tmp_path):
        """适配器明确给了值就该盖掉旧值——租户搬迁时靠这个纠正。"""
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)
        db.register_source(conn, "s", "某公司", "feishu", "https://x", tenant="old")

        db.register_source(conn, "s", "某公司", "feishu", "https://x", tenant="new")

        row = conn.execute("SELECT tenant FROM sources WHERE source_key='s'").fetchone()
        assert row["tenant"] == "new"

    def test_positional_call_still_works(self, tmp_path):
        """tenant 是加在 notes 后面的关键字参数，老的位置调用不能被打断。"""
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)

        db.register_source(conn, "s", "某公司", "self_built", "https://x", "备注")

        row = conn.execute("SELECT * FROM sources WHERE source_key='s'").fetchone()
        assert row["notes"] == "备注" and row["tenant"] is None


def _seed_job(conn: sqlite3.Connection) -> int:
    """建一条真实的 source + job。applications.job_id 是外键，而 connect()
    打开了 PRAGMA foreign_keys=ON，所以不能直接塞假 job_id。"""
    db.register_source(conn, "tencent_join", "腾讯", "self_built", "https://join.qq.com")
    cur = conn.execute(
        """INSERT INTO jobs(source_key, external_id, company, title,
                            fingerprint, first_seen_at, last_seen_at)
           VALUES('tencent_join','J1','腾讯','产品经理','fp1',?,?)""",
        (db.now(), db.now()),
    )
    conn.commit()
    return int(cur.lastrowid)


LEGACY_SUBMISSIONS_DDL = """
CREATE TABLE submissions (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL,
    external_id     TEXT,
    source_key      TEXT,
    company         TEXT,
    submitted_at    TEXT,
    status          TEXT,
    error           TEXT,
    screenshot_path TEXT,
    created_at      TEXT
);
"""


def test_legacy_submission_without_time_is_migrated_once(tmp_path):
    """反复初始化不能复制同一条没有提交时间的历史失败记录。"""
    conn = db.connect(tmp_path / "legacy.db")
    db.init(conn)
    job_id = _seed_job(conn)
    conn.executescript(LEGACY_SUBMISSIONS_DDL)
    conn.execute(
        """INSERT INTO submissions(
               id, job_id, external_id, source_key, company,
               submitted_at, status, error, created_at)
           VALUES(1, ?, 'J1', 'tencent_join', '腾讯', NULL,
                  'failed', '提交结果未知', ?)""",
        (job_id, db.now()),
    )
    conn.commit()

    for _ in range(5):
        db.migrate(conn)

    migrated = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE note='从 submissions 表迁移'"
    ).fetchone()[0]
    used, _ = db.quota_state(conn, "腾讯")
    assert migrated == 1
    assert used == 1


def test_distinct_legacy_attempts_without_time_are_both_kept(tmp_path):
    """同一岗位的两次真实尝试不能因为提交时间都为空而合并。"""
    conn = db.connect(tmp_path / "legacy.db")
    db.init(conn)
    job_id = _seed_job(conn)
    conn.executescript(LEGACY_SUBMISSIONS_DDL)
    conn.executemany(
        """INSERT INTO submissions(
               id, job_id, external_id, source_key, company,
               submitted_at, status, error, created_at)
           VALUES(?, ?, 'J1', 'tencent_join', '腾讯', NULL,
                  'failed', ?, ?)""",
        (
            (1, job_id, "第一次结果未知", "2026-08-20T10:00:00+08:00"),
            (2, job_id, "第二次结果未知", "2026-08-20T11:00:00+08:00"),
        ),
    )
    conn.commit()

    db.migrate(conn)

    migrated = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE note='从 submissions 表迁移'"
    ).fetchone()[0]
    assert migrated == 2


def test_unrelated_null_application_does_not_hide_legacy_attempt(tmp_path):
    """现有 blocked 记录不能挡住同岗位的历史 failed 记录。"""
    conn = db.connect(tmp_path / "legacy.db")
    db.init(conn)
    job_id = _seed_job(conn)
    db.record_blocked(
        conn,
        job_id=job_id,
        source_key="tencent_join",
        external_id="J1",
        company="腾讯",
        error="需要登录",
    )
    conn.executescript(LEGACY_SUBMISSIONS_DDL)
    conn.execute(
        """INSERT INTO submissions(
               id, job_id, external_id, source_key, company,
               submitted_at, status, error, created_at)
           VALUES(1, ?, 'J1', 'tencent_join', '腾讯', NULL,
                  'failed', '提交结果未知', ?)""",
        (job_id, db.now()),
    )
    conn.commit()

    db.migrate(conn)

    migrated = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE note='从 submissions 表迁移'"
    ).fetchone()[0]
    used, _ = db.quota_state(conn, "腾讯")
    assert migrated == 1
    assert used == 1


def test_existing_migrated_row_is_linked_instead_of_copied(tmp_path):
    """升级前已经搬过的行只补旧记录序号，不再复制一份。"""
    conn = db.connect(tmp_path / "legacy.db")
    db.init(conn)
    job_id = _seed_job(conn)
    created_at = "2026-08-20T10:00:00+08:00"
    conn.execute(
        """INSERT INTO applications(
               job_id, source_key, external_id, company, status,
               submitted_at, error, note, created_at)
           VALUES(?, 'tencent_join', 'J1', '腾讯', 'failed',
                  NULL, '提交结果未知', '从 submissions 表迁移', ?)""",
        (job_id, created_at),
    )
    conn.executescript(LEGACY_SUBMISSIONS_DDL)
    conn.execute(
        """INSERT INTO submissions(
               id, job_id, external_id, source_key, company,
               submitted_at, status, error, created_at)
           VALUES(7, ?, 'J1', 'tencent_join', '腾讯', NULL,
                  'failed', '提交结果未知', ?)""",
        (job_id, created_at),
    )
    conn.commit()

    assert "legacy_submission_id" in _cols(conn, "applications")
    db.migrate(conn)

    rows = conn.execute(
        """SELECT legacy_submission_id FROM applications
           WHERE note='从 submissions 表迁移'"""
    ).fetchall()
    assert [row["legacy_submission_id"] for row in rows] == [7]


class TestConfirmTokenIsNullNotEmpty:
    """`confirm_token` 取不到时必须写 NULL，不能写空串。

    `applications` 上那条唯一索引的条件是 `WHERE confirm_token IS NOT NULL`。
    空串是**非 NULL**，会参与去重，于是第二条 blocked 就撞唯一约束了。
    这个坑踩过一次，见 docs/plans/001-代投人工闸门.md §12。

    这一组是 2026-08 补的：写方案 001 的复盘时声称「这个坑被 test_db_migrate 钉住」，
    核对时发现根本没有——`record_blocked` / `record_prefill` 当时零测试覆盖。
    与其把方案里那句话改软，不如把测试补上。
    """

    def test_two_blocked_rows_coexist(self, tmp_path):
        """两条 blocked 必须能共存。这是原始 bug 的最小复现。"""
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)
        job_id = _seed_job(conn)

        for err in ("岗位已关闭", "需要登录"):
            db.record_blocked(
                conn, job_id=job_id, source_key="tencent_join",
                external_id="J1", company="腾讯", error=err,
            )

        n = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE status='blocked'"
        ).fetchone()[0]
        assert n == 2, "两条 blocked 撞了唯一索引，说明 token 写成了空串"

    def test_blocked_writes_sql_null(self, tmp_path):
        """不只是「能插进去」，值本身必须是 SQL NULL。

        单查 `IS NULL` 而不是查 Python 的 None：空串取出来也是真值判断为假，
        用 `not row["confirm_token"]` 断言会把空串放过去。
        """
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)
        job_id = _seed_job(conn)

        db.record_blocked(
            conn, job_id=job_id, source_key="tencent_join",
            external_id="J1", company="腾讯", error="岗位已关闭",
        )

        n = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE confirm_token IS NULL"
        ).fetchone()[0]
        assert n == 1, "confirm_token 不是 SQL NULL，唯一索引会收下它"

    def test_prefill_converts_empty_token_to_null(self, tmp_path):
        """`prepare` 在 blocked 时给的是空串（`plan.confirm_token == ""`），
        `record_prefill` 里的 `confirm_token or None` 负责翻成 NULL。
        两条这样的记录也必须能共存。"""
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)
        job_id = _seed_job(conn)

        for _ in range(2):
            db.record_prefill(
                conn, job_id=job_id, source_key="tencent_join",
                external_id="J1", company="腾讯", confirm_token="",
                filled_fields=[], skipped_fields=[],
            )

        n = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE confirm_token IS NULL"
        ).fetchone()[0]
        assert n == 2

    def test_real_token_still_dedupes(self, tmp_path):
        """反向对照：真 token 重复时**必须**撞。

        少了这一条，上面三个测试在「索引压根没建出来」的情况下也会全绿——
        那就把「NULL 不参与去重」测成了「什么都不去重」。
        """
        conn = db.connect(tmp_path / "t.db")
        db.init(conn)
        job_id = _seed_job(conn)

        db.record_prefill(
            conn, job_id=job_id, source_key="tencent_join", external_id="J1",
            company="腾讯", confirm_token="tok-abc", filled_fields=[], skipped_fields=[],
        )

        with pytest.raises(sqlite3.IntegrityError):
            db.record_prefill(
                conn, job_id=job_id, source_key="tencent_join", external_id="J1",
                company="腾讯", confirm_token="tok-abc",
                filled_fields=[], skipped_fields=[],
            )


# M6 补列之前的 applications 表：把当前 schema 里属于 APPLICATION_COLUMNS
# 的八列去掉，其余照抄。
#
# 为什么 filled_fields / skipped_fields / submitted_at / note 留着：它们不在
# APPLICATION_COLUMNS 里，也就是说 migrate 不会补——真实老库本来就有这几列
# （applications 从一开始就是为「先填好、再确认、才提交」设计的）。写成只有
# 四列的极简表，测的就不是老库，而是一张现实中不存在的表。
#
# 为什么 created_at 去掉：它在 APPLICATION_COLUMNS 里。去掉之后这组测试同时
# 盖住 idx_apps_status——那条索引引用 created_at，和 idx_apps_token 是同一个
# 毛病，只是没人踩到过。
OLD_APPS_DDL = """
CREATE TABLE applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    submitted_at   TEXT,
    filled_fields  TEXT,
    skipped_fields TEXT,
    note           TEXT
);
"""


class TestIndexesBuiltAfterColumns:
    """引用补列的索引必须建在补列之后，而且只能有一个归属地。

    `init()` 是先 `executescript(schema.sql)`、再 `migrate()`。所以任何一条
    索引只要引用了 APPLICATION_COLUMNS 里的列，写在 schema.sql 里就会在老库上
    直接报 `no such column`——挂掉的是整个 init，不是「索引没建上」而已。

    这一组 2026-08 写出来时就抓到一个活的：`idx_apps_token` 当初的修法只做了
    一半，加到了 `db.migrate()`，却没从 `schema.sql` 删掉，两边都有。见
    docs/plans/001-代投人工闸门.md §12。
    """

    def test_old_db_migrates_and_gets_indexes(self, tmp_path):
        conn = db.connect(tmp_path / "old.db")
        conn.executescript(OLD_APPS_DDL)
        conn.commit()
        assert "confirm_token" not in _cols(conn, "applications")
        assert "created_at" not in _cols(conn, "applications")

        db.init(conn)          # 不许抛异常

        assert "confirm_token" in _cols(conn, "applications")
        idx = {
            r["name"]
            for r in conn.execute("PRAGMA index_list(applications)")
        }
        assert "idx_apps_token" in idx, "列补上了但索引没建，去重就是空的"
        assert "idx_apps_status" in idx, "同样引用补列，一起钉住"

    def test_schema_sql_declares_no_index_on_backfilled_columns(self):
        """守住归属：schema.sql 不许再出现引用 applications 补列的索引。

        直接读文件而不是跑 SQL——两边都写的时候新库照样全绿（表是全的，
        索引语句不会失败），只有老库会挂。所以这条得在源码层面拦。

        只看 `ON applications(...)` 的语句：APPLICATION_COLUMNS 是
        **applications 这张表**要补的列，同名列在别的表上未必是补的。
        `idx_runs_source ON runs(source_key, ...)` 就是这种情况——runs
        从一开始就有 source_key，不该被这条规则拦下来。
        """
        sql = db.SCHEMA_PATH.read_text(encoding="utf-8")
        backfilled = {col for col, _ in db.APPLICATION_COLUMNS}

        for stmt in sql.split(";"):
            body = " ".join(
                ln.strip() for ln in stmt.splitlines()
                if not ln.strip().startswith("--")
            )
            body = " ".join(body.split())
            upper = body.upper()
            if "INDEX" not in upper or "ON APPLICATIONS" not in upper:
                continue
            for col in backfilled:
                assert col not in body, (
                    f"schema.sql 里这条索引引用了补列 {col}，老库 init 会挂：{body}"
                )

    def test_migrated_old_db_enforces_the_index(self, tmp_path):
        """迁移出来的索引得真的管事，不能只是名字在那儿。"""
        conn = db.connect(tmp_path / "old.db")
        conn.executescript(OLD_APPS_DDL)
        conn.commit()
        db.init(conn)
        job_id = _seed_job(conn)

        db.record_prefill(
            conn, job_id=job_id, source_key="tencent_join", external_id="J1",
            company="腾讯", confirm_token="tok-1", filled_fields=[], skipped_fields=[],
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.record_prefill(
                conn, job_id=job_id, source_key="tencent_join", external_id="J1",
                company="腾讯", confirm_token="tok-1",
                filled_fields=[], skipped_fields=[],
            )
