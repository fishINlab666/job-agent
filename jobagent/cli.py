"""M5 交互层：CLI。后面在外面包一层 MCP server，逻辑复用这里的函数。"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import (
    ats,
    db,
    ingest,
    match,
    notifications,
    observation,
    official_truth,
    profile,
    queries,
    routing,
    scheduler,
)
from .targets import OBSERVATION_SOURCES
from .submitters.base import SubmissionPlan

app = typer.Typer(add_completion=False, help="校招 Agent")
console = Console()
_run_observation = observation.run
_install_observation_schedule = scheduler.install
_uninstall_observation_schedule = scheduler.uninstall
_observation_progress = observation.progress
_capture_official_candidates = official_truth.capture_candidates
_deliver_observation_notification = notifications.deliver_observation
_review_observation_day = official_truth.review_day
_accept_observation_day = observation.accept_day

# 这里原来有两张 {source_key: cls} 表。现在注册表移到 jobagent/routing.py，
# 键从「公司入口」换成「招聘系统」，登记动作在 adapters/ 和 submitters/ 的包
# 初始化里。原因见 docs/ATS_RESEARCH.md：同一个 ATS 上几十家公司的前端是同一套
# 应用，按公司注册等于把同一份逻辑抄几十遍。

FAM_ZH = {
    "tech": "技术", "product": "产品", "operations": "运营", "design": "设计",
    "marketing": "市场", "sales": "销售", "hr": "人力", "finance": "财务",
    "legal": "法务", "other": "其他",
}
RTYPE_ZH = {"campus": "应届", "intern": "实习", "social": "社招"}


APP_STATUS_ZH = {
    "reserved": "已预占", "prefilled": "填好待确认", "submitting": "提交中",
    "submitted": "已提交", "duplicate": "投过了", "unknown": "结果未知",
    "closed": "岗位已关", "failed": "历史失败", "blocked": "被拦",
    "abandoned": "已放弃",
}

# 漏斗的分档。**写死每个状态的归属，不许用「其他」兜底** —— 见
# docs/plans/012 §6：把 duplicate/closed 混进失败档，会让「代投不好用」
# 这个结论凭空多出两类本来正常的记录（duplicate 是「已经投过了」、
# closed 是「岗位关了」，两个都不是错误）。
# 顺序就是展示顺序，也是这条链路的真实先后。
APP_FUNNEL = (
    ("被拦", ("blocked",), "yellow"),
    ("进行中", ("reserved", "prefilled", "submitting"), "cyan"),
    ("已提交", ("submitted",), "green"),
    ("结果未知", ("unknown", "failed"), "red"),
    ("无需投递", ("duplicate", "closed"), "dim"),
    ("已放弃", ("abandoned",), "dim"),
)
APP_STATUSES = tuple(s for _, ss, _ in APP_FUNNEL for s in ss)
RECONCILABLE_APPLICATION_STATUSES = frozenset({
    "reserved", "prefilled", "unknown", "failed", "closed",
})

SOURCE_HEALTH_EVENT_KINDS = frozenset({
    "source_degraded", "source_sync_failed",
})
PROFILE_FILTERED_EVENT_KINDS = frozenset({
    "family_first_seen", "job_opened", "job_reopened", "job_updated",
})


def _find_job_or_exit(
    conn, external_id: str, source_key: str | None = None
) -> dict:
    """给写操作选定唯一岗位；重号时要求用户明确来源。"""
    try:
        job = queries.find_job(conn, external_id, source_key=source_key)
    except queries.AmbiguousJobError as exc:
        choices = "、".join(exc.source_keys)
        console.print(
            f"[red]岗位编号不唯一[/red] {external_id} 来自：{choices}"
        )
        console.print("[yellow]请加 --source 指定其中一个来源[/yellow]")
        raise typer.Exit(1)
    if job is None:
        where = f"（来源 {source_key}）" if source_key else ""
        console.print(f"[red]岗位不存在[/red] {external_id}{where}（先跑 sync）")
        raise typer.Exit(1)
    return job


def _fmt_cities(cities: list[str]) -> str:
    """diff 里的城市列表转成给人看的一行。

    空列表显示「未写」而不是「不限」：「不限」在本仓是一个**真实的城市值**
    （`normalize.CITY_WILDCARDS`），源站写「工作地点不限」时它会作为一个元素
    出现在 cities 里，`any_city_ok` 认它。拿「不限」去表示空列表，会让
    `["不限"]`（源站说哪都行）和 `[]`（我们没拿到）打印成同一句话。
    「未写」沿用 digest 表格里已有的写法（同文件 `cities = ... or "未写"`），
    这里不写行号：这个函数本身就把下面的行号推移过一次。
    """
    return "、".join(cities) if cities else "未写"


# ---------- unsure 分组与整列降级检测 ----------

DEGRADED_AT = 0.9  # 某列缺失率达到这个阈值，视为不可得
DIM_COLUMNS = {
    "grad_year": "grad_year",
    "job_family": "job_family",
    "cities": "cities",
    "recruit_type": "recruit_type",
}


def _missing_dims(job: dict) -> list[str]:
    """这条岗位缺哪几个可筛维度。看库里的列，不解析 `_why` 文案。"""
    out = []
    for dim, col in DIM_COLUMNS.items():
        v = job.get(col)
        if v is None or (isinstance(v, (list, tuple, dict)) and not v) or (
            isinstance(v, str) and v.strip() in ("", "[]", "null")
        ):
            out.append(dim)
    return out


def _degraded_dims(conn) -> dict[tuple[str, str], tuple[int, int]]:
    """哪些 (源, 维度) 已经整列不可得。只统计开放岗位。

    返回 {(source_key, dim): (null_count, total)} 其中 null_ratio >= DEGRADED_AT。
    """
    rows = conn.execute(
        "SELECT * FROM jobs WHERE closed_at IS NULL"
    ).fetchall()

    from collections import defaultdict
    stats: dict[tuple[str, str], tuple[int, int]] = defaultdict(lambda: (0, 0))

    for r in rows:
        job = dict(r)
        source_key = job["source_key"]
        missing = _missing_dims(job)
        for dim in DIM_COLUMNS:
            null_count, total = stats[(source_key, dim)]
            stats[(source_key, dim)] = (
                null_count + (1 if dim in missing else 0),
                total + 1,
            )

    return {
        k: v
        for k, v in stats.items()
        if v[1] > 0 and v[0] / v[1] >= DEGRADED_AT
    }


def _print_unsure(conn, unsure: list[dict], limit: int = 15) -> None:
    """整列缺失收成一行，其余按缺几维分组，只差一维的排前面。"""
    degraded = _degraded_dims(conn)
    degraded_sources = {src for (src, _) in degraded}

    # 整列降级的源单独一行
    from_degraded = [j for j in unsure if j["source_key"] in degraded_sources]
    rest = [j for j in unsure if j["source_key"] not in degraded_sources]

    if from_degraded:
        sources_list = sorted(set(j["source_key"] for j in from_degraded))
        console.print(
            f"  [dim]整列缺失 {len(from_degraded)} 条（来自 {', '.join(sources_list)}）[/dim]"
        )
        for src in sources_list:
            missing_dims_for_src = sorted(
                {dim for (s, dim) in degraded if s == src}
            )
            console.print(
                f"    [dim]{src}: {', '.join(missing_dims_for_src)} 不可得[/dim]"
            )

    # 按缺失维度数分组，少的排前
    by_missing_count: dict[int, list[dict]] = {}
    for j in rest:
        count = len(_missing_dims(j))
        by_missing_count.setdefault(count, []).append(j)

    shown_count = 0
    for count in sorted(by_missing_count.keys()):
        jobs = by_missing_count[count]
        if shown_count >= limit:
            remaining = sum(len(js) for c, js in by_missing_count.items() if c >= count)
            console.print(f"  [dim]…还有 {remaining} 条，见 jobs --matched --loose[/dim]")
            break

        for j in jobs[:limit - shown_count]:
            cities = "/".join(match.city_list(j)[:3]) or "未写"
            console.print(
                f"  ? {j['company']} · {j['title']} · {cities}"
                f"  [dim][{j['source_key']} / {j['external_id']}][/dim]"
                f"  [dim]{j['_why']}[/dim]"
            )
            console.print(f"    [dim]{j.get('apply_url') or '-'}[/dim]")
            shown_count += 1


@app.command()
def init() -> None:
    """建库建表。"""
    conn = db.connect()
    db.init(conn)
    console.print(f"[green]库已就绪[/green] {db.DB_PATH}")


@app.command(name="source-add")
def source_add(
    source_key: str = typer.Argument(..., help="源 key，如 feishu:nio:campus"),
    company: str = typer.Option(..., "--company", help="公司名，落到 jobs.company"),
    entry_url: str = typer.Option(..., "--entry-url", help="该租户的招聘站入口"),
    tenant: str = typer.Option(None, "--tenant", help="租户（默认从 source_key 第二段取）"),
    apply_limit: int = typer.Option(
        None, "--apply-limit",
        help="该公司最多接受投几个岗位。不确定就别填，留空=不限",
    ),
    notes: str = typer.Option("", "--notes", help="备注"),
) -> None:
    """登记一个多租户源，之后 `sync` 才看得见它。

    这条命令原来不存在，后果是飞书系四家（422 行采集 + 1305 行代投）对用户完全
    不可达：`sync --source all` 的源列表 = 自建系统 ∪ `sources` 表已有的行，
    而多租户的键（`feishu`）不是合法源；`sync --source feishu:nio` 也不行 ——
    `company` / `host` 只从 sources 行取，没那行就 `RouteError`。整条链路是通的，
    只差把行写进去，而唯一的写入口是手敲 SQL。

    **登记前先真造一次采集器**，不只是校验字段格式。造得出来才写库，理由是
    `FeishuAdapter.__init__` 里已经有一道 host↔tenant 核对（`entry_url` 抄错行、
    复制上一家忘改子域名），那道检查值钱：不核对的失败方式是静默的 —— 拿着这家
    的配置去打另一家的接口，把别人的岗位落在这家名下。让它在登记时炸，比在
    第一次 sync 时炸好，因为那时人还记得自己填了什么。

    自建源（`tencent_join`）不用登记，`sync --source all` 从注册表直接取。
    """
    conn = db.connect()
    db.init(conn)

    key = source_key.strip()
    system = key.split(":", 1)[0]
    if system not in routing.registered_adapters():
        known = "、".join(sorted(routing.registered_adapters()))
        console.print(f"[red]没有 {system} 的采集器[/red]，现在有：{known}")
        raise typer.Exit(1)
    if (v := ats.BY_KEY.get(system)) is not None and v.self_built:
        console.print(
            f"[yellow]{system} 是自建源，不用登记[/yellow] —— "
            f"`sync --source all` 从注册表直接取。"
        )
        raise typer.Exit(1)

    # 租户默认从键里取，而不是让人重复输一遍：键里那段**就是**判据（见
    # routing.portal_of）。显式给 --tenant 是为了北森/Moka 那种租户抠不出来、
    # 只能人工配的系统。
    who = (tenant or "").strip() or (key.split(":")[1] if ":" in key else "")
    if not who:
        console.print(
            f"[red]取不到租户[/red] {key} 只有一段。"
            f"多租户源的键至少两段（`feishu:nio`），或者显式给 --tenant。"
        )
        raise typer.Exit(1)

    row = {
        "source_key": key, "company": company.strip(),
        "system": system, "entry_url": entry_url.strip(), "tenant": who,
    }
    try:
        routing.get_adapter({"source_key": key}, row)
    except Exception as exc:
        # 原话往外传。这里的失败几乎都是「填错了哪一项」，而适配器的报错已经
        # 指名了是哪一项对不上 —— 换成类名等于把最有用的部分丢掉。
        console.print(f"[red]这行配置造不出采集器[/red]：{exc}")
        raise typer.Exit(1)

    existed = conn.execute(
        "SELECT company FROM sources WHERE source_key=?", (key,)
    ).fetchone()
    db.register_source(conn, key, row["company"], system, row["entry_url"],
                       notes, who, apply_limit)
    verb = "更新" if existed else "登记"
    quota = f" · 限投 {apply_limit}" if apply_limit is not None else ""
    console.print(
        f"[green]已{verb}[/green] {key} · {row['company']} · 租户 {who}{quota}\n"
        f"[dim]下一步：sync --source {key}[/dim]"
    )


@app.command()
def sync(
    source: str = typer.Option("all", help="源 key，或 all"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只跑不写库"),
) -> None:
    """跑一轮采集与增量判定。"""
    conn = db.connect()
    db.init(conn)

    if source == "all":
        # 待同步的是「源」，不是「系统」。自建系统 source_key 就等于 system，
        # 可以直接从注册表拿；多租户系统的键（feishu）不是一个合法源——
        # 光说 feishu 不说租户，同步谁？这类只能来自库里配好的 sources 行。
        self_built = {
            k for k in routing.registered_adapters()
            if (v := ats.BY_KEY.get(k)) is not None and v.self_built
        }
        rows = conn.execute("SELECT source_key FROM sources WHERE enabled=1").fetchall()
        keys = sorted({r["source_key"] for r in rows} | self_built)
    else:
        keys = [source]

    for key in keys:
        row = conn.execute(
            "SELECT * FROM sources WHERE source_key=?", (key,)
        ).fetchone()
        try:
            adapter = routing.get_adapter({"source_key": key}, dict(row) if row else None)
        except routing.RouteError as exc:
            console.print(f"[red]采不了[/red] {key}: {exc}")
            continue
        console.print(f"[cyan]同步 {key}[/cyan] ...")
        try:
            st = ingest.sync(conn, adapter, dry_run=dry_run)
        except Exception as exc:
            console.print(f"[red]失败[/red] {key}: {exc}")
            continue

        tag = "[dim](首轮，不发单条事件)[/dim]" if st["bootstrap"] else ""
        console.print(
            f"  抓取 {st['fetched']} · 新增 {st['opened']} · 变更 {st['updated']} "
            f"· 关闭 {st['closed']} {tag}"
        )
        # 判不出族的条数。不是调试信息，是给用户的一句实话：飞书上这个比例
        # 能到 48.5%，将近一半岗位按族筛不到。不说的话用户筛出 10 条会以为
        # 这家只有 10 条对得上。只在真有的时候打。
        unknown = st.get("family_unknown", 0)
        if unknown:
            pct = unknown / st["fetched"] if st["fetched"] else 0
            console.print(
                f"  [yellow]判不出岗位族 {unknown} 条（{pct:.0%}）[/yellow]"
                "—— 按族筛会漏掉这些"
            )
        # 指纹和列不同步的行数。这行是给维护者看的，不是日常信息：正常恒为 0，
        # 非 0 说明有人绕过 sync 直接改了列（repair_apply_url / refresh_grad_year
        # 那类命令按设计就会这样）。这些行本轮重算了指纹但**不发事件** ——
        # 不打出来的话，「diff 为空所以吞掉」和「压根没变化」在输出里长得一样，
        # 而这个 bug 第三次复发正是因为那个状态一直没人看见。见方案 016。
        desync = st.get("fingerprint_desync", 0)
        if desync:
            console.print(
                f"  [dim]指纹与列不同步 {desync} 条（已重算指纹，未发事件）[/dim]"
            )
        if st["guard_tripped"]:
            console.print(
                "  [yellow]关闭守卫触发[/yellow]：消失比例异常，本轮未关闭任何岗位，"
                "run 标为 partial"
            )
        for f in st["families_first_seen"]:
            fam, rt = f.split("/")
            console.print(
                f"  [bold magenta]★ 新岗位族开放[/bold magenta] "
                f"{FAM_ZH.get(fam, fam)}/{RTYPE_ZH.get(rt, rt)}"
            )


@app.command()
def observe(
    db_path: Path | None = typer.Option(None, "--db", help="观察数据库路径"),
    trigger: str = typer.Option("manual", help="触发来源：manual / scheduled"),
    slot: str = typer.Option("manual", help="计划时段：09:30 / 14:30 / 20:30"),
) -> None:
    """同步五家目标公司，并把这一轮留成可连续核对的观察记录。"""
    if trigger not in {"manual", "scheduled"}:
        raise typer.BadParameter("--trigger 只能是 manual 或 scheduled")
    if trigger == "scheduled" and slot not in observation.SCHEDULE_SLOTS:
        choices = " / ".join(observation.SCHEDULE_SLOTS)
        raise typer.BadParameter(f"定时观察只允许 {choices}")
    candidate_report = None
    notification_result = None
    try:
        with observation.exclusive_run(db_path or db.DB_PATH):
            conn = db.connect(db_path)
            db.init(conn)
            try:
                report = _run_observation(
                    conn,
                    specs=OBSERVATION_SOURCES,
                    trigger=trigger,
                    slot=slot,
                )
                if trigger == "scheduled":
                    candidate_report = _capture_official_candidates(
                        conn, report, OBSERVATION_SOURCES
                    )
                    notification_report = report
                    if candidate_report["status"] != "ok":
                        notification_report = {**report, "status": "partial"}
                    notification_result = _deliver_observation_notification(
                        conn, notification_report, slot=slot
                    )
            finally:
                conn.close()
    except (observation.AlreadyRunningError, observation.DuplicateObservationError) as exc:
        console.print(f"[yellow]{exc}，本次未重复执行。[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"[green]观察 #{report['id']}[/green] · {report['status']}"
    )
    if trigger == "scheduled" and not report.get("on_time", False):
        console.print(
            "[yellow]本轮启动晚于计划时段 60 分钟，只保留记录，"
            "不计入连续工作日验收。[/yellow]"
        )
    for result in report["results"]:
        if result["status"] == "failed":
            console.print(
                f"  [red]失败[/red] {result['company']}：{result['error']}"
            )
            continue
        baseline = " · 首轮基线" if result["bootstrap"] else ""
        console.print(
            f"  [cyan]{result['company']}[/cyan] · 抓取 {result['fetched']}"
            f" · 新增 {result['opened']} · 变更 {result['updated']}"
            f" · 关闭 {result['closed']} · {result['status']}{baseline}"
        )
    if candidate_report is not None:
        for result in candidate_report["results"]:
            if result["status"] != "captured":
                console.print(
                    f"  [red]官网候选失败[/red] {result['source_key']}：{result['error']}"
                )
        if candidate_report["status"] == "ok":
            console.print("[dim]官网候选已保存；仍需每日人工确认，不能自动代签。[/dim]")
    else:
        console.print("[dim]手工观察不生成官网验收候选。[/dim]")
    if notification_result is not None and notification_result["status"] == "failed":
        console.print(
            f"[red]本机通知失败[/red]：{notification_result['error']}"
        )
    if (
        report["status"] != "ok"
        or (candidate_report is not None and candidate_report["status"] != "ok")
        or (
            notification_result is not None
            and notification_result["status"] == "failed"
        )
    ):
        raise typer.Exit(1)


@app.command(name="observe-review")
def observe_review(
    observation_id: int = typer.Argument(..., help="观察轮次编号"),
    source_key: str = typer.Argument(..., help="数据源 key"),
    db_path: Path | None = typer.Option(None, "--db", help="观察数据库路径"),
    evidence_path: Path = typer.Option(
        ..., "--evidence", exists=True, dir_okay=False, help="官网岗位清单 JSON"
    ),
) -> None:
    """把同期官网逐项核对结果记回观察轮次。"""
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        official_url = evidence["official_url"]
        captured_at = evidence["captured_at"]
        reviewer = evidence["reviewer"]
        external_ids = evidence["external_ids"]
        verified_event_ids = evidence["verified_event_ids"]
        note = evidence["note"]
        if evidence["source_key"] != source_key:
            raise ValueError("证据 source_key 与命令参数不一致")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"官网证据格式不正确：{exc}") from exc
    conn = db.connect(db_path)
    db.init(conn)
    try:
        batch = conn.execute(
            "SELECT trigger FROM observation_batches WHERE id=?",
            (observation_id,),
        ).fetchone()
        if batch is not None and batch["trigger"] == "scheduled":
            console.print(
                "[red]定时观察不能逐公司确认。[/red]"
                "请使用 observation-review-day 一次核对当天 15 份证据。"
            )
            raise typer.Exit(1)
        observation.record_truth(
            conn,
            observation_id,
            source_key,
            official_url=official_url,
            captured_at=captured_at,
            reviewer=reviewer,
            official_job_ids=external_ids,
            verified_event_ids=verified_event_ids,
            note=note,
        )
        verdict = conn.execute(
            """SELECT truth_status FROM observation_sources
               WHERE observation_id=? AND source_key=?""",
            (observation_id, source_key),
        ).fetchone()["truth_status"]
    finally:
        conn.close()
    translated = "一致" if verdict == "verified" else "发现差异"
    console.print(f"[green]官网核对已记录[/green] · {translated}")


@app.command(name="observation-status")
def observation_status(
    db_path: Path | None = typer.Option(None, "--db", help="观察数据库路径"),
) -> None:
    """查看三工作日观察窗口还差什么。"""
    conn = db.connect(db_path)
    db.init(conn)
    try:
        state = _observation_progress(conn)
    finally:
        conn.close()
    dates = "、".join(state["qualified_dates"]) or "暂无"
    console.print(
        f"有效工作日：{state['qualified_workdays']} 天（{dates}）"
    )
    if state["status"] == "passed":
        console.print("[green]三工作日观察通过，且已捕获并核对真实变化。[/green]")
    elif state["status"] == "stability_only":
        console.print(
            "[yellow]已有 3 个有效工作日，但还没有捕获并核对真实变化；"
            "只能证明稳定，继续观察。[/yellow]"
        )
    else:
        console.print("[dim]观察窗口仍在累计。周末记录保留但不占工作日名额。[/dim]")


@app.command(name="observation-review-day")
def observation_review_day(
    observed_date: str = typer.Argument(..., help="要核对的工作日（YYYY-MM-DD）"),
    db_path: Path | None = typer.Option(None, "--db", help="观察数据库路径"),
    accept: bool = typer.Option(False, "--accept", help="明确确认并一次写入 15 份证据"),
    reviewer: str = typer.Option("", "--reviewer", help="确认人"),
    note: str = typer.Option("", "--note", help="确认说明"),
) -> None:
    """预览一天的官网清单；只有 --accept 才写最终验收证据。"""
    try:
        date.fromisoformat(observed_date)
    except ValueError as exc:
        raise typer.BadParameter("日期必须是 YYYY-MM-DD") from exc
    if accept and (not reviewer.strip() or not note.strip()):
        raise typer.BadParameter("--accept 必须同时提供 --reviewer 和 --note")

    path = db_path or db.DB_PATH
    conn = db.connect(path) if accept else db.connect_readonly(path)
    try:
        if accept:
            db.init(conn)
        review = _review_observation_day(conn, observed_date)
        for item in review["items"]:
            count = item.get("official_count", "?")
            digest = item.get("official_ids_sha256", "")[:12] or "未提供"
            console.print(
                f"  {item['slot']} · {item['company']} · "
                f"官网/系统 {count}/{item.get('system_count', '?')} · "
                f"清单摘要 {digest} · 变化 {item['change_count']} 条"
            )
            for event in item.get("events", []):
                label = event.get("title") or event.get("external_id") or "未知岗位"
                console.print(
                    f"    变化 #{event['id']} · {event['kind']} · {label}"
                )
        if not review["ready"]:
            for problem in review["problems"]:
                console.print(f"[red]未就绪[/red] {problem}")
            raise typer.Exit(1)
        if not accept:
            console.print(
                "[green]当天 15 份官网候选可以确认。[/green]"
                "当前只是预览，尚未写入最终验收。"
            )
            return
        result = _accept_observation_day(
            conn,
            observed_date,
            reviewer=reviewer.strip(),
            note=note.strip(),
        )
    finally:
        conn.close()
    console.print(
        f"[green]每日官网核对已确认[/green] · {result['accepted']} 份证据"
    )


@app.command(name="schedule-install")
def schedule_install(
    project_root: Path = typer.Option(db.ROOT, "--project-root"),
    python_executable: Path = typer.Option(Path(sys.executable), "--python"),
    db_path: Path = typer.Option(db.DB_PATH, "--db"),
    home: Path = typer.Option(Path.home(), "--home", hidden=True),
) -> None:
    """安装每天 09:30、14:30、20:30 的本机自动观察任务。"""
    slots = _install_observation_schedule(
        project_root=project_root.resolve(),
        # 虚拟环境里的 python 通常是指向基础解释器的 symlink。resolve() 会把
        # venv 身份抹掉，launchd 随后从基础环境启动，找不到已安装的 jobagent。
        python_executable=python_executable.absolute(),
        db_path=db_path.resolve(),
        home=home.resolve(),
    )
    console.print("[green]自动观察已安装[/green] " + " / ".join(slots))


@app.command(name="schedule-uninstall")
def schedule_uninstall(
    home: Path = typer.Option(Path.home(), "--home", hidden=True),
) -> None:
    """停止自动观察；历史记录和数据库不删除。"""
    _uninstall_observation_schedule(home=home.resolve())
    console.print("[green]自动观察已停止[/green]，历史记录已保留。")


@app.command()
def refresh_grad_year(
    source: str = typer.Option(..., help="源 key，例如 tencent_join"),
    apply: bool = typer.Option(False, "--apply", help="真写库。不带这个 flag 只打印"),
) -> None:
    """按适配器当前的推导规则重算存量岗位的届别。

    换季改了 `CURRENT_CAMPUS_YEAR` 之后要跑一次。`grad_year` 不在指纹里，
    单靠 sync 刷不动存量（会看到「改了代码、updated=0、库里没动」）。
    """
    conn = db.connect()
    db.init(conn)
    row = conn.execute("SELECT * FROM sources WHERE source_key=?", (source,)).fetchone()
    try:
        adapter = routing.get_adapter(
            {"source_key": source}, dict(row) if row else None
        )
    except routing.RouteError as exc:
        console.print(f"[red]认不出这个源[/red] {source}: {exc}")
        raise typer.Exit(1)

    try:
        st = ingest.refresh_grad_year(conn, adapter, apply=apply)
    except ingest.RefreshUnsupported as exc:
        console.print(f"[yellow]这个源不需要刷新[/yellow] {source}: {exc}")
        raise typer.Exit(0)

    if not st["changed"]:
        console.print(
            f"[green]届别已是最新[/green] {source} · 查了 {st['examined']} 行，无需改动"
        )
    else:
        verb = "已更新" if apply else "会更新"
        console.print(
            f"{verb} [bold]{st['changed']}[/bold] 行 · 查了 {st['examined']} 行"
            f"（{st['unchanged']} 行本来就对）"
        )
        # 报变化明细而不是只报一个总数：「441 行」看不出对不对，
        # 「'27'→'不限' 348 条」能让人当场判断这次刷新是不是自己想要的。
        for (old, new), n in sorted(st["transitions"].items(), key=lambda x: -x[1]):
            console.print(f"    {old!r} → {new!r}   {n} 条")
        if not apply:
            console.print("[dim]这是预演，没写库。加 --apply 才真写。[/dim]")

    # 这两条只在真发生时打。都是「该修但这次没修」，不说就等于瞒。
    if st["skipped_would_null"]:
        console.print(
            f"  [yellow]{st['skipped_would_null']} 行跳过[/yellow]"
            "：重算出来是空值，但库里有值 —— 不拿空值覆盖好值。"
            "源站大概改了招聘标签的字面量，去核对适配器的推导规则。"
        )
    if st["no_snapshot"]:
        console.print(
            f"  [yellow]{st['no_snapshot']} 行没有快照[/yellow]"
            "，重算不了（这些岗位入库时的原文没留下）。"
        )


@app.command()
def repair_apply_url(
    source_prefix: str = typer.Option(
        "feishu", help="源 key 前缀，默认 feishu（四个租户一起修）"
    ),
    apply: bool = typer.Option(False, "--apply", help="真写库。不带这个 flag 只打印"),
) -> None:
    """给存量飞书 apply_url 补上漏掉的 `/detail` 后缀。

    2026-08-10 实测四个租户：少 `/detail` 的链接全部渲染「您正在寻找的页面不存在」，
    库里 8594 条飞书链接因此全是死的。`apply_url` 的唯一用途是「点开就是官网
    那一页」拿去人工核对，链接死了整条筛选链的产出就没法核对。

    为什么不靠 sync 修：`apply_url` 在指纹里，走 sync 会造出 8594 条
    假 `job_updated` 事件（每条 diff 都是「我们修了个 bug」）。见 plan 010 §7。
    """
    conn = db.connect()
    db.init(conn)
    st = ingest.repair_apply_url(conn, source_prefix=source_prefix, apply=apply)

    if not st["changed"]:
        console.print(
            f"[green]链接形状已是最新[/green] {source_prefix}* · "
            f"查了 {st['examined']} 行，{st['already_ok']} 行本来就带 /detail"
        )
    else:
        verb = "已修" if apply else "会修"
        console.print(
            f"{verb} [bold]{st['changed']}[/bold] 行 · 查了 {st['examined']} 行"
            f"（{st['already_ok']} 行本来就对）"
        )
        if not apply:
            console.print("[dim]这是预演，没写库。加 --apply 才真写。[/dim]")

    # 「该修但这次没修」的，不说就等于瞒。
    if st["skipped_unknown_shape"]:
        console.print(
            f"  [yellow]{st['skipped_unknown_shape']} 行形状不认识[/yellow]"
            "：链接里没有 /position/ 段，没按套路拼 —— 保留原值。"
            "源站大概改版了，去核对适配器的 _position_url()。"
        )
    if st["skipped_empty"]:
        console.print(
            f"  [yellow]{st['skipped_empty']} 行 apply_url 是空的[/yellow]，没链接可修。"
        )


@app.command()
def jobs(
    family: str = typer.Option(None, help="按归一岗位族过滤"),
    city: str = typer.Option(None, help="按城市过滤"),
    recruit_type: str = typer.Option(None, help="campus / intern"),
    limit: int = typer.Option(30),
    matched: bool = typer.Option(False, "--matched", help="只看命中我画像的"),
    allow_missing: list[str] = typer.Option(
        None, "--allow-missing",
        help=f"某一维没写也算能看，可重复。可选：{'/'.join(match.MISSING_DIMS)}",
    ),
    loose: bool = typer.Option(
        False, "--loose", help="连信息不全的一起看（等于放开全部维度）"
    ),
) -> None:
    """看当前开放岗位。"""
    conn = db.connect()

    # --loose 是「全放开」的简写，保持老行为。两个都给时取并集。
    allowed = set(allow_missing or ())
    if loose:
        allowed |= set(match.MISSING_DIMS)

    # 查询和筛选都在 queries 里，这里只负责把它的报错翻成 CLI 的形式、把它的
    # 提醒打出来。**不许在这儿再放一份筛选逻辑** —— 两处都在的时候常见路径照样
    # 全绿，只有改了一边才会分叉，而分叉的表现是 CLI 和 MCP 对同一个问题给出
    # 不同答案。守这条归属的测试见 tests/test_queries.py。
    try:
        queries.validate_allow_missing(allowed)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    try:
        intent = match.load_intent() if matched else None
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]画像不可用[/red] {exc}")
        console.print("[yellow]提示[/yellow] 复制 profile.yaml.example 为 profile.yaml 后填写")
        raise typer.Exit(1) from exc

    try:
        rows, notes = queries.open_jobs(
            conn,
            family=family, city=city, recruit_type=recruit_type,
            matched=matched, allow_missing=allowed,
            intent=intent,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")

    total = len(rows)
    table = Table(title=f"开放岗位 {total} 条" + (f"（显示前 {limit}）" if total > limit else ""))
    table.add_column("公司", style="cyan", no_wrap=True)
    table.add_column("来源", style="dim", no_wrap=True)
    table.add_column("岗位编号", no_wrap=True)
    table.add_column("岗位")
    table.add_column("族", no_wrap=True)
    table.add_column("类型", no_wrap=True)
    table.add_column("城市")
    table.add_column("首次发现", style="dim", no_wrap=True)

    for r in rows[:limit]:
        cities = json.loads(r["cities"] or "[]") if isinstance(r["cities"], str) else (r["cities"] or [])
        title = r["title"]
        if r.get("_why"):
            title = f"{title} [yellow]?[/yellow]"
        table.add_row(
            r["company"], r["source_key"], r["external_id"], title,
            FAM_ZH.get(r["job_family"], r["job_family"] or "-"),
            RTYPE_ZH.get(r["recruit_type"], r["recruit_type"] or "-"),
            "/".join(cities[:4]) + ("…" if len(cities) > 4 else ""),
            (r["first_seen_at"] or "")[:10],
        )
    console.print(table)
    if any(r.get("_why") for r in rows[:limit]):
        console.print("[dim]带 [yellow]?[/yellow] 的是信息不全的岗位，没被排除也没被确认，自己看一眼：[/dim]")
        for r in rows[:limit]:
            if r.get("_why"):
                console.print(f"  [dim]{r['title']} — {r['_why']}[/dim]")


@app.command()
def digest(mark: bool = typer.Option(False, "--mark", help="标记为已推送")) -> None:
    """每日增量：只看还没推给我、且命中画像的事件。"""
    conn = db.connect()
    rows = conn.execute(
        """SELECT e.id, e.kind, e.source_key AS event_source_key,
                  e.company, e.payload, e.occurred_at, j.*
           FROM events e LEFT JOIN jobs j ON j.id = e.job_id
           WHERE e.notified_at IS NULL
           ORDER BY e.occurred_at DESC"""
    ).fetchall()

    # 没有事件时，答案只取决于是否采集过；不该为了打印空状态强制读取画像。
    if not rows:
        ever_synced = conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
        if ever_synced:
            console.print("[dim]没有新增。[/dim]")
        else:
            console.print(
                "[dim]库里还没有任何一次采集记录。先跑 [/dim]"
                "[cyan]sync[/cyan][dim]，再回来看 digest。[/dim]"
            )
        return

    needs_profile = any(
        row["kind"] in PROFILE_FILTERED_EVENT_KINDS for row in rows
    )
    profile_error: str | None = None
    try:
        intent = match.load_intent() if needs_profile else {}
    except (FileNotFoundError, ValueError) as exc:
        intent = {}
        profile_error = str(exc)

    shown: list[int] = []
    alerts: list[str] = []
    highlights: list[str] = []
    hits: list[dict] = []
    unsure: list[dict] = []
    changed: list[tuple[dict, dict]] = []

    for r in rows:
        d = dict(r)
        kind = d["kind"]
        payload = json.loads(d["payload"] or "{}")
        # 画像缺失时，源健康告警仍要显示；真正依赖画像的岗位事件留在队列里，
        # 不能标成已处理，否则用户补完画像后也看不到了。
        if profile_error and kind in PROFILE_FILTERED_EVENT_KINDS:
            continue
        # 所有检查过的事件都算已处理。不这么做，被过滤掉的事件会永久积压在
        # 待推送队列里，下次 digest 又全部重扫一遍。
        shown.append(d["id"])

        if kind == "source_degraded":
            label = d["company"] or d["event_source_key"] or "未知来源"
            alerts.append(
                f"⚠ {label} 数据源异常：{payload.get('disappeared')}/"
                f"{payload.get('live_before')} 个岗位本轮消失，已暂停关闭。"
                f"{payload.get('error') or ''}"
            )
        elif kind == "source_sync_failed":
            label = d["company"] or d["event_source_key"] or "未知来源"
            alerts.append(
                f"⛔ {label} 同步失败：{payload.get('error') or '原因未记录'}"
            )
        elif kind == "family_first_seen":
            fam = payload.get("job_family")
            if not intent.get("families") or fam in intent["families"]:
                highlights.append(
                    f"★ {d['company']} 新开放 "
                    f"{FAM_ZH.get(fam, fam)}/{RTYPE_ZH.get(payload.get('recruit_type'), '')} 岗位族"
                )
        elif kind == "batch_started":
            highlights.append(
                f"● {d['company']} 疑似批次启动，单轮新增 {payload.get('count')} 个岗位"
            )
        elif kind in ("job_opened", "job_reopened") and d.get("title"):
            v = match.classify(d, intent)
            if v.state == "hit":
                hits.append(d)
            elif v.state == "unknown":
                # 不能丢。源站没写届别/城市的岗位，恰恰包括「2026-2027年毕业」
                # 「全国」这种最该推的，静默过滤掉用户永远不知道它开过。
                unsure.append({**d, "_why": v.reason})
        elif kind == "job_updated" and d.get("title"):
            # 变更事件多半是招聘方改文案，绝大部分不值得打扰用户。
            # 只在两个条件同时成立时才提：岗位现在够得着画像，且变的是城市或岗位族。
            diff = payload.get("diff") or {}
            if match.classify(d, intent).worth_showing and (
                "job_family" in diff or "cities" in diff or "title" in diff
            ):
                changed.append((d, diff))

    if alerts:
        console.print()
        for alert in alerts:
            console.print(f"  [bold red]{alert}[/bold red]")

    if profile_error:
        console.print(
            f"[yellow]岗位提醒暂未处理：{profile_error}。补好画像后会继续显示。[/yellow]"
        )

    if highlights:
        console.print()
        for h in highlights:
            console.print(f"  [bold magenta]{h}[/bold magenta]")

    if changed:
        console.print()
        console.print("[yellow]岗位变更（影响你的画像）[/yellow]")
        for job, diff in changed:
            parts = []
            # 内层键是 from/to —— 生产端 ingest.sync() 建 diff 时写的就是这两个。
            # 原来这里读的是 old/new，真实负载上当场 KeyError，见方案 006 问题 0。
            # 改键名要两端一起改，库里 90 条存量负载全是 from/to。
            if "title" in diff:
                parts.append(f"标题: {diff['title']['from']} → {diff['title']['to']}")
            if "job_family" in diff:
                fam = diff["job_family"]
                # job_family 可空（schema.sql:57），取不到显示 "-"，别打印字面量 None
                from_fam = FAM_ZH.get(fam["from"], fam["from"] or "-")
                to_fam = FAM_ZH.get(fam["to"], fam["to"] or "-")
                parts.append(f"族: {from_fam} → {to_fam}")
            if "cities" in diff:
                # 负载里是 list（ingest 建 diff 时存的排序后列表），不是字符串 ——
                # 直接塞进 f-string 会打印成 ['北京'] → ['深圳']
                cty = diff["cities"]
                parts.append(
                    f"城市: {_fmt_cities(cty['from'])} → {_fmt_cities(cty['to'])}"
                )
            console.print(
                f"  • {job['title']} ({job['company']}) "
                f"[{job['source_key']} / {job['external_id']}] — {'; '.join(parts)}"
            )

    if hits:
        table = Table(title=f"新增岗位 {len(hits)} 条（已按画像筛选）")
        table.add_column("公司", style="cyan")
        table.add_column("来源", style="dim", no_wrap=True)
        table.add_column("岗位编号", no_wrap=True)
        table.add_column("岗位")
        table.add_column("族", no_wrap=True)
        table.add_column("城市")
        table.add_column("投递链接", style="dim")
        for d in sorted(hits, key=lambda x: match.score(x, intent), reverse=True):
            table.add_row(
                d["company"], d["source_key"], d["external_id"], d["title"],
                FAM_ZH.get(d["job_family"], d["job_family"] or "-"),
                "/".join(json.loads(d["cities"] or "[]")[:3]),
                d.get("apply_url") or "-",
            )
        console.print(table)

    if unsure:
        console.print()
        console.print(
            f"[yellow]信息不全 {len(unsure)} 条[/yellow]"
            "[dim]（源站没写清楚，没敢直接筛掉，你自己扫一眼）[/dim]"
        )
        _print_unsure(conn, unsure, limit=15)

    if (not profile_error and not alerts and not hits and not unsure
            and not highlights and not changed):
        # 「没有新增」和「压根没同步过」对人的下一步动作要求不同，得分开说。
        # 判据取 runs 表有没有行，不取 jobs —— sync 跑了但源站关站、抓到 0 条
        # 也是可能的，那种情况让人再跑一次 sync 是把人往错的方向指。
        ever_synced = conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
        if ever_synced:
            console.print("[dim]没有新增。[/dim]")
        else:
            console.print(
                "[dim]库里还没有任何一次采集记录。先跑 [/dim]"
                "[cyan]sync[/cyan][dim]，再回来看 digest。[/dim]"
            )

    if mark and shown:
        conn.executemany(
            "UPDATE events SET notified_at=? WHERE id=?",
            [(db.now(), i) for i in shown],
        )
        conn.commit()
        console.print(f"[dim]已标记 {len(shown)} 条事件为已推送[/dim]")


def _check_grad_year_staleness(conn) -> None:
    """检查届别常量是否过期（对比站点自我声明）。

    判据：抓取腾讯站点的 Project_CampusSubtitle，提取届别，和代码常量对比。
    粒度：source_key × recruit_type 两级（见 plan 008 §6）。
    """
    import re
    import httpx
    from jobagent.adapters import tencent_join

    console.print("[cyan]届别过期检查[/cyan]")

    # 1. 抓取站点声明的届别
    try:
        resp = httpx.get(
            "https://cdn.multilingualres.hr.tencent.com/campusjoin/V2Index_zh-cn.js",
            headers={
                "User-Agent": tencent_join.UA,
                "Referer": "https://join.qq.com/",
            },
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        match = re.search(r'"Project_CampusSubtitle":"(\d{4})应届生招聘"', resp.text)
        if not match:
            console.print("[yellow]警告[/yellow] 无法从站点提取届别声明")
            return
        site_year_full = match.group(1)  # "2026"
        site_year = site_year_full[-2:]   # "26"
    except Exception as exc:
        console.print(f"[yellow]警告[/yellow] 抓取站点声明失败: {exc}")
        return

    # 2. 对比代码常量
    code_year = tencent_join.CURRENT_CAMPUS_YEAR

    if site_year != code_year:
        console.print(
            f"[red]届别过期[/red] 腾讯站点声明 {site_year} 届，"
            f"代码常量 CURRENT_CAMPUS_YEAR='{code_year}'"
        )
        console.print(
            f"[yellow]建议[/yellow] 更新 jobagent/adapters/tencent_join.py:50 "
            f"后运行 refresh-grad-year"
        )
    else:
        # 3. 检查库中实际分布（按 source_key × recruit_type 分组）
        rows = conn.execute("""
            SELECT source_key, recruit_type, grad_year, COUNT(*) as n
            FROM jobs
            WHERE source_key = 'tencent_join'
              AND closed_at IS NULL
              AND recruit_type IN ('campus', 'intern')
            GROUP BY source_key, recruit_type, grad_year
            ORDER BY source_key, recruit_type, grad_year
        """).fetchall()

        has_stale = False
        for row in rows:
            rt = row["recruit_type"]
            gy = row["grad_year"]
            n = row["n"]

            # 校招应该是当届，实习应该是"不限"
            if rt == "campus" and gy != site_year:
                console.print(
                    f"[yellow]警告[/yellow] {rt} 有 {n} 条届别为 '{gy}'，"
                    f"期望 '{site_year}'"
                )
                has_stale = True
            elif rt == "intern" and gy not in ("不限", None):
                # 实习允许"不限"或None（未知），但不应该有具体届别
                console.print(
                    f"[dim]提示[/dim] {rt} 有 {n} 条届别为 '{gy}'（实习通常为'不限'）"
                )

        if not has_stale:
            console.print(f"[green]✓[/green] 届别正常（站点={site_year}，代码={code_year}）")


@app.command()
def status(
    check_grad_year: bool = typer.Option(False, "--check-grad-year", help="检查届别是否过期"),
) -> None:
    """看各源的健康度。"""
    conn = db.connect()
    table = Table(title="数据源状态")
    for c in ("源", "公司", "开放岗位", "最近同步", "状态", "抓取数"):
        table.add_column(c)
    for s in queries.source_health(conn):
        r = s["last_run"]
        style = {"ok": "green", "partial": "yellow", "failed": "red"}.get(
            r["status"] if r else "", "dim"
        )
        table.add_row(
            s["source_key"], s["company"], str(s["open_jobs"]),
            (r["started_at"] or "")[:16] if r else "-",
            f"[{style}]{r['status']}[/{style}]" if r else "-",
            str(r["fetched"]) if r else "-",
        )
    console.print(table)

    if check_grad_year:
        console.print()
        _check_grad_year_staleness(conn)


@app.command()
def apply(
    job_id: str = typer.Argument(..., help="岗位的 external_id"),
    source: str = typer.Option(None, help="岗位编号重号时指定来源"),
    profile_path: str = typer.Option("profile.yaml", help="用户画像文件"),
    headless: bool = typer.Option(False, help="无头模式。确认环节要看页面，默认关"),
    user_data_dir: str = typer.Option(None, help="浏览器用户数据目录（持久化登录态）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只填表看计划，不给确认机会"),
    again: bool = typer.Option(
        False, "--again", help="核对过源站后，显式重试已有终态记录的同一岗位"
    ),
) -> None:
    """代投单个岗位：先填好表停下，你确认了才提交。

    没有 --yes 这种开关，这是故意的。提交不可逆，对方系统里多一条记录就撤
    不回来，所以确认环节不提供跳过的口子。想批量投递也得一个一个看过去。
    """
    conn = db.connect()

    job = _find_job_or_exit(conn, job_id, source)
    src = job["source_key"]

    try:
        form = profile.load_profile(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("[yellow]提示[/yellow] 复制 profile.yaml.example 改一份")
        raise typer.Exit(1)

    src_row = conn.execute("SELECT * FROM sources WHERE source_key=?", (src,)).fetchone()
    lookup = dict(job)

    route = routing.resolve(lookup, dict(src_row) if src_row else None)
    try:
        submitter = routing.get_submitter(
            lookup, dict(src_row) if src_row else None,
            headless=headless, user_data_dir=user_data_dir,
        )
    except routing.RouteError as exc:
        console.print(f"[red]投不了[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[dim]路由 {route.key}（{route.basis}）[/dim]")

    # ---- 阶段零：防重 + 限投额度 + 占位 ----
    # 三件事必须在同一笔 SQLite 写事务里完成。否则两个进程都能在人工确认期间
    # 看见「还剩一个名额」，然后各自开浏览器，最终投出两份。
    try:
        reservation = db.reserve_application(
            conn,
            job_id=job["id"], source_key=src, external_id=job["external_id"],
            company=job["company"], allow_repeat=again,
        )
    except db.ApplicationInProgress as exc:
        console.print(
            f"[red]不投了[/red] 该岗位已有进行中的投递（{APP_STATUS_ZH.get(exc.status, exc.status)}）"
        )
        console.print("[dim]先去源站核对，不要同时打开第二次投递。[/dim]")
        raise typer.Exit(1) from exc
    except db.DuplicateApplication as exc:
        console.print(
            f"[red]不投了[/red] 该岗位已有投递记录（{APP_STATUS_ZH.get(exc.status, exc.status)}）"
        )
        console.print(
            "[dim]先用 applications 查看并去源站核对；确认确实需要重投时，"
            "显式加 --again。[/dim]"
        )
        raise typer.Exit(1) from exc
    except db.QuotaExceeded as exc:
        console.print(
            f"[red]不投了[/red] 已达 {job['company']} 的投递上限 {exc.limit}"
            f"（已用 {exc.used}）"
        )
        console.print(
            "[dim]名额花在哪了：[/dim][cyan]applications --company "
            f"{job['company']}[/cyan]"
        )
        console.print(
            "[dim]上限记错了就改：[/dim][cyan]source-add … --apply-limit N[/cyan]"
        )
        db.record_blocked(
            conn,
            job_id=job["id"], source_key=src, external_id=job["external_id"],
            company=job["company"],
            error=f"已达该公司投递上限 {exc.limit}（已用 {exc.used}）",
        )
        db.add_event(conn, "apply_blocked", source_key=src, company=job["company"],
                     job_id=job["id"],
                     payload={"blocker": "quota_exhausted",
                              "limit": exc.limit, "used": exc.used})
        conn.commit()
        raise typer.Exit(1) from exc
    if reservation.limit is not None:
        console.print(f"[dim]额度 {reservation.used}/{reservation.limit}[/dim]")

    # ---- 阶段一：填表，停在提交按钮前 ----
    console.print(f"[dim]启动浏览器，填表但不提交…[/dim]")
    plan = submitter.prepare(job, form)

    if plan.status == "blocked":
        console.print(f"[yellow]未能填表[/yellow] {plan.blocker}")
        if plan.screenshot_path:
            console.print(f"  截图: {plan.screenshot_path}")
        _record_blocked(conn, job, src, plan, app_id=reservation.app_id)
        raise typer.Exit(1)

    db.mark_application_prefilled(
        conn,
        reservation.app_id,
        confirm_token=plan.confirm_token,
        filled_fields=[f.for_storage() for f in plan.filled_fields],
        skipped_fields=[f.for_storage() for f in plan.skipped_fields],
        screenshot_path=plan.screenshot_path,
        warnings=plan.warnings,
    )
    app_id = reservation.app_id
    _render_plan(plan, job, form)

    # ---- 阶段二：人工确认 ----
    if dry_run:
        console.print("[dim]--dry-run：只看计划，不提交。浏览器关掉。[/dim]")
        submitter.discard(plan.confirm_token)
        db.complete_application(
            conn, app_id, expected_status="prefilled",
            status="abandoned", note="dry_run",
        )
        return

    if plan.missing_required:
        names = "、".join(f.label for f in plan.missing_required)
        console.print(f"[red]必填项还空着：{names}[/red]")
        console.print("[dim]照这样提交对方会退回。建议先补 profile.yaml。[/dim]")

    console.print()
    ok = typer.confirm("以上信息确认无误，提交给对方？", default=False)

    if not ok:
        console.print("[yellow]已放弃[/yellow] 没有提交，浏览器已关闭。")
        submitter.discard(plan.confirm_token)
        db.complete_application(
            conn, app_id, expected_status="prefilled",
            status="abandoned", note="用户放弃",
        )
        db.add_event(conn, "apply_abandoned", source_key=src,
                     company=job["company"], job_id=job["id"])
        conn.commit()
        return

    # ---- 阶段三：提交 ----
    console.print("[dim]提交中…[/dim]")
    db.transition_application(
        conn, app_id, expected_status="prefilled", status="submitting"
    )
    try:
        result = submitter.execute(plan.confirm_token)
    except Exception as exc:
        # execute 开始后无法可靠判断按钮是否已经生效。这里绝不能写 failed 并建议
        # 重试；保守记 unknown，让用户先去源站核对。
        error = f"{type(exc).__name__}: {exc}"
        db.complete_application(
            conn, app_id, expected_status="submitting",
            status="unknown", error=error,
            note="execute_exception_outcome_unknown",
        )
        db.add_event(
            conn, "apply_unknown", source_key=src, company=job["company"],
            job_id=job["id"], payload={"title": job["title"], "error": error},
        )
        conn.commit()
        console.print("[red]提交结果未确认[/red] 点击后连接中断或程序异常。")
        console.print("[yellow]先去源站核对，不要直接重投。[/yellow]")
        raise typer.Exit(1) from exc

    persisted_status = "unknown" if result.status == "failed" else result.status
    db.complete_application(
        conn, app_id,
        expected_status="submitting", status=persisted_status,
        submitted_at=result.submitted_at,
        error=result.error,
        screenshot_path=result.screenshot_path,
        filled_fields=result.filled_fields or None,
        skipped_fields=result.skipped_fields or None,
        note=result.note,
    )
    db.add_event(conn, f"apply_{persisted_status}", source_key=src,
                 company=job["company"], job_id=job["id"],
                 payload={"title": job["title"], "error": result.error})
    conn.commit()

    if result.success:
        console.print(f"[green]已提交[/green] {job['company']} · {job['title']}")
    elif result.status == "duplicate":
        console.print(f"[yellow]对方系统提示已投递过[/yellow] {job['company']}")
    elif result.status == "blocked":
        console.print(f"[red]已中止，没有提交[/red] {result.error}")
        console.print("[dim]页面内容和你确认时不一致，或者确认超时了。重跑一次。[/dim]")
    elif persisted_status == "unknown":
        console.print(f"[red]提交结果未确认[/red] {result.error or ''}")
        console.print("[yellow]先去源站核对，不要直接重投。[/yellow]")
    else:
        console.print(f"[red]提交未完成[/red] {result.error}")
    if result.screenshot_path:
        console.print(f"  截图: {result.screenshot_path}")
    if not result.success:
        raise typer.Exit(1)


@app.command()
def application_reconcile(
    attempt_id: int = typer.Argument(..., help="applications 列表里的记录 #"),
    confirmed_not_submitted: bool = typer.Option(
        False,
        "--confirmed-not-submitted",
        help="已在招聘官网确认这次没有形成投递记录",
    ),
) -> None:
    """人工核对源站后，释放一条悬挂或结果未知的投递占位。"""
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM applications WHERE id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        console.print(f"[red]找不到投递记录 #{attempt_id}[/red]")
        raise typer.Exit(1)
    attempt = dict(row)

    if not confirmed_not_submitted:
        console.print("[red]尚未释放[/red] 先去招聘官网核对是否已有这条投递。")
        console.print(
            "[dim]确认官网确实没有后，重新运行并显式加 "
            "--confirmed-not-submitted。[/dim]"
        )
        raise typer.Exit(1)

    status = attempt["status"]
    if status not in RECONCILABLE_APPLICATION_STATUSES:
        console.print(
            f"[red]不能释放[/red] 记录 #{attempt_id} 当前是 "
            f"{APP_STATUS_ZH.get(status, status)}。"
        )
        raise typer.Exit(1)

    console.print("[bold]即将释放投递占位[/bold]")
    console.print(f"  记录: #{attempt_id}")
    console.print(f"  公司: {attempt['company'] or '-'}")
    console.print(f"  岗位引用: {attempt['source_key']} / {attempt['external_id']}")
    console.print(f"  当前状态: {APP_STATUS_ZH.get(status, status)}")
    if not typer.confirm("我已在招聘官网确认没有这条投递，释放占位？", default=False):
        console.print("[yellow]未释放[/yellow]")
        return

    resolution_note = "用户在源站确认未形成投递后手动释放"
    if attempt["note"]:
        resolution_note = f"{attempt['note']}；{resolution_note}"
    try:
        db.transition_application(
            conn, attempt_id, expected_status=status, status="abandoned",
            error=attempt["error"], note=resolution_note,
        )
    except db.ApplicationStateError as exc:
        console.print(f"[red]未释放[/red] {exc}")
        raise typer.Exit(1) from exc
    db.add_event(
        conn, "apply_reconciled_not_submitted",
        source_key=attempt["source_key"], company=attempt["company"],
        job_id=attempt["job_id"],
        payload={"application_id": attempt_id, "previous_status": status},
    )
    conn.commit()
    console.print("[green]已释放[/green] 这次占位已记为放弃，可以重新开始投递。")


@app.command()
def applications(
    status: str = typer.Option(None, "--status", help=f"只看某个状态：{'/'.join(APP_STATUSES)}"),
    source: str = typer.Option(None, "--source", help="只看某个源"),
    company: str = typer.Option(None, "--company", help="只看某家公司（跨该公司的所有源）"),
    limit: int = typer.Option(30),
    funnel: bool = typer.Option(False, "--funnel", help="只看分档汇总，不列明细"),
) -> None:
    """看投递记录：投了什么、卡在哪、截图在哪。

    本命令只读，不提供任何改状态开关。悬挂或结果未知的记录只有在用户去源站
    核对后，才能另走 ``application-reconcile`` 的显式确认闸门；提交中的记录
    连该闸门也不能释放。
    """
    conn = db.connect()

    if status and status not in APP_STATUSES:
        raise typer.BadParameter(
            f"不认识的状态 {status!r}，可选：{'/'.join(APP_STATUSES)}"
        )

    # LEFT JOIN 不是 JOIN：job_id 指向的 jobs 行可能已经不在了（schema 自己写了
    # external_id 的用途是「jobs 行被重建也能追溯」）。用 INNER JOIN 的话这种
    # 孤儿行会静默消失，而投递记录消失比岗位消失严重得多 —— 它是不可撤销动作
    # 的唯一凭证。见 docs/plans/012 §4。
    #
    # 排序用 created_at 而不是 submitted_at：后者在 blocked/prefilled 行全是
    # NULL，拿它排序等于让这些行的顺序变成未定义。
    sql = """SELECT a.*, j.title AS job_title
             FROM applications a LEFT JOIN jobs j ON a.job_id = j.id"""
    where, params = [], []
    if status:
        where.append("a.status = ?")
        params.append(status)
    if source:
        where.append("a.source_key = ?")
        params.append(source)
    if company:
        # 按公司过滤而不是按源：限投额度是按公司算的（db.quota_state），一家公司
        # 可以有多个 source_key，用 --source 看不全「名额花在哪了」。
        where.append("a.company = ?")
        params.append(company)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.created_at DESC, a.id DESC"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        what = f"状态 {status} 的" if status else ""
        console.print(f"[dim]没有{what}投递记录。[/dim]")
        if not status and not source and not company:
            console.print("[dim]代投还没跑过，先看 `cli jobs --matched` 挑一个岗位。[/dim]")
        return

    # 表里出现了分档表没覆盖的状态时显式报出来，不静默归类 —— 静默归类
    # 就是下一次口径事故。这里用全表算，不受 --status/--source 过滤影响。
    unknown = {
        r["status"]
        for r in conn.execute("SELECT DISTINCT status FROM applications").fetchall()
    } - set(APP_STATUSES)
    if unknown:
        console.print(
            f"[red]警告：库里有分档表没认的状态 {sorted(unknown)}[/red]"
            f"[dim] —— 它们不在漏斗里，改 cli.APP_FUNNEL 之后再看这个数[/dim]"
        )

    if funnel:
        _render_app_funnel(rows)
        return

    # 标题必须带上过滤条件：`--status blocked` 打出「投递记录 14 条」时，
    # 截图/回滚屏幕里看不出这是筛过的，会被当成全表。
    total = len(rows)
    filters = []
    if status:
        filters.append(f"status={status}")
    if source:
        filters.append(f"source={source}")
    title = f"投递记录 {total} 条"
    if filters:
        title += f"（筛选：{'，'.join(filters)}）"
    if total > limit:
        title += f"（显示前 {limit}）"
    table = Table(title=title)
    # min_width 保 # 和状态两列：窄终端下 rich 会按比例压所有列，这两列一压
    # 就只剩「…」，而它们是这张表的定位信息（要照着 # 去找截图、照着状态判断
    # 该不该重投）。宁可挤岗位名。
    table.add_column("#", style="dim", no_wrap=True, min_width=2)
    table.add_column("公司", style="cyan", no_wrap=True, min_width=4)
    # 中日韩字符是双宽，光靠截断字数压不住列宽，要显式给 max_width，
    # 否则每行会被撑成三四行。
    # 中日韩字符是双宽，光靠截断字数压不住列宽。而且光给 max_width 不够：
    # rich 会先按空格断词再套宽度，所以必须同时 no_wrap，才是「一行截断」。
    table.add_column("岗位", max_width=24, no_wrap=True, overflow="ellipsis")
    table.add_column("状态", no_wrap=True, min_width=8)
    table.add_column("时间", style="dim", no_wrap=True, min_width=11)
    table.add_column("卡在哪 / 备注", no_wrap=True, overflow="ellipsis")

    style_of = {s: c for _, ss, c in APP_FUNNEL for s in ss}
    for r in rows[:limit]:
        st = r["status"]
        color = style_of.get(st, "red")
        # 岗位行没了就退化成 external_id 并标记，不留空白：空白会被读成
        # 「这条记录坏了」，而它其实是完好的记录 + 消失的岗位。
        title = r["job_title"] or "[dim]<岗位行已不在>[/dim]"
        title += f"\n[dim]{r['source_key']} / {r['external_id']}[/dim]"
        # blocked 的 error 是给人看的整段建议（最长 60+ 字），只取第一句：
        # 后面几句是「怎么办」，那属于 --funnel 的输出，不该挤在表格里。
        note = r["error"] or r["note"] or ""
        note = note.split("。")[0]
        # 两个 JSON 列在 blocked 行是 NULL（表单没见到过，无从填起），
        # 所以是 `or "[]"` 而不是直接 loads —— 直接 loads 会在这些行抛。
        filled = len(json.loads(r["filled_fields"] or "[]"))
        skipped = len(json.loads(r["skipped_fields"] or "[]"))
        if filled or skipped:
            note = f"填 {filled}/跳 {skipped}" + (f"；{note}" if note else "")
        table.add_row(
            str(r["id"]), r["company"] or "-", title,
            f"[{color}]{APP_STATUS_ZH.get(st, st)}[/{color}]",
            # 只到「月-日 时:分」：年份对投递记录没有区分度（都是本季），
            # 省下来的 6 列留给岗位名。完整时间戳在库里，需要就直接查。
            (r["created_at"] or "")[5:16].replace("T", " "), note,
        )
    console.print(table)

    console.print("[dim]岗位引用（apply 时使用 source_key / external_id）：[/dim]")
    for r in rows[:limit]:
        console.print(f"  [dim]#{r['id']} {r['source_key']} / {r['external_id']}[/dim]")

    shots = [r for r in rows[:limit] if r["screenshot_path"]]
    if shots:
        console.print("[dim]截图存证：[/dim]")
        for r in shots:
            console.print(f"  [dim]#{r['id']} {r['screenshot_path']}[/dim]")


def _render_app_funnel(rows: list[dict]) -> None:
    """分档汇总。**报条数，不报成功率** —— 见 docs/plans/012 §7。

    今天是 14 尝试 / 0 提交，成功率算出来是 0%，但那 14 条全部停在登录门，
    提交逻辑一次都没执行过。0% 的分母里装的全是「还没试到那一步」的记录，
    等于把「没试」洗成「失败」。`blocked` 是信息不全，不是不命中。
    """
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # 尝试数和岗位数必须分开报。实测 14 次尝试只覆盖 7 个岗位（腾讯一个岗位重试
    # 了 7 次），把 14 读成「14 个岗位试过了」就是翻倍高估覆盖面。
    jobs_n = len({r["external_id"] for r in rows})
    # expand 避免新增状态列稍宽后把标题从「3 次尝试」中间断行；这段标题是用户
    # 判断尝试数和岗位数的关键口径，不能被表格内容宽度挤碎。
    table = Table(
        title=f"投递漏斗（{len(rows)} 次尝试，{jobs_n} 个岗位）",
        expand=True,
    )
    table.add_column("阶段", no_wrap=True)
    table.add_column("条数", justify="right", no_wrap=True)
    for label, states, color in APP_FUNNEL:
        n = sum(counts.get(s, 0) for s in states)
        style = color if n else "dim"
        table.add_row(f"[{style}]{label}[/{style}]", f"[{style}]{n}[/{style}]")
    console.print(table)

    blocked_rows = [r for r in rows if r["status"] == "blocked"]
    if blocked_rows:
        console.print(f"\n[yellow]被拦的 {len(blocked_rows)} 条卡在哪：[/yellow]")
        _render_blocked_reasons(blocked_rows)

# 拦截原因的归类。**认的是 error 文案**，所以它和 011 的届别判据是同一类东西：
# 换个文案就静默失效。守卫写在下面 —— 认不出的原因**原样打印成独立一行**，
# 不进「其他」桶。文案变了的表现是列表里多出一行陌生原因，而不是某个数字悄悄
# 变小。见 docs/plans/012 §6 和 memory 里的 string-level-judgements-need-a-watchdog。
BLOCK_REASONS = (
    ("要登录（手机号+验证码，只能你本人做）", ("登录",)),
    ("找不到投递按钮（页面结构变了）", ("未找到申请按钮", "找不到")),
)


def _render_blocked_reasons(blocked_rows: list[dict]) -> None:
    buckets: dict[str, list[dict]] = {}
    for r in blocked_rows:
        err = r["error"] or ""
        for label, keys in BLOCK_REASONS:
            if any(k in err for k in keys):
                buckets.setdefault(label, []).append(r)
                break
        else:
            # 归不进已知原因就自己成一档，原文当档名（截断只为排版）。
            buckets.setdefault(f"[dim]未归类：[/dim]{err[:24] or '(error 为空)'}", []).append(r)

    # 带上「最近一次」的时间：原因是历史记录，不都还成立。实测那 4 条
    # 「找不到投递按钮」比同一岗位的「要登录」更早 —— 说明按钮问题当时已修掉，
    # 后来那次跑得更远。不写时间的话这两档看起来像并列的两个现存问题。
    for label, rs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        who = sorted({r["company"] or "-" for r in rs})
        jobs_n = len({r["external_id"] for r in rs})
        last = max((r["created_at"] or "") for r in rs)[5:16].replace("T", " ")
        console.print(
            f"  {len(rs)} 次 / {jobs_n} 个岗位  {label}"
            f"  [dim]({'、'.join(who)}；最近 {last})[/dim]"
        )

    # --user-data-dir 是这句话的重点：不带它登录态不落盘，这次登录白登。
    # headless 默认就是关的（cli.py 里 apply 的 headless 默认 False），不用加开关。
    if any("登录" in (r["error"] or "") for r in blocked_rows):
        console.print(
            "\n[dim]登录只能你本人做一次，且必须带 --user-data-dir，否则登录态不落盘：\n"
            "  uv run python -m jobagent.cli apply <external_id> --user-data-dir .browser[/dim]"
        )


def _render_plan(plan: SubmissionPlan, job: dict, form: "profile.FormProfile") -> None:
    """把「即将提交什么」摊开给用户看。

    这是整条代投链路里唯一的人工审查点，所以宁可啰嗦：链接、公司、岗位、
    每个字段填什么、值从画像哪一行来的。手机号邮箱这类按掩码显示——确认页
    经常要截图发人看，不该在这里泄一遍。
    """
    cities = ""
    if job.get("cities"):
        try:
            cities = "、".join(json.loads(job["cities"]))
        except (ValueError, TypeError):
            cities = str(job["cities"])

    console.print()
    console.print("[bold]即将投递[/bold]")
    console.print(f"  公司: {job['company']}")
    console.print(f"  岗位: {job['title']}")
    if job.get("department"):
        console.print(f"  部门: {job['department']}")
    if cities:
        console.print(f"  城市: {cities}")
    if job.get("recruit_type"):
        console.print(f"  类型: {RTYPE_ZH.get(job['recruit_type'], job['recruit_type'])}")
    console.print(f"  链接: {plan.apply_url or job.get('apply_url') or '-'}")
    console.print(f"  画像: {form.source_path}")

    table = Table(title="表单填写内容", title_justify="left", show_lines=False)
    table.add_column("页面字段")
    table.add_column("将填入")
    table.add_column("来源")
    table.add_column("状态")
    for f in plan.fields:
        if f.action == "skip":
            state = "[yellow]跳过[/yellow]"
        elif f.filled:
            state = "[green]已填[/green]"
        else:
            state = "[red]未填[/red]"
        label = f"{f.label}[red]*[/red]" if f.required else f.label
        table.add_row(label, f.display, f.source or "-", state)
    console.print(table)

    for f in plan.fields:
        if f.note:
            console.print(f"  [dim]{f.label}: {f.note}[/dim]")
    for warning in plan.warnings:
        console.print(f"  [bold yellow]注意：{warning}[/bold yellow]")
    if plan.screenshot_path:
        console.print(f"  [dim]填表后截图: {plan.screenshot_path}[/dim]")
    console.print(f"  [dim]确认有效期 {int((plan.expires_at - plan.created_at) / 60)} 分钟[/dim]")


@app.command()
def checkup(
    job_id: str = typer.Argument(..., help="拿哪个岗位的表单来体检（external_id）"),
    source: str = typer.Option(None, help="岗位编号重号时指定来源"),
    user_data_dir: str = typer.Option(None, help="浏览器用户数据目录（要有登录态）"),
) -> None:
    """核一遍投递表单的判据还认不认页面。**只读，不填不投。**

    为什么需要它：投递器里靠字符串认页面的常量有一打（中文字段名、CSS-modules
    类名前缀、勾选框旁的文案、下拉选项全称）。它们坏掉的方式全是静默的 ——
    命中 0 个，然后代投交出一张几乎空的表单加一句「填了 2 个字段」。

    判据写对了不算完，得有一条命令能回答「怎么知道它失效了」。这就是那条命令。
    改完选择器、或者隔一段时间没投过，跑一次。
    """
    conn = db.connect()
    job = _find_job_or_exit(conn, job_id, source)
    src = job["source_key"]

    src_row = conn.execute("SELECT * FROM sources WHERE source_key=?", (src,)).fetchone()
    lookup = dict(job)
    try:
        submitter = routing.get_submitter(
            lookup, dict(src_row) if src_row else None,
            headless=False, user_data_dir=user_data_dir,
        )
    except routing.RouteError as exc:
        console.print(f"[red]投不了[/red] {exc}")
        raise typer.Exit(1)

    if not hasattr(submitter, "checkup"):
        console.print(f"[yellow]{type(submitter).__name__} 还没有体检实现[/yellow]")
        raise typer.Exit(1)

    console.print("[dim]启动浏览器，走到表单但不填…[/dim]")
    rows = submitter.checkup(job)

    table = Table(title=f"判据体检 {job['company']}／{job['title']}")
    table.add_column("判据")
    table.add_column("")
    table.add_column("实际")
    for name, ok, note in rows:
        table.add_row(name, "[green]OK[/green]" if ok else "[red]坏了[/red]",
                      note or "")
    console.print(table)

    module = type(submitter).__module__.split(".")[-1]
    bad = [n for n, ok, _ in rows if not ok]
    if bad:
        console.print(f"\n[red]{len(bad)} 条判据失效[/red]：{'、'.join(bad)}")
        console.print(f"[dim]每条的后果见上面「实际」那一列。改 {module}.py 里对应的"
                      "常量，改完再跑一次这个命令。[/dim]")
        raise typer.Exit(1)

    # 全绿不等于「判据还认得页面」。有些判据（认『已关闭』『提交成功』这类只在
    # 异常页面上出现的文案）在一个健康页面上核不动 —— 词换成什么都是 0 命中。
    # 投递器把这层限制写在自己那一行的说明里，这里如实转述，不然「全部有效」
    # 会被读成「站点没改过文案」。
    caveats = [n for n, ok, note in rows if ok and "不代表" in note]
    console.print(f"\n[green]{len(rows)} 条判据全部有效[/green]")
    if caveats:
        console.print(f"[yellow]其中 {len(caveats)} 条只证明了判据自身没写坏，"
                      "证明不了站点还在用这些文案[/yellow]")
        console.print("[dim]这类判据只在异常页面上才触发（岗位已关闭、提交成功、"
                      "重复投递），拿一个正常岗位页核不出文案有没有换。[/dim]")


@app.command()
def health(
    source: str = typer.Option(None, help="只查一个源（默认全查）"),
    sample: int = typer.Option(
        5,
        min=1,
        max=20,
        help="每源抽几条（1–20）。选 5 是为区分度，不是覆盖率",
    ),
) -> None:
    """抽查 `apply_url` 还能打开。**只报告，不改库。**

    为什么需要它：2026-08-10 发现库里 8594 条飞书 `apply_url` 全是死链，
    少了 `/detail`，从采集第一天就坏，三个月没人发现 —— HTTP 回 200，
    只有渲染出来才是「您正在寻找的页面不存在」。

    两层检查，先便宜的：URL 形状（不联网，秒级），再真开浏览器抽样。
    形状层就报错的话不必往下打 —— 形状全错时抽样只是把同一个结论测 40 遍。
    """
    from . import health as hm

    conn = db.connect_readonly()
    problems = hm.check_shapes(conn)
    if problems:
        console.print("[red]URL 形状对不上登记值[/red]")
        for p in problems:
            console.print(f"  ⚠ {p}")
        console.print("\n[dim]先修适配器的链接拼装，别逐条改库。"
                      "形状层没过就不抽样了 —— 那只是把同一个结论测几十遍。[/dim]")
        conn.close()
        raise typer.Exit(1)
    console.print(f"[green]URL 形状全部对上登记值[/green]（{len(hm.EXPECTED_SHAPE)} 个源）")

    sources = [source] if source else sorted(hm.EXPECTED_SHAPE)
    skipped = [s for s in sources if s in hm.UNJUDGEABLE]
    todo = [s for s in sources if s not in hm.UNJUDGEABLE]

    plan: list[tuple[str, str, str, bool]] = []
    for src in todo:
        picked = hm.sample_urls(conn, src, sample)
        if not picked:
            console.print(f"[yellow]{src}：没有开放岗位可抽[/yellow]")
            continue
        plan.extend((s, eid, url, False) for s, eid, url in picked)
        # 对照组：形状对、id 不存在，必须判出 gone。判不出说明判据失效了。
        s0, eid0, url0 = picked[0]
        plan.append((s0, hm.SENTINEL_ID, hm.sentinel_url(url0, eid0), True))
    conn.close()

    if not plan:
        console.print("[red]一条都没抽到[/red]（源名写对了吗？）")
        raise typer.Exit(1)

    real = sum(1 for *_, is_sent in plan if not is_sent)
    # 拿轮询预算估，不是拿「平均单页耗时」估。健康页要等满预算才返回
    # （health.probe_one 说明了为什么），而抽样打的绝大多数就是健康页。
    # 早先按 6.9s/页 估，实测 24 页跑了 7 分钟、估的是 2.8 分钟 —— 差一倍多。
    est = len(plan) * hm.POLL_BUDGET_SEC / 60
    console.print(f"[dim]真开浏览器打 {len(plan)} 个页面"
                  f"（{real} 条抽样 + {len(plan) - real} 条对照），最多约 "
                  f"{est:.0f} 分钟（坏页秒返回，健康页等满 "
                  f"{hm.POLL_BUDGET_SEC:.0f}s）…[/dim]")

    results, sentinels = _run_probes(plan)
    _render_health(results, sentinels, skipped, sample)


def _run_probes(
    plan: list[tuple[str, str, str, bool]],
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """真打一轮。返回 `([(源, id, 判定)], {源: 对照组判定})`。

    复用一个 browser + 一个 page：实测 6.9s/页，其中启浏览器只占 0.3s，
    每条新开会把这个数字翻几倍。
    """
    from playwright.sync_api import sync_playwright
    from . import health as hm

    results: list[tuple[str, str, str]] = []
    sentinels: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for src, eid, url, is_sentinel in plan:
                verdict, _ = hm.probe_one(page, url)
                if is_sentinel:
                    sentinels[src] = verdict
                else:
                    results.append((src, eid, verdict))
        finally:
            browser.close()
    return results, sentinels


def _render_health(
    results: list[tuple[str, str, str]],
    sentinels: dict[str, str],
    skipped: list[str],
    sample: int,
) -> None:
    """按源出表。`unknown` 单独一栏 —— 不算活也不算死。"""
    from collections import Counter
    from . import health as hm

    by_source: dict[str, Counter] = {}
    for src, _eid, verdict in results:
        by_source.setdefault(src, Counter())[verdict] += 1

    table = Table(title="apply_url 巡检")
    table.add_column("源")
    table.add_column("抽", justify="right")
    table.add_column("活", justify="right")
    table.add_column("链接坏了", justify="right")
    table.add_column("岗位没了", justify="right")
    table.add_column("判不出", justify="right")
    for src in sorted(by_source):
        c = by_source[src]
        n = sum(c.values())
        broken = c["broken_route"]
        table.add_row(
            src, str(n),
            f"[green]{c['healthy']}[/green]" if c["healthy"] else "0",
            f"[red]{broken}[/red]" if broken else "0",
            str(c["gone"]),
            f"[yellow]{c['unknown'] + c['error']}[/yellow]"
            if c["unknown"] + c["error"] else "0",
        )
    console.print(table)

    # 对照组：判据自己的体检。它不占抽样配额，但它红了上面整张表都不可信。
    dead_judgement = [s for s, v in sentinels.items() if v != "gone"]
    if dead_judgement:
        console.print(f"\n[red]判据可能已失效[/red]：{'、'.join(dead_judgement)} 的对照组"
                      f"（形状对、id 不存在）没判出「岗位没了」")
        console.print("[dim]源站前端换了框架，空值不再渲染成 undefined？"
                      "这种失效是静默的 —— 上面那张表会把不存在的岗位报成「活」。"
                      f"改 health.py 的 GONE_MARK。[/dim]")

    for src, c in sorted(by_source.items()):
        n = sum(c.values())
        if c["broken_route"] == n and n > 0:
            console.print(f"\n[red]⚠ {src} 整源异常[/red]：抽中的 {n} 条全是链接坏了，"
                          f"不像零散下架")
            console.print("[dim]核对适配器的链接拼装，别逐条改库。[/dim]")

    if skipped:
        console.print(f"\n[yellow]没查：{'、'.join(skipped)}[/yellow]")
        console.print("[dim]腾讯 post.html?pid= 是列表页，真 pid / 假 pid / 负 pid "
                      "渲染出的正文逐字节相同，详情不登录不渲染 —— 没有判据可用。"
                      "这些源只被 URL 形状不变量守着。[/dim]")

    hit = {5: "21.6%", 10: "38.6%", 20: "62.6%"}.get(sample)
    console.print(f"\n[dim]口径：分母是**抽中的 {sample} 条**，不是源里全部的。"
                  f"「抽 {sample} 条全活」证不了这个源都活着 —— 抽样只能证伪。[/dim]")
    console.print("[dim]整源坏掉抽 1 条就能发现；零散下架（库里积 30 条未标）"
                  + (f"抽 {sample} 条只有 {hit} 命中率，" if hit else "的命中率远不到一半，")
                  + "所以这里**不宣称**「没发现下架岗位」。[/dim]")
    if any(c["gone"] for c in by_source.values()):
        console.print("[dim]「岗位没了」是源站的业务事件，不是我们的 bug。"
                      "巡检不自动标 closed_at —— 判据换季失效时，"
                      "错的方向会是「把活岗位标成关闭」，而这个动作没有回滚。[/dim]")


def _record_blocked(
    conn, job: dict, src: str, plan: SubmissionPlan, *, app_id: int | None = None
) -> None:
    """prepare 没走通，也要留痕：不然用户只看到一句报错，查不到发生过什么。"""
    if app_id is None:
        db.record_blocked(
            conn,
            job_id=job["id"], source_key=src, external_id=job["external_id"],
            company=job["company"], error=plan.blocker,
            screenshot_path=plan.screenshot_path,
        )
    else:
        db.transition_application(
            conn, app_id, expected_status="reserved", status="blocked",
            error=plan.blocker, screenshot_path=plan.screenshot_path,
        )
    db.add_event(conn, "apply_blocked", source_key=src, company=job["company"],
                 job_id=job["id"], payload={"blocker": plan.blocker})
    conn.commit()


if __name__ == "__main__":
    app()
