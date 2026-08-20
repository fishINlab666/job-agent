"""飞书代投测试 —— 两阶段闸门 + 登录门 + 填表判据。

测的重点分三块：

1. **闸门结构**：协议里没有一步到底的 `submit()`、blocked 不发 token、
   无 token / 过期 / 重放 / 漂移全部拒绝。这几条不依赖表单长什么样，
   是产品承诺「不会背着你投出去」的防线。
2. **边界**：多租户必须给 tenant、老形状 apply_url 直接拦下、
   SPA 的「页面不存在」按正文判而不是按状态码判。
3. **填表判据**：选择器收窄到叶子、下拉值不做近似匹配、label 漂移能被
   `label_drift` 抓到、下拉命中歧义时拒绝猜。

注意这些全是对假 page 跑的 —— 只验形状，不验行为。选择器在真页面上还灵不灵，
只有 `label_drift()` 对着真 DOM 跑才知道（2026-08-10 逐条核实过一次）。

页面用假 locator 模拟：按选择器文案返回命中数，而不是按调用顺序排一串
side_effect。这样将来多加一次探测调用，老测试不会莫名其妙地错位。
"""
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from jobagent import profile as P
from jobagent.submitters.base import SESSIONS, LiveSession, TokenError
from jobagent.submitters.feishu import FeishuSubmitter

JOB = {
    "external_id": "7592540658310154534",
    "source_key": "feishu:nio:campus",
    "title": "提前批-技术项目经理",
    "apply_url": "https://nio.jobs.feishu.cn/campus/position/7592540658310154534/detail",
}


@pytest.fixture(autouse=True)
def clean_sessions():
    """会话表是进程级单例，测试之间必须清干净。"""
    yield
    for token in list(SESSIONS._sessions):
        SESSIONS.discard(token)


@pytest.fixture
def profile():
    """嵌套结构的画像 —— 和真实 profile.yaml 一致。"""
    return P.from_dict({
        "identity": {"name": "测试用户", "phone": "13800138000",
                     "email": "test@example.com"},
        "education": [{"school": "清华大学", "major": "计算机科学与技术",
                       "degree": "硕士", "end": "2027-06", "city": "北京"}],
    })


@pytest.fixture
def mock_page():
    page = MagicMock()
    page.url = "https://nio.jobs.feishu.cn/campus/position/1/detail"
    page.locator.return_value.count.return_value = 0
    return page


def fake_page(*, missing=False, closed=False, no_apply_btn=False,
              logged_in=False, url=None, labels_present=False):
    """按选择器文案作答的假页面。

    logged_in=True 模拟「已登录，点投递后进了表单」—— 本轮那也是 blocked，
    但 blocker 文案不同（要求补 FORM_FIELDS 而不是要求登录）。
    """
    state = {"clicked": False}
    base_url = url or JOB["apply_url"]
    page = MagicMock()
    page.url = base_url

    def counts(sel: str) -> int:
        if "页面不存在" in sel:
            return int(missing)
        if "已停止" in sel:
            return int(closed)
        if "获取验证码" in sel:
            # 点了投递之后才会出现登录页文案
            return int(state["clicked"] and not logged_in)
        if labels_present and sel.startswith('label:text-is('):
            # 让 label_drift 查空。默认返 0 会让 prepare 在「一个都对不上」
            # 那道闸就停下，走不到填表 —— 想验填表行为的测试必须开这个。
            return 1
        return 0

    def locator(sel: str):
        loc = MagicMock()
        loc.count.return_value = counts(sel)
        target = MagicMock()
        if "投递" in sel:
            loc.count.return_value = 0 if no_apply_btn else 1

            def click(*a, **k):
                state["clicked"] = True
                if not logged_in:
                    page.url = (
                        "https://nio.jobs.feishu.cn/campus/login"
                        "?redirect_path=%2Fresume%2F1%2Fapply"
                    )
            target.click.side_effect = click
        loc.first = target
        return loc

    page.locator.side_effect = locator
    page.state = state
    return page


def patched(page):
    """把 sync_playwright 替成产出指定假页面的工厂。"""
    pw = MagicMock()
    pw.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
    factory = MagicMock()
    factory.return_value.start.return_value = pw
    return factory


def prep(page, job=None, *, tenant="nio", sub=None, fill_fields=True, **kw):
    with patch("jobagent.submitters.feishu.sync_playwright", patched(page)):
        s = sub or FeishuSubmitter(tenant=tenant, **kw)
        return s.prepare(job or JOB, _prof(), fill_fields=fill_fields)


def _prof():
    return P.from_dict({"identity": {"name": "测试用户", "phone": "13800138000"}})


# ---- 结构约束：闸门本体 ----

class TestGateStructure:
    def test_no_one_shot_submit(self):
        """协议里不能有一步到底的 submit()。

        谁哪天为了省事加回 submit()，这里就红。
        """
        sub = FeishuSubmitter(tenant="nio")
        assert not hasattr(sub, "submit")
        assert all(hasattr(sub, m) for m in ("prepare", "execute", "discard"))

    def test_execute_without_token_refuses(self):
        """没 token 调不动 execute。"""
        with pytest.raises(TokenError) as exc:
            FeishuSubmitter(tenant="nio").execute("伪造的token")
        assert exc.value.reason == "unknown"

    def test_blocked_plan_carries_no_token(self, profile):
        """blocked 时不许发 token，也不许注册会话。

        发了 token 就等于「没到提交这步，却给了提交的钥匙」。
        """
        plan = prep(fake_page())

        assert plan.status == "blocked"
        assert plan.confirm_token == ""
        assert not plan.is_ready
        assert SESSIONS._sessions == {}, "blocked 却注册了会话"


# ---- 多租户：不许猜是哪家公司 ----

class TestTenant:
    def test_tenant_required(self):
        """飞书是多租户系统，不给租户直接拒绝构造。

        少一个租户参数不会报错，只会投到别人家公司去 —— 所以这里必须硬拦。
        """
        with pytest.raises(ValueError, match="tenant"):
            FeishuSubmitter(tenant="")

    def test_known_tenants_map_to_verified_names(self):
        """四个租户的公司名是核实过的（kb/company-portals.md）。"""
        assert FeishuSubmitter(tenant="nio").company == "蔚来"
        assert FeishuSubmitter(tenant="xiaopeng").company == "小鹏汽车"
        assert FeishuSubmitter(tenant="bytedance").company == "字节跳动"
        assert FeishuSubmitter(tenant="sensetime").company == "商汤科技"

    def test_unknown_tenant_falls_back_to_slug_not_invented(self):
        """没核实过归属的租户，公司名退回 slug，**不编中文名**。

        「域名 slug 不是归属判据」是这个项目栽过的坑
        （luckin 不是瑞幸、horizon 不是地平线）。
        """
        sub = FeishuSubmitter(tenant="someco")
        assert sub.company == "someco"

    def test_system_is_registered_as_feishu(self):
        """注册键是招聘系统，不是公司。一个类管四家。"""
        assert FeishuSubmitter.system == "feishu"


# ---- 本轮特有：老形状 apply_url 要被拦下 ----

class TestApplyUrlGuard:
    def test_missing_url_blocks(self):
        plan = prep(fake_page(), job={**JOB, "apply_url": ""})
        assert plan.status == "blocked"
        assert "没有 apply_url" in plan.blocker

    def test_old_shape_blocked_with_actionable_message(self):
        """少 /detail 的老形状直接拦下，并告诉用户跑哪条命令。

        不拦的话会打开一个「页面不存在」，然后被归因成「站点改版了」——
        而真正的原因是库里的链接没修（plan 010）。
        """
        old = "https://nio.jobs.feishu.cn/campus/position/777"
        plan = prep(fake_page(), job={**JOB, "apply_url": old})

        assert plan.status == "blocked"
        assert "/detail" in plan.blocker
        assert "repair-apply-url" in plan.blocker, "没告诉用户怎么修"

    def test_never_builds_url_itself(self):
        """反向用例：导航只用库里的 apply_url，不自己拼。

        自己拼就得在 submitter 里再维护一份 host/门户/后缀的知识，
        和适配器两处走岔。这里用一个 host 完全不同的链接，
        断言它就是被访问的那个。

        注意：LOGIN_HOSTS 的 host 改写是这条规则的**唯一例外**，且只换 netloc、
        只影响导航 —— 见下面两条。sensetime 不在那张表里，所以这条不受影响。
        """
        odd = "https://hr-jobs.sensetime.com/edu/position/9/detail"
        page = fake_page(url=odd)
        prep(page, job={**JOB, "apply_url": odd})

        page.goto.assert_called_once()
        assert page.goto.call_args[0][0] == odd


class TestLoginHostRewrite:
    """字节租户要改 host 才能登录，见 FeishuSubmitter.LOGIN_HOSTS。"""

    def test_bytedance_navigates_to_working_host(self):
        """导航去 jobs.bytedance.com，不是库里那个 feishu 镜像。

        feishu 镜像上登录控件依赖的 POST /accounts/flow/init 是 404，
        控件静默不渲染 —— 页面一片空白、不报错，排查代价极高。
        """
        url = "https://bytedance.jobs.feishu.cn/campus/position/766/detail"
        page = fake_page(url=url)
        prep(page, job={**JOB, "apply_url": url}, tenant="bytedance")

        navigated = page.goto.call_args[0][0]
        assert navigated == "https://jobs.bytedance.com/campus/position/766/detail"

    def test_rewrite_touches_host_only(self):
        """只换 netloc，path/query/fragment 一律保留。

        同路径同岗位 ID 在两个 host 上都有效，所以不需要、也不该在这里
        掺进任何路径形状的知识。
        """
        sub = FeishuSubmitter(tenant="bytedance")
        got = sub._login_host_url(
            "https://bytedance.jobs.feishu.cn/campus/login?redirect_path=%2Fa%2Fb#frag"
        )
        assert got == "https://jobs.bytedance.com/campus/login?redirect_path=%2Fa%2Fb#frag"

    def test_untouched_tenants_keep_their_host(self):
        """不在表里的租户不改写 —— nio/xiaopeng 原 host 的登录是好的。"""
        for tenant, host in (("nio", "nio.jobs.feishu.cn"),
                             ("xiaopeng", "xiaopeng.jobs.feishu.cn")):
            url = f"https://{host}/campus/position/1/detail"
            assert FeishuSubmitter(tenant=tenant)._login_host_url(url) == url

    def test_plan_keeps_original_url_for_the_record(self):
        """存证记的是库里那个链接，不是我们绕道走的那个。"""
        url = "https://bytedance.jobs.feishu.cn/campus/position/766/detail"
        plan = prep(fake_page(url=url), job={**JOB, "apply_url": url},
                    tenant="bytedance")
        assert plan.apply_url == url


# ---- 页面状态：SPA 的坑 ----

class TestPageStates:
    def test_page_missing_blocks(self):
        """渲染出「页面不存在」就停，不往下点。"""
        plan = prep(fake_page(missing=True))
        assert plan.status == "blocked"
        assert "页面不存在" in plan.blocker

    def test_missing_judged_by_body_not_status_code(self, mock_page):
        """判据是渲染后的正文，不是 HTTP 状态码。

        这条是 plan 010 那个 bug 的回归测试：SPA 的 404 在渲染层，
        HTTP 照样 200 且 body 200KB。谁把判据改回状态码，这条红。
        """
        sub = FeishuSubmitter(tenant="nio")
        mock_page.locator.return_value.count.return_value = 0
        assert not sub._is_page_missing(mock_page)
        mock_page.locator.return_value.count.return_value = 1
        assert sub._is_page_missing(mock_page)
        # 用的是文案选择器，不是 response.status
        sel = mock_page.locator.call_args[0][0]
        assert "页面不存在" in sel

    def test_closed_job_blocks(self):
        plan = prep(fake_page(closed=True))
        assert plan.status == "blocked"
        assert "已关闭" in plan.blocker

    def test_no_apply_button_blocks(self):
        plan = prep(fake_page(no_apply_btn=True))
        assert plan.status == "blocked"
        assert "投递" in plan.blocker

    def test_login_gate_blocks_and_tells_user_what_to_do(self):
        """撞登录门时 blocked，并说清「只能你自己做」。

        登录是手机号+验证码，绕不过也不该绕。
        """
        plan = prep(fake_page())

        assert plan.status == "blocked"
        assert "需要登录" in plan.blocker
        assert "--no-headless" in plan.blocker
        assert "蔚来" in plan.blocker, "没说是哪家公司的账号"

    def test_need_login_uses_elements_not_url(self):
        """登录判据用控件元素，不用 URL。

        2026-08-10 换掉了原来的 URL 判据。原因：改 host 走 jobs.bytedance.com 之后，
        登录表单**内联渲染在 /campus/resume/<id>/apply 上**，URL 全程不含 /login。
        拿 URL 判会漏掉登录门，然后把登录表单当投递表单去填。

        也不用「投递按钮不见了」反推 —— 点投递后按钮本来就不在了，那样会把
        「跳到别处」也算成需要登录。
        """
        sub = FeishuSubmitter(tenant="bytedance")

        # 关键回归：URL 不含 /login，但验证码框在 —— 必须判成需要登录
        inline = _page_with_visible({"input#code:visible": 1})
        inline.url = "https://jobs.bytedance.com/campus/resume/123/apply"
        assert sub._need_login(inline), "URL 不含 /login 的内联登录门被漏掉了"

        # 飞书标准控件（nio/xiaopeng）走另一个选择器
        atsx = _page_with_visible({"input.atsx-phone-input:visible": 1})
        atsx.url = "https://nio.jobs.feishu.cn/campus/login?redirect_path=x"
        assert FeishuSubmitter(tenant="nio")._need_login(atsx)

        # URL 长得像登录页，但控件元素一个都没有（就是字节 feishu 镜像那个空容器
        # 的形状）—— 不该只因为 URL 就判成「登录门在」
        empty = _page_with_visible({})
        empty.url = "https://bytedance.jobs.feishu.cn/campus/login?redirect_path=x"
        assert not sub._need_login(empty)

    def test_email_password_tab_still_counts_as_login_gate(self):
        """默认的「邮箱+密码」tab 也是登录门。

        2026-08-10 实跑踩到的：jobs.bytedance.com 登录页默认落在邮箱密码 tab，
        `#code` 还不存在。只认 `#code` 的那版把这里判成「不需要登录」，
        再被 _form_ready 当成投递表单 —— 差一步就往登录框里填简历字段。
        `#password` 是最硬的判据：投递表单不会问密码。
        """
        sub = FeishuSubmitter(tenant="bytedance")
        email_tab = _page_with_visible({
            "input#password:visible": 1,
            "input:visible, textarea:visible, select:visible": 3,
        })
        email_tab.url = "https://jobs.bytedance.com/campus/login?redirect_path=x"
        assert sub._need_login(email_tab)
        assert not sub._form_ready(email_tab)

    def test_form_ready_does_not_mistake_login_form_for_apply_form(self):
        """登录表单自己也有输入框，不能靠数输入框判「表单好了」。

        登录表单实测 3 个可见 input（手机号/验证码/勾选框）。任何
        `count() > N` 的阈值判法都会把登录页当成投递表单。
        """
        sub = FeishuSubmitter(tenant="bytedance")

        login = _page_with_visible(
            {"input#code:visible": 1, "input:visible, textarea:visible, select:visible": 3}
        )
        assert not sub._form_ready(login), "把登录表单当成投递表单了"

        form = _page_with_visible(
            {"input:visible, textarea:visible, select:visible": 5}
        )
        assert sub._form_ready(form)


def _all_anchors_present() -> dict[str, int]:
    """造出「FORM_FIELDS 锚定的每个 label 在页面上都能找到」的 counts。

    按**锚点**造而不是按展示名 —— label_drift 查的是选择器里 `text-is("...")`
    的内容。两者不一样（「手机区号」锚在「手机号码」上）。
    """
    from jobagent.submitters.feishu import _ANCHOR_RE, FORM_FIELDS

    return {
        f'label:text-is("{a}")': 1
        for _k, _l, sel, _a in FORM_FIELDS
        for a in _ANCHOR_RE.findall(sel)
    }


def _page_with_visible(counts: dict[str, int]):
    """造一个假 page：只有 counts 里列出的选择器有元素，其余为 0。

    `locator(sel).locator("visible=true")` 这条链也会命中同一个数 ——
    `_pick` 走的是这个形状。
    """
    from unittest.mock import MagicMock

    page = MagicMock()
    page.url = ""

    def locator(sel):
        loc = MagicMock()
        n = counts.get(sel, 0)
        loc.count.return_value = n
        loc.locator.return_value = loc      # .locator("visible=true") 保持同一个数
        loc.first = loc
        return loc

    page.locator.side_effect = locator
    return page


def _page_with_cards(*, cards: int, per_card: int):
    """造一个有 `cards` 张条目卡的假 page，卡内目标字段命中 `per_card` 个。

    返回 (page, calls)，`calls["nth"]` 记下取过第几张 —— 「收窄到第 2 条」这件事
    只能从这里验：命中数对了不代表取的是对的那张。
    """
    from unittest.mock import MagicMock

    calls: dict[str, list[int]] = {"nth": []}

    inner = MagicMock()
    inner.count.return_value = per_card
    inner.locator.return_value = inner
    inner.first = inner

    card = MagicMock()
    card.locator.return_value = inner

    card_list = MagicMock()
    card_list.count.return_value = cards
    card_list.locator.return_value = card_list       # .locator("visible=true")
    card_list.nth.side_effect = lambda i: (calls["nth"].append(i) or card)

    page = MagicMock()
    page.url = ""
    page.locator.return_value = card_list
    return page, calls


# ---- 字段计划 ----

class TestPlanFields:
    def test_plan_covers_the_probed_fields(self, profile):
        """FORM_FIELDS 已按实测填好，计划要覆盖到画像里有值的字段。

        （这条以前断言的是「空表」—— 那时表单还没探明。2026-08-10 在
        jobs.bytedance.com 真登录后逐条核实了选择器，表填上了，断言跟着换成
        「填对了什么」。）
        """
        plans = {f.label: f for f in FeishuSubmitter(tenant="nio")._plan_fields(profile)}

        assert plans["姓名"].value == "测试用户"
        assert plans["姓名"].action == "fill"
        # 学历是自定义下拉，不是原生 select —— 动作必须是 pick
        assert plans["学历"].action in ("pick", "skip")

    def test_selectors_scope_to_leaf_not_array_wrapper(self):
        """选择器必须带 `-size-large`（叶子），不能只写 `.ud-formily-item`。

        为什么钉这条：formily 的 item 是嵌套的，教育经历那种可增删的段落，
        外层数组容器也带 `.ud-formily-item`。少了 `-size-large`，
        `:has(label:text-is("学校名称"))` 命中的是整段容器，后面的 `input`
        会把段内 10 个输入框全抓进来（实测命中 10 而不是 1）。
        """
        from jobagent.submitters.feishu import FORM_FIELDS

        for _key, label, selector, _action in FORM_FIELDS:
            if selector.startswith("input[type="):
                continue        # 简历上传锚在 file input 上，不走 label
            assert "-size-large" in selector, f"{label} 的选择器没收窄到叶子"

    def test_dropdown_value_not_in_options_is_skipped_not_guessed(self, profile):
        """画像里的学历对不上页面选项时，跳过并说明，不做近似匹配。

        「硕士研究生」→「硕士」这种转换看着无害，但那是代投替用户改了申报内容。
        """
        profile.fields["degree"].value = "硕士研究生"      # 不在选项里
        plans = {f.label: f for f in FeishuSubmitter(tenant="nio")._plan_fields(profile)}

        assert plans["学历"].action == "skip"
        assert "不在页面选项里" in (plans["学历"].note or "")

    def test_dropdown_exact_match_is_kept(self, profile):
        """正好等于某个选项时，正常走 pick。"""
        profile.fields["degree"].value = "硕士"
        plans = {f.label: f for f in FeishuSubmitter(tenant="nio")._plan_fields(profile)}

        assert plans["学历"].action == "pick"
        assert plans["学历"].value == "硕士"

    def test_page_validation_error_unmarks_filled(self):
        """页面说值不合法 → 那个字段不能再显示「已填」。

        为什么必须这样：`page.fill` 对一个非法身份证号照样成功，什么都不抛。
        只看 `_fill` 有没有异常，清单上会写「已填」，然后带着非法值提交出去，
        对方系统里多一条脏数据。实测这个错误是页面前端给的
        （`个人证件 → 请输入正确的身份证号码`，2026-08-10）。
        """
        from jobagent.submitters.base import FieldPlan

        f = FieldPlan(selector="x", label="个人证件", value="110000000000000000",
                      action="fill", filled=True)
        rest = FeishuSubmitter._apply_page_errors(
            [f], {"个人证件": "请输入正确的身份证号码"}
        )

        assert f.filled is False
        assert "请输入正确的身份证号码" in (f.note or "")
        assert rest == {}

    def test_unattributed_page_error_is_returned_not_swallowed(self):
        """归不到字段的校验错误要交回调用方，不能吞。

        吞掉等于让用户以为页面上什么提示都没有 —— 那些提示可能来自我们
        压根没填的项（学历类型、期望工作地点之类）。
        """
        from jobagent.submitters.base import FieldPlan

        f = FieldPlan(selector="x", label="姓名", value="张三", filled=True)
        rest = FeishuSubmitter._apply_page_errors([f], {"学历类型": "请选择学历类型"})

        assert rest == {"学历类型": "请选择学历类型"}
        assert f.filled is True          # 别人的错误不该牵连这个字段

    def test_page_errors_returns_empty_when_readout_fails(self):
        """读错误这步自己炸了 → 返回空表，不能让整个 prepare 挂掉。

        它是个附加检查。为了读一句提示把已经填好的表单弄没了，不划算。
        """
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("evaluate 挂了")

        assert FeishuSubmitter(tenant="bytedance").page_errors(page) == {}

    def test_phone_country_code_is_pinned_and_visible_on_the_plan(self, profile):
        """区号固定 +86，而且必须出现在确认清单上。

        两件事各有理由：
        - **固定**：页面默认 +1（美国）。号码对了区号错了，HR 照样打不通。
        - **上清单**：藏在 `_fill` 里偷偷设，等于在两阶段闸门上开个口 ——
          用户看不见代投替他填了什么。source 标「固定值」以示不是画像来的。
        """
        plans = {f.label: f for f in FeishuSubmitter(tenant="bytedance")._plan_fields(profile)}

        cc = plans["手机区号"]
        assert cc.value == "+86"
        assert cc.action == "pick"
        assert cc.source == "固定值"

    def test_country_code_keeps_click_text_and_digest_value_apart(self):
        """点选用的文案和参与 digest 的值必须分开存。

        两头都错得很具体（都是 2026-08-10 实测）：
        - 只拿「+86」去点：`:has-text` 是子串匹配，251 项区号表里可见命中 2 个
          （「+86 （中国大陆）」「+869 （圣基茨和尼维斯）」），`_pick` 拒绝猜、抛。
        - 拿全称当 value：控件选完只显示「+86」，`_readback_digest` 读回来对不上
          计划值，`execute` 每次都判 `drifted`，**永久拒绝提交**。

        所以 value = 控件显示值（digest 口径），option_text = 列表项文案（点击口径）。
        """
        from jobagent.submitters.feishu import FIXED_VALUES, PICK_OPTION_TEXT

        assert FIXED_VALUES["phone_cc"] == "+86"
        assert "中国大陆" in PICK_OPTION_TEXT["phone_cc"]
        assert PICK_OPTION_TEXT["phone_cc"].startswith(FIXED_VALUES["phone_cc"])

    def test_pick_clicks_option_text_not_the_digest_value(self):
        """`_fill` 对 pick 字段要拿 option_text 去点，不是拿 value。

        拿 value（「+86」）去点会撞上子串歧义。这条测的就是那个分流没接错。
        """
        from jobagent.submitters.base import FieldPlan

        sub = FeishuSubmitter(tenant="bytedance")
        seen: list[str] = []
        # 签名写宽一点：_pick 的参数以后还会加，写死了会让这条测试用
        # TypeError 假装通过（_fill 把它吞成「填写失败」，seen 空着，断言才炸）。
        sub._pick = lambda page, sel, val, *a, **kw: seen.append(val)

        f = FieldPlan(selector="#cc", label="手机区号", value="+86",
                      action="pick", option_text="+86 （中国大陆）")
        sub._fill(MagicMock(), [f])

        assert seen == ["+86 （中国大陆）"], f"字段 note: {f.note}"
        assert f.value == "+86"          # 计划值不能被点选文案改掉

    def test_pick_refuses_when_option_match_is_ambiguous(self):
        """下拉项命中数不是 1 就抛，不赌「第一个大概是对的」。

        `:has-text` 是子串匹配。学历那个列表里「硕士」能同时命中
        「硕士」和假想的「硕士研究生」。选错学历没法从提交结果里看出来，
        所以宁可报错让人工接手。
        """
        from jobagent.submitters.feishu import SELECT_ITEM

        sub = FeishuSubmitter(tenant="bytedance")
        page = _page_with_visible({
            ".sel": 1,
            f'{SELECT_ITEM}:has-text("硕士")': 2,        # 歧义
        })

        with pytest.raises(ValueError, match="命中 2 个"):
            sub._pick(page, ".sel", "硕士")

        page.keyboard.press.assert_called_with("Escape")   # 抛之前要把下拉关掉

    def test_pick_refuses_when_option_is_missing(self):
        """一个都没命中同样抛 —— 静默不选会留下一个空下拉照样提交。"""
        sub = FeishuSubmitter(tenant="bytedance")
        page = _page_with_visible({".sel": 1})

        with pytest.raises(ValueError, match="命中 0 个"):
            sub._pick(page, ".sel", "硕士")

    def test_label_drift_is_empty_when_every_label_is_present(self):
        """label 都在 → 空表。空表才是「可以信任这次填写」的前提。"""
        page = _page_with_visible(_all_anchors_present())

        assert FeishuSubmitter(tenant="bytedance").label_drift(page) == []

    def test_label_drift_checks_anchors_not_display_names(self):
        """查的是选择器里锚定的文案，不是展示名 —— 这两个可以不一样。

        「手机区号」在页面上没有自己的 label（它锚在「手机号码」上）。
        按展示名查会报一个不存在的漂移，prepare 于是把没坏的东西当坏的处理。
        """
        page = _page_with_visible(_all_anchors_present())
        drift = FeishuSubmitter(tenant="bytedance").label_drift(page)

        assert "手机区号" not in drift, "把展示名当页面 label 查了"

    def test_label_drift_names_the_renamed_label(self):
        """站点把「学校名称」改名 → 必须点名报出这一个，而不是静默跳过。

        为什么钉这条：FORM_FIELDS 每条都锚在中文 label 上（那些 input 没 id、
        没 name、class 跨字段通用），改文案是最可能发生的静默失效。没有这条守卫，
        `_fill` 会把每个 miss 吞成 per-field note，最后交出一张空表单
        加一句「填了 0 个字段」。
        """
        present = _all_anchors_present()
        del present['label:text-is("学校名称")']
        drift = FeishuSubmitter(tenant="bytedance").label_drift(_page_with_visible(present))

        assert drift == ["学校名称"]

    def test_label_drift_skips_the_file_input_field(self):
        """简历附件锚在 `input[type=file]` 上，不该因为「没有这个 label」被误报。"""
        page = _page_with_visible({})        # 一个 label 都找不到
        drift = FeishuSubmitter(tenant="bytedance").label_drift(page)

        assert "简历附件" not in drift

    def test_plan_fields_reads_nested_profile_when_filled(self, profile, monkeypatch):
        """等 FORM_FIELDS 填上之后，必须能从**嵌套**画像取到值。

        腾讯那版栽过这个：submitter 读扁平 key，真实画像是嵌套的，
        每个字段都取到 None，表单一个字没填却照样点提交。
        这里预先钉住，免得下一轮填字段表时重犯。
        """
        monkeypatch.setattr(
            "jobagent.submitters.feishu.FORM_FIELDS",
            [("name", "姓名", 'input[name="name"]', "fill"),
             ("school", "学校", 'input[name="school"]', "fill")],
        )
        plans = {f.label: f for f in FeishuSubmitter(tenant="nio")._plan_fields(profile)}

        assert plans["姓名"].value == "测试用户"
        assert plans["姓名"].source == "identity.name"
        assert plans["学校"].value == "清华大学"
        assert plans["学校"].source == "education[0].school"


# ---- execute 的拒绝路径 ----
# 这几条是产品承诺的回归防线。骨架不依赖表单长什么样，现在就能钉。

def _seeded_session(page=None):
    """手工塞一个 ready 会话进 SESSIONS，绕过本轮必定 blocked 的 prepare。

    为什么要手工塞：本轮 prepare 一定返回 blocked、不发 token，
    但 execute 的拒绝路径必须现在就测 —— 等表单探明再补，
    中间这段时间闸门是没有回归防线的。
    """
    from jobagent.submitters.base import PLAN_TTL_SECONDS, SubmissionPlan, mint_token

    page = page or MagicMock()
    plan = SubmissionPlan(
        job_id="1", source_key="feishu:nio:campus", company="蔚来",
        apply_url=JOB["apply_url"],
        confirm_token=mint_token(), expires_at=time.time() + PLAN_TTL_SECONDS,
    )
    SESSIONS.put(LiveSession(plan, page, lambda: None))
    return plan


class TestExecuteRejections:
    def test_expired_token_refuses(self):
        plan = _seeded_session()
        SESSIONS.peek(plan.confirm_token).expires_at = time.time() - 1

        res = FeishuSubmitter(tenant="nio").execute(plan.confirm_token)

        assert res.status == "blocked"
        assert res.note == "token_expired"

    def test_replay_refuses(self):
        """同一个 token 提交两次，第二次必须拒绝。

        注意第二次是 `blocked` + `note=token_consumed`，**不是抛异常** ——
        `execute` 把 `TokenError` 收在里面转成结果（只有「压根没这个 token」
        那条才抛，见 `test_execute_without_token_refuses`）。
        """
        plan = _seeded_session()
        sub = FeishuSubmitter(tenant="nio")
        sub.execute(plan.confirm_token)

        second = sub.execute(plan.confirm_token)

        assert second.status == "blocked"
        assert second.note == "token_consumed"

    def test_drift_refuses(self):
        """确认期间页面值被改了，拒绝提交。

        用户确认的是「这些值填进这些字段」，值变了就等于没确认过。
        """
        from jobagent.submitters.base import FieldPlan

        page = MagicMock()
        plan = _seeded_session(page)
        plan.fields = [FieldPlan(
            selector='input[name="name"]', label="姓名",
            value="测试用户", action="fill", filled=True,
        )]
        page.input_value.return_value = "被改成了别人"

        res = FeishuSubmitter(tenant="nio").execute(plan.confirm_token)

        assert res.status == "blocked"
        assert res.note == "token_drifted"

    def test_discard_records_abandoned(self):
        plan = _seeded_session()

        res = FeishuSubmitter(tenant="nio").discard(plan.confirm_token)

        assert res.status == "abandoned"
        assert res.company == "蔚来"
        assert SESSIONS.peek(plan.confirm_token) is None

    def test_sweep_reclaims_expired_sessions(self):
        """放弃的确认不能把浏览器泄漏掉。"""
        plan = _seeded_session()
        SESSIONS.peek(plan.confirm_token).expires_at = time.time() - 1

        assert SESSIONS.sweep() == 1
        assert SESSIONS.peek(plan.confirm_token) is None


# ---- 歧义守卫：命中数不是 1 就不填 ----

class TestAmbiguityGuard:
    """`_one` 存在的理由：`.first` 会在重名字段上静默填错一条。

    教育经历段点「添加」会长出第二套重名字段（学校名称/学历/专业，2026-08-10
    实测 9 个叶子变 18 个）。真有两条时 `.first` 按 DOM 顺序挑第一条，而画像层
    是**特意挑最近一段**算的值 —— 第一条要是本科，硕士的数据就写进本科那行了。
    `filled=True`、回读也走 `.first`、digest 照样对得上，一声不响。
    """

    def _sub(self):
        return FeishuSubmitter(tenant="bytedance")

    def test_one_returns_the_single_visible_match(self):
        page = _page_with_visible({"#x": 1})

        assert self._sub()._one(page, "#x", "姓名") is not None

    def test_one_refuses_when_two_records_share_a_label(self):
        """两条同名记录 → 抛，并且原话里要有「多条同名记录」这个线索。"""
        page = _page_with_visible({"#x": 2})

        with pytest.raises(ValueError, match="可见命中 2 个"):
            self._sub()._one(page, "#x", "学校名称")

    def test_one_refuses_when_nothing_matches(self):
        page = _page_with_visible({})

        with pytest.raises(ValueError, match="可见命中 0 个"):
            self._sub()._one(page, "#x", "学校名称")

    def test_scope_narrows_to_one_card_so_duplicates_stop_being_ambiguous(self):
        """收窄之后「命中 1 个」重新变成有意义的断言。

        可重复段里每条目的字段名完全一样。不收窄的话两条目就是死局：
        `.first` 静默挑一个，`_one` 全部拒绝 —— 两个都填不成第 2 条。
        """
        page, calls = _page_with_cards(cards=2, per_card=1)

        loc = self._sub()._one(page, "#school", "学校名称",
                               scope=".card", scope_index=1)

        assert loc is not None
        assert calls["nth"] == [1]        # 真的取了第 2 张，不是第 1 张

    def test_scope_index_beyond_the_card_count_says_how_many_there_are(self):
        """要第 3 条但只有 1 条 → 抛，并且把「只有几条」说出来。

        这是 `_plan_fields` 算 scope_index 时唯一可能算错的方向（画像里条目数
        比页面上的卡片多），错误话术得让人一眼看出是哪边多了。
        """
        page, _calls = _page_with_cards(cards=1, per_card=1)

        with pytest.raises(ValueError, match="只有 1 条"):
            self._sub()._one(page, "#school", "学校名称",
                             scope=".card", scope_index=2)

    def test_scoped_ambiguity_names_which_card(self):
        """卡片内还歧义 → 错误话术要带「第几条」，不然没法定位。"""
        page, _calls = _page_with_cards(cards=2, per_card=2)

        with pytest.raises(ValueError, match="第 2 条里可见命中 2 个"):
            self._sub()._one(page, "#school", "学校名称",
                             scope=".card", scope_index=1)

    def test_readback_uses_the_same_scope_as_the_write(self):
        """回读必须和写走同一条寻址。

        读第 1 条、写第 2 条的话，digest 比的是另一张卡 —— 对不上是虚警，
        **对上了更糟**：拿一张没动过的卡替被改过的卡背书，闸门就废了。
        """
        from jobagent.submitters.base import FieldPlan, SubmissionPlan

        seen: list[tuple[str | None, int]] = []

        def spy(page, sel, label, scope=None, idx=0):
            seen.append((scope, idx))
            loc = MagicMock()
            # 得回真字符串：MagicMock 赋进 f.value 之后 digest 序列化会炸，
            # 那样测试是死在别的地方，验不到寻址。
            loc.input_value.return_value = "测试大学"
            loc.inner_text.return_value = "测试大学"
            return loc

        sub = self._sub()
        sub._one = spy

        plan = SubmissionPlan(job_id="j", company="字节跳动", source_key="feishu", fields=[
            FieldPlan(selector="#s", label="学校名称", value="测试大学",
                      filled=True, scope=".card", scope_index=1),
        ])
        sub._readback_digest(MagicMock(), plan)

        assert seen == [(".card", 1)]

    def test_education_fields_are_scoped_to_the_same_card(self, profile):
        """学校名称/专业/学历必须落在**同一张**教育卡上。

        三个字段分散到不同卡片就等于把一段学历拆着填 —— 页面上会出现
        「A 大学 + B 专业」这种谁都没申报过的组合。
        """
        plans = {f.label: f for f in
                 FeishuSubmitter(tenant="bytedance")._plan_fields(profile)}

        scopes = {plans[lb].scope for lb in ("学校名称", "专业", "学历")}
        idxs = {plans[lb].scope_index for lb in ("学校名称", "专业", "学历")}

        assert len(scopes) == 1 and next(iter(scopes))
        assert idxs == {0}

    def test_checkup_path_does_not_touch_the_page(self):
        """`fill_fields=False` 一个字都不能写。

        体检是只读的。往里写等于每次体检都在用户账号上留一次痕，
        而且写完页面状态就变了，后面的判据核的是被我们改过的页面。
        """
        page = fake_page(logged_in=True, labels_present=True)
        page.evaluate.return_value = {}
        sub = FeishuSubmitter(tenant="nio")
        touched: list[str] = []
        sub._fill = lambda p, plans: touched.append("filled")

        plan = prep(page, sub=sub, fill_fields=False)

        assert touched == []
        # 而且得真走到了表单 —— 在 drift 那道闸就停下的话这条测试是假绿。
        assert plan.status == "ready", plan.blocker

    def test_normal_prepare_still_fills(self):
        """反向守卫：默认路径必须照旧填。

        上一条那个 flag 要是默认值搞反了，代投会静默交出空表单 ——
        而所有「不提交」的测试都还是绿的。
        """
        page = fake_page(logged_in=True, labels_present=True)
        page.evaluate.return_value = {}
        sub = FeishuSubmitter(tenant="nio")
        touched: list[str] = []
        sub._fill = lambda p, plans: touched.append("filled")

        prep(page, sub=sub)

        assert touched == ["filled"]

    def test_checkup_reports_every_judgement_as_broken_on_a_blank_page(self):
        """空页面上每一条判据都该红。

        体检最容易出的问题是**永远绿** —— 判据查不到东西时静默算过，
        那就成了一个只会说「没事」的检查。这条钉住反方向。
        """
        page = _page_with_visible({})
        page.evaluate.return_value = ""

        rows = FeishuSubmitter(tenant="bytedance").check_selectors(page)

        assert rows, "一条都没查等于没查"
        assert all(not ok for _n, ok, _note in rows), \
            [n for n, ok, _ in rows if ok]

    def test_checkup_names_the_wrong_box_case_not_just_the_text(self):
        """同意框的语义锚点红了，话术要同时指出「可能是判据指错了框」。

        数命中数分不出「找到了」和「找错了」—— 上一轮勾错「没有实习经历」时
        命中数正好是 1，合法。用户照字面去改 CONSENT_TEXT 就修错了地方。
        """
        from jobagent.submitters.feishu import CONSENT_BOX

        page = _page_with_visible({CONSENT_BOX: 1})
        page.locator(CONSENT_BOX).locator("visible=true").first.evaluate.return_value = (
            "没有实习经历"
        )

        rows = FeishuSubmitter(tenant="bytedance").check_selectors(page)
        row = next(r for r in rows if r[0].startswith("CONSENT_TEXT"))

        assert row[1] is False
        assert "CONSENT_BOX" in row[2]

    def test_card_selector_excludes_the_content_wrapper(self):
        """卡片判据末尾那两个下划线是必需的。

        `apply-form-array-card-content__xxx` 也以 `apply-form-array-card` 开头，
        少了 `__` 会把内层 content 也算成一张卡 —— 卡片数直接翻倍，
        scope_index 全部错位。（2026-08-10 实测）
        """
        from jobagent.submitters.feishu import ARRAY_CARD

        assert "apply-form-array-card__" in ARRAY_CARD

    def test_fill_surfaces_the_ambiguity_reason_not_just_the_class_name(self):
        """歧义要出现在字段 note 里。

        只写 `ValueError` 用户没法分辨「页面上有两条同名记录」和「这个字段没找到」，
        而这两件事的处理方式完全不同。
        """
        from jobagent.submitters.base import FieldPlan

        page = _page_with_visible({"#x": 2})
        f = FieldPlan(selector="#x", label="学校名称", value="测试大学")

        self._sub()._fill(page, [f])

        assert f.filled is False
        assert "命中 2 个" in (f.note or "")


# ---- 同意动作：勾错框比没勾上更坏 ----

class TestConsent:
    """隐私政策勾选框。

    老代码把 `input.atsx-checkbox-input` 和 `input.ud__checkbox__input` 用逗号
    并起来取 `.first`，命中的是「没有实习经历」（它默认 checked=True），于是
    `if not is_checked()` 直接跳过，隐私政策那个从头到尾没勾上。（2026-08-10 实测）

    两个风险代价不对称：漏勾只是提交被页面拦下；勾错是**替用户申报了一条不实信息**。
    """

    def _box(self, *, count=1, near="我已阅读并同意隐私政策", checked=False,
             check_raises=None):
        """造一个假勾选框。check() 之后 is_checked 翻成 True，除非指定抛异常。"""
        state = {"checked": checked}
        box = MagicMock()
        box.count.return_value = count
        box.first = box
        box.locator.return_value = box
        box.evaluate.return_value = near
        box.is_checked.side_effect = lambda: state["checked"]

        def do_check(**kw):
            if check_raises:
                raise check_raises
            state["checked"] = True
        box.check.side_effect = do_check

        page = MagicMock()
        page.locator.return_value = box
        return page, box, state

    def _sub(self):
        return FeishuSubmitter(tenant="bytedance")

    def test_consent_checks_the_box_and_returns_no_reason(self):
        page, box, state = self._box()

        assert self._sub()._consent(page) is None
        assert state["checked"] is True

    def test_consent_refuses_when_more_than_one_candidate(self):
        """命中 2 个就停手 —— 这正是老 bug 的形状。"""
        page, _box, state = self._box(count=2)

        why = self._sub()._consent(page)

        assert why and "2 个" in why
        assert state["checked"] is False        # 一个都没动

    def test_consent_refuses_a_box_without_the_privacy_text(self):
        """旁边文案不对就不勾，哪怕选择器只命中一个。

        类名会随组件升级变。命中 1 个不代表命中对了 —— 万一变成了
        「没有实习经历」那个，勾上就是替用户申报不实信息。
        """
        page, _box, state = self._box(near="没有实习经历")

        why = self._sub()._consent(page)

        assert why and "隐私政策" in why
        assert "没有实习经历" in why           # 把读到的原话说出来
        assert state["checked"] is False

    def test_consent_reports_when_check_silently_does_nothing(self):
        """`check()` 没抛不等于状态真变了 —— 勾完要回读一次。"""
        page, box, _state = self._box()
        box.check.side_effect = lambda **kw: None      # 假装成功，状态不变

        assert self._sub()._consent(page) == "勾了但状态没变"

    def test_consent_reports_the_exception_instead_of_swallowing_it(self):
        """勾不上要返回原因。

        老代码是 `except Exception: pass`，指望「让下面的提交去报错」——
        但提交按钮在没勾的时候通常还是 enabled，点下去的报错是页面文案，
        和「我们没勾上」这个事实对不上。
        """
        page, _box, _state = self._box(check_raises=RuntimeError("boom"))

        why = self._sub()._consent(page)

        assert why and "RuntimeError" in why

    def test_consent_selector_targets_only_the_privacy_checkbox(self):
        """判据里不能再带上 ud__checkbox__input —— 那是「没有实习经历」。"""
        from jobagent.submitters.feishu import CONSENT_BOX

        assert "ud__checkbox__input" not in CONSENT_BOX
        assert "atsx-checkbox-input" in CONSENT_BOX


class TestDiscardDoesNotLeakPlaintext:
    """放弃这条路径的落库形态必须和提交一致，否则「放弃」比「提交」漏得多。

    `discard()` 原来用 `f.model_dump()`，把明文身份证连同 selector 一起塞进
    `SubmissionResult.skipped_fields`。腾讯那张表没有身份证字段，**这一份才是
    身份证真正会漏的地方**（feishu.py:114 的 `个人证件`）。

    当时没被发现是因为 CLI 两个调用点都把返回值丢了（`cli.py:847,861` 只调不接），
    没真的入库 —— 但「现在没人接」不是安全边界。
    """

    def _seeded(self):
        from jobagent.submitters.base import (
            PLAN_TTL_SECONDS, SubmissionPlan, mint_token,
        )
        # 这个类要的画像必须带 id_card —— 文件顶上那个 profile fixture 没有，
        # 用它的话身份证那条断言会恒真。
        prof = P.from_dict({
            "identity": {"name": "测试用户", "phone": "13800138000",
                         "email": "test@example.com",
                         "id_card": "110101200001011234"},
            "education": [{"school": "清华大学", "major": "计算机科学与技术",
                           "degree": "硕士", "end": "2027-06", "city": "北京"}],
        })
        sub = FeishuSubmitter(tenant="nio")
        fields = sub._plan_fields(prof)
        assert any(f.label == "个人证件" and f.value for f in fields), \
            "计划里没有身份证字段，这个类测不到东西"
        # 填成功的字段 value 也是满的 —— `_fill` 填完会把值从页面回读一遍。
        for f in fields:
            f.filled = True
        plan = SubmissionPlan(
            job_id="1", source_key="feishu:nio:campus", company="蔚来",
            apply_url=JOB["apply_url"], fields=fields,
            confirm_token=mint_token(), expires_at=time.time() + PLAN_TTL_SECONDS,
        )
        SESSIONS.put(LiveSession(plan, MagicMock(), lambda: None))
        return sub, plan

    def test_id_card_phone_and_email_are_masked(self):
        sub, plan = self._seeded()

        res = sub.discard(plan.confirm_token)

        blob = json.dumps(res.skipped_fields, ensure_ascii=False)
        # 断言真值不出现，而不是「有 * 号」—— 后者在明文和打码值
        # 同时出现时照样成立。
        assert "110101200001011234" not in blob
        assert "13800138000" not in blob
        assert "test@example.com" not in blob
        # 正向确认打码了、不是整条被丢掉；非敏感字段照原样留着，
        # 否则「都打码」和「都丢掉」两种错法不可分。
        assert "11**************34" in blob
        assert "清华大学" in blob

    def test_selector_is_not_exposed(self):
        """for_storage() 的白名单里没有 selector，model_dump() 有。

        选择器不是隐私，但它是 model_dump/for_storage 之间最好认的指纹。
        """
        sub, plan = self._seeded()

        res = sub.discard(plan.confirm_token)

        assert all("selector" not in f for f in res.skipped_fields)
