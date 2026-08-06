"""ATS（招聘系统）识别与路由。

为什么适配层按「招聘系统 × 租户」组织，而不是按公司：

大厂自建，一家一套，适配器和公司一一对应，这部分躲不掉。但中厂大多买 SaaS——
北森、Moka、用友大易这些。**同一个 ATS 上几十家公司的招聘前端是同一套应用**，
只是租户配置不同：接口路径一样、字段名一样、表单控件一样，变的只有一个租户标识。
一个适配器覆盖一批公司，边际成本接近零。按公司建适配器等于把同一份逻辑抄几十遍。

调研结论（2026-08，见 docs/ATS_RESEARCH.md）：**厂商的开放 API 这条路是关的。**
Moka 开放平台走 OAuth 2.0，clientId/clientSecret 由厂商 CSM 线下发给测评、视频面试
这类第三方服务商，没有任何公开岗位端点，也没有免鉴权路径，租户靠 appId 区分而不是
子域名。北森（iTalent）是同类白标 SaaS。所以采集和投递都只能走公开招聘前端。

这里的 domains 是核实过的，dom_hints 是**没核实的猜测**，等 scripts/probe_ats.py
实测真实页面后再替换——猜测写死在代码里比留空更危险，所以标注清楚。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# 判定可信度。域名命中可以直接路由；标记命中只能算线索，要人确认过再落库。
DOMAIN = "domain"
MARKUP = "markup"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Vendor:
    """一个招聘系统厂商。"""

    key: str
    name: str
    domains: tuple[str, ...] = ()        # 核实过的域名，命中即可判定
    dom_hints: tuple[str, ...] = ()      # 未核实的页面标记猜测，probe 用来验证
    self_built: bool = False
    campus_path_hints: tuple[str, ...] = ()
    tenant_seg: int | None = None        # 租户在第几个路径段（已核实的才填）
    notes: str = ""


VENDORS: tuple[Vendor, ...] = (
    Vendor(
        key="mokahr", name="Moka",
        domains=("mokahr.com", "mokahr.cn"),
        dom_hints=("mokahr", "moka-", "__MOKA__"),
        campus_path_hints=("/campus_apply/", "/campus-recruitment/", "/campus/"),
        notes="app.mokahr.com / api.mokahr.com 已核实；租户在路径里，格式待 probe 确认。"
              "hireMode 1=社招 2=校招（来自开放平台文档）",
    ),
    Vendor(
        key="beisen", name="北森 iTalent",
        # zhiye.com 是 2026-08 实测加进来的租户域名，<tenant>.zhiye.com。
        # 原来这张表里只有厂商官网，所以 job.youzan.com 302 到 youzan.zhiye.com
        # 之后只能靠页面标记命中，判成 markup 级、不可路由。
        domains=("beisen.com", "italent.cn", "zhiye.com"),
        # 原来这里是裸词 "beisen"，实测在字节自建页面上误命中了：那页有
        # FeatureDailyExamUseBeisen / BeisenExamAccount 这类自家灰度开关
        # （字节自建 ATS 只是接了北森的在线考试）。裸品牌词太松，改成带点号的串。
        # bstatics.com 是北森的静态资源域（acdn.bstatics.com/ux/beisen-common/），
        # 带点号、不是裸品牌词，符合这一列的收录规则。
        dom_hints=("beisen.com", "italent.cn", "talent.beisen", "bstatics.com"),
        campus_path_hints=("/campus/", "/school/"),
        notes="www.beisen.com 已核实（2026-08 实测，官网自带 italent.cn 串）；"
              "租户域名 <tenant>.zhiye.com 已核实：job.youzan.com 302 到 "
              "youzan.zhiye.com，页面自称「有赞招聘」，静态资源走 tcdn.bstatics.com "
              "和 acdn.bstatics.com/ux/beisen-common/，图片走 stcms.beisen.com。"
              "反向对照：qwerasdfzxcv0000 / nonexistent-tenant-xyz99 / xiaopeng "
              "三个非租户子域名返回**字节完全相同**的软 404（200、1228B、同一 sha、"
              "跳 /404?errorpath=/），所以 200 本身不算证据，内容不同才算。"
              "注意口径：已核实的是「这些页面由北森的前端和资源链路提供」，"
              "不是「zhiye.com 这个域名归北森所有」——白标/代理的可能性从外部排除不了，"
              "但对采集解析而言前者才是操作性事实。租户页 fetch() 仍未写（n=1，格式不敢外推）",
    ),
    Vendor(
        key="feishu", name="飞书招聘（字节）",
        # 实测捞出来的，不是查资料查到的：campus.xiaopeng.com 302 到
        # xiaopeng.jobs.feishu.cn。原来整张表里没有这家，于是小鹏被判成
        # 「无厂商线索、大概率自建」——正好判反。国内中厂这块它是主力之一。
        # 原来这里是 ("jobs.feishu.cn", "feishu.cn")，裸 feishu.cn 是个 domain 级的错：
        # 飞书是一整套办公套件，feishu.cn 底下绝大多数子域名跟招聘毫无关系。
        # 实测 `abcde.feishu.cn/docx/...`（一篇飞书文档）被判成
        # system=feishu / tenant=abcde / domain / routable=True——
        # 也就是把一篇文档当成「abcde 这家公司的招聘入口」交给采集器。
        # ATS 只在 <tenant>.jobs.feishu.cn 上，收窄到这一个后缀，
        # 子域名匹配照样能命中 xiaopeng.jobs.feishu.cn。
        domains=("jobs.feishu.cn",),
        # feishucdn.com 拿掉了：那是字节所有产品共用的 CDN，不是招聘专用。
        # 实测它在 jobs.bytedance.com（字节自建招聘页）上命中，把字节报成
        # 「命中第三方 ATS」——而飞书本来就是字节自己的产品，这是**第一方**依赖，
        # 不是「采用了别人家的 ATS」。跟裸 "beisen" 误命中字节是同一个毛病：
        # 品牌级的串太宽，够不上判据。留下的 jobs.feishu.cn 是招聘专用的。
        dom_hints=("jobs.feishu.cn",),
        campus_path_hints=("/campus/", "/index/"),
        notes="<tenant>.jobs.feishu.cn，租户在子域名，已实测（小鹏 2026-08）。"
              "只收 jobs.feishu.cn 这一个后缀：裸 feishu.cn 会把飞书文档"
              "（abcde.feishu.cn/docx/…）判成某家公司的招聘入口。"
              "www.feishu.cn 官网因此不再命中，这是有意的——官网不是岗位源。"
              "larksuite.com 海外版未核实",
    ),
    Vendor(
        key="dayee", name="用友大易",
        domains=("dayee.com",),
        dom_hints=("dayee.com",),
        notes="www.dayee.com 已核实（2026-08 实测）；租户页格式待实测",
    ),
    Vendor(
        key="workday", name="Workday",
        domains=("myworkdayjobs.com", "myworkdaysite.com", "workday.com"),
        # 裸词 "workday" 会命中同行的对比营销页（实测在 lever.co 官网上就命中了），
        # 只留区分度高的串。
        dom_hints=("myworkdayjobs.com", "myworkdaysite.com", "workdaycdn", "wd1.", "wd3.", "wd5."),
        notes="子域名形如 <tenant>.wd3.myworkdayjobs.com，租户可从子域名直接取",
    ),
    Vendor(
        key="successfactors", name="SAP SuccessFactors",
        domains=("successfactors.com", "successfactors.eu", "sapsf.com"),
        dom_hints=("successfactors", "sapsf"),
    ),
    Vendor(
        key="taleo", name="Oracle Taleo",
        domains=("taleo.net",),
        dom_hints=("taleo",),
    ),
    Vendor(
        key="greenhouse", name="Greenhouse",
        # greenhouse.com 是实测补的：www.greenhouse.io 现在 301 到 www.greenhouse.com
        # （改名了），只写 .io 的话官网本身都判不出来，只能靠标记兜底。
        # 老的 boards.greenhouse.io 还在服务，两个都得留。
        domains=("greenhouse.io", "greenhouse.com"),
        dom_hints=("greenhouse.io", "boards.greenhouse", "grnhse"),
        # boards.greenhouse.io/<tenant> —— 这两家的租户在路径首段，是公开文档写明的，
        # 不是猜的，所以敢填 tenant_seg。国内几家没填，等 probe 实测。
        tenant_seg=0,
    ),
    Vendor(
        key="lever", name="Lever",
        domains=("lever.co",),
        dom_hints=("lever.co", "jobs.lever"),   # 裸 "lever" 会命中 "leverage"
        tenant_seg=0,          # jobs.lever.co/<tenant>
    ),
    Vendor(
        key="tencent_join", name="腾讯招聘（自建）",
        domains=("join.qq.com",), self_built=True,
        notes="国内校招入口，已有适配器",
    ),
    Vendor(
        key="tencent_careers", name="腾讯招聘社招（自建）",
        domains=("careers.tencent.com",), self_built=True,
    ),
)

BY_KEY = {v.key: v for v in VENDORS}

# 子域名里这些词不是租户名，是厂商自己的功能域。
# join / boards 是实测补上的：join.qq.com 取出 tenant="join"、
# boards.greenhouse.io 取出 tenant="boards"，两个都是厂商自己的主机名。
# 这种「取到了但取错了」比取不到更坏——route_key 会变成 greenhouse:boards，
# 于是所有 greenhouse 公司共用一个适配器条目，还看不出哪里错了。
_GENERIC_SUBDOMAINS = frozenset({
    "app", "api", "www", "talent", "jobs", "job", "career", "careers",
    "hr", "recruit", "recruitment", "campus", "join", "boards", "board",
    "apply", "m", "static", "cdn", "web", "hire", "hiring",
})
_SLUG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,40}$")

# dom_hints 里允许出现的「无点号」标记白名单。
#
# 一条 hint 合格的标准只有一个：**它会不会出现在跟这家厂商无关的页面上**。
# 带点号或连字符的串天然合格（域名片段、资源前缀、代码标识符），
# 剩下的必须在这张白名单里，加之前先问一遍上面那个问题。
#
# 这几个为什么算合格：
#   workdaycdn / grnhse / sapsf —— 基础设施和静态资源前缀，只有自家页面会带。
#   mokahr / taleo / successfactors —— 厂商自造词，不是英文单词，正文撞不上。
#
# 反面例子，一律不许进表（都是实测踩过的）：
#   greenhouse、workday —— 普通英文词，同行对比页上就有（lever.co 官网同时命中这两个）。
#   lever —— 是 leverage 的子串。
#   beisen —— 会撞进别人家的标识符：字节自建招聘页上有 FeatureDailyExamUseBeisen
#             （它自建 ATS，只是接了北森的在线考试），于是字节被判成北森。
COINED_HINTS = frozenset({
    "mokahr", "workdaycdn", "grnhse", "sapsf", "taleo", "successfactors",
})


@dataclass
class Detection:
    """一次识别的结果。故意把「判定」和「证据」分开存。

    evidence 是给人看的：判错的时候要能一眼看出是哪条特征骗了它。
    """

    system: str = UNKNOWN
    tenant: str | None = None
    confidence: str = UNKNOWN
    evidence: list[str] = field(default_factory=list)

    @property
    def routable(self) -> bool:
        """够不够格直接用来路由。域名命中才算，标记命中要人确认。"""
        return self.system != UNKNOWN and self.confidence == DOMAIN

    @property
    def route_key(self) -> str:
        """适配器注册表的键：系统 + 租户。"""
        return f"{self.system}:{self.tenant}" if self.tenant else self.system


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def detect_from_url(url: str) -> tuple[str, list[str]]:
    """按域名判 ATS。这是唯一可以直接拿来路由的信号。"""
    host = host_of(url)
    if not host:
        return UNKNOWN, []
    for v in VENDORS:
        for d in v.domains:
            if host == d or host.endswith("." + d):
                return v.key, [f"host={host} 命中 {v.key} 域名 {d}"]
    return UNKNOWN, []


def detect_from_html(html: str) -> tuple[str, list[str]]:
    """按页面标记猜 ATS。只当线索，不足以直接路由。

    hints 目前是猜的，所以这个函数的产出必须标成 MARKUP，
    由 probe 汇总成真实特征表之后再谈收紧。
    """
    if not html:
        return UNKNOWN, []
    low = html.lower()
    for v in VENDORS:
        for hint in v.dom_hints:
            if hint.lower() in low:
                return v.key, [f"页面出现标记 {hint!r} → 疑似 {v.key}"]
    return UNKNOWN, []


def tenant_from_url(url: str, system: str) -> str | None:
    """从 URL 里取租户标识。取不到就返回 None，不要编。

    两种取法，按可靠性排：
      子域名  <tenant>.wd3.myworkdayjobs.com → tenant
      路径段  /campus_apply/<tenant>/...     → tenant

    取不到返回 None 是有意的：注册表可以先按系统落一条、租户留空，
    等 probe 实测出格式再补。猜一个错的租户比留空难查得多。
    """
    v = BY_KEY.get(system)
    if v is None:
        return None

    # 自建系统天生只有一个租户，取租户没有意义。
    # 不 return 就会从 join.qq.com 里抠出 tenant="join"，
    # route_key 变成 tencent_join:join——多出来的那半截是纯噪声。
    if v.self_built:
        return None

    host = host_of(url)
    if host:
        parts = host.split(".")
        if len(parts) > 2 and parts[0] not in _GENERIC_SUBDOMAINS and _SLUG.match(parts[0]):
            return parts[0]

    segs = [s for s in urlparse(url).path.split("/") if s]

    # 路径首段就是租户的（greenhouse / lever），只有核实过格式的厂商才走这条。
    if v.tenant_seg is not None and v.tenant_seg < len(segs):
        seg = segs[v.tenant_seg]
        if _SLUG.match(seg) and seg not in _GENERIC_SUBDOMAINS:
            return seg

    for hint in v.campus_path_hints:
        marker = hint.strip("/")
        if marker in segs:
            i = segs.index(marker)
            if i + 1 < len(segs) and _SLUG.match(segs[i + 1]):
                return segs[i + 1]
    return None


def detect(url: str, html: str = "") -> Detection:
    """识别一个招聘入口。域名优先，页面标记兜底。"""
    system, evidence = detect_from_url(url)
    if system != UNKNOWN:
        return Detection(system, tenant_from_url(url, system), DOMAIN, evidence)

    system, evidence = detect_from_html(html)
    if system != UNKNOWN:
        # 标记命中时不猜租户：连系统都还没确认，租户更不可能对。
        return Detection(system, None, MARKUP, evidence)

    return Detection(UNKNOWN, None, UNKNOWN, [f"host={host_of(url)} 未匹配任何已知 ATS"])
