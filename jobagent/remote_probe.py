"""一次性远端网络探针；只输出来源级摘要，不保存岗位内容。"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .collection import snapshot_digest, to_public_payload, validate_snapshot
from .targets import OBSERVATION_SOURCES, build_observation_adapter


FEISHU_SOURCE_KEYS = frozenset(
    str(spec["source_key"])
    for spec in OBSERVATION_SOURCES
    if spec["system"] == "feishu"
)


class UntrustedSnapshotError(RuntimeError):
    """来源响应不能证明是一份完整、可发布的岗位清单。"""


def _feishu_specs(
    specs: Sequence[Mapping[str, str | None]],
) -> tuple[Mapping[str, str | None], ...]:
    selected = tuple(spec for spec in specs if spec.get("system") == "feishu")
    keys = {str(spec.get("source_key") or "") for spec in selected}
    if keys != FEISHU_SOURCE_KEYS or len(selected) != len(FEISHU_SOURCE_KEYS):
        raise ValueError("远端探针来源必须精确等于当前四家飞书观察源")
    return selected


def _validate_jobs(adapter: Any, jobs: list[Any]) -> None:
    try:
        validate_snapshot(adapter, jobs)
    except ValueError as exc:
        raise UntrustedSnapshotError(str(exc)) from None


def probe_feishu_sources(
    *,
    specs: Sequence[Mapping[str, str | None]] = OBSERVATION_SOURCES,
    adapter_builder: Callable[[Mapping[str, str | None]], Any] = build_observation_adapter,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """顺序探测四个飞书来源，返回闭合且不含岗位正文的报告。"""
    results: list[dict] = []
    for spec in _feishu_specs(specs):
        source_key = str(spec["source_key"])
        started = monotonic()
        try:
            adapter = adapter_builder(spec)
            if adapter.source_key != source_key or adapter.company != spec["company"]:
                raise UntrustedSnapshotError("Adapter 身份与固定观察源不一致")
            jobs = adapter.fetch()
            _validate_jobs(adapter, jobs)
            payloads = [
                to_public_payload(source_key, str(spec["company"]), job)
                for job in jobs
            ]
            result = {
                "source_key": source_key,
                "status": "success",
                "fetched_count": len(jobs),
                "snapshot_sha256": snapshot_digest(payloads),
                "elapsed_ms": max(0, int(round((monotonic() - started) * 1000))),
                "error_kind": None,
            }
        except Exception as exc:
            result = {
                "source_key": source_key,
                "status": "failed",
                "fetched_count": None,
                "snapshot_sha256": None,
                "elapsed_ms": max(0, int(round((monotonic() - started) * 1000))),
                "error_kind": type(exc).__name__,
            }
        results.append(result)

    success_count = sum(result["status"] == "success" for result in results)
    return {
        "schema_version": 1,
        "status": "success" if success_count == len(results) else "failed",
        "source_count": len(results),
        "success_count": success_count,
        "results": results,
    }


def _report_bytes(report: dict) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError(f"报告目录不存在：{path.parent}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _summary(report: dict) -> str:
    lines = [
        "## Remote Feishu Network Probe",
        "",
        "| source_key | status | count | snapshot_sha256 | elapsed_ms | error_kind |",
        "|---|---:|---:|---|---:|---|",
    ]
    for result in report["results"]:
        lines.append(
            "| {source_key} | {status} | {count} | {digest} | {elapsed} | {error} |".format(
                source_key=result["source_key"],
                status=result["status"],
                count=result["fetched_count"] if result["fetched_count"] is not None else "—",
                digest=result["snapshot_sha256"] or "—",
                elapsed=result["elapsed_ms"],
                error=result["error_kind"] or "—",
            )
        )
    lines.extend(
        [
            "",
            f"Overall: **{report['status']}** ({report['success_count']}/{report['source_count']})",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict, output: Path, summary: Path | None = None) -> None:
    """先原子写 JSON，再追加安全 Markdown summary；不得写岗位载荷。"""
    _atomic_write(Path(output), _report_bytes(report))
    if summary is not None:
        with Path(summary).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_summary(report))


def main(
    argv: Sequence[str] | None = None,
    *,
    probe: Callable[[], dict] = probe_feishu_sources,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    report = probe()
    write_report(report, args.output, args.summary)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":  # pragma: no cover - 由 Workflow 直接执行
    raise SystemExit(main())
