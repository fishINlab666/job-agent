"""增量与守卫逻辑测试。

重点测两件事，都是「静默错误」类型的故障 —— 不测就发现不了：
  1. 上游半残返回时，不能把没消失的岗位判成关闭。
  2. 首轮抓取不能产生几百条单条事件。
"""
from __future__ import annotations

import json

import pytest

from jobagent import db, ingest
from jobagent.adapters.base import RawJob


class FakeAdapter:
    """可控的假适配器，用来构造 diff 场景。"""

    source_key = "fake_src"
    company = "测试公司"
    system = "self_built"
    entry_url = "https://example.test"

    def __init__(self, jobs: list[RawJob]) -> None:
        self._jobs = jobs

    def fetch(self) -> list[RawJob]:
        return self._jobs


def make_job(
    ext_id: str,
    title: str = "产品运营",
    family: str = "operations",
    cities: list[str] | None = None,
) -> RawJob:
    # cities 用 None 当哨兵而不是直接默认 ["北京"]：要能区分「没传」和
    # 「传了空列表」—— 城市变空是一个正经用例（TestCitiesDiff 里那条）。
    return RawJob(
        external_id=ext_id,
        title=title,
        raw_json={"id": ext_id, "title": title},
        job_family=family,
        cities=["北京"] if cities is None else cities,
        recruit_type="campus",
        grad_year="27",
        apply_url=f"https://example.test/{ext_id}",
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init(c)
    yield c
    c.close()


def events_of(conn, kind: str) -> list:
    return conn.execute("SELECT * FROM events WHERE kind=?", (kind,)).fetchall()


class TestBootstrap:
    def test_first_run_emits_summary_not_per_job(self, conn) -> None:
        jobs = [make_job(str(i)) for i in range(50)]
        st = ingest.sync(conn, FakeAdapter(jobs))

        assert st["bootstrap"] is True
        assert st["opened"] == 50
        # 关键：首轮不产生 50 条 job_opened
        assert len(events_of(conn, "job_opened")) == 0
        assert len(events_of(conn, "source_bootstrapped")) == 1

    def test_second_run_emits_per_job_events(self, conn) -> None:
        ingest.sync(conn, FakeAdapter([make_job("1")]))
        st = ingest.sync(conn, FakeAdapter([make_job("1"), make_job("2")]))

        assert st["bootstrap"] is False
        assert st["opened"] == 1
        assert len(events_of(conn, "job_opened")) == 1


class TestCloseGuard:
    def test_guard_blocks_mass_close(self, conn) -> None:
        """上游只返回 2/10，不能把 8 个岗位判成关闭。"""
        ingest.sync(conn, FakeAdapter([make_job(str(i)) for i in range(10)]))
        st = ingest.sync(conn, FakeAdapter([make_job("0"), make_job("1")]))

        assert st["guard_tripped"] is True
        assert st["closed"] == 0
        assert len(events_of(conn, "job_closed")) == 0

        run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["status"] == "partial"
        assert "关闭守卫" in run["error"]

        still_open = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE closed_at IS NULL"
        ).fetchone()["n"]
        assert still_open == 10

    def test_normal_close_still_works(self, conn) -> None:
        """消失比例在阈值内，正常关闭。"""
        ingest.sync(conn, FakeAdapter([make_job(str(i)) for i in range(10)]))
        st = ingest.sync(conn, FakeAdapter([make_job(str(i)) for i in range(8)]))

        assert st["guard_tripped"] is False
        assert st["closed"] == 2
        assert len(events_of(conn, "job_closed")) == 2

    def test_small_source_can_still_close(self, conn) -> None:
        """小源不该被比例守卫锁死。

        某家 AI 公司只有 3 个校招岗，关掉 2 个是正常的季节性收口，
        比例 67% 却会触发纯比例守卫，导致岗位永远关不掉、
        用户一直看到早已关闭的岗位。
        """
        three = [make_job(str(i)) for i in range(3)]
        ingest.sync(conn, FakeAdapter(three))
        st = ingest.sync(conn, FakeAdapter(three[:1]))

        assert st["guard_tripped"] is False
        assert st["closed"] == 2

    def test_empty_fetch_raises_not_closes_everything(self, conn) -> None:
        """适配器返回空必须抛异常，不能被当成「全部关闭」。

        这是腾讯那个形状的适配器：**没有** empty_is_authoritative 属性。
        getattr 默认 False 那条路径，行为必须跟加飞书之前一模一样。
        """
        assert not hasattr(FakeAdapter, "empty_is_authoritative")
        ingest.sync(conn, FakeAdapter([make_job("1")]))
        with pytest.raises(RuntimeError):
            ingest.sync(conn, FakeAdapter([]))

        # 岗位仍在，run 记为 failed
        assert conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE closed_at IS NULL"
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT status FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"] == "failed"

    def test_empty_fetch_ok_when_adapter_says_so(self, conn) -> None:
        """适配器声明「空是可信答案」时，sync 不抛，run 记 ok。

        实测背景：luckin / horizon 是活的飞书租户，接口回 code=0 + count=0，
        它们当下就是没在招。这是事实，不是故障。

        这一条和上一条是一对：**只改适配器让 fetch() 不抛、忘了 ingest 这侧**，
        适配器单测会全绿（它测的确实是 fetch），而 sync 照样 failed，
        表现是「明明返回了空列表还是失败」。所以必须从这一侧再钉一次。
        """

        class QuietAdapter(FakeAdapter):
            empty_is_authoritative = True

        st = ingest.sync(conn, QuietAdapter([]))
        assert st["fetched"] == 0
        assert st["opened"] == 0
        assert conn.execute(
            "SELECT status FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"] == "ok"

    def test_authoritative_empty_does_not_mass_close(self, conn) -> None:
        """就算标记为真，也不许拿空结果去关掉一整批已有岗位。

        标记回答的是「空可不可信」，不是「可以放心关闭」。真出现这种情况
        （昨天 2265 条今天 count=0），关闭守卫必须接住 —— 消失比例 100%。
        """

        class QuietAdapter(FakeAdapter):
            empty_is_authoritative = True

        ingest.sync(conn, FakeAdapter([make_job(str(i)) for i in range(10)]))
        st = ingest.sync(conn, QuietAdapter([]))
        assert st["guard_tripped"] is True
        assert st["closed"] == 0
        assert conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE closed_at IS NULL"
        ).fetchone()["n"] == 10
        assert conn.execute(
            "SELECT status FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"] == "partial"


class TestFamilyUnknownStat:
    """判不出族的条数要如实报，分母是这一轮抓到的条数。"""

    def test_counts_none_families(self, conn) -> None:
        jobs = [make_job("1"), make_job("2", family=None), make_job("3", family=None)]
        st = ingest.sync(conn, FakeAdapter(jobs))
        assert st["fetched"] == 3
        assert st["family_unknown"] == 2

    def test_zero_when_all_decided(self, conn) -> None:
        st = ingest.sync(conn, FakeAdapter([make_job("1"), make_job("2")]))
        assert st["family_unknown"] == 0

    def test_counts_all_fetched_not_just_new(self, conn) -> None:
        """第二轮同样的岗位，family_unknown 不许掉成 0。

        它数的是「这批岗位里有多少判不出」，不是「这轮新增里有多少」——
        判不出的岗位在库里照样按族筛不到，用户每轮都该看到这个数。
        """
        jobs = [make_job("1", family=None), make_job("2")]
        ingest.sync(conn, FakeAdapter(jobs))
        st = ingest.sync(conn, FakeAdapter(jobs))
        assert st["opened"] == 0
        assert st["family_unknown"] == 1


class TestFamilyFirstSeen:
    def test_new_family_emits_event(self, conn) -> None:
        """产品族从 0 变非 0，这是最有价值的信号。"""
        ingest.sync(conn, FakeAdapter([make_job("1", "后台开发", "tech")]))
        st = ingest.sync(conn, FakeAdapter([
            make_job("1", "后台开发", "tech"),
            make_job("2", "产品运营", "operations"),
        ]))

        assert "operations/campus" in st["families_first_seen"]
        evs = events_of(conn, "family_first_seen")
        assert len(evs) == 1

    def test_existing_family_no_event(self, conn) -> None:
        ingest.sync(conn, FakeAdapter([make_job("1", "产品运营", "operations")]))
        st = ingest.sync(conn, FakeAdapter([
            make_job("1", "产品运营", "operations"),
            make_job("2", "内容运营", "operations"),
        ]))

        assert st["families_first_seen"] == []


class TestReopen:
    def test_closed_job_reappearing_emits_reopen(self, conn) -> None:
        # 用 6 个岗位：关掉 1 个是 17%，既低于比例阈值也低于最小数量，
        # 确保守卫不介入，测的是 reopen 本身
        full = [make_job(str(i)) for i in range(6)]
        ingest.sync(conn, FakeAdapter(full))
        ingest.sync(conn, FakeAdapter(full[:5]))       # 5 号关闭
        st = ingest.sync(conn, FakeAdapter(full))      # 5 号回来

        assert st["updated"] == 1
        assert len(events_of(conn, "job_reopened")) == 1
        assert conn.execute(
            "SELECT closed_at FROM jobs WHERE external_id='5'"
        ).fetchone()["closed_at"] is None


class TestCitiesDiff:
    """城市变更要进 diff，且口径必须和指纹一致。

    原来 cities 在指纹里（`ingest._fp`）却不在 diff 的字段清单里，于是
    「只有城市变了」会触发 job_updated 但 diff 是空的 —— 用户收到
    「岗位 XXX 有更新」，后面什么都没有。真库里这样的事件有 16 条，
    回查源站快照，变的字段全是 workCities。见方案 006 问题 1。
    """

    def _diff_of(self, conn, before: list[str], after: list[str]) -> dict | None:
        """跑两轮 sync，返回第二轮 job_updated 的 diff（没有事件则 None）。"""
        ingest.sync(conn, FakeAdapter([make_job("1", cities=before)]))
        ingest.sync(conn, FakeAdapter([make_job("1", cities=after)]))
        row = conn.execute(
            "SELECT payload FROM events WHERE kind='job_updated'"
        ).fetchone()
        return json.loads(row["payload"])["diff"] if row else None

    def test_cities_change_enters_diff(self, conn) -> None:
        """城市从三地缩到一地，diff 里要能看出来。

        修之前这条红：diff 是 {}，事件照发，用户看不到变了什么。
        """
        diff = self._diff_of(conn, ["北京", "上海", "深圳"], ["深圳"])

        assert diff is not None, "城市变了却没发 job_updated"
        assert "cities" in diff, f"城市变了但 diff 里没有 cities：{diff}"
        assert diff["cities"] == {"from": ["上海", "北京", "深圳"], "to": ["深圳"]}

    def test_diff_carries_lists_not_json_strings(self, conn) -> None:
        """diff 里的 cities 是 list，不是 JSON 字符串。

        钉的是渲染端的形状依赖：`cli._fmt_cities` 按 list 处理。
        谁图省事把 `prev["cities"]` 原样塞进 diff（那是字符串），这条红。
        """
        diff = self._diff_of(conn, ["北京"], ["深圳"])

        assert isinstance(diff["cities"]["from"], list)
        assert isinstance(diff["cities"]["to"], list)

    def test_unchanged_cities_stay_out_of_diff(self, conn) -> None:
        """城市没变就不该出现在 diff 里（反向用例）。

        忘了反序列化时这条红：库里是 `'["北京"]'`、内存里是 `["北京"]`，
        直接比永远不等，于是**每个**岗位都被判成城市变了。
        """
        diff = self._diff_of(conn, ["北京"], ["北京"])

        # 城市和其它字段都没变 → 指纹不变 → 压根不该有事件
        assert diff is None, f"什么都没变却发了 job_updated：{diff}"

    def test_city_reorder_alone_emits_no_event(self, conn) -> None:
        """只换城市顺序，不算变更 —— 指纹算的是排序后的值。

        这条钉的是不变量，但它**抓不住** diff 里漏掉 sorted 的错：
        顺序变了指纹不变，事件根本不发，diff 那几行走不到。
        真正抓 sorted 的是下一条。
        """
        assert self._diff_of(conn, ["北京", "深圳"], ["深圳", "北京"]) is None

    def test_city_reorder_is_not_a_change_next_to_a_real_change(self, conn) -> None:
        """标题变了、城市只换了顺序：diff 里要有 title，不许有 cities。

        **这条是 sorted 的唯一守卫。** 必须搭一个别的字段变更把指纹顶开，
        才能让代码走到建 diff 那几行；否则顺序变化根本进不了那段逻辑。
        去掉 `_cities` 里的 sorted 之后，这条红（会多出一个假的 cities 变更），
        而上面那条仍然绿。
        """
        ingest.sync(conn, FakeAdapter([make_job("1", cities=["北京", "深圳"])]))
        ingest.sync(
            conn,
            FakeAdapter([make_job("1", title="高级产品运营", cities=["深圳", "北京"])]),
        )
        diff = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE kind='job_updated'"
            ).fetchone()["payload"]
        )["diff"]

        assert "title" in diff, "标题变了却没进 diff，用例本身坏了"
        assert "cities" not in diff, (
            f"只换了城市顺序却报成变更 —— 指纹说没变、diff 说变了，自相矛盾：{diff}"
        )

    def test_cities_going_empty_is_a_change(self, conn) -> None:
        """城市变空是变更，不是「没变」。

        `[]` 的含义是「源站这次什么都没给」，和 `["不限"]`（源站明说哪都行）
        是两件事。空列表不许被当成「跳过比较」。
        """
        diff = self._diff_of(conn, ["北京"], [])

        assert diff is not None and "cities" in diff
        assert diff["cities"] == {"from": ["北京"], "to": []}


class TestDryRunWritesNothing:
    """`--dry-run` 必须是只读的。

    实测踩到的：它不是。`db.register_source` / `db.start_run` 原来无条件
    `conn.commit()`，而 dry-run 靠结尾那句 `conn.rollback()` 抹掉整轮 ——
    commit 过的东西回滚不回来。表现是 `sync --dry-run` 往真库里留下
    sources 一行 + runs 一行永远 `running` 的记录。

    后一行有毒：`cli status` 取 `ORDER BY id DESC LIMIT 1`，于是一个上一轮
    真实成功、有 795 条开放岗位的源，状态栏显示 `running` / 抓取 0。
    不报错，只是显示的东西是错的 —— 「上次同步失败了吗」会被直接带偏。

    这个 bug 能活下来的全部原因是：`dry_run` 在测试里一次都没被跑过。
    """

    TABLES = ("runs", "sources", "jobs", "snapshots", "events")

    def _counts(self, conn) -> dict:
        return {
            t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in self.TABLES
        }

    def test_dry_run_leaves_every_table_untouched(self, conn) -> None:
        """五张表逐个数。只数 jobs 会漏掉真正泄漏的那两张。"""
        ingest.sync(conn, FakeAdapter([make_job("1")]))      # 先有个基线状态
        before = self._counts(conn)
        ingest.sync(conn, FakeAdapter([make_job(str(i)) for i in range(6)]), dry_run=True)
        assert self._counts(conn) == before

    def test_dry_run_does_not_leak_a_new_source(self, conn) -> None:
        """没见过的源跑 dry-run，不许在 sources / runs 里留痕。

        单独钉一条是因为这就是实测现场：探针源跑完一次 dry-run，
        `sources` 和 `runs` 里各多出一行，`jobs`/`snapshots` 却干净 ——
        「大部分没写」比「全写了」更难发现。
        """
        ingest.sync(conn, FakeAdapter([make_job("1")]), dry_run=True)
        assert conn.execute(
            "SELECT COUNT(*) n FROM sources WHERE source_key=?", (FakeAdapter.source_key,)
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE source_key=?", (FakeAdapter.source_key,)
        ).fetchone()["n"] == 0

    def test_dry_run_leaves_no_running_row(self, conn) -> None:
        """具体钉住让 status 说假话的那一行：不许留下 running。"""
        ingest.sync(conn, FakeAdapter([make_job("1")]))
        ingest.sync(conn, FakeAdapter([make_job("1"), make_job("2")]), dry_run=True)
        assert conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE status='running'"
        ).fetchone()["n"] == 0
        # 真跑那轮的结果还在，dry-run 没把它顶掉
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert (last["status"], last["fetched"]) == ("ok", 1)

    def test_failed_dry_run_leaves_nothing_either(self, conn) -> None:
        """失败路径同样不许留行。

        这条最容易漏：`finish_run` 那句 commit 会把 `start_run` 那条还没落盘的
        INSERT 一起提交，于是「只算不写」反倒写进去一条 failed。
        """
        before = self._counts(conn)
        with pytest.raises(RuntimeError):
            ingest.sync(conn, FakeAdapter([]), dry_run=True)
        assert self._counts(conn) == before

    def test_real_run_still_writes(self, conn) -> None:
        """反向对照。少了它，上面几条可以靠「让 sync 什么都不做」全绿。"""
        before = self._counts(conn)
        ingest.sync(conn, FakeAdapter([make_job("1")]))
        after = self._counts(conn)
        for t in ("runs", "sources", "jobs", "snapshots"):
            assert after[t] > before[t], f"{t} 没写进去，dry-run 那几条是假绿"

    def test_dry_run_still_computes_stats(self, conn) -> None:
        """不写库 ≠ 不干活。dry-run 的意义就是把这些数算给人看。"""
        jobs = [make_job("1"), make_job("2", family=None), make_job("3")]
        st = ingest.sync(conn, FakeAdapter(jobs), dry_run=True)
        assert st["fetched"] == 3
        assert st["opened"] == 3
        assert st["family_unknown"] == 1
        assert st["bootstrap"] is True

    def test_repeated_dry_runs_stay_bootstrap(self, conn) -> None:
        """连跑两次 dry-run，第二次仍是首轮 —— 证明第一次真没落库。"""
        jobs = [make_job("1")]
        assert ingest.sync(conn, FakeAdapter(jobs), dry_run=True)["bootstrap"] is True
        assert ingest.sync(conn, FakeAdapter(jobs), dry_run=True)["bootstrap"] is True


class TestSnapshots:
    def test_every_run_stores_raw(self, conn) -> None:
        """快照是漏报排查的唯一依据，每轮都必须落。"""
        ingest.sync(conn, FakeAdapter([make_job("1"), make_job("2")]))
        ingest.sync(conn, FakeAdapter([make_job("1"), make_job("2")]))

        assert conn.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"] == 4
