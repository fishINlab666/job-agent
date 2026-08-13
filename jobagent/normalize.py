"""归一化规则。

这里是整个项目最容易出错、也最该被测试覆盖的地方。
核心难点：源站的岗位族分类和用户心里的岗位族不是一回事。

腾讯把「产品运营」「内容运营」「行业运营」全放在 positionFamily=3（产品）下面，
但对一个想投运营岗的用户来说，运营和产品是两个不同的选择。
所以归一化必须「源站族 + 标题关键词」两层叠加，不能只信源站。
"""
from __future__ import annotations

import hashlib
import json
import re

# 归一后的岗位族取值（jobs.job_family 的合法值）
FAMILIES = (
    "tech", "product", "operations", "design",
    "marketing", "sales", "hr", "finance", "legal", "other",
)

# ---------------------------------------------------------------------------
# 中文岗位名的结构规律：「域 + 职能」，域在前、职能在后。
#   产品运营 = 域(产品) + 职能(运营)      → 运营
#   产品体验设计 = 域(产品体验) + 职能(设计) → 设计
# 朴素的「先匹配到的赢」会让域词抢走判定，所以规则必须分层：
#   第 0 层 强信号：出现研究/工程标记的，无论域词是什么都是技术岗。
#           挡住「腾讯营销—广告推荐基础大模型」被判成市场、
#           「混元多模态-视觉编码器技术研究」被判成设计。
#   第 1 层 复合职能：职能词容易被域词抢走的特例，优先判。
#   第 2 层 常规职能：按职能词判。
# ---------------------------------------------------------------------------

# 第 0 层：技术强信号。刻意不含裸「研究」（会误伤「用户研究」）
# 和裸「技术」（会误伤「技术产品经理」）。
TECH_MARKERS: tuple[str, ...] = (
    "大模型", "大语言模型", "多模态", "算法", "强化学习", "机器学习", "深度学习",
    "智能体", "Agent", "编码器", "微架构", "自动生成", "数据构建", "数据压缩",
    "技术研究", "AI原生", "NLP", "推荐系统", "开发", "工程师", "架构", "运维",
    "测试", "编译", "内核", "芯片", "驱动",
)

# 第 1 层：复合职能特例。域词在前、职能在后，必须比第 2 层先判。
COMPOUND_RULES: list[tuple[tuple[str, ...], str]] = [
    (("员工福利", "员工关系", "薪酬", "招聘"), "hr"),
    (("物业", "办公规划", "行政"), "other"),
    (("投资", "风险管理", "合规", "审计", "财务", "财经"), "finance"),
    (("体验设计", "产品设计", "视觉设计", "交互设计"), "design"),
]

# 第 2 层：常规职能词。
TITLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("运营", "发行"), "operations"),
    (("设计", "美术", "视觉", "交互", "动效", "动画", "特效", "GUI", "多媒体"), "design"),
    (("产品", "策划", "项目管理", "产品经理"), "product"),
    (("市场", "营销", "公关", "品牌", "商务拓展", "商业分析", "战略", "用户研究"), "marketing"),
    # `顾问` 是面客销售/咨询岗。加它救回 233 行 / 21 个岗位型（2026-08-13 快照），改判 0 行
    # （加进这一组和加在表末尾，改判数都是 0 —— 说明它和别的族没有交叠）。
    # 已知误判 1 个岗位型：`AI时尚顾问实习生-广告业务` 里 `顾问` 是头衔不是职能。
    # 不为这 1 行加例外 —— 例外规则是下一个 issue #9 的原料。见方案 017 §3。
    (("销售", "客户成功", "解决方案", "顾问"), "sales"),
    (("人力", "HR"), "hr"),
    (("法务", "法律", "公共策略"), "legal"),
    # 「研发」「后端」在这一层而**不在第 0 层**，是量过的：
    # 放第 0 层同样救回 85 条，但会改判 69 条已经判对的，因为第 0 层盖住所有
    # 其他规则。典型受害者 ——
    #   研发技术类组织招聘 - 人力与管理部      hr → tech（招研发的 HR 岗，判错）
    #   后端研发实习生（运营平台方向）-国际支付   operations → tech
    # 放这一层则「域词在前、职能在后」的结构还是先赢，0 改判。
    #
    # 为什么单独列出这两个词：这一层原本有「开发」「后台」，没有「研发」「后端」，
    # 于是 `后端开发工程师` 判 tech、`后端研发实习生` 判 None —— 同一个岗位换个
    # 说法就掉出分类。字节 79 条、小鹏 3 条、商汤 2 条都是这么丢的。
    (("数据", "安全", "硬件", "模型", "后台", "后端", "前端", "客户端",
      "研发"), "tech"),
    # `采购` 组**必须是最后一条**，且**不能挪进 COMPOUND_RULES**。
    # 量过：进 COMPOUND_RULES（第 2 层之前）会改判 5 条 ——
    #   AI产品经理（采购方向）      product → other
    #   采购政策与合规              finance → other
    #   硬件采购实习生              tech    → other
    # 因为 `采购方向` 里 `采购` 是**域词**（岗位作用在什么上），职能是产品/财务/硬件。
    # 放在这一层末尾则改判 0：所有具体职能都没命中，才该落到「供应链」这个兜底。
    # 归 other 而不是新开 supply_chain：FAMILIES 是画像里 families 的取值域，
    # 加一个族要动画像口径，而 other 已经承担同类角色（物业/行政在 COMPOUND_RULES
    # 里也判 other）。见方案 017 §6。
    (("采购",), "other"),
]


# 第 1.5 层用的两张表。**语义不同**，不要合并、也不要互相补词：
#   域词 = 岗位作用在什么东西上（视觉、多媒体）
#   职能词 = 岗位干什么活（后端、研发）
# 第 2 层 design 组把这两类混在一条规则里（`设计`/`美术` 是职能，`视觉`/`多媒体`
# 是域），这正是 issue #9 的病根 —— `多媒体客户端研发实习生` 因为含 `多媒体` 就
# 判 design，而它其实是个技术岗。这一层只认「域在前、职能在后」这一个方向。
#
# 为什么不是从第 2 层 design 组里摘词：摘 `多媒体` 只修好 3/6，还会造出
# design→product；再摘 `视觉` 会让 `计算机视觉实习生-电商业务` 判成 None ——
# 没有族比判错族更难发现，飞书那边没有兜底。见 docs/plans/015。
DESIGN_DOMAIN_WORDS: tuple[str, ...] = (
    "视觉", "交互", "动效", "动画", "特效", "GUI", "多媒体",
)

# 这里**故意不含** `数据`/`安全`/`硬件`/`模型`：它们在第 2 层 tech 组里，但补到这
# 张表会把 `视觉生成策略运营（图片美感方向）实习生-AI数据与安全` 这类 3 条运营岗
# 判成技术，破模块头号不变量。也**故意不含** `设计`/`美术`：那是职能词表里的域词
# 反串，会让 `设计师（前端方向）` 判 tech。
TECH_FUNCTION_WORDS: tuple[str, ...] = (
    "后台", "后端", "前端", "客户端", "研发",
)


def _first_index(title: str, words: tuple[str, ...]) -> int | None:
    """命中最靠前那个词的下标；没命中返回 None。

    **返回 0 是合法命中**，调用方必须与 None 区分。目标 6 条标题里有 5 条域词在
    下标 0（`多媒体客户端研发实习生…`），写 `if d and f` 会漏掉整类。
    """
    hits = [title.index(w) for w in words if w in title]
    return min(hits) if hits else None


def family_from_title(title: str) -> str | None:
    """标题 → 归一岗位族。返回 None 表示判不出，由调用方用源站族兜底。"""
    if any(m in title for m in TECH_MARKERS):
        return "tech"
    for keywords, fam in COMPOUND_RULES:
        if any(k in title for k in keywords):
            return fam
    # 第 1.5 层：设计域词 + 技术职能词，且**域词在前** → 技术岗。
    # 位置必须在 COMPOUND_RULES **之后**：放前面会把 `交互设计前端实习生`、
    # `视觉设计客户端实习生` 判成 tech（那两条有显式复合短语 `交互设计`/`视觉设计`，
    # 显式短语优先于位置启发式）。真数据上两个位置无差别（0 条冲突），只能靠
    # tests/test_normalize.py 里的构造用例钉住。
    #
    # 顺序判据不能弱化成「两类词都在」：`后台动画设计师`、`客户端UI动效设计`、
    # `后端图形界面GUI设计` 三条都同时含两类词，但技术词在前时它是修饰语，职能仍是
    # 设计。用 `<` 而不是 `<=`：同下标意味着同一个词同时在两张表里，那是词表配错了，
    # 不该在这里被当成命中（两表交集由测试断言为空）。
    domain_at = _first_index(title, DESIGN_DOMAIN_WORDS)
    function_at = _first_index(title, TECH_FUNCTION_WORDS)
    if domain_at is not None and function_at is not None and domain_at < function_at:
        return "tech"
    for keywords, fam in TITLE_RULES:
        if any(k in title for k in keywords):
            return fam
    return None


def normalize_city(raw: str) -> str:
    """深圳总部 → 深圳；中国香港 → 香港。"""
    c = raw.strip()
    if not c:
        return ""
    c = c.replace("总部", "")
    if c.startswith("中国") and len(c) > 2:
        c = c[2:]
    return c


def split_cities(raw: str | None) -> list[str]:
    """腾讯的 workCities 是空格分隔的字符串。"""
    if not raw:
        return []
    seen: list[str] = []
    for part in raw.replace(",", " ").replace("、", " ").split():
        c = normalize_city(part)
        if c and c not in seen:
            seen.append(c)
    return seen


# ---------------------------------------------------------------------------
# 「任意城市」的写法。这类值最坑：它非空，所以过不了「空就放过」的判断，
# 然后拿去和用户目标城市求交集，交集为空 → 岗位被丢掉。
# 一个写「全国」的运营岗，恰恰是最该推给用户的那种。
# ---------------------------------------------------------------------------
CITY_WILDCARDS: tuple[str, ...] = (
    "全国", "不限", "多地", "远程", "各地", "全球", "任意",
    "多城市", "居家", "remote", "anywhere", "flexible",
)


def is_city_wildcard(city: str) -> bool:
    """这个城市值是不是「哪都行」。

    用子串匹配而不是相等：源站会写「工作地点不限」「全国多地」「远程办公」。
    没有真实中国城市名包含这些词，所以子串不会误伤。
    """
    c = (city or "").strip().lower()
    return bool(c) and any(w in c for w in CITY_WILDCARDS)


def any_city_ok(cities: list[str] | None) -> bool:
    """岗位城市里只要有一个是通配值，就当作不限城市。"""
    return any(is_city_wildcard(c) for c in (cities or []))


# 届别：源站写法五花八门，"2027届" / "2026-2027年毕业" / "26/27届" / "不限"。
_YEAR_RANGE = re.compile(r"(20\d{2})\s*[-~—－至]\s*(?:20)?(\d{2})")
_YEAR = re.compile(r"20(\d{2})")
_TERM = re.compile(r"(\d{2})\s*届")
# 裸两位的区间和枚举："26-27届" 是区间，"26/27届" 是枚举。
# 分开处理是因为只认单个 _TERM 的话，"26/27届" 只能捞到 27，26 被悄悄丢掉。
_TERM_RANGE = re.compile(r"(?<!\d)(\d{2})\s*[-~—－至]\s*(\d{2})\s*届")
_TERM_LIST = re.compile(r"(?<!\d)(\d{2}(?:\s*[/、,，+&和或]\s*\d{2})+)\s*届")
_LIST_SEP = re.compile(r"[/、,，+&和或]")
# 中文词按子串匹配：字段值里这些词是连写的（「届别不限」「应届均可」）。
_GRAD_UNLIMITED = ("不限", "均可", "所有", "任意", "全部")
# ASCII 词必须按整词匹配。原先 "any" 也走子串，于是 `Anyscale 平台研发` 被判成
# 「不限届别」——也就是「任何届别都命中」，把「不知道」洗成了「确定命中」。
_GRAD_UNLIMITED_ASCII = re.compile(r"\bany\b")


def parse_grad_years(raw: str | None) -> list[str] | None:
    """届别文本 → 两位届别列表。三种返回值语义不同，调用方必须分开处理。

    None      = 没写或看不懂。**不代表不匹配**，代表信息不足。
    []        = 明确写了不限届别，任何届别都算命中。
    ["26",…]  = 明确的届别集合。

    把「看不懂」和「不匹配」区分开，是因为原来的写法一律按不匹配处理，
    结果是岗位悄悄消失，用户还以为对方没开这个岗。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    low = text.lower()
    if any(w in low for w in _GRAD_UNLIMITED) or _GRAD_UNLIMITED_ASCII.search(low):
        return []

    # 区间优先：区间里的年份用枚举规则去捞只会捞到两个端点，中间的届别会漏。
    for pat in (_YEAR_RANGE, _TERM_RANGE):
        if m := pat.search(text):
            start, end = int(m.group(1)) % 100, int(m.group(2))
            if 0 <= end - start <= 10:
                return [f"{y:02d}" for y in range(start, end + 1)]

    found = [m[-2:] for m in _YEAR.findall(text)]
    if not found and (m := _TERM_LIST.search(text)):
        found = [p.strip() for p in _LIST_SEP.split(m.group(1)) if p.strip()]
    if not found:
        found = _TERM.findall(text)
    if not found and re.fullmatch(r"\d{2}", text):
        found = [text]          # 库里已经存的裸 "27"

    seen: list[str] = []
    for y in found:
        if y not in seen:
            seen.append(y)
    return seen or None


def grad_years_from_title(title: str | None) -> list[str] | None:
    """岗位标题 → 届别集合。第二条观测通道，只在结构化字段没给届别时用。

    比 parse_grad_years 严一档，两处不同：

    1. **必须出现「届」字**才解析。标题是自由文本，裸年份在里面是歧义的——
       可能是招聘年度（「2026年秋季校园招聘」）、活动届次（「2026年校园大使」）、
       毕业届别（「2026届」）。字段名能定死语义，标题不能。
    2. **永不返回 `[]`**。「不限 / 所有 / 任意 / any」在标题里出现，多半跟届别无关
       （撞上过 `Anyscale 平台研发`、`全部业务线-数据分析`）。`[]` 的语义是
       「任何届别都命中」，返回它等于把「不知道」洗成「确定命中」，方向是错的。

    返回值只有两种：明确的届别集合，或者 None（信息不足）。
    """
    if not title or "届" not in title:
        return None
    return parse_grad_years(title) or None


def fingerprint(payload: dict) -> str:
    """岗位指纹：只覆盖「变了就该通知用户」的字段。

    刻意不含 description —— 招聘方常做无意义的文案微调，
    含进来会导致大量假 job_updated 事件，把真信号淹掉。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
