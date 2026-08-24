from pathlib import Path

from cloud.collector.build_shared import SHARED_FILES, build


def test_build_copies_only_the_approved_shared_files(tmp_path: Path) -> None:
    destination = tmp_path / "jobagent"

    manifest = build(destination)

    assert tuple(item["path"] for item in manifest) == SHARED_FILES
    assert all(len(item["sha256"]) == 64 for item in manifest)
    actual = tuple(
        path.relative_to(destination.parent).as_posix()
        for path in sorted(destination.rglob("*.py"))
    )
    assert actual == SHARED_FILES


def test_build_replaces_stale_generated_files(tmp_path: Path) -> None:
    destination = tmp_path / "jobagent"
    destination.mkdir(parents=True)
    (destination / "stale.py").write_text("must disappear", encoding="utf-8")

    build(destination)

    assert not (destination / "stale.py").exists()
