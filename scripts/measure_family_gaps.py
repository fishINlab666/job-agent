"""量 issue #8 的判不出族分布。只读真库，不写。

和 `measure_family_rule.py`（方案 015 专用）分开：那个量的是「域词抢走职能词」
一条规则的代价，这个量的是**判不出的那批到底是几种毛病**。issue #8 原文说
「判不出的样本几乎全是同一类」，这个脚本存在的目的就是把这句话验掉或证掉。

口径两条，先说清楚，因为它们决定了所有百分比怎么读：

1. **判不出 = `job_family IS NULL` 且 `closed_at IS NULL`**（在架岗位）。
   下线岗不算 —— 分类规则只服务于「现在能投什么」。
2. **默认按 distinct 标题计**，不是按行。同一个标题在 N 个城市开 N 行，
   按行数会把「一个词表缺口」放大成 N 个。行数单独打出来做对照。

用法：
    cd <repo> && PYTHONPATH="$PWD" .venv/bin/python scripts/measure_family_gaps.py <子命令>
不带参数列出全部子命令。
"""
from __future__ import annotations

import collections
import json
import re
import sys

from jobagent import db
import jobagent.normalize as n


# `vocab_curve()` 里唯一的人工判断，摊开在这里是为了能被反驳。
# 这些词在标题里高频出现但**不定职能**：招聘类型、届别、公司/品牌、学历。
# 判据：把它当成职能词加进 TITLE_RULES，会把一大批不同职能的岗位判成同一族。
DECORATION: tuple[str, ...] = (
    "实习生", "实习", "校招", "校园招聘", "提前批", "培训生", "管培生", "储备",
    "招聘", "岗位", "秋招", "春招", "社招", "转正", "日常",
    "蔚来", "乐道", "小鹏", "字节", "抖音", "飞书", "商汤", "腾讯",
    "本科", "硕士", "博士", "应届",
)


def _rows() -> list[dict]:
    con = db.connect_readonly()
    return [dict(r) for r in con.execute(
        "SELECT title, cities, source_key FROM jobs "
        "WHERE closed_at IS NULL AND job_family IS NULL AND title IS NOT NULL")]


def _titles() -> list[str]:
    return sorted({r["title"] for r in _rows()})


def _city_vocab() -> set[str]:
    """库里出现过的所有归一城市名。用来判断标题尾段是不是城市。"""
    con = db.connect_readonly()
    out: set[str] = set()
    for (raw,) in con.execute("SELECT DISTINCT cities FROM jobs WHERE cities IS NOT NULL"):
        try:
            out.update(json.loads(raw) or [])
        except (json.JSONDecodeError, TypeError):
            continue
    return {c for c in out if c}


def _stem_map() -> dict[str, set[str]]:
    """岗位型 → 落在它下面的 distinct 标题。只剥标题**尾部**的城市名。

    存在的理由：`_titles()` 的去重挡不住把城市写进标题的源。nio 的
    `校招实习-蔚来顾问-<城市>` 有 116 个城市、`乐道顾问` 90 个，去重后仍是 206 条
    distinct 标题 —— 而它们是**同一个词表缺口**。「按 distinct 标题计」这条口径
    本来就是为了「一个标题开在 N 个城市不算 N 个问题」，nio 的命名让它失效了。
    """
    cities = _city_vocab()
    # 只剥「尾段是城市名」的那一段，不做别的清洗 —— 剥多了会把不同岗位并成一个。
    tail = re.compile(r"[-—－_(（\s]\s*(" + "|".join(
        sorted(map(re.escape, cities), key=len, reverse=True)) + r")\s*[)）]?\s*$")
    stems: dict[str, set[str]] = collections.defaultdict(set)
    for t in _titles():
        s = t
        while (m := tail.search(s)):
            s = s[:m.start()]
        stems[s].add(t)
    return stems


def by_source() -> None:
    """每个源的判不出率（行 / distinct 标题两种口径）。"""
    con = db.connect_readonly()
    rows = con.execute(
        "SELECT source_key, COUNT(*) n, SUM(job_family IS NULL) miss "
        "FROM jobs WHERE closed_at IS NULL GROUP BY source_key ORDER BY n DESC").fetchall()
    # 按源去重标题，不是数行 —— 数行会打出和「判不出」一列一样的数，
    # 那种「两列恰好相等」是最容易被当成巧合放过去的错。
    per_src: dict[str, set[str]] = collections.defaultdict(set)
    for r in _rows():
        per_src[r["source_key"]].add(r["title"])
    dis = {k: len(v) for k, v in per_src.items()}
    print(f"{'源':<26}{'在架':>7}{'判不出':>8}{'占比':>8}{'distinct 标题':>14}")
    tot = miss = 0
    for r in rows:
        tot += r["n"]; miss += r["miss"]
        pct = f"{r['miss'] / r['n']:.1%}" if r["n"] else "-"
        print(f"{r['source_key']:<26}{r['n']:>7}{r['miss']:>8}{pct:>8}{dis.get(r['source_key'], 0):>14}")
    print(f"{'合计':<26}{tot:>7}{miss:>8}{miss / tot:>8.1%}{len(_titles()):>14}")
    # 各源 distinct 之和 ≥ 全局 distinct：同一个标题可以在两个源都出现。
    print(f"（各源 distinct 之和 {sum(dis.values())}，全局去重后 {len(_titles())}）")


def city_collapse() -> None:
    """B1：同一个岗位型在多城市各开一条，distinct 标题被城市数放大了多少倍。"""
    stems = _stem_map()
    infl = sorted(stems.items(), key=lambda kv: -len(kv[1]))
    print(f"distinct 标题 {len(_titles())} → 剥掉尾部城市后 {len(stems)} 个岗位型\n")
    print("放大倍数最高的 10 个岗位型：")
    for stem, ts in infl[:10]:
        print(f"  {len(ts):>4} × {stem}")
    n_infl = sum(len(ts) for _, ts in infl if len(ts) > 1)
    print(f"\n被城市放大的标题共 {n_infl} 条（{n_infl / len(_titles()):.1%}），"
          f"塌成 {sum(1 for _, ts in infl if len(ts) > 1)} 个岗位型")


def stale() -> None:
    """当前代码已经能判、但库里还是 NULL 的 —— 这批不是规则缺口，是行过期。

    这个数必须先从所有百分比里扣掉，否则 issue #8 里的比例都偏高。
    """
    rows = _rows()
    hit = {t: n.family_from_title(t) for t in _titles()}
    stale_t = {t: f for t, f in hit.items() if f is not None}
    stale_r = [r for r in rows if hit.get(r["title"]) is not None]
    print(f"distinct 标题 {len(hit)} 条，当前代码能判出 {len(stale_t)} 条"
          f"（{len(stale_t) / len(hit):.1%}）")
    print(f"对应行 {len(stale_r)} / {len(rows)}（{len(stale_r) / len(rows):.1%}）")
    print(f"判成的族分布：{dict(collections.Counter(stale_t.values()))}")
    print("\n样例：")
    for t, f in list(stale_t.items())[:5]:
        print(f"  {f:<10}{t}")
    print("\n这批只要跑一轮 sync 就会自愈（family 在 _fp() 里，指纹会变）。")


def noise() -> None:
    """结构上不可能判的：没有中文职能词可依据的标题。"""
    cjk = re.compile(r"[一-鿿]")
    out = []
    for t in _titles():
        core = re.sub(r"[-—－_(（)）\[\]【】\s/、,，]", "", t)
        if len(cjk.findall(core)) < 2:
            out.append(t)
    print(f"中文字符少于 2 个的标题：{len(out)} 条（{len(out) / len(_titles()):.1%}）")
    for t in out[:15]:
        print(f"  {t!r}")


def vocab_curve() -> None:
    """加词能治多少：贪心挑覆盖最广的 2-4 字词，打出成本曲线。

    这条替代了「VOCAB 多少条 / SYSTEM 多少条」那种人工贴标签的分法 —— 贴标签
    不可复现，曲线可以。曲线的形状本身就是 issue #8 的答案：
      陡降到接近 0  = 词表缺口，加十几个词就完事
      长尾压不下去  = 不是词表问题，得换机制（岗位型归并 / 源站 category 兜底）

    刻意**不判**每个词该归哪个族 —— 那是实现时逐条要定的，这里只回答规模。

    `DECORATION` 是这个脚本里唯一的人工判断，所以摊开写在下面而不是埋在代码里。
    不排掉它，贪心第一个挑中的是 `实习`（502/658 = 76.3%）、第二个是 `校招`
    （92 条）—— 那是招聘类型词，加进 TITLE_RULES 会把四分之三的标题判成同一个
    族。「覆盖率高」和「能定族」是两件事，曲线本身分不出来。

    **计数单位是岗位型（stem），不是 distinct 标题。** 第一版按标题算，于是
    `蔚来顾问`(116 城) + `乐道顾问`(90 城) 被算成 206 个词表缺口，`顾问` 一个词
    就顶了 34.2% —— 那个数量级是 nio 把城市写进标题造成的，不是真实分布。
    """
    stems = _stem_map()
    # 塌成岗位型不该改变判据结论。城市名不是规则词，所以同一 stem 下的标题
    # family 必须一致；不一致说明剥城市剥过头了（把两个真不同的岗位并成一个）。
    disagree = [
        s for s, ts in stems.items()
        if len({n.family_from_title(t) for t in ts}) > 1
    ]
    if disagree:
        print(f"**剥城市剥过头了**：{len(disagree)} 个岗位型下面的标题 family 不一致，"
              f"例如 {disagree[:3]} —— 下面的数不可信，先修 _stem_map()。\n")
        return
    todo = {s for s in stems if n.family_from_title(s) is None}
    total = len(todo)
    print(f"扣掉过期行后，真正判不出的岗位型：{total} 个"
          f"（对照：distinct 标题 {len({t for t in _titles() if n.family_from_title(t) is None})} 条，"
          f"差额来自把城市写进标题的源）")
    print(f"{len(stems)} 个岗位型下面 family 结论全部一致，塌得住")
    print(f"排掉的装饰词（{len(DECORATION)} 个）：{'、'.join(DECORATION)}\n")
    cover: dict[str, set[str]] = collections.defaultdict(set)
    for t in todo:
        core = re.sub(r"[-—－_(（)）\[\]【】\s/、,，]", "", t)
        for w in DECORATION:
            core = core.replace(w, "|")
        for size in (2, 3, 4):
            for i in range(len(core) - size + 1):
                w = core[i:i + size]
                if re.fullmatch(r"[一-鿿]+", w):
                    cover[w].add(t)
    left, picked = set(todo), []
    # 贪心到 25 个词就停，因为曲线过了头部就不可信了（见 docstring）。
    while left and len(picked) < 25:
        w, got = max(((w, ts & left) for w, ts in cover.items()),
                     key=lambda kv: (len(kv[1]), -len(kv[0])))
        if len(got) < 3:
            break
        picked.append((w, len(got)))
        left -= got
        print(f"  +{len(picked):>2} 个词  {w:<6} 新覆盖 {len(got):>4}  "
              f"累计 {total - len(left):>4}/{total} = {(total - len(left)) / total:>5.1%}")
    print(f"\n{len(picked)} 个词覆盖 {total - len(left)} 个岗位型，剩 {len(left)} 个长尾"
          f"（{len(left) / total:.1%}），每个命中都不到 3 次")
    print("长尾样例：")
    for t in sorted(left)[:12]:
        print(f"  {t}")


# `candidates()` 要试算的规则。摊开成常量，理由同 DECORATION：每一条都是人工
# 判断，得能被逐条反驳。词后面的族是「这个词在中文岗位名里指什么活」：
#   顾问 —— 面客的销售/服务岗（乐道顾问、蔚来顾问是门店销售）
#   服务 —— 客户服务
#   采购 —— 供应链
# 刻意**不含** `策略`/`分析`/`管理`/`方向`/`大区`/`研究`/`基础`/`系统`/`技术`：
# 它们在曲线头部但**不定职能** —— `策略产品`是产品、`策略运营`是运营、
# `广告策略`是技术；`大区`/`方向`是组织/范围词。曲线按覆盖率排，排不出这个区别。
CANDIDATES: tuple[tuple[str, str], ...] = (
    ("顾问", "sales"),
    ("服务", "sales"),
    ("采购", "other"),
)


def _judge_with(title: str, extra: tuple[str, str] | None,
                *, at_end: bool = True) -> str | None:
    """现有三层判定 + 往第 2 层塞一个候选词。extra=None 就是现状。

    **必须真的插进规则表跑一遍**，不能用「含这个词且已能判出」当代价的代理：
    第 2 层是「先命中的赢」，所以同一个词加在 sales 组（表中第 5 条）和加在
    末尾，改判结果不一样。代理算法看不出插入位置，会把 0 误判报成 500。
    """
    rules = list(n.TITLE_RULES)
    if extra is not None:
        word, fam = extra
        idx = next((i for i, (_, f) in enumerate(rules) if f == fam), None)
        if idx is not None and not at_end:
            kws, f = rules[idx]
            rules[idx] = (kws + (word,), f)
        else:
            rules.append(((word,), fam))
    if any(m in title for m in n.TECH_MARKERS):
        return "tech"
    for kws, fam in n.COMPOUND_RULES:
        if any(k in title for k in kws):
            return fam
    d = n._first_index(title, n.DESIGN_DOMAIN_WORDS)
    f = n._first_index(title, n.TECH_FUNCTION_WORDS)
    if d is not None and f is not None and d < f:
        return "tech"
    for kws, fam in rules:
        if any(k in title for k in kws):
            return fam
    return None


def candidates() -> None:
    """逐条**真的插进规则表**试算 CANDIDATES：救回多少、改判多少。

    改判那一列才是决定要不要加的判据。覆盖率只说明「能碰到多少」，
    碰到之后判错和判对都算碰到 —— 见 docs/plans/015 的教训。

    两个插入位置都试：加进已有的同族组（组在表中的位次决定它和别的族谁先命中），
    和加在表末尾（所有既有规则都优先）。两者改判数不同就说明这个词和别的族有交叠，
    交叠的方向要逐条看，不能只看总数。
    """
    con = db.connect_readonly()
    all_rows = [dict(r) for r in con.execute(
        "SELECT title, job_family, source_key FROM jobs "
        "WHERE closed_at IS NULL AND title IS NOT NULL")]
    stems = _stem_map()
    stem_of = {t: s for s, ts in stems.items() for t in ts}
    titles = {r["title"] for r in all_rows}
    base = {t: _judge_with(t, None) for t in titles}
    # 自证试算函数和线上判据一致：extra=None 必须逐条等于 normalize 的结果。
    drift = [t for t in titles if base[t] != n.family_from_title(t)]
    if drift:
        print(f"**试算函数和 normalize.py 不一致**：{len(drift)} 条，"
              f"例如 {drift[:3]} —— 下面的数不可信。\n")
        return
    # 两个 distinct 数**统计的不是同一批**，分开说清楚：
    # titles = 在架全部标题（改判要在这批上找），stems = 判不出的那批塌成的岗位型。
    print(f"在架行 {len(all_rows)}，在架 distinct 标题 {len(titles)}；"
          f"其中判不出的塌成 {len(stems)} 个岗位型")
    print("（试算函数 extra=None 时与 normalize.py 逐条一致）\n")
    has_group = {f for _, f in n.TITLE_RULES}
    for word, fam in CANDIDATES:
        print(f"「{word}」→ {fam}")
        in_group = (f"加进 {fam} 组" if fam in has_group
                    else f"{fam} 在第 2 层没有组，只能加在末尾")
        for at_end, label in ((False, in_group), (True, "加在表末尾")):
            if fam not in has_group and not at_end:
                print(f"  [{label}]")
                continue
            new = {t: _judge_with(t, (word, fam), at_end=at_end) for t in titles}
            rescued = {t for t in titles if base[t] is None and new[t] is not None}
            changed = {t for t in titles
                       if base[t] is not None and new[t] != base[t]}
            r_rows = [r for r in all_rows if r["title"] in rescued]
            c_rows = [r for r in all_rows if r["title"] in changed]
            r_stems = {stem_of.get(t, t) for t in rescued}
            print(f"  [{label}] 救回 {len(r_rows)} 行 / {len(r_stems)} 个岗位型，"
                  f"改判 {len(c_rows)} 行 / {len(changed)} 条标题")
            for t in sorted(changed)[:6]:
                print(f"      {base[t]} → {new[t]}   {t}")
            if len(changed) > 6:
                print(f"      …… 另 {len(changed) - 6} 条")
            # 救回的明细只打一次（两个插入位置救回的是同一批：
            # 救回 = 原本判不出，任何插入位置都不会被既有规则抢走）。
            if at_end:
                srcs = collections.Counter(r["source_key"] for r in r_rows)
                print(f"      救回源分布 {dict(srcs)}")
                print(f"      救回的岗位型 {sorted(r_stems)[:6]}"
                      f"{' ……' if len(r_stems) > 6 else ''}")
        print()


COMMANDS = {
    "by-source": by_source,
    "stale": stale,
    "city-collapse": city_collapse,
    "vocab-curve": vocab_curve,
    "candidates": candidates,
    "noise": noise,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("子命令：")
        for name, fn in COMMANDS.items():
            print(f"  {name:<16}{(fn.__doc__ or '').splitlines()[0]}")
        return 1 if len(sys.argv) != 1 else 0
    print(f"=== {sys.argv[1]} ===\n")
    COMMANDS[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
