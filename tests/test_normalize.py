"""归一化回归测试。

这里的每个 case 都来自真实数据里踩到的坑，不是假想的。
归一化规则一改就容易连带打坏别的分类，所以必须钉死。
"""
from __future__ import annotations

import pytest

from jobagent.normalize import (
    DESIGN_DOMAIN_WORDS,
    FAMILIES,
    TECH_FUNCTION_WORDS,
    TECH_MARKERS,
    TITLE_RULES,
    family_from_title,
    normalize_city,
    split_cities,
)


class TestTechMarkersWinOverDomainWords:
    """第 0 层：算法研究岗不能被域词抢走。"""

    @pytest.mark.parametrize("title", [
        "混元基座模型-视觉理解大模型研究",          # 含「视觉」，曾被判 design
        "混元多模态-视觉编码器技术研究",
        "混元多模态-多模态交互大模型的Model Merge",  # 含「交互」
        "面向以“人”为中心的具身交互智能体研究",
        "腾讯营销—广告推荐基础大模型",               # 含「营销」，曾被判 marketing
        "腾讯营销—基于大模型Agent的腾讯电商广告推荐研究",
        "腾讯营销 —机器学习平台技术研究",
        "WXG-从设计到代码（可信程序自动生成）",       # 含「设计」
        "运营开发",                                  # 含「运营」，实为 SRE
    ])
    def test_research_and_eng_roles_are_tech(self, title: str) -> None:
        assert family_from_title(title) == "tech"


class TestCompoundFunctionWins:
    """第 1 层：职能词在后的复合标题。"""

    @pytest.mark.parametrize("title,expected", [
        ("产品体验设计", "design"),      # 曾被判 product
        ("投资运营分析师", "finance"),   # 曾被判 operations
        ("员工福利运营", "hr"),          # 曾被判 operations
        ("物业运营与办公规划管理", "other"),
    ])
    def test_compound(self, title: str, expected: str) -> None:
        assert family_from_title(title) == expected


class TestOperationsSeparatedFromProduct:
    """源站把运营岗塞在产品族里，这是本项目最关键的一条归一化。"""

    @pytest.mark.parametrize("title", [
        "产品运营", "内容运营", "行业运营",
        "游戏发行/运营培训生", "业务管理运营", "渠道管理运营",
    ])
    def test_is_operations(self, title: str) -> None:
        assert family_from_title(title) == "operations"

    @pytest.mark.parametrize("title", [
        "产品策划", "技术产品经理", "项目管理", "游戏策划培训生", "音频策划",
    ])
    def test_is_product(self, title: str) -> None:
        assert family_from_title(title) == "product"

    def test_user_research_is_not_tech(self) -> None:
        """「研究」不能当技术标记，否则用户研究被误判。"""
        assert family_from_title("用户研究") == "marketing"

    def test_tech_pm_is_not_tech(self) -> None:
        """「技术」不能当技术标记，否则技术产品经理被误判。"""
        assert family_from_title("技术产品经理") == "product"


class TestSynonymsOfDevelopmentAreAllTech:
    """同一个岗位换个说法不能掉出分类。

    这张表原本有「开发」「后台」，没有「研发」「后端」，于是
    `后端开发工程师` 判 tech，`后端研发实习生` 判 None。实测里这么丢掉的：
    字节 79 条、小鹏 3 条、商汤 2 条。
    """

    @pytest.mark.parametrize("title", [
        "后端研发实习生",
        "移动研发实习生-App Infra",
        "后端实习生",
        "机器人灵巧手研发岗位培训生",
        "研发效能实习生",
    ])
    def test_rnd_and_backend_are_tech(self, title: str) -> None:
        assert family_from_title(title) == "tech"

    @pytest.mark.parametrize("title", [
        "多媒体处理平台后端实习生-音视频技术",
        "多媒体C++研发（AI创作方向）实习生-抖音用户产品基础",
        "3D视觉研发实习生-智能创作",
        "多媒体图形/图像研发实习生-抖音-智能创作",
        "多媒体图形/图像研发实习生-抖音用户产品基础",
        "多媒体客户端研发实习生-抖音-智能创作",
    ])
    def test_design_domain_word_does_not_steal_tech_function_word(self, title: str) -> None:
        """issue #9：这 6 条是工程岗，曾经判 design。

        `多媒体`/`视觉` 在第 2 层 design 那条，而 design 排在 tech 前面，域词就把
        职能词抢走了。同一个岗位带上「工程师」（`多媒体研发工程师`）反而判对 ——
        那个词在第 0 层，先于第 2 层生效。说法不同、判据不同，和这个类里修的是同
        一种毛病。

        修法是第 1.5 层「域词在前 + 职能词在后 → tech」，不是把第 2 层 tech 提到
        design 前面（那样改判 227 条，其中 product → tech 75 条，破模块头号不变
        量）。口径和代价见 docs/plans/015。

        这 6 条是 distinct 标题数。库里对应 12 行（2026-08-12 快照，会随新抓取变；
        这个数只在 docs/plans/015 §7 维护，别在这里跟着改）。
        """
        assert family_from_title(title) == "tech"

    @pytest.mark.parametrize("title", [
        "后台动画设计师",
        "客户端UI动效设计",
        "后端图形界面GUI设计",
    ])
    def test_tech_word_before_domain_word_stays_design(self, title: str) -> None:
        """第 1.5 层判的是**有序对**，不是「两类词都在」。

        这三条同时含技术职能词和设计域词，但技术词在**前** —— 那是修饰语，职能仍
        是设计。判据弱化成纯 AND（`d is not None and f is not None`）时上面 6 条
        全绿、只有这 3 条红，所以这条是「做一半会红」的那条。真库里没有这三个标
        题，这个方向只能靠构造用例钉住。
        """
        assert family_from_title(title) == "design"

    @pytest.mark.parametrize("title", [
        "交互设计前端实习生",
        "视觉设计客户端实习生",
    ])
    def test_explicit_compound_phrase_outranks_position_heuristic(self, title: str) -> None:
        """第 1.5 层必须排在 `COMPOUND_RULES` **之后**。

        这两条满足「域词在前、职能词在后」，但它们有显式复合短语 `交互设计`/
        `视觉设计` —— 显式短语优先于位置启发式。新层挪到 COMPOUND_RULES 前面时，
        上面 6 条仍全绿、只有这 2 条红。真库里「新层触发且含 design 复合短语」的
        标题是 0 条，位置在当前数据上无差别，只能靠构造用例钉住。
        """
        assert family_from_title(title) == "design"

    def test_domain_word_at_index_zero_is_a_hit(self) -> None:
        """`_first_index` 返回 0 是合法命中，不许用真值判断。

        目标 6 条里 5 条域词在下标 0。写 `if d and f` 时 `3D视觉研发`（域词在下标
        2）还是绿的，另外 5 条全红 —— 单看一条会以为规则没生效，实际是漏了整类。
        """
        title = "多媒体客户端研发实习生-抖音-智能创作"
        assert title.index("多媒体") == 0, "前提变了：这条标题的域词不再在下标 0"
        assert family_from_title(title) == "tech"

    def test_same_role_two_spellings_agree(self) -> None:
        """这条是这批 bug 的形状本身：说法不同、职能相同，判据必须一致。"""
        assert family_from_title("后端开发工程师") == \
            family_from_title("后端研发实习生") == "tech"


class TestRndStaysInLayerTwo:
    """「研发」「后端」必须留在第 2 层，不能升到第 0 层强信号。

    量过：放第 0 层同样救回 85 条，但会改判 69 条已经判对的 —— 第 0 层盖住
    所有其他规则，「域词在前、职能在后」的结构就失效了。这几条把那 69 条里的
    代表钉住，谁哪天为了「更准」把词挪上去，这里会红。
    """

    @pytest.mark.parametrize("title,expected", [
        # 招研发的 HR 岗。挪到第 0 层会判成 tech
        ("研发技术类组织招聘 - 人力与管理部", "hr"),
        # 域在前、职能在后：这些是运营/市场/销售岗的技术方向，不是技术岗
        ("后端研发实习生（运营平台方向）-国际支付", "operations"),
        ("前端研发实习生-国际化广告创意与品牌", "marketing"),
        ("后端研发实习生-国际商业化客户解决方案", "sales"),
        ("后端研发（财务系统方向）实习生 - 集团信息系统", "finance"),
        ("【27届校招】研发质量培训生（项目管理）", "product"),
    ])
    def test_domain_word_still_wins(self, title: str, expected: str) -> None:
        assert family_from_title(title) == expected

    def test_rnd_is_not_a_layer_zero_marker(self) -> None:
        """直接断言两个词不在第 0 层表里。

        上面那些用例是从行为侧挡的，这条从结构侧挡 —— 有人把词加进
        TECH_MARKERS **同时**改掉上面的期望值，行为侧的挡不住，这条能。
        """
        assert "研发" not in TECH_MARKERS
        assert "后端" not in TECH_MARKERS


class TestDomainAndFunctionWordListsStayDisjoint:
    """第 1.5 层两张词表的结构约束。

    行为侧的用例（上面那些标题）挡不住「有人补词**同时**改期望值」，这几条从结构
    侧挡。两张表语义不同：域词说岗位作用在什么东西上，职能词说岗位干什么活。
    """

    def test_the_two_lists_do_not_overlap(self) -> None:
        """交集必须为空。

        非空意味着某个词既是域又是职能，那时 `domain_at < function_at` 里两个下标
        会相等，判据退化成「这个词在标题里」—— 一个词就能把整层变成子串匹配。
        """
        assert set(DESIGN_DOMAIN_WORDS) & set(TECH_FUNCTION_WORDS) == set()

    def test_function_list_excludes_the_four_words_that_break_operations(self) -> None:
        """`数据`/`安全`/`硬件`/`模型` 不能进职能词表。

        它们在第 2 层 tech 组里，但补进这张表会让 3 条运营岗判成技术（真库实测），
        破模块 docstring 写的头号不变量。行为侧的挡在下一条。
        """
        for word in ("数据", "安全", "硬件", "模型"):
            assert word not in TECH_FUNCTION_WORDS, \
                f"{word} 进了职能词表，运营岗会被判成技术"

    @pytest.mark.parametrize("title", [
        "视觉生成策略运营（图片美感方向）实习生-AI数据与安全",
        "视觉生成策略运营（图片评测方向）实习生-AI数据与安全",
        "视觉生成策略运营（数据分析方向）实习生-AI数据与安全",
    ])
    def test_operations_jobs_with_visual_domain_stay_operations(self, title: str) -> None:
        """上一条的行为侧：这 3 条真库标题域词在前，靠职能词表不含 `数据/安全` 才没被抢走。"""
        assert family_from_title(title) == "operations"

    def test_domain_list_excludes_function_words(self) -> None:
        """`设计`/`美术` 不能进域词表 —— 它们是职能词。

        补进去在真库上无差别（0 条改判），所以只有这条结构断言 + 下一条构造用例
        能挡住。这是「真数据挡不住的改动」，见 docs/plans/015 §9 命令 E/F。
        """
        for word in ("设计", "美术"):
            assert word not in DESIGN_DOMAIN_WORDS, \
                f"{word} 是职能词，进域词表会让「设计师（前端方向）」判成技术"

    @pytest.mark.parametrize("title", [
        "设计师（前端方向）",
        "美术资源-客户端支持",
        "设计中台后端实习生",
    ])
    def test_design_function_word_with_tech_direction_stays_design(self, title: str) -> None:
        """上一条的行为侧：职能是设计、技术词只是方向说明。真库里没有这些标题。"""
        assert family_from_title(title) == "design"

    def test_word_lists_contain_no_family_names(self) -> None:
        """词表里放的是标题里出现的词，不是族名。

        手滑把 `"design"` 写进词表时，判据会变成「标题里含 design 这个英文单词」——
        真库上大概 0 命中，静默失效。
        """
        families = {fam for _, fam in TITLE_RULES}
        for word in DESIGN_DOMAIN_WORDS + TECH_FUNCTION_WORDS:
            assert word not in families, f"{word} 是族名，不是标题里的词"


class TestAdvisorIsSales:
    """方案 017：`顾问` 判 sales。

    这批是 nio 的门店销售岗，占判不出族的 233 行 / 21 个岗位型（2026-08-13 快照，
    会随新抓取变；这个数只在 docs/plans/017 §7 维护，别在这里跟着改）。
    """

    @pytest.mark.parametrize("title", [
        "校招实习-蔚来顾问-上海",
        "校招实习-乐道顾问-重庆",
        "ONVO-乐道行销顾问-重庆",
        "企业效能顾问实习生-飞书",
    ])
    def test_advisor_is_sales(self, title: str) -> None:
        assert family_from_title(title) == "sales"

    def test_advisor_with_city_suffix_still_hits(self) -> None:
        """城市写在标题尾巴上不影响判定。

        nio 把城市拼进标题（`蔚来顾问` 116 个城市、`乐道顾问` 90 个），
        判据是子串匹配所以本来就不受影响 —— 这条钉的是「以后有人改成
        按段切分标题」时不许把这批弄丢。
        """
        for city in ("上海", "乌鲁木齐", "遵义仁怀市", "蔚来广州"):
            assert family_from_title(f"校招实习-蔚来顾问-{city}") == "sales"

    def test_marketing_class_still_wins_over_advisor(self) -> None:
        """`蔚来顾问-"未来星"营销管训班` 仍判 marketing，不是 sales。

        marketing 组在 TITLE_RULES 里排在 sales 之前，所以含两类词时 marketing 先赢。
        这条不是「期望的语义」——它其实更像销售管培 —— 而是**钉住现状**：
        加 `顾问` 这条规则改判 0 行，这两行属于「改判 0」里的 0。
        哪天要改这个顺序，这条会红，提醒去重新量改判数。
        """
        t = '校招实习-蔚来顾问-"未来星"营销管训班-合肥'
        assert family_from_title(t) == "marketing"


class TestProcurementIsOther:
    """方案 017：`采购` 判 other，且必须在第 2 层末尾。"""

    @pytest.mark.parametrize("title", [
        "【27届校招】采购专业培训生（综合采购）",
        "IT品类采购实习生-Corporate Services",
        "【27届校招】备件采购培训生",
    ])
    def test_procurement_is_other(self, title: str) -> None:
        assert family_from_title(title) == "other"

    @pytest.mark.parametrize("title,expected", [
        ("AI产品经理（采购方向） - Corporate Services", "product"),
        ("采购AI产品实习生 - Corporate Services", "product"),
        ("采购政策与合规 - Corporate Services", "finance"),
        ("硬件采购实习生", "tech"),
    ])
    def test_procurement_direction_does_not_steal(
        self, title: str, expected: str
    ) -> None:
        """`采购方向` 里 `采购` 是域词，职能是产品/财务/硬件。

        把 `采购` 挪进 COMPOUND_RULES（第 2 层之前）这四条立刻红 —— 实测会
        改判 5 条，这是其中 4 条。第 5 条是 `AI应用研发工程师（采购方向）`，
        它有第 0 层的 `工程师` 兜着，所以不在这个清单里。
        """
        assert family_from_title(title) == expected

    def test_procurement_precedes_the_018_words(self) -> None:
        """守归属：`采购` 必须排在 018 加的 31 个职能词**之前**。

        这条测试改过一次，原本断言的是「`采购` 组是 TITLE_RULES 最后一条」。
        018 往它后面加了一批职能词（量的时候 31 个，删掉边际为 0 的 `仓储` 后 30 个），那个断言就红了 —— 红得对，它拦住了一次真实的
        位置变化，这正是它存在的意义。但它守的**理由**站不住：017 的原话是
        「`采购` 必须是最后一条」，而量过之后，绝对位置不是约束，层级才是。

        量法和结果（`measure_family_gaps.py rule-order` 的前身，方案 018 §5）：
        当时那 31 个词插在 `采购` 之前 vs 之后，6635 条在架标题里只有 1 条判定不同 ——
            IoT采购履行经理（结构件方向） - AI算力基础设施
            在采购之前 → tech（`结构` 命中）   在采购之后 → other（`采购` 命中）
        职能是采购，`结构件` 是它作用的对象，所以 other 对、tech 错。方向和 017
        记的 `采购方向` 那个病一样，只是镜像：那次是 `采购` 当域词，这次是
        `结构` 当域词。

        所以现在守两件事：`采购` 在这 30 个词之前，且 `other` 兜底仍在具体职能之后。
        """
        fams = [fam for _, fam in TITLE_RULES]
        words = [ws for ws, _ in TITLE_RULES]
        proc = next(i for i, ws in enumerate(words) if "采购" in ws)
        for w in ("软件", "结构", "物流", "备件"):
            idx = next(i for i, ws in enumerate(words) if w in ws)
            assert proc < idx, f"`采购` 必须在 `{w}` 之前，否则 IoT采购履行经理 判 tech"
        assert fams[-1] == "other", "第 2 层末尾不是 other，兜底语义没了"
        # `other` 现在有 3 组（采购/物流/备件），不再是 1 组。断言「恰好 3」
        # 而不是「>= 1」：多出第 4 组说明有人又加了供应链词，那要走 018 §6 的判据
        # （先跑 measure_family_gaps.py db-effect，再看边际贡献是不是 0）。
        assert sum(1 for f in fams if f == "other") == 3, \
            "other 组数变了 —— 加供应链词要先跑 measure_family_gaps.py db-effect"

    def test_procurement_beats_domain_words_after_it(self) -> None:
        """`采购` 在前的具体后果，用真标题钉住。

        上一条守位置，这一条守行为。两条都要有：只守位置挡不住「把 `结构` 也挪到
        `采购` 前面」，只守行为挡不住「两个词一起往前挪」。
        """
        assert family_from_title(
            "IoT采购履行经理（结构件方向） - AI算力基础设施") == "other"


class TestServiceIsDeliberatelyNotSales:
    """反向用例：`服务` 是域词，**故意不加**进任何组。

    这条钉的是一个「决定不做的事」。数据上 `服务 → sales` 能救 59 行，
    看着比 `采购` 的 14 行划算，所以下一个人很可能会去加它 ——
    而加进 sales 组会把 28 行 tech 改判成 sales，没有别的测试拦得住。
    判据：`服务` 在真标题里绝大多数是业务线名（`抖音生活服务`）或者被修饰的
    对象（`服务工程` 是技术岗、`服务大区管理` 是管理岗），不指干什么活。
    """

    @pytest.mark.parametrize("title,expected", [
        ("数据分析实习生-抖音生活服务", "tech"),
        ("后端研发（企业服务系统）实习生-集团信息系统", "tech"),
        ("AI数据服务平台数据实习生-国际化", "tech"),
        ("产品设计师 - 抖音生活服务", "design"),
        ("战略经营分析师 - 抖音生活服务", "marketing"),
    ])
    def test_service_does_not_steal_the_real_function(
        self, title: str, expected: str
    ) -> None:
        assert family_from_title(title) == expected

    @pytest.mark.parametrize("title", [
        "救援服务实习生",
        "【27届校招】AI智能服务培训生",
        "【27届校招】服务工程培训生",
        "服务策略 - 体验与服务",
    ])
    def test_service_alone_stays_undecidable(self, title: str) -> None:
        """只有 `服务` 没有别的职能词时判不出，不许兜底成 sales。

        判不出（None）比判错好：None 会进 digest 的「信息不全」让人看见，
        判错会静默地把岗位筛掉。

        用例换过一次。原本钉的是 `实习-NSC售后服务代表` 和
        `售后服务-增值服务（西安）`，018 加了 `售后 → sales` 之后这两条判 sales ——
        **那是对的**，售后服务代表确实是 sales 岗，蔚来那 12 条 `售后` 标题现在
        10 条判 sales、2 条判 operations（`海外售后运营` 里 `运营` 先命中，也对）。
        变的是用例不是结论：`服务` 单独出现仍然判不出，库里还有 15 条这样的标题。
        挑用例的时候要挑**只含 `服务`、不含 `售后`** 的，否则钉的是 `售后` 不是 `服务`。
        """
        assert family_from_title(title) is None

    @pytest.mark.parametrize("title,expected", [
        ("实习-NSC售后服务代表", "sales"),
        ("售后服务-增值服务（西安）", "sales"),
        ("【27届校招】海外售后运营培训生-东南亚", "operations"),
    ])
    def test_after_sales_is_sales_but_ops_still_wins(
        self, title: str, expected: str
    ) -> None:
        """`售后` 判 sales，但 `运营` 在表里更靠前，所以售后运营岗仍判 operations。

        这条和上面那条是一对：`服务` 不定族、`售后` 定族。分开钉是为了让「哪个词
        在起作用」这件事在测试层面就分得清 —— 合在一起写，`售后` 哪天被删掉，
        红的会是「服务」那条测试，人会去改错的地方。
        """
        assert family_from_title(title) == expected

    def test_service_is_in_no_word_list(self) -> None:
        """`服务` 不在任何词表里 —— 守的是「有人偷偷加进去」。"""
        for words, fam in TITLE_RULES:
            assert "服务" not in words, f"服务 被加进了 {fam} 组，见方案 017 §6"


# 018 加的 30 个词，在测试里**独立写一遍**。
#
# 这是第三份副本（normalize.py、measure_family_gaps.py、这里），是故意的：
# 测试不能读它要检验的那个值。写 `for w, f in normalize.NEW_WORDS` 的话，
# 有人删掉一个词，循环少跑一圈，测试照样绿 —— 那种「改坏了但不咬」正是
# 这批测试要防的东西。副本漂移由 test_word_count_matches 兜住。
NEW_018: tuple[tuple[str, str], ...] = (
    ("软件", "tech"), ("系统", "tech"), ("SRE", "tech"), ("嵌入式", "tech"),
    ("结构", "tech"), ("材料", "tech"), ("热管理", "tech"), ("品质", "tech"),
    ("电控", "tech"), ("仿真", "tech"), ("传感", "tech"),
    ("交付", "operations"), ("履约", "operations"), ("产销", "operations"),
    ("咨询", "sales"), ("售后", "sales"), ("零售", "sales"), ("商家", "sales"),
    ("渠道", "sales"), ("达人", "sales"),
    ("结算", "finance"), ("税务", "finance"), ("资金", "finance"),
    ("内控", "finance"), ("成本", "finance"), ("定价", "finance"),
    ("传播", "marketing"), ("法规", "legal"),
    ("物流", "other"), ("备件", "other"),
)

# 量边际贡献时删掉的词。这里也钉一遍，防止有人「补全」回去。
ZERO_MARGIN_018: tuple[str, ...] = ("仓储",)


class TestVocabGapWords018:
    """方案 018：补 30 个能定族的职能词。

    这个类钉四件事，分开写是因为改坏一件不该让另外三件跟着红：
      ① 每个词各自救回的真标题（删词就红）
      ② 30 个词的位置（挪进前面的层就红）
      ③ 被**否决**的 16 个词不许被加回来（加了就红）
      ④ 边际贡献为 0 的 `仓储` 不许被「补全」回来

    ③ 是最容易被下一个人破坏的：那些词在「救回条数」上看着都很划算，
    `培训` 一个词能救 94 个岗位型 —— 而其中约 91 个是错的。
    """

    # 每条都是库里的真标题。写全称不写片段：片段测不出「词在标题的哪个位置」，
    # 而位置决定它是职能还是部门名（见 issue #13）。
    @pytest.mark.parametrize("title,expected", [
        ("BMS软件培训生", "tech"),
        ("【27届校招】整车软件培训生", "tech"),
        # 这两条挑过一次。原本用的是 `智驾系统安全培训生` 和 `嵌入式软件培训生`，
        # 它们**测不出**对应的词：前者有 `安全`（早就在 tech 组里）、后者有 `软件`
        # （018 的另一个词），删掉 `系统`/`嵌入式` 照样判 tech。用例要挑「这个词是
        # 唯一决定者」的标题，验法见 scripts/mutate_018.sh。
        ("【27届校招】BMS系统培训生", "tech"),
        ("【27届校招】嵌入式EMC培训生", "tech"),
        ("【27届校招】车身结构培训生", "tech"),
        ("【27届校招】材料工程培训生", "tech"),
        ("【27届校招】热管理培训生", "tech"),
        ("【27届校招】整车品质培训生", "tech"),
        ("【27届校招】电控培训生", "tech"),
        ("【27届校招】仿真分析培训生", "tech"),
        ("【27届校招】传感器培训生", "tech"),
        ("交付实习生（上海大区）", "operations"),
        ("【27届校招】履约培训生", "operations"),
        ("【27届校招】产销培训生", "operations"),
        ("【27届校招】海外售后赋能培训生-东南亚", "sales"),
        ("新零售实习生（上海大区）", "sales"),
        ("【27届校招】渠道拓展培训生", "sales"),
        ("税务实习生", "finance"),
        ("【27届校招】资金培训生", "finance"),
        ("【27届校招】内控培训生", "finance"),
        ("【27届校招】成本培训生", "finance"),
        ("【27届校招】备件培训生", "other"),
    ])
    def test_word_rescues_its_target(self, title: str, expected: str) -> None:
        """删掉对应的词，这条就从 expected 掉回 None。"""
        assert family_from_title(title) == expected

    def test_all_words_are_in_layer_two(self) -> None:
        """30 个词必须在第 2 层，不许出现在第 0 层或 COMPOUND_RULES。

        守的是「有人为了让某条标题判对，把词往前挪一层」。往前挪一层的代价在
        017 量过：第 0 层盖住所有其他规则，`研发` 放第 0 层会改判 69 条。
        """
        from jobagent.normalize import COMPOUND_RULES

        layer2 = {w for ws, _ in TITLE_RULES for w in ws}
        for word, _fam in NEW_018:
            assert word in layer2, f"`{word}` 不在第 2 层了"
            assert word not in TECH_MARKERS, f"`{word}` 被挪进第 0 层"
            for ws, fam in COMPOUND_RULES:
                assert word not in ws, f"`{word}` 被挪进 COMPOUND_RULES 的 {fam} 组"

    @pytest.mark.parametrize("word,why", [
        ("培训", "招聘类型不是职能，救 94 个岗位型约 91 个错"),
        ("计划", "`培养计划` 里是「项目」的意思，救 9 个错 6 个"),
        ("质量", "字节 `内容质量` 是运营岗，救 13 个错 3 个"),
        ("分析", "跨族：数据分析/商业分析/财务分析"),
        ("策略", "域词：策略产品/策略运营/广告策略 三族都有"),
        ("基础设施", "11 个救回里 10 个只命中在部门名上，见 issue #13"),
        ("内容", "腾讯是域词，库级改族 5 行"),
        ("治理", "同 `内容`，合计 7 行改族"),
    ])
    def test_rejected_words_stay_out(self, word: str, why: str) -> None:
        """否决的词不许进任何词表。理由跟着用例走，免得下一个人只看到断言。"""
        for ws, fam in TITLE_RULES:
            assert word not in ws, f"`{word}` 被加进 {fam} 组，但它{why}（方案 018 §6）"
        assert word not in TECH_MARKERS, f"`{word}` 被加进第 0 层，但它{why}"

    @pytest.mark.parametrize("title", [
        "【27届校招】NVH培训生",
        "【27届校招】CFD培训生",
        "【27届校招】EMC&射频培训生",
    ])
    def test_training_shaped_titles_still_undecidable(self, title: str) -> None:
        """`培训` 被否决的直接后果：这批仍然判不出。

        这是 issue #8 方向 ② 的料，**故意留着**。判不出会进 digest 的
        「信息不全」让人看见；靠 `培训` 兜底会把它们静默判成 hr，那是判错。
        """
        assert family_from_title(title) is None

    @pytest.mark.parametrize("word", ZERO_MARGIN_018)
    def test_zero_margin_words_stay_out(self, word: str) -> None:
        """`仓储` 判得对，但边际贡献是 0 —— 不许因为「看着该有」加回来。

        它和 `test_rejected_words_stay_out` 那批不是一类：那些加了会**判错**，
        这个加了只是不出力。两种否决混在一起会让人以为 `仓储` 会判错，
        然后拿一条 `仓储` 判对的标题来「反驳」，从而把它加回来。

        库里含 `仓储` 的标题只有 2 条：`备件仓储物流培训生`（`备件`/`物流` 先接住）、
        `仓储库存链路运营`（`运营` 先接住，判 operations 是对的）。
        """
        for ws, fam in TITLE_RULES:
            assert word not in ws, \
                f"`{word}` 被加回 {fam} 组，但它边际贡献是 0（方案 018 §6）"

    def test_zero_margin_word_would_change_nothing(self) -> None:
        """把「它加了也没用」这件事本身钉住，而不是只钉「它不在表里」。

        只钉「不在表里」的话，下一个人有权问「凭什么」。这条给出可运行的答案：
        这 2 条标题的判定和 `仓储` 在不在表里无关。
        """
        assert family_from_title("【27届校招】备件仓储物流培训生") == "other"
        assert family_from_title("仓储库存链路运营 - TikTok Shop") == "operations"

    def test_no_new_family_value(self) -> None:
        """30 个词没有引入新的族名。物流/备件归 other，同 `采购` 的理由。"""
        for _ws, fam in TITLE_RULES:
            assert fam in FAMILIES, f"{fam} 不在 FAMILIES 里"
        for _word, fam in NEW_018:
            assert fam in FAMILIES, f"{fam} 不在 FAMILIES 里"

    def test_word_count_matches(self) -> None:
        """副本漂移的兜底：测试里这份 30 个词必须和 normalize.py 里的一致。

        上面每条测试都只查「这个词在不在」，查不出「normalize.py 多了个词」。
        多出来的词是没经过 018 §6 判据的，必须红。
        """
        layer2 = [w for ws, _ in TITLE_RULES for w in ws]
        assert len(layer2) == len(set(layer2)), \
            f"第 2 层有重复词：{[w for w in layer2 if layer2.count(w) > 1]}"
        declared = {w for w, _ in NEW_018}
        assert len(declared) == 30, f"测试里这份不是 30 个，是 {len(declared)}"
        # 反向：normalize.py 第 2 层里，凡是单词条且在 017 之后的组，都该在 NEW_018 里。
        singles = [ws[0] for ws, _ in TITLE_RULES if len(ws) == 1]
        unexpected = [w for w in singles if w not in declared and w != "采购"]
        assert not unexpected, \
            f"normalize.py 第 2 层多了没在测试里声明的单词条：{unexpected}"
        for word, fam in NEW_018:
            hit = [f for ws, f in TITLE_RULES if word in ws]
            assert hit == [fam], f"`{word}` 在 normalize.py 里判 {hit}，测试期望 {fam}"


class TestCityNormalization:
    def test_strips_headquarters_suffix(self) -> None:
        assert normalize_city("深圳总部") == "深圳"

    def test_strips_china_prefix(self) -> None:
        assert normalize_city("中国香港") == "香港"

    def test_split_space_separated(self) -> None:
        assert split_cities("深圳总部 北京 上海 广州 成都 杭州 ") == [
            "深圳", "北京", "上海", "广州", "成都", "杭州",
        ]

    def test_dedupes(self) -> None:
        assert split_cities("深圳总部 深圳 北京") == ["深圳", "北京"]

    def test_handles_empty(self) -> None:
        assert split_cities(None) == []
        assert split_cities("") == []
