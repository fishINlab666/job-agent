"""跑清单里那 5 个源，把数据真的落进本地库，再把字段摊开给人逐个核对。

用途只有一个：**拿这份输出去和官网对**。所以它做三件事，
每件都是为了让「对不上」当场看得见：

  1. 真跑一轮 `ingest.sync()`，数据落库（默认落 `data/jobagent-5.db`，不动主库）
  2. 打**字段填充率** —— 哪些字段系统性为空先说清楚，别让人去官网白找
  3. 抽样把每条岗位的字段摊开，每行带 `apply_url`，点开就是官网那一页

为什么单独一个库：主库里有腾讯那 795 条真实数据，而核对是要反复跑的动作，
反复往主库写会搅乱 `first_seen_at` 和 `snapshots`。真想写主库就 `--db` 指过去。

清单里这 5 个源。**飞书四家全部指向校招门户**（`website-path`），
2026-08-06 逐个全量翻页实测，每个都 `rows == count`、id 全 unique：

| source_key                | 公司     | 系统   | 实测口径（2026-08-06）|
|---------------------------|----------|--------|---------------------|
| tencent_join              | 腾讯     | 自建   | campus 366（26 届存量）+ intern 435（27 届），届别 100% 有值。27 届秋招还没开 |
| feishu:nio:campus         | 蔚来     | feishu | **627** 条 · campus 218 + intern 409 · 判不出族 279 |
| feishu:xiaopeng:campus    | 小鹏汽车 | feishu | **436** 条 · campus 312 + intern 124 · 判不出族 163 |
| feishu:bytedance:campus   | 字节跳动 | feishu | **7395** 条 · campus 2048 + intern 5347 · 判不出族 416（6%）|
| feishu:sensetime:edu      | 商汤科技 | feishu | **160** 条 · campus 92 + intern 68 · 自定义域名 `hr-jobs.sensetime.com` |

三处和用户给的核对入口不完全一样，先说清楚免得被当成采错：

- **蔚来**：用户给的是 `campus.nio.com/#/`，那是自建 Vue（`nio-school-front`），
  「立即投递」跳 `nio.jobs.feishu.cn/campus/` —— 数据仍在飞书，所以采后者。
- **小鹏**：用户给的是 `/398875`，实测它是 `campus` 的**子门户且被完全包含**
  （`campus ∩ 398875 = 335`，campus 共 436）。采父门户 `campus` 是超集，
  所以核对时官网那一页只会看到 436 里的 335 条，不是我们少采了。
- **商汤**：`hr-jobs.sensetime.com` 与 `sensetime.jobs.feishu.cn` 是同一个租户
  （同样条数、id 全交），但 `apply_url` 用配的那个 host，跟用户的入口一致。

**海底捞换成了字节跳动。** 海底捞压根没有校招门户（`campus` 回
`code=-9000003`），用户核对时也指出官网找不到校招入口。字节是按「页面标题
自称什么」核对过的真租户（`bytedance.jobs.feishu.cn/campus/` 与
`jobs.bytedance.com/campus` 同为「字节跳动校园招聘官网」），
`recruit_type.parent` 恒为校招，判不出族只有 6% —— 四家里最干净的一家。

**上一版这个脚本采的全是社招池**，2026-08-05 用户拿输出去对官网，
指出 5 个入口里 4 个是错的。原因是 `website-path` 请求头才是换门户的开关，
不带头拿到的是第三个来历不明的池：

    website-path: campus  → nio 627 条，recruit_type.parent = 校招
    website-path: index   → nio 2078 条，parent = 社招
    不带头                 → nio 2249 条  ← 上一版采的是这个

**曾经写在这里的「飞书这个口子没有结构化校招」是错的。** 观测能复现
（社招池上 4009 条 `recruit_type.parent.name` 恒为「社招」），但结论错在取样口径:
我在社招池里数了四遍「没有校招」。保留这句话是为了让下一个人认得这个错法 ——
拿到一批数据先问「这是哪一个池」，再问「这批里有什么」。

四家公司共用 `FeishuAdapter` 这一个类（nio/xiaopeng/bytedance/sensetime），
这正是按 ATS 分层的杠杆：加一家公司的边际成本 = 这个列表里加一行。

用法：
    uv run python scripts/run_five.py                      # 全部 5 个，落 data/jobagent-5.db
    uv run python scripts/run_five.py --only feishu:nio:campus   # 只跑一个，可重复给
    uv run python scripts/run_five.py --dry-run            # 只算不写（字段报告要真跑才有）
    uv run python scripts/run_five.py --export /tmp/x.jsonl
    uv run python scripts/run_five.py --fresh              # 先删库重来，跑出干净的首轮
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobagent import db, ingest, routing  # noqa: E402

console = Console()
app = typer.Typer(add_completion=False)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "jobagent-5.db"

SOURCES: list[dict] = [
    {
        "source_key": "tencent_join",
        "company": "腾讯",
        "system": "tencent_join",
        "entry_url": "https://join.qq.com/post.html",
        "tenant": None,
    },
    # 门户在 source_key 第三段里（`routing.portal_of` 取它），不另开一列 ——
    # 这个键就是判据本身，也是关闭守卫的分母边界。见 003 §6。
    {
        "source_key": "feishu:nio:campus",
        "company": "蔚来",
        "system": "feishu",
        # entry_url 带门户路径：这一列是给核对的人点开的，指到租户首页
        # 会让他看到社招列表，然后以为我们采错了。
        "entry_url": "https://nio.jobs.feishu.cn/campus/",
        "tenant": "nio",
    },
    {
        "source_key": "feishu:xiaopeng:campus",
        "company": "小鹏汽车",
        "system": "feishu",
        # 用户给的入口是子门户 /398875（335 条），campus 是它的父门户（436 条）
        # 且完全包含它。采父门户，核对时以官网 campus 那一页为准。
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
        # 自定义域名。host 从这一列取（`routing.get_adapter` 读 entry_url），
        # 不从岗位链接里现推 —— 那等于放宽域名判据。
        "entry_url": "https://hr-jobs.sensetime.com/edu/",
        "tenant": "sensetime",
    },
]

# 试过但没留在清单里的租户，记在这儿免得下一个人再探一遍。
# 探法是**看页面标题自称什么**，不是猜租户名 —— 猜 slug 那一轮 18 个里 16 个
# 直接 JSONDecodeError，和 002 §8 猜门户路径是同一种错法。
#
#   haidilao   海底捞   campus/edu 全回 code=-9000003 —— 没有校招门户，被字节换掉
#   luckin     活租户 count=0（页面自称「加入狂浪俱乐部」，不是瑞幸）
#   horizon    活租户 count=0（页面自称「加入汉森」，不是地平线）
#   chagee     霸王茶姬 campus 只有 12 条，太薄，撑不起主场景（index 85）
#   yonghui / poizon / mogu / soulapp / wumart  活租户但**没有校招门户**
#   moonshot / horizonrobotics / keep / beingmate / missfresh / baic / hisense
#                                             活租户但 count=0
#
# luckin/horizon 验的是 empty_is_authoritative 那条路径，但那条已有单测钉住
# （test_empty_fetch_ok_when_adapter_says_so），不必占清单的位置。
_RETIRED = ("haidilao", "luckin", "horizon", "chagee")

# jobs 表里值得逐列核对的字段。顺序按「先认出是哪个岗位 → 再看归一结果 → 最后看原文」。
CHECK_FIELDS = (
    "external_id", "title", "recruit_type", "grad_year", "job_family",
    "raw_category", "cities", "raw_location", "country", "department",
    "apply_url", "apply_system", "description",
)

# 已核实「这个系统就是没有」的字段。填充率 0% 在这里是对的，不是 bug。
# 写死在这儿是为了让核对的人别跑去官网白找一遍 —— 见 docs/plans/002-飞书招聘采集.md §3。
# 反过来也成立：**不在这张表里的 0% 就是该查的**，报告里会标红。
KNOWN_EMPTY: dict[str, dict[str, str]] = {
    "feishu": {
        # 校招门户上也没有。标题里有（xiaopeng 校招池 349/436 带「27届」），
        # 但抠标题动 normalize.py、会影响腾讯已落的 801 条，是另一份方案（003 §8）。
        "grad_year": "结构化字段里没有届别（四个租户 × 12 个门户全量核实过）",
        "department": "接口只给 department_id 没给名字，不编",
    },
    "tencent_join": {
        "description": "详情要单独打 jobDetails 接口，MVP 没拉",
    },
}
def _register(conn, spec: dict) -> None:
    """把源写进 sources。

    多租户源必须先有这一行才跑得起来：`routing._build()` 拿不到 tenant 会拒绝启动
    （宁可不跑，也不能拿别的租户的页面去投递）。`source add` 命令还没做，
    所以这里手写 —— 见 002 §8。
    """
    db.register_source(
        conn, spec["source_key"], spec["company"], spec["system"],
        spec["entry_url"], notes="scripts/run_five.py 登记",
        tenant=spec["tenant"],
    )


def _sync_one(conn, spec: dict, dry_run: bool) -> dict:
    key = spec["source_key"]
    row = conn.execute("SELECT * FROM sources WHERE source_key=?", (key,)).fetchone()
    adapter = routing.get_adapter({"source_key": key}, dict(row) if row else None)

    console.print(f"[cyan]同步 {key}[/cyan]（{spec['company']}）...")
    try:
        st = ingest.sync(conn, adapter, dry_run=dry_run)
    except Exception as exc:
        # 一个源炸了不该带走其余四个。但**必须原样打出来**，不能只说「失败」——
        # 「返回 0 条判定为上游异常」和「HTTP 405」要的处理动作完全不同。
        console.print(f"  [red]失败[/red] {type(exc).__name__}: {exc}")
        return {"source": key, "error": f"{type(exc).__name__}: {exc}"}

    tag = " [dim](首轮，不发单条事件)[/dim]" if st["bootstrap"] else ""
    console.print(
        f"  抓取 {st['fetched']} · 新增 {st['opened']} · 变更 {st['updated']}"
        f" · 关闭 {st['closed']}{tag}"
    )
    unknown = st.get("family_unknown", 0)
    if unknown:
        pct = unknown / st["fetched"] if st["fetched"] else 0
        console.print(
            f"  [yellow]判不出岗位族 {unknown} 条（{pct:.0%}）[/yellow] —— 按族筛会漏掉这些"
        )
    if st["guard_tripped"]:
        console.print("  [yellow]关闭守卫触发[/yellow]：本轮未关闭任何岗位，run 记 partial")
    # 空但被判为可信：这是 luckin/horizon 那条路径，要显式说出来，
    # 否则「抓取 0」和「抓失败」在屏幕上长得一样。
    if st["fetched"] == 0 and not st["bootstrap"]:
        console.print("  [dim]接口明确回了 count=0：活租户，当下没在招 —— 是事实，不是故障[/dim]")
    elif st["fetched"] == 0:
        console.print("  [dim]接口明确回了 count=0（empty_is_authoritative 那条路径）[/dim]")
    skipped = getattr(adapter, "skipped_no_id", 0)
    if skipped:
        console.print(f"  [yellow]因为没有 id 跳过 {skipped} 条[/yellow]")
    return {"source": key, **st, "skipped_no_id": skipped}
def _fill_report(conn, spec: dict) -> None:
    """字段填充率。核对的第一步是知道**哪些字段根本没值**。

    不打这张表的话，核对的人会拿着一个空的 department 去官网找半天，
    而那本来就是接口没给的。
    """
    key, system = spec["source_key"], spec["system"]
    rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE source_key=? AND closed_at IS NULL", (key,)
        ).fetchall()
    ]
    if not rows:
        console.print(f"[dim]{key}：库里 0 条，跳过字段报告[/dim]\n")
        return

    known = KNOWN_EMPTY.get(system, {})
    t = Table(title=f"{key}（{spec['company']}）字段填充率 · n={len(rows)}",
              show_lines=False)
    t.add_column("字段"); t.add_column("有值", justify="right")
    t.add_column("占比", justify="right"); t.add_column("样例 / 说明")

    for f in CHECK_FIELDS:
        vals = [r.get(f) for r in rows]
        # 空列表的 JSON 是 "[]"，非空才算有值 —— cities 全空会伪装成 100%。
        filled = [v for v in vals if v not in (None, "", "[]")]
        pct = len(filled) / len(rows)
        sample = str(filled[0])[:46] if filled else ""
        if not filled:
            note = known.get(f)
            # 在 KNOWN_EMPTY 里 = 已核实这个系统就是没有，0% 是对的；
            # 不在 = 该查，标红。这个区分就是这张表的全部价值。
            sample = f"[dim]{note}[/dim]" if note else "[red]← 没有已核实的理由，该查[/red]"
        style = None if filled or f in known else "red"
        t.add_row(f, str(len(filled)), f"{pct:.0%}", sample, style=style)
    console.print(t)

    fams = {}
    for r in rows:
        fams[r["job_family"]] = fams.get(r["job_family"], 0) + 1
    unknown = fams.get(None, 0)
    parts = ", ".join(
        f"{k or '判不出'}={v}" for k, v in sorted(fams.items(), key=lambda kv: -kv[1])
    )
    console.print(f"  岗位族分布：{parts}")
    if unknown:
        # 区间要带公司和日期。上一版写死「33%~49%」（两个租户量的），
        # 海底捞和商汤一进来下界掉到 16.9%，换成校招门户又掉到 6%。
        # **这个比例是公司属性不是系统属性**：同一份 TITLE_RULES 下
        # 字节 6% / 商汤 9% / 小鹏 37% / 蔚来 44%，差异来自标题写法
        # （蔚来大量「实习-NSC机电技师」这类内部岗名）。写成区间而不写来源，
        # 下一个人会以为规则坏了，然后去改规则。
        console.print(
            f"  [yellow]判不出 {unknown}/{len(rows)}（{unknown / len(rows):.1%}）[/yellow]"
            " —— 按公司差很多（2026-08-06 校招门户实测 6%~44%），别当常量"
        )
    rt = {}
    for r in rows:
        rt[r["recruit_type"]] = rt.get(r["recruit_type"], 0) + 1
    console.print(
        "  招聘类型：" + ", ".join(f"{k or '认不出'}={v}" for k, v in sorted(rt.items(), key=lambda kv: -kv[1]))
    )
    console.print()
def _samples(conn, spec: dict, n: int) -> None:
    """抽 n 条把字段摊开，每条带 apply_url —— 点开就是官网那一页，直接对。

    抽样刻意包含「判不出族」的那批：核对最该看的就是这些边界样本，
    随机抽很可能全是正常岗位，看着一切正常。
    """
    key = spec["source_key"]
    rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE source_key=? AND closed_at IS NULL "
            "ORDER BY (job_family IS NULL) DESC, external_id LIMIT ?",
            (key, n),
        ).fetchall()
    ]
    if not rows:
        return
    console.print(f"[bold]{key} 抽样 {len(rows)} 条（判不出族的排在前面）[/bold]")
    for r in rows:
        console.print(f"  [cyan]{r['title']}[/cyan]  [dim]{r['apply_url']}[/dim]")
        console.print(
            f"    族={r['job_family'] or '[yellow]判不出[/yellow]'}"
            f" · 源站分类={r['raw_category'] or '-'}"
            f" · 类型={r['recruit_type'] or '-'}"
            f" · 届别={r['grad_year'] or '-'}"
        )
        console.print(
            f"    城市={r['cities']} · 原文位置={r['raw_location'] or '-'}"
            f" · 部门={r['department'] or '-'}"
        )
    console.print()


@app.command()
def main(
    db_path: str = typer.Option(str(DEFAULT_DB), "--db", help="落库路径。默认另开一个库，不动主库"),
    only: list[str] = typer.Option(None, "--only", help="只跑这些 source_key，可重复"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只算不写库"),
    samples: int = typer.Option(5, "--samples", help="每个源摊开几条给人核对"),
    export: str = typer.Option(None, "--export", help="把落库结果导成 JSONL，便于逐字段 diff"),
    fresh: bool = typer.Option(False, "--fresh", help="先删掉那个库，跑一个干净的首轮"),
) -> None:
    """跑清单 5 个源 → 落库 → 打字段报告与抽样。"""
    path = Path(db_path)
    if fresh and path.exists():
        # 只删这个脚本自己的库。删主库是不可逆动作，不在这里做。
        if path.resolve() == (ROOT / "data" / "jobagent.db").resolve():
            console.print("[red]--fresh 不删主库[/red]。要重来请指一个别的 --db。")
            raise typer.Exit(1)
        path.unlink()
        console.print(f"[dim]已删除 {path}[/dim]")

    specs = [s for s in SOURCES if not only or s["source_key"] in only]
    if not specs:
        console.print(f"[red]--only 没匹配到任何源[/red]。可选：{[s['source_key'] for s in SOURCES]}")
        raise typer.Exit(1)

    conn = db.connect(path)
    db.init(conn)
    console.print(f"[green]库[/green] {path}" + ("  [yellow](dry-run，不写)[/yellow]" if dry_run else ""))

    # 登记要在 dry-run 之外做：dry-run 下 sync 不写 sources，而多租户源没有
    # sources 行根本路由不起来。这一行本身是配置不是采集结果，落它不违反「只算不写」。
    for s in specs:
        _register(conn, s)
    conn.commit()

    results = [_sync_one(conn, s, dry_run) for s in specs]
    console.print()

    if dry_run:
        console.print("[yellow]dry-run 不落库，字段报告和抽样需要真跑一轮[/yellow]")
    else:
        for s in specs:
            _fill_report(conn, s)
        for s in specs:
            _samples(conn, s, samples)

    t = Table(title="汇总")
    for c in ("源", "公司", "抓取", "新增", "判不出族", "首轮", "状态"):
        t.add_column(c, justify="right" if c in ("抓取", "新增", "判不出族") else "left")
    for spec, r in zip(specs, results):
        if "error" in r:
            t.add_row(r["source"], spec["company"], "-", "-", "-", "-", f"[red]{r['error'][:40]}[/red]")
            continue
        fetched, unk = r["fetched"], r.get("family_unknown", 0)
        t.add_row(
            r["source"], spec["company"], str(fetched), str(r["opened"]),
            f"{unk} ({unk / fetched:.0%})" if fetched else "-",
            "是" if r["bootstrap"] else "否",
            "[yellow]partial[/yellow]" if r["guard_tripped"] else "[green]ok[/green]",
        )
    console.print(t)

    if export and not dry_run:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM jobs WHERE source_key IN (%s) AND closed_at IS NULL"
                % ",".join("?" * len(specs)),
                [s["source_key"] for s in specs],
            ).fetchall()
        ]
        # 末尾那个换行是必须的：少了它 `wc -l` 比实际条数少 1，
        # 而下一个人会拿 wc 的数去对「导出 N 条」，然后去找那条不存在的丢失记录。
        Path(export).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        console.print(f"[green]导出[/green] {len(rows)} 条 → {export}")

    console.print(
        "\n[dim]核对提示：字段报告里标红的 0% 才是要查的，"
        "带说明的 0% 是已核实「这个系统就是没有」。[/dim]"
    )
    conn.close()


if __name__ == "__main__":
    app()
