#!/usr/bin/env python3
"""逐条改坏 019 的判据，证明每条都能红。

为什么是脚本不是手改：手改 11 处、每处跑一遍再还原，中间一次忘还原，
后面所有结果都在一个脏工作树上产生 —— 而脏工作树的表现和「判据失守」长得一样。

这个脚本自己也要满足它施加给别人的标准：

- **跑之前确认工作树干净**（残留的改坏态会伪装成被测对象的缺陷）；
- **断言改坏真的落上了**：替换后源码里必须出现指定字符串。只比「文件变了」不够 ——
  删+插的插入侧没匹配上，文件也会变，但那是另一条改坏；
- **对照组要断言，不能只打印**：不该红的那条必须还是绿的，否则「全都红了」
  会被当成「判据很灵敏」；
- **`-k` 匹配 0 条算失败**：选择器写错时 pytest 退出码是 0，看起来像绿。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "jobagent" / "mcp_server.py"
INGEST = ROOT / "jobagent" / "ingest.py"
CLI = ROOT / "jobagent" / "cli.py"
TESTS = ROOT / "tests" / "test_mcp_server.py"

# (编号, 说明, 目标文件, 原文, 改成, 该红的测试选择器, 该绿的对照组选择器)
MUTATIONS: list[tuple[str, str, Path, str, str, str, str]] = [
    (
        "1", "白名单退回 4 种（这次修的缺陷本身）", SERVER,
        '    "job_reopened", "family_first_seen", "batch_started",\n', "",
        "test_every_ingest_kind_is_whitelisted",
        "test_apply_events_are_filtered_out",
    ),
    (
        "2", "白名单里加一个拼错的 kind", SERVER,
        '"job_reopened", "family_first_seen", "batch_started",',
        '"job_reopened", "family_first_seen", "batch_started", "job_reopend",',
        "test_whitelist_has_no_kind_ingest_never_emits",
        "test_every_ingest_kind_is_whitelisted",
    ),
    (
        "3", "锚点匹配条件写窄，一个调用点都找不到", TESTS,
        'and node.func.attr == "add_event"):',
        'and node.func.attr == "add_event_NOPE"):',
        "test_anchor_sees_every_add_event_site",
        # 对照组特意选覆盖测试：锚点数出 0 个调用点的时候，它照样**绿**
        # （空集减白名单是空集）。这就是「一条都没匹配上」和「全都合规」长得一样，
        # 也正是 `test_anchor_sees_every_add_event_site` 存在的理由。
        # 对照组绿在这里不是「没受影响」，是把假绿本身钉下来。
        "test_every_ingest_kind_is_whitelisted",
    ),
    (
        "4", "锚点不摊开三元，kind 数掉到等于调用点数", TESTS,
        "            branches = ([arg.body, arg.orelse] if isinstance(arg, ast.IfExp)\n"
        "                        else [arg])",
        "            branches = [arg]",
        "test_anchor_sees_every_add_event_site",
        # 不能拿 `test_ingest_kinds_are_all_enumerable` 当对照组：不摊开三元，
        # 那个 IfExp 就落进 `dynamic`，它**理应**跟着红。选一条服务端行为测试。
        "test_apply_events_are_filtered_out",
    ),
    (
        "5", "采集侧写一个拼出来的 kind（锚点前提过期）", INGEST,
        'conn, "job_closed", source_key=adapter.source_key,',
        'conn, f"job_{\'closed\'}", source_key=adapter.source_key,',
        "test_ingest_kinds_are_all_enumerable",
        # 又一个「对照组绿 = 假绿被钉住」：`job_closed` 变成拼出来的之后，
        # 采集侧枚举只剩 6 种，覆盖测试比的是那 6 种，全在白名单里，所以它绿。
        # 锚点悄悄变窄而覆盖测试不会喊 —— 喊的是前提那条。
        "test_every_ingest_kind_is_whitelisted",
    ),
    (
        "6", "代投侧改成字面量（黑名单理由不再成立）", CLI,
        'db.add_event(conn, f"apply_{result.status}", source_key=src,',
        'db.add_event(conn, "apply_done", source_key=src,',
        "test_apply_side_is_the_one_that_cannot_be_enumerated",
        "test_every_ingest_kind_is_whitelisted",
    ),
    (
        "7", "docstring 退回「省略则全要」", SERVER,
        "**省略则给全部采集侧事件 —— 不是这张表\n    的全部。**",
        "省略则全要。",
        "test_docstring_does_not_promise_everything",
        "test_apply_events_are_filtered_out",
    ),
    (
        "8", "返回值里去掉 excluded_kinds", SERVER,
        '    return {"events": events[:limit], "excluded_kinds": list(EXCLUDED_KINDS)}',
        '    return {"events": events[:limit]}',
        "test_excluded_kinds_is_reported",
        "test_apply_events_are_filtered_out",
    ),
    (
        "9", "excluded_kinds 只在不指定 kind 时给", SERVER,
        '    return {"events": events[:limit], "excluded_kinds": list(EXCLUDED_KINDS)}',
        '    out = {"events": events[:limit]}\n'
        "    if kind is None:\n"
        '        out["excluded_kinds"] = list(EXCLUDED_KINDS)\n'
        "    return out",
        "test_excluded_kinds_is_reported_even_for_one_kind",
        # 对照组必须排掉 `_even_for_one_kind` —— 短名是长名的**子串**，
        # `-k test_excluded_kinds_is_reported` 会把要红的那条一起选进对照组，
        # 于是「对照组也红了」，看起来像这条改坏影响面太宽。
        # 【这一行是脚本自己撞出来的】子串选择器要校验唯一，`-k` 也算选择器。
        "test_excluded_kinds_is_reported and not even_for_one_kind",
    ),
    (
        "10", "EXCLUDED_KINDS 写成空（「什么都没排除」）", SERVER,
        'EXCLUDED_KINDS = ("apply_*",)', "EXCLUDED_KINDS = ()",
        "test_excluded_kinds_is_never_empty",
        # 不能拿 `test_excluded_kinds_is_reported` 当对照组：表空了之后
        # `any("apply" in k for k in [])` 是 False，它理应跟着红。
        "test_apply_events_are_filtered_out",
    ),
    (
        "11", "把 kind=None 透传给下一层（PII 外泄那条路）", SERVER,
        "    kinds = [kind] if kind else sorted(JOB_EVENT_KINDS)\n"
        "    events: list[dict] = []\n"
        "    for k in kinds:\n"
        "        events += queries.job_changes(conn, kind=k, since=since, limit=limit)",
        "    events = queries.job_changes(conn, kind=kind, since=since, limit=limit)",
        "test_apply_events_are_filtered_out",
        "test_excluded_kinds_is_reported",
    ),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def pytest_result(selector: str) -> tuple[bool, int, str]:
    """跑一次 pytest。返回 (是否全绿, 匹配到的测试数, 尾部输出)。

    匹配 0 条时 pytest 退出码是 0 —— 看起来跟绿一样。所以这里把「匹配到几条」
    单独解析出来交给调用方判断，不靠退出码。
    """
    r = subprocess.run(
        [".venv/bin/pytest", "tests/test_mcp_server.py", "-k", selector,
         "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = r.stdout + r.stderr
    n = 0
    for m in re.finditer(r"(\d+) (?:passed|failed)", out):
        n += int(m.group(1))
    return r.returncode == 0, n, out.strip().splitlines()[-1] if out.strip() else ""


def main() -> int:
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                           capture_output=True, text=True).stdout
    tracked = [ln for ln in dirty.splitlines() if not ln.startswith("??")]
    print("工作树上已改动的被跟踪文件（应当只有这次要提交的那些）:")
    for ln in tracked:
        print("   ", ln)
    print()

    baseline = {p: sha(p) for p in (SERVER, INGEST, CLI, TESTS)}
    ok, n, tail = pytest_result("TestOnlyJobSideEvents or TestWhitelistCoversEveryIngestKind")
    print(f"[基线] 全绿={ok} 匹配={n} :: {tail}")
    if not ok or n == 0:
        print("!! 基线就不干净，后面的结果都不能信")
        return 1

    failures = []
    for num, desc, path, old, new, red_sel, green_sel in MUTATIONS:
        backup = path.read_text(encoding="utf-8")
        if old not in backup:
            failures.append(f"改坏 {num}：原文没匹配上，这条改坏根本没施加")
            print(f"[{num}] !! 原文没匹配上 —— {desc}")
            continue
        mutated = backup.replace(old, new, 1)
        path.write_text(mutated, encoding="utf-8")
        try:
            landed = path.read_text(encoding="utf-8")
            # 断言改坏**落上了**，而不是只断言「文件变了」：
            # 删+插的插入侧没匹配上，文件也会变，但那是另一条改坏。
            assert sha(path) != baseline[path], f"改坏 {num} 没改动文件"
            if new.strip():
                assert new.strip().splitlines()[0] in landed, \
                    f"改坏 {num} 的插入侧没出现在源码里"
            if old in new:
                # 追加式改坏（往表里塞一个拼错的词）：`old` 是 `new` 的子串，
                # 「原文消失了」永远不成立。这里改成数出现次数变多。
                # 【这个分支是这个脚本自己被改坏 2 撞出来的】原先无条件断言
                # `old not in landed`，脚本当场炸在改坏 2 上 —— 断言写的是
                # 「替换型改坏」的形状，而表里有一条是追加型。
                assert landed.count(old) >= backup.count(old), \
                    f"改坏 {num} 是追加型，原文出现次数不该变少"
            else:
                assert old not in landed, f"改坏 {num} 的原文还在（替换没生效）"

            red_ok, red_n, red_tail = pytest_result(red_sel)
            if red_n == 0:
                failures.append(f"改坏 {num}：`-k {red_sel}` 匹配 0 条测试")
                print(f"[{num}] !! -k 匹配 0 条 —— {desc}")
                continue
            if red_ok:
                failures.append(f"改坏 {num}：{red_sel} 没红（{desc}）")
                print(f"[{num}] !! 没红 {red_sel} —— {desc}")
                continue

            line = f"[{num}] 红了 ({red_n} 条匹配) {red_sel} —— {desc}"

            if green_sel:
                g_ok, g_n, _ = pytest_result(green_sel)
                if g_n == 0:
                    failures.append(f"改坏 {num}：对照组 `-k {green_sel}` 匹配 0 条")
                    print(f"[{num}] !! 对照组匹配 0 条 —— {green_sel}")
                    continue
                if not g_ok:
                    failures.append(
                        f"改坏 {num}：对照组 {green_sel} 也红了 —— "
                        f"这条改坏的影响面比判据宽，红不能算作判据生效")
                    print(f"[{num}] !! 对照组也红了 —— {green_sel}")
                    continue
                line += f" | 对照组绿 ({g_n} 条) {green_sel}"
            print(line)
        finally:
            path.write_text(backup, encoding="utf-8")
            assert sha(path) == baseline[path], f"改坏 {num} 还原失败！"

    print()
    for p, h in baseline.items():
        assert sha(p) == h, f"{p.name} 没还原干净"
    print("四个文件都还原到基线 hash。")

    ok, n, tail = pytest_result("TestOnlyJobSideEvents or TestWhitelistCoversEveryIngestKind")
    print(f"[还原后] 全绿={ok} 匹配={n} :: {tail}")
    if not ok:
        failures.append("还原之后没回到全绿")

    if failures:
        print(f"\n{len(failures)} 条没通过：")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\n{len(MUTATIONS)} 条改坏全部按预期变红，对照组全部保持绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
