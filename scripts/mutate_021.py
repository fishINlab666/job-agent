#!/usr/bin/env python
"""逐条改坏方案 021 的判据，证明每一条**真的会红**。

这一轮和 015–020 有个结构性差别：021 的判据里有一半查的不是**文件内容**，
而是**环境状态**（venv 里装了什么）。改坏方式因此分两类，混着做会得到假绿：

- **文件级改坏**：改 `pyproject.toml` / `uv.lock` / 文档，还原就是改回来。
- **环境级改坏**：把包从 venv 里卸掉。这类有个陷阱 —— `uv run pytest` 会**自动
  重新同步**，把我刚卸掉的包装回去，于是判据永远绿。所以环境级改坏必须直连
  `.venv/bin/pytest`，绕开 uv 的自动同步。

三道自防沿用 020：
1. 改坏必须先落上（`old` 不在原文里直接抛）
2. 改坏必须真的改了字
3. 对照组必须断言，且 `-k` 匹配到的条数不能是 0
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
MCP_SETUP = ROOT / "docs" / "MCP_SETUP.md"
GUARD = ROOT / "tests" / "test_packaging.py"
VENV_PYTEST = ROOT / ".venv" / "bin" / "pytest"

# 环境级改坏用直连 pytest；文件级也用它，省得两套行为不一致。
BASE = [str(VENV_PYTEST), "-q", "-p", "no:cacheprovider", "--no-header"]


class Mutation:
    """一条改坏：改哪个文件、改什么、该红哪条、对照组是哪条。"""

    def __init__(self, desc: str, target: Path | None, old: str, new: str,
                 should_red: str, control: str, *, uninstall: bool = False,
                 resync: bool = False, snapshot: bool = False):
        self.desc = desc
        self.target = target
        self.old = old
        self.new = new
        self.should_red = should_red
        self.control = control
        self.uninstall = uninstall
        #: 改 `pyproject.toml` 的入口点**不会**重新生成 `.venv/bin/` 里那个脚本。
        #: 要让这类改坏落到环境里，得改完再同步一次 —— 否则改坏落在文件上、
        #: 判据查的是环境，两边不碰面，表现是「该红的没红」。
        self.resync = resync
        #: 把包装成**非 editable 快照副本**：导入仍然成功，但导入到的是
        #: `site-packages/jobagent/`，改仓库里的代码不生效。这个状态是
        #: 「卸掉包」验不出来的 —— 卸掉之后 import 直接失败，第一条断言先红，
        #: 「哪一份副本」这个问题根本问不到。
        self.snapshot = snapshot


def run_k(expr: str) -> tuple[bool, int]:
    """跑 `-k expr`，返回（有没有失败, 匹配到几条）。

    条数单独回传：`-k` 是子串选择器，名字写错会静默匹配 0 条，
    而 0 条在 pytest 里是「没失败」—— 那是假绿，必须当错误。
    """
    r = subprocess.run(BASE + ["-k", expr, str(GUARD)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    m = re.search(r"(\d+)/(\d+) tests collected", out) or re.search(
        r"(\d+) passed|(\d+) failed", out)
    selected = 0
    m2 = re.search(r"(\d+) deselected", out)
    total_m = re.search(r"(\d+) passed", out)
    failed_m = re.search(r"(\d+) failed", out)
    selected = (int(total_m.group(1)) if total_m else 0) + \
               (int(failed_m.group(1)) if failed_m else 0)
    return bool(failed_m), selected


def apply_mutation(m: Mutation) -> str:
    """落上改坏，返回原文以便还原。"""
    text = m.target.read_text(encoding="utf-8")
    if m.old not in text:
        raise SystemExit(
            f"✗ 改坏落不上：{m.target.name} 里找不到\n    {m.old[:90]!r}\n"
            f"  （不抛的话，「原文没匹配上」会伪装成「判据没守住」）")
    new_text = text.replace(m.old, m.new, 1)
    if new_text == text:
        raise SystemExit(f"✗ 改坏没改动任何字：{m.desc}")
    # 替换型要求原文真的消失；追加型（old 是 new 的子串）跳过这条。
    if m.old not in m.new and m.old in new_text:
        raise SystemExit(
            f"✗ 改坏只加没删：{m.desc}\n  原文还在，这会退化成另一条改坏")
    m.target.write_text(new_text, encoding="utf-8")
    return text


def _installed() -> bool:
    """包在不在环境里 —— 拿 site-packages 里的 editable 指针当判据。"""
    return any(p.name.startswith("_editable_impl_job_agent")
               or p.name.startswith("job_agent-")
               for p in (ROOT / ".venv" / "lib").rglob("site-packages/*"))


def uninstall_project() -> None:
    """把包从环境里卸掉，**并断言真的卸掉了**。

    第一版这里用 `python -m pip uninstall`，而 uv 建的 venv 默认不装 pip ——
    命令静默失败（`No module named pip`），包还在，判据当然不红，
    表现和「判据没守住」一模一样。改坏必须先落上，环境级也不例外。
    """
    subprocess.run(["uv", "pip", "uninstall", "job-agent"],
                   cwd=ROOT, capture_output=True, text=True, timeout=300)
    if _installed():
        raise SystemExit(
            "✗ 环境级改坏落不上：包还在 site-packages 里。"
            "不抛的话，「卸载没成功」会伪装成「判据没守住」")


def reinstall_project() -> None:
    subprocess.run(["uv", "sync", "--all-extras"],
                   cwd=ROOT, capture_output=True, text=True, timeout=600)
    if not _installed():
        raise SystemExit("✗ 还原失败：包没装回去，后面每一条都会假红")


def _editable_pth() -> Path | None:
    for p in (ROOT / ".venv" / "lib").rglob("site-packages/_editable_impl_job_agent.pth"):
        return p
    return None


def snapshot_install() -> None:
    """装成非 editable 快照副本，**并断言状态真的换了**。

    判据是 editable 指针消失、而 `site-packages/jobagent/` 出现 —— 只查
    「装上了」分不清这两种装法，那正是这条改坏要区分的东西。
    """
    subprocess.run(["uv", "pip", "uninstall", "job-agent"],
                   cwd=ROOT, capture_output=True, text=True, timeout=300)
    subprocess.run(["uv", "pip", "install", "."],
                   cwd=ROOT, capture_output=True, text=True, timeout=600)
    if _editable_pth() is not None:
        raise SystemExit("✗ 快照改坏落不上：editable 指针还在，装法没换")
    if not any((ROOT / ".venv" / "lib").rglob("site-packages/jobagent/__init__.py")):
        raise SystemExit("✗ 快照改坏落不上：site-packages 里没有 jobagent 副本")


MUTATIONS = [
    # ---- 环境级：包不在环境里 ----
    Mutation(
        "把包从 venv 里卸掉（复现「只加 [build-system] 但没 uv sync」）",
        None, "", "",
        should_red="foreign_cwd or resolves_into_this_repo or module_spec_found",
        control="test_build_system_is_declared",
        uninstall=True,
    ),
    Mutation(
        "卸掉包之后，入口点也该跟着消失（单独验这一条，别和导入混着看）",
        None, "", "",
        should_red="declared_console_script_exists or declared_script_actually_runs",
        control="test_lock_records_project_as_editable",
        uninstall=True,
    ),
    # ---- 文件级：pyproject ----
    Mutation(
        "删掉 [build-system]（复现根因本身）",
        PYPROJECT,
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"',
        '# [build-system] 被改坏拿掉了',
        should_red="test_build_system_is_declared",
        control="test_importable_from_a_foreign_cwd",
    ),
    Mutation(
        "留着 [build-system] 但抽掉 build-backend（半成品）",
        PYPROJECT,
        'build-backend = "hatchling.build"',
        '# build-backend 被改坏拿掉了',
        should_red="test_build_system_is_declared",
        control="test_importable_from_a_foreign_cwd",
    ),
    Mutation(
        "把 [project.scripts] 的入口点改名（声明了但装的是另一个名字）",
        PYPROJECT,
        'jobagent = "jobagent.cli:app"',
        'jobagent-typo = "jobagent.cli:app"',
        should_red="test_declared_console_script_exists",
        control="test_importable_from_a_foreign_cwd",
    ),
    Mutation(
        "把入口点指向不存在的属性（文件在，但一调就崩 —— 存在≠能跑）",
        PYPROJECT,
        'jobagent = "jobagent.cli:app"',
        'jobagent = "jobagent.cli:no_such_attr"',
        should_red="test_declared_script_actually_runs",
        control="test_build_system_is_declared",
        resync=True,   # 不同步的话改坏落在文件上、判据查的是环境，两边不碰面
    ),
    # ---- 文件级：uv.lock ----
    Mutation(
        "把 lock 里的 editable 改回 virtual（复现「pyproject 改了 lock 没跟上」）",
        LOCK,
        'name = "job-agent"\nversion = "0.2.0"\nsource = { editable = "." }',
        'name = "job-agent"\nversion = "0.2.0"\nsource = { virtual = "." }',
        should_red="test_lock_records_project_as_editable",
        control="test_importable_from_a_foreign_cwd",
    ),
    Mutation(
        "把 lock 里 job-agent 那条整个删掉（锚点自己坏掉该有自己的话说）",
        LOCK,
        '[[package]]\nname = "job-agent"',
        '[[package]]\nname = "job-agent-renamed"',
        should_red="test_lock_records_project_as_editable",
        control="test_build_system_is_declared",
    ),
    # ---- 文件级：文档 ----
    Mutation(
        "往文档里塞一个不存在的子命令（复现 `jobagent prepare` 那次）",
        MCP_SETUP,
        "想投递还是走 `jobagent apply <job_id>`",
        "想投递还是走 `jobagent prepare`",
        should_red="test_docs_bare_jobagent_commands_exist",
        control="test_the_doc_scanner_finds_something",
    ),
    Mutation(
        "把文档里所有 `jobagent 子命令` 写法抹掉（锚点算成空集就是假绿）",
        MCP_SETUP,
        "想投递还是走 `jobagent apply <job_id>`",
        "想投递还是走命令行",
        should_red="test_the_doc_scanner_finds_something",
        control="test_docs_bare_jobagent_commands_exist",
    ),
    # ---- 文件级：判据自己的锚点 ----
    Mutation(
        "把子进程的 cwd 换回仓库根（这个文件唯一承重的那道防线）",
        GUARD,
        "        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,",
        "        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,",
        should_red="__SPECIAL_CWD__",
        control="test_build_system_is_declared",
    ),
    # ---- 环境级：装成快照副本而不是 editable ----
    # 这一对是「同一个环境状态，改坏前后判据的反应不同」。单独放任何一条都不够：
    # 前者证明这条断言**抓到了别的判据抓不到的东西**，后者证明抓到它的**就是这条断言**。
    Mutation(
        "装成非 editable 快照副本（改代码不生效那种故障，导入仍然成功）",
        None, "", "",
        should_red="test_import_resolves_into_this_repo",
        control="test_importable_from_a_foreign_cwd",
        snapshot=True,
    ),
    Mutation(
        "同一个快照状态 + 把「导入到的是哪一份」弱化成只查存在（弱化一级）",
        GUARD,
        "assert origin == ROOT / \"jobagent\" / \"__init__.py\", (",
        "assert origin.exists(), (",
        should_red="__SPECIAL_WEAKENED__",
        control="test_importable_from_a_foreign_cwd",
        snapshot=True,
    ),
]


def main() -> int:
    dirty = subprocess.run(["git", "status", "--porcelain", str(PYPROJECT),
                            str(LOCK), str(MCP_SETUP), str(GUARD)],
                           cwd=ROOT, capture_output=True, text=True).stdout
    if dirty.strip():
        print("先把这几个文件的改动提交或 stash 掉，残留改坏态会伪装成被测对象的缺陷：")
        print(dirty)
        return 2

    failed, n = run_k("test_")
    if failed or n == 0:
        print(f"✗ 基线就不干净：failed={failed} 选中 {n} 条")
        return 1
    print(f"基线：{n} 条全绿\n")

    ok = True
    for i, m in enumerate(MUTATIONS, 1):
        backup = None
        try:
            if m.target is not None:
                backup = apply_mutation(m)
            if m.uninstall:
                uninstall_project()
            elif m.snapshot:
                snapshot_install()
            elif m.resync:
                reinstall_project()

            if m.should_red.startswith("__SPECIAL_"):
                red, sel = special_check(m)
            else:
                red, sel = run_k(m.should_red)
            cred, csel = run_k(m.control)

            marks = []
            if sel == 0:
                marks.append("该红那条 -k 匹配 0 条（假绿）")
            elif not red:
                marks.append("该红的没红")
            if csel == 0:
                marks.append("对照组 -k 匹配 0 条")
            elif cred:
                marks.append("对照组也红了（改坏溢出／选错了对照组）")

            if marks:
                ok = False
                print(f"[{i:2}] ✗ {'; '.join(marks)}  ← {m.desc}")
            else:
                print(f"[{i:2}] 该红 {sel} 条→红 · 对照 {csel} 条→绿 ✓  {m.desc}")
        finally:
            if backup is not None:
                m.target.write_text(backup, encoding="utf-8")
            if m.uninstall or m.resync or m.snapshot:
                # 快照那两条必须先卸掉，否则 `uv sync` 之后 site-packages 里
                # 会同时留着快照副本和 editable 指针 —— 那是个第三种状态，
                # 会让后面每一条都在错的前提下跑。
                subprocess.run(["uv", "pip", "uninstall", "job-agent"],
                               cwd=ROOT, capture_output=True, text=True, timeout=300)
                reinstall_project()

    left = subprocess.run(["git", "status", "--porcelain", str(PYPROJECT),
                           str(LOCK), str(MCP_SETUP), str(GUARD)],
                          cwd=ROOT, capture_output=True, text=True).stdout
    if left.strip():
        print(f"\n✗ 还原没干净：\n{left}")
        return 1

    # 条数算出来，不写死 —— 上一版这里硬编码着「12 条」，加了第 13 条之后
    # 它照样打「12 条全部按预期」，而那句话是这个脚本唯一的对外结论。
    print("\n" + (f"{len(MUTATIONS)} 条改坏全部按预期变红，对照组全部保持绿。" if ok
                  else "有改坏没按预期，见上面的 ✗"))
    return 0 if ok else 1


def special_check(m: Mutation) -> tuple[bool, int]:
    """两条弱化型改坏：判据还在、还是绿的，但**已经不查原来那件事了**。

    这类改坏用「该红」查不出来 —— 它就是不会红。判据是：
    弱化之后，再把环境改坏（卸掉包），原本该红的那条**变成不红**。
    """
    if m.should_red == "__SPECIAL_CWD__":
        # 承重的是 cwd：换回仓库根之后，包卸掉了它照样绿。
        uninstall_project()
        red, sel = run_k("test_importable_from_a_foreign_cwd")
    else:
        # 弱化的表现：环境已经是快照副本（上面 snapshot_install 已经落好了），
        # 强判据会红、弱判据不会。这里问的就是「它现在还红不红」。
        red, sel = run_k("test_import_resolves_into_this_repo")
    return (not red and sel > 0), sel


if __name__ == "__main__":
    sys.exit(main())
