"""MVP 真实观察池。

这五家公司已经由 ``scripts/run_five.py`` 逐个核过入口；生产观察和实测脚本共用
同一份清单，避免公司、租户或门户路径出现两套答案。
"""
from __future__ import annotations


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
