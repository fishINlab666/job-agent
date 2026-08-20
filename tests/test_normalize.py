"""归一化回归测试。

这里的每个 case 都来自真实数据里踩到的坑，不是假想的。
归一化规则一改就容易连带打坏别的分类，所以必须钉死。
"""
from __future__ import annotations

import pytest

from jobagent.normalize import (
    DESIGN_DOMAIN_WORDS,
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

    @pytest.mark.parametrize("title,expected", [
        ("人力资源顾问", "hr"),
        ("法律顾问", "legal"),
        ("硬件顾问", "tech"),
        ("产品顾问", "product"),
    ])
    def test_advisor_is_only_a_fallback(
        self, title: str, expected: str
    ) -> None:
        """明确职能必须先赢；裸 `顾问` 只能兜底成 sales。"""
        assert family_from_title(title) == expected


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

    def test_other_group_is_last_in_layer_two(self) -> None:
        """守归属，不是守行为：`other` 组必须是 TITLE_RULES 的最后一条。

        行为测试挡不住「往前挪一格」—— 挪到 tech 之前，`硬件采购实习生`
        才会红，而挪到 tech 之后、marketing 之前，上面那些用例全绿，
        只有别的域词组合会挂。「供应链」是所有具体职能都没命中之后的兜底，
        这个语义只能由位置表达。
        """
        assert TITLE_RULES[-1][1] == "other", "other 组不在末尾，兜底语义没了"
        assert "采购" in TITLE_RULES[-1][0]
        assert sum(1 for _, fam in TITLE_RULES if fam == "other") == 1, \
            "other 在第 2 层只该有一组"


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

    def test_service_alone_stays_undecidable(self) -> None:
        """只有 `服务` 没有别的职能词时判不出，不许兜底成 sales。

        判不出（None）比判错好：None 会进 digest 的「信息不全」让人看见，
        判错会静默地把岗位筛掉。
        """
        assert family_from_title("实习-NSC售后服务代表") is None
        assert family_from_title("售后服务-增值服务（西安）") is None

    def test_service_is_in_no_word_list(self) -> None:
        """`服务` 不在任何词表里 —— 守的是「有人偷偷加进去」。"""
        for words, fam in TITLE_RULES:
            assert "服务" not in words, f"服务 被加进了 {fam} 组，见方案 017 §6"


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
