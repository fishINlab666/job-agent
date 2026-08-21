"""macOS 用户级自动观察任务。

每个时段一个 LaunchAgent，命令用参数数组直达 Python，不经过 shell，也不会读取
浏览器登录态或画像。三个任务共享同一个本地观察数据库。
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .observation import SCHEDULE_SLOTS


LABEL_PREFIX = "com.fishinlab.job-agent.observe"


class SchedulerRollbackError(RuntimeError):
    pass


def _label(slot: str) -> str:
    return f"{LABEL_PREFIX}.{slot.replace(':', '')}"


def build_payload(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    log_dir: Path,
    slot: str,
) -> dict:
    if slot not in SCHEDULE_SLOTS:
        raise ValueError(f"不认识的观察时段：{slot}")
    hour, minute = (int(part) for part in slot.split(":", 1))
    return {
        "Label": _label(slot),
        "ProgramArguments": [
            str(python_executable),
            "-m",
            "jobagent.cli",
            "observe",
            "--db",
            str(db_path),
            "--trigger",
            "scheduled",
            "--slot",
            slot,
        ],
        "EnvironmentVariables": {"PYTHONPATH": str(project_root)},
        "WorkingDirectory": str(project_root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "observe.log"),
        "StandardErrorPath": str(log_dir / "observe-error.log"),
    }


def _system_launchctl(args: list[str], *, check: bool) -> int:
    result = subprocess.run(
        ["/bin/launchctl", *args],
        check=check,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return int(result.returncode)


def _return_code(result: Any) -> int:
    if isinstance(result, int):
        return result
    if hasattr(result, "returncode"):
        return int(result.returncode)
    raise TypeError("launchctl adapter 必须返回退出码")


def _invoke(launchctl: Callable[..., Any], args: list[str], *, check: bool) -> int:
    code = _return_code(launchctl(args, check=check))
    if check and code != 0:
        raise RuntimeError(f"launchctl {' '.join(args)} 失败，退出码 {code}")
    return code


def _is_loaded(launchctl: Callable[..., Any], domain: str, label: str) -> bool:
    return _invoke(
        launchctl,
        ["print", f"{domain}/{label}"],
        check=False,
    ) == 0


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> list[str]:
    """安装三份任务；失败时恢复安装前的 plist 内容和加载状态。"""
    project_root = Path(project_root)
    python_executable = Path(python_executable)
    db_path = Path(db_path)
    home = Path(home)
    if not project_root.is_dir():
        raise ValueError(f"项目目录不存在：{project_root}")
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise ValueError(f"Python 不可执行：{python_executable}")

    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    launch_dir = home / "Library" / "LaunchAgents"
    log_dir = home / "Library" / "Logs" / "job-agent"
    launch_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    targets: list[tuple[str, Path, bytes]] = []
    for slot in SCHEDULE_SLOTS:
        label = _label(slot)
        path = launch_dir / f"{label}.plist"
        if path.is_symlink():
            raise ValueError(f"拒绝覆盖符号链接：{path}")
        payload = build_payload(
            project_root=project_root,
            python_executable=python_executable,
            db_path=db_path,
            log_dir=log_dir,
            slot=slot,
        )
        targets.append((label, path, plistlib.dumps(payload, fmt=plistlib.FMT_XML)))

    previous: dict[str, tuple[bytes | None, int | None, bool]] = {}
    for label, path, _content in targets:
        loaded = _is_loaded(launchctl, domain, label)
        if loaded and not path.is_file():
            raise RuntimeError(f"{label} 已加载但缺少可恢复的 plist，拒绝覆盖")
        previous[label] = (
            path.read_bytes() if path.exists() else None,
            path.stat().st_mode & 0o777 if path.exists() else None,
            loaded,
        )

    try:
        for label, _path, _content in targets:
            if previous[label][2]:
                _invoke(
                    launchctl,
                    ["bootout", f"{domain}/{label}"],
                    check=True,
                )
        for _label_value, path, content in targets:
            _atomic_write(path, content, 0o600)
        for label, path, _content in targets:
            _invoke(launchctl, ["bootstrap", domain, str(path)], check=True)
        for label, _path, _content in targets:
            if not _is_loaded(launchctl, domain, label):
                raise RuntimeError(f"{label} bootstrap 后未处于加载状态")
    except Exception as original:
        rollback_errors: list[str] = []
        safe_to_restore: dict[str, bool] = {}
        for label, _path, _content in targets:
            try:
                if _is_loaded(launchctl, domain, label):
                    _invoke(
                        launchctl,
                        ["bootout", f"{domain}/{label}"],
                        check=True,
                    )
                if _is_loaded(launchctl, domain, label):
                    raise RuntimeError("rollback bootout 后仍在加载")
                safe_to_restore[label] = True
            except Exception as exc:
                safe_to_restore[label] = False
                rollback_errors.append(f"卸载新 {label}: {exc}")
        for label, path, _content in targets:
            if not safe_to_restore[label]:
                continue
            old_content, old_mode, _was_loaded = previous[label]
            try:
                if old_content is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, old_content, old_mode or 0o600)
            except Exception as exc:
                rollback_errors.append(f"恢复 {label} 文件: {exc}")
        for label, path, _content in targets:
            if not safe_to_restore[label] or not previous[label][2]:
                continue
            try:
                _invoke(launchctl, ["bootstrap", domain, str(path)], check=True)
                if not _is_loaded(launchctl, domain, label):
                    raise RuntimeError("恢复后仍未加载")
            except Exception as exc:
                rollback_errors.append(f"恢复 {label} 加载状态: {exc}")
        if rollback_errors:
            raise SchedulerRollbackError("；".join(rollback_errors)) from original
        raise
    return list(SCHEDULE_SLOTS)


def uninstall(
    *,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> None:
    """卸载三个自动任务；观察数据库和历史记录保留。"""
    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    launch_dir = Path(home) / "Library" / "LaunchAgents"
    for slot in SCHEDULE_SLOTS:
        label = _label(slot)
        path = launch_dir / f"{label}.plist"
        if _is_loaded(launchctl, domain, label):
            _invoke(
                launchctl,
                ["bootout", f"{domain}/{label}"],
                check=True,
            )
            if _is_loaded(launchctl, domain, label):
                raise RuntimeError(f"{label} 卸载后仍在运行，保留 plist")
        path.unlink(missing_ok=True)
