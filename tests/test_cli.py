"""CLI 层测试。

这个文件在 refresh-grad-year 之前不存在 —— 之前所有测试都打在函数层。
加它的理由是本命令的闸门（默认不写库）在 **CLI 参数** 上：`--apply` 这个 flag
存不存在、默不默认，函数层测不到。函数层的那套不变量在
`tests/test_ingest.py::TestGradYearRefresh` 里，两边不重复。
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jobagent import cli, db, match

runner = CliRunner()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把 CLI 的默认库指到临时文件，并把腾讯这个源登记好。

    CLI 里是 `db.connect()` 不带参数，取的是模块级 `DB_PATH`。不改这个的话
    测试会打到真库 —— 而本命令是批量写库的，那是不可接受的测试副作用。

    源和 run 必须先建：`jobs.source_key` 和 `snapshots.run_id` 都是外键
    （后者还是 NOT NULL）。第一版这里直接插 jobs、run_id 传 NULL，
    六条用例全挂在 FOREIGN KEY constraint failed 上。
    """
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.connect(path)
    db.init(conn)
    db.register_source(
        conn, "tencent_join", "腾讯", "tencent_join", "https://join.qq.com/post.html"
    )
    db.start_run(conn, "tencent_join")
    yield conn
    conn.close()


def seed(conn, ext_id: str, project_id: int, grad_year: str | None) -> None:
    """造一行腾讯岗位 + 它的快照。

    刷新的输入是 `snapshots.raw_json`，所以两张表都要有 —— 只有 jobs 行时
    重算不出东西（那种情况本身有一条用例：`test_reports_rows_without_snapshot`）。

    run_id 在这里查而不是由 fixture 传：`sqlite3.Connection` 没有 `__dict__`，
    挂不上属性（第一版就是那么写的，九条用例全 AttributeError）。
    """
    run_id = conn.execute(
        "SELECT MAX(id) AS id FROM runs WHERE source_key='tencent_join'"
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO jobs(source_key, external_id, company, title, job_family,
               cities, recruit_type, grad_year, apply_url, apply_system,
               fingerprint, first_seen_at, last_seen_at)
           VALUES('tencent_join',?,'腾讯','产品运营','operations','["深圳"]',
                  'campus',?,'https://join.qq.com/x','tencent_join','fp',?,?)""",
        (ext_id, grad_year, db.now(), db.now()),
    )
    conn.execute(
        """INSERT INTO snapshots(run_id, source_key, external_id, fingerprint,
               raw_json, captured_at)
           VALUES(?,'tencent_join',?, 'fp', ?, ?)""",
        (run_id, ext_id, json.dumps({"projectId": project_id}), db.now()),
    )
    conn.commit()


def gy(conn, ext_id: str) -> str | None:
    return conn.execute(
        "SELECT grad_year FROM jobs WHERE external_id=?", (ext_id,)
    ).fetchone()["grad_year"]


class TestRefreshGradYear:
    """`refresh-grad-year` 的 CLI 形状。函数层不变量见 test_ingest.py。"""

    def test_dry_run_is_the_default(self, tmp_db) -> None:
        """不带 --apply 不写库。**硬约束**，把 apply 默认改成 True 时红。

        一次改几百行的命令，闸门必须在 API 形状上 —— 而不是靠用户记得
        先预演一次。
        """
        seed(tmp_db, "1", 4, "27")  # pid=4 日常实习 → 不限

        r = runner.invoke(cli.app, ["refresh-grad-year", "--source", "tencent_join"])

        assert r.exit_code == 0, r.output
        assert gy(tmp_db, "1") == "27", "预演写了库"
        assert "会更新" in r.output
        assert "--apply" in r.output, "预演必须告诉用户怎么才能真写"

    def test_apply_writes(self, tmp_db) -> None:
        """--apply 真写库，且按 009 的规则写。

        断言库里的值，不是 rowcount —— rowcount 只说明走了 UPDATE 分支。
        """
        seed(tmp_db, "1", 1, "27")   # pid=1 应届毕业生 → 26
        seed(tmp_db, "2", 2, "27")   # pid=2 应届实习（本地例外）→ 26
        seed(tmp_db, "3", 4, "27")   # pid=4 日常实习 → 不限

        r = runner.invoke(
            cli.app, ["refresh-grad-year", "--source", "tencent_join", "--apply"]
        )

        assert r.exit_code == 0, r.output
        assert (gy(tmp_db, "1"), gy(tmp_db, "2"), gy(tmp_db, "3")) == ("26", "26", "不限")
        assert "已更新" in r.output

    def test_output_reports_transitions_not_just_a_total(self, tmp_db) -> None:
        """输出要有「从什么改成什么、多少条」。

        只报「改了 N 行」的话用户没法判断这次刷新对不对 —— 441 这个数字
        本身不携带任何可核对的信息，而 `'27' → '不限' 348 条` 可以当场看出
        是不是自己想要的那次改动。
        """
        seed(tmp_db, "1", 4, "27")   # pid=4 日常实习 → 不限
        seed(tmp_db, "2", 4, "27")   # pid=4 日常实习 → 不限
        seed(tmp_db, "3", 1, "27")   # pid=1 应届毕业生 → 26

        r = runner.invoke(cli.app, ["refresh-grad-year", "--source", "tencent_join"])

        assert "'27'" in r.output and "'不限'" in r.output and "'26'" in r.output
        assert "2 条" in r.output and "1 条" in r.output

    def test_idempotent_second_run_says_nothing_to_do(self, tmp_db) -> None:
        """跑第二次报「已是最新」，不是报「改了 0 行」那种含糊话。"""
        seed(tmp_db, "1", 4, "27")   # pid=4 日常实习 → 不限

        runner.invoke(cli.app, ["refresh-grad-year", "--source", "tencent_join", "--apply"])
        r = runner.invoke(cli.app, ["refresh-grad-year", "--source", "tencent_join", "--apply"])

        assert r.exit_code == 0
        assert "已是最新" in r.output
        assert gy(tmp_db, "1") == "不限"

    def test_reports_skipped_would_null(self, tmp_db) -> None:
        """重算掉成空值时，跳过并**明说**。

        静默保留旧值和静默覆盖一样坏：两者都让人看不出源站已经改了标签。
        这条断言的是「有没有说」，方向性守卫本身在 test_ingest.py 里。
        """
        seed(tmp_db, "1", 999, "不限")   # pid=999 认不出 → None，但库里有值

        r = runner.invoke(
            cli.app, ["refresh-grad-year", "--source", "tencent_join", "--apply"]
        )

        assert gy(tmp_db, "1") == "不限"
        assert "跳过" in r.output
        assert "不拿空值覆盖好值" in r.output

    def test_reports_rows_without_snapshot(self, tmp_db) -> None:
        """没有快照的行重算不了，要报出来而不是当成「本来就对」。"""
        # 只插 jobs 行，故意不给它快照
        tmp_db.execute(
            """INSERT INTO jobs(source_key, external_id, company, title, job_family,
                   cities, recruit_type, grad_year, apply_url, apply_system,
                   fingerprint, first_seen_at, last_seen_at)
               VALUES('tencent_join','9','腾讯','产品运营','operations','["深圳"]',
                      'campus','27','https://join.qq.com/x','tencent_join','fp',?,?)""",
            (db.now(), db.now()),
        )
        tmp_db.commit()

        r = runner.invoke(cli.app, ["refresh-grad-year", "--source", "tencent_join"])

        assert r.exit_code == 0
        assert "没有快照" in r.output

    def test_unsupported_source_exits_zero_with_explanation(
        self, tmp_db, monkeypatch
    ) -> None:
        """源站本来就没有届别通道时，说清楚并正常退出。

        「没得刷」和「认不出这个源」是两件事，退出码必须分开：前者 0，后者 1
        （下一条守后者）。用非零退出码表达「没得刷」会让脚本以为刷新失败了。

        **这个场景现在得用假适配器构造。** 2026-08-10 之前它是拿飞书当例子的
        —— 飞书那时确实没有届别通道。plan 011 给飞书加了 `job_subject` 通道
        （`grad_year_from_raw`），两个真实适配器现在都支持刷新了，
        所以这条分支只剩「将来新增的源」会走。留着它是因为那条分支还在，
        而它静默跳过的后果（被当成不支持刷新）比报错难查。
        """

        class NoGradYearAdapter:
            """没有 grad_year_from_raw 的适配器 —— 将来新增源的最小形状。"""

            source_key = "feishu:nio:campus"

            def fetch(self):
                return []

        monkeypatch.setattr(
            cli.routing, "get_adapter", lambda *a, **k: NoGradYearAdapter()
        )
        tmp_db.execute(
            """INSERT INTO sources(source_key, company, system, entry_url, enabled, tenant)
               VALUES('feishu:nio:campus','蔚来','feishu',
                      'https://nio.jobs.feishu.cn',1,'nio')"""
        )
        tmp_db.commit()

        r = runner.invoke(
            cli.app, ["refresh-grad-year", "--source", "feishu:nio:campus"]
        )

        assert r.exit_code == 0, r.output
        assert "不需要刷新" in r.output

    def test_feishu_now_supports_refresh(self, tmp_db) -> None:
        """反向用例：飞书**有**届别通道了，不许再走「不需要刷新」那条路。

        plan 011 加了 `job_subject` 通道。谁哪天把 `grad_year_from_raw` 从
        FeishuAdapter 上删掉（或改成实例方法），这条会红 —— 而如果只靠上一条
        用例，删掉之后它反而变绿，通道没了却没人知道。
        """
        from jobagent.adapters.feishu import FeishuAdapter

        assert getattr(FeishuAdapter, "grad_year_from_raw", None) is not None
        # 类上直取，不经实例 —— refresh_grad_year 就是这么取的
        assert FeishuAdapter.grad_year_from_raw(
            {"job_subject": {"name": {"zh_cn": "2027届校园招聘"}}}
        ) == "27"

    def test_unknown_source_fails_loudly(self, tmp_db) -> None:
        """认不出的源用非零退出码 —— 这个是真的错，不能和上一条混。"""
        r = runner.invoke(cli.app, ["refresh-grad-year", "--source", "no_such_src"])

        assert r.exit_code == 1
        assert "认不出这个源" in r.output

    def test_source_is_required(self, tmp_db) -> None:
        """不许省略 --source。

        默认 all 会让「刷新腾讯」这个动作在不知不觉中扫过所有源。
        这条命令是批量写库的，作用域必须由调用方明确说出来。
        """
        r = runner.invoke(cli.app, ["refresh-grad-year"])

        assert r.exit_code != 0


def app_row(
    conn,
    *,
    ext_id: str = "j1",
    status: str = "blocked",
    job_id: int | None = None,
    orphan: bool = False,
    error: str | None = None,
    company: str = "腾讯",
    created_at: str | None = None,
    filled: str | None = None,
    skipped: str | None = None,
    submitted_at: str | None = None,
) -> int:
    """直接插一行 applications。

    不走 db.record_blocked 是故意的：本测试要造 record_* 造不出来的形状
    （孤儿行、第 8 个状态、乱序 created_at），那些正是渲染层会出错的地方。

    默认会顺手建一条对应的 jobs 行（正常形状）。`orphan=True` 才造孤儿行，
    **而造孤儿需要临时关掉 FK**：`job_id` 上有 `REFERENCES jobs(id)`，而
    `db.connect()` 会 `PRAGMA foreign_keys=ON`，所以正常情况下孤儿行根本
    插不进来（这是好事）。关掉 FK 插进去，模拟的是**唯一真能产生孤儿的路径**：
    有人手工修库 / 从备份恢复时把 jobs 重建了一遍。见 012 §4 的更正。
    """
    if orphan:
        conn.execute("PRAGMA foreign_keys=OFF")
        job_id = 99999
    elif job_id is None:
        # 同一个 ext_id 重复调用要复用同一条 jobs 行 —— 「一个岗位被投了多次」
        # 是真实形状（腾讯那个岗位重试了 7 次），不是异常。
        row = conn.execute(
            "SELECT id FROM jobs WHERE source_key='tencent_join' AND external_id=?",
            (ext_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO jobs(source_key, external_id, company, title,
                       fingerprint, first_seen_at, last_seen_at)
                   VALUES('tencent_join',?,?,?,?,?,?)""",
                (ext_id, company, f"岗位-{ext_id}", f"fp-{ext_id}",
                 db.now(), db.now()),
            )
            row = conn.execute(
                "SELECT id FROM jobs WHERE source_key='tencent_join' AND external_id=?",
                (ext_id,),
            ).fetchone()
        job_id = int(row["id"])
    cur = conn.execute(
        """INSERT INTO applications(
               job_id, source_key, external_id, company, status, error,
               filled_fields, skipped_fields, submitted_at, created_at)
           VALUES(?,'tencent_join',?,?,?,?,?,?,?,?)""",
        (
            job_id,
            ext_id, company, status, error, filled, skipped,
            submitted_at, created_at or db.now(),
        ),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")  # 只关这一次插入，别泄漏到别的用例
    return int(cur.lastrowid)


def job_id_of(conn, ext_id: str) -> int:
    return conn.execute(
        "SELECT id FROM jobs WHERE external_id=?", (ext_id,)
    ).fetchone()["id"]


class TestApplications:
    """`applications` 的 CLI 形状。方案见 docs/plans/012。

    这条命令是只读的，所以这里测的全是「读出来的东西对不对」，
    重点在两处渲染层的坑：孤儿行会不会消失、NULL 的 JSON 列会不会炸。
    """

    def test_lists_rows(self, tmp_db) -> None:
        seed(tmp_db, "j1", 100, "2026")
        app_row(tmp_db, job_id=job_id_of(tmp_db, "j1"), error="需要登录")

        r = runner.invoke(cli.app, ["applications"])

        assert r.exit_code == 0
        assert "投递记录 1 条" in r.output
        assert "被拦" in r.output

    def test_lists_source_and_external_job_id(self, tmp_db, monkeypatch) -> None:
        seed(tmp_db, "COPY-ME-APP", 100, "2026")
        app_row(
            tmp_db,
            ext_id="COPY-ME-APP",
            job_id=job_id_of(tmp_db, "COPY-ME-APP"),
        )
        monkeypatch.setattr(cli.console, "width", 240)

        result = runner.invoke(cli.app, ["applications"])

        assert result.exit_code == 0, result.output
        assert "tencent_join" in result.output
        assert "COPY-ME-APP" in result.output

    def test_empty_says_so(self, tmp_db) -> None:
        """空表要明说「没有」，不是打一张空表框。"""
        r = runner.invoke(cli.app, ["applications"])

        assert r.exit_code == 0
        assert "没有" in r.output

    def test_orphan_application_still_listed(self, tmp_db) -> None:
        """孤儿行照样出现。**这条是「做一半会红」的那条。**

        把 LEFT JOIN 写成 JOIN 时，正常数据全绿，只有这条静默消失 ——
        而投递记录消失比岗位消失严重得多：它是不可撤销动作的唯一凭证。
        """
        seed(tmp_db, "j1", 100, "2026")
        app_row(tmp_db, ext_id="j1", job_id=job_id_of(tmp_db, "j1"))
        app_row(tmp_db, ext_id="gone-job", orphan=True)  # jobs 里没有这行

        r = runner.invoke(cli.app, ["applications"])

        assert r.exit_code == 0
        assert "投递记录 2 条" in r.output, "孤儿行被 JOIN 吃掉了"

    def test_orphan_shows_external_id(self, tmp_db) -> None:
        """孤儿行的标题退化成 external_id，不是空白也不是 None。

        空白会被读成「这条记录坏了」，而它其实是完好的记录 + 消失的岗位。
        """
        app_row(tmp_db, ext_id="gone-job", orphan=True)

        r = runner.invoke(cli.app, ["applications"])

        assert "None" not in r.output
        # 表格会按列宽截断，所以只断言前缀能看见
        assert "gone-job" in r.output or "岗位行已不在" in r.output

    def test_null_json_columns_render(self, tmp_db) -> None:
        """filled_fields / skipped_fields 是 NULL 时不炸。

        blocked 行这两列必然是 NULL（表单根本没见到过，无从填起），
        直接 json.loads(None) 会抛 TypeError。
        """
        app_row(tmp_db, status="blocked", filled=None, skipped=None)

        r = runner.invoke(cli.app, ["applications"])

        assert r.exit_code == 0, r.output
        assert r.exception is None

    def test_ordering_uses_created_at(self, tmp_db) -> None:
        """按 created_at 倒序，不是 submitted_at。

        submitted_at 在 blocked/prefilled 行全是 NULL，拿它排序等于让这些行
        的顺序变成未定义。这里故意让「早创建的那条有 submitted_at」，
        用 submitted_at 排序就会把它排到前面。
        """
        app_row(tmp_db, ext_id="older", created_at="2026-08-01T00:00:00+08:00",
                status="submitted", submitted_at="2026-08-09T23:00:00+08:00")
        app_row(tmp_db, ext_id="newer", created_at="2026-08-09T00:00:00+08:00")

        r = runner.invoke(cli.app, ["applications"])

        assert r.output.index("newer") < r.output.index("older"), \
            "排序用了 submitted_at"

    def test_funnel_buckets_all_statuses(self, tmp_db) -> None:
        """所有已知状态各自归档，尤其 duplicate/closed 不算结果未知。

        把它们混进失败档会让「代投不好用」这个结论凭空多出两类本来正常的
        记录：duplicate 是「已经投过了」，closed 是「岗位关了」。
        """
        for st in cli.APP_STATUSES:
            app_row(tmp_db, ext_id=f"j-{st}", status=st)

        r = runner.invoke(cli.app, ["applications", "--funnel"])

        assert r.exit_code == 0, r.output
        assert "警告" not in r.output, "已知状态不该触发未知告警"
        # 新 unknown 与历史 failed 都要求人工核对，不能诱导自动重试；
        # duplicate+closed 仍归在无需投递（2 条）。
        lines = [ln for ln in r.output.splitlines() if "结果未知" in ln]
        assert lines and "2" in lines[0], lines
        no_need = [ln for ln in r.output.splitlines() if "无需投递" in ln]
        assert no_need and "2" in no_need[0], no_need

    def test_unknown_status_is_surfaced(self, tmp_db) -> None:
        """库里出现第 8 个状态时显式报出。**这条是「做一半会红」的那条。**

        静默归进「其他」桶的话，这个 weird 状态会被算进某一档，测试照样全绿,
        而口径已经错了。见 012 §6：静默归类就是下一次口径事故。
        """
        app_row(tmp_db, status="weird_new_status")

        r = runner.invoke(cli.app, ["applications", "--funnel"])

        assert r.exit_code == 0, r.output
        assert "警告" in r.output
        assert "weird_new_status" in r.output

    def test_bad_status_option_lists_choices(self, tmp_db) -> None:
        """不认识的 --status 要报错并列出可选值，不能给一张空表。

        空表会被读成「确实没有这个状态的记录」，而真相是参数拼错了。
        """
        r = runner.invoke(cli.app, ["applications", "--status", "nonsense"])

        assert r.exit_code != 0
        assert "blocked" in r.output, "报错信息里没列可选值"

    def test_funnel_has_no_percentage(self, tmp_db) -> None:
        """--funnel 不输出百分比。见 012 §7。

        今天是 14 尝试 / 0 提交，成功率算出来 0%，但那些记录全部停在登录门，
        提交逻辑一次都没执行过 —— 0% 的分母里装的全是「还没试到那一步」。
        blocked 是信息不全，不是不命中。
        """
        app_row(tmp_db, status="blocked", error="需要登录")
        app_row(tmp_db, status="blocked", error="未找到申请按钮，页面结构可能已变")

        r = runner.invoke(cli.app, ["applications", "--funnel"])

        assert "%" not in r.output, "漏斗里出现了比率"

    def test_funnel_groups_blocked_reasons(self, tmp_db) -> None:
        """被拦的原因要分开报，别把不同原因混成一个数。

        实测 14 条 blocked 里 10 条是登录门、4 条是找不到投递按钮，
        后者比同一岗位的登录记录更早（说明当时已修掉）。混成一个数会让
        「卡在登录」被高估，也看不出哪个原因还活着。
        """
        for _ in range(3):
            app_row(tmp_db, status="blocked", error="需要登录 腾讯 的招聘账号。")
        app_row(tmp_db, status="blocked", error="未找到申请按钮，页面结构可能已变")

        r = runner.invoke(cli.app, ["applications", "--funnel"])

        assert "要登录" in r.output
        assert "找不到投递按钮" in r.output
        reason_lines = [ln for ln in r.output.splitlines() if "要登录" in ln]
        assert reason_lines and "3" in reason_lines[0], reason_lines

    def test_funnel_separates_attempts_from_jobs(self, tmp_db) -> None:
        """尝试次数和岗位数要分开报。

        实测 14 次尝试只覆盖 7 个岗位（腾讯一个岗位重试了 7 次）。
        把 14 读成「14 个岗位试过了」是翻倍高估覆盖面。
        """
        for _ in range(3):
            app_row(tmp_db, ext_id="same-job", status="blocked", error="需要登录")

        r = runner.invoke(cli.app, ["applications", "--funnel"])

        assert "3 次尝试" in r.output
        assert "1 个岗位" in r.output

    def test_no_status_mutation_flags(self, tmp_db) -> None:
        """这条命令不许有改状态的开关。见 012 §5/§8。

        状态变更必须走 apply 的 prepare/execute 两阶段闸门（001 定的硬约束），
        从一个查看命令里改终态等于给那条闸门开后门。
        """
        # 只看 typer 真正注册的参数名，不看 --help 的正文：docstring 里就写着
        # 「`--mark-abandoned` 之类都没有」，扫全文会被自己的说明文字骗到。
        params = {
            opt
            for c in cli.app.registered_commands
            if c.callback.__name__ == "applications"
            for p in c.callback.__annotations__
            for opt in (f"--{p.replace('_', '-')}",)
        }
        for flag in ("--mark", "--retry", "--delete", "--submit", "--abandon"):
            assert not any(p.startswith(flag) for p in params), f"{flag} 不该存在"

    def test_filtered_title_says_it_is_filtered(self, tmp_db) -> None:
        """筛过的表要在标题里写明筛了什么。

        `--status blocked` 打出「投递记录 14 条」时，截图里看不出这是筛过的，
        会被当成全表读 —— 那就是凭空少了一批记录。
        """
        app_row(tmp_db, ext_id="a", status="blocked")
        app_row(tmp_db, ext_id="b", status="abandoned")

        r = runner.invoke(cli.app, ["applications", "--status", "blocked"])

        assert "投递记录 1 条" in r.output
        assert "筛选" in r.output and "status=blocked" in r.output


class TestSourceAdd:
    """`source-add` —— 多租户源的登记入口。

    这条命令补的是一个可达性缺口：飞书系四家的采集和代投全都写完了，但没有任何
    命令能把 `sources` 行写进去，而 `sync` 两条路径都要那行（`--source all` 从表里
    读，`--source feishu:nio` 要 `company`/`host`）。所以下面第一条用例钉的是
    「登记完 sync 真能看见它」，不是「INSERT 执行了」—— 后者对不可达这件事不构成证据。
    """

    def test_registering_makes_the_source_visible_to_sync(self, tmp_db) -> None:
        """登记完，`sync --source all` 的源列表里必须有它。

        这是整条 issue 的复现点：在这条命令之前，唯一的写入口是手敲 SQL。
        """
        r = runner.invoke(cli.app, [
            "source-add", "feishu:nio:campus", "--company", "蔚来",
            "--entry-url", "https://nio.jobs.feishu.cn",
        ])
        assert r.exit_code == 0, r.output

        # 直接复算 sync 的源列表判据（cli.sync 里那两行），而不是去 assert 库里
        # 有行 —— 有行但 sync 看不见的话，用户的处境和没登记完全一样。
        keys = {
            row["source_key"] for row in
            tmp_db.execute("SELECT source_key FROM sources WHERE enabled=1")
        }
        assert "feishu:nio:campus" in keys

    def test_tenant_comes_from_the_key_so_it_is_not_typed_twice(self, tmp_db) -> None:
        """不给 --tenant 时从键第二段取。键里那段就是判据，重复输一遍只多一次抄错机会。"""
        runner.invoke(cli.app, [
            "source-add", "feishu:xiaopeng:campus", "--company", "小鹏汽车",
            "--entry-url", "https://xiaopeng.jobs.feishu.cn",
        ])

        row = tmp_db.execute(
            "SELECT tenant FROM sources WHERE source_key='feishu:xiaopeng:campus'"
        ).fetchone()
        assert row["tenant"] == "xiaopeng"

    def test_mismatched_host_and_tenant_is_refused(self, tmp_db) -> None:
        """`entry_url` 的子域名和租户对不上就拒绝写库。

        这是本命令存在的第二个理由。这行配置写进去不会立刻报错，它会在下一轮
        sync 时拿着 nio 的租户去打小鹏的接口，把小鹏的岗位落在蔚来名下 ——
        表现是「采集跑了」而不是「采集错了」。所以必须是拒绝，不是警告。
        """
        r = runner.invoke(cli.app, [
            "source-add", "feishu:nio:campus", "--company", "蔚来",
            "--entry-url", "https://xiaopeng.jobs.feishu.cn",   # 抄错行
        ])

        assert r.exit_code == 1
        # 报错要指名是哪两项对不上，不然人不知道该改 entry_url 还是改 tenant。
        assert "nio" in r.output and "xiaopeng" in r.output
        assert tmp_db.execute(
            "SELECT COUNT(*) n FROM sources WHERE source_key='feishu:nio:campus'"
        ).fetchone()["n"] == 0, "拒绝了却把行写进去了"

    def test_explicit_tenant_must_match_key_on_custom_domain(self, tmp_db) -> None:
        """自定义域名取不出租户时，也不能让 --tenant 和键指向两家公司。"""
        r = runner.invoke(cli.app, [
            "source-add", "feishu:sensetime", "--company", "商汤",
            "--entry-url", "https://hr-jobs.sensetime.com", "--tenant", "nio",
        ])

        assert r.exit_code == 1
        output = r.output.replace("\n", "")  # Rich 会按终端宽度折行
        assert "sensetime" in output and "nio" in output
        assert tmp_db.execute(
            "SELECT COUNT(*) n FROM sources WHERE source_key='feishu:sensetime'"
        ).fetchone()["n"] == 0

    def test_self_built_source_is_turned_away(self, tmp_db) -> None:
        """自建源不用登记，直说，别让人以为漏了一步。"""
        r = runner.invoke(cli.app, [
            "source-add", "tencent_join", "--company", "腾讯",
            "--entry-url", "https://join.qq.com",
        ])

        assert r.exit_code == 1
        assert "自建" in r.output

    def test_unknown_system_lists_what_exists(self, tmp_db) -> None:
        """没采集器的系统要报出现有的有哪些 —— 光说「不支持」帮不上忙。"""
        r = runner.invoke(cli.app, [
            "source-add", "mokahr:youzan", "--company", "有赞",
            "--entry-url", "https://youzan.mokahr.com",
        ])

        assert r.exit_code == 1
        assert "feishu" in r.output

    def test_single_segment_key_asks_for_a_tenant(self, tmp_db) -> None:
        """`feishu` 一段的键取不到租户。这是老式注册留下的形状，得当场拦住。"""
        r = runner.invoke(cli.app, [
            "source-add", "feishu", "--company", "某公司",
            "--entry-url", "https://x.jobs.feishu.cn",
        ])

        assert r.exit_code == 1
        assert "租户" in r.output

    def test_re_registering_updates_instead_of_erroring(self, tmp_db) -> None:
        """同一个键再登记一次是改配置，不是报错（公司名写错了要能改回来）。"""
        base = ["source-add", "feishu:nio:campus",
                "--entry-url", "https://nio.jobs.feishu.cn"]
        runner.invoke(cli.app, base + ["--company", "蔚来汽车"])
        r = runner.invoke(cli.app, base + ["--company", "蔚来"])

        assert r.exit_code == 0, r.output
        row = tmp_db.execute(
            "SELECT company FROM sources WHERE source_key='feishu:nio:campus'"
        ).fetchone()
        assert row["company"] == "蔚来"


class TestDigestEmptyState:
    """digest 的空状态要分清「没新增」和「没同步过」。

    这两句话对人的下一步动作要求不同：前者是「等着」，后者是「去跑 sync」。
    原来两种情况都打印「没有新增。」，第二种把人留在原地。

    判据取 `runs` 表有没有行，不取 `jobs` —— sync 跑了但源站关站、一条都没抓到
    也是可能的，那种情况让人再跑一次 sync 是把人往错方向指。下面两条用例
    钉的就是这个区分：有 run 无 jobs 时**不该**提示 sync。
    """

    def test_never_synced_points_at_sync(self, tmp_path, monkeypatch) -> None:
        """runs 表空 —— 说清楚是没采集过，并给出下一步。"""
        path = tmp_path / "fresh.db"
        monkeypatch.setattr(db, "DB_PATH", path)
        monkeypatch.setattr(match, "PROFILE_PATH", tmp_path / "missing-profile.yaml")
        conn = db.connect(path)
        db.init(conn)          # 建表，但不 start_run
        conn.commit()

        r = runner.invoke(cli.app, ["digest"])

        assert r.exit_code == 0, r.output
        assert "sync" in r.output
        conn.close()

    def test_synced_but_empty_does_not_say_sync(
        self, tmp_db, tmp_path, monkeypatch
    ) -> None:
        """有 run、没岗位 —— 这是「真的没新增」，不能让人再跑一次 sync。

        tmp_db 建了 run 但没插 jobs，正好是源站关站那种形状。
        """
        monkeypatch.setattr(match, "PROFILE_PATH", tmp_path / "missing-profile.yaml")
        r = runner.invoke(cli.app, ["digest"])

        assert r.exit_code == 0, r.output
        assert "没有新增" in r.output
        assert "sync" not in r.output


class TestSyncSurfacesDesyncCount:
    """`fingerprint_desync` 必须出现在 sync 的输出里。见方案 016 §5 约束 3。

    为什么单独测 CLI 而不只测 stats：方案 016 的第 2 和第 3 个方向的差别**全在
    这一条上** —— 判空但不报出来（方向 2），「diff 为空所以吞掉」和「压根没变化」
    在输出里就长得一样了，而这个 bug 第三次复发正是因为那个状态一直没人看见。
    只加 stats 键不打印，等于选了方向 2 还以为选了方向 3。

    这里 monkeypatch `ingest.sync` 而不是走真适配器：要验的是「CLI 拿到这个数会不会
    打」，不是「sync 算得对不对」（那 6 条在 test_ingest.py 里）。走真路径得连网。
    """

    def _fake_stats(self, desync: int) -> dict:
        return {
            "source": "tencent_join", "bootstrap": False, "fetched": 10,
            "opened": 0, "updated": desync, "closed": 0, "guard_tripped": False,
            "families_first_seen": [], "family_unknown": 0,
            "fingerprint_desync": desync,
        }

    def test_nonzero_desync_is_printed(self, tmp_db, monkeypatch) -> None:
        monkeypatch.setattr(
            cli.ingest, "sync", lambda *a, **k: self._fake_stats(8594)
        )
        r = runner.invoke(cli.app, ["sync", "--source", "tencent_join"])

        assert r.exit_code == 0, r.output
        assert "8594" in r.output
        assert "指纹与列不同步" in r.output

    def test_zero_desync_is_not_printed(self, tmp_db, monkeypatch) -> None:
        """正常情况下恒为 0，打出来就是每轮一行噪声。"""
        monkeypatch.setattr(
            cli.ingest, "sync", lambda *a, **k: self._fake_stats(0)
        )
        r = runner.invoke(cli.app, ["sync", "--source", "tencent_join"])

        assert r.exit_code == 0, r.output
        assert "指纹与列不同步" not in r.output


class TestHealthSampleBounds:
    @pytest.mark.parametrize("sample", ["-1", "0", "21"])
    def test_invalid_sample_is_rejected_before_health_runs(self, sample) -> None:
        r = runner.invoke(cli.app, ["health", "--sample", sample])

        assert r.exit_code == 2
        assert "20" in r.output


class TestJobIdentity:
    def _seed_duplicate(self, conn) -> None:
        db.register_source(
            conn, "feishu:nio", "蔚来", "feishu",
            "https://nio.jobs.feishu.cn", tenant="nio",
        )
        for source_key, company, system in (
            ("tencent_join", "腾讯", "tencent_join"),
            ("feishu:nio", "蔚来", "feishu"),
        ):
            conn.execute(
                """INSERT INTO jobs(source_key, external_id, company, title,
                       apply_url, apply_system, fingerprint,
                       first_seen_at, last_seen_at)
                   VALUES(?, 'DUP', ?, '产品运营', 'https://x', ?, 'fp', ?, ?)""",
                (source_key, company, system, db.now(), db.now()),
            )
        conn.commit()

    @pytest.mark.parametrize("command", ["apply", "checkup"])
    def test_ambiguous_job_requires_source(
        self, command, tmp_db, monkeypatch
    ) -> None:
        self._seed_duplicate(tmp_db)

        def must_not_route(*args, **kwargs):
            raise AssertionError("岗位还没消歧，不得进入投递路由")

        monkeypatch.setattr(cli.routing, "get_submitter", must_not_route)
        result = runner.invoke(cli.app, [command, "DUP"])
        output = result.output.replace("\n", "")

        assert result.exit_code == 1
        assert "feishu:nio" in output and "tencent_join" in output
        assert "--source" in output
        assert not isinstance(result.exception, AssertionError)


class TestMatchedJobsUsability:
    def test_missing_profile_exits_with_a_fix_instead_of_a_traceback(
        self, tmp_db, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(match, "PROFILE_PATH", tmp_path / "missing-profile.yaml")

        result = runner.invoke(cli.app, ["jobs", "--matched"])

        assert result.exit_code == 1
        assert "画像" in result.output
        assert "profile.yaml.example" in result.output
        assert "Traceback" not in result.output

    def test_jobs_show_copyable_source_and_external_id(
        self, tmp_db, monkeypatch
    ) -> None:
        seed(tmp_db, "COPY-ME-JOB", 100, "2026")
        monkeypatch.setattr(cli.console, "width", 240)

        result = runner.invoke(cli.app, ["jobs"])

        assert result.exit_code == 0, result.output
        assert "tencent_join" in result.output
        assert "COPY-ME-JOB" in result.output
