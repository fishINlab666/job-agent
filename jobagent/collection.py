"""本机与云端共用的公开岗位采集契约。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from .adapters.base import RawJob
from .normalize import fingerprint


CLOSE_GUARD_RATIO = 0.4
CLOSE_GUARD_MIN_COUNT = 5


def close_guard_tripped(*, live_before: int, disappeared: int) -> bool:
    """两套持久化共用同一批量关闭守卫，宁可暂缓关闭也不误关。"""
    return (
        disappeared >= CLOSE_GUARD_MIN_COUNT
        and live_before > 0
        and (disappeared / live_before) > CLOSE_GUARD_RATIO
    )


def _sorted_cities(value: str | list[str] | None) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return sorted(value or [])


def to_public_payload(source_key: str, company: str, job: RawJob) -> dict:
    """返回允许离开本机的公开岗位字段，不包含源站原始响应。"""
    return {
        "source_key": source_key,
        "external_id": job.external_id,
        "company": company,
        "title": job.title,
        "job_family": job.job_family,
        "raw_category": job.raw_category,
        "cities": _sorted_cities(job.cities),
        "raw_location": job.raw_location,
        "country": job.country,
        "department": job.department,
        "recruit_type": job.recruit_type,
        "grad_year": job.grad_year,
        "apply_url": job.apply_url,
        "apply_system": job.apply_system,
        "description": job.description,
    }


def job_fingerprint(job: RawJob) -> str:
    """只覆盖变更后应通知用户的字段，保持现有 ingest 口径。"""
    return fingerprint(
        {
            "title": job.title,
            "family": job.job_family,
            "cities": _sorted_cities(job.cities),
            "recruit_type": job.recruit_type,
            "department": job.department,
            "apply_url": job.apply_url,
        }
    )


def snapshot_digest(jobs: Sequence[Mapping]) -> str:
    """计算完整公开清单的稳定 SHA-256。"""
    ordered = sorted(
        (dict(job) for job in jobs),
        key=lambda job: (str(job["source_key"]), str(job["external_id"])),
    )
    canonical = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
