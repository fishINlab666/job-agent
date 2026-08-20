"""Packaging must work when the process starts outside the repository."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run_outside(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_package_imports_from_foreign_cwd(tmp_path: Path) -> None:
    result = _run_outside(
        "import jobagent, pathlib; print(pathlib.Path(jobagent.__file__).resolve())",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == ROOT / "jobagent" / "__init__.py"


def test_mcp_module_is_discoverable_from_foreign_cwd(tmp_path: Path) -> None:
    result = _run_outside(
        "import importlib.util; assert importlib.util.find_spec('jobagent.mcp_server')",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr


def test_console_script_runs_from_foreign_cwd(tmp_path: Path) -> None:
    script = Path(sys.executable).parent / "jobagent"

    assert script.is_file()
    result = subprocess.run(
        [str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_build_and_lock_install_the_project() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert project["build-system"]["build-backend"] == "hatchling.build"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "jobagent"
    ]
    assert 'name = "job-agent"\nversion = "0.2.0"\nsource = { editable = "." }' in lock
