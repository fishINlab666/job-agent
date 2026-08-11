"""腾讯投递器的判据体检测试。

体检本身是个「能不能发现判据坏了」的工具，所以它的测试有一条硬要求：
**每条判据都得被单独改坏一次，看它真的红。** 只验全绿等于没验 —— 一个
`return [(name, True, "")]` 的假体检也能让全绿测试通过。

腾讯这边和飞书不同的地方在于判据分两类，能核的程度不一样：

  「必须在」的（FORM_FIELDS、SUBMIT_TEXT）：健康页面上应该命中，可以直接核。
  「必须不在」的（CLOSED_TEXT / SUCCESS_TEXT / DUPLICATE_TEXT）：健康页面上
      本来就是 0 命中，把词改成「阿巴阿巴」照样 0 命中。这类只能从两边夹：
      正例控制（白页上认不认自己的词）+ 反例控制（表单页上别误报）。
      「站点换了文案」这件事在一个还开着的岗位页上核不了，测试也不假装核了。

页面用假 locator 模拟。正例控制那部分的假白页会**真的跑一遍正则**（Python 的
re，语义与 Playwright 的 text=/.../ 在这些用例上一致），否则「白页认不认这个词」
就变成了 mock 自己回答自己，测不出拼错。
"""
import re
from unittest.mock import MagicMock, patch

import pytest

from jobagent import profile as P
from jobagent.submitters import tencent_join as T
from jobagent.submitters.base import SESSIONS
from jobagent.submitters.tencent_join import FORM_FIELDS, TencentJoinSubmitter

JOB = {"external_id": "12345", "title": "内容运营（校招）",
       "apply_url": "https://join.qq.com/post.html?pid=12345"}

SELECTORS = {key: sel for key, _, sel, _ in FORM_FIELDS}
UPLOAD_KEYS = {key for key, _, _, action in FORM_FIELDS if action == "upload"}


@pytest.fixture(autouse=True)
def clean_sessions():
    yield
    for token in list(SESSIONS._sessions):
        SESSIONS.discard(token)


def _scratch_page():
    """假白页：set_content 存正文，locator('text=/a|b/') 真的跑正则。

    这里不能用「count 恒等于 1」的 mock —— 正例控制要测的就是「这个词自己
    匹配得上吗」，mock 恒真的话拼错的词也是绿的。
    """
    state = {"html": ""}
    pg = MagicMock()
    pg.set_content.side_effect = lambda html: state.__setitem__("html", html)

    def locator(sel: str):
        loc = MagicMock()
        m = re.fullmatch(r"text=/(.*)/", sel)
        if not m:
            loc.count.return_value = 0
            return loc
        pattern = m.group(1)

        def count():
            # 无效正则时 Playwright 会抛（实测 text=/+86/ -> SyntaxError），
            # 这里照抛，让被测代码走它的 except 分支。
            return len(re.findall(pattern, state["html"]))

        loc.count.side_effect = count
        return loc

    pg.locator.side_effect = locator
    return pg


def form_page(*, visible=None, submit_n=1, page_text=(), login_btn_texts=("登录",),
              apply_btn=1):
    """健康表单页。也要能撑住 prepare 走一遍（checkup 是从 prepare 进来的）。

    visible: 覆盖某条选择器的可见命中数，默认每条 1（健康）
    submit_n: 「提交申请」按钮命中数
    page_text: 表单页上额外出现的文案，用来模拟判据误报
    login_btn_texts: `button:has-text('登录')` 子串命中到的按钮文案
    apply_btn: 「立即申请」命中数。0 会让 `_need_login` 认为未登录
    """
    vis = dict.fromkeys(SELECTORS, 1)
    vis.update(visible or {})
    counts = {SELECTORS[k]: n for k, n in vis.items()}
    extra = "".join(page_text)

    def locator(sel: str):
        loc = MagicMock()
        n = 0
        if sel in counts:
            n = counts[sel]
        elif sel == f"button:has-text('{T.SUBMIT_TEXT}')":
            n = submit_n
        elif sel == f"button:has-text('{T.LOGIN_BTN_TEXT}')":
            n = len(login_btn_texts)
            loc.all_inner_texts.return_value = list(login_btn_texts)
        elif sel == f"text={T.APPLY_TEXT}":
            n = apply_btn
        elif (m := re.fullmatch(r"text=/(.*)/", sel)):
            n = len(re.findall(m.group(1), extra))
        loc.count.return_value = n
        loc.first.is_visible.return_value = n > 0
        # `.locator("visible=true")` 链式调用：可见数与总数一致（除文件框，
        # 那条被测代码本来就不加 visible 过滤）
        loc.locator.return_value = loc
        return loc

    page = MagicMock()
    page.locator.side_effect = locator
    page.context.new_page.side_effect = _scratch_page
    page.input_value.return_value = ""
    return page


def rows_of(page):
    return {name: (ok, note) for name, ok, note in
            TencentJoinSubmitter().check_selectors(page)}


# ---- 全绿基线 ----

def test_healthy_form_passes_every_judgement():
    rows = rows_of(form_page())
    bad = {n: note for n, (ok, note) in rows.items() if not ok}
    assert not bad, f"健康页面上不该有红的：{bad}"
    # 九个字段 + 提交 + 4 条正例 + 4 条反例 + 登录按钮
    assert len(rows) == len(FORM_FIELDS) + 10


# ---- 「必须在」的判据：逐条改坏 ----

def test_missing_field_selector_goes_red():
    """命中 0：这个字段代投会静默跳过，体检必须点出来。"""
    rows = rows_of(form_page(visible={"phone": 0}))
    ok, note = rows["选择器 手机号"]
    assert not ok
    assert "静默跳过" in note


def test_ambiguous_field_selector_goes_red():
    """命中 2 也是坏的 —— 这条是本次实现的重点。

    `_fill` 走的是 page.fill()，**页面级 API 不 strict**（实测：命中 2 个时
    不报错，写第一个）。所以 `input[placeholder*="手机"]` 同时命中「手机号」
    和「手机验证码」时，手机号会被写进验证码框，而计划里显示 filled=True。
    按 >= 1 判会让这种情况一路绿灯。
    """
    rows = rows_of(form_page(visible={"phone": 2}))
    ok, note = rows["选择器 手机号"]
    assert not ok, "命中 2 个必须红，不然写错框也是绿的"
    assert "歧义" in note


def test_every_form_field_is_checked_individually():
    """九个字段逐个改坏，每次只有它自己红。

    防的是「体检只核了前几条」或者「一条坏了就整组红」。
    """
    for key, label, _sel, _action in FORM_FIELDS:
        rows = rows_of(form_page(visible={key: 0}))
        reds = [n for n, (ok, _) in rows.items() if not ok]
        assert reds == [f"选择器 {label}"], f"{label} 坏掉时红的是 {reds}"


def test_submit_button_count_must_be_exactly_one():
    """execute 点的是 .first，命中 0 点不到，命中 2 可能点错按钮。"""
    assert not rows_of(form_page(submit_n=0))[f"SUBMIT_TEXT「{T.SUBMIT_TEXT}」"][0]
    assert not rows_of(form_page(submit_n=2))[f"SUBMIT_TEXT「{T.SUBMIT_TEXT}」"][0]
    assert rows_of(form_page(submit_n=1))[f"SUBMIT_TEXT「{T.SUBMIT_TEXT}」"][0]


# ---- 「必须不在」的判据：正例控制 ----

@pytest.mark.parametrize("name", ["CLOSED_TEXT", "LOGIN_TEXT",
                                  "SUCCESS_TEXT", "DUPLICATE_TEXT"])
def test_wrong_word_cannot_be_caught_on_a_healthy_page(monkeypatch, name):
    """**这是记录盲区的测试，不是记录能力的。**

    把 `CLOSED_TEXT` 整组换成 `("阿巴阿巴",)`，体检**全绿**。因为：
      正例控制把「阿巴阿巴」写进白页，然后问「认得吗」—— 认得，自己写的。
      反例控制在健康表单页上问「误报吗」—— 不误报，页面上没这四个字。

    所以「站点把『已停止招聘』改成了『招聘已截止』」这件事，在一个**还开着**的
    岗位页上没有任何办法知道。要发现它得有一个已关闭的岗位页做对照。

    这条测试的作用是把这个盲区钉住：写成断言，别人读体检输出时才不会把
    「19 条全绿」当成「判据还认得页面」。哪天真做了对照组（比如存一份已关闭
    岗位的 HTML 当 fixture），这条会红，那时候删掉它。
    """
    monkeypatch.setattr(T, name, ("阿巴阿巴",))
    rows = rows_of(form_page())
    assert rows[f"{name} 认得自己的词"][0]
    assert rows[f"{name} 不误报"][0]
    assert not [n for n, (ok, _) in rows.items() if not ok], "全绿正是问题所在"
    # 输出里必须写清这层限制，不然绿得像是核过了
    assert "不代表站点还在用这些词" in rows[f"{name} 认得自己的词"][1]


@pytest.mark.parametrize("name", ["CLOSED_TEXT", "LOGIN_TEXT",
                                  "SUCCESS_TEXT", "DUPLICATE_TEXT"])
def test_regex_metachar_in_word_goes_red(monkeypatch, name):
    """含正则元字符的词要被抓出来。

    `_any_text` 是拼进 `text=/.../` 的，词里带 `+` 或 `[` 会让整条正则失效
    （实测 Playwright 对 text=/+86/ 抛 SyntaxError）。这时**整组判据全瞎**，
    不只是这一个词 —— 已停止/已下线/已结束一个都认不出来。
    """
    monkeypatch.setattr(T, name, ("+86", "已停止"))
    rows = rows_of(form_page())
    ok, note = rows[f"{name} 认得自己的词"]
    assert not ok
    assert "+86" in note


@pytest.mark.parametrize("name", ["CLOSED_TEXT", "LOGIN_TEXT",
                                  "SUCCESS_TEXT", "DUPLICATE_TEXT"])
def test_empty_word_list_goes_red(monkeypatch, name):
    """空元组：`text=//` 会命中一切或什么都不命中，两种都是坏的。"""
    monkeypatch.setattr(T, name, ())
    ok, note = rows_of(form_page())[f"{name} 认得自己的词"]
    assert not ok
    assert "永远不触发" in note


def test_each_word_is_checked_alone():
    """逐个词写进白页，而不是整组一起写。

    整组一起写的话，只要有一个词没拼错整组就命中，另外两个拼错也是绿的 ——
    查的就变成了「至少一个词是对的」。

    这里断言的是**每次写进去的正文里只有一个词**，不是 set_content 被调了几次。
    只数次数抓不到「每次都把整组写进去」——次数一样，内容不一样。
    （这条就是这么发现的：先写的版本只数次数，突变测试里它是绿的。）
    """
    page = form_page()
    pages = []

    def track():
        pages.append(_scratch_page())
        return pages[-1]

    page.context.new_page.side_effect = track
    TencentJoinSubmitter().check_selectors(page)

    groups = (T.CLOSED_TEXT, T.LOGIN_TEXT, T.SUCCESS_TEXT, T.DUPLICATE_TEXT)
    all_words = [w for g in groups for w in g]
    written = [c.args[0] for p in pages for c in p.set_content.call_args_list]
    assert len(written) == len(all_words), "每个词得单独写一次白页"
    for html, group in zip(written, [g for g in groups for _ in g]):
        hits = [w for w in group if w in html]
        assert len(hits) == 1, f"这一页里有 {len(hits)} 个词，应该只有 1 个：{html}"
    for p in pages:
        assert p.close.called, "白页读完要关掉"


# ---- 「必须不在」的判据：反例控制 ----

def test_overbroad_closed_text_goes_red():
    """判据在健康表单页上误报 —— 能投的岗位会被判成已关闭。"""
    ok, note = rows_of(form_page(page_text=("本次活动已结束",)))["CLOSED_TEXT 不误报"]
    assert not ok
    assert "判据太宽" in note


def test_overbroad_success_text_is_the_worst_one():
    """`_is_success` 误报最贵：没投上却记成 submitted，还占掉一个名额。

    execute 里 `_is_success` 是第一个判的，它误报会盖掉后面所有分支。
    """
    ok, note = rows_of(form_page(page_text=("查看已提交的材料",)))["SUCCESS_TEXT 不误报"]
    assert not ok
    assert "submitted" in note


def test_overbroad_duplicate_text_goes_red():
    ok, _ = rows_of(form_page(page_text=("我的已申请职位",)))["DUPLICATE_TEXT 不误报"]
    assert not ok


def test_overbroad_login_text_goes_red():
    ok, _ = rows_of(form_page(page_text=("切换微信登录",)))["LOGIN_TEXT 不误报"]
    assert not ok


def test_logout_button_trips_the_login_judgement():
    """`button:has-text('登录')` 会连「退出登录」一起命中。

    这条不是假想：`has-text` 是子串匹配，而「退出登录」这个按钮恰恰是
    **登录成功后**才出现的。真撞上的话，登录态越正常越投不出去 ——
    `_need_login` 返回 True，prepare 直接 blocked。
    """
    row = f"LOGIN_BTN_TEXT「{T.LOGIN_BTN_TEXT}」不误报"
    ok, note = rows_of(form_page(login_btn_texts=("退出登录",)))[row]
    assert not ok
    assert "退出登录" in note and "已登录" in note
    # 只有正牌「登录」按钮时不该红
    assert rows_of(form_page(login_btn_texts=("登录",)))[row][0]


# ---- checkup 的整体行为 ----

def _patched(page):
    pw = MagicMock()
    pw.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
    factory = MagicMock()
    factory.return_value.start.return_value = pw
    return factory


def test_checkup_writes_nothing_to_the_page():
    """体检不能在对方系统的表单里留下任何输入。这是它敢对着真岗位跑的前提。

    断言的是 **`_fill` 一次都没被调**，不是「page.fill 没被调」。后者用空画像
    跑必然成立 —— 没值可填 —— 于是 `fill_fields=False` 传没传都是绿的，
    这条测试就等于没测。（这条就是这么发现的：突变把 `fill_fields=False`
    去掉之后，只看 page.fill 的版本照样全绿。）

    所以这里用一个真画像 + 拦 `_fill`：它被调到就是失败，与画像里有没有值无关。
    """
    prof = P.from_dict({"identity": {"name": "测试用户", "phone": "13800138000",
                                     "email": "t@example.com"}})
    page = form_page()
    page.url = JOB["apply_url"]
    sub = TencentJoinSubmitter()
    calls = []
    real_prepare = sub.prepare
    # 让 checkup 拿到的画像是有值的那份：它内部写死了 from_dict({})，
    # 这里换掉，好证明「不填」是 fill_fields 挡的，不是画像空。
    sub.prepare = lambda job, _p, fill_fields=True: real_prepare(
        job, prof, fill_fields=fill_fields)
    with patch("jobagent.submitters.tencent_join.sync_playwright", _patched(page)), \
            patch.object(TencentJoinSubmitter, "_fill",
                         lambda self, pg, plans: calls.append(plans)):
        rows = sub.checkup(JOB)

    assert not calls, "体检调了 _fill —— 会在对方表单里留下输入"
    assert not page.fill.called, "体检不该填任何字段"
    assert not page.set_input_files.called, "体检不该上传简历"
    submit = page.locator(f"button:has-text('{T.SUBMIT_TEXT}')")
    assert not submit.first.click.called, "体检绝对不能点提交"
    assert rows[0][0] == "走到表单" and rows[0][1] is True


def test_checkup_reports_blocker_when_prepare_cannot_reach_form():
    """走不到表单就只回一行，不去核一张登录页上的选择器。

    在登录页上核 FORM_FIELDS 会得到九条「命中 0」—— 那是九条假红，
    真正的问题只有一条：没登录。
    """
    # 未登录页：没有「立即申请」，但有「登录」按钮 —— `_need_login` 的第二条路
    page = form_page(apply_btn=0, visible=dict.fromkeys(SELECTORS, 0))
    page.url = JOB["apply_url"]
    with patch("jobagent.submitters.tencent_join.sync_playwright", _patched(page)):
        rows = TencentJoinSubmitter(headless=True).checkup(JOB)
    assert len(rows) == 1
    assert rows[0][0] == "走到表单" and rows[0][1] is False
    assert "登录" in rows[0][2]


def test_checkup_releases_the_session():
    """体检开的浏览器要关掉，token 不能留在会话表里。

    留着的话下一次 prepare 会撞上一个别人的活会话，而那个会话的页面
    停在一张体检用的表单上。
    """
    page = form_page()
    page.url = JOB["apply_url"]
    with patch("jobagent.submitters.tencent_join.sync_playwright", _patched(page)):
        TencentJoinSubmitter().checkup(JOB)
    assert not SESSIONS._sessions, "体检结束后会话表必须是空的"


# ---- CLI 层：全绿时那句限制说明 ----
# 打在 CLI 而不是函数层，因为它是**汇总话术**：函数层返回的行里带着 caveat，
# 但用户读的是最后那两行。「20 条判据全部有效」如果不带限制，会被读成
# 「站点没改过文案」—— 而那正是这批判据核不出来的东西。

@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """把 CLI 的默认库指到临时文件，登好腾讯源和一个岗位。

    CLI 里是 `db.connect()` 不带参数，取模块级 DB_PATH，不改会打到真库。
    """
    from jobagent import db
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.connect(path)
    db.init(conn)
    db.register_source(conn, "tencent_join", "腾讯", "tencent_join",
                       "https://join.qq.com/post.html")
    conn.execute(
        """INSERT INTO jobs (external_id, source_key, company, title, apply_url,
           fingerprint, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("12345", "tencent_join", "腾讯", "内容运营（校招）",
         "https://join.qq.com/post.html?pid=12345", "fp1",
         "2026-08-11T00:00:00", "2026-08-11T00:00:00"),
    )
    conn.commit()
    yield conn
    conn.close()


def _run_cli(page):
    from typer.testing import CliRunner
    from jobagent import cli
    page.url = JOB["apply_url"]
    with patch("jobagent.submitters.tencent_join.sync_playwright", _patched(page)):
        return CliRunner().invoke(cli.app, ["checkup", "12345"])


def test_cli_green_summary_states_what_it_could_not_check(cli_db):
    """全绿时必须说清楚「哪几条只是自身没写坏」。

    没有这一句，`checkup` 变成一个让人放心的假信号：4 条判据在健康页面上
    永远绿，而它们恰好是认「岗位已关闭」「提交成功」「重复投递」的那几条 ——
    投递结果记错账全靠它们。
    """
    r = _run_cli(form_page())
    assert r.exit_code == 0
    assert "20 条判据全部有效" in r.output
    assert "证明不了站点还在用这些文案" in r.output
    assert "4 条" in r.output


def test_cli_red_hint_names_the_right_module(cli_db):
    """失效提示要指向 tencent_join.py，不是硬编码的 feishu.py。

    指错文件的提示比没有提示更费时间 —— 人会先去 feishu.py 找一圈。
    """
    r = _run_cli(form_page(visible={"degree": 0}))
    assert r.exit_code == 1
    assert "tencent_join.py" in r.output
    assert "feishu.py" not in r.output


def test_cli_omits_caveat_when_rows_have_none(cli_db):
    """没有 caveat 的投递器不该被塞进这句话。

    这句是按行内容触发的（note 里有「不代表」），不是按投递器写死的 ——
    飞书那套判据全部是「必须在」型，在健康页面上核得动，不适用这句限制。
    """
    plain = [("走到表单", True, ""), ("选择器 姓名", True, "可见命中 1")]
    with patch.object(TencentJoinSubmitter, "checkup", lambda self, job: plain):
        r = _run_cli(form_page())
    assert r.exit_code == 0
    assert "2 条判据全部有效" in r.output
    assert "证明不了" not in r.output


def test_prepare_still_fills_by_default():
    """`fill_fields` 默认 True —— 加参数不能顺手把代投改成不填表。

    这条是给上面那堆 `fill_fields=False` 断言配的对照：不加它的话，把默认值
    写成 False 也能让「体检不填表」全绿，而代投从此静默交空表。
    """
    prof = P.from_dict({"identity": {"name": "测试用户", "phone": "13800138000"}})
    page = form_page()
    page.url = JOB["apply_url"]
    page.input_value.return_value = ""
    with patch("jobagent.submitters.tencent_join.sync_playwright", _patched(page)):
        TencentJoinSubmitter().prepare(JOB, prof)
    assert page.fill.called, "默认路径必须还是会填表"
