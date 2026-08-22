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
    (("销售", "客户成功", "解决方案"), "sales"),
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
    # `采购` 组**不能挪进 COMPOUND_RULES**，且必须在下面那 31 个职能词**之前**。
    # 017 原话是「必须是最后一条」，018 量过之后改成了这句：真实约束是层级，不是
    # 绝对位置。量的时候那 31 个词接在它后面只有 1 条标题的判定会变，而那条恰好因此判对 ——
    #   IoT采购履行经理（结构件方向） - AI算力基础设施   在采购前=tech / 在采购后=other
    # 职能是采购，`结构件` 是域，other 才对。见方案 018 §5、命令 `rule-order`。
    # 量过：进 COMPOUND_RULES（第 2 层之前）会改判 5 条 ——
    #   AI产品经理（采购方向）      product → other
    #   采购政策与合规              finance → other
    #   硬件采购实习生              tech    → other
    # 因为 `采购方向` 里 `采购` 是**域词**（岗位作用在什么上），职能是产品/财务/硬件。
    # 放在这一层则所有具体职能都没命中，才落到「供应链」这个兜底。
    #
    # 【017 这里原本写着「放在这一层末尾则改判 0」，那句话是错的，别照抄】
    # 「加在第 2 层末尾之后改判 0」对**任何**词都成立，因为这一层是首命中即返回：
    # 前面的规则先赢，能走到末尾的标题本来就判 None。拿刻意选错的词试就看出来了 ——
    #   实习生→legal 加在末尾：救回 270 条、改判 0 条
    #   运营→tech / 设计→finance / 产品→hr：同样 0 改判
    # 一个取值恒为 0 的判据等于没有判据。替代判据是**库级**比对（规则层之下还隔着
    # 源站族兜底，规则层「无变化」不等于库里无变化）：
    #   scripts/measure_family_gaps.py db-effect   → 第二行「已有族被改成另一个族」必须是 0
    # 见方案 018 §6、§9 命令 B/E。
    # 归 other 而不是新开 supply_chain：FAMILIES 是画像里 families 的取值域，
    # 加一个族要动画像口径，而 other 已经承担同类角色（物业/行政在 COMPOUND_RULES
    # 里也判 other）。见方案 017 §6。
    (("采购",), "other"),

    # ↓↓↓ 018 加的 30 个词。基数：399 个判不出族的岗位型（在架、库里 job_family
    # IS NULL、剥掉尾部城市名和 `大区` 段）。这 30 个词救回 173 行 / 110 个岗位型，
    # 剩 289 个判不出（72.4%）。库级改族 0，规则层改判 0。
    # （量的时候是 31 个，`仓储` 边际贡献为 0 被删掉，见下面 other 组那段。）
    #
    # 放行判据是**库级 0 改族**，不是「改判 0」（见上面 `采购` 那段为什么）。这个
    # 判据能红，且验过能红：把 `内容→operations` 加回来是 5 行、再加 `治理` 是 7 行。
    # 复现：scripts/measure_family_gaps.py db-effect / rescued
    #
    # 每个词的采纳理由不是「救回条数多」，是**读过它救回的是什么**。被否决的词都
    # 栽在这一步，而它们全都通过了「改判 0」：
    #   培训  救 94 个岗位型，其中约 91 个错 —— 小鹏的 `NVH培训生` 是招聘类型不是职能
    #   计划  救 9 个错 6 个 —— `计划` 在 `培养计划` 里是「项目」的意思
    #   质量  救 13 个错 3 个 —— 字节 `内容质量` 是运营岗
    #   策略  53 个岗位型但跨族 —— 策略产品=product / 策略运营=operations / 广告策略=tech
    #   基础设施 11 个里 10 个只命中在部门名上（见 issue #13）
    #   内容/治理 在腾讯是域词（`内容培训生-艺术创作方向`），是库级 7 行改族的全部来源
    # 剩下 9 个（分析/供应链/工艺/绩效/商务/增长/激励/版权/基建）同理否决。
    # 【别在这里手写词名】完整的 16 个词连否决理由都在
    # `scripts/measure_family_gaps.py` 的 `REJECTED_018` 里，那是唯一的一份。
    # 这行原本多写了一个「猎聘」—— 它从没被度量过，是写注释时凭空多出来的，全 repo
    # 只有那一行提到它。`REJECTED_018` 存在的目的就是防止否决理由只活在注释里，
    # 而我在它旁边的注释里绕开了它。现在 tests/test_normalize.py 的
    # test_rejected_comment_matches_the_table 守着这段注释和那张表一致。
    #
    # 这批词由 `_family_from_title_rules` 统一消歧：财务/法务/传播等明确职能优先；
    # 其余兼具业务域含义的词同时命中时取标题中最靠后的词，避免靠静态换序修一处、
    # 又在另一组组合上复发。原有 product/hr/legal/tech 等主规则仍在这批之前。
    (("结算",), "finance"),
    (("税务",), "finance"),
    (("资金",), "finance"),
    (("内控",), "finance"),
    (("成本",), "finance"),
    (("定价",), "finance"),
    (("法规",), "legal"),
    (("传播",), "marketing"),
    (("软件",), "tech"),
    (("系统",), "tech"),
    (("SRE",), "tech"),
    (("嵌入式",), "tech"),
    (("结构",), "tech"),
    (("材料",), "tech"),
    (("热管理",), "tech"),
    (("品质",), "tech"),
    (("电控",), "tech"),
    (("仿真",), "tech"),
    (("传感",), "tech"),
    (("交付",), "operations"),
    (("履约",), "operations"),
    (("产销",), "operations"),
    (("咨询",), "sales"),
    (("售后",), "sales"),
    (("零售",), "sales"),
    (("商家",), "sales"),
    (("渠道",), "sales"),
    (("达人",), "sales"),
    # 物流/备件 归 other 而不是新开 supply_chain，同 `采购` 的理由。
    #
    # 这里原本还有 `仓储`，量边际贡献时删掉了：库里只有 2 条含 `仓储` 的标题，
    #   【27届校招】备件仓储物流培训生   `备件`/`物流` 已经判 other
    #   仓储库存链路运营 - TikTok Shop   `运营` 先命中，判 operations（对）
    # 单独摘掉 `仓储` 之后判定一条都不变 —— 它通过了全部判据（库级改族 0、
    # 救回的内容也读过没问题），但**边际贡献是 0**。判对但不出力的词是净风险：
    # 以后新岗位撞上它只会加错，不会加对。其余 30 个词边际都 ≥1 行。
    (("物流",), "other"),
    (("备件",), "other"),
    # `顾问` 是整个常规职能层的 sales 兜底，不是强销售词。放在最后，确保法律、
    # 人力、硬件、产品以及本轮新增的软件/系统/法规等明确职能先赢；蔚来/乐道
    # 门店顾问没有更具体职能时仍归 sales。
    (("顾问",), "sales"),
]

# 018 新词分两档。明确职能只要命中就优先；其余词经常是「业务域」，多个同时
# 命中时按中文岗位名常见的「域在前、职能在后」取最靠后的那个。它们仍保留在
# TITLE_RULES 中，方便测量脚本和结构测试看到同一份规则真源。
_NEW_018_SPECIFIC_WORDS = frozenset({
    "结算", "税务", "资金", "内控", "成本", "定价", "法规", "传播",
})
_NEW_018_POSITIONAL_WORDS = frozenset({
    "软件", "系统", "SRE", "嵌入式", "结构", "材料", "热管理", "品质",
    "电控", "仿真", "传感", "交付", "履约", "产销", "咨询", "售后",
    "零售", "商家", "渠道", "达人", "物流", "备件",
})
_NEW_018_WORDS = _NEW_018_SPECIFIC_WORDS | _NEW_018_POSITIONAL_WORDS


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


def _last_family_for_words(
    title: str,
    rules: list[tuple[tuple[str, ...], str]],
    allowed: frozenset[str],
) -> str | None:
    """返回 allowed 中在标题里最后出现的词对应的族。"""
    best_at = -1
    best_family: str | None = None
    for keywords, family in rules:
        for word in keywords:
            if word not in allowed:
                continue
            at = title.rfind(word)
            if at > best_at:
                best_at = at
                best_family = family
    return best_family


def _family_from_title_rules(
    title: str,
    rules: list[tuple[tuple[str, ...], str]],
) -> str | None:
    """用给定第 2 层规则分类；测量脚本也复用，避免复制判定逻辑。"""
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
    # 原有规则（含采购）保持既有顺序。018 新词和泛化的顾问由下面单独消歧。
    for keywords, fam in rules:
        stable_keywords = tuple(
            k for k in keywords if k not in _NEW_018_WORDS and k != "顾问"
        )
        if any(k in title for k in stable_keywords):
            return fam
    specific = _last_family_for_words(title, rules, _NEW_018_SPECIFIC_WORDS)
    if specific is not None:
        return specific
    positional = _last_family_for_words(title, rules, _NEW_018_POSITIONAL_WORDS)
    if positional is not None:
        return positional
    for keywords, fam in rules:
        if "顾问" in keywords and "顾问" in title:
            return fam
    return None


_ROLE_HEAD_SOURCES = frozenset({"feishu:bytedance:campus"})
_ORG_SEPARATORS = "-—－"


def _role_head(title: str) -> str | None:
    """取 ``岗位 - 部门`` 的岗位段；只认 issue #13 量过的三种分隔符。"""
    index = max((title.rfind(separator) for separator in _ORG_SEPARATORS), default=-1)
    if index <= 0 or index >= len(title) - 1:
        return None
    return title[:index].strip() or None


def family_from_title(title: str, *, source_key: str | None = None) -> str | None:
    """标题 → 归一岗位族。返回 None 表示判不出，由调用方用源站族兜底。

    字节校招标题的最后一段是部门名；头段本身能判时，部门词不能覆盖岗位职能。
    其他来源没有这条已验证的命名约定，仍按完整标题处理。
    """
    if source_key in _ROLE_HEAD_SOURCES and (head := _role_head(title)):
        if family := _family_from_title_rules(head, TITLE_RULES):
            return family
    return _family_from_title_rules(title, TITLE_RULES)


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
