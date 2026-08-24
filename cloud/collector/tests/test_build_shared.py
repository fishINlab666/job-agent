from pathlib import Path
import subprocess
import sys

from cloud.collector.build_shared import OUTPUT_FILES, build


def test_build_copies_only_the_approved_shared_files(tmp_path: Path) -> None:
    destination = tmp_path / "jobagent"

    manifest = build(destination)

    assert tuple(item["path"] for item in manifest) == OUTPUT_FILES
    assert all(len(item["sha256"]) == 64 for item in manifest)
    actual = tuple(
        path.relative_to(destination.parent).as_posix()
        for path in sorted(destination.rglob("*.py"))
    )
    assert actual == OUTPUT_FILES


def test_build_replaces_stale_generated_files(tmp_path: Path) -> None:
    destination = tmp_path / "jobagent"
    destination.mkdir(parents=True)
    (destination / "stale.py").write_text("must disappear", encoding="utf-8")

    build(destination)

    assert not (destination / "stale.py").exists()


def test_worker_shared_tree_imports_without_the_repository_on_sys_path(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "jobagent"
    build(destination)

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import jobagent.targets",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
