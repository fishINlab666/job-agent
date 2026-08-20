"""`jobagent/health.py` 钉的是「链接形状」和「正文判据」两件事。

**为什么这些测试不联网。** 判据函数只吃一个正文字符串，真开浏览器那部分靠
`docs/plans/013-apply_url健康巡检.md §9` 的命令人工验。分开的理由：判据要能进 CI
且不会因为源站抖动假红；反过来，网络那层再稳也钉不住「判定顺序」这种逻辑。

**形状检查在 `--real-data` 下读真库。** 这是有意的 —— 它要回答的问题就是
「**库里现在**的链接是不是还长着对的样子」，喂假数据的话它永远绿。
默认测试和 CI 不读取本机数据；人工巡检时显式加开关。2026-08-10 那次事故
（8594 条飞书链接少了 `/detail`）就是这条要抓的东西，而当时库里每个源的
形状**数**依然是 1。
"""
from __future__ import annotations

import sqlite3

import pytest

from jobagent import db, health


# ---------------------------------------------------------------- 形状：真库

@pytest.fixture(scope="module")
def real_conn(request):
    if not request.config.getoption("--real-data"):
        pytest.skip("真实数据库检查默认关闭；人工巡检时加 --real-data")
    conn = db.connect_readonly()
    yield conn
    conn.close()


def test_expected_shape_is_not_empty() -> None:
    """前置：登记表非空。

    没有这条，下面的形状检查在表被清空时会**一条问题都报不出来**、
    静默全绿 —— 那正是「守卫悄悄消失」。
    """
    assert health.EXPECTED_SHAPE, "EXPECTED_SHAPE 空了，形状检查已形同不存在"


def test_real_db_shapes_match_the_pinned_literals(real_conn) -> None:
    """库里每个源的 `apply_url` 形状必须等于登记的字面量。

    这条是整个巡检的核心守卫，也是唯一在提交时就能红的。
    2026-08-10 那次事故它会红（`/detail` 被删）。
    """
    problems = health.check_shapes(real_conn)
    assert not problems, "URL 形状对不上：\n" + "\n".join(problems)


def test_real_db_has_rows_to_check(real_conn) -> None:
    """前置：真库里真的有开放岗位。

    库空了的话上一条会拿空字典比、返回空列表、绿。
    """
    shapes = health.shapes_by_source(real_conn)
    assert shapes, "真库里没有开放岗位，形状检查静默通过了"


def test_unregistered_source_is_a_failure() -> None:
    """库里出现没登记的源 → 报问题，不是跳过。

    源名是 f-string 拼出来的（`feishu:<tenant>:<portal>`），数不出全集，
    所以只能白名单。漏登记的方向必须是「报错」而不是「不检查」。
    """
    conn = _fake_db([("feishu:newco:campus", "1" * 19,
                      "https://newco.jobs.feishu.cn/campus/position/" + "1" * 19 + "/detail")])
    problems = health.check_shapes(conn)
    conn.close()
    assert len(problems) == 1
    assert "没登记" in problems[0]


def test_wrong_shape_is_reported_with_the_count() -> None:
    """形状不对时要说清有几条、实际长什么样 —— 否则修的人不知道是全库还是零星。"""
    bad = "https://nio.jobs.feishu.cn/campus/position/{}"  # 少 /detail，复现事故
    conn = _fake_db([("feishu:nio:campus", str(i) * 19, bad.format(str(i) * 19))
                     for i in range(1, 4)])
    problems = health.check_shapes(conn)
    conn.close()
    assert len(problems) == 1
    assert "3 条" in problems[0]
    assert "/detail" in problems[0], "得把登记的正确形状也打出来"


def test_empty_apply_url_raises_instead_of_being_skipped() -> None:
    """`apply_url` 空值**抛**，不静默跳过。

    今天真库是 0 条空值。真出现了说明采集侧漏了拼装 —— 那正是这个巡检要抓的
    事故，跳过等于把它藏起来。
    """
    conn = _fake_db([("feishu:nio:campus", "1" * 19, None)])
    with pytest.raises(AssertionError, match="没有 apply_url"):
        health.check_shapes(conn)
    conn.close()


def test_closed_jobs_are_not_in_the_sampling_pool() -> None:
    """已下架的岗位不进形状统计也不进抽样池。

    已知下架的岗位打开当然是死的，算进死链数是自己制造噪音。
    """
    good = "https://nio.jobs.feishu.cn/campus/position/" + "1" * 19 + "/detail"
    bad = "https://nio.jobs.feishu.cn/campus/position/" + "2" * 19  # 少 /detail
    conn = _fake_db([("feishu:nio:campus", "1" * 19, good),
                     ("feishu:nio:campus", "2" * 19, bad, "2026-08-01T00:00:00")])
    assert health.check_shapes(conn) == [], "下架岗位的坏形状不该报出来"
    assert len(health.sample_urls(conn, "feishu:nio:campus", 10)) == 1
    conn.close()


# ---------------------------------------------------------------- 形状：归一化

def test_normalize_keeps_negative_ids_in_the_same_shape() -> None:
    """`pid=-2` 和 `pid=104437` 必须归一成同一形状。

    腾讯源站自己就有 `pid=-2`~`-6` 五条占位卡片（不是我们的脏数据）。
    不认负号的写法（`[0-9]{4,}`）会让每条负 pid 自成一形状，腾讯报 6 种。
    """
    a = health.normalize_url("https://join.qq.com/post.html?pid=104437")
    b = health.normalize_url("https://join.qq.com/post.html?pid=-2")
    assert a == b == "https://join.qq.com/post.html?pid=<ID>"


def test_normalize_does_not_eat_the_portal_segment() -> None:
    """门户段（`/campus/`、`/edu/`）要保住 —— 那是「这批岗位从哪采的」。"""
    got = health.normalize_url(
        "https://nio.jobs.feishu.cn/campus/position/7123456789012345678/detail")
    assert got == "https://nio.jobs.feishu.cn/campus/position/<ID>/detail"


def test_normalize_collapses_only_whole_numeric_segments() -> None:
    """只换整段数字，不换段里的数字 —— 否则含数字的门户名会被吃掉半截。"""
    got = health.normalize_url("https://x.com/edu2/position/123456/detail")
    assert got == "https://x.com/edu2/position/<ID>/detail"


# ---------------------------------------------------------------- 正文判据

#: 实测的四种正文形状（`/tmp/verify_markers.py`，4 租户 × 4 用例，2026-08-12）。
#: 不用真串全文，只留判据认的那几个词 + 会误导判据的干扰词。
BROKEN = "您正在寻找的页面不存在"
FAKE_ID = "职位描述 undefined 职位要求 undefined"
CLOSED = "该职位已下线 查看工作机会"
HEALTHY = "AI算法实习生-剪映 职位描述 负责推荐算法 职位要求 本科及以上"


def test_broken_route_marker_means_our_bug() -> None:
    """少 `/detail` → broken_route。实测 4 个租户 4/4 命中，8 条正常页 0/8。"""
    assert health.classify(BROKEN) == "broken_route"


def test_undefined_marks_a_nonexistent_job() -> None:
    """形状对、id 不存在 → gone。实测 4/4 假 id 正文含 `undefined`，正常页 0/8。"""
    assert health.classify(FAKE_ID) == "gone"


def test_closed_marker_means_gone() -> None:
    """「该职位已下线」→ gone。

    注意：这条标记**没有实测到过** —— 探测用例全取自 `closed_at IS NULL`，
    一次没撞上下架页（方案 §3 假设表）。这条测试钉的是「代码认这个串」，
    不是「源站真的用这个串」。后者要拿 `closed_at IS NOT NULL` 的行实打。
    """
    assert health.classify(CLOSED) == "gone"


def test_a_real_job_page_is_healthy() -> None:
    assert health.classify(HEALTHY) == "healthy"


def test_job_description_label_alone_is_not_healthy() -> None:
    """含「职位描述」「职位要求」但也含 `undefined` → gone，不是 healthy。

    这条是被实测打死的一版判据：假 id 页**也**渲染这两个标签（4/4 命中），
    只是标签底下没内容。拿「有职位描述」判健康，等于拿「页面框架加载出来了」
    判「岗位存在」—— 4 条假 id 会全部假绿。
    """
    assert "职位描述" in FAKE_ID and "职位要求" in FAKE_ID
    assert health.classify(FAKE_ID) == "gone"


def test_broken_route_wins_over_undefined() -> None:
    """同时含两个标记时判 broken_route。

    顺序颠倒的后果：我们自己的 bug（链接拼错）被归成源站的业务事件
    （岗位没了）—— 而归成业务事件的那条不会有人去修。
    """
    assert health.classify(BROKEN + " undefined") == "broken_route"


def test_empty_body_is_unknown_not_dead() -> None:
    """正文空 → unknown。**判不出来不等于坏了。**

    并进「死」则慢租户每轮假红（nio 有正常页 15.9s 才首现标记）；
    并进「活」则判据失效那天巡检安静地全绿 —— 后者正是 2026-08-10 事故的形状。
    """
    assert health.classify("") == "unknown"
    assert health.classify("   \n  ") == "unknown"


def test_title_based_judgement_would_have_been_a_no_op() -> None:
    """钉住那条被否掉的判据**为什么**被否 —— 防下一个人捡回来。

    上一版方案打算按 title 的「岗位名段为空」判死：`split(" - ")[0].strip() == ""`。
    实测渲染出的 title 是 `- 蔚来校招`，破折号前**没有空格**，
    所以 ` - ` 根本不是它的子串，`split` 返回原串、判据恒为假。
    这条判据是 100% 空操作，写下去会得到一个永远绿的巡检。
    """
    rendered_title = "- 蔚来校招"
    assert " - " not in rendered_title
    assert rendered_title.split(" - ")[0].strip() != ""


def test_verdict_domain_is_a_whitelist() -> None:
    """所有判定都在 `VERDICTS` 里 —— 报表按这几个值出栏，多一个就漏计。"""
    for text in (BROKEN, FAKE_ID, CLOSED, HEALTHY, "", "随便什么字"):
        assert health.classify(text) in health.VERDICTS


def test_only_broken_route_counts_as_our_bug() -> None:
    """`gone` 不算我们的 bug。

    混在一起的后果：每轮都有固定几条「假警报」稀释信号，最后没人看巡检。
    """
    assert health.OUR_BUG == ("broken_route",)
    assert "gone" not in health.OUR_BUG
    assert "unknown" not in health.OUR_BUG


def test_sentinel_url_keeps_the_shape_and_kills_the_id() -> None:
    """对照组 URL：形状不变、id 换成不存在的。

    它是判据自己的体检。`undefined` 是这几条里最脆的一条 —— 源站换个前端框架，
    空值不再渲染成字面量 `undefined`，判据静默失效、假 id 全判 healthy。
    """
    eid = "7123456789012345678"
    url = f"https://nio.jobs.feishu.cn/campus/position/{eid}/detail"
    got = health.sentinel_url(url, eid)
    assert got == f"https://nio.jobs.feishu.cn/campus/position/{health.SENTINEL_ID}/detail"
    assert health.normalize_url(got) == health.EXPECTED_SHAPE["feishu:nio:campus"]
    assert eid not in got


# ---------------------------------------------------------------- 轮询的终止条件

class FakePage:
    """按脚本逐次返回正文，模拟页面边加载边变。

    `inner_text` 每被调一次就往前走一格，走完停在最后一格。
    """

    def __init__(self, script: list[str]) -> None:
        self.script = script
        self.calls = 0

    def goto(self, url, **kw) -> None:  # noqa: ARG002
        pass

    def inner_text(self, selector, **kw) -> str:  # noqa: ARG002
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[i]


@pytest.fixture
def fake_clock(monkeypatch):
    """让轮询按「读了几次正文」推进，不跟真实时钟绑。

    为什么必须假掉时钟：这几条测试要验的是**提前退出**。真时钟下，
    一旦提前退出被改坏，测试不会失败 —— 它会挂到预算耗尽。
    第一次跑这组改坏验证时 `budget=999` 就让 pytest 卡了两分钟被杀，
    那种「用超时表达的失败」在 CI 里读起来像环境问题，不像守卫报警。
    """
    ticks = {"t": 0.0}
    monkeypatch.setattr(health.time, "sleep",
                        lambda s: ticks.__setitem__("t", ticks["t"] + s))
    monkeypatch.setattr(health.time, "time", lambda: ticks["t"])
    return ticks


def test_healthy_does_not_stop_the_poll_early(fake_clock) -> None:
    """看起来像 healthy 的中间态**不许**提前返回。

    这是实测抓到的真 bug：第一版一见 healthy 就 return，4 条对照组
    （形状对、id 不存在）里 3 条被判成活的。nio 那条的真实时间线：

        2.2s  正文 0 字            → unknown
        6.2s  正文 20 字           → healthy   ← 第一版在这里返回了
       11.3s  正文 49 字 undefined → gone      ← 真判据 5 秒后才到

    页面框架先渲染，`undefined` 后到。`healthy` 的定义是「没有坏标记」，
    一个还没渲染完的页面天然满足它 —— 那是加载中途，不是结论。
    """
    page = FakePage(["", "职位 校招官网 登录", FAKE_ID])
    verdict, _ = health.probe_one(page, "http://x", budget=5.0)
    assert verdict == "gone", "healthy 中间态提前返回了，假 id 会被报成活岗位"


def test_broken_verdicts_do_stop_the_poll_early(fake_clock) -> None:
    """「坏」的判定要立刻返回 —— 坏标记 1.4s 就到，没必要等满 20s。

    没有这条，上一条可以靠「永远等满预算」假绿，而那样每条都拖满 20 秒，
    一轮巡检从 4 分钟变成 8 分钟。
    """
    page = FakePage([BROKEN, HEALTHY, HEALTHY])
    # budget 给小值：提前退出坏掉时这里要以「断言失败」结束，不是挂到预算耗尽。
    # 拿 budget=999 写这条的话，改坏 _EARLY_EXIT 会让测试真等 999 秒。
    verdict, _ = health.probe_one(page, "http://x", budget=0.5)
    assert verdict == "broken_route"
    assert page.calls == 1, f"判出坏了还继续轮询，读了 {page.calls} 次"


def test_a_page_that_stays_healthy_is_healthy(fake_clock) -> None:
    """等满预算也没有坏标记 → healthy。

    这就是「活着」的全部证据。反向对照：少了它，上面那条可以靠
    「永远返回 unknown」假绿 —— 而那样所有正常岗位都会进「判不出」栏。
    """
    page = FakePage([HEALTHY])
    verdict, _ = health.probe_one(page, "http://x", budget=0.01)
    assert verdict == "healthy"


def test_a_page_that_never_renders_is_unknown(fake_clock) -> None:
    """正文一直是空 → unknown，不是 healthy 也不是坏。"""
    page = FakePage([""])
    verdict, _ = health.probe_one(page, "http://x", budget=0.01)
    assert verdict == "unknown"


def test_unreadable_body_does_not_crash_the_sweep(fake_clock) -> None:
    """读正文抛异常时继续轮询，不让一条页面搞掉整轮。

    实测撞到过：sensetime 的哨兵页 `inner_text` 超时 30s 直接抛，
    把整个探测脚本崩了。一轮巡检打几十页，一页的异常不该带走其余几十页的结果。
    """

    class Flaky(FakePage):
        def inner_text(self, selector, **kw):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("Page.inner_text: Timeout 30000ms exceeded")
            return BROKEN

    verdict, _ = health.probe_one(Flaky([]), "http://x", budget=5.0)
    assert verdict == "broken_route"


def test_navigation_failure_is_error_not_dead(fake_clock) -> None:
    """导航本身失败 → error，不算死链。

    网断了、DNS 挂了不是源站的问题，把它算进死链数每轮假红。
    """

    class Dead(FakePage):
        def goto(self, url, **kw):
            raise TimeoutError("net::ERR_CONNECTION_REFUSED")

    verdict, _ = health.probe_one(Dead([]), "http://x")
    assert verdict == "error"


def test_early_exit_set_excludes_healthy() -> None:
    """把这条不对称显式钉住 —— 它读起来像个笔误，很容易被「顺手修好」。"""
    assert "healthy" not in health._EARLY_EXIT
    assert "unknown" not in health._EARLY_EXIT
    assert set(health._EARLY_EXIT) == {"broken_route", "gone"}


def test_tencent_is_declared_unjudgeable() -> None:
    """腾讯必须在 `UNJUDGEABLE` 里，不然 808 条会被打成 unknown 白烧 90 分钟。

    已复验：真 pid / 负 pid / 假 pid 三条渲染出逐字节相同的正文
    （875 字，无「职位描述」、无「不存在」）。`post.html?pid=` 是列表页。
    """
    assert "tencent_join" in health.UNJUDGEABLE
    assert "tencent_join" in health.EXPECTED_SHAPE, "判不出死活，但形状还得守着"
    for src in health.UNJUDGEABLE:
        assert src in health.EXPECTED_SHAPE


def _fake_db(rows) -> sqlite3.Connection:
    """临时内存库，只建 `jobs` 表里这几列。

    形状检查只读 `source_key` / `external_id` / `apply_url` / `closed_at`，
    不必拖整份 schema 进来。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs (source_key TEXT, external_id TEXT, "
                 "apply_url TEXT, closed_at TEXT)")
    for row in rows:
        source, eid, url = row[0], row[1], row[2]
        closed = row[3] if len(row) > 3 else None
        conn.execute("INSERT INTO jobs VALUES (?,?,?,?)", (source, eid, url, closed))
    conn.commit()
    return conn
