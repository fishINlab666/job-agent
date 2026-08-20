"""腾讯 join.qq.com 投递器 —— 两阶段版。

原来这里是一个 submit()：打开页面、填表、点提交，一气呵成。问题是提交
不可逆——点下去对方系统里就多一条记录，撤不回来。所以现在拆成两半，
中间必须夹一次人工确认：

    prepare(job, profile)   打开岗位页 → 查是否已关闭 → 点「立即申请」
                            → 查登录态 → 逐字段填表 → 截图 → 返回计划。
                            **停在提交按钮前，不点。**
    execute(confirm_token)  把页面上的字段值读回来重算摘要，和用户确认
                            时一致才点「提交申请」。

两阶段之间浏览器不关。这点是有意的：如果 execute 时重开页面重新填一遍，
用户确认过的内容和最终提交的内容就成了两回事。

页面流程（人工观察 join.qq.com 得到，站点改版时这里最先坏）：
1. 访问 https://join.qq.com/post.html?pid={postId}
2. 点「立即申请」
3. 未登录会出现扫码/手机号登录 —— prepare 到此为止，交回给人
4. 填基本信息 + 上传简历（PDF/DOC/DOCX，5MB 内）
5. 点「提交申请」  ← 只有 execute 会走到这一步
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from jobagent.profile import FormProfile

from .base import (
    PLAN_TTL_SECONDS,
    SESSIONS,
    FieldPlan,
    LiveSession,
    SubmissionPlan,
    SubmissionResult,
    TokenError,
    mint_token,
)

POST_URL = "https://join.qq.com/post.html?pid="

# 表单字段表：(画像字段, 页面标签, 选择器, 动作)
# 抽成数据是为了 prepare 能逐字段生成计划给用户看，而不是埋在一串 if 里。
FORM_FIELDS: list[tuple[str, str, str, str]] = [
    ("name",        "姓名",     'input[placeholder*="姓名"]',     "fill"),
    ("phone",       "手机号",   'input[placeholder*="手机"]',     "fill"),
    ("email",       "邮箱",     'input[placeholder*="邮箱"]',     "fill"),
    ("school",      "学校",     'input[placeholder*="学校"]',     "fill"),
    ("major",       "专业",     'input[placeholder*="专业"]',     "fill"),
    ("degree",      "学历",     'select[placeholder*="学历"]',    "select"),
    ("grad_year",   "毕业年份", 'input[placeholder*="毕业年份"]', "fill"),
    ("grad_month",  "毕业月份", 'input[placeholder*="毕业月份"]', "fill"),
    ("resume_path", "简历文件", 'input[type="file"]',             "upload"),
]

# 页面上标了必填的字段。这里少了值，prepare 会在计划里高亮出来，
# 让用户先去补画像，而不是填半张表就提交上去。
REQUIRED_FIELDS = {"name", "phone", "email", "school", "major", "degree"}

# ---- 靠文案认页面的那几条判据 ----
# 抽成常量而不是留在各自的 locator 里，是为了让 `checkup` 能核**同一个**字符串。
# 体检自己抄一份的话，它核的是它自己那份，两边可以静默分叉 —— 而分叉正是体检
# 要抓的东西。所以这里是 checkup 有意义的前提，不是顺手整理。
#
# 判据坏掉的方式全是静默的，而且方向各不相同：
#   CLOSED_TEXT   认不出「已关闭」→ 照常填表提交，投给一个已下线的岗位
#   LOGIN_TEXT    认不出「要登录」→ 在登录页上填表，字段全部落空
#   SUCCESS_TEXT  认不出「成功」→ 明明投上了却记成 failed，占用额度算不准
#   DUPLICATE_TEXT 认不出「投过了」→ 同上，而且会诱导用户再投一次
#   APPLY_TEXT    找不到「立即申请」→ prepare 到不了表单
#   SUBMIT_TEXT   找不到「提交申请」→ execute 点不下去（这条不静默，会抛）
CLOSED_TEXT = ("已停止", "已下线", "已结束")
LOGIN_TEXT = ("扫码登录", "手机号登录", "微信登录")
SUCCESS_TEXT = ("申请成功", "提交成功", "已提交")
DUPLICATE_TEXT = ("已申请", "重复投递", "已经投递")
APPLY_TEXT = "立即申请"
SUBMIT_TEXT = "提交申请"
LOGIN_BTN_TEXT = "登录"


def _any_text(words: tuple[str, ...]) -> str:
    """把一组词拼成 Playwright 的正则文本选择器。

    只有一处构造，`_is_*` 和 `checkup` 共用 —— 拼法不同（比如少个竖线）
    会让两边查的不是一回事。
    """
    return f"text=/{'|'.join(words)}/"

class TencentJoinSubmitter:
    source_key = "tencent_join"
    company = "腾讯"
    system = "tencent_join"     # 自建，只有一个租户，构造函数不收 tenant

    def __init__(
        self,
        headless: bool = True,
        timeout: float = 30.0,
        user_data_dir: str | None = None,
    ) -> None:
        """
        Args:
            headless: 是否无头模式。确认环节要人看，实际用时建议 False
            timeout: 页面操作超时（秒）
            user_data_dir: 浏览器用户数据目录，持久化登录态用
        """
        self.headless = headless
        self.timeout = timeout * 1000  # Playwright 用毫秒
        self.user_data_dir = user_data_dir

    # ---------- 阶段一：填好但不提交 ----------

    def prepare(
        self, job: dict, profile: FormProfile, *, fill_fields: bool = True
    ) -> SubmissionPlan:
        """走到提交按钮前停下，返回逐字段计划 + confirm_token。

        `fill_fields=False` 走到表单就停，一个字都不写。`checkup()` 用它 ——
        体检不该在对方系统的表单里留下任何输入。传空画像也能达到同样效果，
        但那是「恰好没值」，这个参数是「明确不写」，后者才能挡住以后有人给
        这个投递器加固定值（像 feishu 的 FIXED_VALUES 那样）之后体检开始写字。
        """
        job_id = str(job.get("external_id") or job.get("id") or "")
        plan = SubmissionPlan(
            job_id=job_id,
            source_key=self.source_key,
            company=self.company,
            title=str(job.get("title") or ""),
            apply_url=str(job.get("apply_url") or f"{POST_URL}{job_id}"),
        )

        pw = sync_playwright().start()
        try:
            page, closer = self._launch(pw)
        except Exception as exc:
            pw.stop()
            return self._blocked(plan, f"浏览器启动失败: {exc}")

        try:
            page.goto(plan.apply_url, timeout=self.timeout)
            page.wait_for_load_state("networkidle", timeout=self.timeout)
            shot = self._shot(page, job_id, "prepare")
            plan.screenshot_path = shot

            if self._is_job_closed(page):
                closer()
                return self._blocked(plan, "岗位已关闭", shot)

            # 先检查登录态，未登录时页面上没有"立即申请"按钮
            if self._need_login(page):
                if self.headless:
                    # 无头模式下无法手动登录，直接报错
                    closer()
                    return self._blocked(
                        plan,
                        "需要登录。请先用 --no-headless 手动登录一次（配合 user_data_dir "
                        "持久化），再重新 prepare",
                        self._shot(page, job_id, "login"),
                    )
                else:
                    # 有头模式下等待用户手动登录，最多等180秒
                    print("检测到未登录，请在浏览器中完成登录...", flush=True)
                    try:
                        page.wait_for_selector(f"text={APPLY_TEXT}", timeout=180000)
                        print("登录成功，继续填表", flush=True)
                    except PlaywrightTimeout:
                        closer()
                        return self._blocked(
                            plan,
                            "等待登录超时（180秒）",
                            self._shot(page, job_id, "login_timeout"),
                        )

            apply_btn = page.locator(f"text={APPLY_TEXT}").first
            if not apply_btn.is_visible(timeout=10000):
                closer()
                return self._blocked(plan, "未找到申请按钮，页面结构可能已变", shot)
            apply_btn.click()
            page.wait_for_timeout(2000)

            # 点击后可能还会弹登录框
            if self._need_login(page):
                closer()
                return self._blocked(
                    plan,
                    "需要登录。请先用 --headed 手动登录一次（配合 user_data_dir "
                    "持久化），再重新 prepare",
                    self._shot(page, job_id, "login"),
                )

            plan.fields = self._plan_fields(profile)
            if fill_fields:
                self._fill(page, plan.fields)
            plan.screenshot_path = self._shot(page, job_id, "prefilled")

            plan.confirm_token = mint_token()
            plan.expires_at = time.time() + PLAN_TTL_SECONDS
            SESSIONS.put(LiveSession(plan, page, closer))
            return plan

        except PlaywrightTimeout as exc:
            closer()
            return self._blocked(plan, f"操作超时: {exc}")
        except Exception as exc:
            closer()
            return self._blocked(plan, f"未知错误: {exc}")


    # ---------- 阶段二：确认之后才点提交 ----------

    def execute(self, confirm_token: str) -> SubmissionResult:
        """校验 token 与字段摘要，然后点「提交申请」。"""
        session = SESSIONS.peek(confirm_token)
        if session is None:
            SESSIONS.take(confirm_token)  # 抛 unknown，错误话术只在一处

        plan = session.plan
        page = session.page

        # 把页面上现在的值读回来重算摘要。用户确认的是「这些值在这些字段里」，
        # 中间被 JS 重置、清空、改写过，就不算确认过。
        try:
            live_digest = self._readback_digest(page, plan)
        except Exception:
            live_digest = "__unreadable__"

        try:
            SESSIONS.take(confirm_token, expect_digest=live_digest)
        except TokenError as exc:
            if exc.reason in ("expired", "drifted"):
                SESSIONS.discard(confirm_token)
            return SubmissionResult(
                status="blocked",
                job_id=plan.job_id,
                company=self.company,
                error=str(exc),
                screenshot_path=plan.screenshot_path,
                note=f"token_{exc.reason}",
            )

        try:
            page.locator(f"button:has-text('{SUBMIT_TEXT}')").first.click()
            page.wait_for_timeout(3000)
            shot = self._shot(page, plan.job_id, "submitted")

            if self._is_success(page):
                return self._result(plan, "submitted", shot,
                                    submitted_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            if self._is_duplicate(page):
                return self._result(plan, "duplicate", shot, error="源站提示已投递过")
            if self._is_job_closed(page):
                return self._result(plan, "closed", shot, error="提交时岗位已关闭")
            return self._result(plan, "failed", shot, error="提交后状态未知")

        except PlaywrightTimeout as exc:
            return self._result(plan, "failed", plan.screenshot_path,
                                error=f"提交超时: {exc}")
        except Exception as exc:
            return self._result(plan, "failed", plan.screenshot_path,
                                error=f"提交失败: {exc}")
        finally:
            session.close()

    def discard(self, confirm_token: str) -> SubmissionResult:
        """用户在确认环节放弃。关掉浏览器，记一条 abandoned。"""
        session = SESSIONS.peek(confirm_token)
        job_id = session.plan.job_id if session else ""
        shot = session.plan.screenshot_path if session else None
        fields = session.plan.fields if session else []
        SESSIONS.discard(confirm_token)
        return SubmissionResult(
            status="abandoned",
            job_id=job_id,
            company=self.company,
            screenshot_path=shot,
            # 必须是 for_storage() 而不是 model_dump()：后者把明文手机号/邮箱连同
            # selector 一起吐出来（`_fill` 填完还会从页面把值回读一遍，所以填成功的
            # 字段 value 也是满的）。放弃这条路径和 `_result()` 走的是同一个
            # SubmissionResult，落库形态必须一致，否则「放弃」反而比「提交」漏得多。
            # 腾讯这张表没有身份证字段，飞书那边有（feishu.py:114），同一个毛病在
            # 那边漏的是身份证 —— 所以两边一起改。
            skipped_fields=[f.for_storage() for f in fields],
            note="用户在确认环节放弃",
        )


    # ---------- 字段计划与填写 ----------

    def _plan_fields(self, profile: FormProfile) -> list[FieldPlan]:
        """把画像翻译成逐字段计划。纯函数，不碰页面，方便单测。"""
        plans: list[FieldPlan] = []
        for key, label, selector, action in FORM_FIELDS:
            value = profile.get(key)
            pf = profile.field(key)
            source = profile.source_of(key)
            sensitive = bool(pf and pf.sensitive)

            if not value:
                plans.append(FieldPlan(
                    selector=selector, label=label, value=None, source=source,
                    required=key in REQUIRED_FIELDS, action="skip",
                    sensitive=sensitive,
                    note="画像里没有这一项，将留空" + (
                        "（页面必填，需要你手动补）" if key in REQUIRED_FIELDS else ""
                    ),
                ))
                continue

            if action == "upload" and not Path(value).exists():
                plans.append(FieldPlan(
                    selector=selector, label=label, value=value, source=source,
                    required=False, action="skip", sensitive=sensitive,
                    note=f"简历文件不存在：{value}",
                ))
                continue

            plans.append(FieldPlan(
                selector=selector, label=label, value=value, source=source,
                required=key in REQUIRED_FIELDS, action=action,
                sensitive=sensitive,
            ))
        return plans

    def _fill(self, page: Page, plans: list[FieldPlan]) -> None:
        """按计划填页面。单个字段失败不中断——用户在确认界面能看到谁没填上。"""
        for f in plans:
            if f.action == "skip" or not f.value:
                continue
            try:
                if f.action == "fill":
                    page.fill(f.selector, str(f.value))
                elif f.action == "select":
                    page.locator(f.selector).select_option(str(f.value))
                elif f.action == "upload":
                    page.set_input_files(f.selector, str(f.value))
                    page.wait_for_timeout(2000)
                f.filled = True
            except Exception as exc:
                f.filled = False
                f.note = f"填写失败：{type(exc).__name__}，需人工补"


    def _readback_digest(self, page: Page, plan: SubmissionPlan) -> str:
        """把页面上**现在**的值读回来，按同样规则算摘要。

        不是拿 plan.digest() 和自己比（那永远相等，等于没校验）。
        读回来才能发现：确认期间表单被 JS 重置了、值被改写了、页面刷新了。
        文件上传读不回来，跳过；skip 的字段本来就没值，保持原样。
        """
        snapshot = plan.model_copy(deep=True)
        for f in snapshot.fields:
            if f.action in ("skip", "upload") or not f.filled:
                continue
            try:
                f.value = page.input_value(f.selector, timeout=3000) or None
            except Exception:
                f.value = "__unreadable__"
        return snapshot.digest()

    # ---------- 浏览器与截图 ----------

    def _launch(self, pw) -> tuple[Page, Callable[[], None]]:
        """开浏览器，返回 page 和收尾函数。

        用 start()/stop() 而不是 with sync_playwright()：两阶段之间浏览器
        得活着，with 一出块就把页面关了。
        """
        browser = ctx = None
        if self.user_data_dir:
            # 持久化模式：保存 cookie、localStorage，登录一次能用很久
            ctx = pw.chromium.launch_persistent_context(
                self.user_data_dir, headless=self.headless
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            browser = pw.chromium.launch(headless=self.headless)
            ctx = browser.new_context()
            page = ctx.new_page()

        def closer() -> None:
            for closing in (ctx, browser, pw):
                if closing is None:
                    continue
                try:
                    closing.close() if closing is not pw else pw.stop()
                except Exception:
                    pass

        return page, closer

    def _shot(self, page: Page, job_id: str, stage: str) -> str:
        """分阶段存截图。prepare 和 execute 各存一张，出问题能对照着看。"""
        d = Path("screenshots")
        d.mkdir(exist_ok=True)
        path = d / f"{stage}_{job_id}_{int(time.time())}.png"
        try:
            page.screenshot(path=str(path))
        except Exception:
            return ""
        return str(path)


    # ---------- 结果构造 ----------

    @staticmethod
    def _blocked(
        plan: SubmissionPlan, reason: str, shot: str | None = None
    ) -> SubmissionPlan:
        """标成 blocked。不发 token —— 没有 token 就调不动 execute。"""
        plan.status = "blocked"
        plan.blocker = reason
        plan.confirm_token = ""
        if shot:
            plan.screenshot_path = shot
        return plan

    def _result(
        self,
        plan: SubmissionPlan,
        status: str,
        shot: str | None,
        error: str | None = None,
        submitted_at: str | None = None,
    ) -> SubmissionResult:
        """带上填了什么/没填什么，写进 applications 表用。敏感值入库前打码。"""
        return SubmissionResult(
            status=status,  # type: ignore[arg-type]
            job_id=plan.job_id,
            company=self.company,
            error=error,
            screenshot_path=shot or plan.screenshot_path,
            submitted_at=submitted_at,
            filled_fields=[f.for_storage() for f in plan.filled_fields],
            skipped_fields=[f.for_storage() for f in plan.skipped_fields],
        )

    # ---------- 页面状态探测 ----------
    # 这几个是站点改版时最先失效的地方，文案一变就全瞎。
    # 保持独立小函数，方便单测和快速替换。

    def _is_job_closed(self, page: Page) -> bool:
        """岗位是否已关闭。常见文案：该岗位已停止招聘 / 岗位已下线。"""
        return page.locator(_any_text(CLOSED_TEXT)).count() > 0

    def _need_login(self, page: Page) -> bool:
        """是否需要登录。检测登录相关元素或缺少"立即申请"按钮。"""
        # 方法1：页面上有明确的登录入口文字
        if page.locator(_any_text(LOGIN_TEXT)).count() > 0:
            return True
        # 方法2：页面顶部有"登录"按钮且没有"立即申请"按钮
        has_login_btn = page.locator(
            f"button:has-text('{LOGIN_BTN_TEXT}')").count() > 0
        has_apply_btn = page.locator(f"text={APPLY_TEXT}").count() > 0
        return has_login_btn and not has_apply_btn

    def _is_success(self, page: Page) -> bool:
        """提交是否成功。成功文案：申请成功 / 提交成功 / 已提交。"""
        return page.locator(_any_text(SUCCESS_TEXT)).count() > 0

    def _is_duplicate(self, page: Page) -> bool:
        """是否重复投递。"""
        return page.locator(_any_text(DUPLICATE_TEXT)).count() > 0

    # ---------- 判据体检 ----------

    def check_selectors(self, page: Page) -> list[tuple[str, bool, str]]:
        """逐条核这个模块里的字符串级判据。返回 [(名字, 通过, 说明)]。

        **在健康页面上能核什么、不能核什么** —— 这是这个方法和 feishu 那个最大的
        区别，写清楚免得读的人以为绿了就等于判据还认得页面：

        「必须在」的判据可以直接核：`FORM_FIELDS` 的 9 条选择器、`SUBMIT_TEXT`。
        它们在表单页上应该命中，命中数不对就是坏了。

        「必须不在」的判据核不了。`CLOSED_TEXT` / `SUCCESS_TEXT` / `DUPLICATE_TEXT`
        只在异常页面上出现，健康表单页上本来就是 0 命中。把 `CLOSED_TEXT` 改成
        `("阿巴阿巴",)` 照样 0 命中 —— 那是一条**永远绿的检查**，比没有更糟，
        因为它给人一种查过了的错觉。

        所以这三条改成从两边夹：
          正例控制：另开一张白页，逐个词写进去，看 `_any_text` 认不认自己那几个
                    词。这能抓到拼错、抓到元字符（`已停止(招聘)` 里的括号会被
                    当成正则分组）、抓到空元组。抓不到「站点换了文案」。
          反例控制：在**这张健康表单页**上，这几条判据应该是 0 命中。不是 0 就是
                    判据太宽 —— 它会在正常页面上误报。`_is_success` 误报的后果
                    是没投上却记成 submitted，`_need_login` 误报的后果是能投的
                    岗位被拦在门外。

        「站点把『已停止招聘』改成了别的说法」这件事，在一个还开着的岗位页上
        没有任何办法知道。这条限制写在输出里（见 note），不假装核过了。

        只读：不填任何值，不点提交。正例控制开的白页是 about:blank，与目标站
        无关，读完就关。
        """
        out: list[tuple[str, bool, str]] = []

        def add(name: str, ok: bool, note: str = "") -> None:
            out.append((name, ok, note))

        # 1) 表单字段选择器。命中数必须是 1。
        #    这条是 == 而不是 >= 1，因为 `_fill` 走的是 page.fill()/page.
        #    set_input_files()，**页面级 API 不是 strict 的**：选择器命中 2 个
        #    时它不报错，直接写第一个。`input[placeholder*="手机"]` 同时命中
        #    「手机号」和「手机验证码」的话，手机号会被写进验证码框，而计划里
        #    这个字段显示 filled=True。成功地做错事，比报错难发现。
        for key, label, selector, action in FORM_FIELDS:
            try:
                if action == "upload":
                    # file input 常是隐藏的，set_input_files 不要求可见，
                    # 所以这条按总命中数算，不加 visible 过滤。
                    n = page.locator(selector).count()
                    scope = "命中"
                else:
                    n = page.locator(selector).locator("visible=true").count()
                    scope = "可见命中"
            except Exception as exc:
                add(f"选择器 {label}", False, str(exc).split("\n")[0][:100])
                continue
            hint = ""
            if n == 0:
                hint = "（页面上没有，这个字段代投会静默跳过）"
            elif n > 1:
                hint = "（有歧义，page.fill 会写第一个而不报错）"
            add(f"选择器 {label}", n == 1, f"{scope} {n}{hint}")

        # 2) 提交按钮。execute 点的是 .first，命中 >1 说明它可能点错按钮。
        n = page.locator(f"button:has-text('{SUBMIT_TEXT}')").count()
        add(f"SUBMIT_TEXT「{SUBMIT_TEXT}」", n == 1, f"命中 {n}")

        # 3) 「必须不在」的三条 + 登录判据：正例控制（认不认自己的词）
        for name, words in (
            ("CLOSED_TEXT", CLOSED_TEXT),
            ("LOGIN_TEXT", LOGIN_TEXT),
            ("SUCCESS_TEXT", SUCCESS_TEXT),
            ("DUPLICATE_TEXT", DUPLICATE_TEXT),
        ):
            out.extend(self._self_match_rows(page, name, words))

        # 4) 反例控制：这几条在健康表单页上应该是 0 命中
        for name, words, harm in (
            ("CLOSED_TEXT", CLOSED_TEXT, "能投的岗位会被判成已关闭"),
            ("SUCCESS_TEXT", SUCCESS_TEXT, "没投上也会记成 submitted"),
            ("DUPLICATE_TEXT", DUPLICATE_TEXT, "首投会被记成 duplicate"),
            ("LOGIN_TEXT", LOGIN_TEXT, "已登录也会被拦成需要登录"),
        ):
            try:
                n = page.locator(_any_text(words)).count()
            except Exception as exc:
                add(f"{name} 不误报", False, str(exc).split("\n")[0][:100])
                continue
            add(f"{name} 不误报", n == 0,
                "表单页上 0 命中" if n == 0
                else f"表单页上命中 {n} 处 —— 判据太宽，{harm}")

        # 5) `_need_login` 的第二条路：有「登录」按钮且没有「立即申请」就算未登录。
        #    `has-text` 是子串匹配，「退出登录」也含「登录」—— 而那个按钮恰恰是
        #    **登录成功后**才出现的。真撞上这条的话，登录态越正常越投不出去。
        try:
            login_btn = page.locator(f"button:has-text('{LOGIN_BTN_TEXT}')").count()
            texts = page.locator(f"button:has-text('{LOGIN_BTN_TEXT}')").all_inner_texts()
        except Exception as exc:
            add(f"LOGIN_BTN_TEXT「{LOGIN_BTN_TEXT}」不误报", False,
                str(exc).split("\n")[0][:100])
        else:
            bad = [t.strip() for t in texts if t.strip() and t.strip() != LOGIN_BTN_TEXT]
            add(f"LOGIN_BTN_TEXT「{LOGIN_BTN_TEXT}」不误报", not bad,
                f"命中 {login_btn} 个登录按钮" if not bad
                else f"子串命中了：{'、'.join(bad[:3])} —— 这类按钮在**已登录**时"
                     f"才出现，会把正常登录态判成未登录")

        return out

    def _self_match_rows(
        self, page: Page, name: str, words: tuple[str, ...]
    ) -> list[tuple[str, bool, str]]:
        """正例控制：白页上逐个词写进去，看 `_any_text(words)` 认不认。

        逐个词而不是整组一起写：三个词写在同一张页面上，只要有一个能命中整组
        就命中，拼错另外两个也是绿的。那样查的是「至少有一个词没写错」，不是
        「每个词都对」。
        """
        rows: list[tuple[str, bool, str]] = []
        if not words:
            return [(f"{name} 认得自己的词", False, "空元组 —— 这条判据永远不触发")]

        sel = _any_text(words)
        try:
            # 和目标站同一个 context（那里带着招聘账号的 cookie），但只 set_content
            # 不 goto —— 这张页停在 about:blank，不发任何请求，cookie 不出去。
            # 想省事换成 pw.chromium.launch() 另开浏览器的话，两阶段的 pw 还活着，
            # 多一个实例要多一份收尾，得不偿失。
            scratch = page.context.new_page()
        except Exception as exc:
            return [(f"{name} 认得自己的词", False,
                     f"开不了白页核不了：{type(exc).__name__}: {exc}"[:100])]
        try:
            missed = []
            for w in words:
                # 只放这一个词。命中 0 就是这个词自己有问题（拼写、元字符）。
                scratch.set_content(f"<div>{w}</div>")
                try:
                    if scratch.locator(sel).count() < 1:
                        missed.append(w)
                except Exception:
                    missed.append(w)
            rows.append((
                f"{name} 认得自己的词", not missed,
                f"{len(words)} 个词逐个都能命中（不代表站点还在用这些词）"
                if not missed else
                f"这些词自己都匹配不上：{'、'.join(missed)} —— 拼错了，"
                f"或者含正则元字符被当成语法",
            ))
        finally:
            try:
                scratch.close()
            except Exception:
                pass
        return rows

    def checkup(self, job: dict) -> list[tuple[str, bool, str]]:
        """走到表单、核判据、关浏览器。**一个字都不填，不提交。**

        走到表单这件事本身就核掉了 `APPLY_TEXT`：prepare 是靠点「立即申请」
        进表单的，点不到就走不到这里。所以那条不单独列一行。
        """
        from ..profile import from_dict

        plan = self.prepare(job, from_dict({}), fill_fields=False)
        if plan.status == "blocked" or not plan.confirm_token:
            return [("走到表单", False, plan.blocker or "prepare 没到表单")]

        session = SESSIONS.peek(plan.confirm_token)
        try:
            rows = [("走到表单", True, f"点到了「{APPLY_TEXT}」")] + \
                self.check_selectors(session.page)
        finally:
            SESSIONS.discard(plan.confirm_token)
        return rows


