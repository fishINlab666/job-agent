"""生成 Worker 使用的共用 jobagent 源码副本；生成物不入库。"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_FILES = (
    "jobagent/__init__.py",
    "jobagent/adapters/__init__.py",
    "jobagent/adapters/base.py",
    "jobagent/adapters/feishu.py",
    "jobagent/adapters/tencent_join.py",
    "jobagent/collection.py",
    "jobagent/normalize.py",
    "jobagent/targets.py",
)
GENERATED_FILES = {
    "jobagent/adapters/__init__.py": (
        b'"""Worker-only adapter package; registry side effects are forbidden."""\n'
    ),
}


def build(destination: Path | None = None) -> list[dict[str, str]]:
    destination = destination or Path(__file__).resolve().parent / "src" / "jobagent"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    manifest = []
    for relative in OUTPUT_FILES:
        if relative in GENERATED_FILES:
            data = GENERATED_FILES[relative]
        else:
            source = REPO_ROOT / relative
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"共用源码必须是普通文件: {relative}")
            data = source.read_bytes()
        target = destination.parent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest.append(
            {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}
        )
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, sort_keys=True, indent=2))
