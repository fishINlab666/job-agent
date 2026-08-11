"""限投额度的计数与闸门。

这一层守的损失和别处不一样：投递不可逆，超出对方公司的限投上限之后，多投的那
几次的后果由对方系统决定，撤不回来。两阶段闸门（prepare/execute）保的是「这一
次提交是你确认过的」，它保不了「这是这家公司的第几次」—— 用户挨个确认 5 个腾讯
岗位，每一步看起来都正常。这个文件测的就是那另一半。

计数口径的两条判据单独测，因为它们都是「猜错了就投穿」的方向：
  - 按公司算，不按 source_key 算（实测蔚来在 sources 里有两行）
  - failed 算占用（它全都写在 execute() 点击之后，点击超时不等于没点上）
"""
from __future__ import annotations

import itertools

import pytest
from typer.testing import CliRunner

from jobagent import cli, db

runner = CliRunner()

# external_id 得真的不重样（jobs 上有 UNIQUE(source_key, external_id)）。
# 第一版用的 id(object())：临时对象当场被回收，CPython 会把地址复用给下一个，
# 于是同一个测试里插第二条就撞唯一约束。
_ids = itertools.count(1)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.connect(path)
    db.init(conn)
    yield conn
    conn.close()


def add_app(conn, company: str, status: str, source_key: str = "s1") -> None:
    """造一条投递记录。job_id 走外键，所以得先有 jobs 行。"""
    run_exists = conn.execute(
        "SELECT 1 FROM sources WHERE source_key=?", (source_key,)
    ).fetchone()
    if not run_exists:
        db.register_source(conn, source_key, company, "feishu",
                           f"https://{source_key}.feishu.cn/hire")
    cur = conn.execute(
        """INSERT INTO jobs(source_key, external_id, company, title,
               fingerprint, first_seen_at, last_seen_at)
           VALUES(?,?,?,'岗位','fp',?,?)""",
        (source_key, f"e{next(_ids)}", company, db.now(), db.now()),
    )
    conn.execute(
        """INSERT INTO applications(job_id, source_key, external_id, company,
               status, created_at)
           VALUES(?,?,?,?,?,?)""",
        (cur.lastrowid, source_key, "e", company, status, db.now()),
    )
    conn.commit()


class TestConsumingStatuses:
    """哪些终态算名额花掉了。"""

    def test_submitted_and_duplicate_and_failed_consume(self, tmp_db):
        for st in ("submitted", "duplicate", "failed"):
            add_app(tmp_db, "A公司", st)
        used, _ = db.quota_state(tmp_db, "A公司")
        assert used == 3

    def test_failed_counts_because_click_already_happened(self, tmp_db):
        """failed 必须算占用。

        execute() 里所有 failed 分支都在 `click()` 之后（submitters/
        tencent_join.py:211 起），包括 PlaywrightTimeout —— 点击超时不代表没点上，
        可能只是页面没稳。少算一次的代价是投穿不可逆上限，多算一次的代价是用户
        去源站看一眼。两边不对等。
        """
        add_app(tmp_db, "A公司", "failed")
        used, _ = db.quota_state(tmp_db, "A公司")
        assert used == 1

    def test_pre_click_statuses_do_not_consume(self, tmp_db):
        """没点到提交的那些不算：blocked/prefilled/abandoned 停在确认环节，
        closed 是源站以岗位已关为由拒收、没落单。"""
        for st in ("prefilled", "blocked", "abandoned", "closed"):
            add_app(tmp_db, "A公司", st)
        used, _ = db.quota_state(tmp_db, "A公司")
        assert used == 0


class TestCountedByCompany:
    def test_two_source_keys_of_one_company_share_the_quota(self, tmp_db):
        """一家公司多个源，用量要合起来算。

        实测数据里蔚来就是这样：sources 有 feishu:nio 和 feishu:nio:campus 两行。
        按 source_key 数的话每个源各算一份，两份都不到上限，于是投穿。
        """
        add_app(tmp_db, "蔚来", "submitted", source_key="feishu:nio")
        add_app(tmp_db, "蔚来", "submitted", source_key="feishu:nio:campus")
        used, _ = db.quota_state(tmp_db, "蔚来")
        assert used == 2

    def test_other_companies_do_not_leak_in(self, tmp_db):
        add_app(tmp_db, "A公司", "submitted", source_key="a1")
        add_app(tmp_db, "B公司", "submitted", source_key="b1")
        used, _ = db.quota_state(tmp_db, "A公司")
        assert used == 1


class TestLimitLookup:
    def test_no_row_means_unlimited(self, tmp_db):
        used, limit = db.quota_state(tmp_db, "没登记过的公司")
        assert (used, limit) == (0, None)

    def test_null_limit_means_unlimited(self, tmp_db):
        db.register_source(tmp_db, "s1", "A公司", "feishu", "https://x.feishu.cn/hire")
        tmp_db.commit()
        _, limit = db.quota_state(tmp_db, "A公司")
        assert limit is None

    def test_takes_the_smallest_limit_among_a_companys_rows(self, tmp_db):
        """同一家多行且上限填得不一样时取最小的。拦早了能核一下再继续，
        拦晚了名额已经没了。"""
        db.register_source(tmp_db, "n1", "蔚来", "feishu",
                           "https://nio.feishu.cn/hire", "", None, 5)
        db.register_source(tmp_db, "n2", "蔚来", "feishu",
                           "https://nio.feishu.cn/hire", "", None, 2)
        tmp_db.commit()
        _, limit = db.quota_state(tmp_db, "蔚来")
        assert limit == 2

    def test_sync_reregistration_does_not_wipe_the_limit(self, tmp_db):
        """sync 每轮都重新登记一次（ingest.py:284）且不传 apply_limit。
        直接覆盖的话，配好上限后第一次 sync 就把闸门静默拆了。
        """
        db.register_source(tmp_db, "s1", "A公司", "feishu",
                           "https://x.feishu.cn/hire", "", None, 3)
        db.register_source(tmp_db, "s1", "A公司", "feishu",
                           "https://x.feishu.cn/hire")  # sync 的调用形状
        tmp_db.commit()
        _, limit = db.quota_state(tmp_db, "A公司")
        assert limit == 3


@pytest.fixture
def no_browser(monkeypatch):
    """把 prepare() 换成一个「被调到就炸」的桩。

    这不是为了跑得快，是为了让回归**失败**而不是**挂住**。第一版没有这个桩：
    把计数判据改坏之后闸门不触发，`apply` 就真的去启动 playwright 打 join.qq.com，
    测试卡在那儿不返回。挂住的测试下一个人碰到就会把它关掉，等于这条判据没人守。

    桩打在 `prepare` 而不是 `get_submitter`：浏览器是在 prepare 里起的
    （`__init__` 只存参数），而 `get_submitter` 在闸门之前就会被正常调用一次，
    打在那儿的话正常路径也会炸。
    """
    from jobagent.submitters.tencent_join import TencentJoinSubmitter

    def boom(*a, **kw):
        raise AssertionError("闸门没拦住：走到 prepare() 了，这一步会开真浏览器")
    monkeypatch.setattr(TencentJoinSubmitter, "prepare", boom)


class TestApplyGate:
    def _seed_job(self, conn, ext_id: str, company: str = "A公司") -> None:
        db.register_source(conn, "tencent_join", company, "tencent_join",
                           "https://join.qq.com/post.html", "", None, 2)
        conn.execute(
            """INSERT INTO jobs(source_key, external_id, company, title,
                   apply_url, apply_system, fingerprint, first_seen_at, last_seen_at)
               VALUES('tencent_join',?,?,'产品运营','https://join.qq.com/x',
                      'tencent_join','fp',?,?)""",
            (ext_id, company, db.now(), db.now()),
        )
        conn.commit()

    def test_refuses_when_limit_reached(self, tmp_db, tmp_path, no_browser):
        self._seed_job(tmp_db, "J1")
        for _ in range(2):
            add_app(tmp_db, "A公司", "submitted", source_key="tencent_join")
        prof = tmp_path / "p.yaml"
        prof.write_text(
            "name: 张三\nphone: '13800000000'\nemail: a@b.com\n", encoding="utf-8"
        )
        r = runner.invoke(cli.app, ["apply", "J1", "--profile-path", str(prof)])
        assert r.exit_code == 1
        assert "投递上限 2" in r.output

    def test_refusal_leaves_a_trace(self, tmp_db, tmp_path, no_browser):
        """拦下来也要留痕：不然用户只看到一句报错，查不到发生过什么。"""
        self._seed_job(tmp_db, "J1")
        for _ in range(2):
            add_app(tmp_db, "A公司", "submitted", source_key="tencent_join")
        prof = tmp_path / "p.yaml"
        prof.write_text(
            "name: 张三\nphone: '13800000000'\nemail: a@b.com\n", encoding="utf-8"
        )
        runner.invoke(cli.app, ["apply", "J1", "--profile-path", str(prof)])
        row = tmp_db.execute(
            "SELECT status, error FROM applications WHERE external_id='J1'"
        ).fetchone()
        assert row is not None and row["status"] == "blocked"
        assert "上限" in row["error"]

    def test_lets_you_through_below_the_limit(self, tmp_db, tmp_path, no_browser):
        """另一个方向：没到上限就得放行。

        只测「到了拦得住」的话，把闸门写成无条件拦截也能过。放行的证据取那句
        `额度 1/2` —— 它只在闸门算完且没触发时打出来。桩会在下一步炸，
        那正说明放行了。
        """
        self._seed_job(tmp_db, "J1")
        add_app(tmp_db, "A公司", "submitted", source_key="tencent_join")
        prof = tmp_path / "p.yaml"
        prof.write_text(
            "name: 张三\nphone: '13800000000'\nemail: a@b.com\n", encoding="utf-8"
        )
        r = runner.invoke(cli.app, ["apply", "J1", "--profile-path", str(prof)])
        assert "额度 1/2" in r.output
        assert "投递上限" not in r.output
