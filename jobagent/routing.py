"""把岗位路由到采集器/代投器。**按 ATS 系统注册，不按公司注册。**

这是调研（docs/ATS_RESEARCH.md）落到代码上的那一步。原来的注册表是
`{source_key: cls}`——一家公司一个键一个类。实测下来这个键选错了：

    campus.xiaopeng.com  →  xiaopeng.jobs.feishu.cn
    （同一套页面上还有 nio、luckin 等真实租户）

同一个 ATS 上几十家公司的招聘前端是同一套应用，只有租户配置不同。
所以注册表的键是**系统**，租户是实例参数：

    _SUBMITTERS["feishu"] = FeishuSubmitter       # 一个类
    feishu:xiaopeng / feishu:nio / feishu:luckin  # 三条 route_key，共用那个类

加一家公司的边际成本≈一行 sources 记录。按公司注册等于把同一份逻辑抄几十遍。

两条硬规则，都是被实测打出来的：
  1. 只有域名级识别（DOMAIN）能路由。页面标记（MARKUP）只当线索——
     字节自建页里有 `FeatureDailyExamUseBeisen`，按标记就会被判成北森，
     然后拿北森的逻辑去打一个根本不是北森的页面。
  2. 多租户系统拿不到租户就不许启动。少一个租户参数不会报错，
     只会投到别人家公司去。
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from typing import Any

from . import ats

_SUBMITTERS: dict[str, type] = {}
_ADAPTERS: dict[str, type] = {}

# 实现登记在 jobagent.submitters / jobagent.adapters 的包初始化里。查表前得先确保
# 那两个包被 import 过，否则表是空的，报出来的是「识别出是 X，但还没写代投器」——
# 一句听着很确定的错话。真实情况是「写了，只是没加载」。
#
# 分开加载是有意的：代投层依赖 playwright，采集层不依赖。合成一个函数的话，
# 光是查采集器也会把 playwright 拽进来，环境里没装就直接 ImportError。
_LOADED: set[str] = set()


def _ensure(pkg: str) -> None:
    if pkg not in _LOADED:
        _LOADED.add(pkg)          # 先记后导：导入过程中万一回头查表，不会递归
        importlib.import_module(f"jobagent.{pkg}")


class RouteError(RuntimeError):
    """路由不出去。消息里必须写清楚「按什么依据、缺什么」，不要只说找不到。"""


@dataclass(frozen=True)
class Route:
    """一次路由判定。lead 和 system 分开存。

    system=None 但 lead 有值，意思是「像某个系统，但证据不够，别拿去用」。
    把这两种情况混成一个字段，就等于让线索悄悄升级成判定。
    """

    system: str | None
    tenant: str | None
    basis: str                  # 依据，给人看的
    lead: str | None = None     # 够不上路由的猜测
    conflict: str | None = None  # 两处证据对不上。resolve 只记录，_build 才拒绝

    @property
    def key(self) -> str:
        if not self.system:
            return ats.UNKNOWN
        return f"{self.system}:{self.tenant}" if self.tenant else self.system

    @property
    def ok(self) -> bool:
        return self.system is not None


def register_submitter(system: str, cls: type) -> None:
    _SUBMITTERS[system] = cls


def register_adapter(system: str, cls: type) -> None:
    _ADAPTERS[system] = cls


def registered_submitters() -> dict[str, type]:
    _ensure("submitters")
    return dict(_SUBMITTERS)


def registered_adapters() -> dict[str, type]:
    _ensure("adapters")
    return dict(_ADAPTERS)


def _on_vendor_domain(system: str, url: str) -> bool:
    """这个链接是不是落在这家厂商自己的域名上。

    落在自己域名上，子域名/路径里的租户才有意义。公司自有域名（campus.xiaopeng.com
    这种 CNAME 过去的）抠不出租户——那种情况的租户只能靠 sources.tenant 配，
    这也正是那一列存在的理由。
    """
    v = ats.BY_KEY.get(system)
    host = ats.host_of(url)
    if v is None or not host:
        return False
    return any(host == d or host.endswith(f".{d}") for d in v.domains)


def _pick_tenant(system: str, url: str, src: dict) -> tuple[str | None, str | None]:
    """定租户，并把「配的和链接里的对不上」当故障报出来。

    从链接里取到的是**证据**（xiaopeng.jobs.feishu.cn 里那个 xiaopeng 已实测核实，
    页面自称「加入小鹏汽车」）；sources.tenant 是**人工配的**。所以证据优先，
    配置只做兜底——北森/Moka 的租户页格式还没实测，取不到时只能靠配。

    两个都有且不一样时，不许挑一个接着用：那说明有一个是错的（配置过期、复制粘贴
    串行），而下一步是不可逆的投递动作。赌错方向就是投到别人家公司去。
    """
    configured = (src.get("tenant") or "").strip() or None
    # 只有链接确实落在这家厂商的域名上，才算「链接里的租户」。
    # ats.tenant_from_url 不校验归属，它只管抠第一段子域名——拿一个跟厂商无关的
    # 链接去问，会从 acme.example.com 里抠出 "acme" 当租户，凭空造一个。
    derived = ats.tenant_from_url(url, system) if url and _on_vendor_domain(system, url) else None
    if derived and configured and derived != configured:
        return derived, f"sources.tenant={configured} 与链接里的 {derived} 对不上"
    return derived or configured, None


def resolve(job: dict, source: dict | None = None) -> Route:
    """判断这个岗位该走哪个系统。按可靠性从高到低试。

    顺序是有意的：库里存的明确值 > 域名识别 > 源站配置 > 老的 source_key。
    前面的赢，因为越往后越像猜。
    """
    src = source or {}

    url = (job.get("apply_url") or "").strip()

    # 1. 采集时就写进库的判定，最可信。
    if system := (job.get("apply_system") or "").strip():
        tenant, conflict = _pick_tenant(system, url, src)
        return Route(system, tenant, f"jobs.apply_system={system}", conflict=conflict)

    # 2. 从投递链接识别。只认域名级。
    if url:
        d = ats.detect(url)
        if d.routable:
            tenant, conflict = _pick_tenant(d.system, url, src)
            return Route(d.system, tenant, f"apply_url 域名命中 {d.system}",
                         conflict=conflict)
        if d.system != ats.UNKNOWN:
            # 标记命中到此为止。要用就人工核实后写进 sources.system，
            # 别让它从这里悄悄变成路由依据。
            return Route(
                None, None,
                f"apply_url 认不出（{d.confidence} 级证据不够路由）",
                lead=d.system,
            )

    # 3. 源站上配的系统。
    if system := (src.get("system") or "").strip():
        if system in ats.BY_KEY:
            tenant, conflict = _pick_tenant(system, url, src)
            return Route(system, tenant, f"sources.system={system}", conflict=conflict)

    # 4. 老路：source_key 本身当系统用（tencent_join 这种自建的一直是这么注册的）。
    #    老库里 sources.system 存的是 "self_built"——那是形容词不是厂商 key，
    #    第 3 步认不出，会落到这里由 source_key 兜住。
    if key := (job.get("source_key") or src.get("source_key") or "").strip():
        if key in ats.BY_KEY:
            return Route(key, None, f"source_key={key}（老式注册）")

    return Route(None, None, "没有任何可路由的依据")


def _build(pkg: str, kind: str, table: dict[str, type], route: Route, **kwargs: Any) -> Any:
    _ensure(pkg)
    if not route.ok:
        hint = f"，疑似 {route.lead}（证据只到页面标记，需人工核实）" if route.lead else ""
        raise RouteError(f"认不出这个岗位用的什么招聘系统：{route.basis}{hint}")

    # 两处证据对不上就停。resolve 只记录不拒绝（它是纯判断，给人看的地方也要能
    # 显示出来）；到这里要动真格了，才是该拦的地方。
    if route.conflict:
        raise RouteError(f"租户对不上，不敢往下走：{route.conflict}。先核实是哪一处过期了。")

    cls = table.get(route.system or "")
    if cls is None:
        known = "、".join(sorted(table)) or "（空）"
        raise RouteError(
            f"识别出是 {route.system}（{route.basis}），但还没写{kind}。已注册：{known}"
        )

    params = inspect.signature(cls).parameters
    takes_tenant = "tenant" in params

    # 多租户系统必须把租户传进去。传不进去就别启动——
    # 少一个租户参数不会报错，只会投到别人家公司去。
    vendor = ats.BY_KEY.get(route.system or "")
    multi_tenant = vendor is not None and not vendor.self_built
    if multi_tenant and not takes_tenant:
        raise RouteError(
            f"{route.system} 是多租户系统，但 {cls.__name__} 的构造函数不接 tenant 参数。"
            "这样跑起来会拿别的租户的页面填表。"
        )
    if multi_tenant and not route.tenant:
        raise RouteError(
            f"{route.system} 是多租户系统，但没取到租户（{route.basis}）。"
            "补上 jobs.apply_url 或 sources.tenant 再来。"
        )

    if takes_tenant:
        kwargs["tenant"] = route.tenant
    # 按构造函数实际接受的参数过滤：不是每个实现都要 user_data_dir。
    return cls(**{k: v for k, v in kwargs.items() if k in params})


def get_submitter(job: dict, source: dict | None = None, **kwargs: Any) -> Any:
    return _build("submitters", "代投器", _SUBMITTERS, resolve(job, source), **kwargs)


def portal_of(source_key: str) -> str | None:
    """从 `feishu:<tenant>:<portal>` 里取门户。两段的键返回 None。

    门户为什么从 `source_key` 里取、而不是新加一列：这个键**就是**判据本身。
    `sources` 里两行 `feishu:nio` 和 `feishu:nio:campus` 是两个源、两条 run、
    两套关闭守卫分母，而它们的区别只有门户。把门户放进键，
    「一行 sources = 一个采集单位」这条不变；另开一列则要多回答一个问题：
    同一个 source_key 配了两个门户时哪个说话算数。

    只切前两个冒号，门户名里再有冒号也不切（`maxsplit=2`）——
    宁可把奇怪的门户名原样传给接口，也不要在这里悄悄截断成另一个门户。
    """
    parts = (source_key or "").strip().split(":", 2)
    if len(parts) < 3:
        return None
    return parts[2].strip() or None


def get_adapter(job: dict, source: dict | None = None, **kwargs: Any) -> Any:
    """按 sources 行造采集器。

    `company` 要从 sources 行传进去，别让适配器自己编。多租户适配器手里只有租户名
    （`nio`），公司名（`蔚来`）只有 sources 行里有。不传的后果不是报错，是 2265 条
    岗位全落在 company='nio' 底下，然后 `jobs --company 蔚来` 一条都查不到——
    看起来像采集没跑，实际是跑了但挂错了名字。

    `portal` 和 `host` 同理，也都**只从 sources 行里取**：
    - `portal` 从 `source_key` 第三段（`feishu:nio:campus` → `campus`）。
    - `host` 从 `entry_url`，为了自定义域名（`hr-jobs.sensetime.com` 和
      `sensetime.jobs.feishu.cn` 是同一个租户，实测同样 159 条、id 交集 159）。
      **不从岗位链接里现推** —— 那等于放宽域名判据，会把无关站点判成飞书。
      指到别的租户这种情况由 `_pick_tenant` 的冲突检查挡住（同一厂商域名下
      链接里的租户和配的租户对不上就拒绝启动）。

    显式传参而不是塞进 kwargs 让调用方操心：`_build` 会按构造函数实际接受的参数
    过滤，腾讯那种不接 company / portal / host 的实现不受影响。调用方给的优先。
    """
    src = source or {}
    if company := (src.get("company") or "").strip():
        kwargs.setdefault("company", company)
    key = (src.get("source_key") or job.get("source_key") or "").strip()
    if portal := portal_of(key):
        kwargs.setdefault("portal", portal)
    if host := ats.host_of(src.get("entry_url") or ""):
        kwargs.setdefault("host", host)
    adapter = _build("adapters", "采集器", _ADAPTERS, resolve(job, source), **kwargs)
    actual_key = str(getattr(adapter, "source_key", "") or "").strip()
    if key and actual_key != key:
        raise RouteError(
            "源身份对不上，不敢采集："
            f"sources.source_key={key!r}，采集器最终使用 {actual_key or '空值'!r}。"
            "核对 source_key 的租户段与 sources.tenant。"
        )
    return adapter
