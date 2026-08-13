"""MCP server：只读层。

对话里能问库、不能动库、不能替你投。**这不是提示词约定，是形状上做不到**，
三条硬约束各落在一处代码上：

1. **注册表里没有写动词。** `prepare` / `execute` / `submit` / `apply` / `sync`
   一个都不注册。模型调不到不存在的工具 —— 这是主约束，其余两条是兜底。
   守它的是 `tests/test_mcp_server.py::test_no_write_verb_is_registered`：
   遍历真实注册表比对黑名单，将来谁手滑加一个 `@mcp.tool()` 的写动词，那条会红。
2. **连接是 `mode=ro`。** 见 `db.connect_readonly`。管的是「我在工具体里写错一句
   SQL」这种情况，SQLite 自己拒绝。
3. **只有 `intent` 过边界。** `profile.yaml` 里有姓名/手机/身份证。`_intent()`
   是这一层**唯一**读那个文件的地方，读完只取 `intent` 往下传，其余键当场丢掉。
   哨兵测试往 profile 里塞可识别的假身份值，调每个工具，断言哨兵串不出现在
   任何输出里。

为什么代投不在这儿：`execute()` 提交之后对方系统里那条记录撤不回来，闸门的价值
全在「人看过字段清单再点头」。把它做成工具就等于把闸门交给一个会自己决定要不要
调工具的东西 —— 所以 `prepare`/`execute` 留在 CLI，`SESSIONS` 这一层压根不碰。

跑法（stdio）：

    .venv/bin/python -m jobagent.mcp_server
"""
from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server import MCPServer

from . import db, match, queries, routing

mcp = MCPServer(
    name="job-agent",
    instructions=(
        "校招岗位库的只读查询。可以查岗位、问某条岗位为什么命中我的画像、"
        "看采集历史和数据源健康度。**这里没有投递工具**，代投只在命令行里做，"
        "因为提交不可逆、必须人工逐字段确认。"
    ),
)

#: 名词短语式的工具名，白名单单列。
#:
#: `job_changes` 没有动词打头（读作「岗位变动」，是个名词短语）。与其为了迁就规则
#: 把它改成 `list_job_changes` 那种啰嗦名字，不如把例外写下来 —— 例外**列出来**
#: 就还是可审的，规则被悄悄放松才不可审。
NOUN_PHRASE_TOOLS = frozenset({"job_changes"})

#: 工具名的**头一个词**只能是这几个。头动词决定这个工具干什么。
#:
#: 为什么管头词而不是「名字里不许出现写动词」：`list_sync_runs` 读的是采集历史，
#: 名字里有 sync 但它不采集 —— 按「出现即禁」会把它拦下来，逼人把守卫放松成
#: 子串匹配之类，那才是真的把门拆了。反过来正面清单漏的方向是「新工具名不在
#: 清单里就红」，需要人来答一句「它是只读的吗」，这个方向是安全的。
READ_VERBS = frozenset({"list", "get", "explain", "count", "search", "check"})

#: 这些词做**头动词**一定是写。和上面那条是一体两面，留着是为了报错时能点名
#: 「你注册的是个写工具」，而不是只说「头词不在清单里」。
WRITE_VERBS = frozenset({
    "prepare", "execute", "submit", "apply", "confirm", "discard",
    "sync", "init", "repair", "delete", "update", "insert", "write",
    "create", "remove", "set", "refresh", "add", "mark", "run",
})


#: `intent` 里允许过边界的键。见 `_intent()` 里为什么是白名单。
INTENT_KEYS = frozenset({
    "grad_years", "families", "cities", "recruit_types",
    "boost_keywords", "exclude_keywords",
})


def _conn() -> sqlite3.Connection:
    """每次调用开一个只读连接。

    不缓存成模块级单例：sqlite3 连接默认绑在创建它的线程上，而 MCP 的工具调用
    不保证同线程。开连接对本地 SQLite 是微秒级的事，省这个不值当。
    """
    return db.connect_readonly()


def _intent() -> dict:
    """从 `profile.yaml` 里只取 `intent`。

    **这一层唯一读 profile 的地方。** 那个文件里还有 identity（姓名/手机/身份证）、
    education、internships，都不往下传、不进任何返回值。写成一个函数而不是在每个
    工具里各读一次，是为了让「敏感数据在哪儿被读进来」只有一处可看、可测。

    档案不存在时返回 `{}`：匹配退化成「什么都不排除」，而不是让整个工具挂掉。

    只取 intent **还不够**，所以下面还按白名单挑了一遍键。原因是红队验出来的：
    把这里改成 `return match.load_profile()`（整份 profile 往下传），哨兵测试
    **照样全绿** —— 因为 `match.classify` 只读它认识的那几个键，身份数据传进去了
    但没被用到、于是没出现在返回值里。也就是说「只有 intent 过边界」这条当时
    靠的是「下游恰好不读」，不是这一层真的挡住了。下游哪天多读一个键，
    这条就静默失效，而失效的表现是身份数据出现在对话里。

    白名单比「排除 identity」稳：profile 将来加一个 `contacts` 之类的段落时，
    黑名单会默认放它过去，白名单会默认拦下来。
    """
    try:
        raw = match.load_profile().get("intent") or {}
    except FileNotFoundError:
        return {}
    return {k: v for k, v in raw.items() if k in INTENT_KEYS}


@mcp.tool()
def list_jobs(
    family: str | None = None,
    city: str | None = None,
    recruit_type: str | None = None,
    company: str | None = None,
    matched: bool = False,
    allow_missing: list[str] | None = None,
    limit: int = 30,
) -> dict:
    """查当前开放岗位。

    参数：
      family: 岗位族，等值匹配（tech / product / operations / design / other）。
        注意判不出族的岗位这一列是空的，**按任何族筛都查不到它**，包括 other。
      city / recruit_type（campus 应届 / intern 实习）/ company: 同样等值匹配。
      matched: 只看命中我画像的。
      allow_missing: 某一维没写也算能看，只在 matched 下生效。
        可选 job_family / recruit_type / grad_year / cities。
      limit: 最多返回几条。`total` 给的是筛完的**全量**条数，不受 limit 影响。

    返回里 `notes` 是给人看的提醒（例如 allow_missing 没生效），有内容就该转述。
    """
    rows, notes = queries.open_jobs(
        _conn(),
        family=family, city=city, recruit_type=recruit_type, company=company,
        matched=matched, allow_missing=allow_missing,
        intent=_intent() if matched else None,
    )
    return {
        "total": len(rows),
        "returned": min(len(rows), limit),
        "notes": notes,
        "jobs": [
            {
                "external_id": r["external_id"],
                "company": r["company"],
                "title": r["title"],
                "job_family": r["job_family"],
                "recruit_type": r["recruit_type"],
                "grad_year": r["grad_year"],
                "cities": r["cities"],
                "first_seen_at": r["first_seen_at"],
                "apply_url": r["apply_url"],
                # 信息不全的岗位带上原因，别让它看起来和确定命中的一样
                "why_unsure": r.get("_why"),
            }
            for r in rows[:limit]
        ],
    }


@mcp.tool()
def explain_match(external_id: str) -> dict:
    """一条岗位为什么命中／不命中我的画像。

    `state` 是三态，别当布尔看：
      hit     —— 确定命中
      miss    —— 确定不该推，`reason` 说明哪一条把它排除的
      unknown —— **信息不全，既没被排除也没被确认**，`missing` 列出缺哪几维。
                 这类要由人看一眼，不要当成不合格。
    """
    out = queries.explain_match(_conn(), external_id, _intent())
    if out is None:
        return {"found": False, "external_id": external_id,
                "hint": "库里没这条。external_id 要用 list_jobs 返回的那个值。"}
    return {"found": True, **out}


@mcp.tool()
def list_sources() -> dict:
    """每个数据源：开放岗位数、最近一次采集、投递配额。

    `last_run` 为 null = **这个源一次都没跑过**（该去 sync），
    和「跑过但失败了」不是一回事（那时 `last_run.status` 是 failed，该去查错）。

    `apply_limit` / `apply_remaining` 为 null 表示没设上限，不是 0。
    配额按公司算，同一家公司多行源时用量是合起来的。
    """
    return {"sources": queries.source_health(_conn())}


@mcp.tool()
def list_sync_runs(source_key: str | None = None, limit: int = 20) -> dict:
    """采集批次历史，最近的在前。

    （名字以 list_ 开头是刻意的：动词打头才看得出它是读还是写。原先叫
    `sync_history` —— 读的东西，名字却以写动词开头，守卫测试当场把它拦下来了。
    这个歧义留着的代价是模型看名字判断不了它会不会触发采集。）

    `finished_at` 为 null = 这一轮没收尾（进程被杀，或正在跑）。这条痕迹是故意
    留着的，不要当成数据缺失。
    """
    return {"runs": queries.sync_runs(_conn(), source_key=source_key, limit=limit)}


#: `events` 表里**采集侧**的事件种类。这一层只交出这些。
#:
#: 为什么是白名单不是黑名单：同一张表里还躺着代投侧的事件（`apply_blocked` 等），
#: 而它们的 payload 里有 `screenshots/` 路径 —— 那些截图是填好的表单，画面上有
#: 姓名手机身份证。更要紧的是代投侧的 kind 是 `f"apply_{result.status}"` 拼出来的，
#: **grep 数不出全集**（`cli.py:877`），将来多一个状态就多一个漏出去的 kind。
#: 黑名单在这种形状下一定会漏。
#:
#: 【这张表原本只有 4 个，漏了后面 3 个，见 019】漏掉的是 `job_reopened` /
#: `family_first_seen` / `batch_started`，库里当时正丢着 8 条事件。原来的注释在这里
#: 写着「白名单漏的方向是『少给一类事件』，看得见」—— 那句话的前提是文档没有反向
#: 承诺，而底下 `job_changes` 的 docstring 恰好写着「省略则全要」，漏掉就从「看得见」
#: 变成了「读成没发生过」。所以这次除了补全，还把「排除了什么」做进返回值
#: （`EXCLUDED_KINDS`），不再只靠注释。
#:
#: 判据是**发射点级**，不是名字级：`ingest.py` 发的都给，`cli.py` 发的都不给。
#: 名字级判据（「叫 `job_*` 的就给」）解释不了 `source_bootstrapped`，
#: 「有没有 `job_id`」也解释不了它 —— 正是这两个判据说不清的地方漏了 3 个。
#: `tests/test_mcp_server.py` 的 `test_every_ingest_kind_is_whitelisted` 拿
#: `ingest.py` 的 AST 当锚点守着这张表，能查出「少了一个」。
JOB_EVENT_KINDS = frozenset({
    "job_opened", "job_closed", "job_updated", "source_bootstrapped",
    "job_reopened", "family_first_seen", "batch_started",
})

#: 这一层**永远**排除的事件种类，随 `job_changes` 的返回值一起交出去。
#:
#: 为什么是个非空常量而不是「算出来的差集」：差集会在白名单补全之后变成空列表，
#: 而空列表会被读成「什么都没排除」—— 那正是这次要修掉的误读。代投侧一条不给
#: 是这一层的固定边界，不是某次快照的结果，所以它写成常量。
EXCLUDED_KINDS = ("apply_*",)


@mcp.tool()
def job_changes(
    kind: str | None = None, since: str | None = None, limit: int = 50
) -> dict:
    """岗位变动事件：新开、关闭、改动、复活、某族首现、批次启动、某个源首次接入。

    kind: job_opened / job_closed / job_updated / job_reopened / family_first_seen
    / batch_started / source_bootstrapped。**省略则给全部采集侧事件 —— 不是这张表
    的全部。** 返回值里的 `excluded_kinds` 写明了差在哪。
    since: ISO 时间字符串，只看这之后的。

    **只有采集侧的事件。** 投递记录不在这一层 —— 代投全程在命令行里做，
    问投了什么请去看 `jobagent applications`。所以 `events` 为空只说明
    「这段时间没有岗位变动」，推不出「没有投递」。

    某条事件带 `payload_raw` 说明它的 payload 存坏了解不开 —— 那是数据问题，
    不是「这次没有变动」。
    """
    if kind is not None and kind not in JOB_EVENT_KINDS:
        raise ValueError(
            f"不认识的事件种类 {kind!r}，可选：{'/'.join(sorted(JOB_EVENT_KINDS))}"
        )

    conn = _conn()
    kinds = [kind] if kind else sorted(JOB_EVENT_KINDS)
    events: list[dict] = []
    for k in kinds:
        events += queries.job_changes(conn, kind=k, since=since, limit=limit)
    # 各 kind 分别取了 limit 条，合起来要重新排序再截断，否则「最近 N 条」
    # 会变成「每种最近 N 条拼在一起」——条数对，但不是最近的那些。
    events.sort(key=lambda e: (e["occurred_at"] or "", e["id"]), reverse=True)
    # `excluded_kinds` 每次都带上，哪怕调用方指定了单个 kind：它说的是
    # 「这一层永远不给什么」，不是「这一次筛掉了什么」。省略它等于让调用方
    # 把一份被裁过的结果当成全集 —— 019 修的就是这个。
    return {"events": events[:limit], "excluded_kinds": list(EXCLUDED_KINDS)}


@mcp.tool()
def check_form_selectors(
    external_id: str,
    user_data_dir: str,
    source: str | None = None,
) -> dict:
    """核一遍投递表单的判据还认不认对方页面。**只读，不填不提交。**

    ⚠️ **这个工具会启一个真浏览器访问对方招聘站，一次几十秒。** 不要连着反复调，
    也不要为了「看看」而调 —— 改完选择器、或者隔一段时间没投过，才跑一次。
    它是这一层唯一对外发请求的工具，其余五个都只读本地库。

    参数：
      external_id: 拿哪个岗位的表单来体检。
      user_data_dir: 浏览器用户数据目录，**必填**。里面是招聘站的登录态。
        拿不到登录态时会走到登录墙，那时返回一条 blocker 说明走不到表单
        （不是一片假红），`reached_form` 为 false。
      source: 指定源，默认从库里推断。

    返回里 `all_valid` 为 true **不等于**「站点没改过文案」：有些判据只在异常页面
    上才触发（岗位已关闭、提交成功、重复投递），拿一个正常岗位页核不动它们 ——
    那几条在 `unprovable` 里列出来，转述结论时要一起说，别只说「全部有效」。
    """
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM jobs WHERE external_id=?", (external_id,)
    ).fetchone()
    if not row:
        return {"found": False, "external_id": external_id,
                "hint": "库里没这条。external_id 要用 list_jobs 返回的那个值。"}

    job = dict(row)
    src = source or job["source_key"]
    src_row = conn.execute(
        "SELECT * FROM sources WHERE source_key=?", (src,)
    ).fetchone()

    lookup = dict(job)
    if source:
        lookup.pop("apply_system", None)
        lookup["source_key"] = src

    try:
        submitter = routing.get_submitter(
            lookup, dict(src_row) if src_row else None,
            # headless 默认 True，和 CLI 相反。CLI 那边是 False 因为确认环节要人
            # 看着页面；这里没人看着屏幕，弹一个看不见的窗口只是白占资源。
            headless=True, user_data_dir=user_data_dir,
        )
    except routing.RouteError as exc:
        return {"found": True, "external_id": external_id,
                "supported": False, "reason": str(exc)}

    if not hasattr(submitter, "checkup"):
        return {"found": True, "external_id": external_id, "supported": False,
                "reason": f"{type(submitter).__name__} 还没有体检实现"}

    rows = submitter.checkup(job)
    checks = [{"name": n, "ok": bool(ok), "detail": note or ""}
              for n, ok, note in rows]
    broken = [c["name"] for c in checks if not c["ok"]]
    # 「只证明了判据自身没写坏」那类，投递器在自己那一行的说明里写了「不代表」。
    # 如实转述，不然 all_valid=true 会被读成「站点没改过文案」。
    unprovable = [c["name"] for c in checks
                  if c["ok"] and "不代表" in c["detail"]]

    return {
        "found": True,
        "external_id": external_id,
        "company": job["company"],
        "title": job["title"],
        "supported": True,
        # 走不到表单时 checkup 只返回一行 blocker（不是一片假红），
        # 所以「就一条且是红的」就是没登录那种情况
        "reached_form": not (len(checks) == 1 and broken),
        "all_valid": not broken,
        "broken": broken,
        "unprovable": unprovable,
        "checks": checks,
        "submitter_module": type(submitter).__module__.split(".")[-1],
    }


def main() -> None:
    """stdio 传输。不开 HTTP —— 一个本地库不需要监听端口，端口就是攻击面。"""
    mcp.run()


if __name__ == "__main__":
    main()
