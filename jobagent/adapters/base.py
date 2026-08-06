"""适配器契约。

每个招聘入口一个适配器。注意是「入口」不是「公司」——
腾讯就有两个入口（join.qq.com 国内校招 / careers.tencent.com 社招+海外），
数据粒度和字段完全不同，必须当两个源处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RawJob:
    """适配器的统一输出。字段对齐 jobs 表。"""

    external_id: str
    title: str
    raw_json: dict                      # 源站原文，落 snapshots 用
    job_family: str | None = None
    raw_category: str | None = None
    cities: list[str] = field(default_factory=list)
    raw_location: str | None = None
    country: str | None = None
    department: str | None = None
    recruit_type: str | None = None     # campus / intern / social
    grad_year: str | None = None
    apply_url: str | None = None
    apply_system: str | None = None     # M6 路由依据
    description: str | None = None


class Adapter(Protocol):
    source_key: str
    company: str
    system: str
    entry_url: str

    def fetch(self) -> list[RawJob]:
        """拉取当前全量岗位。失败就抛异常，不要返回空列表——
        空列表会被 diff 当成「所有岗位都关闭了」，是最危险的静默故障。
        """
        ...
