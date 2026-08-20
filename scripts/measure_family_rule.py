"""复现方案 015 引用的每个数字。只读真库，不写。

为什么是脚本而不是方案里的 heredoc：方案 §0 每一行「已核实」都得附一条能跑的
命令，而这些测量各有二三十行。埋在 markdown 里的长 heredoc 没人会真去跑 ——
plan 001 §12 引用一个不存在的测试就是这么来的。

两个词表**不写死**，按「第 2 层 design 组 − {设计, 美术}」和
「第 2 层 tech 组 − {数据, 安全, 硬件, 模型}」现算。这样实现落地后改了第 2 层，
这里的数字跟着动，不会悄悄和代码脱节。实现完之后 `self-check` 会断言现算的
词表和 `normalize.py` 里的常量相等。

用法：
    cd <repo> && PYTHONPATH="$PWD" .venv/bin/python scripts/measure_family_rule.py <子命令>
不带参数列出全部子命令。
"""
from __future__ import annotations

import collections
import copy
import json
import pathlib
import sys
import tempfile

from jobagent import db
import jobagent.normalize as n

# 从第 2 层现算，不写死。排掉的词各有反例，见 word_lists()。
FUNC_EXCLUDE = ("数据", "安全", "硬件", "模型")
DOM_EXCLUDE = ("设计", "美术")


def _group(fam: str, rules=None) -> tuple[str, ...]:
    return next(kw for kw, f in (rules or n.TITLE_RULES) if f == fam)


def _dom() -> tuple[str, ...]:
    return tuple(w for w in _group("design") if w not in DOM_EXCLUDE)


def _func() -> tuple[str, ...]:
    return tuple(w for w in _group("tech") if w not in FUNC_EXCLUDE)


def _titles() -> list[str]:
    con = db.connect_readonly()
    return [r[0] for r in con.execute(
        "SELECT DISTINCT title FROM jobs WHERE title IS NOT NULL")]


def _first_idx(title: str, words) -> int | None:
    """命中最靠前那个词的下标。**返回 0 是合法命中**，调用方不许用真值判断。"""
    hits = [title.index(w) for w in words if w in title]
    return min(hits) if hits else None


def _fires(title: str, dom, func, *, ordered: bool = True) -> bool:
    d, f = _first_idx(title, dom), _first_idx(title, func)
    if d is None or f is None:
        return False
    return d < f if ordered else True


def _span(title: str, dom, func) -> str:
    """域词起点到职能词终点的最小连续子串 —— 粘连短语路线要枚举的那个东西。"""
    d = _first_idx(title, dom)
    fw = min(((title.index(w), w) for w in func if w in title))[1]
    return title[d:title.index(fw) + len(fw)]


def _judge(title: str, *, dom=None, func=None, before_compound=False,
           rules=None, ordered=True) -> str | None:
    """三层判定 + 可选的新层。dom/func 为 None 表示不加新层（= 现状）。"""
    rules = rules if rules is not None else n.TITLE_RULES
    on = dom is not None and func is not None
    if any(m in title for m in n.TECH_MARKERS):
        return "tech"
    if on and before_compound and _fires(title, dom, func, ordered=ordered):
        return "tech"
    for kws, fam in n.COMPOUND_RULES:
        if any(k in title for k in kws):
            return fam
    if on and not before_compound and _fires(title, dom, func, ordered=ordered):
        return "tech"
    for kws, fam in rules:
        if any(k in title for k in kws):
            return fam
    return None


def _diff(titles, base, **kw) -> list[tuple[str | None, str | None, str]]:
    out = []
    for t in titles:
        new = _judge(t, **kw)
        if new != base[t]:
            out.append((base[t], new, t))
    return out


def _summary(rows) -> dict:
    return dict(collections.Counter(f"{a} → {b}" for a, b, _ in rows))


TARGETS = (
    "3D视觉研发实习生-智能创作",
    "多媒体C++研发（AI创作方向）实习生-抖音用户产品基础",
    "多媒体图形/图像研发实习生-抖音-智能创作",
    "多媒体图形/图像研发实习生-抖音用户产品基础",
    "多媒体处理平台后端实习生-音视频技术",
    "多媒体客户端研发实习生-抖音-智能创作",
)

def root_cause() -> None:
    """A：第 1 层是「粘连短语 OR」，表达不了「域词 + 职能词」。"""
    print("COMPOUND_RULES 的判定语义（normalize.py:79-81）：any(k in title for k in kws)")
    print("→ 每个 k 是一个粘连短语，整词匹配。四条规则：")
    for kws, fam in n.COMPOUND_RULES:
        print(f"    {fam:<10} {kws}")
    dom, func = _dom(), _func()
    print("\n目标标题里域词和职能词之间隔着什么：")
    for t in TARGETS:
        d, f = _first_idx(t, dom), _first_idx(t, func)
        dw = min(((t.index(w), w) for w in dom if w in t))[1]
        fw = min(((t.index(w), w) for w in func if w in t))[1]
        gap = t[d + len(dw):f]
        note = "  ← 相邻，粘连短语能命中" if gap == "" else ""
        print(f"    {dw}[{d}] … {gap!r} … {fw}[{f}]   {t}{note}")

    print("\n=== 粘连短语路线要枚举多少个 ===")
    print("（这一段纠正了方案初稿的断言「一条都不中」—— 6 条里有 2 条相邻）")
    spans6 = sorted({_span(t, dom, func) for t in TARGETS})
    print(f"  目标 6 条标题 → 需要 {len(spans6)} 个不同短语: {spans6}")
    titles = _titles()
    base = {t: n.family_from_title(t) for t in titles}
    fires = [t for t in titles if _fires(t, dom, func)]
    spans_all = sorted({_span(t, dom, func) for t in fires})
    print(f"  全部 {len(fires)} 次触发 → 需要 {len(spans_all)} 个不同短语：")
    for s in spans_all:
        fams = {base[t] for t in fires if _span(t, dom, func) == s}
        junk = "  ← 不是岗位语义，是字符串排布的意外" if any(
            c in s for c in "）(-— ") else ""
        print(f"    {s!r:<26} 现判 {fams}{junk}")
    print(f"\n  对比：AND 规则用 {len(dom)}+{len(func)}={len(dom) + len(func)} 个词覆盖全部写法。")
    print("  枚举路线每来一个新写法就要加一条，而写法集合数不出来 —— 必漏。")

    orig = copy.deepcopy(n.COMPOUND_RULES)
    n.COMPOUND_RULES = [(tuple(spans6), "tech")] + orig
    rows = [(base[t], n.family_from_title(t), t)
            for t in titles if n.family_from_title(t) != base[t]]
    n.COMPOUND_RULES = orig
    print(f"\n  枚举 {len(spans6)} 个短语当 tech 加到第 1 层：改判 {len(rows)} 条")
    print("  → 今天的 6 条全中、无副作用。它不是「错」，是「不长期成立」。")

    print("\n=== 图形/图像 单独出现时判什么（说明它们不是致病词，方案 §8）===")
    for t in ("图形研发实习生", "图像处理实习生", "图形学工程师"):
        print(f"    {str(n.family_from_title(t)):>8}  {t}")


def reorder_cost() -> None:
    """B：便宜修法（第 2 层 tech 组前移）的代价。两个插入点两个数。"""
    titles = _titles()
    base = {t: n.family_from_title(t) for t in titles}
    orig = copy.deepcopy(n.TITLE_RULES)
    tech_row = next(r for r in orig if r[1] == "tech")
    rest = [r for r in orig if r[1] != "tech"]
    for label, rules in [
        ("插到最前面（连 运营 一起抢）", [tech_row] + rest),
        ("插在 operations 之后 / design 之前", [rest[0], tech_row] + rest[1:]),
    ]:
        rows = _diff(titles, base, rules=rules)
        print(f"\n{label}：改判 {len(rows)} 条 distinct")
        for k, v in sorted(_summary(rows).items(), key=lambda x: -x[1]):
            print(f"    {k}  {v}")
    print("\n→ 测试 docstring 里的 99 对应后者。差的 128 全是 operations → tech。")


def drop_domain() -> None:
    """C：另一种修法 —— 把域词从第 2 层 design 组摘掉。会造 None。"""
    titles = _titles()
    counts = {r["title"]: r["n"] for r in db.connect_readonly().execute(
        "SELECT title, COUNT(*) n FROM jobs WHERE title IS NOT NULL GROUP BY title")}
    base = {t: n.family_from_title(t) for t in titles}
    orig = copy.deepcopy(n.TITLE_RULES)
    for label, drop in [("B 摘掉 多媒体", {"多媒体"}), ("C 摘掉 多媒体+视觉", {"多媒体", "视觉"})]:
        rules = [(tuple(w for w in kws if w not in drop) if fam == "design" else kws, fam)
                 for kws, fam in orig]
        rows = _diff(titles, base, rules=rules)
        fixed = sum(1 for t in TARGETS if _judge(t, rules=rules) == "tech")
        nones = [(a, t) for a, b, t in rows if b is None]
        print(f"\n=== {label} ===")
        print(f"  改判 {len(rows)} 条 distinct：{_summary(rows)}")
        print(f"  修好目标 {fixed}/{len(TARGETS)}")
        print(f"  掉成 None：{len(nones)} 条标题 / {sum(counts[t] for _, t in nones)} 行")
        for a, t in sorted(nones):
            print(f"     {a} → None   {t}    ← 没有族，按族筛永远取不到")
    dom, func = _dom(), _func()
    rows = _diff(titles, base, dom=dom, func=func)
    fixed = sum(1 for t in TARGETS if _judge(t, dom=dom, func=func) == "tech")
    print(f"\n=== A 加位置层（选定）===")
    print(f"  改判 {len(rows)} 条 distinct：{_summary(rows)}")
    print(f"  修好目标 {fixed}/{len(TARGETS)}")
    print(f"  掉成 None：{sum(1 for _, b, _ in rows if b is None)} 条")


CONSTRUCTED = {
    # 职能词在前、域词在后 —— 技术词是修饰语，职能仍是设计。带顺序不该触发。
    "职能在前（应保持 design）": (
        "后台动画设计师", "客户端UI动效设计", "后端图形界面GUI设计", "前端交互设计",
    ),
    # 显式设计复合短语 + 技术职能词 —— 复合层必须先赢。
    "复合短语在场（应保持 design）": (
        "交互设计前端实习生", "视觉设计客户端实习生", "体验设计后台实习生", "交互设计师",
    ),
    # 域词在前、职能在后 —— 目标形状。
    "域在前（应判 tech）": (
        "视觉研发实习生", "多媒体后端实习生", "动效引擎客户端研发",
    ),
}


def variants() -> None:
    """D：带顺序 vs 纯 AND。真数据上等价，构造用例上纯 AND 错。"""
    titles = _titles()
    base = {t: n.family_from_title(t) for t in titles}
    dom, func = _dom(), _func()
    for label, ordered in [("带顺序 d<f", True), ("纯 AND", False)]:
        rows = _diff(titles, base, dom=dom, func=func, ordered=ordered)
        fires = sum(1 for t in titles if _fires(t, dom, func, ordered=ordered))
        print(f"{label:<12} 触发 {fires:>3}   改判 {len(rows)} 条   {_summary(rows)}")
    print("\n真数据上改判集合是否相同:",
          {t for _, _, t in _diff(titles, base, dom=dom, func=func, ordered=True)}
          == {t for _, _, t in _diff(titles, base, dom=dom, func=func, ordered=False)})
    print("\n构造用例（真库里没有这些标题，只能这么验）：")
    for group, cases in CONSTRUCTED.items():
        print(f"\n  --- {group} ---")
        print(f"  {'标题':<24} {'现判':>8} {'带顺序':>8} {'纯AND':>8}")
        for t in cases:
            a = _judge(t, dom=dom, func=func, ordered=True)
            b = _judge(t, dom=dom, func=func, ordered=False)
            flag = "   ← 分歧" if a != b else ""
            print(f"  {t:<24} {str(n.family_from_title(t)):>8} {str(a):>8} {str(b):>8}{flag}")


def word_lists() -> None:
    """E：两个词表的边界各有反例。"""
    titles = _titles()
    base = {t: n.family_from_title(t) for t in titles}
    dom, func = _dom(), _func()
    print(f"DESIGN_DOMAIN_WORDS = {dom}")
    print(f"TECH_FUNCTION_WORDS = {func}")
    print(f"交集: {set(dom) & set(func)}   （必须为空，见方案 §4 末段）")
    keep = {t for _, _, t in _diff(titles, base, dom=dom, func=func)}
    for label, d2, f2 in [
        ("职能词表补回 数据/安全/硬件/模型", dom, func + FUNC_EXCLUDE),
        ("域词表补回 设计/美术", dom + DOM_EXCLUDE, func),
    ]:
        rows = _diff(titles, base, dom=d2, func=f2)
        print(f"\n=== {label} ===  改判 {len(rows)} 条  {_summary(rows)}")
        for a, b, t in sorted(rows, key=lambda r: r[2]):
            print(f"    {a} → {b}   {t}" + ("   ← 新增" if t not in keep else ""))
    print("\n构造用例（域词表补回 设计/美术 在真数据上无差别，只能这么验）：")
    for t in ("设计师（前端方向）", "美术资源-客户端支持", "设计中台后端实习生"):
        a = _judge(t, dom=dom, func=func)
        b = _judge(t, dom=dom + DOM_EXCLUDE, func=func)
        print(f"    {t:<22} 选定={str(a):>7}  补回={str(b):>7}" + ("   ← 分歧" if a != b else ""))


def layer_position() -> None:
    """G/K：新层放 COMPOUND_RULES 前还是后。真数据无差别，构造用例有。"""
    titles = _titles()
    dom, func = _dom(), _func()
    compound_design = next(kw for kw, f in n.COMPOUND_RULES if f == "design")
    collide = [t for t in titles
               if _fires(t, dom, func) and any(c in t for c in compound_design)]
    print(f"真数据上「新层触发 且 含 design 复合短语」的标题: {len(collide)} 条")
    for t in collide:
        print(f"    {t}")
    print("→ 0 条则位置在当前数据上无差别，只能靠构造用例钉住。\n")
    print(f"  {'标题':<24} {'现判':>8} {'放前':>8} {'放后':>8}")
    for cases in CONSTRUCTED.values():
        for t in cases:
            a = _judge(t, dom=dom, func=func, before_compound=True)
            b = _judge(t, dom=dom, func=func, before_compound=False)
            flag = "   ← 分歧" if a != b else ""
            print(f"  {t:<24} {str(n.family_from_title(t)):>8} {str(a):>8} {str(b):>8}{flag}")


def stock_impact() -> None:
    """H：存量多少行、谁的、family_first_seen 会不会误报。"""
    con = db.connect_readonly()
    titles = _titles()
    dom, func = _dom(), _func()
    tg = [t for t in titles if _fires(t, dom, func) and n.family_from_title(t) != "tech"]
    q = ",".join("?" * len(tg))
    rows = con.execute(
        f"SELECT id, company, source_key, recruit_type, job_family, closed_at, title "
        f"FROM jobs WHERE title IN ({q}) ORDER BY id", tg).fetchall()
    print(f"改判 {len(tg)} 条 distinct 标题 → 受影响 {len(rows)} 行")
    print(f"存族分布: {dict(collections.Counter(r['job_family'] for r in rows))}")
    print(f"在架/下线: {dict(collections.Counter('下线' if r['closed_at'] else '在架' for r in rows))}")

    print("\n=== family_first_seen 会不会误报 ===")
    print("判据不是「改判几条」，是「该公司该 recruit_type 下 tech 开放数是否为 0」")
    for company, rtype in sorted({(r["company"], r["recruit_type"]) for r in rows}):
        n_tech = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE company=? AND recruit_type IS ? "
            "AND job_family='tech' AND closed_at IS NULL", (company, rtype)).fetchone()[0]
        verdict = "**会**误报" if n_tech == 0 else "不会误报"
        print(f"  {company} / {rtype}：已有开放 tech {n_tech} 条 → {verdict}")

    print("\n=== 反方向：design 族会不会被清空 ===")
    for (company,) in sorted({(r["company"],) for r in rows}):
        tot = con.execute("SELECT COUNT(*) FROM jobs WHERE company=? AND "
                          "job_family='design' AND closed_at IS NULL", (company,)).fetchone()[0]
        aff = sum(1 for r in rows if r["company"] == company and not r["closed_at"])
        print(f"  {company}：开放 design {tot} 条，改走 {aff} 条 → 剩 {tot - aff}")

    print("\n=== 明细 ===")
    for r in rows:
        print(f"  id={r['id']:>5} {r['company']} {r['source_key']} {r['recruit_type']} {r['title']}")


def sync_events() -> None:
    """I：存量交给 sync 收拾会产出什么。建临时库，不碰真库。"""
    from jobagent import ingest
    from jobagent.adapters.base import RawJob

    class FA:
        company = "测试公司"
        system = "self_built"
        entry_url = "https://e.test"

        def __init__(self, jobs, key="feishu:nio:campus"):
            self._jobs, self.source_key = jobs, key

        def fetch(self):
            return self._jobs

    def fresh():
        c = db.connect(pathlib.Path(tempfile.mkdtemp()) / "t.db")
        db.init(c)
        return c

    def events(c):
        return [(r["kind"], json.loads(r["payload"] or "{}").get("diff", "∅"))
                for r in c.execute("SELECT kind, payload FROM events ORDER BY id")]

    base = dict(external_id="777", title="多媒体客户端研发实习生",
                raw_json={"id": "777"}, cities=["北京"], recruit_type="campus")
    url = "https://nio.jobs.feishu.cn/campus/position/777"

    print("=== 情形 1：先例 repair_apply_url 只改列不碰指纹 ===")
    c = fresh()
    ingest.sync(c, FA([RawJob(**base, job_family="tech", apply_url=url)]))
    fp0 = c.execute("SELECT fingerprint FROM jobs").fetchone()[0]
    ingest.repair_apply_url(c, apply=True)
    fp1 = c.execute("SELECT fingerprint FROM jobs").fetchone()[0]
    print(f"  修完指纹是否变: {fp0 != fp1}（没变 = 只改了列）")
    ingest.sync(c, FA([RawJob(**base, job_family="tech", apply_url=url + "/detail")]))
    print(f"  ▶ 再同步产出: {events(c)[1:]}")
    print("  → diff 为 {} 的 job_updated。这是既存 committed 代码的独立缺陷，")
    print("    单独开 issue，本方案不动（方案 §8）。")

    print("\n=== 情形 2：本方案 —— 不写修复命令，交给 sync ===")
    c = fresh()
    old = RawJob(**base, job_family="design", apply_url=url + "/detail")
    new = RawJob(**base, job_family="tech", apply_url=url + "/detail")
    ingest.sync(c, FA([old]))
    ingest.sync(c, FA([new]))
    print(f"  ▶ 第二轮产出: {events(c)}")
    print(f"  库里存族: {c.execute('SELECT job_family FROM jobs').fetchone()[0]}")
    ingest.sync(c, FA([new]))
    print(f"  ▶ 第三轮产出（应无新事件，证明一次性）: {events(c)[3:]}")

    print("\n=== 情形 3：列和指纹一起改（全静默，本方案不采用）===")
    c = fresh()
    ingest.sync(c, FA([old]))
    c.execute("UPDATE jobs SET job_family=?, fingerprint=? WHERE external_id=?",
              ("tech", ingest._fp(new), "777"))
    c.commit()
    ingest.sync(c, FA([new]))
    print(f"  ▶ 再同步产出: {events(c)}")


def title_nulls() -> None:
    """J：入参 title 的空值形状。"""
    con = db.connect_readonly()
    print("schema.sql:61  title TEXT NOT NULL")
    print("  真库 title IS NULL :",
          con.execute("SELECT COUNT(*) FROM jobs WHERE title IS NULL").fetchone()[0])
    print("  真库 title = ''    :",
          con.execute("SELECT COUNT(*) FROM jobs WHERE title=''").fetchone()[0])
    print("  family_from_title('') =", repr(n.family_from_title("")))
    try:
        n.family_from_title(None)  # type: ignore[arg-type]
    except Exception as e:
        print(f"  family_from_title(None) → {type(e).__name__}: {e}")
        print("  → 签名是 title: str，调用方违约当场炸，不静默判 None")
    print("\n_first_idx 返回 0 是合法命中，不许用真值判断：")
    for t in TARGETS:
        d = _first_idx(t, _dom())
        print(f"    d={d}  bool(d)={bool(d)}   {t}")
    print("  → 目标里多数 d==0。写 `if d and f` 会漏掉整类。")


def self_check() -> None:
    """实现落地后跑：断言现算词表 == normalize.py 里的常量。"""
    missing = [name for name in ("DESIGN_DOMAIN_WORDS", "TECH_FUNCTION_WORDS")
               if not hasattr(n, name)]
    if missing:
        print(f"normalize.py 还没有 {missing} —— 方案未实现，self-check 跳过")
        return 1
    ok = True
    for name, computed in (("DESIGN_DOMAIN_WORDS", _dom()), ("TECH_FUNCTION_WORDS", _func())):
        actual = tuple(getattr(n, name))
        same = set(actual) == set(computed)
        ok &= same
        print(f"{name}: {'一致' if same else '**不一致**'}")
        print(f"  现算 {computed}")
        print(f"  代码 {actual}")
        if not same:
            print(f"  只在现算里: {set(computed) - set(actual)}")
            print(f"  只在代码里: {set(actual) - set(computed)}")
    return 0 if ok else 1


COMMANDS = {
    "root-cause": ("A", root_cause),
    "reorder-cost": ("B", reorder_cost),
    "drop-domain": ("C", drop_domain),
    "variants": ("D", variants),
    "word-lists": ("E/F", word_lists),
    "layer-position": ("G/K", layer_position),
    "stock-impact": ("H", stock_impact),
    "sync-events": ("I", sync_events),
    "title-nulls": ("J", title_nulls),
    "self-check": ("-", self_check),
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("子命令（括号里是方案 015 §9 的编号）：")
        for name, (tag, fn) in COMMANDS.items():
            first = (fn.__doc__ or "").splitlines()[0]
            print(f"  {name:<16} [{tag:>3}]  {first}")
        return 1 if len(sys.argv) != 1 else 0
    tag, fn = COMMANDS[sys.argv[1]]
    print(f"=== {sys.argv[1]}  （方案 015 §9 命令 {tag}）===\n")
    return fn() or 0


if __name__ == "__main__":
    raise SystemExit(main())
