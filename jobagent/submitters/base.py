"""M6 代投层：协议与数据模型。

核心设计：代投是不可逆动作，所以协议里**没有**一步到底的 submit()。
能力被拆成两半，中间必须夹一次人工确认：

    prepare(job, profile) → 导航、查岗位状态、查登录态、逐字段填表、截图，
                            但绝不点提交。返回逐字段计划 + confirm_token。
    execute(confirm_token) → 校验 token 和字段摘要，才点提交。

为什么不用提示词让模型「记得先问用户」：提示词是软约束，模型会抽风。
这里是 API 形状约束——拿不到 prepare 产出的 token 就调不动 execute，
CLI 写错、模型跳步、将来包 MCP 都绕不过去。硬约束优先于提示词约束。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from jobagent.profile import mask

if TYPE_CHECKING:  # 仅供类型标注，避免运行时循环依赖
    from jobagent.profile import FormProfile

# 计划的存活时长。太长的话页面状态早变了，token 却还有效。
PLAN_TTL_SECONDS = 15 * 60


class FieldPlan(BaseModel):
    """单个表单字段的填写计划。确认界面就是把这些逐行念给用户听。"""

    selector: str                       # 页面定位方式
    label: str                          # 页面上的字段名，如「姓名」
    value: str | None = None            # 打算填什么。None = 留空
    source: str = ""                    # 值来自画像哪个路径，如 identity.name
    required: bool = False              # 页面是否标记必填
    action: Literal["fill", "select", "upload", "skip"] = "fill"
    note: str | None = None             # 留空原因 / 需人工补的说明
    filled: bool = False                # prepare 是否已成功写入页面
    sensitive: bool = False             # 展示和入库时要打码（身份证、手机号）

    @property
    def display(self) -> str:
        """给人看的值。敏感字段打码：确认界面会连同截图一起留在本地。"""
        if not self.value:
            return "（留空）"
        return mask(self.value) if self.sensitive else self.value

    def for_storage(self) -> dict:
        """落库形态。敏感值打码——本地库也不该存明文身份证。

        复投时值会从 profile.yaml 重新读，这里留 source 路径就够追溯了。
        """
        d = self.model_dump(include={"label", "source", "action", "filled", "note"})
        d["value"] = mask(self.value) if (self.sensitive and self.value) else self.value
        return d


class SubmissionPlan(BaseModel):
    """prepare() 的产出：表单已填好、停在提交按钮前的快照。

    status="ready" 才允许 execute。blocked 的原因（岗位已关闭、需要登录、
    没找到申请按钮）在 blocker 里，直接给用户看，不要试图自己绕过。
    """

    job_id: str
    source_key: str
    company: str
    title: str = ""
    apply_url: str = ""
    status: Literal["ready", "blocked"] = "ready"
    blocker: str | None = None
    fields: list[FieldPlan] = Field(default_factory=list)
    screenshot_path: str | None = None
    confirm_token: str = ""
    created_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and bool(self.confirm_token)

    @property
    def missing_required(self) -> list[FieldPlan]:
        """页面必填但画像里没值的字段。有这些就该提醒用户先补画像。"""
        return [f for f in self.fields if f.required and not f.value]

    @property
    def filled_fields(self) -> list[FieldPlan]:
        return [f for f in self.fields if f.filled]

    @property
    def skipped_fields(self) -> list[FieldPlan]:
        return [f for f in self.fields if not f.filled]

    def digest(self) -> str:
        """字段计划的摘要。execute 时重算一遍，对不上说明页面变了，拒绝提交。

        用户确认的是「这些值填进这些字段」，不是「这个岗位」。
        值变了就等于没确认过。
        """
        payload = [
            [f.selector, f.label, f.value or "", f.action] for f in self.fields
        ]
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class SubmissionResult(BaseModel):
    """execute() 的产出。status 取值与 applications 表对齐。"""

    status: Literal[
        "submitted",    # 提交成功
        "duplicate",    # 源站说已投过
        "failed",       # 提交了但没成功
        "closed",       # 岗位已关闭
        "blocked",      # 没到提交这步（需登录等）
        "abandoned",    # 用户在确认环节放弃
    ]
    job_id: str
    company: str
    error: str | None = None
    screenshot_path: str | None = None
    submitted_at: str | None = None
    filled_fields: list[dict] = Field(default_factory=list)
    skipped_fields: list[dict] = Field(default_factory=list)
    note: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "submitted"


class TokenError(RuntimeError):
    """token 校验失败。reason 用来决定给用户看什么话。

    unknown  —— 没这个 token（伪造、或换了进程）
    expired  —— 计划过期，页面状态大概率已经变了
    consumed —— 这个 token 已经提交过一次，不给第二次
    drifted  —— 页面字段和用户确认时不一样了
    """

    def __init__(
        self,
        reason: Literal["unknown", "expired", "consumed", "drifted"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason


def mint_token() -> str:
    """生成一次性确认令牌。

    不把任何信息编码进 token，它只是个不可猜的句柄，真正的绑定关系
    （job_id、字段摘要、过期时间）留在 SessionStore 里。
    这样就不用管签名密钥，也不存在伪造 token 骗过校验的路径。
    """
    return secrets.token_urlsafe(24)


class LiveSession:
    """prepare 和 execute 之间挂着的活浏览器。

    为什么要留活的：用户确认的是「这个页面上这些字段现在填成了这样」。
    如果 execute 时重开页面重新填一遍，用户确认过的东西和实际提交的东西
    就是两回事了——中间可能岗位关了、表单改了、值填错了。所以页面不关，
    execute 只做一件事：核对摘要，点提交。
    """

    def __init__(self, plan: SubmissionPlan, page: Any, closer: Any = None) -> None:
        self.plan = plan
        self.page = page
        self._closer = closer          # 收尾用（关 context / 停 playwright）
        self.digest = plan.digest()    # 冻结用户确认那一刻的字段快照
        self.expires_at = plan.expires_at
        self.consumed = False

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def close(self) -> None:
        """释放浏览器。收尾失败不该影响投递结果，所以吞掉异常。"""
        if self._closer is None:
            return
        try:
            self._closer()
        except Exception:
            pass
        finally:
            self._closer = None


class SessionStore:
    """按 token 管活会话。只在进程内存里，不落盘。

    不落盘是故意的：一个待确认的投递跨不过进程重启，重启后就该重新
    prepare、重新给用户看一遍。落盘反而会造出「用户几小时前确认过的
    计划现在还能提交」这种情况。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, LiveSession] = {}

    def put(self, session: LiveSession) -> None:
        self._sessions[session.plan.confirm_token] = session

    def peek(self, token: str) -> LiveSession | None:
        return self._sessions.get(token)

    def take(self, token: str, expect_digest: str | None = None) -> LiveSession:
        """取出会话并校验。这里是硬约束的落点，四道检查都不能省。"""
        session = self._sessions.get(token)
        if session is None:
            raise TokenError("unknown", "确认令牌无效，请重新执行 prepare")
        if session.consumed:
            raise TokenError("consumed", "这个计划已经提交过一次了，不重复提交")
        if session.expired:
            self.discard(token)
            raise TokenError(
                "expired",
                f"计划已过期（超过 {PLAN_TTL_SECONDS // 60} 分钟），"
                "页面状态可能已变，请重新 prepare",
            )
        if expect_digest is not None and expect_digest != session.digest:
            raise TokenError(
                "drifted", "表单内容与确认时不一致，已中止提交，请重新 prepare"
            )
        session.consumed = True
        return session

    def discard(self, token: str) -> None:
        """丢弃会话（用户放弃、或校验失败）。顺手把浏览器关掉。"""
        session = self._sessions.pop(token, None)
        if session is not None:
            session.close()

    def sweep(self) -> int:
        """清理过期会话，返回清掉的个数。防止放弃的确认把浏览器泄漏掉。"""
        stale = [t for t, s in self._sessions.items() if s.expired]
        for token in stale:
            self.discard(token)
        return len(stale)


# 进程级单例：CLI 和将来的 MCP server 共用同一份，
# 保证 prepare 和 execute 一定看到同一个会话。
SESSIONS = SessionStore()


class Submitter(Protocol):
    """代投器协议。注意这里**没有** submit()。

    想提交只有一条路：prepare 拿 token → 人工确认 → execute(token)。
    少了中间那步，execute 拿不到有效 token，物理上跑不通。
    """

    source_key: str

    def prepare(self, job: dict, profile: "FormProfile") -> SubmissionPlan:
        """导航、检查状态、逐字段填表、截图。**不点提交。**

        必须在返回前把 confirm_token 和 expires_at 填好，并把活会话
        注册进 SESSIONS。status="blocked" 时不注册（没有可提交的东西）。
        """
        ...

    def execute(self, confirm_token: str) -> SubmissionResult:
        """校验 token 与字段摘要，然后点提交。用户确认后才能调。"""
        ...

    def discard(self, confirm_token: str) -> SubmissionResult:
        """用户放弃：关掉会话，记一条 abandoned。"""
        ...
