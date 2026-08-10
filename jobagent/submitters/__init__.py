"""M6 代投层。

注册表在这里，不在调用方。cli.py 说了后面要在外面包一层 MCP server，
如果注册写在 cli 里，MCP 那边就得记着再写一遍——漏一次的表现是「这家投不了」，
而不是报错。谁提供实现谁登记，调用方只管查。
"""
from ..routing import register_submitter
from .base import SubmissionResult, Submitter
from .feishu import FeishuSubmitter
from .tencent_join import TencentJoinSubmitter

# 键是**招聘系统**，不是公司。自建的系统名就是它自己。
# 飞书一个类管四个租户（同一套前端），租户由 routing 传进构造函数。
register_submitter(TencentJoinSubmitter.system, TencentJoinSubmitter)
register_submitter(FeishuSubmitter.system, FeishuSubmitter)

__all__ = [
    "Submitter",
    "SubmissionResult",
    "TencentJoinSubmitter",
    "FeishuSubmitter",
]
