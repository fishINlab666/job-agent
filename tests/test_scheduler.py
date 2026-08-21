from __future__ import annotations

import plistlib
from pathlib import Path
import stat

import pytest

from jobagent import scheduler


def test_payload_uses_direct_python_arguments_and_fixed_slot(tmp_path) -> None:
    payload = scheduler.build_payload(
        project_root=tmp_path / "repo",
        python_executable=tmp_path / "venv/bin/python",
        db_path=tmp_path / "data/jobagent.db",
        log_dir=tmp_path / "logs",
        slot="14:30",
    )

    assert payload["Label"] == "com.fishinlab.job-agent.observe.1430"
    assert payload["StartCalendarInterval"] == {"Hour": 14, "Minute": 30}
    assert payload["ProgramArguments"] == [
        str(tmp_path / "venv/bin/python"),
        "-m",
        "jobagent.cli",
        "observe",
        "--db",
        str(tmp_path / "data/jobagent.db"),
        "--trigger",
        "scheduled",
        "--slot",
        "14:30",
    ]
    assert payload["EnvironmentVariables"] == {
        "PYTHONPATH": str(tmp_path / "repo")
    }
    assert "Program" not in payload
    assert payload["RunAtLoad"] is False


def test_install_writes_three_private_plists_and_bootstraps(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    calls: list[tuple[list[str], bool]] = []

    loaded: set[str] = set()

    def fake_launchctl(args: list[str], *, check: bool) -> int:
        calls.append((args, check))
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootstrap":
            loaded.add(plistlib.loads((tmp_path / args[-1]).read_bytes())["Label"] if not args[-1].startswith("/") else plistlib.loads(Path(args[-1]).read_bytes())["Label"])
        elif args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return 0

    slots = scheduler.install(
        project_root=project_root,
        python_executable=python,
        db_path=tmp_path / "data/jobagent.db",
        home=tmp_path / "home",
        launchctl=fake_launchctl,
        uid=501,
    )

    assert slots == ["09:30", "14:30", "20:30"]
    launch_dir = tmp_path / "home/Library/LaunchAgents"
    files = sorted(launch_dir.glob("com.fishinlab.job-agent.observe.*.plist"))
    assert len(files) == 3
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert {
        plistlib.loads(path.read_bytes())["StartCalendarInterval"]["Hour"]
        for path in files
    } == {9, 14, 20}
    bootstraps = [args for args, check in calls if args[0] == "bootstrap" and check]
    assert len(bootstraps) == 3
    assert all(args[1] == "gui/501" for args in bootstraps)
    assert loaded == {
        "com.fishinlab.job-agent.observe.0930",
        "com.fishinlab.job-agent.observe.1430",
        "com.fishinlab.job-agent.observe.2030",
    }


def test_install_rolls_back_all_tasks_when_one_bootstrap_fails(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    calls: list[tuple[list[str], bool]] = []

    loaded: set[str] = set()

    def failing_launchctl(args: list[str], *, check: bool) -> int:
        calls.append((args, check))
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootstrap" and args[-1].endswith("1430.plist"):
            raise RuntimeError("bootstrap failed")
        if args[0] == "bootstrap":
            loaded.add(plistlib.loads(Path(args[-1]).read_bytes())["Label"])
        elif args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return 0

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        scheduler.install(
            project_root=project_root,
            python_executable=python,
            db_path=tmp_path / "data/jobagent.db",
            home=tmp_path / "home",
            launchctl=failing_launchctl,
            uid=501,
        )

    launch_dir = tmp_path / "home/Library/LaunchAgents"
    assert not list(launch_dir.glob("com.fishinlab.job-agent.observe.*.plist"))
    assert any(
        args == ["bootout", "gui/501/com.fishinlab.job-agent.observe.0930"]
        for args, _ in calls
    )
    assert loaded == set()


def test_failed_reinstall_restores_previous_files_and_loaded_state(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    launch_dir = tmp_path / "home/Library/LaunchAgents"
    launch_dir.mkdir(parents=True)
    old_payloads: dict[str, bytes] = {}
    loaded: set[str] = set()
    for slot in scheduler.SCHEDULE_SLOTS:
        label = f"com.fishinlab.job-agent.observe.{slot.replace(':', '')}"
        payload = plistlib.dumps({"Label": label, "OldVersion": True})
        old_payloads[label] = payload
        (launch_dir / f"{label}.plist").write_bytes(payload)
        loaded.add(label)
    failed_once = False

    def failing_reinstall(args: list[str], *, check: bool) -> int:
        nonlocal failed_once
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
            return 0
        payload = plistlib.loads(Path(args[-1]).read_bytes())
        if payload.get("StartCalendarInterval", {}).get("Hour") == 14 and not failed_once:
            failed_once = True
            raise RuntimeError("new bootstrap failed")
        loaded.add(payload["Label"])
        return 0

    with pytest.raises(RuntimeError, match="new bootstrap failed"):
        scheduler.install(
            project_root=project_root,
            python_executable=python,
            db_path=tmp_path / "data/jobagent.db",
            home=tmp_path / "home",
            launchctl=failing_reinstall,
            uid=501,
        )

    assert loaded == set(old_payloads)
    for label, old_bytes in old_payloads.items():
        assert (launch_dir / f"{label}.plist").read_bytes() == old_bytes


def test_rollback_bootout_failure_keeps_the_loaded_task_plist(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    loaded: set[str] = set()
    bootstrap_count = 0

    def torn_launchctl(args: list[str], *, check: bool) -> int:
        nonlocal bootstrap_count
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootstrap":
            bootstrap_count += 1
            if bootstrap_count == 2:
                raise RuntimeError("second bootstrap failed")
            loaded.add(plistlib.loads(Path(args[-1]).read_bytes())["Label"])
            return 0
        label = args[1].split("/")[-1]
        if label in loaded:
            return 1
        return 0

    with pytest.raises(scheduler.SchedulerRollbackError):
        scheduler.install(
            project_root=project_root,
            python_executable=python,
            db_path=tmp_path / "data/jobagent.db",
            home=tmp_path / "home",
            launchctl=torn_launchctl,
            uid=501,
        )

    assert loaded == {"com.fishinlab.job-agent.observe.0930"}
    assert (
        tmp_path
        / "home/Library/LaunchAgents/com.fishinlab.job-agent.observe.0930.plist"
    ).exists()


def test_uninstall_keeps_plist_when_bootout_fails(tmp_path) -> None:
    launch_dir = tmp_path / "home/Library/LaunchAgents"
    launch_dir.mkdir(parents=True)
    label = "com.fishinlab.job-agent.observe.0930"
    path = launch_dir / f"{label}.plist"
    path.write_text("loaded", encoding="utf-8")

    def failing_bootout(args: list[str], *, check: bool) -> int:
        if args[0] == "print":
            return 0 if args[1].endswith(label) else 1
        if args[0] == "bootout" and args[1].endswith(label):
            raise RuntimeError("bootout failed")
        return 0

    with pytest.raises(RuntimeError, match="bootout failed"):
        scheduler.uninstall(
            home=tmp_path / "home",
            launchctl=failing_bootout,
            uid=501,
        )

    assert path.exists()


def test_uninstall_removes_all_three_tasks(tmp_path) -> None:
    launch_dir = tmp_path / "home/Library/LaunchAgents"
    launch_dir.mkdir(parents=True)
    for slot in scheduler.SCHEDULE_SLOTS:
        (launch_dir / f"com.fishinlab.job-agent.observe.{slot.replace(':', '')}.plist").write_text(
            "old", encoding="utf-8"
        )
    calls: list[tuple[list[str], bool]] = []

    loaded = {
        f"com.fishinlab.job-agent.observe.{slot.replace(':', '')}"
        for slot in scheduler.SCHEDULE_SLOTS
    }

    def fake_launchctl(args: list[str], *, check: bool) -> int:
        calls.append((args, check))
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return 0

    scheduler.uninstall(
        home=tmp_path / "home",
        launchctl=fake_launchctl,
        uid=501,
    )

    assert not list(launch_dir.glob("com.fishinlab.job-agent.observe.*.plist"))
    assert len([args for args, _ in calls if args[0] == "bootout"]) == 3
    assert loaded == set()
