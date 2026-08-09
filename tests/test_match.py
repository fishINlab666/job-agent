"""匹配层回归测试。

这里钉死的是一类特定 bug：**岗位被静默丢掉**。

原来的写法把「源站没写清楚」和「确定不匹配」当成同一件事处理，于是
grad_year 没写、cities 写「全国」的岗位一律被过滤掉，用户永远看不到，
也不会收到任何提示——排查时只能靠一条条翻库才能发现少了东西。

所以下面的用例大多不是在测「能不能筛对」，而是在测「不该消失的有没有消失」。
"""
from __future__ import annotations

import pytest

from jobagent.match import (
    MISSING_DIMS,
    city_list,
    classify,
    filter_jobs,
    matches,
    partition,
    score,
)
from jobagent.normalize import (
    any_city_ok,
    grad_years_from_title,
    is_city_wildcard,
    parse_grad_years,
)

INTENT = {
    "grad_years": ["26", "27"],
    "families": ["operations", "product"],
    "cities": ["北京", "上海", "深圳"],
    "recruit_types": ["campus", "intern"],
    "exclude_keywords": ["外包", "派遣"],
}


def job(**over):
    """一个确定命中的岗位，用例只覆盖自己关心的那个字段。"""
    base = dict(
        title="产品运营", job_family="operations", recruit_type="campus",
        cities=["北京"], grad_year="26",
    )
    return {**base, **over}


class TestGradYearParsing:
    """届别文本 → 届别集合。真实源站写法。"""

    @pytest.mark.parametrize("raw,want", [
        ("2027届", ["27"]),
        ("2027", ["27"]),
        ("27", ["27"]),                       # 库里已存的裸两位
        ("2026-2027年毕业", ["26", "27"]),      # reviewer 点名的写法
        ("2026届、2027届", ["26", "27"]),
        ("26-27届", ["26", "27"]),
        ("26/27届", ["26", "27"]),            # 枚举，曾只捞到 27
        ("25、26、27届", ["25", "26", "27"]),
        ("26届和27届", ["26", "27"]),
        ("2027年6月-2028年6月毕业", ["27", "28"]),
    ])
    def test_parses_real_formats(self, raw: str, want: list[str]) -> None:
        assert parse_grad_years(raw) == want

    @pytest.mark.parametrize("raw", ["不限", "届别不限", "应届均可", "任意届别"])
    def test_unlimited_is_empty_list_not_none(self, raw: str) -> None:
        """明确写「不限」→ []，语义是「任何届别都命中」。

        和 None（看不懂）必须分开：[] 应当直接放过，None 只能算信息不足。
        """
        assert parse_grad_years(raw) == []

    @pytest.mark.parametrize("raw", [None, "", "   ", "待定", "面议", "全日制本科"])
    def test_unparseable_is_none(self, raw) -> None:
        assert parse_grad_years(raw) is None

    def test_range_does_not_explode_on_garbage(self) -> None:
        """区间跨度离谱的不当区间处理，避免生成上百个届别。"""
        assert parse_grad_years("2020-2099年") != [f"{y:02d}" for y in range(20, 100)]

    @pytest.mark.parametrize("raw", ["Anyscale 平台研发", "anybody 岗", "Company 招聘"])
    def test_ascii_any_needs_word_boundary(self, raw: str) -> None:
        """`any` 按整词匹配，不按子串。

        原先是子串：`Anyscale 平台研发` 被判成「不限届别」，也就是「任何届别都命中」。
        把「不知道」洗成「确定命中」，方向是错的——宁可漏推，不可错推。
        """
        assert parse_grad_years(raw) != []

    @pytest.mark.parametrize("raw", ["any", "Any grade", "不限", "届别不限"])
    def test_real_unlimited_still_empty_list(self, raw: str) -> None:
        """修 `any` 不能修过头：真写了「不限」的还得是 []。"""
        assert parse_grad_years(raw) == []


class TestGradYearFromTitle:
    """第二条观测通道：标题。比字段严一档。

    飞书四家的结构化字段里根本没有届别这一列（003 全量核实），但小鹏、蔚来的标题上
    明写着「【27届校招】」。标题上写着的东西不算「没写」。
    """

    @pytest.mark.parametrize("title,want", [
        ("【27届校招】渠道拓展培训生", ["27"]),
        ("实习-蔚来2027届校园大使", ["27"]),
        ("26/27届-内容运营", ["26", "27"]),
    ])
    def test_parses_real_titles(self, title: str, want: list[str]) -> None:
        assert grad_years_from_title(title) == want

    @pytest.mark.parametrize("title", [
        "2026年校园大使",              # 可能是活动年份，不是毕业届别
        "2026年秋季校园招聘-运营",      # 招聘年度
        "产品运营",
        None,
        "",
    ])
    def test_title_needs_jie_marker(self, title) -> None:
        """没有「届」字不解析。

        裸年份在标题里是歧义的：招聘年度 / 活动届次 / 毕业届别都可能。
        字段名能定死语义，标题不能，所以这条通道要求显式的「届」字。
        """
        assert grad_years_from_title(title) is None

    @pytest.mark.parametrize("title", [
        "混元-原生多模态(Any-to-Any模型)预训练",
        "全部业务线-数据分析",
        "任意方向-算法",
        "届别不限的运营岗",             # 带「届」但语义是不限
    ])
    def test_title_never_returns_unlimited(self, title: str) -> None:
        """标题通道永不返回 []。

        [] 的语义是「任何届别都命中」。标题里出现「不限/所有/任意/any」多半跟届别
        无关（实测撞上过前三条），返回 [] 等于凭一个无关词把岗位判成确定命中。
        """
        assert grad_years_from_title(title) != []


class TestCityWildcards:
    @pytest.mark.parametrize("raw", [
        "全国", "不限", "工作地点不限", "远程", "Remote", "remote",
        "全国多地", "多地", "居家办公", "全球",
    ])
    def test_wildcards_recognized(self, raw: str) -> None:
        assert is_city_wildcard(raw)

    @pytest.mark.parametrize("raw", ["北京", "深圳", "上海", "成都", "香港", ""])
    def test_real_cities_are_not_wildcards(self, raw: str) -> None:
        assert not is_city_wildcard(raw)

    def test_any_wildcard_in_list_counts(self) -> None:
        assert any_city_ok(["成都", "全国"])
        assert not any_city_ok(["成都", "武汉"])


class TestMissingInfoIsNotAMiss:
    """reviewer 指出的两个 case。这两条是本次改动的核心。"""

    def test_missing_grad_year_is_unknown_not_dropped(self) -> None:
        v = classify(job(grad_year=None), INTENT)
        assert v.state == "unknown"
        assert "届别" in v.reason

    def test_empty_cities_is_unknown_not_dropped(self) -> None:
        v = classify(job(cities=[]), INTENT)
        assert v.state == "unknown"
        assert "城市" in v.reason

    def test_missing_family_is_unknown(self) -> None:
        assert classify(job(job_family=None), INTENT).state == "unknown"

    def test_missing_recruit_type_is_unknown(self) -> None:
        assert classify(job(recruit_type=None), INTENT).state == "unknown"

    def test_unknown_lists_every_missing_field(self) -> None:
        v = classify(job(grad_year=None, cities=[]), INTENT)
        assert len(v.unknowns) == 2


class TestWildcardValuesHit:
    """「全国」「2026-2027年毕业」是最该推的那批，必须是确定命中。"""

    @pytest.mark.parametrize("cities", [["全国"], ["不限"], ["远程"], ["成都", "全国"]])
    def test_wildcard_city_hits(self, cities: list[str]) -> None:
        assert classify(job(cities=cities), INTENT).state == "hit"

    @pytest.mark.parametrize("raw", ["2026-2027年毕业", "26/27届", "不限"])
    def test_interval_and_unlimited_grad_year_hits(self, raw: str) -> None:
        assert classify(job(grad_year=raw), INTENT).state == "hit"

    def test_profile_may_write_four_digit_years(self) -> None:
        """画像写 2026，库里存 26，两边都归一到两位才对得上。"""
        intent = {**INTENT, "grad_years": ["2026", "2027"]}
        assert classify(job(grad_year="26"), intent).state == "hit"


class TestRealMissesStillMiss:
    """放宽不能放到什么都命中。"""

    def test_wrong_grad_year(self) -> None:
        assert classify(job(grad_year="2024届"), INTENT).state == "miss"

    def test_wrong_city(self) -> None:
        assert classify(job(cities=["成都"]), INTENT).state == "miss"

    def test_wrong_family(self) -> None:
        assert classify(job(job_family="tech"), INTENT).state == "miss"

    def test_wrong_recruit_type(self) -> None:
        assert classify(job(recruit_type="social"), INTENT).state == "miss"

    def test_exclude_keyword(self) -> None:
        assert classify(job(title="运营外包"), INTENT).state == "miss"

    def test_exclude_beats_missing_info(self) -> None:
        """确定不该推的，不要因为别的字段缺失就升级成「你自己看一眼」。"""
        v = classify(job(title="运营派遣", grad_year=None, cities=[]), INTENT)
        assert v.state == "miss"
        assert "派遣" in v.reason

    def test_empty_intent_matches_everything(self) -> None:
        assert classify(job(grad_year=None, cities=[], job_family=None), {}).state == "hit"


class TestPartition:
    def test_splits_three_ways(self) -> None:
        rows = [
            job(title="产品运营"),                        # hit
            job(title="内容运营", grad_year=None),         # unknown
            job(title="后台开发", job_family="tech"),      # miss
        ]
        hits, unsure = partition(rows, INTENT)
        assert [r["title"] for r in hits] == ["产品运营"]
        assert [r["title"] for r in unsure] == ["内容运营"]

    def test_unknown_carries_why_for_display(self) -> None:
        _, unsure = partition([job(cities=[])], INTENT)
        assert "_why" in unsure[0] and unsure[0]["_why"]

    def test_partition_does_not_mutate_input(self) -> None:
        row = job(cities=[])
        partition([row], INTENT)
        assert "_why" not in row

    def test_sorted_by_score(self) -> None:
        intent = {**INTENT, "boost_keywords": ["内容运营"]}
        rows = [job(title="产品运营"), job(title="内容运营")]
        hits, _ = partition(rows, intent)
        assert hits[0]["title"] == "内容运营"

    def test_unknown_carries_machine_readable_missing(self) -> None:
        """_why 给人看，_missing 给 filter_jobs 看。两者都要带上。"""
        _, unsure = partition([job(grad_year=None)], INTENT)
        assert unsure[0]["_missing"] == ("grad_year",)


class TestTitleFallbackInClassify:
    """字段没给届别时退到标题。飞书四家全靠这条通路。"""

    def test_title_grad_year_upgrades_unknown_to_hit(self) -> None:
        v = classify(job(grad_year=None, title="【27届校招】产品运营"), INTENT)
        assert v.state == "hit"

    def test_title_grad_year_can_cause_miss(self) -> None:
        """反向用例：标题写的届别不合，要判 miss，不能因为字段空就升成 unknown。"""
        v = classify(job(grad_year=None, title="【24届校招】产品运营"), INTENT)
        assert v.state == "miss"
        assert "标题" in v.reason

    def test_field_wins_over_title(self) -> None:
        """标题是回退，不是覆盖。字段有值时不看标题。"""
        v = classify(job(grad_year="26", title="【24届校招】产品运营"), INTENT)
        assert v.state == "hit"

    def test_title_without_jie_stays_unknown(self) -> None:
        """标题里的裸年份不认。判据是「届」字级，不是年份级。"""
        v = classify(job(grad_year=None, title="2026年校园大使"), INTENT)
        assert v.state == "unknown"
        assert v.missing == ("grad_year",)

    def test_missing_keys_are_stable(self) -> None:
        """机读键不随人读文案变。这些键是 --allow-missing 的参数值。"""
        v = classify(
            job(grad_year=None, cities=[], job_family=None, recruit_type=None), INTENT
        )
        assert set(v.missing) == {"grad_year", "cities", "job_family", "recruit_type"}
        assert set(v.missing) <= set(MISSING_DIMS)


class TestAllowMissingIsPerDimension:
    """按维度放宽，不是一个布尔开关。

    原先只有 include_unknown 一个开关：一按下去四个维度同时放开，于是「族、类型、
    城市都已确认、只差届别」的 1911 条，和「连是不是运营岗都不知道」的 581 条混在
    同一个结果里。这两种「不确定」的可信度差得远，不该共用一个开关。
    """

    ROWS = [
        job(title="确定命中"),                                   # hit
        job(title="只差届别", grad_year=None),                    # unknown: grad_year
        job(title="只差族", job_family=None),                     # unknown: job_family
        job(title="届别和族都缺", grad_year=None, job_family=None),  # unknown: 两维
        job(title="后台开发", job_family="tech"),                  # miss
    ]

    def _titles(self, **kw) -> list[str]:
        return [r["title"] for r in filter_jobs(self.ROWS, INTENT, **kw)]

    def test_default_is_strict(self) -> None:
        assert self._titles() == ["确定命中"]

    def test_none_and_empty_set_mean_the_same(self) -> None:
        """不制造第三种含义：None 和 frozenset() 都是严格。"""
        assert self._titles(allow_missing=None) == self._titles(allow_missing=set())

    def test_allow_missing_is_per_dimension(self) -> None:
        """放开届别，进来的只有「只差届别」——族缺的两条要挡住。

        断言的是「进来了哪些、挡住了哪些」，不是条数：如果 allow_missing 被当成
        布尔用（一放全放），条数断言可能碰巧还对，成员断言不会对。
        """
        got = self._titles(allow_missing={"grad_year"})
        assert set(got) == {"确定命中", "只差届别"}
        assert "只差族" not in got and "届别和族都缺" not in got

    def test_multi_dimension_needs_all_allowed(self) -> None:
        """缺两维的行，两维都放开才进来。"""
        assert "届别和族都缺" not in self._titles(allow_missing={"job_family"})
        assert "届别和族都缺" in self._titles(allow_missing={"grad_year", "job_family"})

    def test_loose_still_allows_everything(self) -> None:
        """老的 include_unknown=True 等于放开全部维度，行为不能改坏。"""
        got = self._titles(allow_missing=MISSING_DIMS)
        assert set(got) == {"确定命中", "只差届别", "只差族", "届别和族都缺"}

    def test_miss_never_comes_back(self) -> None:
        """放宽不能放到确定不命中的也回来。"""
        assert "后台开发" not in self._titles(allow_missing=MISSING_DIMS)


class TestBackwardCompat:
    """matches() 的旧签名还有调用方，不能改坏。"""

    def test_returns_bool_and_reason(self) -> None:
        ok, reason = matches(job(), INTENT)
        assert ok is True and isinstance(reason, str)

    def test_unknown_is_not_a_confident_hit(self) -> None:
        ok, _ = matches(job(grad_year=None), INTENT)
        assert ok is False

    def test_city_list_reads_json_string_from_db(self) -> None:
        assert city_list({"cities": '["北京","上海"]'}) == ["北京", "上海"]

    def test_city_list_survives_dirty_data(self) -> None:
        assert city_list({"cities": "北京"}) == []
        assert city_list({"cities": None}) == []

    def test_wildcard_scores_below_explicit_city(self) -> None:
        assert score(job(cities=["全国"]), INTENT) < score(job(cities=["北京"]), INTENT)
