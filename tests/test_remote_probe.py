from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobagent.adapters.base import RawJob
from jobagent.remote_probe import main, probe_feishu_sources
from jobagent.targets import OBSERVATION_SOURCES


FEISHU_KEYS = {
    "feishu:nio:campus",
    "feishu:xiaopeng:campus",
    "feishu:bytedance:campus",
    "feishu:sensetime:edu",
}
ROOT_KEYS = {"schema_version", "status", "source_count", "success_count", "results"}
RESULT_KEYS = {
    "source_key",
    "status",
    "fetched_count",
    "snapshot_sha256",
    "elapsed_ms",
    "error_kind",
}


class FakeAdapter:
    def __init__(self, source_key: str, company: str, *, mode: str = "ok") -> None:
        self.source_key = source_key
        self.company = company
        self.mode = mode
        self.empty_is_authoritative = False
        self.skipped_no_id = 0

    def fetch(self) -> list[RawJob]:
        if self.mode == "raise":
            raise RuntimeError("upstream failed with private-looking detail")
        if self.mode == "empty":
            return []
        if self.mode == "skipped":
            self.skipped_no_id = 1
        ids = ["J1", "J2"]
        if self.mode == "empty-id":
            ids[0] = ""
        if self.mode == "duplicate-id":
            ids[1] = ids[0]
        return [
            RawJob(
                external_id=external_id,
                title="SHOULD_NOT_LEAK",
                raw_json={"raw": "SHOULD_NOT_LEAK"},
                cities=["上海"],
                apply_url=f"https://example.test/{external_id}",
            )
            for external_id in ids
        ]


def _clock():
    values = iter(float(value) for value in range(20))
    return values.__next__


def _builder_with_mode(mode: str, calls: list[str] | None = None):
    first_key = next(
        str(spec["source_key"])
        for spec in OBSERVATION_SOURCES
        if spec["system"] == "feishu"
    )

    def build(spec):
        source_key = str(spec["source_key"])
        if calls is not None:
            calls.append(source_key)
        return FakeAdapter(
            source_key,
            str(spec["company"]),
            mode=mode if source_key == first_key else "ok",
        )

    return build


def test_probe_uses_exact_four_feishu_sources() -> None:
    calls: list[str] = []

    report = probe_feishu_sources(
        adapter_builder=_builder_with_mode("ok", calls),
        monotonic=_clock(),
    )

    assert set(calls) == FEISHU_KEYS
    assert len(calls) == 4
    assert report["status"] == "success"
    assert report["source_count"] == report["success_count"] == 4
    assert {result["source_key"] for result in report["results"]} == FEISHU_KEYS


@pytest.mark.parametrize("mode", ["empty", "empty-id", "duplicate-id", "skipped"])
def test_probe_rejects_incomplete_source_snapshots(mode: str) -> None:
    report = probe_feishu_sources(
        adapter_builder=_builder_with_mode(mode),
        monotonic=_clock(),
    )

    failed = [result for result in report["results"] if result["status"] == "failed"]
    assert report["status"] == "failed"
    assert report["success_count"] == 3
    assert len(failed) == 1
    assert failed[0]["fetched_count"] is None
    assert failed[0]["snapshot_sha256"] is None
    assert failed[0]["error_kind"] == "UntrustedSnapshotError"


def test_probe_report_has_a_closed_public_schema() -> None:
    report = probe_feishu_sources(
        adapter_builder=_builder_with_mode("ok"),
        monotonic=_clock(),
    )

    assert set(report) == ROOT_KEYS
    assert report["schema_version"] == 1
    for result in report["results"]:
        assert set(result) == RESULT_KEYS
        assert result["status"] == "success"
        assert result["fetched_count"] == 2
        assert len(result["snapshot_sha256"]) == 64
        assert result["elapsed_ms"] == 1000
        assert result["error_kind"] is None
    serialized = json.dumps(report, ensure_ascii=False)
    assert "SHOULD_NOT_LEAK" not in serialized
    assert "raw_json" not in serialized
    assert "apply_url" not in serialized


def test_main_writes_report_before_returning_failure(tmp_path: Path) -> None:
    output = tmp_path / "probe.json"
    summary = tmp_path / "summary.md"
    report = probe_feishu_sources(
        adapter_builder=_builder_with_mode("raise"),
        monotonic=_clock(),
    )

    code = main(
        ["--output", str(output), "--summary", str(summary)],
        probe=lambda: report,
    )

    assert code == 1
    assert json.loads(output.read_text(encoding="utf-8")) == report
    summary_text = summary.read_text(encoding="utf-8")
    assert "feishu:nio:campus" in summary_text
    assert "RuntimeError" in summary_text
    assert "private-looking detail" not in summary_text
    assert "SHOULD_NOT_LEAK" not in summary_text


def test_remote_probe_workflow_is_one_shot_and_read_only() -> None:
    path = Path(".github/workflows/remote-feishu-probe.yml")
    text = path.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"pull_request"}
    assert workflow["on"]["pull_request"] == {"types": ["opened"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"probe"}
    job = workflow["jobs"]["probe"]
    assert job["if"] == "github.event.pull_request.head.repo.full_name == github.repository"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "20"
    assert "secrets." not in text
    assert "schedule:" not in text
    assert "workflow_dispatch" not in text
    run_scripts = "\n".join(
        step["run"] for step in job["steps"] if "run" in step
    ).lower()
    assert "cloudflare" not in run_scripts
    assert "d1" not in run_scripts
    assert "notify" not in run_scripts
    assert "submit" not in run_scripts
    assert "uv run --frozen python -m jobagent.remote_probe" in text
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert uses == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d",
    ]
