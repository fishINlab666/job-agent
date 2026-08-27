"""macOS 用户级自动观察任务。

每个时段一个 LaunchAgent，命令用参数数组直达 Python，不经过 shell，也不会读取
浏览器登录态或画像。三个任务共享同一个本地观察数据库。
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from . import observation
from .observation import SCHEDULE_SLOTS


LABEL_PREFIX = "com.fishinlab.job-agent.observe"
CLOUD_LABEL = "com.fishinlab.job-agent.cloud-check"
FLEXIBLE_LABEL = "com.fishinlab.job-agent.observe-daily"
CLOUD_CHECK_INTERVAL_SECONDS = 15 * 60


class SchedulerRollbackError(RuntimeError):
    pass


def _reject_symlink_components(path: Path, *, label: str) -> None:
    """Reject a path if any checked component can be redirected later."""
    if not path.is_absolute():
        raise ValueError(f"{label} 必须使用绝对路径")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} 不能经过符号链接：{current}")


def _validate_flexible_runtime_inputs(
    *, project_root: Path, python_executable: Path, db_path: Path
) -> None:
    """Bind flexible scheduling to one SHA-named release and one exact database."""
    _reject_symlink_components(project_root, label="固定 release")
    release_name = project_root.name
    if (
        project_root.parent.name != "releases"
        or len(release_name) != 40
        or any(char not in "0123456789abcdef" for char in release_name)
    ):
        raise ValueError("项目目录必须是 releases/<40位提交SHA> 的固定 release")
    if not project_root.is_dir():
        raise ValueError(f"项目目录不存在：{project_root}")

    expected_python = project_root / ".venv" / "bin" / "python"
    if python_executable != expected_python:
        raise ValueError(f"Python 必须来自固定 release：{expected_python}")
    # venv 的 python 文件本身通常是解释器 symlink；它的所有父目录必须固定。
    _reject_symlink_components(python_executable.parent, label="release Python")
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise ValueError(f"Python 不可执行：{python_executable}")

    _reject_symlink_components(db_path, label="观察数据库")
    if not db_path.is_file():
        raise ValueError(f"观察数据库必须是已存在的普通文件：{db_path}")


@contextmanager
def _scheduler_lock(home: Path):
    """串行化新旧调度的检查、安装、验证与回滚。"""
    lock_dir = Path(home) / "Library" / "Application Support" / "job-agent"
    if lock_dir.is_symlink():
        raise ValueError(f"拒绝使用符号链接锁目录：{lock_dir}")
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not lock_dir.is_dir():
        raise ValueError(f"调度锁目录不是普通目录：{lock_dir}")
    lock_dir.chmod(0o700)
    lock_path = lock_dir / "scheduler.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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


def build_cloud_payload(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    config_path: Path,
    log_dir: Path,
) -> dict:
    """构建混合模式检查任务；私有令牌只由 CLI 运行时读取。"""
    return {
        "Label": CLOUD_LABEL,
        "ProgramArguments": [
            str(python_executable),
            "-m",
            "jobagent.cli",
            "cloud-check",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
        ],
        "EnvironmentVariables": {"PYTHONPATH": str(project_root)},
        "WorkingDirectory": str(project_root),
        "StartInterval": CLOUD_CHECK_INTERVAL_SECONDS,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "cloud-check.log"),
        "StandardErrorPath": str(log_dir / "cloud-check-error.log"),
    }


def build_flexible_payload(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    log_dir: Path,
) -> dict:
    """Build a wake-aware daily job without a shell or a second data path."""
    return {
        "Label": FLEXIBLE_LABEL,
        "ProgramArguments": [
            str(python_executable),
            "-m",
            "jobagent.cli",
            "observe-daily",
            "--db",
            str(db_path),
        ],
        "EnvironmentVariables": {"PYTHONPATH": str(project_root)},
        "WorkingDirectory": str(project_root),
        "StartCalendarInterval": [{"Minute": 0}, {"Minute": 30}],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "observe-daily.log"),
        "StandardErrorPath": str(log_dir / "observe-daily-error.log"),
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


def _cutover_marker(home: Path) -> Path:
    return (
        Path(home)
        / "Library"
        / "Application Support"
        / "job-agent"
        / "flexible-cutover.json"
    )


def _system_runtime_probe(domain: str, label: str, *, timeout: float = 130) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"{domain}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        if "last exit code = 0" in result.stdout:
            return True
        time.sleep(0.25)
    return False


def _snapshot(path: Path, loaded: bool) -> dict:
    return {
        "path": str(path),
        "loaded": loaded,
        "mode": (path.stat().st_mode & 0o777) if path.exists() else None,
        "content": (
            base64.b64encode(path.read_bytes()).decode("ascii")
            if path.exists()
            else None
        ),
    }


def _write_cutover_marker(path: Path, payload: dict) -> None:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    _atomic_write(path, content, 0o600)


def _load_cutover_marker(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("灵活调度恢复 marker 不存在或不是普通文件")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("灵活调度恢复 marker 权限过宽")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("灵活调度恢复 marker 损坏") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("states"), dict):
        raise RuntimeError("灵活调度恢复 marker 格式不受支持")
    return payload


def _validate_cutover_marker(
    marker: dict,
    *,
    home: Path,
    db_path: Path,
    domain: str,
) -> None:
    if marker.get("db_path") != str(db_path) or marker.get("domain") != domain:
        raise RuntimeError("恢复 marker 与数据库或用户身份不匹配")
    expected_labels = {_label(slot) for slot in SCHEDULE_SLOTS} | {FLEXIBLE_LABEL}
    states = marker.get("states")
    if not isinstance(states, dict) or set(states) != expected_labels:
        raise RuntimeError("恢复 marker 的任务集合不匹配")
    launch_dir = Path(home) / "Library/LaunchAgents"
    for label, state in states.items():
        expected_path = launch_dir / f"{label}.plist"
        if not isinstance(state, dict) or state.get("path") != str(expected_path):
            raise RuntimeError("恢复 marker 的 plist 路径不匹配")
        if not isinstance(state.get("loaded"), bool):
            raise RuntimeError("恢复 marker 的 loaded 状态无效")
        content = state.get("content")
        mode = state.get("mode")
        if content is None:
            if mode is not None:
                raise RuntimeError("恢复 marker 的空文件状态无效")
            continue
        if not isinstance(content, str) or not isinstance(mode, int):
            raise RuntimeError("恢复 marker 的文件快照无效")
        try:
            base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("恢复 marker 的文件内容损坏") from exc


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


def _install_observation_unlocked(
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


def install(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> list[str]:
    with _scheduler_lock(home):
        marker = _cutover_marker(home)
        domain = f"gui/{os.getuid() if uid is None else uid}"
        if marker.exists() or _is_loaded(launchctl, domain, FLEXIBLE_LABEL):
            raise RuntimeError("灵活调度已启用或正在切换，不能安装旧三时段任务")
        if _is_loaded(launchctl, domain, CLOUD_LABEL):
            raise RuntimeError("云端检查已启用，不能与本机观察调度并存")
        return _install_observation_unlocked(
            project_root=project_root,
            python_executable=python_executable,
            db_path=db_path,
            home=home,
            launchctl=launchctl,
            uid=uid,
        )


def uninstall(
    *,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> None:
    """卸载旧三时段或灵活每日任务；观察数据库和历史记录保留。"""
    home = Path(home)
    with _scheduler_lock(home):
        if _cutover_marker(home).exists():
            raise RuntimeError("灵活调度切换未收口，请先运行恢复命令")
        user_id = os.getuid() if uid is None else uid
        domain = f"gui/{user_id}"
        launch_dir = home / "Library" / "LaunchAgents"
        labels = [_label(slot) for slot in SCHEDULE_SLOTS] + [FLEXIBLE_LABEL]
        for label in labels:
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


def _validate_cloud_install_inputs(
    *,
    project_root: Path,
    python_executable: Path,
    config_path: Path,
) -> None:
    if not project_root.is_dir():
        raise ValueError(f"项目目录不存在：{project_root}")
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise ValueError(f"Python 不可执行：{python_executable}")
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError(f"云配置必须是普通文件且不能是符号链接：{config_path}")
    mode = config_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(f"云配置权限过宽（应为 0600）：{config_path}")


def _install_cloud_check_unlocked(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    config_path: Path,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> str:
    """安装单个混合检查任务；失败时恢复原任务和 plist。"""
    project_root = Path(project_root)
    python_executable = Path(python_executable)
    db_path = Path(db_path)
    config_path = Path(config_path)
    home = Path(home)
    _validate_cloud_install_inputs(
        project_root=project_root,
        python_executable=python_executable,
        config_path=config_path,
    )

    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    launch_dir = home / "Library" / "LaunchAgents"
    log_dir = home / "Library" / "Logs" / "job-agent"
    launch_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = launch_dir / f"{CLOUD_LABEL}.plist"
    if path.is_symlink():
        raise ValueError(f"拒绝覆盖符号链接：{path}")

    loaded = _is_loaded(launchctl, domain, CLOUD_LABEL)
    if loaded and not path.is_file():
        raise RuntimeError(f"{CLOUD_LABEL} 已加载但缺少可恢复的 plist，拒绝覆盖")
    old_content = path.read_bytes() if path.exists() else None
    old_mode = path.stat().st_mode & 0o777 if path.exists() else None
    payload = build_cloud_payload(
        project_root=project_root,
        python_executable=python_executable,
        db_path=db_path,
        config_path=config_path,
        log_dir=log_dir,
    )
    content = plistlib.dumps(payload, fmt=plistlib.FMT_XML)

    try:
        if loaded:
            _invoke(launchctl, ["bootout", f"{domain}/{CLOUD_LABEL}"], check=True)
            if _is_loaded(launchctl, domain, CLOUD_LABEL):
                raise RuntimeError(f"{CLOUD_LABEL} 卸载旧任务后仍在运行")
        _atomic_write(path, content, 0o600)
        _invoke(launchctl, ["bootstrap", domain, str(path)], check=True)
        if not _is_loaded(launchctl, domain, CLOUD_LABEL):
            raise RuntimeError(f"{CLOUD_LABEL} bootstrap 后未处于加载状态")
    except Exception as original:
        try:
            if _is_loaded(launchctl, domain, CLOUD_LABEL):
                _invoke(
                    launchctl,
                    ["bootout", f"{domain}/{CLOUD_LABEL}"],
                    check=True,
                )
            if _is_loaded(launchctl, domain, CLOUD_LABEL):
                raise RuntimeError("rollback bootout 后仍在加载")
        except Exception as exc:
            raise SchedulerRollbackError(
                f"卸载失败的新 {CLOUD_LABEL}: {exc}"
            ) from original

        try:
            if old_content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, old_content, old_mode or 0o600)
            if loaded:
                _invoke(launchctl, ["bootstrap", domain, str(path)], check=True)
                if not _is_loaded(launchctl, domain, CLOUD_LABEL):
                    raise RuntimeError("恢复后仍未加载")
        except Exception as exc:
            raise SchedulerRollbackError(
                f"恢复 {CLOUD_LABEL} 失败: {exc}"
            ) from original
        raise
    return CLOUD_LABEL


def install_cloud_check(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    config_path: Path,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> str:
    with _scheduler_lock(home):
        marker = _cutover_marker(home)
        domain = f"gui/{os.getuid() if uid is None else uid}"
        legacy_loaded = any(
            _is_loaded(launchctl, domain, _label(slot))
            for slot in SCHEDULE_SLOTS
        )
        if marker.exists() or _is_loaded(launchctl, domain, FLEXIBLE_LABEL):
            raise RuntimeError("灵活调度已启用或正在切换，不能安装云端检查")
        if legacy_loaded:
            raise RuntimeError("云端检查不能与本机观察调度并存")
        return _install_cloud_check_unlocked(
            project_root=project_root,
            python_executable=python_executable,
            db_path=db_path,
            config_path=config_path,
            home=home,
            launchctl=launchctl,
            uid=uid,
        )


def uninstall_cloud_check(
    *,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> None:
    """卸载混合检查任务；配置、数据库和观察历史均保留。"""
    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    path = Path(home) / "Library" / "LaunchAgents" / f"{CLOUD_LABEL}.plist"
    if _is_loaded(launchctl, domain, CLOUD_LABEL):
        _invoke(launchctl, ["bootout", f"{domain}/{CLOUD_LABEL}"], check=True)
        if _is_loaded(launchctl, domain, CLOUD_LABEL):
            raise RuntimeError(f"{CLOUD_LABEL} 卸载后仍在运行，保留 plist")
    path.unlink(missing_ok=True)


def _restore_cutover_unlocked(
    *,
    marker_path: Path,
    marker: dict,
    launchctl: Callable[..., Any],
) -> None:
    domain = marker["domain"]
    states = marker["states"]
    rollback_errors: list[str] = []
    safe_to_restore = {label: True for label in states}
    for label in states:
        try:
            if _is_loaded(launchctl, domain, label):
                _invoke(launchctl, ["bootout", f"{domain}/{label}"], check=True)
                if _is_loaded(launchctl, domain, label):
                    raise RuntimeError("bootout 后仍在加载")
        except Exception as exc:
            safe_to_restore[label] = False
            rollback_errors.append(f"停止 {label}: {exc}")
    for label, state in states.items():
        if not safe_to_restore[label]:
            continue
        path = Path(state["path"])
        try:
            if state["content"] is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(
                    path,
                    base64.b64decode(state["content"], validate=True),
                    int(state["mode"] or 0o600),
                )
        except Exception as exc:
            rollback_errors.append(f"恢复 {label} 文件: {exc}")
    for label, state in states.items():
        if not safe_to_restore[label] or not state["loaded"]:
            continue
        try:
            _invoke(
                launchctl,
                ["bootstrap", domain, state["path"]],
                check=True,
            )
            if not _is_loaded(launchctl, domain, label):
                raise RuntimeError("恢复后仍未加载")
        except Exception as exc:
            rollback_errors.append(f"恢复 {label} 加载状态: {exc}")
    if rollback_errors:
        raise SchedulerRollbackError("；".join(rollback_errors))
    marker_path.unlink()


def recover_flexible(
    *,
    db_path: Path,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
) -> None:
    """Consume an exact cutover marker after a crash or failed first run."""
    db_path = Path(db_path).absolute()
    home = Path(home).absolute()
    marker_path = _cutover_marker(home)
    marker = _load_cutover_marker(marker_path)
    domain = f"gui/{os.getuid() if uid is None else uid}"
    _validate_cutover_marker(
        marker, home=home, db_path=db_path, domain=domain
    )

    # A hung RunAtLoad may own the observation lock. Stop only the exact job first.
    with _scheduler_lock(home):
        if _is_loaded(launchctl, domain, FLEXIBLE_LABEL):
            _invoke(
                launchctl,
                ["bootout", f"{domain}/{FLEXIBLE_LABEL}"],
                check=True,
            )
            if _is_loaded(launchctl, domain, FLEXIBLE_LABEL):
                raise SchedulerRollbackError("灵活任务停止后仍在运行")

    with observation.exclusive_run(db_path, wait_seconds=120):
        with _scheduler_lock(home):
            marker = _load_cutover_marker(marker_path)
            _validate_cutover_marker(
                marker, home=home, db_path=db_path, domain=domain
            )
            _restore_cutover_unlocked(
                marker_path=marker_path,
                marker=marker,
                launchctl=launchctl,
            )


def install_flexible(
    *,
    project_root: Path,
    python_executable: Path,
    db_path: Path,
    home: Path,
    launchctl: Callable[..., Any] = _system_launchctl,
    uid: int | None = None,
    runtime_probe: Callable[[str, str], bool] = _system_runtime_probe,
) -> str:
    """Replace the exact legacy class and prove the first flexible run."""
    project_root = Path(project_root).absolute()
    python_executable = Path(python_executable).absolute()
    db_path = Path(db_path).absolute()
    home = Path(home).absolute()
    _validate_flexible_runtime_inputs(
        project_root=project_root,
        python_executable=python_executable,
        db_path=db_path,
    )

    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    launch_dir = home / "Library/LaunchAgents"
    log_dir = home / "Library/Logs/job-agent"
    launch_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    flexible_path = launch_dir / f"{FLEXIBLE_LABEL}.plist"
    if flexible_path.is_symlink():
        raise ValueError(f"拒绝覆盖符号链接：{flexible_path}")
    content = plistlib.dumps(
        build_flexible_payload(
            project_root=project_root,
            python_executable=python_executable,
            db_path=db_path,
            log_dir=log_dir,
        ),
        fmt=plistlib.FMT_XML,
    )
    marker_path = _cutover_marker(home)

    with observation.exclusive_run(db_path, wait_seconds=120):
        with _scheduler_lock(home):
            if marker_path.exists():
                raise RuntimeError("上一次灵活调度切换尚未恢复")
            legacy = {_label(slot) for slot in SCHEDULE_SLOTS}
            legacy_loaded = {
                label for label in legacy if _is_loaded(launchctl, domain, label)
            }
            cloud_loaded = _is_loaded(launchctl, domain, CLOUD_LABEL)
            flexible_loaded = _is_loaded(launchctl, domain, FLEXIBLE_LABEL)
            if flexible_loaded:
                if legacy_loaded or cloud_loaded or not flexible_path.is_file():
                    raise RuntimeError("灵活调度与其他任务混合加载，拒绝代签成功")
                if flexible_path.read_bytes() != content:
                    raise RuntimeError("已加载灵活调度与当前候选不一致")
                return FLEXIBLE_LABEL
            if cloud_loaded or legacy_loaded != legacy:
                raise RuntimeError("只允许从完整且唯一的旧三时段调度迁移")
            states: dict[str, dict] = {}
            for label in sorted(legacy | {FLEXIBLE_LABEL}):
                path = launch_dir / f"{label}.plist"
                loaded = label in legacy_loaded
                if path.is_symlink():
                    raise ValueError(f"拒绝快照或覆盖符号链接：{path}")
                if loaded and not path.is_file():
                    raise RuntimeError(f"{label} 已加载但缺少可恢复 plist")
                states[label] = _snapshot(path, loaded)
            marker = {
                "version": 1,
                "domain": domain,
                "db_path": str(db_path),
                "project_root": str(project_root),
                "python_executable": str(python_executable),
                "states": states,
            }
            _write_cutover_marker(marker_path, marker)
            try:
                for label in sorted(legacy):
                    _invoke(
                        launchctl,
                        ["bootout", f"{domain}/{label}"],
                        check=True,
                    )
                    if _is_loaded(launchctl, domain, label):
                        raise RuntimeError(f"{label} 卸载后仍在运行")
                _atomic_write(flexible_path, content, 0o600)
                _invoke(
                    launchctl,
                    ["bootstrap", domain, str(flexible_path)],
                    check=True,
                )
                if not _is_loaded(launchctl, domain, FLEXIBLE_LABEL):
                    raise RuntimeError("灵活调度 bootstrap 后未加载")
            except Exception:
                _restore_cutover_unlocked(
                    marker_path=marker_path,
                    marker=marker,
                    launchctl=launchctl,
                )
                raise

    try:
        runtime_ok = runtime_probe(domain, FLEXIBLE_LABEL)
    except Exception:
        recover_flexible(
            db_path=db_path,
            home=home,
            launchctl=launchctl,
            uid=user_id,
        )
        raise
    if not runtime_ok:
        recover_flexible(
            db_path=db_path,
            home=home,
            launchctl=launchctl,
            uid=user_id,
        )
        raise RuntimeError("灵活调度首次真实运行未通过，已恢复旧任务")

    try:
        with observation.exclusive_run(db_path, wait_seconds=120):
            with _scheduler_lock(home):
                marker = _load_cutover_marker(marker_path)
                _validate_cutover_marker(
                    marker, home=home, db_path=db_path, domain=domain
                )
                if not _is_loaded(launchctl, domain, FLEXIBLE_LABEL):
                    raise RuntimeError("验收后灵活调度已不在加载状态")
                if any(
                    _is_loaded(launchctl, domain, label)
                    for label in states
                    if label != FLEXIBLE_LABEL
                ):
                    raise RuntimeError("验收后旧任务意外重新加载")
                marker_path.unlink()
    except Exception:
        recover_flexible(
            db_path=db_path,
            home=home,
            launchctl=launchctl,
            uid=user_id,
        )
        raise
    return FLEXIBLE_LABEL
