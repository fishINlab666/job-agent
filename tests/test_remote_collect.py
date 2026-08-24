from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from jobagent.adapters.base import RawJob
from jobagent.remote_collect import (
    RemoteIngestClient,
    Snapshot,
    collect_snapshots,
    execute_remote_collection,
)
from jobagent.targets import OBSERVATION_SOURCES
from jobagent.collection import snapshot_digest


class FakeAdapter:
    skipped_no_id = 0

    def __init__(self, spec, *, fail: bool = False) -> None:
        self.source_key = str(spec["source_key"])
        self.company = str(spec["company"])
        self.fail = fail

    def fetch(self):
        if self.fail:
            raise RuntimeError("upstream private detail")
        return [
            RawJob(
                external_id=f"{self.source_key}-J1",
                title="工程师",
                raw_json={"private": "must-not-leak"},
                apply_url="https://example.test/J1",
            )
        ]


def test_all_five_sources_are_collected_before_the_first_write() -> None:
    calls: list[str] = []

    def builder(spec):
        calls.append(str(spec["source_key"]))
        return FakeAdapter(spec, fail=len(calls) == len(OBSERVATION_SOURCES))

    class NeverWrite:
        def upload(self, *args, **kwargs):
            raise AssertionError("upload must not start after an incomplete five-source read")

    report = execute_remote_collection(
        client=NeverWrite(), adapter_builder=builder, run_id="run-1", mode="official"
    )

    assert calls == [str(spec["source_key"]) for spec in OBSERVATION_SOURCES]
    assert report == {
        "schema_version": 1,
        "status": "failed",
        "source_count": 5,
        "success_count": 0,
        "results": [
            {
                "source_key": str(OBSERVATION_SOURCES[-1]["source_key"]),
                "status": "failed",
                "fetched_count": None,
                "snapshot_sha256": None,
                "error_kind": "RuntimeError",
            }
        ],
    }
    assert "private" not in json.dumps(report)


def test_successful_collection_uploads_every_source_with_safe_summary() -> None:
    uploaded: list[Snapshot] = []

    class Client:
        def upload(self, snapshot, *, run_id, mode):
            assert run_id == "run-2"
            assert mode == "technical-trial"
            uploaded.append(snapshot)
            return {"status": "committed", "window_id": 7, "window_key": "technical-trial"}

    report = execute_remote_collection(
        client=Client(),
        adapter_builder=lambda spec: FakeAdapter(spec),
        run_id="run-2",
        mode="technical-trial",
    )

    assert report["status"] == "success"
    assert report["source_count"] == report["success_count"] == 5
    assert len(uploaded) == 5
    assert all(result["status"] == "committed" for result in report["results"])
    serialized = json.dumps(report, ensure_ascii=False)
    assert "must-not-leak" not in serialized
    assert "apply_url" not in serialized


def test_http_upload_is_chunked_and_does_not_retry_failed_writes() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "temporary"})
        raise AssertionError("write request was retried")

    client = RemoteIngestClient(
        "https://collector.example.test",
        "secret-" + "x" * 32,
        transport=httpx.MockTransport(handler),
    )
    snapshot = collect_snapshots(
        adapter_builder=lambda spec: FakeAdapter(spec)
    )[0]

    with pytest.raises(httpx.HTTPStatusError):
        client.upload(snapshot, run_id="run-3", mode="official")
    assert len(calls) == 1
    assert calls[0].headers["authorization"].startswith("Bearer secret-")
    client.close()


def test_http_failure_summary_keeps_status_but_never_response_body() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            503,
            json={"error": "private upstream detail must-not-leak"},
            request=request,
        )

    client = RemoteIngestClient(
        "https://collector.example.test",
        "secret-" + "x" * 32,
        transport=httpx.MockTransport(handler),
    )

    report = execute_remote_collection(
        client=client,
        adapter_builder=lambda spec: FakeAdapter(spec),
        run_id="run-http-status",
        mode="technical-trial",
    )

    assert len(calls) == 1
    assert report["status"] == "failed"
    assert report["results"][0]["error_kind"] == "HTTP_503"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "private upstream detail" not in serialized
    assert "must-not-leak" not in serialized
    assert "secret-" not in serialized
    client.close()


def test_http_upload_chunks_by_encoded_bytes_below_worker_limit() -> None:
    calls: list[httpx.Request] = []
    collection_id = RemoteIngestClient._collection_id(
        "run-bytes", "technical-trial"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/ingest/sessions":
            return httpx.Response(
                201,
                json={
                    "status": "pending",
                    "window_id": 1,
                    "window_key": "technical-trial",
                },
            )
        if "/chunks/" in request.url.path:
            return httpx.Response(200, json={"status": "stored"})
        return httpx.Response(
            200,
            json={
                "status": "committed",
                "collection_id": collection_id,
                "fetched_count": len(snapshot.payloads),
                "snapshot_sha256": snapshot.snapshot_sha256,
                "window_id": 1,
                "window_key": "technical-trial",
            },
        )

    base = collect_snapshots(adapter_builder=lambda spec: FakeAdapter(spec))[0]
    payloads = (
        {**base.payloads[0], "external_id": "J1", "description": "x" * 800_000},
        {**base.payloads[0], "external_id": "J2", "description": "y" * 800_000},
    )
    snapshot = Snapshot(
        source_key=base.source_key,
        company=base.company,
        payloads=payloads,
        snapshot_sha256=snapshot_digest(payloads),
    )
    client = RemoteIngestClient(
        "https://collector.example.test",
        "secret-" + "x" * 32,
        transport=httpx.MockTransport(handler),
    )

    result = client.upload(snapshot, run_id="run-bytes", mode="technical-trial")

    assert result["status"] == "committed"
    chunk_calls = [call for call in calls if "/chunks/" in call.url.path]
    assert len(chunk_calls) == 2
    assert all(len(call.content) <= 1_500_000 for call in chunk_calls)
    client.close()


def test_technical_trial_rejects_a_committed_receipt_from_an_old_snapshot() -> None:
    snapshot = collect_snapshots(
        adapter_builder=lambda spec: FakeAdapter(spec)
    )[0]
    run_id = "fresh-trial"
    collection_id = RemoteIngestClient._collection_id(run_id, "technical-trial")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "committed",
                "collection_id": collection_id,
                "expected_count": len(snapshot.payloads),
                "snapshot_sha256": "0" * 64,
                "window_id": 1,
                "window_key": "technical-trial",
            },
        )

    client = RemoteIngestClient(
        "https://collector.example.test",
        "secret-" + "x" * 32,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="摘要不属于本轮"):
        client.upload(snapshot, run_id=run_id, mode="technical-trial")
    client.close()


def test_formal_workflow_is_scheduled_read_only_and_secret_scoped() -> None:
    assert not Path(".github/workflows/remote-feishu-probe.yml").exists()
    path = Path(".github/workflows/cloud-collection.yml")
    text = path.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "jobagent-cloud-collection",
        "cancel-in-progress": "false",
    }
    job = workflow["jobs"]["collect"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "30"
    assert text.count("secrets.JOBAGENT_INGEST_TOKEN") == 1
    assert "pull_request" not in workflow["on"]
    assert "uv run --frozen python -m jobagent.remote_collect" in text
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert uses == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d",
    ]


def test_required_ci_runs_worker_protocol_tests() -> None:
    text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pytest -q -p no:cacheprovider tests cloud/collector/tests" in text
