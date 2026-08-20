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
