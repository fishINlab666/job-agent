"""只读查询层测试。

这一层是从 `cli.jobs` / `cli.status` 的函数体里抽出来的，而那两条命令**在抽之前
一个测试都没有**（`tests/test_cli.py` 只覆盖 refresh-grad-year / applications /
source-add / digest）。所以这个文件同时干两件事：给新层立不变量，
和给那两条命令补上它们一直缺的网。

最后一个类守的不是行为而是**归属**：抽取式重构最典型的半成品是「加到了新地方、
没从老地方删掉」，两份逻辑都在的时候常见路径照样全绿。
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobagent import cli, db, queries

runner = CliRunner()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """临时库 + 两个源。CLI 的 `db.connect()` 也一并指过来。"""
    path = tmp_path / "q.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    c = db.connect(path)
    db.init(c)
    db.register_source(c, "tencent_join", "腾讯", "tencent_join",
                       "https://join.qq.com/post.html")
    db.register_source(c, "feishu:nio", "蔚来", "feishu",
                       "https://nio.jobs.feishu.cn/", tenant="nio")
    yield c
    c.close()


def seed_job(
    c,
    ext_id: str,
    *,
    source_key: str = "tencent_join",
    company: str = "腾讯",
    title: str = "产品运营",
    family: str | None = "operations",
    cities: object = '["深圳"]',
    recruit_type: str | None = "campus",
    grad_year: str | None = "26",
    closed: bool = False,
) -> None:
    c.execute(
        """INSERT INTO jobs(source_key, external_id, company, title, job_family,
               cities, recruit_type, grad_year, apply_url, apply_system,
               fingerprint, first_seen_at, last_seen_at, closed_at)
           VALUES(?,?,?,?,?,?,?,?,'https://x','tencent_join','fp',?,?,?)""",
        (source_key, ext_id, company, title, family, cities, recruit_type,
         grad_year, db.now(), db.now(), db.now() if closed else None),
    )
    c.commit()


INTENT = {
    "families": ["operations", "product"],
    "recruit_types": ["campus", "intern"],
    "grad_years": ["26", "27"],
    "cities": ["深圳", "北京"],
}


class TestOpenJobs:

    def test_closed_jobs_are_excluded(self, conn) -> None:
        seed_job(conn, "open1")
        seed_job(conn, "gone1", closed=True)
        rows, _ = queries.open_jobs(conn)
        assert [r["external_id"] for r in rows] == ["open1"]

    def test_family_filter_is_equality_not_substring(self, conn) -> None:
        """族筛选是等值，不是包含。

        用 `operation`（少一个 s）来问：等值实现答 0 条，包含实现会把
        `operations` 捞出来。两个族名互为前缀时，包含式实现给的是「看着对、
        其实多了一批」的答案，而多出来的那批不会有任何提示。
        """
        seed_job(conn, "ops", family="operations")
        seed_job(conn, "prod", family="product")
        rows, _ = queries.open_jobs(conn, family="operations")
        assert [r["external_id"] for r in rows] == ["ops"]
        rows, _ = queries.open_jobs(conn, family="operation")
        assert rows == []

    def test_none_family_matches_no_filter(self, conn) -> None:
        """`job_family` 判不出来（NULL）的岗位按族筛永远查不到。

        **这条锁的是当前行为，也是 issue #8 那块地。** 判不出族的岗位落 NULL，
        而筛选是等值比较，所以 `--family operations` 和 `--family other` 都不会
        命中它 —— 它不属于任何族，也不在「其他」里。
        这不是 bug，是「判不出就是判不出、不兜底成 other」那个决定的下游后果
        （见 adapters/feishu.py 里 job_family 那行的注释）。改口径之前这条得先红。
        """
        seed_job(conn, "unknown_fam", family=None)
        for fam in ("operations", "other", "tech"):
            rows, _ = queries.open_jobs(conn, family=fam)
            assert rows == [], f"--family {fam} 不该命中族为 NULL 的岗位"
        # 但不带筛选时它必须在，不能被静默扣掉
        rows, _ = queries.open_jobs(conn)
        assert [r["external_id"] for r in rows] == ["unknown_fam"]

    def test_city_filter_reads_json_column(self, conn) -> None:
        seed_job(conn, "sz", cities='["深圳"]')
        seed_job(conn, "bj", cities='["北京", "上海"]')
        rows, _ = queries.open_jobs(conn, city="上海")
        assert [r["external_id"] for r in rows] == ["bj"]

    def test_city_filter_survives_broken_json(self, conn) -> None:
        """`cities` 存坏了不该让整个查询炸掉，那条岗位只是筛不到城市。"""
        seed_job(conn, "bad", cities="{不是合法 JSON")
        rows, _ = queries.open_jobs(conn)
        assert rows[0]["cities"] == []
        rows, _ = queries.open_jobs(conn, city="深圳")
        assert rows == []

    def test_empty_cities_is_not_a_wildcard(self, conn) -> None:
        """城市没写 ≠ 哪都行。空 list 按城市筛不该命中。"""
        seed_job(conn, "nocity", cities=None)
        rows, _ = queries.open_jobs(conn, city="深圳")
        assert rows == []

    def test_recruit_type_and_company_filters(self, conn) -> None:
        seed_job(conn, "a", recruit_type="campus")
        seed_job(conn, "b", recruit_type="intern")
        seed_job(conn, "c", source_key="feishu:nio", company="蔚来")
        rows, _ = queries.open_jobs(conn, recruit_type="intern")
        assert [r["external_id"] for r in rows] == ["b"]
        rows, _ = queries.open_jobs(conn, company="蔚来")
        assert [r["external_id"] for r in rows] == ["c"]

    def test_cities_come_back_as_a_list(self, conn) -> None:
        """库里存的是 JSON 串，交出去必须是 list。

        留成字符串的话 `"北京" in '["北京海淀"]'` 这类子串巧合会静默命中，
        而调用方看不出自己拿到的是哪一种。
        """
        seed_job(conn, "x", cities='["深圳", "北京"]')
        rows, _ = queries.open_jobs(conn)
        assert rows[0]["cities"] == ["深圳", "北京"]

    def test_no_truncation_here(self, conn) -> None:
        """这一层不截断：CLI 要在标题里打「共 N 条」，截断之后就数不出 N。"""
        for i in range(5):
            seed_job(conn, f"j{i}")
        rows, _ = queries.open_jobs(conn)
        assert len(rows) == 5


class TestAllowMissing:

    def test_unknown_dimension_raises_valueerror(self, conn) -> None:
        """写错维度名要炸，不许静默忽略。

        静默忽略的后果是「以为放宽了某一维、实际一维都没放宽」，用户看到的条数
        偏少，而少掉的恰好是他刚要求要看的那批。
        """
        with pytest.raises(ValueError) as exc:
            queries.open_jobs(conn, matched=True, allow_missing=["nonsense"])
        assert "nonsense" in str(exc.value)
        assert "job_family" in str(exc.value)      # 报错要列出可选值

    def test_it_raises_valueerror_not_typer_error(self, conn) -> None:
        """这一层不许依赖 typer —— 依赖了就把 MCP 绑在 CLI 上。"""
        import typer
        with pytest.raises(ValueError) as exc:
            queries.open_jobs(conn, allow_missing=["nope"])
        assert not isinstance(exc.value, typer.BadParameter)

    def test_allow_missing_without_matched_is_reported_not_swallowed(
        self, conn
    ) -> None:
        seed_job(conn, "a")
        rows, notes = queries.open_jobs(conn, allow_missing=["grad_year"])
        assert any("只在 --matched 下生效" in n for n in notes), \
            "放宽维度被忽略了却没提醒，用户会以为放宽生效了"

    def test_allowing_a_dimension_admits_only_that_dimension(self, conn) -> None:
        """只放开届别，就只该多出「只差届别」的那些，不该连族未知的一起进来。"""
        seed_job(conn, "only_gy_missing", grad_year=None)
        seed_job(conn, "fam_missing", family=None)
        rows, _ = queries.open_jobs(
            conn, matched=True, allow_missing=["grad_year"], intent=INTENT
        )
        ids = {r["external_id"] for r in rows}
        assert "only_gy_missing" in ids
        assert "fam_missing" not in ids

    def test_matched_without_allowance_gives_only_certain_hits(self, conn) -> None:
        seed_job(conn, "sure")
        seed_job(conn, "unsure", grad_year=None)
        rows, _ = queries.open_jobs(conn, matched=True, intent=INTENT)
        assert [r["external_id"] for r in rows] == ["sure"]


class TestExplainMatch:

    def test_missing_job_returns_none(self, conn) -> None:
        assert queries.explain_match(conn, "nope") is None

    def test_hit_reports_state_and_score(self, conn) -> None:
        seed_job(conn, "good")
        out = queries.explain_match(conn, "good", INTENT)
        assert out["state"] == "hit"
        assert out["score"] > 0
        assert out["cities"] == ["深圳"]

    def test_incomplete_job_is_unknown_not_miss(self, conn) -> None:
        """信息不全必须是第三态。折进 miss 会让这批岗位被静默扣掉。"""
        seed_job(conn, "partial", grad_year=None)
        out = queries.explain_match(conn, "partial", INTENT)
        assert out["state"] == "unknown"
        assert "grad_year" in out["missing"]

    def test_excluded_job_is_miss_with_a_reason(self, conn) -> None:
        seed_job(conn, "wrongfam", family="tech")
        out = queries.explain_match(conn, "wrongfam", INTENT)
        assert out["state"] == "miss"
        assert "tech" in out["reason"]

    def test_closed_job_is_still_explainable(self, conn) -> None:
        """关掉的岗位也要能问「为什么」，否则投递记录里的历史岗位查不动。"""
        seed_job(conn, "shut", closed=True)
        out = queries.explain_match(conn, "shut", INTENT)
        assert out is not None and out["closed_at"]


class TestSourceHealth:

    def test_source_without_runs_reports_none_not_a_dash(self, conn) -> None:
        """一次都没跑过的源 `last_run` 是 None。

        填一个 "-" 字符串会把「没跑过」和「跑过但状态未知」混成一件事：
        前者该去 sync，后者该去查错。
        """
        rows = {s["source_key"]: s for s in queries.source_health(conn)}
        assert rows["feishu:nio"]["last_run"] is None

    def test_latest_run_wins(self, conn) -> None:
        r1 = db.start_run(conn, "tencent_join")
        db.finish_run(conn, r1, "failed", fetched=0, error="旧的那次挂了")
        r2 = db.start_run(conn, "tencent_join")
        db.finish_run(conn, r2, "ok", fetched=42)
        rows = {s["source_key"]: s for s in queries.source_health(conn)}
        assert rows["tencent_join"]["last_run"]["status"] == "ok"
        assert rows["tencent_join"]["last_run"]["fetched"] == 42

    def test_open_jobs_counted_per_source(self, conn) -> None:
        seed_job(conn, "t1")
        seed_job(conn, "t2")
        seed_job(conn, "t3", closed=True)
        seed_job(conn, "n1", source_key="feishu:nio", company="蔚来")
        rows = {s["source_key"]: s for s in queries.source_health(conn)}
        assert rows["tencent_join"]["open_jobs"] == 2      # 关掉的那条不算
        assert rows["feishu:nio"]["open_jobs"] == 1

    def test_quota_is_counted_by_company_not_source(self, conn) -> None:
        """同一家公司多行源时，用量必须合起来算。

        按 source_key 数会把用量拆成两份、每份都不到上限，于是投穿 ——
        这正是 apply_limit 要防的那个方向。
        """
        db.register_source(conn, "feishu:nio:campus", "蔚来", "feishu",
                           "https://nio.jobs.feishu.cn/", tenant="nio",
                           apply_limit=3)
        seed_job(conn, "n1", source_key="feishu:nio", company="蔚来")
        job_id = conn.execute(
            "SELECT id FROM jobs WHERE external_id='n1'"
        ).fetchone()["id"]
        for i, src in enumerate(("feishu:nio", "feishu:nio:campus")):
            conn.execute(
                """INSERT INTO applications(job_id, source_key, external_id,
                       company, status, created_at)
                   VALUES(?,?,?,'蔚来','submitted',?)""",
                (job_id, src, f"n{i}", db.now()),
            )
        conn.commit()
        rows = {s["source_key"]: s for s in queries.source_health(conn)}
        for key in ("feishu:nio", "feishu:nio:campus"):
            assert rows[key]["apply_used"] == 2, f"{key} 的用量没按公司合并"
            assert rows[key]["apply_limit"] == 3
            assert rows[key]["apply_remaining"] == 1

    def test_no_limit_means_none_not_zero(self, conn) -> None:
        """上限拿不到就是 None。填 0 等于「一个都不许投」，方向正好反了。"""
        rows = {s["source_key"]: s for s in queries.source_health(conn)}
        assert rows["tencent_join"]["apply_limit"] is None
        assert rows["tencent_join"]["apply_remaining"] is None


class TestSyncRuns:

    def test_newest_first_and_limited(self, conn) -> None:
        for _ in range(4):
            db.finish_run(conn, db.start_run(conn, "tencent_join"), "ok", fetched=1)
        rows = queries.sync_runs(conn, limit=2)
        assert len(rows) == 2
        assert rows[0]["id"] > rows[1]["id"]

    def test_filter_by_source(self, conn) -> None:
        db.start_run(conn, "tencent_join")
        db.start_run(conn, "feishu:nio")
        rows = queries.sync_runs(conn, source_key="feishu:nio")
        assert [r["source_key"] for r in rows] == ["feishu:nio"]

    def test_unfinished_run_keeps_a_null_finished_at(self, conn) -> None:
        """没收尾的那一轮 `finished_at` 留空。

        补一个时间等于把痕迹擦掉 —— `runs` 那行是先落盘的，就是为了让崩掉的
        一轮留得下「开过、没收尾」的证据。
        """
        db.start_run(conn, "tencent_join")
        rows = queries.sync_runs(conn)
        assert rows[0]["finished_at"] is None
        assert rows[0]["status"] == "running"


class TestJobChanges:

    def test_payload_comes_back_as_a_dict(self, conn) -> None:
        db.add_event(conn, "job_opened", company="腾讯", payload={"n": 3})
        conn.commit()
        rows = queries.job_changes(conn)
        assert rows[0]["payload"] == {"n": 3}

    def test_broken_payload_is_surfaced_not_swallowed(self, conn) -> None:
        """payload 存坏了要留下原文。

        悄悄吞成 {} 会让「这次 diff 什么都没有」和「diff 存坏了」看起来一样。
        """
        conn.execute(
            """INSERT INTO events(kind, payload, occurred_at)
               VALUES('job_updated','{坏了', ?)""",
            (db.now(),),
        )
        conn.commit()
        rows = queries.job_changes(conn)
        assert rows[0]["payload"] == {}
        assert rows[0]["payload_raw"] == "{坏了"

    def test_filter_by_kind_and_since(self, conn) -> None:
        db.add_event(conn, "job_opened", company="腾讯", payload={})
        db.add_event(conn, "job_closed", company="腾讯", payload={})
        conn.commit()
        rows = queries.job_changes(conn, kind="job_closed")
        assert [r["kind"] for r in rows] == ["job_closed"]
        assert queries.job_changes(conn, since="2999-01-01") == []


class TestCliDelegatesInsteadOfKeepingItsOwnCopy:
    """守**归属**，不是守行为。

    抽取式重构最典型的半成品是「加到了新地方、没从老地方删掉」：两份筛选逻辑
    同时在的时候，常见路径照样全绿，只有改了一边才会分叉 —— 而分叉的表现是
    CLI 和 MCP 对同一个问题给出不同答案，谁也不报错。

    所以这几条把 `queries` 的函数换成抛异常的，断言 CLI 会跟着挂。老代码还留在
    `cli.py` 里的话，CLI 根本不会调到被换掉的函数，这几条就不红。
    """

    def test_cli_jobs_calls_queries_open_jobs(self, conn, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("SENTINEL_open_jobs")
        monkeypatch.setattr(queries, "open_jobs", boom)
        res = runner.invoke(cli.app, ["jobs"])
        assert res.exit_code != 0
        assert "SENTINEL_open_jobs" in str(res.exception)

    def test_cli_status_calls_queries_source_health(self, conn, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("SENTINEL_source_health")
        monkeypatch.setattr(queries, "source_health", boom)
        res = runner.invoke(cli.app, ["status"])
        assert res.exit_code != 0
        assert "SENTINEL_source_health" in str(res.exception)

    def test_cli_still_translates_the_error_into_exit_2(self, conn) -> None:
        """`queries` 抛 ValueError，CLI 要翻成 typer 的用法错误（exit 2）。

        不翻的话用户看到的是一条 traceback，而这只是打错了一个参数名。
        """
        res = runner.invoke(cli.app, ["jobs", "--allow-missing", "nonsense"])
        assert res.exit_code == 2
        assert "不认识的维度" in res.output
