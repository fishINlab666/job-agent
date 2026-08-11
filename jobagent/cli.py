"""M5 交互层：CLI。后面在外面包一层 MCP server，逻辑复用这里的函数。"""
from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from . import ats, db, ingest, match, profile, routing
from .submitters.base import SubmissionPlan

app = typer.Typer(add_completion=False, help="校招 Agent")
console = Console()

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
    "prefilled": "填好待确认", "submitted": "已提交", "duplicate": "投过了",
    "closed": "岗位已关", "failed": "失败", "blocked": "被拦", "abandoned": "已放弃",
}

# 漏斗的分档。**写死每个状态的归属，不许用「其他」兜底** —— 见
# docs/plans/012 §6：把 duplicate/closed 混进失败档，会让「代投不好用」
# 这个结论凭空多出两类本来正常的记录（duplicate 是「已经投过了」、
# closed 是「岗位关了」，两个都不是错误）。
# 顺序就是展示顺序，也是这条链路的真实先后。
APP_FUNNEL = (
    ("被拦", ("blocked",), "yellow"),
    ("填好待确认", ("prefilled",), "cyan"),
    ("已提交", ("submitted",), "green"),
    ("失败", ("failed",), "red"),
    ("无需投递", ("duplicate", "closed"), "dim"),
    ("已放弃", ("abandoned",), "dim"),
)
APP_STATUSES = tuple(s for _, ss, _ in APP_FUNNEL for s in ss)


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
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM jobs WHERE closed_at IS NULL ORDER BY first_seen_at DESC"
        ).fetchall()
    ]

    # --loose 是「全放开」的简写，保持老行为。两个都给时取并集。
    allowed = set(allow_missing or ())
    if bad := allowed - set(match.MISSING_DIMS):
        raise typer.BadParameter(
            f"不认识的维度 {sorted(bad)}，可选：{'/'.join(match.MISSING_DIMS)}"
        )
    if loose:
        allowed |= set(match.MISSING_DIMS)
    if allowed and not matched:
        console.print("[yellow]--allow-missing / --loose 只在 --matched 下生效，已忽略[/yellow]")

    if matched:
        rows = match.filter_jobs(
            rows, match.load_profile().get("intent") or {}, allow_missing=allowed
        )
    if family:
        rows = [r for r in rows if r["job_family"] == family]
    if recruit_type:
        rows = [r for r in rows if r["recruit_type"] == recruit_type]
    if city:
        rows = [r for r in rows if city in json.loads(r["cities"] or "[]")]

    total = len(rows)
    table = Table(title=f"开放岗位 {total} 条" + (f"（显示前 {limit}）" if total > limit else ""))
    table.add_column("公司", style="cyan", no_wrap=True)
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
            r["company"], title,
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
    intent = match.load_profile().get("intent") or {}
    rows = conn.execute(
        """SELECT e.id, e.kind, e.company, e.payload, e.occurred_at, j.*
           FROM events e LEFT JOIN jobs j ON j.id = e.job_id
           WHERE e.notified_at IS NULL
           ORDER BY e.occurred_at DESC"""
    ).fetchall()

    shown: list[int] = []
    highlights: list[str] = []
    hits: list[dict] = []
    unsure: list[dict] = []
    changed: list[tuple[dict, dict]] = []

    for r in rows:
        d = dict(r)
        kind = d["kind"]
        payload = json.loads(d["payload"] or "{}")
        # 所有检查过的事件都算已处理。不这么做，被过滤掉的事件会永久积压在
        # 待推送队列里，下次 digest 又全部重扫一遍。
        shown.append(d["id"])

        if kind == "family_first_seen":
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
            console.print(f"  • {job['title']} ({job['company']}) — {'; '.join(parts)}")

    if hits:
        table = Table(title=f"新增岗位 {len(hits)} 条（已按画像筛选）")
        table.add_column("公司", style="cyan")
        table.add_column("岗位")
        table.add_column("族", no_wrap=True)
        table.add_column("城市")
        table.add_column("投递链接", style="dim")
        for d in sorted(hits, key=lambda x: match.score(x, intent), reverse=True):
            table.add_row(
                d["company"], d["title"],
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

    if not hits and not unsure and not highlights and not changed:
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
    for s in conn.execute("SELECT * FROM sources ORDER BY source_key").fetchall():
        n = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE source_key=? AND closed_at IS NULL",
            (s["source_key"],),
        ).fetchone()["n"]
        r = conn.execute(
            """SELECT started_at, status, fetched FROM runs
               WHERE source_key=? ORDER BY id DESC LIMIT 1""",
            (s["source_key"],),
        ).fetchone()
        style = {"ok": "green", "partial": "yellow", "failed": "red"}.get(
            r["status"] if r else "", "dim"
        )
        table.add_row(
            s["source_key"], s["company"], str(n),
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
    source: str = typer.Option(None, help="指定源（默认从 jobs 表推断）"),
    profile_path: str = typer.Option("profile.yaml", help="用户画像文件"),
    headless: bool = typer.Option(False, help="无头模式。确认环节要看页面，默认关"),
    user_data_dir: str = typer.Option(None, help="浏览器用户数据目录（持久化登录态）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只填表看计划，不给确认机会"),
) -> None:
    """代投单个岗位：先填好表停下，你确认了才提交。

    没有 --yes 这种开关，这是故意的。提交不可逆，对方系统里多一条记录就撤
    不回来，所以确认环节不提供跳过的口子。想批量投递也得一个一个看过去。
    """
    conn = db.connect()

    row = conn.execute("SELECT * FROM jobs WHERE external_id=?", (job_id,)).fetchone()
    if not row:
        console.print(f"[red]岗位不存在[/red] {job_id}（先跑 sync）")
        raise typer.Exit(1)
    job = dict(row)
    src = source or job["source_key"]

    try:
        form = profile.load_profile(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("[yellow]提示[/yellow] 复制 profile.yaml.example 改一份")
        raise typer.Exit(1)

    src_row = conn.execute("SELECT * FROM sources WHERE source_key=?", (src,)).fetchone()
    lookup = dict(job)
    if source:
        # 显式指定了 --source 就以它为准：库里的 apply_system 会赢过 sources.system，
        # 不清掉的话「我指定了源，它却走了另一个投递器」——排查起来看不出为什么。
        lookup.pop("apply_system", None)
        lookup["source_key"] = src

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

    # ---- 阶段零：限投额度 ----
    # 放在开浏览器之前：拦下来的话不必启动 playwright，也不会在源站留下访问痕迹。
    # 两阶段闸门保的是「这一次是你确认过的」，保不了「这是第几次」，这里补那一半。
    used, limit = db.quota_state(conn, job["company"])
    if limit is not None and used >= limit:
        console.print(
            f"[red]不投了[/red] 已达 {job['company']} 的投递上限 {limit}"
            f"（已用 {used}）"
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
            error=f"已达该公司投递上限 {limit}（已用 {used}）",
        )
        db.add_event(conn, "apply_blocked", source_key=src, company=job["company"],
                     job_id=job["id"],
                     payload={"blocker": "quota_exhausted",
                              "limit": limit, "used": used})
        conn.commit()
        raise typer.Exit(1)
    if limit is not None:
        console.print(f"[dim]额度 {used}/{limit}[/dim]")

    # ---- 阶段一：填表，停在提交按钮前 ----
    console.print(f"[dim]启动浏览器，填表但不提交…[/dim]")
    plan = submitter.prepare(job, form)

    if plan.status == "blocked":
        console.print(f"[yellow]未能填表[/yellow] {plan.blocker}")
        if plan.screenshot_path:
            console.print(f"  截图: {plan.screenshot_path}")
        _record_blocked(conn, job, src, plan)
        raise typer.Exit(1)

    _render_plan(plan, job, form)

    app_id = db.record_prefill(
        conn,
        job_id=job["id"], source_key=src, external_id=job["external_id"],
        company=job["company"], confirm_token=plan.confirm_token,
        filled_fields=[f.for_storage() for f in plan.filled_fields],
        skipped_fields=[f.for_storage() for f in plan.skipped_fields],
        screenshot_path=plan.screenshot_path,
    )

    # ---- 阶段二：人工确认 ----
    if dry_run:
        console.print("[dim]--dry-run：只看计划，不提交。浏览器关掉。[/dim]")
        submitter.discard(plan.confirm_token)
        db.finalize_application(conn, app_id, status="abandoned", note="dry_run")
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
        db.finalize_application(conn, app_id, status="abandoned", note="用户放弃")
        db.add_event(conn, "apply_abandoned", source_key=src,
                     company=job["company"], job_id=job["id"])
        conn.commit()
        return

    # ---- 阶段三：提交 ----
    console.print("[dim]提交中…[/dim]")
    result = submitter.execute(plan.confirm_token)

    db.finalize_application(
        conn, app_id,
        status=result.status,
        submitted_at=result.submitted_at,
        error=result.error,
        screenshot_path=result.screenshot_path,
        filled_fields=result.filled_fields or None,
        skipped_fields=result.skipped_fields or None,
        note=result.note,
    )
    db.add_event(conn, f"apply_{result.status}", source_key=src,
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
    else:
        console.print(f"[red]提交失败[/red] {result.error}")
    if result.screenshot_path:
        console.print(f"  截图: {result.screenshot_path}")
    if not result.success:
        raise typer.Exit(1)


@app.command()
def applications(
    status: str = typer.Option(None, "--status", help=f"只看某个状态：{'/'.join(APP_STATUSES)}"),
    source: str = typer.Option(None, "--source", help="只看某个源"),
    company: str = typer.Option(None, "--company", help="只看某家公司（跨该公司的所有源）"),
    limit: int = typer.Option(30),
    funnel: bool = typer.Option(False, "--funnel", help="只看分档汇总，不列明细"),
) -> None:
    """看投递记录：投了什么、卡在哪、截图在哪。

    只读。**不提供任何改状态的开关**（`--mark-abandoned` 之类都没有）：
    状态变更必须走 `apply` 的 prepare/execute 两阶段闸门，从一个查看命令里
    改终态等于给那条硬约束开后门。见 docs/plans/012 §5。
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
        title = r["job_title"] or f"[dim]<岗位行已不在>[/dim] {r['external_id']}"
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
    table = Table(title=f"投递漏斗（{len(rows)} 次尝试，{jobs_n} 个岗位）")
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
    if plan.screenshot_path:
        console.print(f"  [dim]填表后截图: {plan.screenshot_path}[/dim]")
    console.print(f"  [dim]确认有效期 {int((plan.expires_at - plan.created_at) / 60)} 分钟[/dim]")


@app.command()
def checkup(
    job_id: str = typer.Argument(..., help="拿哪个岗位的表单来体检（external_id）"),
    source: str = typer.Option(None, help="指定源（默认从 jobs 表推断）"),
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
    row = conn.execute("SELECT * FROM jobs WHERE external_id=?", (job_id,)).fetchone()
    if not row:
        console.print(f"[red]岗位不存在[/red] {job_id}（先跑 sync）")
        raise typer.Exit(1)
    job = dict(row)
    src = source or job["source_key"]

    src_row = conn.execute("SELECT * FROM sources WHERE source_key=?", (src,)).fetchone()
    lookup = dict(job)
    if source:
        lookup.pop("apply_system", None)
        lookup["source_key"] = src
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

    bad = [n for n, ok, _ in rows if not ok]
    if bad:
        console.print(f"\n[red]{len(bad)} 条判据失效[/red]：{'、'.join(bad)}")
        console.print("[dim]这些字段代投会静默跳过。改 feishu.py 里对应的常量，"
                      "改完再跑一次这个命令。[/dim]")
        raise typer.Exit(1)
    console.print(f"\n[green]{len(rows)} 条判据全部有效[/green]")


def _record_blocked(conn, job: dict, src: str, plan: SubmissionPlan) -> None:
    """prepare 没走通，也要留痕：不然用户只看到一句报错，查不到发生过什么。"""
    db.record_blocked(
        conn,
        job_id=job["id"], source_key=src, external_id=job["external_id"],
        company=job["company"], error=plan.blocker,
        screenshot_path=plan.screenshot_path,
    )
    db.add_event(conn, "apply_blocked", source_key=src, company=job["company"],
                 job_id=job["id"], payload={"blocker": plan.blocker})
    conn.commit()


if __name__ == "__main__":
    app()
