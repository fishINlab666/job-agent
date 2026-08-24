"""MVP 真实观察池。

这五家公司已经由 ``scripts/run_five.py`` 逐个核过入口；生产观察和实测脚本共用
同一份清单，避免公司、租户或门户路径出现两套答案。
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from .adapters.base import Adapter
from .adapters.feishu import FeishuAdapter
from .adapters.tencent_join import TencentJoinAdapter


OBSERVATION_SLOTS: tuple[str, ...] = ("09:30", "14:30", "20:30")


OBSERVATION_SOURCES: tuple[dict[str, str | None], ...] = (
    {
        "source_key": "tencent_join",
        "company": "腾讯",
        "system": "tencent_join",
        "entry_url": "https://join.qq.com/post.html",
        "tenant": None,
    },
    {
        "source_key": "feishu:nio:campus",
        "company": "蔚来",
        "system": "feishu",
        "entry_url": "https://nio.jobs.feishu.cn/campus/",
        "tenant": "nio",
    },
    {
        "source_key": "feishu:xiaopeng:campus",
        "company": "小鹏汽车",
        "system": "feishu",
        "entry_url": "https://xiaopeng.jobs.feishu.cn/campus/",
        "tenant": "xiaopeng",
    },
    {
        "source_key": "feishu:bytedance:campus",
        "company": "字节跳动",
        "system": "feishu",
        "entry_url": "https://bytedance.jobs.feishu.cn/campus/",
        "tenant": "bytedance",
    },
    {
        "source_key": "feishu:sensetime:edu",
        "company": "商汤科技",
        "system": "feishu",
        "entry_url": "https://hr-jobs.sensetime.com/edu/",
        "tenant": "sensetime",
    },
)


def build_observation_adapter(spec: Mapping[str, str | None]) -> Adapter:
    """从唯一观察池配置构造采集器，并拒绝配置与采集器身份不一致。"""
    system = spec.get("system")
    source_key = spec.get("source_key")
    company = spec.get("company")
    if not source_key or not company:
        raise ValueError("观察源必须包含 source_key 和 company")

    if system == "tencent_join":
        adapter = TencentJoinAdapter()
    elif system == "feishu":
        tenant = spec.get("tenant")
        entry_url = spec.get("entry_url")
        if not tenant or not entry_url:
            raise ValueError(f"{source_key}: 飞书观察源缺少 tenant 或 entry_url")
        parts = source_key.split(":")
        if len(parts) != 3:
            raise ValueError(f"{source_key}: 飞书观察源必须固定门户")
        host = urlsplit(entry_url).hostname
        if not host:
            raise ValueError(f"{source_key}: entry_url 缺少有效 host")
        adapter = FeishuAdapter(
            tenant=tenant,
            company=company,
            portal=parts[2],
            host=host,
        )
    else:
        raise ValueError(f"未知观察源系统: {system!r}")

    if adapter.source_key != source_key or adapter.company != company:
        raise ValueError(
            f"观察源身份不一致: 配置={source_key}/{company}, "
            f"采集器={adapter.source_key}/{adapter.company}"
        )
    return adapter
