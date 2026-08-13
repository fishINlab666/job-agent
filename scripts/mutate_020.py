#!/usr/bin/env python
"""逐条改坏方案 020 的判据，证明每一条**真的会红**（不是只会绿）。

为什么要这个脚本：只验绿等于没验。一条永远不红的判据和一条不存在的判据
在输出上完全一样 —— `docs/plans/018` 那个「加在表末尾改判 0 行」就是这么活了
两轮的（取值域是常数，对任何词都成立）。

这一轮被验的判据里有一半是**守卫测试自己**，所以这个脚本也得挨同样的标准：
它自己的缺陷长得像被测对象坏了（而且偏向报「坏」）。三道自防：

1. **改坏必须先落上**：`old` 不在原文里就直接抛，不然「原文没匹配上」会
   伪装成「判据没守住」。
2. **改坏必须真的改了字**：追加型（`old` 是 `new` 的子串）单独判，替换型
   要求原文消失 —— 否则「删+插的插入侧没匹配」会退化成另一条改坏。
3. **对照组必须断言，不能只打印**：既要看该红的红了，也要看**不该红的没红**，
   还要看 `-k` 匹配到的条数不是 0（`-k` 是子串选择器，写错名字会静默匹配 0 条，
   而 0 条在 pytest 里是「没失败」）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


@dataclass
class Mutation:
    num: int
    what: str          # 改坏了什么
    path: str          # 动哪个文件
    old: str
    new: str
    expect_red: str    # 该红的那条（-k 表达式）
    control: str       # 不该红的那条（-k 表达式）
    #: `old` 该命中几处。默认 1 = **必须唯一**：落点有歧义时我没法确认改坏落在
    #: 想改的那处，而「成功地改错地方」比报错难发现。`all` = 故意换掉全部
    #: （判据要求某个字串**完全不出现**时才需要，比如「SPEC 没提过 queries」）。
    count: int | str = 1


def run_k(expr: str) -> tuple[bool, int, str]:
    """跑 `-k expr`，回 (全绿?, 匹配到几条, 输出尾部)。"""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_docs_match_code.py",
         "-q", "-p", "no:cacheprovider", "-k", expr],
        cwd=ROOT, capture_output=True, text=True, env=ENV,
    )
    tail = r.stdout[-700:]
    m = re.search(r"(\d+) deselected", tail)
    total = len(re.findall(r"^tests/", tail, re.M))
    picked = 0
    if mm := re.search(r"(\d+) passed", tail):
        picked += int(mm.group(1))
    if mm := re.search(r"(\d+) failed", tail):
        picked += int(mm.group(1))
    del m, total
    return r.returncode == 0, picked, tail


SPEC = "docs/SPEC.md"
README = "README.md"
GUARD = "tests/test_docs_match_code.py"

#: 从 README 现读，不写死。写死等于把「唯一出处」那个数抄第七份 ——
#: 而这个方案要修的正是那个毛病。加一条测试就得改这个脚本，那也是烂法。
_m = re.search(r"\*\*(\d{3,})\s*个测试用例\*\*（截至 (20\d\d-\d\d-\d\d)）",
               (ROOT / README).read_text(encoding="utf-8"))
assert _m, "README 里读不到「**N 个测试用例**（截至 日期）」，改坏脚本的落点全靠它"
N, DATE = _m.group(1), _m.group(2)
COUNT_LINE = f"**{N} 个测试用例**（截至 {DATE}），跑这条看最后一行："

MUTATIONS = [
    # ── 判据 ②：SPEC 的 CLI 清单 == cli --help ──────────────────────────
    Mutation(
        1, "从 SPEC 的 CLI 清单里删掉 `health`（复现「漏第四次」）", SPEC,
        # 「`checkup` / `health`。」在 SPEC 里有 2 处（变更记录那行也这么结尾），
        # 所以带上前一行收窄到清单本身。
        old="`applications` /\n`checkup` / `health`。",
        new="`applications` /\n`checkup`。",
        expect_red="test_spec_cli_list_matches_help",
        control="test_spec_mentions_every_module",
    ),
    Mutation(
        2, "往 SPEC 的 CLI 清单里塞一个不存在的命令（反方向）", SPEC,
        old="CLI 全部命令（**12 条**）：`init` /",
        new="CLI 全部命令（**12 条**）：`init` / `teleport` /",
        expect_red="test_spec_cli_list_matches_help",
        control="test_spec_mentions_every_module",
    ),
    Mutation(
        3, "改掉「CLI 全部命令」的措辞，让正则找不到（判据的锚点失效）", SPEC,
        old="CLI 全部命令（**12 条**）：",
        new="CLI 的命令一览（**12 条**）：",
        expect_red="test_spec_cli_list_matches_help",
        control="test_spec_mentions_every_module",
    ),
    # ── 判据 ①：--help 解析器自己 ──────────────────────────────────────
    Mutation(
        4, "把 --help 解析器的命令区识别弄坏（探针自己坏掉）", GUARD,
        old='        if "Commands" in line:',
        new='        if "CommandsXX" in line:',
        expect_red="test_help_parser_finds_commands",
        control="test_spec_mentions_every_module",
    ),
    # ── 判据 ③：SPEC 认识每个模块 ──────────────────────────────────────
    Mutation(
        5, "把 SPEC 里所有 `queries` 抹掉（复现「新模块静默不进文档」）", SPEC,
        # 判据是「`queries` 这个串在 SPEC 里一次都不出现」，所以必须全换掉 ——
        # 只换一处它还在，改坏不会咬到，看起来就像判据没守住。
        old="queries", new="qqqqqq", count="all",
        expect_red="test_spec_mentions_every_module",
        control="test_spec_cli_list_matches_help",
    ),
    Mutation(
        6, "把 `mcp_server` 塞进 NOT_REQUIRED（用豁免绕开判据）", GUARD,
        old='        "__init__.py": "空的包标记",',
        new='        "__init__.py": "空的包标记",\n        "mcp_server.py": "懒得写",',
        # 这条第一次跑的时候**该红的没红**：SPEC 本来就提了 mcp_server，
        # 所以加豁免不改变 test_spec_mentions_every_module 的结果 ——
        # 豁免口当时没有任何判据看着它。补了
        # test_exemptions_are_only_for_empty_modules 之后才咬得住。
        expect_red="test_exemptions_are_only_for_empty_modules",
        control="test_spec_mentions_every_module",
    ),
    # ── 判据 ⑧⑨：README 分文件表覆盖每个测试文件（两个方向）────────────
    Mutation(
        15, "从 README 的分文件表里删掉一行（复现「8 个文件在表外」）", README,
        old="| `test_queries.py` | 只读查询层 |\n",
        new="",
        expect_red="test_every_test_file_is_in_the_table",
        control="test_table_has_no_file_that_does_not_exist",
    ),
    Mutation(
        16, "往表里塞一个不存在的测试文件（反方向）", README,
        old="| `test_e2e.py` |",
        new="| `test_ghost.py` | 不存在的文件 |\n| `test_e2e.py` |",
        expect_red="test_table_has_no_file_that_does_not_exist",
        control="test_every_test_file_is_in_the_table",
    ),
    Mutation(
        17, "把表头的列名改掉（判据的锚点失效）", README,
        old="| 文件 | 覆盖 |",
        new="| 测试文件 | 覆盖范围 |",
        expect_red="test_every_test_file_is_in_the_table",
        control="test_readme_count_is_actually_true",
    ),
    # ── 判据 ④：SPEC 不许钉当前用例数 ──────────────────────────────────
    Mutation(
        7, "把钉死的基线数放回 SPEC §9（复现 464 那次）", SPEC,
        old="新功能的验收 = 这个数**变大**且全绿。",
        new="新功能的验收 = 这个数**变大**且全绿。当前基线 464 passed。",
        expect_red="test_no_pinned_current_count",
        control="test_readme_count_is_actually_true",
    ),
    Mutation(
        8, "把基线数钉在 SPEC 页首（另一个位置）", SPEC,
        old="# 方案文档：校招 Agent 当前实现",
        new=f"# 方案文档：校招 Agent 当前实现\n\n测试基线 **{N} passed**。",
        expect_red="test_no_pinned_current_count",
        control="test_readme_count_is_actually_true",
    ),
    # ── 判据 ⑤：README 的数旁边有命令和日期 ────────────────────────────
    Mutation(
        9, "拿掉 README 用例数旁边的日期", README,
        old=COUNT_LINE,
        new=f"**{N} 个测试用例**，跑这条看最后一行：",
        expect_red="test_readme_count_travels_with_its_command",
        control="test_readme_count_is_actually_true",
    ),
    Mutation(
        10, "拿掉 README 用例数旁边的 pytest 命令", README,
        old=f"{COUNT_LINE}\n\n```bash\nuv run pytest -q\n```",
        new=f"**{N} 个测试用例**（截至 {DATE}）。",
        expect_red="test_readme_count_travels_with_its_command",
        control="test_no_pinned_current_count",
    ),
    Mutation(
        11, "把 README 的唯一出处整句删掉（出处消失）", README,
        old=COUNT_LINE,
        new="测试跑这条看最后一行：",
        expect_red="test_readme_count_travels_with_its_command",
        control="test_spec_cli_list_matches_help",
    ),
    # ── 判据 ⑥：README 的数必须是真的 ──────────────────────────────────
    Mutation(
        12, "把 README 的数改成假的（格式全合规，只是不对 —— 504 那次的形状）", README,
        old=f"**{N} 个测试用例**（截至 {DATE}）",
        new=f"**504 个测试用例**（截至 {DATE}）",
        expect_red="test_readme_count_is_actually_true",
        control="test_readme_count_travels_with_its_command",
    ),
    Mutation(
        14, "只把紧跟着数的日期挪远（正文里另一处日期还在）", README,
        # 改坏 9 第一次跑时该红的没红，就是因为判据拿 600 字窗口找日期，
        # 被正文里引用 SPEC 的那个 `2026-08-13` 喂饱了。这条钉住修法：
        # 日期挪到下一段，窗口里**仍然有**日期，但它不再跟着这个数。
        # 第一版这条写成「挪到句尾（统计日期 X）」，结果**该红的没红** ——
        # 那是我的改坏太软：15 字、同一行，本来就算「跟着这个数」，判据没错。
        # 要打的口子是**跨段**：窗口里仍然有日期，但它不再属于这个数。
        old=COUNT_LINE,
        new=f"**{N} 个测试用例**，跑这条看最后一行：\n\n统计日期 {DATE}。",
        expect_red="test_readme_count_travels_with_its_command",
        control="test_readme_count_is_actually_true",
    ),
    Mutation(
        13, "把 --collect-only 锚点弄坏（锚点自己坏掉该有自己的话说）", GUARD,
        old='             "--collect-only", str(ROOT / "tests")],',
        new='             "--collect-only", "-k", "nothing_matches_this", str(ROOT / "tests")],',
        expect_red="test_readme_count_is_actually_true",
        control="test_readme_count_travels_with_its_command",
    ),
]


def main() -> int:
    # 工作树必须干净：残留的改坏态会伪装成被测对象的缺陷，而且伪装得合理。
    dirty = subprocess.run(["git", "status", "--porcelain",
                            SPEC, README, GUARD],
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if dirty:
        print(f"工作树不干净，先处理掉再跑：\n{dirty}")
        return 2

    ok, picked, tail = run_k("test_")
    if not ok:
        print(f"改坏之前套件就不是绿的，先修：\n{tail}")
        return 2
    print(f"基线：{picked} 条全绿\n")

    failures: list[str] = []
    for m in MUTATIONS:
        target = ROOT / m.path
        backup = target.read_text(encoding="utf-8")

        hits = backup.count(m.old)
        if hits == 0:
            failures.append(f"改坏 {m.num} 的原文没找到（落点写错了，不是判据的问题）")
            print(f"[{m.num:>2}] 落点没找到 ✗  {m.what}")
            continue
        if m.count == 1 and hits != 1:
            failures.append(
                f"改坏 {m.num} 的落点命中 {hits} 处，不唯一。歧义落点会让我"
                f"「成功地改错地方」，而那比报错难发现。要么把 old 写长，"
                f"要么显式声明 count='all'。")
            print(f"[{m.num:>2}] 落点命中 {hits} 处 ✗  {m.what}")
            continue

        try:
            n = -1 if m.count == "all" else 1
            target.write_text(backup.replace(m.old, m.new, n), encoding="utf-8")
            landed = target.read_text(encoding="utf-8")

            # 改坏真的落上了吗 —— 「文件变了」不等于「这条改坏落上了」。
            assert m.new.split("\n")[0] in landed or m.new in landed, \
                f"改坏 {m.num} 的新文本没出现在文件里"
            if m.old in m.new:
                # 追加型：`old` 是 `new` 的子串，「原文消失」永远不成立。
                assert landed.count(m.old) >= 1, f"改坏 {m.num} 是追加型，原文不该消失"
            else:
                assert m.old not in landed, \
                    f"改坏 {m.num} 的原文还在（原有 {hits} 处，换掉 {n} 处）"

            red_ok, red_n, red_tail = run_k(m.expect_red)
            ctl_ok, ctl_n, ctl_tail = run_k(m.control)
        finally:
            target.write_text(backup, encoding="utf-8")

        # `-k` 是子串选择器：匹配 0 条在 pytest 里长得像「没失败」。
        if red_n == 0:
            failures.append(f"改坏 {m.num}：`-k {m.expect_red}` 匹配 0 条")
        elif red_ok:
            failures.append(f"改坏 {m.num}：该红的没红 —— {m.what}\n{red_tail}")

        if ctl_n == 0:
            failures.append(f"改坏 {m.num}：对照组 `-k {m.control}` 匹配 0 条")
        elif not ctl_ok:
            failures.append(
                f"改坏 {m.num}：对照组红了。要么改坏的波及面超出判据，"
                f"要么对照组挑错了 —— 两者输出一样，得看内容。\n{ctl_tail}")

        mark = "✓" if (red_n and not red_ok and ctl_n and ctl_ok) else "✗"
        print(f"[{m.num:>2}] 该红 {red_n} 条→{'红' if not red_ok else '绿'} · "
              f"对照 {ctl_n} 条→{'绿' if ctl_ok else '红'} {mark}  {m.what}")

    # 还原干净吗 —— 重排式改坏会留过期 .pyc，还原后假装还是红的。
    left = subprocess.run(["git", "status", "--porcelain", SPEC, README, GUARD],
                          cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if left:
        failures.append(f"跑完没还原干净：\n{left}")

    print()
    if failures:
        print(f"有 {len(failures)} 处不对：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{len(MUTATIONS)} 条改坏全部按预期变红，对照组全部保持绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
