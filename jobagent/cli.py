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


@app.command()
def init() -> None:
    """建库建表。"""
    conn = db.connect()
    db.init(conn)
    console.print(f"[green]库已就绪[/green] {db.DB_PATH}")


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
            raise typer.Exit(1)
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
def jobs(
    family: str = typer.Option(None, help="按归一岗位族过滤"),
    city: str = typer.Option(None, help="按城市过滤"),
    recruit_type: str = typer.Option(None, help="campus / intern"),
    limit: int = typer.Option(30),
    matched: bool = typer.Option(False, "--matched", help="只看命中我画像的"),
    loose: bool = typer.Option(
        False, "--loose", help="连信息不全的一起看（届别/城市没写的岗位）"
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

    if matched:
        rows = match.filter_jobs(
            rows, match.load_profile().get("intent") or {}, include_unknown=loose
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
            if "title" in diff:
                parts.append(f"标题: {diff['title']['old']} → {diff['title']['new']}")
            if "job_family" in diff:
                old_fam = FAM_ZH.get(diff["job_family"]["old"], diff["job_family"]["old"])
                new_fam = FAM_ZH.get(diff["job_family"]["new"], diff["job_family"]["new"])
                parts.append(f"族: {old_fam} → {new_fam}")
            if "cities" in diff:
                parts.append(f"城市: {diff['cities']['old']} → {diff['cities']['new']}")
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
        for d in sorted(unsure, key=lambda x: match.score(x, intent), reverse=True)[:15]:
            cities = "/".join(match.city_list(d)[:3]) or "未写"
            console.print(
                f"  ? {d['company']} · {d['title']} · {cities}"
                f"  [dim]{d['_why']}[/dim]"
            )
            console.print(f"    [dim]{d.get('apply_url') or '-'}[/dim]")
        if len(unsure) > 15:
            console.print(f"  [dim]…还有 {len(unsure) - 15} 条，见 jobs --matched --loose[/dim]")

    if not hits and not unsure and not highlights and not changed:
        console.print("[dim]没有新增。[/dim]")

    if mark and shown:
        conn.executemany(
            "UPDATE events SET notified_at=? WHERE id=?",
            [(db.now(), i) for i in shown],
        )
        conn.commit()
        console.print(f"[dim]已标记 {len(shown)} 条事件为已推送[/dim]")


@app.command()
def status() -> None:
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
