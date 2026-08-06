"""采集适配器。**一个适配器 = 一个招聘系统**，不是一个公司。

原来这句写的是「一个 source_key = 一个公司的一个入口」。实测把它否掉了
（docs/ATS_RESEARCH.md）：campus.xiaopeng.com 302 到 xiaopeng.jobs.feishu.cn，
同一套飞书招聘前端上还挂着 nio、luckin 等真实租户。按公司写适配器，
等于把同一份解析逻辑抄几十遍，然后每家分别坏一次。

自建的另说——大厂 10/10 自建，各写一份，那是真的没法共用。
"""
from ..routing import register_adapter
from .base import Adapter, RawJob
from .feishu import FeishuAdapter
from .tencent_join import TencentJoinAdapter

register_adapter(TencentJoinAdapter.system, TencentJoinAdapter)
# 第一个多租户适配器：一个类摊 nio / xiaopeng / ...，
# route_key 是 feishu:<tenant>，租户由 sources 表那一行提供。
register_adapter(FeishuAdapter.system, FeishuAdapter)

__all__ = ["Adapter", "RawJob", "FeishuAdapter", "TencentJoinAdapter"]
