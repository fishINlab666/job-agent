"""归一化回归测试。

这里的每个 case 都来自真实数据里踩到的坑，不是假想的。
归一化规则一改就容易连带打坏别的分类，所以必须钉死。
"""
from __future__ import annotations

import pytest

from jobagent.normalize import (
    TECH_MARKERS,
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
    ])
    def test_design_domain_word_still_beats_tech_function_word(self, title: str) -> None:
        """**这条锁的是已知判错，不是期望行为。**

        这些都是工程岗，现在判 design：`多媒体`/`视觉` 在第 2 层的 design 那条，
        而 design 排在 tech 前面，域词就把职能词抢走了。同一个岗位带上「工程师」
        （`多媒体研发工程师`）反而判对 —— 那个词在第 0 层，先于第 2 层生效。
        说法不同、判据不同，和这个类里修的是同一种毛病。

        没在这次一起修，是因为便宜的修法是错的：把 tech 挪到 design 前面会改判
        99 条 distinct 标题，其中 75 条是 product → tech（`提前批-AI数据产品经理`
        会变成技术岗），而「运营/产品不能和技术混」是这个模块 docstring 里写的
        头号不变量。要修得加第 1 层复合规则，那是单独一件事 —— issue #9，
        那里有复现这 99/75 的命令。

        写成断言而不是留空：不写的话，谁哪天顺手改动这一层的顺序，这 6 条会
        静默换族而没人知道。
        """
        assert family_from_title(title) == "design"

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
