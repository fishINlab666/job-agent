"""GitHub hosted runner 的五源公开岗位采集与分块上传入口。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from .collection import snapshot_digest, to_public_payload, validate_snapshot
from .targets import OBSERVATION_SOURCES, build_observation_adapter


CHUNK_SIZE = 10
MAX_CHUNK_BODY_BYTES = 1_500_000
MAX_JOB_JSON_BYTES = 1_000_000
MAX_TOTAL_PAYLOAD_BYTES = 75_000_000
MAX_SOURCE_JOBS = 20_000
MAX_TOTAL_JOBS = 30_000
EXPECTED_SOURCE_KEYS = tuple(str(spec["source_key"]) for spec in OBSERVATION_SOURCES)


@dataclass(frozen=True)
class Snapshot:
    source_key: str
    company: str
    payloads: tuple[dict, ...]
    snapshot_sha256: str


class RemoteSourceError(RuntimeError):
    def __init__(self, source_key: str, error_kind: str) -> None:
        super().__init__(f"{source_key}: {error_kind}")
        self.source_key = source_key
        self.error_kind = error_kind


def collect_snapshots(
    *,
    specs: Sequence[Mapping[str, str | None]] = OBSERVATION_SOURCES,
    adapter_builder: Callable[[Mapping[str, str | None]], Any] = build_observation_adapter,
) -> tuple[Snapshot, ...]:
    """先完整收齐五源；任一失败时调用方尚未得到可上传结果。"""
    observed_keys = tuple(str(spec.get("source_key") or "") for spec in specs)
    if observed_keys != EXPECTED_SOURCE_KEYS:
        raise ValueError("远端正式采集来源及顺序必须精确等于批准的五源")

    snapshots: list[Snapshot] = []
    total = 0
    total_bytes = 0
    for spec in specs:
        source_key = str(spec["source_key"])
        company = str(spec["company"])
        try:
            adapter = adapter_builder(spec)
            if adapter.source_key != source_key or adapter.company != company:
                raise ValueError("Adapter 身份与批准来源不一致")
            jobs = adapter.fetch()
            validate_snapshot(adapter, jobs)
            if len(jobs) > MAX_SOURCE_JOBS:
                raise ValueError("单个来源岗位数超过批准上限")
            payloads = tuple(
                to_public_payload(source_key, company, job) for job in jobs
            )
            payload_sizes = [
                len(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                for payload in payloads
            ]
            if any(size > MAX_JOB_JSON_BYTES for size in payload_sizes):
                raise ValueError("单岗位 JSON 超过 D1 安全上限")
            total_bytes += sum(payload_sizes)
            if total_bytes > MAX_TOTAL_PAYLOAD_BYTES:
                raise ValueError("五源 JSON 总量超过批准的 75 MB 上限")
            total += len(payloads)
            if total > MAX_TOTAL_JOBS:
                raise ValueError("五源岗位总数超过批准上限")
            snapshots.append(
                Snapshot(
                    source_key=source_key,
                    company=company,
                    payloads=payloads,
                    snapshot_sha256=snapshot_digest(payloads),
                )
            )
        except Exception as exc:
            raise RemoteSourceError(source_key, type(exc).__name__) from None
    return tuple(snapshots)


class RemoteIngestClient:
    def __init__(self, base_url: str, token: str, *, transport=None) -> None:
        normalized = str(base_url).rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path:
            raise ValueError("JOBAGENT_CLOUD_URL 必须是无路径的 HTTPS 地址")
        if len(token) < 32:
            raise ValueError("JOBAGENT_INGEST_TOKEN 长度不足")
        self.client = httpx.Client(
            base_url=normalized,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _json(self, method: str, path: str, **kwargs) -> dict:
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("云端上传响应必须是 JSON object")
        return payload

    @staticmethod
    def _collection_id(run_id: str, mode: str) -> str:
        return hashlib.sha256(
            "\0".join(("remote-collection-v1", run_id, mode)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _session_id(snapshot: Snapshot, run_id: str, mode: str) -> str:
        fields = (
            "remote-ingest-v1",
            run_id,
            mode,
            snapshot.source_key,
            snapshot.snapshot_sha256,
        )
        return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()

    @staticmethod
    def _chunk_bodies(payloads: tuple[dict, ...]):
        chunk: list[dict] = []
        for payload in payloads:
            encoded_job = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded_job) > MAX_JOB_JSON_BYTES:
                raise ValueError("单岗位 JSON 超过 D1 安全上限")
            candidate = [*chunk, payload]
            digest = snapshot_digest(candidate)
            body = json.dumps(
                {"chunk_sha256": digest, "jobs": candidate},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if chunk and (
                len(candidate) > CHUNK_SIZE or len(body) > MAX_CHUNK_BODY_BYTES
            ):
                previous_digest = snapshot_digest(chunk)
                yield json.dumps(
                    {"chunk_sha256": previous_digest, "jobs": chunk},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                chunk = [payload]
                continue
            if len(body) > MAX_CHUNK_BODY_BYTES:
                raise ValueError("单岗位无法装入批准的上传分块")
            chunk = candidate
        if chunk:
            yield json.dumps(
                {"chunk_sha256": snapshot_digest(chunk), "jobs": chunk},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

    def upload(self, snapshot: Snapshot, *, run_id: str, mode: str) -> dict:
        if not run_id or len(run_id) > 128:
            raise ValueError("远端 run_id 必须为 1-128 字符")
        if mode not in {"official", "technical-trial"}:
            raise ValueError("远端采集模式未获批准")
        session_id = self._session_id(snapshot, run_id, mode)
        collection_id = self._collection_id(run_id, mode)
        begin = self._json(
            "POST",
            "/v1/ingest/sessions",
            json={
                "session_id": session_id,
                "collection_id": collection_id,
                "source_key": snapshot.source_key,
                "mode": mode,
                "expected_count": len(snapshot.payloads),
                "expected_bytes": sum(
                    len(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    for payload in snapshot.payloads
                ),
                "snapshot_sha256": snapshot.snapshot_sha256,
            },
        )
        if begin.get("status") == "committed":
            self._validate_trial_receipt(
                begin,
                snapshot=snapshot,
                collection_id=collection_id,
                mode=mode,
            )
            return begin
        if begin.get("status") != "pending":
            raise ValueError("云端 session 未进入 pending 或 committed")

        for index, body in enumerate(self._chunk_bodies(snapshot.payloads)):
            result = self._json(
                "PUT",
                f"/v1/ingest/sessions/{session_id}/chunks/{index}",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            if result.get("status") not in {"stored", "already-stored"}:
                raise ValueError("云端 chunk 未被确认存储")

        committed = self._json(
            "POST", f"/v1/ingest/sessions/{session_id}/commit", json={}
        )
        self._validate_trial_receipt(
            committed,
            snapshot=snapshot,
            collection_id=collection_id,
            mode=mode,
        )
        return committed

    @staticmethod
    def _validate_trial_receipt(
        receipt: Mapping[str, Any],
        *,
        snapshot: Snapshot,
        collection_id: str,
        mode: str,
    ) -> None:
        if mode != "technical-trial":
            return
        observed_count = receipt.get("expected_count", receipt.get("fetched_count"))
        if int(observed_count) != len(snapshot.payloads):
            raise ValueError("技术试运行回执岗位数不属于本轮快照")
        if str(receipt.get("snapshot_sha256")) != snapshot.snapshot_sha256:
            raise ValueError("技术试运行回执摘要不属于本轮快照")
        if str(receipt.get("collection_id")) != collection_id:
            raise ValueError("技术试运行回执不属于本轮 collection")


def execute_remote_collection(
    *,
    client,
    run_id: str,
    mode: str,
    adapter_builder: Callable[[Mapping[str, str | None]], Any] = build_observation_adapter,
) -> dict:
    try:
        snapshots = collect_snapshots(adapter_builder=adapter_builder)
    except RemoteSourceError as exc:
        return {
            "schema_version": 1,
            "status": "failed",
            "source_count": len(EXPECTED_SOURCE_KEYS),
            "success_count": 0,
            "results": [
                {
                    "source_key": exc.source_key,
                    "status": "failed",
                    "fetched_count": None,
                    "snapshot_sha256": None,
                    "error_kind": exc.error_kind,
                }
            ],
        }

    results: list[dict] = []
    approved_window: tuple[int, str] | None = None
    for snapshot in snapshots:
        try:
            response = client.upload(snapshot, run_id=run_id, mode=mode)
            status = str(response.get("status"))
            if status != "committed":
                raise ValueError("云端没有确认 committed")
            observed_window = (
                int(response["window_id"]),
                str(response["window_key"]),
            )
            if approved_window is None:
                approved_window = observed_window
            elif observed_window != approved_window:
                raise ValueError("同一轮五源被服务端拆进不同窗口")
            results.append(
                {
                    "source_key": snapshot.source_key,
                    "status": status,
                    "fetched_count": int(
                        response.get(
                            "expected_count",
                            response.get("fetched_count", len(snapshot.payloads)),
                        )
                    ),
                    "snapshot_sha256": str(
                        response.get("snapshot_sha256") or snapshot.snapshot_sha256
                    ),
                    "error_kind": None,
                }
            )
        except Exception as exc:
            error_kind = (
                f"HTTP_{exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            results.append(
                {
                    "source_key": snapshot.source_key,
                    "status": "failed",
                    "fetched_count": len(snapshot.payloads),
                    "snapshot_sha256": snapshot.snapshot_sha256,
                    "error_kind": error_kind,
                }
            )
            break
    attempted = {str(result["source_key"]) for result in results}
    for snapshot in snapshots:
        if snapshot.source_key not in attempted:
            results.append(
                {
                    "source_key": snapshot.source_key,
                    "status": "unattempted",
                    "fetched_count": len(snapshot.payloads),
                    "snapshot_sha256": snapshot.snapshot_sha256,
                    "error_kind": None,
                }
            )
    success_count = sum(result["status"] == "committed" for result in results)
    return {
        "schema_version": 1,
        "status": "success" if success_count == len(EXPECTED_SOURCE_KEYS) else "failed",
        "source_count": len(EXPECTED_SOURCE_KEYS),
        "success_count": success_count,
        "results": results,
    }


def main() -> int:
    base_url = os.environ.get("JOBAGENT_CLOUD_URL", "")
    token = os.environ.get("JOBAGENT_INGEST_TOKEN", "")
    run_id = "-".join(
        filter(None, [os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")])
    )
    mode = os.environ.get("JOBAGENT_COLLECTION_MODE", "official")
    client = RemoteIngestClient(base_url, token)
    try:
        report = execute_remote_collection(client=client, run_id=run_id, mode=mode)
    finally:
        client.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":  # pragma: no cover - Workflow 直接执行
    raise SystemExit(main())
