"""飞书招聘代投 —— 两阶段版。

四个租户（nio / xiaopeng / bytedance / sensetime）共用同一套飞书前端，所以这
一个类管四家，租户由 `routing` 传进来（它有「配的和链接里的对不上就停」那道
闸，见 `routing._pick_tenant`）。

能力边界：

    prepare(job, profile)   打开岗位页 → 判岗位是否关闭 → 点「投递」
                            → 未登录则 blocked，交回给人（手机号 + 验证码，
                            只有用户本人做得了）→ 已登录则填表、截图、
                            发 confirm_token。**不提交。**
    execute(confirm_token)  校验 token + 回读摘要 → 勾隐私政策 → 点「提交简历」。
                            这是唯一对外发生不可逆动作的地方。

**`execute` 的最后一击从未真跑过**（截至 2026-08-10）。所以 `_is_success` /
`_is_duplicate` / `_is_job_closed` 的文案还是猜的 —— 只验过「未提交时不误报」。

页面流程（2026-08-10 实地探测四租户，四家一致，见 plan 010 §3）：
1. 访问 https://<host>/<portal>/position/<id>/detail
   —— **`/detail` 必须有**，少了渲染「您正在寻找的页面不存在」而 HTTP 仍是 200
2. 页面上有一个「投递」按钮（四家文案统一）
3. 点它 → 未登录时跳 /<portal>/login?redirect_path=%2Fresume%2F<id>%2Fapply
4. 表单：基本信息 + 教育经历直接展开，其余段（工作经历 / 项目经历 / 作品 /
   竞赛 / 证书 / 语言能力 / 自我评价 / 社交账号）要点「添加」才长出字段。
   **这些折叠段现在一个都不填** —— 画像里 `internships` / `projects` 是空列表，
   `FormProfile` 也只有平铺字段。要做得先扩画像层。
   实习经历那段连「添加」都没有：被「没有实习经历」勾选框关掉了，而它**默认勾上**。
   要填实习得先取消勾选 —— 那是改申报内容，必须先上确认清单。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

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

# 投递按钮的文案。四个租户实测一致（2026-08-10）。
APPLY_TEXT = "投递"

# 表单底部真正的提交按钮。**不能只写「提交」** —— 页面上「投递」「提交简历」
# 同时存在，模糊匹配会点错。实测字节是「提交简历」（2026-08-10 截图核实）。
SUBMIT_TEXT = "提交简历"

# 隐私政策勾选框。不勾提交按钮点不动。实测有两种类名（atsx-* 是飞书标准控件，
# ud__* 是字节自己那套），两个都试。
# 隐私政策勾选框。**只能是 atsx- 这一个**，不能和 ud__checkbox__input 用逗号并起来 ——
# 表单里那个「没有实习经历」正是 ud__checkbox__input，并起来可见命中 2 个，`.first`
# 拿到的是「没有实习经历」（它默认就 checked=True），于是 `if not is_checked()` 直接跳过，
# 隐私政策那个从头到尾没被勾上。这不是静默失败，是**静默勾错对象**：
# 误勾「没有实习经历」等于替用户申报了一条不实信息。（2026-08-10 实测祖先链确认）
CONSENT_BOX = "input.atsx-checkbox-input"
# 勾之前要在祖先文本里看到这几个字。类名会随组件升级变，文案是语义锚点；
# 两个都对不上就停手，不赌。
CONSENT_TEXT = "隐私政策"

# 有头模式下等人手动登录的上限（秒）。手机号+验证码要收短信，60s 不够宽裕。
LOGIN_WAIT_SECONDS = 180

# 叶子字段的容器。表单是 Formily 渲染的，每个字段一个 `.ud-formily-item`，
# **但这些 item 是嵌套的** —— 教育经历那种可增删的段落，外层数组容器也带
# `.ud-formily-item`。只写 `.ud-formily-item:has(label:text-is("学校名称"))` 会
# 命中外层容器，然后段内 10 个输入框全被抓进来（实测命中数 10）。
#
# 叶子和容器的区别在 size 类名：叶子是 `-size-large`，数组容器是 `-size`。
# 加上这个类名之后 12 条选择器全部唯一命中（实测）。
LEAF = ".ud-formily-item-size-large"

# 自定义下拉的列表项。**不是原生 `<select>`**，所以 `select_option` 对它无效，
# 要「点开容器 → 点列表项」，这是 FORM_FIELDS 里 action="pick" 的来历。
SELECT_ITEM = ".ud__select__list__item"

# 表单字段表。2026-08-10 在 jobs.bytedance.com 真登录后逐条核实：
# 每条选择器在真页面上可见命中数都是 1，填/选之后都做过回读。
#
# 复核命令（要登录态，`.browser` 里有）：
#   uv run python -m jobagent.cli apply <job_id> --user-data-dir .browser --no-headless
#
# 关于选择器为什么长这样：这些 input **没有 id、没有 name**，class 全是
# `ud__native-input` 这种跨字段通用值 —— 唯一能区分字段的锚点就是 label 文案。
# 所以判据落在中文字符串上，这是被迫的，不是偷懒。**因此必须配守卫**：
# `label_drift()` 会检查这张表里的 label 是否还都在页面上，见那个方法的注释。
#
# 动作词汇：
#   fill    普通文本框，page.fill
#   pick    ud__select 自定义下拉，点开再点选项（不是 select_option）
#   upload  文件输入，set_input_files（该 input 是隐藏的，隐藏不影响上传）
FORM_FIELDS: list[tuple[str, str, str, str]] = [
    ("name",        "姓名",     f'{LEAF}:has(label:text-is("姓名")) input.ud__native-input', "fill"),
    # 手机号那个 item 里有两个控件：区号下拉（combobox）+ 号码框。要排掉前者。
    ("phone",       "手机号码", f'{LEAF}:has(label:text-is("手机号码")) input.ud__native-input:not([role="combobox"])', "fill"),
    ("email",       "邮箱",     f'{LEAF}:has(label:text-is("邮箱")) input.ud__native-input', "fill"),
    # 个人证件同理：证件类型下拉 + 证件号框。
    ("id_card",     "个人证件", f'{LEAF}:has(label:text-is("个人证件")) input.ud__native-input:not([role="combobox"])', "fill"),
    ("school",      "学校名称", f'{LEAF}:has(label:text-is("学校名称")) input.ud__native-input', "fill"),
    ("major",       "专业",     f'{LEAF}:has(label:text-is("专业")) input.ud__native-input', "fill"),
    ("degree",      "学历",     f'{LEAF}:has(label:text-is("学历")) .ud__select__selector', "pick"),
    ("resume_path", "简历附件", 'input[type="file"]', "upload"),
    # 手机区号。默认 +1（美国），必须改成中国大陆 —— 号码对了区号错了，
    # HR 那边照样打不通。值来自 FIXED_VALUES，不来自画像。
    ("phone_cc",    "手机区号", f'{LEAF}:has(label:text-is("手机号码")) .ud__select__selector', "pick"),
]

# 不来自画像、由我们固定填的值。
#
# **为什么走 FORM_FIELDS 而不是在 `_fill` 里偷偷设**：这样它会出现在确认清单上，
# 用户看得见代投替他填了什么。这个项目的整个两阶段闸门就是为了「不会背着你动」，
# 藏一个字段进去等于在闸门上开个小口。source 列会标成「固定值」而不是画像路径。
#
FIXED_VALUES: dict[str, str] = {
    "phone_cc": "+86",
}

# 点选时用的**列表项文案**，只在它和控件显示值不一样时才记一条。
#
# 区号这项两者是不一样的：列表里写「+86 （中国大陆）」，选完控件上只显示「+86」。
# 混用会各错一头 ——
#   · 拿「+86」去点：`:has-text` 是子串匹配，251 项区号表里可见命中 **2 个**
#     （「+86 （中国大陆）」和「+869 （圣基茨和尼维斯）」），`_pick` 拒绝猜、直接抛。
#   · 拿全称当值：`_readback_digest` 从控件读回来的是「+86」，和计划里的全称对不上，
#     `execute` 每次都报 `drifted`，**永久拒绝提交**。（两个都是 2026-08-10 实测）
#
# 所以 value 记控件显示值（digest 的口径），option_text 记列表项文案（点击的口径）。
# 学历这类两者相同的字段不用在这里出现。
PICK_OPTION_TEXT: dict[str, str] = {
    "phone_cc": "+86 （中国大陆）",
}

# 可重复段的条目卡片。CSS-modules 哈希后缀每次构建都变，只能按前缀匹配 ——
# 注意末尾那两个下划线是必需的：`apply-form-array-card-content__xxx` 也以
# `apply-form-array-card` 开头，带上 `__` 才只命中卡片本身。（2026-08-10 实测）
ARRAY_CARD = '[class*="apply-form-array-card__"]'

# 字段属于哪一类卡片的第几张。**用卡里的一个字段名认卡，不用段名** ——
# 段标题不在数组容器的 label 里（那个 label 抓到的是内层第一个字段名，实测显示
# 「起止时间」），而「含『学校名称』那张卡」本身就唯一确定了它是教育经历卡。
#
# 现在全部指向第 0 张：画像层只给一段教育经历（`_latest_education_index` 挑最近的）。
# 多条目要等 FormProfile 能装多条 —— profile.yaml 里 internships / projects 是空列表。
CARD_SCOPE: dict[str, tuple[str, int]] = {
    "school": ("学校名称", 0),
    "major":  ("学校名称", 0),
    "degree": ("学校名称", 0),
}


def _card(anchor: str) -> str:
    """「含某个 label 的条目卡片」的选择器。"""
    return f'{ARRAY_CARD}:has(label:text-is("{anchor}"))'

# 从选择器里抽出 `label:text-is("...")` 锚定的文案。`label_drift` 用它查页面 ——
# 查真正锚定的字符串，而不是展示名（这两个可以不一样，见 phone_cc）。
_ANCHOR_RE = re.compile(r'label:text-is\("([^"]+)"\)')

# 页面上没有任何必填标记 —— 这是实测结论，不是没查：
# label 的 innerHTML 里没有星号（`*` / `＊` 全表 0 命中），控件上没有 required、
# 没有 aria-required，item 类名里也没有 asterisk/required（带必填属性的元素 0 个）。
# 校验大概是提交时才跑的。
#
# 所以这里留空，而不是照直觉把姓名/手机号写进去 —— 那会在计划里显示「页面必填」，
# 是我们自己编的一句话。留空的代价只是缺一个提前提醒，编一句的代价是用户以为
# 那是站点说的。
REQUIRED_FIELDS: set[str] = set()

# 下拉的可选值，实测抄下来的（2026-08-10，点开下拉读的列表）。画像里的值必须
# 正好等于其中一项，对不上就跳过并说明 —— 不做近似匹配，「硕士研究生」自动变
# 「硕士」这种事应该由用户在画像里改对，不该由代投偷偷替他决定。
#
# 这张表只用来**提前拦下对不上的值**（在计划里就说清楚，而不是等填的时候失败）。
# 站点加了新选项而这里没跟上，后果是「本来能填的被拦下」——啰嗦但不会填错。
DROPDOWN_OPTIONS: dict[str, list[str]] = {
    "degree": ["博士", "MBA", "硕士", "本科", "大专", "高中", "专职", "初中", "小学"],
}

# 学历类型的选项（统招全日制/自考…）。**FORM_FIELDS 里没有这一条** ——
# 画像的 FIELD_SPECS 里没有对应字段，编一个映射过去就是猜。记在这里是因为
# 探测时读到了，将来画像加了这一项可以直接接上。
DEGREE_TYPE_OPTIONS = ["海外及港澳台", "统招全日制", "统招非全日制", "自考", "其他"]

# 截整张投递表单用的两段 JS。见 `_shot` 的 docstring 讲为什么要这么绕。
#
# 关键事实（2026-08-10 实测字节投递页）：从内层滚动容器一路到 <html>，
# section / div / body / html **四层全部锁在 720px**，只放开最内层那一层，
# document.scrollHeight 仍是 720，`full_page=True` 照样只截首屏。所以要
# 沿 parentElement 整条链解锁，然后把容器高度设成它的 scrollHeight。
# 实测这么做之后文档高度 720 → 3931，截图拿到 1280x3931 的完整表单。
_EXPAND_JS = """() => {
    const el = [...document.querySelectorAll('*')].find(e => {
        const s = getComputedStyle(e);
        return e.scrollHeight > e.clientHeight + 50
            && ['auto', 'scroll', 'overlay'].includes(s.overflowY)
            && e.clientHeight > 100;
    });
    if (!el) return false;
    const target = el.scrollHeight;
    // 记原值以便还原；标记这条链，还原时靠 data 属性找回来
    for (let n = el; n; n = n.parentElement) {
        n.dataset.jobagentShot = JSON.stringify({
            h: n.style.height, max: n.style.maxHeight,
            ov: n.style.overflow, pos: n.style.position,
        });
        n.style.height = 'auto';
        n.style.maxHeight = 'none';
        n.style.overflow = 'visible';
        // fixed 的祖先会把自己钉在视口上、跟着撑不开，临时改静态定位
        if (getComputedStyle(n).position === 'fixed') n.style.position = 'static';
    }
    el.style.height = target + 'px';
    return true;
}"""

_RESTORE_JS = """() => {
    for (const n of document.querySelectorAll('[data-jobagent-shot]')) {
        let prev;
        try { prev = JSON.parse(n.dataset.jobagentShot); } catch { prev = null; }
        if (prev) {
            n.style.height = prev.h;
            n.style.maxHeight = prev.max;
            n.style.overflow = prev.ov;
            n.style.position = prev.pos;
        }
        delete n.dataset.jobagentShot;
    }
}"""


class FeishuSubmitter:
    source_key = "feishu"
    system = "feishu"       # 多租户，构造函数必须收 tenant（routing 会检查）

    # 租户 slug → 公司名。归属都用页面标题核实过，见 kb/company-portals.md。
    # 不在这张表里的租户照样能跑，company 退回 slug 本身 —— 但不许编一个中文名。
    COMPANIES = {
        "nio": "蔚来",
        "xiaopeng": "小鹏汽车",
        "bytedance": "字节跳动",
        "sensetime": "商汤科技",
    }

    # 租户 slug → 代投时要改写成的 host。**只影响导航，不改库里的 apply_url。**
    #
    # 为什么要这张表（2026-08-10 实测）：字节这个租户在 `*.jobs.feishu.cn` 镜像上
    # 加载的是字节自家的 IAM Passport 登录控件（`div.login-content-container`,
    # data-version 1.0.8），而该控件 bootstrap 要打的 `POST /accounts/flow/init`
    # **只在 jobs.bytedance.com 上存在**：feishu 镜像返 404，控件拿不到 flow 配置
    # 就静默放弃 —— 容器挂进 DOM 但 innerHTML 恒为 0，等 65s 也不填充，且不抛 JS
    # 异常（pageerror 为 0），所以页面看起来像"正常加载完了"，只有一片空白。
    # 换 host 后同路径同岗位 ID 就能正常渲染手机号+验证码表单。
    #
    # 三条排除依据，都不依赖中文文案：
    #   1. 不是反自动化：headless/headful、真 Chrome UA、5 种视口，结果全一致；
    #      httpx 裸发（无浏览器）同样 404，而打到 jobs.bytedance.com 是 400
    #      —— 400 说明端点在、只是 body 不合法。反自动化不会表现成"路由不存在"。
    #   2. 不是我们的代码/浏览器：同一份 Playwright 代码在 nio/xiaopeng 上正常出
    #      表单（走的是飞书标准 `atsx-*` 组件，页面里根本没有 iam-passport 容器，
    #      也不发 flow/init）—— 三个租户两正常一空白，变量是租户不是我们。
    #   3. 不是个例：抽 4 个真实岗位 ID 逐个对照，4/4 都是 feishu host 无表单 /
    #      bytedance host 有表单。
    #
    # 不在表里的租户不改写（nio/xiaopeng 原 host 就是好的）。这张表是"哪个 host
    # 能登录"的知识，和 apply_url 的形状知识分开放 —— 后者归 repair-apply-url。
    LOGIN_HOSTS = {
        "bytedance": "jobs.bytedance.com",
    }

    def __init__(
        self,
        tenant: str,
        headless: bool = True,
        timeout: float = 30.0,
        user_data_dir: str | None = None,
    ) -> None:
        """
        Args:
            tenant: 租户 slug，由 routing 传入。**不给默认值** —— 少一个租户
                参数不会报错，只会投到别人家公司去（routing 里有同样的注释）
            headless: 是否无头模式。要人登录时必须 False
            timeout: 页面操作超时（秒）
            user_data_dir: 浏览器用户数据目录，持久化登录态用
        """
        if not tenant:
            raise ValueError(
                "FeishuSubmitter 必须给 tenant。飞书是多租户系统，"
                "没有租户就不知道这是哪家公司的岗位。"
            )
        self.tenant = tenant
        self.company = self.COMPANIES.get(tenant, tenant)
        self.headless = headless
        self.timeout = timeout * 1000  # Playwright 用毫秒
        self.user_data_dir = user_data_dir

    # ---------- 阶段一：走到登录门（本轮到此为止） ----------

    def prepare(self, job: dict, profile: FormProfile, *,
                fill_fields: bool = True) -> SubmissionPlan:
        """打开岗位页 → 判状态 → 点投递 → 过登录门 → 填表 → 发 token。不提交。

        `fill_fields=False` 走到表单就停，一个字都不写。`checkup()` 用它 ——
        判据体检要的是「选择器还认不认这个页面」，不需要往里写东西。

        为什么是个参数而不是把导航段抽成独立方法：那 90 行里每个早退都带着
        `closer()` 和一条具体的归因话术（SPA 的 404 / 岗位关闭 / 登录门 /
        中间态转圈），是三次判错换来的。为了体检去重构它，风险换不来收益。
        """
        job_id = str(job.get("external_id") or job.get("id") or "")
        apply_url = str(job.get("apply_url") or "")
        plan = SubmissionPlan(
            job_id=job_id,
            source_key=str(job.get("source_key") or self.source_key),
            company=self.company,
            title=str(job.get("title") or ""),
            apply_url=apply_url,
        )

        # 链接是导航的唯一依据，不自己拼 —— 拼的话就得在这里再维护一份
        # host/门户/后缀的知识，和适配器两处走岔。缺链接就停。
        if not apply_url:
            return self._blocked(plan, "这个岗位没有 apply_url，没法导航")
        if not apply_url.endswith("/detail"):
            # 老形状是死链（plan 010）。直接报出来，别打开一个「页面不存在」
            # 然后归因成「站点改版」。
            return self._blocked(
                plan,
                f"apply_url 是修复前的老形状（少 /detail），打开会是空页面："
                f"{apply_url}。先跑 repair-apply-url",
            )

        # 只改导航用的 host，plan.apply_url 保持库里的原值（存证要的是我们记录的
        # 那个链接，不是我们绕道走的那个）。见 LOGIN_HOSTS 的注释。
        nav_url = self._login_host_url(apply_url)

        pw = sync_playwright().start()
        try:
            page, closer = self._launch(pw)
        except Exception as exc:
            pw.stop()
            return self._blocked(plan, f"浏览器启动失败: {exc}")

        try:
            # wait_until 显式给 domcontentloaded，不用默认的 "load"。
            # 实测这个页面：load 要 14s，domcontentloaded 要 6s，而
            # **「投递」按钮在 domcontentloaded 之后就已经在 DOM 里了**
            # （实测 count()==1）。等 load 是在等图片和统计脚本，跟我们要的
            # 元素没关系。默认值在 30s 上限下能过，但余量只有一倍，
            # 有头模式 + 冷的持久化目录（首次要写 21MB）就会顶出去 ——
            # 2026-08-10 用户第一次登录就是栽在这，报 Timeout 30000ms。
            page.goto(nav_url, timeout=self.timeout, wait_until="domcontentloaded")
            # networkidle 拿掉了。它要求 500ms 无网络活动，而这个页面有统计/
            # 长连接，实测还要再花 6~13s，且拿到的东西跟 domcontentloaded 时
            # 一样（按钮个数相同）。Playwright 官方也不建议用它。
            # 换成等按钮自己出现：**等我们真正要的那个元素，而不是等「页面大概好了」**。
            try:
                page.wait_for_selector(
                    f"button:has-text('{APPLY_TEXT}')", timeout=self.timeout
                )
            except PlaywrightTimeout:
                # 等不到不在这里判死：下面 _is_page_missing / _is_job_closed
                # 要先看正文，才能分清「岗位关了」和「页面结构变了」。
                pass
            shot = self._shot(page, job_id, "prepare")
            plan.screenshot_path = shot

            # SPA 的 404 在渲染层，HTTP 是 200 —— 判据只能是正文（plan 010 §3）
            if self._is_page_missing(page):
                closer()
                return self._blocked(
                    plan,
                    "页面渲染「页面不存在」。岗位可能已下架，或链接形状又变了",
                    shot,
                )
            if self._is_job_closed(page):
                closer()
                return self._blocked(plan, "岗位已关闭", shot)

            # count() 要问 locator 本身，不是问 .first —— .first 是「第 0 个元素」
            # 这个概念，对它数个数在语义上是错的（真实 Playwright 下恒为 0 或 1，
            # 掩盖得住；假页面下直接暴露）。
            apply_loc = page.locator(f"button:has-text('{APPLY_TEXT}')")
            if not apply_loc.count():
                closer()
                return self._blocked(
                    plan, f"没找到「{APPLY_TEXT}」按钮，页面结构可能已变", shot
                )
            apply_loc.first.click()

            # 点投递后页面不是立刻到位：先落在 /resume/<id>/apply 且正文只有一个
            # loading spinner（实测 body 40 字节），要 3~5s 才到稳态。立刻判
            # _need_login 会看到这个中间态、误判成「不需要登录」，然后把一张转圈的
            # 图当成表单交出去 —— 2026-08-10 就是这么错了三次。
            #
            # 所以等「两个稳态之一」：登录门出现，或投递表单出现。用轮询而不是
            # wait_for_function：这中间可能发生真实导航，注入的函数会连同旧
            # execution context 一起失效。
            for _ in range(20):
                page.wait_for_timeout(1000)
                if self._need_login(page) or self._form_ready(page):
                    break

            if self._need_login(page):
                login_shot = self._shot(page, job_id, "login")
                # 无头模式下等人是白等 —— 没有窗口可以让他操作。直接交回去。
                if self.headless:
                    closer()
                    return self._blocked(
                        plan,
                        f"需要登录 {self.company} 的招聘账号（手机号+验证码，只能你自己做）。"
                        f"用 --no-headless 配 --user-data-dir 跑一次，登录态会持久化",
                        login_shot,
                    )
                # jobs.bytedance.com 的登录页默认是「邮箱+密码」tab，但校招账号
                # 通常没有密码、只能手机号+验证码。替人把 tab 切过去 —— 这不是
                # 代填凭据，只是点一下切换，人还是要自己输手机号和收验证码。
                self._switch_to_phone_login(page)

                # 有头模式：把窗口留给人，轮询等他登完。
                # 判据是「登录门消失」而不是「URL 离开 /login」—— 换 host 之后
                # 登录表单可能内联渲染在 /resume/<id>/apply 上，URL 从头到尾不含
                # /login，用 URL 判会一进来就以为已经登录成功了。
                print(
                    f"\n请在浏览器窗口里登录 {self.company} 的招聘账号"
                    f"（手机号+验证码）。\n最多等 {LOGIN_WAIT_SECONDS} 秒，登完会自动继续。\n"
                )
                for i in range(LOGIN_WAIT_SECONDS):
                    page.wait_for_timeout(1000)
                    if not self._need_login(page):
                        print("登录门已消失，继续")
                        break
                    if i and i % 15 == 0:
                        print(f"  仍在等登录…（{i}s / {LOGIN_WAIT_SECONDS}s）")
                else:
                    closer()
                    return self._blocked(
                        plan,
                        f"等了 {LOGIN_WAIT_SECONDS} 秒登录门还在。可能是没登完，"
                        f"也可能是登录控件坏了 —— 后者先查 flow_init_status()",
                        login_shot,
                    )

            # 不管有没有登录门，都要等表单渲染好。
            # 已登录的情况：点投递后直接进 /resume/<id>/apply，页面要 3~5s 渲染表单。
            for _ in range(20):
                page.wait_for_timeout(1000)
                if self._form_ready(page):
                    break

            # 填之前先查守卫：label 全丢了说明选择器整套过期，这时候「填不上」
            # 会被 _fill 逐字段吞成 note，最后交出一张空表单 + 一句「填了 0 个」。
            # 宁可在这里停住并说清楚。
            drift = self.label_drift(page)
            # 分母是**去重后的锚点总数**，和 label_drift 的口径一致。按 FORM_FIELDS
            # 条数算会永远差一点（手机号码被两条共用、简历附件不锚 label），
            # 于是「一个都对不上」这个分支永远进不去。
            anchors = {a for _k, _l, sel, _a in FORM_FIELDS for a in _ANCHOR_RE.findall(sel)}
            if anchors and len(drift) == len(anchors):
                form_shot = self._shot(page, job_id, "drift")
                closer()
                return self._blocked(
                    plan,
                    f"表单上一个都对不上了（找不到这些字段名：{'、'.join(drift)}）。"
                    f"页面结构大概改版了，FORM_FIELDS 需要重新探。截图在 "
                    f"{form_shot or '（截图失败）'}",
                    form_shot,
                )

            plan.fields = self._plan_fields(profile)
            if not fill_fields:
                # 体检路径：走到表单就停，页面保持原样交给调用方查判据。
                plan.confirm_token = mint_token()
                plan.expires_at = time.time() + PLAN_TTL_SECONDS
                SESSIONS.put(LiveSession(plan, page, closer))
                return plan

            self._fill(page, plan.fields)

            # 填完读一遍页面自己的校验结果。`_fill` 只知道有没有抛异常，
            # 页面判「身份证号不合法」时什么都不抛 —— 不读这一遍，清单上会
            # 显示「已填」，然后带着一个非法值提交出去。
            errors = self._apply_page_errors(plan.fields, self.page_errors(page))

            plan.screenshot_path = self._shot(page, job_id, "prefilled")

            # 这几句都往 blocker 里汇，所以先攒起来再拼 —— 直接赋值会互相覆盖，
            # 后写的那条把前一条顶掉，用户就少看到一半原因。
            notes: list[str] = []
            # 部分 label 失效：填了一些但不是全部。不拦，但要在计划里说出来，
            # 让用户在确认环节看得见「这几项是因为页面变了才没填」。
            if drift:
                notes.append(f"这些字段名在页面上没找到，已跳过：{'、'.join(drift)}")
            # 归不到任何字段的校验错误（可能来自我们没填的那些项）也得说，别吞。
            if errors:
                rest = "；".join(f"{k}：{v}" for k, v in errors.items())
                notes.append(f"页面上还有这些校验提示：{rest}")
            if notes:
                plan.blocker = " / ".join(notes)

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
        """校验 token 与字段摘要，然后点「提交简历」。

        走到这里意味着用户已经在确认环节看过逐字段清单并同意了 —— 这是两阶段
        闸门里唯一允许对外发生动作的地方。
        """
        session = SESSIONS.peek(confirm_token)
        if session is None:
            SESSIONS.take(confirm_token)  # 抛 unknown，错误话术只在一处

        plan = session.plan
        page = session.page

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
            # 隐私政策勾选框：不勾提交按钮点不动。放在这里而不是 _fill 里 ——
            # 它不是画像里的一项，是一次同意动作，只该发生在用户确认之后。
            if why := self._consent(page):
                return self._result(plan, "blocked", plan.screenshot_path,
                                    error=f"没能勾上隐私政策：{why}")

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
            skipped_fields=[f.model_dump() for f in fields],
            note="用户在确认环节放弃",
        )

    # ---------- 字段计划与填写 ----------

    def _plan_fields(self, profile: FormProfile) -> list[FieldPlan]:
        """把画像翻译成逐字段计划。形状和腾讯那份一致。"""
        plans: list[FieldPlan] = []
        for key, label, selector, action in FORM_FIELDS:
            anchor, idx = CARD_SCOPE.get(key, (None, 0))
            scope = _card(anchor) if anchor else None

            if key in FIXED_VALUES:
                # 固定值字段（区号）：不查画像，但照样进计划、照样上确认清单。
                plans.append(FieldPlan(
                    selector=selector, label=label, value=FIXED_VALUES[key],
                    source="固定值", required=False, action=action,
                    option_text=PICK_OPTION_TEXT.get(key),
                    scope=scope, scope_index=idx,
                ))
                continue

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

            # 下拉：画像里的值必须**正好等于**某个选项。不做近似匹配 ——
            # 「硕士研究生」→「硕士」这种转换看着无害，但它是代投替用户改了
            # 申报内容。跳过并说清楚，让用户自己去画像里改对。
            opts = DROPDOWN_OPTIONS.get(key)
            if action == "pick" and opts and value not in opts:
                plans.append(FieldPlan(
                    selector=selector, label=label, value=value, source=source,
                    required=key in REQUIRED_FIELDS, action="skip",
                    sensitive=sensitive,
                    note=f"画像里的「{value}」不在页面选项里（可选：{'、'.join(opts)}），"
                         f"需要你改画像或手动选",
                ))
                continue

            plans.append(FieldPlan(
                selector=selector, label=label, value=value, source=source,
                required=key in REQUIRED_FIELDS, action=action,
                sensitive=sensitive, scope=scope, scope_index=idx,
            ))
        return plans

    @staticmethod
    def _one(page: Page, selector: str, label: str,
             scope: str | None = None, scope_index: int = 0):
        """要求可见命中**正好一个**，否则抛。

        **为什么不能用 `.first`**：这些选择器全锚在中文 label 上，而可重复段里
        每条目的字段名完全一样（教育经历点「添加」就长出一整套重名的学校名称/
        学历/专业，2026-08-10 实测 9 个叶子变 18 个）。命中 2 个时 `.first` 按
        DOM 顺序闷头挑第一个，`filled=True`，回读走同一个 `.first` 所以 digest
        照样对得上 —— 写错了对象，全程一声不响。

        澄清一句以免误读：**表单每次加载都是全空的、只有 1 张卡**（同日实测，
        ATS 侧不预填），所以第二张卡只会由「有人点了添加」产生。这个守卫防的
        不是加载出来就有两条，是我们自己填多条目时把字段写到别的卡上。

        歧义就是「我不知道该填哪个」，那就该说出来让人看，不是替他挑一个。

        `scope` 给了就先收窄到那一类容器的第 `scope_index` 个 —— 可重复段里
        每条目的字段名完全一样，收窄之后「命中 1 个」才重新变成一个有意义的断言。
        """
        root = page
        if scope:
            cards = page.locator(scope).locator("visible=true")
            total = cards.count()
            if scope_index >= total:
                raise ValueError(
                    f"要填第 {scope_index + 1} 条，但页面上只有 {total} 条"
                    f"（判据 `{scope}`）"
                )
            root = cards.nth(scope_index)

        loc = root.locator(selector).locator("visible=true")
        n = loc.count()
        if n != 1:
            where = f"第 {scope_index + 1} 条里" if scope else "页面上"
            raise ValueError(
                f"「{label}」在{where}可见命中 {n} 个，不敢猜填哪个"
                + ("（可能有多条同名记录）" if n > 1 else "")
            )
        return loc.first

    def _fill(self, page: Page, plans: list[FieldPlan]) -> None:
        """按计划填页面。单个字段失败不中断 —— 用户在确认界面能看到谁没填上。"""
        for f in plans:
            if f.action == "skip" or not f.value:
                continue
            try:
                if f.action == "fill":
                    self._one(page, f.selector, f.label,
                              f.scope, f.scope_index).fill(
                        str(f.value), timeout=self.timeout
                    )
                elif f.action == "pick":
                    self._pick(page, f.selector,
                               f.option_text or str(f.value), f.label,
                               f.scope, f.scope_index)
                elif f.action == "upload":
                    # 这个 input 是隐藏的（Formily 用自定义上传区包着），
                    # set_input_files 不要求可见，照样能用。
                    page.set_input_files(f.selector, str(f.value))
                    page.wait_for_timeout(2000)
                f.filled = True
            except Exception as exc:
                f.filled = False
                # 我们自己抛的 ValueError 带着「命中 2 个」这类判据，原话比
                # 类名有用得多 —— 只写 `ValueError` 用户没法知道是歧义还是没找到。
                why = (str(exc).split("\n")[0][:120]
                       if isinstance(exc, ValueError) else type(exc).__name__)
                f.note = f"填写失败：{why}，需人工补"

    @staticmethod
    def _apply_page_errors(
        fields: list[FieldPlan], errors: dict[str, str]
    ) -> dict[str, str]:
        """把页面校验错误挂回对应字段，返回**没能归属**的那些。

        挂上的字段 `filled` 打回 False —— 它会从「已填」挪到「需人工补」那一栏。
        页面说这个值不合法，清单上就不该显示「已填」。

        归不到字段的错误原样返回，由调用方说出来（可能来自我们没填的项）。
        吞掉它们等于让用户以为页面上什么提示都没有。
        """
        rest = dict(errors)
        for f in fields:
            err = rest.pop(f.label, None)
            if err:
                f.filled = False
                f.note = f"页面判这个值不合法：{err}"
        return rest

    def page_errors(self, page: Page) -> dict[str, str]:
        """读页面自己的校验错误，返回 {label: 错误文案}。

        **为什么必须有这个**：`_fill` 只知道「那行代码有没有抛异常」。
        页面前端把值判成非法时不会抛任何东西 —— 填了一个错的身份证号，
        `page.fill` 照样成功、`filled` 照样是 True，确认清单上照样显示「已填」，
        然后提交出去，对方系统里多一条脏数据。

        实测（2026-08-10 字节投递页）：错误文案在 `.ud-formily-item-error-help`，
        往上找最近的叶子 item 就能拿到它归属的字段名。填了一个非法身份证号后
        读到 `{'个人证件': '请输入正确的身份证号码'}`。
        """
        try:
            raw = page.evaluate(
                """(leaf) => Object.fromEntries(
                    [...document.querySelectorAll('.ud-formily-item-error-help')]
                        .filter(e => e.offsetParent && e.innerText.trim())
                        .map(e => [
                            (e.closest(leaf)?.querySelector('label')?.innerText || '?').trim(),
                            e.innerText.trim(),
                        ])
                )""",
                LEAF,
            )
        except Exception:
            return {}
        return {str(k): str(v) for k, v in (raw or {}).items()}

    def _consent(self, page: Page, timeout: int = 5000) -> str | None:
        """勾上隐私政策。成功返回 None，失败返回**原因**（调用方要说给用户听）。

        三道守卫，每道都是实测踩出来的：

        1. **可见命中必须正好 1 个** —— 老代码用 `input.atsx-checkbox-input,
           input.ud__checkbox__input` 并起来，命中 2 个，`.first` 拿到的是
           「没有实习经历」。
        2. **祖先文本里得有「隐私政策」** —— 类名随组件升级会变，文案是语义锚点。
           这两个风险的代价不对称：漏勾只是提交被页面拦下，勾错是替用户申报了
           一条不实信息。所以宁可停手。
        3. **勾完回读一次** —— `check()` 没抛不等于状态真变了。

        不返 bool 而返原因字符串：调用方要把它写进结果给用户看。「没能勾上」
        和「没能勾上，因为页面上找到 2 个候选框」是两条不同的信息。
        """
        loc = page.locator(CONSENT_BOX).locator("visible=true")
        try:
            n = loc.count()
        except Exception as exc:
            return f"读不到勾选框（{type(exc).__name__}）"
        if n != 1:
            return f"可见候选框 {n} 个（判据 `{CONSENT_BOX}`），不敢猜勾哪个"

        box = loc.first
        try:
            near = box.evaluate(
                """(el) => {
                    for (let n = el, d = 0; n && d < 6; n = n.parentElement, d++) {
                        const t = (n.innerText || '').trim();
                        if (t) return t;
                    }
                    return '';
                }"""
            )
        except Exception as exc:
            return f"读不到勾选框旁边的文案（{type(exc).__name__}）"
        if CONSENT_TEXT not in (near or ""):
            return (f"勾选框旁边没看到「{CONSENT_TEXT}」"
                    f"（读到的是「{(near or '')[:30]}」），拒绝勾一个不认识的框")

        try:
            if not box.is_checked():
                box.check(timeout=timeout)
            if not box.is_checked():
                return "勾了但状态没变"
        except Exception as exc:
            return f"勾不上（{type(exc).__name__}）"
        return None

    def _pick(self, page: Page, selector: str, value: str,
              label: str = "下拉", scope: str | None = None,
              scope_index: int = 0) -> None:
        """在 ud__select 自定义下拉里选一项：点开容器 → 点列表项。

        两个坑，都是实测踩出来的：

        1. **`select_option` 无效** —— 这不是原生 `<select>`，是一堆 div。
        2. **列表项要用 `:has-text` 不能用 `:text-is`** —— 项的文字在内层
           `.ud__select__list__item__content` 里，外层 item 自己没有直接文本节点，
           `:text-is("硕士")` 命中 0，`:has-text("硕士")` 命中 1。

        `:has-text` 是子串匹配，所以**命中数不是 1 就抛**，不赌「第一个大概是对的」：
        选错学历这种事没法从提交结果里看出来。
        """
        box = self._one(page, selector, label, scope, scope_index)
        box.scroll_into_view_if_needed()
        page.wait_for_timeout(250)
        box.click(timeout=self.timeout)
        page.wait_for_timeout(900)          # 列表是异步渲染的

        opt = page.locator(f'{SELECT_ITEM}:has-text("{value}")').locator("visible=true")
        n = opt.count()
        if n != 1:
            page.keyboard.press("Escape")
            raise ValueError(f"下拉项「{value}」命中 {n} 个，拒绝猜")
        opt.first.click(timeout=self.timeout)
        page.wait_for_timeout(600)

    def check_selectors(self, page: Page) -> list[tuple[str, bool, str]]:
        """逐条核这个模块里的字符串级判据。返回 [(名字, 通过, 说明)]。

        **为什么必须有这个**：这个文件里能被站点单方面改坏的常量有一打 ——
        `FORM_FIELDS` 的 8 个中文 label、`ARRAY_CARD` 的类名前缀、`CARD_SCOPE`
        的锚点、`CONSENT_BOX` / `CONSENT_TEXT`、`PICK_OPTION_TEXT` 的选项全称、
        `SUBMIT_TEXT`。它们坏掉的方式全都是**静默**的：命中 0 个 → `_fill` 把每个
        miss 吞成 per-field note → 交出一张几乎空的表单加一句「填了 2 个字段」。
        比报错难发现得多。

        所以判据不能只是写对，还得有一条命令能回答「怎么知道它失效了」。
        这个方法就是那条命令的实现（`jobagent checkup`）。

        只读：不填任何值。唯一的交互是把下拉点开读选项（关掉时列表项虽在 DOM 里
        但不可见，读不到文案），读完按 Escape。
        """
        out: list[tuple[str, bool, str]] = []

        def add(name: str, ok: bool, note: str = "") -> None:
            out.append((name, ok, note))

        # 1) 字段 label
        drift = self.label_drift(page)
        add("FORM_FIELDS 的 label", not drift,
            f"页面上找不到：{'、'.join(drift)}" if drift else "全部在")

        # 2) 每条选择器可见命中数。1 才算对 —— 0 是没找到，>1 是歧义。
        #    简历附件那个 file input 是隐藏的，走 set_input_files 不要求可见，
        #    所以它按「命中 >= 1」算过。
        for key, label, selector, action in FORM_FIELDS:
            anchor, idx = CARD_SCOPE.get(key, (None, 0))
            try:
                if action == "upload":
                    n = page.locator(selector).count()
                    ok = n >= 1
                else:
                    self._one(page, selector, label,
                              _card(anchor) if anchor else None, idx)
                    n, ok = 1, True
            except Exception as exc:
                add(f"选择器 {label}", False, str(exc).split("\n")[0][:100])
                continue
            add(f"选择器 {label}", ok, f"命中 {n}")

        # 3) 条目卡片。少写末尾的 `__` 会把内层 content 也算进来（实测 1 → 11）。
        cards = page.locator(ARRAY_CARD).locator("visible=true").count()
        loose = page.locator('[class*="apply-form-array-card"]').locator(
            "visible=true").count()
        add("ARRAY_CARD 类名前缀", cards >= 1 and cards < loose,
            f"卡片 {cards} 张（不带 __ 会命中 {loose}）")

        # 4) 卡片锚点：CARD_SCOPE 认卡靠的那个 label
        for anchor in {a for a, _i in CARD_SCOPE.values()}:
            n = page.locator(_card(anchor)).locator("visible=true").count()
            add(f"卡片锚点「{anchor}」", n >= 1, f"命中 {n} 张卡")

        # 5) 同意勾选框。这里连语义锚点一起核 —— `_consent` 真正拦人的是文案那道。
        n = page.locator(CONSENT_BOX).locator("visible=true").count()
        add("CONSENT_BOX", n == 1, f"命中 {n}")
        if n == 1:
            try:
                near = page.locator(CONSENT_BOX).locator("visible=true").first.evaluate(
                    """(el) => {
                        for (let n = el, d = 0; n && d < 6; n = n.parentElement, d++) {
                            const t = (n.innerText || '').trim();
                            if (t) return t;
                        }
                        return '';
                    }"""
                )
            except Exception as exc:
                near = f"<读不到：{type(exc).__name__}>"
            hit = CONSENT_TEXT in (near or "")
            # 这一行红了可能是两件事：文案改了，或者 CONSENT_BOX 指到了别的框上。
            # 后者的数命中数是 1（合法），只有这道语义锚点认得出来 —— 上一轮
            # 勾错「没有实习经历」就是这么漏过去的。所以两种可能都得写出来。
            add(f"CONSENT_TEXT「{CONSENT_TEXT}」", hit,
                f"框旁文案：{(near or '')[:40]}" if hit else
                f"框旁读到的是「{(near or '')[:30]}」—— 要么文案改了，"
                f"要么 CONSENT_BOX 指错了框")

        # 6) 提交按钮
        n = page.locator(f"button:has-text('{SUBMIT_TEXT}')").count()
        add(f"SUBMIT_TEXT「{SUBMIT_TEXT}」", n == 1, f"命中 {n}")

        # 7) 下拉选项全称。这是最容易烂的一条：`PICK_OPTION_TEXT` 存的是页面上
        #    的原文，站点改一个字（比如「+86 （中国大陆）」的全角括号换半角）
        #    就命中 0，而 `_pick` 只会在 execute 前抛，之前一路都是绿的。
        for key, label, selector, action in FORM_FIELDS:
            if action != "pick":
                continue
            want = PICK_OPTION_TEXT.get(key) or FIXED_VALUES.get(key)
            if not want:
                continue                    # 值来自画像，体检时无从得知
            try:
                anchor, idx = CARD_SCOPE.get(key, (None, 0))
                box = self._one(page, selector, label,
                                _card(anchor) if anchor else None, idx)
                box.scroll_into_view_if_needed()
                box.click(timeout=self.timeout)
                page.wait_for_timeout(900)
                hits = page.locator(
                    f'{SELECT_ITEM}:has-text("{want}")'
                ).locator("visible=true").count()
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception as exc:
                add(f"选项「{want}」", False, str(exc).split("\n")[0][:100])
                continue
            add(f"选项「{want}」", hits == 1, f"可见命中 {hits}")

        return out

    def checkup(self, job: dict) -> list[tuple[str, bool, str]]:
        """走到表单、核判据、关浏览器。**一个字都不填，不提交。**"""
        from ..profile import from_dict

        plan = self.prepare(job, from_dict({}), fill_fields=False)
        if plan.status == "blocked" or not plan.confirm_token:
            return [("走到表单", False, plan.blocker or "prepare 没到表单")]

        session = SESSIONS.peek(plan.confirm_token)
        try:
            rows = [("走到表单", True, "")] + self.check_selectors(session.page)
        finally:
            SESSIONS.discard(plan.confirm_token)
        return rows

    def label_drift(self, page: Page) -> list[str]:
        """FORM_FIELDS 的守卫：返回页面上**找不到**的 label 列表，空表示都在。

        **为什么需要它**：这张表的选择器全部锚在中文 label 上（那些 input 没有
        id 也没有 name，class 是跨字段通用的，label 是唯一能区分字段的东西）。
        字符串级判据换季会静默失效 —— 站点把「学校名称」改成「毕业院校」，选择器
        命中 0，`_fill` 里每个字段都吞掉异常记一句「填写失败」，流程照样往下走，
        最后交出一张**空表单**加一句「已填 0 个字段」。那比报错难发现得多。

        这个方法把它变成一个可断言的列表。CI 或定期体检里跑：

            drift = sub.label_drift(page)
            assert not drift, f"这些 label 在页面上没了: {drift}"

        注意它只查 label 在不在，不查「label 对应的控件还是不是原来那种」——
        后者要真填一次才知道，那是 execute 前的回读摘要在管的事。

        查的是**选择器里真正锚定的那个文案**，不是展示名。这两个可以不一样：
        「手机区号」在页面上没有自己的 label，它锚在「手机号码」上。按展示名查
        会报一个不存在的漂移，然后 prepare 把没坏的东西当坏的处理。
        """
        missing = []
        for _key, _label, selector, _action in FORM_FIELDS:
            for anchor in _ANCHOR_RE.findall(selector):
                if not page.locator(f'label:text-is("{anchor}")').count():
                    if anchor not in missing:
                        missing.append(anchor)
        return missing

    def _readback_digest(self, page: Page, plan: SubmissionPlan) -> str:
        """把页面上**现在**的值读回来，按同样规则算摘要。

        不是拿 plan.digest() 和自己比（那永远相等，等于没校验）。

        pick 的字段不能用 input_value 读 —— 那个 combobox 的 value 是空的，
        选中的值显示在 `.ud__select__selector` 的文本里，所以按动作分流。

        **读必须和写走同一条寻址**（同一个 `_one`、同一个 scope）。读第 1 条、
        写第 2 条的话，digest 比的是另一张卡的值 —— 对不上是虚警，对上了更糟，
        那是拿一张没动过的卡替被改过的卡背书。
        """
        snapshot = plan.model_copy(deep=True)
        for f in snapshot.fields:
            if f.action in ("skip", "upload") or not f.filled:
                continue
            try:
                loc = self._one(page, f.selector, f.label,
                                f.scope, f.scope_index)
                if f.action == "pick":
                    f.value = loc.inner_text(timeout=3000).strip() or None
                else:
                    f.value = loc.input_value(timeout=3000) or None
            except Exception:
                f.value = "__unreadable__"
        return snapshot.digest()

    # ---------- 浏览器与截图 ----------

    def _launch(self, pw) -> tuple[Page, Callable[[], None]]:
        """开浏览器，返回 page 和收尾函数。

        用 start()/stop() 而不是 with sync_playwright()：两阶段之间浏览器
        得活着，with 一出块就把页面关了。
        """
        browser = None
        if self.user_data_dir:
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

    def _login_host_url(self, url: str) -> str:
        """把 apply_url 的 host 换成该租户能登录的 host。表里没有就原样返回。

        只换 netloc，path/query/fragment 全部保留 —— 实测同路径同岗位 ID 在两个
        host 上都有效，所以这是纯 host 级改写，不需要知道路径形状。
        """
        target = self.LOGIN_HOSTS.get(self.tenant)
        if not target:
            return url
        parts = urlsplit(url)
        if parts.netloc == target:      # 已经是目标 host，别重复改
            return url
        return urlunsplit(parts._replace(netloc=target))

    def flow_init_status(self, host: str | None = None) -> int:
        """探 `POST /accounts/flow/init` 的状态码 —— LOGIN_HOSTS 那张表的守卫。

        **为什么要这个**：LOGIN_HOSTS 是一条「我们相信 host A 能登录、host B 不能」
        的判断，站点改回来或再改一次，它就静默失效 —— 而失效的表现是「浏览器打开一片
        空白」，那个现象和网络慢、和选择器过期长得一模一样，排查一次要花掉今天这么多
        时间。这个方法把它变成一个数字。

        判据不认任何中文文案（换季不会漂），也不需要浏览器（httpx 裸发就行）：
          404 -> 该 host 上没这条路由，登录控件必然渲染不出来
          400 -> 端点在（400 只是因为我们发的空 body 不合法），host 可用
          200 -> 端点在且接受了请求

        用法：CI 或定期体检里断言目标 host 不是 404。真变了就会在这里响，
        而不是等到用户对着空白页等两分钟。
        """
        import httpx

        h = host or self.LOGIN_HOSTS.get(self.tenant) or ""
        if not h:
            raise ValueError(f"租户 {self.tenant} 没有登录 host 可探")
        return httpx.post(f"https://{h}/accounts/flow/init", json={}, timeout=20).status_code

    def _shot(self, page: Page, job_id: str, stage: str) -> str:
        """分阶段存截图。存证 + 下一轮探表单的输入。

        **要截到整个表单，而 `full_page=True` 在这里没用。** 实测字节的投递页
        document 高度恒等于视口（720px），表单装在一个内部滚动的
        `section.midas-customized-transition-scroll-element` 里，真实高度 3931px，
        而且从它到 <html> 一共四层全部锁死在 720px。full_page 按文档高度截，于是
        每次都只拿到首屏 —— 导航栏加半个「简历」框，22 个字段一个都看不到。

        做法：沿 parentElement 把整条链的 height/maxHeight/overflow 放开、把滚动
        容器撑到 scrollHeight，截完再还原。改的是内存里的 inline style，不碰站点、
        不提交任何东西。找不到滚动容器就直接 full_page（别的租户可能是正常文档流）；
        撑开这步抛异常也要退回去截一张首屏 —— 截图失败不该中断投递流程。
        """
        d = Path("screenshots")
        d.mkdir(exist_ok=True)
        path = d / f"feishu_{self.tenant}_{stage}_{job_id}_{int(time.time())}.png"
        try:
            found = page.evaluate(_EXPAND_JS)
            if found:
                page.wait_for_timeout(300)      # 撑开后等重排
            page.screenshot(path=str(path), full_page=True)
            if found:
                page.evaluate(_RESTORE_JS)
        except Exception:
            try:                      # 撑开这步炸了也要留一张首屏
                page.screenshot(path=str(path), full_page=True)
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
    # 站点改版时这几个最先失效，保持独立小函数方便单测和快速替换。

    def _is_page_missing(self, page: Page) -> bool:
        """页面是不是「不存在」。

        **判据必须是渲染后的正文，不能是 HTTP 状态码。** 这些页面是客户端渲染
        的 SPA，不存在的路由照样回 200 且 body 有 200KB（实测 nio 209298 字节）。
        当初 apply_url 少了 /detail 而没被发现，就是因为只看了状态码。
        """
        return page.locator("text=/页面不存在|页面已下线/").count() > 0

    def _is_job_closed(self, page: Page) -> bool:
        """岗位是否已关闭。"""
        return page.locator("text=/已停止|已下线|已结束|停止招聘/").count() > 0

    def _need_login(self, page: Page) -> bool:
        """是否撞上登录门。

        判据是**登录控件的实际元素**，不是 URL，也不主要靠文案：

        - URL 不能用。改 host 之后（见 LOGIN_HOSTS）字节的登录表单是**内联渲染
          在 /campus/resume/<id>/apply 上**的，URL 全程不含 /login —— 拿 URL 判会
          直接漏掉登录门，然后把登录表单当成投递表单去填。
        - 三个选择器，任一命中即算。**`input#password` 是最硬的一条** —— 投递表单
          不会问密码。2026-08-10 只用 `#code` 判过一版，漏了：jobs.bytedance.com
          的登录页**默认落在「邮箱+密码」tab**，`#code` 要点了「手机号码登录」
          才出现，于是登录页被判成「不需要登录」、还被 _form_ready 当成投递表单。
        - `input#code`（验证码框）覆盖手机号 tab；`.atsx-phone-input` 覆盖飞书标准
          控件（nio/xiaopeng 直接就是手机号 tab）。
        - 文案判据留着兜底，但排在最后 —— 认中文字符串换季会静默失效。

        看得见才算：控件容器可能在 DOM 里但整个隐藏（字节 feishu 镜像那个空容器
        就是），所以用 :visible。
        """
        for sel in (
            "input#password:visible",
            "input#code:visible",
            "input.atsx-phone-input:visible",
        ):
            if page.locator(sel).count():
                return True
        return page.locator("text=/获取验证码|手机号登录|扫码登录/").count() > 0

    def _switch_to_phone_login(self, page: Page) -> bool:
        """把登录 tab 切到「手机号码登录」，并设置区号为 +86。

        为什么要切：jobs.bytedance.com 默认落在邮箱+密码 tab，而校招账号一般没有
        密码，只能走手机号+验证码。不切的话人要自己找那个 tab。

        为什么设置区号：默认是 +1（美国），中国用户每次都要改。设置成 +86 省一步。

        **只点 tab + 设置区号，不碰凭据** —— 手机号和验证码始终由人自己输。
        切不过去或设置失败也不判死：人可以自己操作，所以只回 False 不抛。
        """
        if page.locator("input#code:visible, input.atsx-phone-input:visible").count():
            already_on_phone_tab = True
        else:
            tab = page.locator("text=/手机号码登录|手机号登录/")
            if not tab.count():
                return False
            try:
                tab.first.click(timeout=5000)
                page.wait_for_selector(
                    "input#code:visible, input.atsx-phone-input:visible", timeout=10000
                )
                already_on_phone_tab = True
            except PlaywrightTimeout:
                return False

        # 切到手机号 tab 后，设置区号为 +86。
        # 常见形状：下拉框（select）或可点击的区号显示（span/div）
        if already_on_phone_tab:
            try:
                # 先试下拉框（一些表单用原生 select）
                select = page.locator("select[name*='country'], select[name*='area']").first
                if select.count():
                    select.select_option(label="+86")
                else:
                    # 再试自定义下拉（点开后选 +86 那项）
                    trigger = page.locator("text=/^\\+1$|^\\+86$/").first
                    if trigger.count():
                        trigger.click(timeout=3000)
                        page.wait_for_timeout(500)
                        option = page.locator("text=/\\+86|中国大陆|China/").first
                        if option.count():
                            option.click(timeout=3000)
            except Exception:
                # 设置失败不阻断 —— 用户自己改也行
                pass

        return True

    def _form_ready(self, page: Page) -> bool:
        """投递表单是否已经渲染出来（区别于登录表单和 loading 页）。

        「有输入框」不够 —— 登录表单也有输入框（手机号/验证码/勾选框，实测 3 个），
        导航栏搜索框也是输入框。用数量卡阈值分不开这三件事。

        所以要求：
        1. 登录门不在
        2. loading 转圈动画不在（投递表单加载中）
        3. 有可见输入框

        转圈判据：飞书的 loading 容器通常有「loading」或「spin」类名，或者
        用 SVG circle 画转圈动画。
        """
        if self._need_login(page):
            return False
        # 判 loading：常见类名 + SVG 转圈
        if page.locator("[class*='loading'], [class*='spin'], svg circle[class*='circular']").count():
            return False
        # 至少 3 个可见输入框才算投递表单（导航栏搜索框 1 个 + 表单至少 2 个）
        return page.locator("input:visible, textarea:visible, select:visible").count() >= 3

    def _is_success(self, page: Page) -> bool:
        """提交是否成功。**文案未实测** —— 等真提交过一次再核。"""
        return page.locator("text=/投递成功|提交成功|已提交|申请成功/").count() > 0

    def _is_duplicate(self, page: Page) -> bool:
        """是否重复投递。**文案未实测** —— 等真提交过一次再核。"""
        return page.locator("text=/已投递|重复投递|已经投递|已申请/").count() > 0
