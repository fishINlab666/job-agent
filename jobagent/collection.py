"""本机与云端共用的公开岗位采集契约。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from .adapters.base import RawJob
from .normalize import fingerprint


CLOSE_GUARD_RATIO = 0.4
CLOSE_GUARD_MIN_COUNT = 5
PUBLIC_PAYLOAD_KEYS = frozenset(
    {
        "source_key",
        "external_id",
        "company",
        "title",
        "job_family",
        "raw_category",
        "cities",
        "raw_location",
        "country",
        "department",
        "recruit_type",
        "grad_year",
        "apply_url",
        "apply_system",
        "description",
    }
)


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
    return payload_fingerprint(
        to_public_payload("fingerprint-only", "fingerprint-only", job)
    )


def payload_fingerprint(payload: Mapping) -> str:
    """从严格公开载荷重算变化指纹；远端接收方不信任客户端指纹。"""
    if set(payload) != PUBLIC_PAYLOAD_KEYS:
        raise ValueError("公开岗位载荷字段不等于批准集合")
    if not str(payload.get("source_key") or "").strip():
        raise ValueError("公开岗位缺少 source_key")
    if not str(payload.get("external_id") or "").strip():
        raise ValueError("公开岗位缺少 external_id")
    if not str(payload.get("company") or "").strip():
        raise ValueError("公开岗位缺少 company")
    if not str(payload.get("title") or "").strip():
        raise ValueError("公开岗位缺少 title")
    cities = payload.get("cities")
    if not isinstance(cities, list) or any(not isinstance(city, str) for city in cities):
        raise ValueError("公开岗位 cities 必须是字符串列表")
    return fingerprint(
        {
            "title": payload["title"],
            "family": payload["job_family"],
            "cities": _sorted_cities(cities),
            "recruit_type": payload["recruit_type"],
            "department": payload["department"],
            "apply_url": payload["apply_url"],
        }
    )


def validate_snapshot(adapter, jobs: Sequence[RawJob]) -> None:
    """验证来源清单足以代表完整快照；失败时绝不进入 diff/发布。"""
    if not jobs:
        raise ValueError("不接受不可信的空岗位清单")
    if int(getattr(adapter, "skipped_no_id", 0)) != 0:
        raise ValueError("来源清单跳过了缺少 external_id 的行")
    external_ids = [str(job.external_id).strip() for job in jobs]
    if any(not external_id for external_id in external_ids):
        raise ValueError("来源返回空 external_id")
    if len(external_ids) != len(set(external_ids)):
        raise ValueError("来源返回重复 external_id")


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
