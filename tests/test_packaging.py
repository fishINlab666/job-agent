"""守住「包真的装进了环境」，而不是「cwd 恰好在仓库根」。

为什么要这一整个文件：这个项目的 `pyproject.toml` 长期缺 `[build-system]`，
uv 因此把它当虚拟项目 —— 只装依赖，不装它自己。表现是**测试全绿而 server 起不来**：
pytest 的 `pythonpath = ["."]` 是相对 cwd 的，从仓库根跑当然成立；而 MCP 客户端
从别的目录启 `python -m jobagent.mcp_server` 就 `ModuleNotFoundError`。见方案 021。

这里每条判据都必须**带 cwd 变化**。在仓库根 `import jobagent` 成功是现状就成立的，
取值域是常数，写下来等于没写。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENV_BIN = ROOT / ".venv" / "bin"


def _run_outside(code: str, cwd: Path) -> subprocess.CompletedProcess:
    """在 `cwd` 下拿本 venv 的解释器跑一段代码，**掐掉 cwd 这条旁路**。

    承重的是 `cwd`：传仓库根进来的话，仓库会被塞进子进程的 `sys.path`，
    于是「装没装进环境」这个问题问不出来 —— 包卸掉了测试照样绿。
    `scripts/mutate_021.py` 里有一条专门验这个假绿。

    清 `PYTHONPATH` 这行**今天边际贡献是 0**：`uv run pytest` 下它没被设置
    （pytest 的 `pythonpath = ["."]` 改的是进程内 `sys.path`，不是环境变量）。
    留着是因为 `PYTHONPATH=. pytest` 是个真会有人敲的调用方式，而那种时候
    它就是承重的。写清楚是为了下次读的人别把它当成已经在起作用的防线。
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


class TestImportableWithoutCwd:
    def test_importable_from_a_foreign_cwd(self, tmp_path):
        """核心判据：cwd 不在仓库里，也要能 import。"""
        r = _run_outside("import jobagent; print('ok')", tmp_path)
        assert r.returncode == 0, (
            f"从 {tmp_path} 导入 jobagent 失败 —— 包没装进环境，"
            f"现在能用只是因为 cwd 恰好在仓库根。stderr:\n{r.stderr}"
        )
        assert "ok" in r.stdout

    def test_import_resolves_into_this_repo(self, tmp_path):
        """导入到的必须是**本仓库这份**，不是 site-packages 里的快照副本。

        非 editable 安装时这条会红。它和上一条查的不是同一件事：上一条问
        「找得到吗」，这条问「找到的是哪一份」—— 快照副本会让改了代码不生效，
        表现是测试绿、行为旧。
        """
        r = _run_outside(
            "import jobagent, pathlib; print(pathlib.Path(jobagent.__file__).resolve())",
            tmp_path,
        )
        assert r.returncode == 0, r.stderr
        origin = Path(r.stdout.strip())
        assert origin == ROOT / "jobagent" / "__init__.py", (
            f"导入到了 {origin}，不是本仓库那份 —— 大概装成了快照副本而非 editable"
        )

    def test_mcp_server_module_spec_found_from_foreign_cwd(self, tmp_path):
        """MCP 客户端用的是 `-m jobagent.mcp_server`，单独钉一条。

        走 `find_spec` 而不是真 import：真 import 会拉起 mcp 依赖树，
        而这条要问的只是「`-m` 找不找得到它」。
        """
        r = _run_outside(
            "import importlib.util as u;"
            "s=u.find_spec('jobagent.mcp_server');"
            "print(s.origin if s else 'NOT_FOUND')",
            tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() != "NOT_FOUND", (
            "从仓库外找不到 jobagent.mcp_server —— 客户端启 server 会 ModuleNotFoundError，"
            "而表现是「配好了但对话里看不见工具」"
        )


class TestDeclaredEntryPoints:
    @staticmethod
    def _declared_scripts() -> dict[str, str]:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return data.get("project", {}).get("scripts", {})

    def test_declared_console_script_exists(self):
        """声明了就得装出来。反向：装出来的也得在声明里。

        两个方向各一条 —— 只查一个方向的话，「手工往 venv/bin 塞了个脚本」
        和「声明了但没装」里只有一种查得出来。
        """
        declared = self._declared_scripts()
        assert declared, "pyproject.toml 的 [project.scripts] 是空的，这条判据无对象"

        missing = [n for n in declared if not (VENV_BIN / n).exists()]
        assert not missing, (
            f"[project.scripts] 声明了 {missing} 但 {VENV_BIN} 里没有 —— "
            f"照文档敲会得到 command not found"
        )

        # 反向：venv/bin 里叫得上名字的本项目脚本，必须是声明过的。
        # 只看和包同名的那些，不然会把 pytest / playwright 之类的依赖脚本算进来。
        stem = ROOT.name.replace("-", "")
        strays = [
            p.name for p in VENV_BIN.iterdir()
            if p.name.replace("-", "").startswith(stem) and p.name not in declared
        ]
        assert not strays, f"{strays} 在 venv/bin 里但没在 [project.scripts] 声明过"

    def test_declared_script_actually_runs(self):
        """存在不等于能跑 —— 入口点指向不存在的属性时，文件在而调用即崩。"""
        for name in self._declared_scripts():
            r = subprocess.run(
                [str(VENV_BIN / name), "--help"],
                capture_output=True, text=True, timeout=60, cwd=ROOT,
            )
            assert r.returncode == 0, f"`{name} --help` 退出码 {r.returncode}:\n{r.stderr}"


class TestDocsMatchRealCommands:
    """文档里出现的 `jobagent <子命令>` 必须是真子命令。

    这条不是凑数的：021 落地时它当场查出 `docs/MCP_SETUP.md` 写着
    `jobagent prepare`，而 `prepare` **从来不存在** —— 两阶段闸门在 `apply`
    底下。入口点复活之后这句话从「命令不存在」变成「子命令不存在」，
    照着敲仍然是错的，只是错法换了一层。
    """

    DOC_FILES = ("README.md", "docs/MCP_SETUP.md", "docs/SPEC.md", "docs/WIKI.md")

    @staticmethod
    def _real_subcommands() -> set[str]:
        from jobagent.cli import app

        names = set()
        for c in app.registered_commands:
            # typer 的 name 可能是 None（用函数名），也可能被显式改过
            names.add(c.name or c.callback.__name__.replace("_", "-"))
        assert names, "从 cli.app 里一个子命令都读不出来，锚点坏了"
        return names

    @staticmethod
    def _doc_mentions() -> list[tuple[str, int, str]]:
        import re

        # 只认反引号里、紧跟着一个小写子命令的形式：`jobagent apply`。
        # 不认 `jobagent.cli` 这种模块路径，也不认 `uv run jobagent --help`。
        pat = re.compile(r"`jobagent\s+([a-z][a-z-]*)")
        out = []
        for rel in TestDocsMatchRealCommands.DOC_FILES:
            p = ROOT / rel
            if not p.exists():
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                for m in pat.finditer(line):
                    out.append((rel, i, m.group(1)))
        return out

    def test_docs_bare_jobagent_commands_exist(self):
        real = self._real_subcommands()
        bad = [(f, i, c) for f, i, c in self._doc_mentions() if c not in real]
        assert not bad, (
            "文档里这些 `jobagent <子命令>` 不是真子命令，照着敲会挂：\n  "
            + "\n  ".join(f"{f}:{i} → {c}" for f, i, c in bad)
            + f"\n真实子命令：{sorted(real)}"
        )

    def test_the_doc_scanner_finds_something(self):
        """锚点自己的体检：正则一条都不匹配的话，上一条恒绿。

        「一条都没匹配上」和「全都合规」在集合运算里是同一个结果。
        """
        assert self._doc_mentions(), (
            "扫不到任何 `jobagent <子命令>` —— 正则或文件清单坏了，"
            "上面那条判据现在是恒绿的"
        )


class TestLockIsShareable:
    """行为级判据只证明我这台机器的 venv 现在是对的，证明不了别人 clone 能用。

    这条是文件级的（方案 021 §6 里最弱的一级），单独放它不够，
    但少了它就有一个查不出来的半成品：`pyproject.toml` 改了而 `uv.lock` 没跟上，
    别人 `uv sync` 装到的还是虚拟项目。
    """

    def test_lock_records_project_as_editable(self):
        import re

        text = (ROOT / "uv.lock").read_text(encoding="utf-8")
        m = re.search(
            r'\[\[package\]\]\nname = "job-agent"\nversion = "[^"]*"\nsource = \{ ([^}]*) \}',
            text,
        )
        assert m, "uv.lock 里找不到 job-agent 这个 package 条目 —— 锚点坏了"
        source = m.group(1).strip()
        assert "editable" in source, (
            f'uv.lock 把本项目记成 `{source}`，不是 editable —— '
            f"pyproject.toml 改了但 lock 没跟上，别人 clone 下来装的还是虚拟项目。"
            f"跑 `uv sync` 让它同步"
        )

    def test_build_system_is_declared(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert "build-system" in data, (
            "没有 [build-system]，uv 会把这个项目当虚拟项目：只装依赖不装它自己"
        )
        assert data["build-system"].get("build-backend"), "[build-system] 缺 build-backend"
