"""M6 代投层测试 —— 两阶段投递。

这里测的核心不是「能不能填对表」，而是**没经过人工确认能不能提交**。
所以最重要的几条是 execute 的拒绝路径：没 token、过期、重放、页面漂移。
这些是产品承诺（不会背着你投出去）的回归防线，改坏了必须红。

页面用假 locator 模拟：按选择器文案返回命中数，而不是按调用顺序排一串
side_effect。这样将来多加一次探测调用，老测试不会莫名其妙地错位。
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from jobagent import profile as P
from jobagent.submitters.base import SESSIONS, TokenError
from jobagent.submitters.tencent_join import FORM_FIELDS, TencentJoinSubmitter

JOB = {"external_id": "12345", "title": "内容运营（校招）",
       "apply_url": "https://join.qq.com/post.html?pid=12345"}


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
                     "email": "test@example.com", "id_card": "110101200001011234"},
        "education": [{"school": "清华大学", "major": "计算机科学与技术",
                       "degree": "硕士", "end": "2026-06", "city": "北京"}],
    })


@pytest.fixture
def mock_page():
    """给探测函数单测用的简单页面。"""
    page = MagicMock()
    page.locator.return_value.count.return_value = 0
    page.locator.return_value.is_visible.return_value = True
    return page


def fake_page(*, closed=False, need_login=False, outcome="success", values=None):
    """按选择器文案作答的假页面。

    outcome 决定「点了提交之后」页面显示什么：success / duplicate / closed / unknown。
    values 覆盖回读结果，用来模拟确认期间页面被改掉（漂移）。
    """
    state = {"submitted": False}
    filled: dict[str, str] = {}
    overrides = values or {}

    def counts(sel: str) -> int:
        if "已停止" in sel:                       # _is_job_closed
            return int(outcome == "closed") if state["submitted"] else int(closed)
        if "扫码登录" in sel:                     # _need_login
            return int(need_login)
        if "申请成功" in sel:                     # _is_success
            return int(state["submitted"] and outcome == "success")
        if "已申请" in sel:                       # _is_duplicate
            return int(state["submitted"] and outcome == "duplicate")
        return 0

    def locator(sel: str):
        loc = MagicMock()
        loc.count.return_value = counts(sel)
        loc.select_option.side_effect = lambda v, *a, **k: filled.__setitem__(sel, v)
        target = MagicMock()
        target.is_visible.return_value = True
        if "提交申请" in sel:
            target.click.side_effect = lambda *a, **k: state.__setitem__("submitted", True)
        loc.first = target
        return loc

    page = MagicMock()
    page.locator.side_effect = locator
    page.fill.side_effect = lambda sel, val, *a, **k: filled.__setitem__(sel, val)
    page.input_value.side_effect = lambda sel, *a, **k: overrides.get(sel, filled.get(sel, ""))
    page.recorded = filled
    page.state = state
    return page


def patched(page):
    """把 sync_playwright 替成产出指定假页面的工厂。"""
    pw = MagicMock()
    pw.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
    factory = MagicMock()
    factory.return_value.start.return_value = pw
    return factory


SELECTORS = {key: sel for key, _, sel, _ in FORM_FIELDS}


def test_no_one_shot_submit():
    """协议里不能有一步到底的 submit()。

    这条是结构约束的守门测试：谁哪天为了省事加回 submit()，这里就红。
    """
    sub = TencentJoinSubmitter()
    assert not hasattr(sub, "submit")
    assert all(hasattr(sub, m) for m in ("prepare", "execute", "discard"))


def test_submitter_init():
    sub = TencentJoinSubmitter(headless=False, timeout=60.0)
    assert sub.headless is False
    assert sub.timeout == 60000          # 转成毫秒
    assert sub.source_key == "tencent_join"
    assert sub.company == "腾讯"


def test_plan_fields_reads_nested_profile(profile):
    """字段计划必须从嵌套画像里取到值。

    这是修 bug 的回归测试：之前 submitter 读扁平 key，真实画像是嵌套的，
    每个字段都取到 None，表单一个字没填却照样点提交。
    """
    plans = {f.label: f for f in TencentJoinSubmitter()._plan_fields(profile)}

    assert plans["姓名"].value == "测试用户"
    assert plans["姓名"].source == "identity.name"
    assert plans["学校"].value == "清华大学"
    assert plans["学校"].source == "education[0].school"
    assert plans["毕业年份"].value == "2026"     # 从 education[0].end 拆出来
    assert plans["毕业月份"].value == "6"
    assert plans["学历"].action == "select"
    # 画像里没简历 → 标 skip 并说明原因，而不是静默漏掉
    assert plans["简历文件"].action == "skip"
    assert "没有这一项" in (plans["简历文件"].note or "")


def test_plan_skips_missing_resume_file(tmp_path):
    """简历路径写了但文件不在：标 skip 并说清是哪个路径，别静默不传。"""
    ghost = tmp_path / "resume.pdf"
    plans = {f.label: f for f in TencentJoinSubmitter()._plan_fields(
        P.from_dict({"identity": {"name": "甲", "resume_path": str(ghost)}})
    )}
    assert plans["简历文件"].action == "skip"
    assert "不存在" in plans["简历文件"].note
    assert str(ghost) in plans["简历文件"].note

    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4")
    plans2 = {f.label: f for f in TencentJoinSubmitter()._plan_fields(
        P.from_dict({"identity": {"name": "甲", "resume_path": str(real)}})
    )}
    assert plans2["简历文件"].action == "upload"


def test_plan_marks_missing_required():
    """必填字段缺值要显式标出来，让用户先补画像。"""
    thin = P.from_dict({"identity": {"name": "只有名字"}})
    plans = TencentJoinSubmitter()._plan_fields(thin)
    missing = {f.label for f in plans if f.required and not f.value}
    assert {"手机号", "邮箱", "学校", "专业", "学历"} <= missing


def test_sensitive_fields_flagged(profile):
    """身份证、手机、邮箱要标成敏感，展示和入库都得打码。"""
    plans = {f.label: f for f in TencentJoinSubmitter()._plan_fields(profile)}
    assert plans["手机号"].sensitive is True
    assert plans["邮箱"].sensitive is True
    assert P.mask("13800138000") == "138****8000"


def test_prepare_fills_but_does_not_submit(profile):
    """prepare 要把表填好，但绝不能点提交。"""
    page = fake_page()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = TencentJoinSubmitter().prepare(JOB, profile)

    assert plan.status == "ready"
    assert plan.confirm_token                       # 发了 token
    assert plan.expires_at > time.time()
    assert page.state["submitted"] is False         # 关键：没提交
    assert page.recorded[SELECTORS["name"]] == "测试用户"
    assert page.recorded[SELECTORS["school"]] == "清华大学"
    assert len(plan.filled_fields) >= 6


def test_prepare_blocked_when_job_closed(profile):
    """岗位已关闭：不发 token，也不注册会话。"""
    page = fake_page(closed=True)
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = TencentJoinSubmitter().prepare(JOB, profile)

    assert plan.status == "blocked"
    assert "已关闭" in plan.blocker
    assert plan.confirm_token == ""
    assert plan.is_ready is False
    assert SESSIONS._sessions == {}


def test_prepare_blocked_when_login_needed(profile):
    """需要登录：停下来交回给人，不猜、不硬闯。"""
    page = fake_page(need_login=True)
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = TencentJoinSubmitter().prepare(JOB, profile)

    assert plan.status == "blocked"
    assert "需要登录" in plan.blocker
    assert plan.confirm_token == ""
    assert page.state["submitted"] is False


def test_blocked_plan_cannot_be_executed(profile):
    """blocked 计划没 token，execute 无从下手 —— 这就是硬约束的意思。"""
    page = fake_page(closed=True)
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)

    with pytest.raises(TokenError) as exc:
        sub.execute(plan.confirm_token)
    assert exc.value.reason == "unknown"


def test_execute_after_confirm_submits(profile):
    """确认过的计划：execute 才真的点提交。"""
    page = fake_page(outcome="success")
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)
        result = sub.execute(plan.confirm_token)

    assert result.status == "submitted"
    assert result.success is True
    assert page.state["submitted"] is True
    assert result.submitted_at


def test_execute_rejects_unknown_token():
    """伪造/过期进程的 token 直接拒。"""
    with pytest.raises(TokenError) as exc:
        TencentJoinSubmitter().execute("not-a-real-token")
    assert exc.value.reason == "unknown"


def test_execute_rejects_expired_plan(profile):
    """计划过期就不能提交 —— 页面状态早变了，确认不再代表现在。"""
    page = fake_page()
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)
        SESSIONS.peek(plan.confirm_token).expires_at = time.time() - 1
        result = sub.execute(plan.confirm_token)

    assert result.status == "blocked"
    assert result.note == "token_expired"
    assert page.state["submitted"] is False


def test_execute_rejects_drifted_form(profile):
    """确认后表单被改（这里模拟手机号被清空）→ 拒绝提交。

    用户确认的是「这些值填进这些字段」。值变了就等于没确认过。
    """
    page = fake_page(values={SELECTORS["phone"]: ""})
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)
        result = sub.execute(plan.confirm_token)

    assert result.status == "blocked"
    assert result.note == "token_drifted"
    assert "不一致" in result.error
    assert page.state["submitted"] is False


def test_token_is_single_use(profile):
    """一个 token 只能提交一次。重放会被 consumed 挡住。"""
    page = fake_page()
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)
        first = sub.execute(plan.confirm_token)
        second = sub.execute(plan.confirm_token)

    assert first.status == "submitted"
    assert second.status == "blocked"
    assert second.note == "token_consumed"


def test_discard_abandons_without_submitting(profile):
    """用户放弃：关会话、记 abandoned、不提交。"""
    page = fake_page()
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)
        result = sub.discard(plan.confirm_token)

    assert result.status == "abandoned"
    assert page.state["submitted"] is False
    assert SESSIONS.peek(plan.confirm_token) is None


def test_duplicate_and_unknown_outcomes(profile):
    """源站说已投过 / 状态未知，都要如实记，不能报成成功。"""
    for outcome, expected in (("duplicate", "duplicate"), ("unknown", "failed")):
        page = fake_page(outcome=outcome)
        sub = TencentJoinSubmitter()
        with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
            plan = sub.prepare(JOB, profile)
            result = sub.execute(plan.confirm_token)
        assert result.status == expected, outcome
        assert result.success is False


def test_stored_fields_are_masked(profile):
    """落库的字段值要打码，本地库也不存明文手机号/身份证。"""
    page = fake_page()
    sub = TencentJoinSubmitter()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = sub.prepare(JOB, profile)
        result = sub.execute(plan.confirm_token)

    stored = {f["label"]: f["value"] for f in result.filled_fields}
    assert stored["手机号"] == "138****8000"
    assert "13800138000" not in str(result.filled_fields)
    assert stored["姓名"] == "测试用户"          # 非敏感字段照常存


def test_sweep_reclaims_expired_sessions(profile):
    """放弃的确认不能把浏览器泄漏掉，sweep 要能收走。"""
    page = fake_page()
    with patch("jobagent.submitters.tencent_join.sync_playwright", patched(page)):
        plan = TencentJoinSubmitter().prepare(JOB, profile)

    SESSIONS.peek(plan.confirm_token).expires_at = time.time() - 1
    assert SESSIONS.sweep() == 1
    assert SESSIONS.peek(plan.confirm_token) is None


# ---- 页面状态探测 ----
# 站点改版时这几个最先失效，单独测好定位。

def test_is_job_closed(mock_page):
    sub = TencentJoinSubmitter()
    mock_page.locator.return_value.count.return_value = 0
    assert not sub._is_job_closed(mock_page)
    mock_page.locator.return_value.count.return_value = 1
    assert sub._is_job_closed(mock_page)


def test_need_login(mock_page):
    sub = TencentJoinSubmitter()
    mock_page.locator.return_value.count.return_value = 0
    assert not sub._need_login(mock_page)
    mock_page.locator.return_value.count.return_value = 1
    assert sub._need_login(mock_page)


def test_is_success(mock_page):
    sub = TencentJoinSubmitter()
    mock_page.locator.return_value.count.return_value = 0
    assert not sub._is_success(mock_page)
    mock_page.locator.return_value.count.return_value = 1
    assert sub._is_success(mock_page)


def test_is_duplicate(mock_page):
    sub = TencentJoinSubmitter()
    mock_page.locator.return_value.count.return_value = 0
    assert not sub._is_duplicate(mock_page)
    mock_page.locator.return_value.count.return_value = 1
    assert sub._is_duplicate(mock_page)
