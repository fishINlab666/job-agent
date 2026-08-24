"""固定五源的云端采集编排。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from jobagent.collection import job_fingerprint, snapshot_digest, to_public_payload
from jobagent.targets import OBSERVATION_SOURCES, build_observation_adapter

if __package__:
    from .repository import D1Repository, StagedJob
    from .windowing import Window
else:  # Cloudflare 把 src/main.py 作为顶层模块加载。
    from repository import D1Repository, StagedJob
    from windowing import Window


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Collector:
    def __init__(
        self,
        repository: D1Repository,
        *,
        adapter_builder: Callable = build_observation_adapter,
    ) -> None:
        self.repository = repository
        self.adapter_builder = adapter_builder

    async def run_window(self, window: Window, *, trigger: str) -> dict:
        del trigger  # 触发方式不改变采集事实；后续可只用于可观测日志。
        now = _utc_now()
        window_id = await self.repository.get_or_create_window(window, now)
        owner = uuid4().hex
        lease_now = datetime.now(timezone.utc)
        acquired = await self.repository.acquire_window(
            window_id,
            owner,
            lease_now.isoformat(),
            (lease_now + timedelta(minutes=25)).isoformat(),
        )
        if not acquired:
            return await self.repository.window_summary(window_id)
        expected = {str(spec["source_key"]) for spec in OBSERVATION_SOURCES}
        successful = await self.repository.successful_sources(window_id)

        for spec in OBSERVATION_SOURCES:
            source_key = str(spec["source_key"])
            if source_key in successful:
                continue
            renewal_now = datetime.now(timezone.utc)
            if not await self.repository.acquire_window(
                window_id,
                owner,
                renewal_now.isoformat(),
                (renewal_now + timedelta(minutes=25)).isoformat(),
            ):
                return await self.repository.window_summary(window_id)
            run_id = await self.repository.start_run(window_id, source_key, _utc_now())
            try:
                adapter = self.adapter_builder(spec)
                raw_jobs = await adapter.fetch_async()
                if not raw_jobs and not getattr(adapter, "empty_is_authoritative", False):
                    raise RuntimeError("source returned an untrusted empty snapshot")
                external_ids = [job.external_id for job in raw_jobs]
                if len(external_ids) != len(set(external_ids)):
                    raise RuntimeError("source returned duplicate external_id values")
                payloads = [
                    to_public_payload(source_key, str(spec["company"]), job)
                    for job in raw_jobs
                ]
                staged = [
                    StagedJob(
                        external_id=job.external_id,
                        fingerprint=job_fingerprint(job),
                        payload=payload,
                    )
                    for job, payload in zip(raw_jobs, payloads, strict=True)
                ]
                await self.repository.stage_jobs(run_id, source_key, staged)
                await self.repository.finalize_snapshot(
                    run_id,
                    window_id,
                    source_key,
                    snapshot_digest(payloads),
                    len(payloads),
                    _utc_now(),
                )
            except Exception as exc:
                await self.repository.mark_failed(
                    run_id,
                    type(exc).__name__,
                    str(exc) or type(exc).__name__,
                    _utc_now(),
                )

        await self.repository.finish_window(window_id, expected, _utc_now(), owner)
        return await self.repository.window_summary(window_id)
