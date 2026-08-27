from __future__ import annotations

import json
import plistlib
from pathlib import Path
import stat
import threading

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


def test_cloud_payload_checks_every_fifteen_minutes_without_embedding_token(
    tmp_path,
) -> None:
    payload = scheduler.build_cloud_payload(
        project_root=tmp_path / "repo",
        python_executable=tmp_path / "venv/bin/python",
        db_path=tmp_path / "data/jobagent.db",
        config_path=tmp_path / "private/cloud.json",
        log_dir=tmp_path / "logs",
    )

    assert payload["Label"] == "com.fishinlab.job-agent.cloud-check"
    assert payload["StartInterval"] == 15 * 60
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == [
        str(tmp_path / "venv/bin/python"),
        "-m",
        "jobagent.cli",
        "cloud-check",
        "--db",
        str(tmp_path / "data/jobagent.db"),
        "--config",
        str(tmp_path / "private/cloud.json"),
    ]
    serialized = plistlib.dumps(payload).decode("utf-8")
    assert "token" not in serialized.lower()


def test_cloud_schedule_install_is_private_and_loaded(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    config = tmp_path / "private/cloud.json"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    config.parent.mkdir()
    config.write_text('{"token":"not-in-plist"}', encoding="utf-8")
    config.chmod(0o600)
    loaded: set[str] = set()

    def fake_launchctl(args: list[str], *, check: bool) -> int:
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootstrap":
            loaded.add(plistlib.loads(Path(args[-1]).read_bytes())["Label"])
        elif args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return 0

    label = scheduler.install_cloud_check(
        project_root=project_root,
        python_executable=python,
        db_path=tmp_path / "data/jobagent.db",
        config_path=config,
        home=tmp_path / "home",
        launchctl=fake_launchctl,
        uid=501,
    )

    assert label == "com.fishinlab.job-agent.cloud-check"
    path = tmp_path / f"home/Library/LaunchAgents/{label}.plist"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert label in loaded
    assert b"not-in-plist" not in path.read_bytes()


def test_cloud_schedule_install_restores_previous_task_on_failure(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    config = tmp_path / "private/cloud.json"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    launch_dir = tmp_path / "home/Library/LaunchAgents"
    launch_dir.mkdir(parents=True)
    path = launch_dir / "com.fishinlab.job-agent.cloud-check.plist"
    old = plistlib.dumps({"Label": scheduler.CLOUD_LABEL, "OldVersion": True})
    path.write_bytes(old)
    loaded = {scheduler.CLOUD_LABEL}
    bootstrap_calls = 0

    def failing_launchctl(args: list[str], *, check: bool) -> int:
        nonlocal bootstrap_calls
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
            return 0
        bootstrap_calls += 1
        if bootstrap_calls == 1:
            raise RuntimeError("new bootstrap failed")
        loaded.add(scheduler.CLOUD_LABEL)
        return 0

    with pytest.raises(RuntimeError, match="new bootstrap failed"):
        scheduler.install_cloud_check(
            project_root=project_root,
            python_executable=python,
            db_path=tmp_path / "data/jobagent.db",
            config_path=config,
            home=tmp_path / "home",
            launchctl=failing_launchctl,
            uid=501,
        )

    assert path.read_bytes() == old
    assert loaded == {scheduler.CLOUD_LABEL}


def test_cloud_and_direct_observation_schedules_can_coexist(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    config = tmp_path / "private/cloud.json"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)

    loaded = {scheduler._label(slot) for slot in scheduler.SCHEDULE_SLOTS}

    def launchctl(args: list[str], *, check: bool) -> int:
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootstrap":
            loaded.add(plistlib.loads(Path(args[-1]).read_bytes())["Label"])
        elif args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return 0

    with pytest.raises(RuntimeError, match="不能与本机观察调度并存"):
        scheduler.install_cloud_check(
            project_root=project_root,
            python_executable=python,
            db_path=tmp_path / "data/jobagent.db",
            config_path=config,
            home=tmp_path / "home",
            launchctl=launchctl,
            uid=501,
        )
    assert scheduler.CLOUD_LABEL not in loaded
    assert all(scheduler._label(slot) in loaded for slot in scheduler.SCHEDULE_SLOTS)


def test_concurrent_schedule_installs_serialize_and_only_one_class_wins(tmp_path) -> None:
    project_root = tmp_path / "repo"
    python = tmp_path / "venv/bin/python"
    config = tmp_path / "private/cloud.json"
    home = tmp_path / "home"
    project_root.mkdir()
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    loaded: set[str] = set()
    state_lock = threading.Lock()
    start = threading.Barrier(3)
    outcomes: list[str] = []

    def fake_launchctl(args: list[str], *, check: bool) -> int:
        with state_lock:
            if args[0] == "print":
                return 0 if args[1].split("/")[-1] in loaded else 1
            if args[0] == "bootstrap":
                loaded.add(plistlib.loads(Path(args[-1]).read_bytes())["Label"])
            elif args[0] == "bootout":
                loaded.discard(args[1].split("/")[-1])
            return 0

    def install_observers() -> None:
        start.wait()
        try:
            scheduler.install(
                project_root=project_root,
                python_executable=python,
                db_path=tmp_path / "data/jobagent.db",
                home=home,
                launchctl=fake_launchctl,
                uid=501,
            )
            outcomes.append("observation")
        except RuntimeError:
            outcomes.append("observation-blocked")

    def install_cloud() -> None:
        start.wait()
        try:
            scheduler.install_cloud_check(
                project_root=project_root,
                python_executable=python,
                db_path=tmp_path / "data/jobagent.db",
                config_path=config,
                home=home,
                launchctl=fake_launchctl,
                uid=501,
            )
            outcomes.append("cloud")
        except RuntimeError:
            outcomes.append("cloud-blocked")

    threads = [
        threading.Thread(target=install_observers),
        threading.Thread(target=install_cloud),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    has_observation = any(label.startswith(scheduler.LABEL_PREFIX) for label in loaded)
    has_cloud = scheduler.CLOUD_LABEL in loaded
    assert has_observation != has_cloud
    assert sorted(outcomes) in (
        ["cloud", "observation-blocked"],
        ["cloud-blocked", "observation"],
    )


def test_flexible_payload_uses_calendar_wake_catch_up(tmp_path) -> None:
    payload = scheduler.build_flexible_payload(
        project_root=tmp_path / "release",
        python_executable=tmp_path / "release/.venv/bin/python",
        db_path=tmp_path / "data/jobagent.db",
        log_dir=tmp_path / "logs",
    )

    assert payload["Label"] == scheduler.FLEXIBLE_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["StartCalendarInterval"] == [{"Minute": 0}, {"Minute": 30}]
    assert "StartInterval" not in payload
    assert payload["ProgramArguments"][-3:] == [
        "observe-daily",
        "--db",
        str(tmp_path / "data/jobagent.db"),
    ]


def _seed_loaded_legacy_schedule(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library/LaunchAgents"
    launch_dir.mkdir(parents=True)
    loaded = set()
    for slot in scheduler.SCHEDULE_SLOTS:
        label = scheduler._label(slot)
        path = launch_dir / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "Old": True}))
        path.chmod(0o600)
        loaded.add(label)
    return home, loaded


def _fake_launchctl_for(loaded: set[str]):
    def launchctl(args: list[str], *, check: bool) -> int:
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootstrap":
            loaded.add(plistlib.loads(Path(args[-1]).read_bytes())["Label"])
        elif args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        return 0
    return launchctl


@pytest.mark.parametrize(
    "unsafe_binding",
    ["mutable-project", "project-parent-symlink", "python-parent-symlink", "db-parent-symlink"],
)
def test_flexible_runtime_binding_rejects_mutable_or_indirect_paths(
    tmp_path, unsafe_binding
) -> None:
    release_parent = tmp_path / "releases"
    release = release_parent / ("a" * 40)
    release.mkdir(parents=True)
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    database = tmp_path / "data/jobagent.db"
    database.parent.mkdir()
    database.write_bytes(b"db")

    if unsafe_binding == "mutable-project":
        release = tmp_path / "checkout"
        release.mkdir()
        python = release / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        python.chmod(0o755)
    elif unsafe_binding == "project-parent-symlink":
        real_parent = tmp_path / "real-releases"
        real_parent.mkdir()
        release.rename(real_parent / release.name)
        release_parent.rmdir()
        release_parent.symlink_to(real_parent, target_is_directory=True)
    elif unsafe_binding == "python-parent-symlink":
        real_venv = tmp_path / "real-venv"
        python.parent.parent.rename(real_venv)
        (release / ".venv").symlink_to(real_venv, target_is_directory=True)
    else:
        real_data = tmp_path / "real-data"
        database.parent.rename(real_data)
        database.parent.symlink_to(real_data, target_is_directory=True)

    with pytest.raises(ValueError):
        scheduler._validate_flexible_runtime_inputs(
            project_root=release.absolute(),
            python_executable=python.absolute(),
            db_path=database.absolute(),
        )


def test_flexible_install_replaces_only_the_complete_legacy_class(tmp_path) -> None:
    home, loaded = _seed_loaded_legacy_schedule(tmp_path)
    project_root = tmp_path / "releases" / ("a" * 40)
    python = project_root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    database = tmp_path / "data/jobagent.db"
    database.parent.mkdir()
    database.write_bytes(b"db")

    label = scheduler.install_flexible(
        project_root=project_root,
        python_executable=python,
        db_path=database,
        home=home,
        launchctl=_fake_launchctl_for(loaded),
        uid=501,
        runtime_probe=lambda _domain, _label: True,
    )

    assert label == scheduler.FLEXIBLE_LABEL
    assert loaded == {scheduler.FLEXIBLE_LABEL}
    assert not scheduler._cutover_marker(home).exists()
    assert all(
        (home / "Library/LaunchAgents" / f"{scheduler._label(slot)}.plist").exists()
        for slot in scheduler.SCHEDULE_SLOTS
    )


def test_flexible_install_rejects_symlinked_legacy_plist(tmp_path) -> None:
    home, loaded = _seed_loaded_legacy_schedule(tmp_path)
    project_root = tmp_path / "releases" / ("a" * 40)
    python = project_root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    database = tmp_path / "data/jobagent.db"
    database.parent.mkdir()
    database.write_bytes(b"db")
    label = scheduler._label(scheduler.SCHEDULE_SLOTS[0])
    plist = home / "Library/LaunchAgents" / f"{label}.plist"
    target = tmp_path / "outside.plist"
    target.write_bytes(plist.read_bytes())
    plist.unlink()
    plist.symlink_to(target)

    with pytest.raises(ValueError, match="符号链接"):
        scheduler.install_flexible(
            project_root=project_root,
            python_executable=python,
            db_path=database,
            home=home,
            launchctl=_fake_launchctl_for(loaded),
            uid=501,
            runtime_probe=lambda _domain, _label: True,
        )

    assert loaded == {scheduler._label(slot) for slot in scheduler.SCHEDULE_SLOTS}
    assert plist.is_symlink()
    assert not scheduler._cutover_marker(home).exists()


def test_flexible_recover_restores_after_process_disappears(tmp_path) -> None:
    home, loaded = _seed_loaded_legacy_schedule(tmp_path)
    project_root = tmp_path / "releases" / ("a" * 40)
    python = project_root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    database = tmp_path / "data/jobagent.db"
    database.parent.mkdir()
    database.write_bytes(b"db")
    launchctl = _fake_launchctl_for(loaded)

    with pytest.raises(SystemExit):
        scheduler.install_flexible(
            project_root=project_root,
            python_executable=python,
            db_path=database,
            home=home,
            launchctl=launchctl,
            uid=501,
            runtime_probe=lambda _domain, _label: (_ for _ in ()).throw(SystemExit()),
        )
    assert scheduler._cutover_marker(home).exists()
    assert loaded == {scheduler.FLEXIBLE_LABEL}

    scheduler.recover_flexible(
        db_path=database,
        home=home,
        launchctl=launchctl,
        uid=501,
    )

    assert loaded == {scheduler._label(slot) for slot in scheduler.SCHEDULE_SLOTS}
    assert not scheduler._cutover_marker(home).exists()


def test_flexible_recover_rejects_tampered_plist_path(tmp_path) -> None:
    home, loaded = _seed_loaded_legacy_schedule(tmp_path)
    project_root = tmp_path / "releases" / ("a" * 40)
    python = project_root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    database = tmp_path / "data/jobagent.db"
    database.parent.mkdir()
    database.write_bytes(b"db")
    launchctl = _fake_launchctl_for(loaded)
    with pytest.raises(SystemExit):
        scheduler.install_flexible(
            project_root=project_root,
            python_executable=python,
            db_path=database,
            home=home,
            launchctl=launchctl,
            uid=501,
            runtime_probe=lambda _domain, _label: (_ for _ in ()).throw(SystemExit()),
        )
    marker_path = scheduler._cutover_marker(home)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["states"][scheduler.FLEXIBLE_LABEL]["path"] = str(tmp_path / "unrelated")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(RuntimeError, match="plist 路径不匹配"):
        scheduler.recover_flexible(
            db_path=database,
            home=home,
            launchctl=launchctl,
            uid=501,
        )

    assert marker_path.exists()
    assert not (tmp_path / "unrelated").exists()


def test_flexible_recover_does_not_overwrite_a_job_that_cannot_stop(tmp_path) -> None:
    home, loaded = _seed_loaded_legacy_schedule(tmp_path)
    project_root = tmp_path / "releases" / ("a" * 40)
    python = project_root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    database = tmp_path / "data/jobagent.db"
    database.parent.mkdir()
    database.write_bytes(b"db")
    base_launchctl = _fake_launchctl_for(loaded)
    with pytest.raises(SystemExit):
        scheduler.install_flexible(
            project_root=project_root,
            python_executable=python,
            db_path=database,
            home=home,
            launchctl=base_launchctl,
            uid=501,
            runtime_probe=lambda _domain, _label: (_ for _ in ()).throw(SystemExit()),
        )
    flexible_path = home / f"Library/LaunchAgents/{scheduler.FLEXIBLE_LABEL}.plist"
    live_content = flexible_path.read_bytes()

    def cannot_stop(args: list[str], *, check: bool) -> int:
        if args[0] == "print":
            return 0 if args[1].split("/")[-1] in loaded else 1
        if args[0] == "bootout" and args[1].endswith(scheduler.FLEXIBLE_LABEL):
            return 1
        return base_launchctl(args, check=check)

    with pytest.raises(RuntimeError):
        scheduler.recover_flexible(
            db_path=database,
            home=home,
            launchctl=cannot_stop,
            uid=501,
        )

    assert flexible_path.read_bytes() == live_content
    assert scheduler._cutover_marker(home).exists()


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


def test_uninstall_also_stops_the_flexible_daily_task(tmp_path) -> None:
    launch_dir = tmp_path / "home/Library/LaunchAgents"
    launch_dir.mkdir(parents=True)
    path = launch_dir / f"{scheduler.FLEXIBLE_LABEL}.plist"
    path.write_text("flexible", encoding="utf-8")
    loaded = {scheduler.FLEXIBLE_LABEL}

    scheduler.uninstall(
        home=tmp_path / "home",
        launchctl=_fake_launchctl_for(loaded),
        uid=501,
    )

    assert loaded == set()
    assert not path.exists()


def test_uninstall_refuses_to_mutate_an_incomplete_cutover(tmp_path) -> None:
    home = tmp_path / "home"
    marker = scheduler._cutover_marker(home)
    marker.parent.mkdir(parents=True)
    marker.write_text("incomplete", encoding="utf-8")
    loaded = {scheduler.FLEXIBLE_LABEL}

    with pytest.raises(RuntimeError, match="切换未收口"):
        scheduler.uninstall(
            home=home,
            launchctl=_fake_launchctl_for(loaded),
            uid=501,
        )

    assert loaded == {scheduler.FLEXIBLE_LABEL}
    assert marker.exists()
