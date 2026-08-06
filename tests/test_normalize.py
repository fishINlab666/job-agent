"""归一化回归测试。

这里的每个 case 都来自真实数据里踩到的坑，不是假想的。
归一化规则一改就容易连带打坏别的分类，所以必须钉死。
"""
from __future__ import annotations

import pytest

from jobagent.normalize import family_from_title, normalize_city, split_cities


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
