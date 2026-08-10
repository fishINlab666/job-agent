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

    def prepare(self, job: dict, profile: FormProfile) -> SubmissionPlan:
        """走到提交按钮前停下，返回逐字段计划 + confirm_token。"""
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
                        page.wait_for_selector("text=立即申请", timeout=180000)
                        print("登录成功，继续填表", flush=True)
                    except PlaywrightTimeout:
                        closer()
                        return self._blocked(
                            plan,
                            "等待登录超时（180秒）",
                            self._shot(page, job_id, "login_timeout"),
                        )

            apply_btn = page.locator("text=立即申请").first
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
            page.locator("button:has-text('提交申请')").first.click()
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
            skipped_fields=[f.model_dump() for f in fields],
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
        return page.locator("text=/已停止|已下线|已结束/").count() > 0

    def _need_login(self, page: Page) -> bool:
        """是否需要登录。检测登录相关元素或缺少"立即申请"按钮。"""
        # 方法1：页面上有明确的登录入口文字
        if page.locator("text=/扫码登录|手机号登录|微信登录/").count() > 0:
            return True
        # 方法2：页面顶部有"登录"按钮且没有"立即申请"按钮
        has_login_btn = page.locator("button:has-text('登录')").count() > 0
        has_apply_btn = page.locator("text=立即申请").count() > 0
        return has_login_btn and not has_apply_btn

    def _is_success(self, page: Page) -> bool:
        """提交是否成功。成功文案：申请成功 / 提交成功 / 已提交。"""
        return page.locator("text=/申请成功|提交成功|已提交/").count() > 0

    def _is_duplicate(self, page: Page) -> bool:
        """是否重复投递。"""
        return page.locator("text=/已申请|重复投递|已经投递/").count() > 0


