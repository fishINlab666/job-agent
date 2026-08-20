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


# 尾段是「…大区」的那一段。和城市名分开一条正则，因为 `大区` 不在 `_city_vocab()`
# 里 —— 库里的归一城市是「上海」「广州」，而小鹏往标题里写的是「（上海大区）」
# 「（鲁豫大区）」「（西北大区）」，前者匹配不到后者。
#
# 边界：只吃**尾段**。`服务大区管理培训生` 里 `大区` 在词干中间，不能剥 ——
# 剥了会把它和 `服务管理培训生` 并成一个岗位型，那是两个不同的岗。
REGION_TAIL = re.compile(r"[-—－_(（\s][^()（）]*大区[^()（）]*[)）]?\s*$")


def _stem_map() -> dict[str, set[str]]:
    """岗位型 → 落在它下面的 distinct 标题。剥标题**尾部**的城市名和 `大区` 段。

    存在的理由：`_titles()` 的去重挡不住把城市写进标题的源。nio 的
    `校招实习-蔚来顾问-<城市>` 有 116 个城市、`乐道顾问` 90 个，去重后仍是 206 条
    distinct 标题 —— 而它们是**同一个词表缺口**。「按 distinct 标题计」这条口径
    本来就是为了「一个标题开在 N 个城市不算 N 个问题」，nio 的命名让它失效了。

    `大区` 是同一个毛病的第二种形态，018 才发现：小鹏的
    `新零售实习生（上海大区）`…`（鲁豫大区）` 是 1 个缺口被数了 15 次。
    加上这一段之后基数从 428 降到 399 —— 也就是说 017/018 之前所有以 428 为分母
    的百分比都偏小。见方案 018 §7。
    """
    cities = _city_vocab()
    # 只剥「尾段是城市名」的那一段，不做别的清洗 —— 剥多了会把不同岗位并成一个。
    tail = re.compile(r"[-—－_(（\s]\s*(" + "|".join(
        sorted(map(re.escape, cities), key=len, reverse=True)) + r")\s*[)）]?\s*$")
    stems: dict[str, set[str]] = collections.defaultdict(set)
    for t in _titles():
        s = t
        while (m := tail.search(s)) or (m := REGION_TAIL.search(s)):
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
    """B1：同一个岗位型在多城市各开一条，distinct 标题被城市数放大了多少倍。

    三个数都打出来，因为 018 之前和之后的分母不是同一个：
      430  distinct 标题
      428  只剥城市（017 及之前的口径 —— 所有以 428 为分母的百分比都偏小）
      399  剥城市 + 剥 `大区`（018 之后的口径）
    原来只打 `430 → 399`，措辞还写着「剥掉尾部城市后」，而实际剥了两样。方案
    018 §9 A 写的是「应打出 428 → 399」，和输出对不上 —— 428 是**只剥城市后的
    岗位型数**，430 是 distinct 标题数，两个都对但不是一个量。中间那档不打出来，
    「428 → 399」这句话就没有能直接看的出处。
    """
    stems = _stem_map()
    infl = sorted(stems.items(), key=lambda kv: -len(kv[1]))
    saved = globals()["REGION_TAIL"]
    globals()["REGION_TAIL"] = re.compile(r"(?!x)x")   # 永不匹配 = 只剥城市
    try:
        city_only = len(_stem_map())
    finally:
        globals()["REGION_TAIL"] = saved
    print(f"distinct 标题 {len(_titles())} → 只剥城市 {city_only} 个岗位型"
          f" → 再剥 `大区` {len(stems)} 个岗位型\n")
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
    return n._family_from_title_rules(title, rules)


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


# 018 加进 TITLE_RULES 的 30 个词。这里**重复**写一遍是故意的：脚本要能回答
# 「这 30 个词各自救回了什么」，就得能判「没有它们时是什么」。从 TITLE_RULES 反推
# 「哪些是 018 加的」需要靠位置或注释，那种耦合一改表就静默失效。
NEW_WORDS_018: tuple[tuple[str, str], ...] = (
    ("软件", "tech"), ("系统", "tech"), ("SRE", "tech"), ("嵌入式", "tech"),
    ("结构", "tech"), ("材料", "tech"), ("热管理", "tech"), ("品质", "tech"),
    ("电控", "tech"), ("仿真", "tech"), ("传感", "tech"),
    ("交付", "operations"), ("履约", "operations"), ("产销", "operations"),
    ("咨询", "sales"), ("售后", "sales"), ("零售", "sales"), ("商家", "sales"),
    ("渠道", "sales"), ("达人", "sales"),
    ("结算", "finance"), ("税务", "finance"), ("资金", "finance"),
    ("内控", "finance"), ("成本", "finance"), ("定价", "finance"),
    ("传播", "marketing"), ("法规", "legal"),
    ("物流", "other"), ("备件", "other"),
)

# 量过边际贡献之后**删掉**的词。和 REJECTED_018 分开：那些是「读了救回内容发现判错」，
# 这个是「判得对但一条都不多救」。两种否决理由不同，混在一张表里会让人以为
# `仓储` 也是个会判错的词。
ZERO_MARGIN_018: tuple[tuple[str, str], ...] = (
    ("仓储", "库里 2 条含仓储的标题分别被 `备件`/`物流` 和 `运营` 先接住，单摘它判定不变"),
)

# 量过之后**否决**的词，连理由一起留着 —— 否则下一个人会重新发现它们「能救很多」
# 然后加回来。每条的第二个元素是「如果加了会怎样」。
REJECTED_018: tuple[tuple[str, str, str], ...] = (
    ("培训", "hr", "招聘类型不是职能：救 94 个岗位型约 91 个错，吃掉小鹏全部工程 `培训生`"),
    ("计划", "operations", "`培养计划` 里是「项目」的意思：救 9 个错 6 个"),
    ("质量", "tech", "字节 `内容质量` 是运营岗：救 13 个错 3 个"),
    # 这一行的族原本写的是 `data`，而 FAMILIES 里没有 data ——
    # 被 test_no_family_names_in_either 逮住的。否决表里的族名也要是真族名，
    # 否则「如果加了会怎样」这句话本身就没法验。
    ("分析", "tech", "跨族：数据分析=tech / 商业分析=sales / 财务分析=finance"),
    ("策略", "product", "域词：策略产品=product / 策略运营=operations / 广告策略=tech"),
    ("基础设施", "tech", "11 个救回里 10 个只命中在部门名上，见 issue #13"),
    ("内容", "operations", "腾讯是域词（`内容培训生-艺术创作方向`），库级改族 5 行"),
    ("治理", "operations", "同 `内容`，再叠 2 行，合计 7 行改族"),
    ("供应链", "other", "已被 `采购`/`物流`/`备件` 覆盖，自身只多救 1 个"),
    ("工艺", "tech", "1 个救回，已被 `品质` 覆盖"),
    ("绩效", "hr", "唯一那条救回是尾段命中（部门名），见 issue #13"),
    ("商务", "sales", "`商务` 在字节指 BD 也指法务合同，跨族"),
    ("增长", "marketing", "增长产品/增长运营/增长算法三族都有"),
    ("激励", "hr", "`激励` 多数在 `激励中台` 里，是技术岗"),
    ("版权", "legal", "腾讯 `版权` 是内容采买，偏 sales"),
    ("基建", "tech", "同 `基础设施`，命中都在部门名"),
)


def _judge_without(title: str, drop: set[str]) -> str | None:
    """把 `drop` 里的词从第 2 层摘掉之后再判。用来回答「没有这些词时是什么」。

    不是 `_judge_with` 的反函数：`_judge_with` 往表里加词，这个从表里减词。两个都
    读**活的** TITLE_RULES，所以表一改这里跟着变，不会留下过期副本。
    """
    if any(m in title for m in n.TECH_MARKERS):
        return "tech"
    for kws, fam in n.COMPOUND_RULES:
        if any(k in title for k in kws):
            return fam
    d = n._first_index(title, n.DESIGN_DOMAIN_WORDS)
    f = n._first_index(title, n.TECH_FUNCTION_WORDS)
    if d is not None and f is not None and d < f:
        return "tech"
    for kws, fam in n.TITLE_RULES:
        kept = tuple(k for k in kws if k not in drop)
        if any(k in title for k in kept):
            return fam
    return None


def _all_rows() -> list[dict]:
    con = db.connect_readonly()
    return [dict(r) for r in con.execute(
        "SELECT title, job_family, source_key FROM jobs "
        "WHERE closed_at IS NULL AND title IS NOT NULL")]


def end_insert_is_blind() -> None:
    """证明「加在第 2 层末尾改判 0」这个判据恒为 0，对任何词都成立。

    017 把这句话写进了 normalize.py 的注释，018 把它撤掉。这条命令的用途不是
    「验通过」，是把一个假判据的失效摆出来 —— 拿刻意选错的词试，改判仍是 0。
    """
    rows = _all_rows()
    titles = {r["title"] for r in rows}
    base = {t: n.family_from_title(t) for t in titles}
    print("刻意选错的词，加在第 2 层末尾：")
    print(f"{'词 → 族':<20}{'救回行':>8}{'改判行':>8}   结论")
    for word, fam in (("实习生", "legal"), ("运营", "tech"),
                      ("设计", "finance"), ("产品", "hr")):
        new = {t: _judge_with(t, (word, fam), at_end=True) for t in titles}
        resc = {t for t in titles if base[t] is None and new[t] is not None}
        chg = {t for t in titles if base[t] is not None and new[t] != base[t]}
        r_rows = sum(1 for r in rows if r["title"] in resc)
        c_rows = sum(1 for r in rows if r["title"] in chg)
        verdict = "判据没拦住" if c_rows == 0 else f"**拦住了**"
        print(f"{word + ' → ' + fam:<20}{r_rows:>8}{c_rows:>8}   {verdict}")
    print("\n四行改判全是 0 —— 因为第 2 层首命中即返回，能走到末尾的标题本来就判 None。")
    print("`实习生→legal` 救回上百行、改判 0，是这个判据最直白的反例。")
    print("替代判据见 `db-effect`：改到库级，那一层的值域不是常数。")


def rescued(*words: str) -> None:
    """018 这 30 个词各自救回了什么。不带参数打汇总，带词名打明细。

    判据 2 的载体。采纳一个词的理由不是「救回条数多」，是**读过它救回的是什么** ——
    被否决的 16 个词全部通过了「改判 0」，栽在这一步。所以明细必须能一条命令打出来
    供反驳，而不是写在方案正文里。
    """
    rows = _all_rows()
    titles = {r["title"] for r in rows}
    stem_of = {t: s for s, ts in _stem_map().items() for t in ts}
    live = {w for w, _ in NEW_WORDS_018}
    drift = [t for t in titles if _judge_without(t, set()) != n.family_from_title(t)]
    if drift:
        print(f"**试算与 normalize.py 不一致** {len(drift)} 条：{drift[:3]}")
        return
    without = {t: _judge_without(t, live) for t in titles}
    now = {t: n.family_from_title(t) for t in titles}

    if words:
        for w in words:
            fam = dict((a, b) for a, b in NEW_WORDS_018).get(w)
            if fam is None:
                rj = {a: c for a, _, c in REJECTED_018}
                print(f"「{w}」不在采纳表里。" +
                      (f"它在否决表里，理由：{rj[w]}" if w in rj else "也不在否决表里。"))
                continue
            hit = sorted({stem_of.get(t, t) for t in titles
                          if without[t] is None and w in t and now[t] == fam})
            print(f"\n「{w}」→ {fam}   救回 {len(hit)} 个岗位型")
            for s in hit:
                print(f"    {s}")
        return

    # 每个词打**两个**数，因为它们能差很远，而只看第一个会留下不出力的词：
    #   归因：全摘 30 个词时判不出、且标题里含这个词 —— 会重复计数。
    #         `备件仓储物流培训生` 同时含 备件/物流，两个词都记一笔。
    #   边际：只摘这一个词，判定从有族掉回 None —— 这个词自己挣的。
    # `仓储` 归因 1 行、边际 0 行，就是这么被抓出来删掉的。
    print(f"{'词 → 族':<18}{'归因行':>8}{'边际行':>8}{'边际岗位型':>12}")
    tot_rows: set[str] = set()
    dead = []
    for w, fam in NEW_WORDS_018:
        t_hit = {t for t in titles if without[t] is None and w in t and now[t] == fam}
        tot_rows |= t_hit
        nr = sum(1 for r in rows if r["title"] in t_hit)
        marg = {t for t in titles
                if now[t] is not None and _judge_without(t, {w}) is None}
        mr = sum(1 for r in rows if r["title"] in marg)
        ms = {stem_of.get(t, t) for t in marg} & set(_stem_map())
        if not marg:
            dead.append(w)
        print(f"{w + ' → ' + fam:<18}{nr:>8}{mr:>8}{len(ms):>12}")
    if dead:
        print(f"\n**边际为 0 的词**：{dead} —— 判得对但一条都不多救，是净风险，删掉")
    else:
        print(f"\n30 个词边际都 ≥1 行（`仓储` 曾是 0，已删，见 ZERO_MARGIN_018）")
    # 合计不是逐行相加：一条标题可能含两个新词（`结构` 和 `材料` 会同时命中
    # `结构材料实习生`），逐行相加会把它数两次。合计要在**并集**上数。
    all_r = sum(1 for r in rows if r["title"] in tot_rows)
    # 岗位型必须**限定在基数集合内**再数。不限定会数出 116，而基数是 399 里的 110：
    # 多出来的 6 个（腾讯 `…系统研究` 那批）判定原本是 None，但库里 job_family
    # 已经有源站兜底的 tech，所以它们压根不在 `_stem_map()` 收的那 399 里。
    # 「救回 110/399」和「救回 116」不是同一个集合上的数，相减会得出错的残留数。
    base = _stem_map()
    all_s = {stem_of.get(t, t) for t in tot_rows} & set(base)
    outside = len({stem_of.get(t, t) for t in tot_rows}) - len(all_s)
    print(f"\n合计（并集，非逐行相加）：{all_r} 行 / {len(all_s)} 个岗位型")
    print(f"另有 {outside} 个岗位型在基数之外（库里已有源站族，不在 399 里），不计入")
    print(f"基数 {len(base)} 个岗位型，剩 {len(base) - len(all_s)} 个判不出 "
          f"（{(len(base) - len(all_s)) / len(base):.1%}）")
    fams = collections.Counter(now[t] for t in tot_rows)
    print(f"救回的族分布 {dict(fams.most_common())}")
    print(f"\n否决的 {len(REJECTED_018)} 个词（加了会怎样）：")
    for w, fam, why in REJECTED_018:
        assert w not in live, f"{w} 同时在采纳表和否决表里"
        print(f"  {w + ' → ' + fam:<18}{why}")


def db_effect() -> None:
    """库级效果 —— 018 唯一的放行闸门。

    为什么不用规则层改判：规则层之下还隔着源站族兜底（`family_from_title` 返回
    None 时 ingest 用源站给的族）。所以「规则层判定没变」不等于「库里那一列没变」：
    一个词把 None 变成 tech，而库里那行存的是源站给的 product，这就是一次改族，
    规则层的 diff 结构上看不见它。
    """
    rows = _all_rows()
    titles = {r["title"] for r in rows}
    live = {w for w, _ in NEW_WORDS_018}
    without = {t: _judge_without(t, live) for t in titles}
    now = {t: n.family_from_title(t) for t in titles}

    add = [r for r in rows if r["job_family"] is None and now[r["title"]] is not None]
    over = [r for r in rows if r["job_family"] is not None
            and now[r["title"]] is not None
            and now[r["title"]] != r["job_family"]]
    rule = [r for r in rows if without[r["title"]] is not None
            and now[r["title"]] != without[r["title"]]]
    print(f"库级新增（None → 某族）        {len(add):>5} 行")
    print(f"库级改族（已有族 → 另一个族）  {len(over):>5} 行   ← 判据，必须是 0")
    print(f"规则层改判                     {len(rule):>5} 行")
    for r in over[:10]:
        print(f"    {r['job_family']} → {now[r['title']]}   {r['title']}")
    print()
    if over:
        print("**放行失败**：有行的既有族被改掉了。逐条看是新词判对还是判错，"
              "判错就把那个词挪进 REJECTED_018。")
    else:
        print("放行通过。这个判据能红，验过：把 `内容→operations` 加回词表是 5 行，"
              "再加 `治理` 是 7 行。见 scripts/mutate_018.sh。")


# 只认这三个字符做组织分隔符。**故意不含** `/`、`|`、`_`：
#   `前端/后端开发实习生`  斜杠是「或」，不是「岗位-部门」
#   `数据_平台`            下划线在几条标题里是排版，不表示组织边界
# 加宽到 `-—－_|｜/` 量过：带分隔符的标题从 5980 涨到 6004，变族数从 86 变 88，
# 多出来的 2 条是斜杠误切。窄口径是 issue #13 记的那个口径。
SEPS = "-—－"


def _split_last(title: str) -> tuple[str, str] | None:
    """在**最后一个**分隔符上切开，返回（头段, 尾段）。没有分隔符返回 None。

    必须是最后一个，不是第一个。第一次写这个探针时 docstring 写着「最后一个」、
    代码切的是第一个，把结论从 602 条虚报成 867 条 —— 探针自己的缺陷穿着被测对象
    的症状出场。见 [[probe-defects-look-like-target-failures]]。
    """
    idx = max((title.rfind(s) for s in SEPS), default=-1)
    if idx <= 0 or idx >= len(title) - 1:
        return None
    return title[:idx].strip(), title[idx + 1:].strip()


def tail_steals() -> None:
    """issue #13 的复现：部门尾段抢走岗位族。

    形状：`岗位-部门`，而定族的词只出现在**部门**那一侧。`大模型算法工程师-内容
    治理` 判 tech 是对的，但 `运营策略-大模型内容治理` 判 tech 是错的 —— 后者是
    运营岗，tech 是部门名贡献的。

    哪一侧是组织单位不能靠肉眼定，要看**复用度**：组织单位会在很多不同的对方身上
    重复出现。bytedance 写 `岗位-部门`（尾段复用中位数 15、头段 1），tencent 写
    `组织—课题`（尾段 1、头段 17）—— 同一条判据在两个源上方向相反。
    """
    rows = _all_rows()
    titles = sorted({r["title"] for r in rows})
    src_of: dict[str, str] = {}
    for r in rows:
        src_of.setdefault(r["title"], r["source_key"])

    split = {t: _split_last(t) for t in titles}
    have = {t: v for t, v in split.items() if v}
    print(f"在架 distinct 标题 {len(titles)}，含分隔符 {len(have)}")

    # 复用度：一个段在多少个**不同的对方**身上出现过。
    head_partners: dict[str, set[str]] = collections.defaultdict(set)
    tail_partners: dict[str, set[str]] = collections.defaultdict(set)
    for t, (h, tl) in have.items():
        head_partners[h].add(tl)
        tail_partners[tl].add(h)

    changed = []
    for t, (h, _tl) in have.items():
        full, head = n.family_from_title(t), n.family_from_title(h)
        if full != head:
            changed.append((t, full, head))
    to_none = [c for c in changed if c[2] is None]
    to_other = [c for c in changed if c[2] is not None]
    print(f"去掉尾段后判定变化 {len(changed)} 条："
          f"变 None {len(to_none)} 条、变成另一个族 {len(to_other)} 条")
    print("（变 None 的那批不是缺陷：说明整条标题里只有尾段带信息，"
          "去掉就没词可判了，本来也没判错）\n")

    # 真缺陷 vs 现判正确 —— **两种拆法都打出来**，因为它们不完全一致，而那个不一致
    # 本身是要看见的东西，不是要藏起来的东西。
    #
    # 拆法 1（issue #13 记的那个）：按源。bytedance 写 `岗位-部门`，尾段是组织，
    #   所以尾段定族=判错；tencent 写 `组织—课题`，头段才是组织，尾段定族=判对。
    # 拆法 2（结构判据）：比两侧复用度。不依赖源名，能推广到没见过的新源。
    by_src = collections.defaultdict(list)
    for c in to_other:
        by_src[src_of[c[0]]].append(c)
    print(f"拆法 1 · 按源（issue #13 的口径）：")
    for k, v in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        shape = "岗位-部门 → 尾段定族即判错" if "bytedance" in k else "组织—课题 → 尾段是课题，判对"
        nr = sum(1 for r in rows if r["title"] in {c[0] for c in v})
        print(f"  {k:<26}{len(v):>4} 条 / {nr:>3} 行   {shape}")

    real = [c for c in to_other
            if len(tail_partners[have[c[0]][1]]) >= len(head_partners[have[c[0]][0]])]
    fake = [c for c in to_other if c not in real]
    print(f"\n拆法 2 · 按两侧复用度：尾段更像组织 {len(real)} 条、头段更像组织 {len(fake)} 条")
    src_real = {c[0] for c in by_src.get("feishu:bytedance:campus", [])}
    reuse_real = {c[0] for c in real}
    print(f"  两种拆法不一致 {len(src_real ^ reuse_real)} 条 —— 结构判据比按源保守，"
          f"漏的都是尾段只带过 1~2 个头段的部门（`销售运营管理平台` 这类小部门）")
    print(f"  取哪个：修 issue #13 用**按源**那个数（65 条 / 83 行），"
          f"因为修法要落在源的命名习惯上；结构判据用来判断新源属于哪种形状。\n")

    print("抢走族的尾段词 top：")
    stealer = collections.Counter()
    for t, full, head, *_ in real:
        _h, tl = have[t]
        stealer[tl.split("（")[0][:12]] += 1
    for w, c in stealer.most_common(10):
        print(f"  {c:>3}  {w}")
    def reuse(t: str) -> tuple[int, int]:
        h, tl = have[t]
        return len(tail_partners[tl]), len(head_partners[h])

    print("\n两侧复用中位数（这是「哪侧是组织单位」的证据，按源分组看）：")
    mid = lambda xs: sorted(xs)[len(xs) // 2]
    for k, v in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        tl_r = [reuse(c[0])[0] for c in v]
        hd_r = [reuse(c[0])[1] for c in v]
        print(f"  {k:<26}尾段复用 {mid(tl_r):>3}、头段复用 {mid(hd_r):>3}")
    print("  两个源方向相反 —— 这就是为什么同一条「尾段定族」判据在 bytedance 上"
          "是缺陷、在 tencent 上是正确。")

    print("\n例子（bytedance，族是部门名定的）：")
    bd = by_src.get("feishu:bytedance:campus", [])
    for t, full, head in sorted(bd, key=lambda c: -reuse(c[0])[0])[:7]:
        tr, hr = reuse(t)
        print(f"  判 {full} 应≈{head}  尾复用{tr}/头复用{hr}  {t}")
    print("\n口径提醒：这批和 issue #8 的 532 条判不出**交集为 0** —— #8 是「判不出」，"
          "这里是「判出来但判错」，是两个独立缺陷。修法见 issue #13。")


COMMANDS = {
    "by-source": by_source,
    "stale": stale,
    "city-collapse": city_collapse,
    "vocab-curve": vocab_curve,
    "candidates": candidates,
    "noise": noise,
    "end-insert-is-blind": end_insert_is_blind,
    "rescued": rescued,
    "db-effect": db_effect,
    "tail-steals": tail_steals,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("子命令：")
        for name, fn in COMMANDS.items():
            print(f"  {name:<20}{(fn.__doc__ or '').splitlines()[0]}")
        print("\n`rescued` 可以带词名：rescued 系统 售后")
        return 1 if len(sys.argv) != 1 else 0
    print(f"=== {' '.join(sys.argv[1:])} ===\n")
    # 只有 `rescued` 收位置参数。多给的参数不能静默丢掉 —— 那会让
    # `db-effect 内容` 看起来像「量了内容」，实际量的是全表。
    extra = sys.argv[2:]
    fn = COMMANDS[sys.argv[1]]
    if extra and fn is not rescued:
        print(f"`{sys.argv[1]}` 不接受参数，收到 {extra}")
        return 1
    fn(*extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
