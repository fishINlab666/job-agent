"""守 `scripts/measure_family_gaps.py` 的试算函数。

**为什么一个测量脚本值得测。** 它的输出会被抄进 issue #8 和方案 017 ——
错数比不出数难发现得多。issue #8 原文那句「一个大桶，顾问 225 条占 34.2%」
就是这个脚本的第一版按 distinct 标题计算出来的，而 nio 把城市拼进了标题，
于是同一个词表缺口被数成了 206 个。数字错了，据它做的决策也就错了。

这里只测**不碰库**的那部分：`_judge_with()` 的试算逻辑。
`_stem_map()` / `vocab_curve()` 要读真库，它们的自证写在脚本里
（`disagree` / `drift` 两个检查，不一致就拒绝出数），由 §9 的命令 B、C 覆盖。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from jobagent.normalize import TITLE_RULES, family_from_title

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """按路径加载测量脚本。它不是包的一部分，但试算逻辑是承重的。"""
    spec = importlib.util.spec_from_file_location(
        "measure_family_gaps", ROOT / "scripts" / "measure_family_gaps.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mfg = _load()

# 试算函数要在这些标题上和线上判据逐条一致。覆盖三层各一条 + 判不出一条。
SAMPLE = [
    "混元多模态-视觉编码器技术研究",        # 第 0 层
    "员工福利实习生",                        # 第 1 层
    "多媒体客户端研发实习生",                # 第 1.5 层
    "内容运营实习生",                        # 第 2 层
    "校招实习-蔚来顾问-上海",                # 第 2 层（本次新增）
    "【27届校招】采购专业培训生（综合采购）",  # 第 2 层末尾（本次新增）
    "实习-NSC售后服务代表",                  # 判不出
    "实习-PMO",                              # 判不出
]


class TestSimulatorMatchesProduction:
    """`extra=None` 的试算必须逐条等于 `family_from_title()`。

    这是所有「救回 N 行 / 改判 M 行」的地基：试算和线上不一致时，
    差额会被算进「改判」，而那些改判根本不会发生。脚本里有同名的 `drift`
    自证（对全库跑），这条测试是它的离线版本 —— 不依赖库里当前有什么数据。
    """

    @pytest.mark.parametrize("title", SAMPLE)
    def test_no_extra_equals_production(self, title: str) -> None:
        assert mfg._judge_with(title, None) == family_from_title(title)

    def test_simulator_reads_the_live_rule_table(self) -> None:
        """试算函数不许自己抄一份规则表。

        抄一份的后果是：改了 `normalize.py` 之后试算还按老表算，
        「改判 0」这个结论静默失效。判据是**加一条假规则进真表**，
        试算必须跟着变 —— 说明它读的是同一张表。
        """
        title = "完全不含任何职能词的岗位ZZZ"
        assert mfg._judge_with(title, None) is None
        TITLE_RULES.append((("岗位ZZZ",), "legal"))
        try:
            assert mfg._judge_with(title, None) == "legal", \
                "试算没跟着真规则表变，说明它抄了一份"
        finally:
            TITLE_RULES.pop()
        assert mfg._judge_with(title, None) is None


class TestInsertPositionMatters:
    """`at_end` 两个取值必须真的产生不同的插入位置。

    这个参数存在的全部理由是「第 2 层先命中的赢」，所以同一个词加进已有组
    和加在末尾，结果可以不同。如果两条路径实际上等价，那 `candidates` 打出的
    两行数就永远相同，看着像「验过两种情况」而其实只验了一种。
    """

    def test_service_into_sales_group_steals_tech(self) -> None:
        """`服务` 进 sales 组会抢走 tech —— 这正是它不该被加的理由。"""
        t = "数据分析实习生-抖音生活服务"
        assert mfg._judge_with(t, None) == "tech"
        assert mfg._judge_with(t, ("服务", "sales"), at_end=False) == "sales"

    def test_service_at_end_does_not_steal(self) -> None:
        """加在末尾则 tech 先命中，不改判。"""
        t = "数据分析实习生-抖音生活服务"
        assert mfg._judge_with(t, ("服务", "sales"), at_end=True) == "tech"

    def test_two_positions_differ_on_at_least_one_title(self) -> None:
        """守的是「参数没接线」：两个取值必须至少在一条标题上不同。"""
        diff = [
            t for t in SAMPLE + ["数据分析实习生-抖音生活服务"]
            if mfg._judge_with(t, ("服务", "sales"), at_end=False)
            != mfg._judge_with(t, ("服务", "sales"), at_end=True)
        ]
        assert diff, "两个插入位置结果完全相同，at_end 参数没起作用"


class TestDecorationListIsWordsNotFamilies:
    """`DECORATION` 和 `CANDIDATES` 里放的是标题里的词，不是族名。

    手滑把族名写进 `DECORATION` 会静默失效（真库上 0 命中），
    和 `test_word_lists_contain_no_family_names` 是同一个形状。
    """

    def test_decoration_has_no_family_names(self) -> None:
        families = {fam for _, fam in TITLE_RULES}
        for w in mfg.DECORATION:
            assert w not in families, f"{w} 是族名，不是标题里的词"

    def test_candidate_families_are_legal(self) -> None:
        """候选词的目标族必须在 FAMILIES 里 —— 拼错了会造出一个不存在的族。"""
        from jobagent.normalize import FAMILIES
        for word, fam in mfg.CANDIDATES:
            assert fam in FAMILIES, f"{word} → {fam}：{fam} 不是合法族"
            assert word not in FAMILIES, f"{word} 看着像族名"


class TestEndInsertIsBlind:
    """把「加在第 2 层末尾改判 0」这个**假判据**写成断言。

    这条测试通过不代表代码对，它代表「这个判据已知无效」被记住了。017 把这句话
    写进了 `normalize.py` 的注释，下一个人会照着用；这里钉住反例，让「它恒为 0」
    成为一个有测试撑着的事实，而不是一句注释里的话。
    """

    @pytest.mark.parametrize("word,fam", [
        ("实习生", "legal"),      # 刻意选错：招聘类型当职能
        ("运营", "tech"),         # 刻意选错：已有词换错族
        ("设计", "finance"),
        ("产品", "hr"),
    ])
    def test_wrong_word_at_end_changes_nothing(self, word: str, fam: str) -> None:
        """明显选错的词加在末尾，一条 SAMPLE 都不会改判。"""
        changed = [
            t for t in SAMPLE
            if (b := mfg._judge_with(t, None)) is not None
            and mfg._judge_with(t, (word, fam), at_end=True) != b
        ]
        assert not changed, \
            f"`{word}→{fam}` 竟然改判了 {changed} —— 那这个判据不是恒为 0，结论要重看"

    def test_wrong_word_at_end_still_rescues(self) -> None:
        """而且它照样「救回」东西 —— 这就是判据失效的机制。

        判不出的标题走到末尾，被这条错规则接住，看起来是「净收益、零代价」。
        """
        t = "实习-PMO"
        assert mfg._judge_with(t, None) is None
        assert mfg._judge_with(t, ("实习", "legal"), at_end=True) == "legal"


class TestRegionTailStripped:
    """`_stem_map()` 必须剥掉尾部的 `大区` 段，且只剥尾部。"""

    def test_region_regex_matches_tail(self) -> None:
        for t in ("新零售实习生（上海大区）", "新零售实习生（鲁豫大区）",
                  "交付实习生（浙江大区）", "【27届校招】服务大区管理培训生-深闽大区"):
            assert mfg.REGION_TAIL.search(t), f"{t} 的大区尾段没被识别"

    def test_region_regex_does_not_match_midstem(self) -> None:
        """`大区` 在词干里不许剥 —— 剥了会把两个不同的岗并成一个岗位型。"""
        assert not mfg.REGION_TAIL.search("【27届校招】服务大区管理培训生")
        assert not mfg.REGION_TAIL.search("大区经理")

    def test_region_is_not_in_city_vocab(self) -> None:
        """这条解释了为什么要单开一条正则：`大区` 写法匹配不到归一城市名。

        库里的归一城市是「上海」，标题里写的是「（上海大区）」。城市那条正则要求
        尾段**就是**城市名，`上海大区` 不是，所以漏掉了 —— 基数虚高成 428、
        剥掉之后才是 399 的原因。（方向别写反：428 是虚高的那个数。）
        """
        city_re = r"[-—－_(（\s]\s*(上海)\s*[)）]?\s*$"
        assert not re.search(city_re, "新零售实习生（上海大区）"), \
            "城市正则本来就该匹配不到，如果匹配到了说明它剥得太狠"

    def test_city_collapse_restores_the_region_regex(self, capsys) -> None:
        """`city_collapse()` 为了打「只剥城市」那一档会临时把 `REGION_TAIL` 换掉。

        换掉不还原的话，同一个进程里后面每一次 `_stem_map()` 都会退回 428 口径，
        而输出照样像模像样 —— 分母静默变大，所有百分比一起偏小。这是
        [[boundary-resting-on-downstream-coincidence]] 的形状：现在恰好没人在
        同一进程里连着调两个子命令，所以「不还原」眼下不出事。
        """
        before = mfg.REGION_TAIL
        mfg.city_collapse()
        capsys.readouterr()
        assert mfg.REGION_TAIL is before, "临时替换的正则没还原"
        assert len(mfg._stem_map()) == 399, "还原后基数必须回到 399 口径"


class TestKeepAndRejectAreDisjoint:
    """采纳表和否决表不许有交集，且两张表里都不许出现族名。"""

    def test_disjoint(self) -> None:
        keep = {w for w, _ in mfg.NEW_WORDS_018}
        rej = {w for w, _, _ in mfg.REJECTED_018}
        assert not (keep & rej), f"同时在两张表里：{keep & rej}"

    def test_no_family_names_in_either(self) -> None:
        from jobagent.normalize import FAMILIES
        for w, fam in mfg.NEW_WORDS_018:
            assert fam in FAMILIES, f"{w} → {fam} 不是合法族"
            assert w not in FAMILIES, f"{w} 看着像族名"
        for w, fam, why in mfg.REJECTED_018:
            assert fam in FAMILIES, f"{w} → {fam} 不是合法族"
            assert why, f"{w} 没写否决理由 —— 没理由的否决会被下一个人推翻"

    def test_keep_table_matches_normalize(self) -> None:
        """脚本里那份副本必须和 `TITLE_RULES` 一致，否则 `rescued` 量的是幻觉。

        脚本靠 `NEW_WORDS_018` 反推「没有这些词时是什么」。副本漂移的话，
        `_judge_without` 会漏摘或多摘词，救回数静默偏移 —— 而输出照样像模像样。

        注意这条只查**脚本有、normalize 没有**这个方向。反方向（脚本少一个词）
        由 test_keep_table_is_not_short 查，原因见那条。
        """
        for word, fam in mfg.NEW_WORDS_018:
            hit = [f for ws, f in TITLE_RULES if word in ws]
            assert hit == [fam], f"`{word}`：normalize 里是 {hit}，脚本里写 {fam}"
        for word, _fam, _why in mfg.REJECTED_018:
            for ws, f in TITLE_RULES:
                assert word not in ws, f"否决词 `{word}` 出现在 normalize 的 {f} 组里"

    def test_keep_table_is_not_short(self) -> None:
        """脚本副本**少**一个词也要红 —— 上面那条查不出这个方向。

        `mutate_018.sh` 第 9 条改坏（从 `NEW_WORDS_018` 里删掉 `税务`）当时
        全绿：上面那条是 `for word in mfg.NEW_WORDS_018`，词被删了循环就少跑
        一圈，一个断言都不会执行。「遍历被检对象」的测试天生只能查一个方向。

        对照的锚点用 test_normalize.py 里那份独立副本，不是 normalize.py 的
        `TITLE_RULES` —— 后者的第 2 层还有 017 及更早加的单字词，没法从中区分出
        「018 这 30 个」而不重新读一遍脚本的表。
        """
        from tests.test_normalize import NEW_018

        script = dict(mfg.NEW_WORDS_018)
        anchor = dict(NEW_018)
        assert script == anchor, (
            f"脚本副本少了 {set(anchor) - set(script)}，多了 {set(script) - set(anchor)}；"
            "两份副本是故意分开写的，漂移就在这里红"
        )


class TestJudgeWithoutIsWiredUp:
    """`_judge_without` 必须真的从活表里摘词，不是摆设。"""

    def test_dropping_a_word_changes_the_verdict(self) -> None:
        t = "税务实习生"
        assert mfg._judge_without(t, set()) == "finance"
        assert mfg._judge_without(t, {"税务"}) is None

    def test_empty_drop_equals_production(self) -> None:
        """`drop=∅` 必须逐条等于线上判据 —— 这是所有库级数的地基。"""
        for t in SAMPLE:
            assert mfg._judge_without(t, set()) == family_from_title(t)

    def test_drop_reads_the_live_table(self) -> None:
        """和 `test_simulator_reads_the_live_rule_table` 同一个形状。"""
        title = "完全不含任何职能词的岗位ZZZ"
        TITLE_RULES.append((("岗位ZZZ",), "legal"))
        try:
            assert mfg._judge_without(title, set()) == "legal"
            assert mfg._judge_without(title, {"岗位ZZZ"}) is None
        finally:
            TITLE_RULES.pop()


class TestSplitLastUsesLastSeparator:
    """`_split_last` 必须切**最后**一个分隔符。

    存在的理由是一次真实的探针缺陷：第一版 docstring 写「最后一个」、代码取
    `SEP.split(t)[0]`（第一个），把 issue #13 的结论从 602 条虚报成 867 条。
    探针自己的毛病穿着被测对象的症状出场，而且偏向报「坏」。
    """

    def test_splits_at_last_separator(self) -> None:
        assert mfg._split_last("AI全栈研发前端实习生 - 财经业务") == \
            ("AI全栈研发前端实习生", "财经业务")
        # 两个分隔符：必须切后面那个，头段要保留前面那个分隔符
        assert mfg._split_last("校招实习-蔚来顾问-上海") == ("校招实习-蔚来顾问", "上海")

    def test_no_separator_returns_none(self) -> None:
        assert mfg._split_last("税务实习生") is None

    def test_separator_at_edges_returns_none(self) -> None:
        """开头或结尾的分隔符切出空段，没有意义，返回 None。"""
        assert mfg._split_last("-税务实习生") is None
        assert mfg._split_last("税务实习生-") is None

    def test_slash_is_not_a_separator(self) -> None:
        """`/` 是「或」，不是组织边界。加宽会让变族数从 86 变 88。"""
        assert "/" not in mfg.SEPS
        assert mfg._split_last("前端/后端开发实习生") is None
