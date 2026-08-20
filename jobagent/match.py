"""M4 匹配订阅：画像 × 岗位 → 推什么给用户。

规则筛选，不做打分模型。MVP 阶段规则的可解释性比准确率更重要——
用户问「为什么这个岗位没推给我」，得答得出来。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

import yaml

from .normalize import any_city_ok, grad_years_from_title, parse_grad_years

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "profile.yaml"


class Verdict(NamedTuple):
    """三态判定。

    hit / miss 之外必须有第三态。源站经常不写届别、不写城市，这种岗位既不能
    当命中（用户会收到一堆不相关的），也不能当不命中——原先就是后者，于是
    「2026-2027年毕业」「全国」这类岗位被静默丢掉，用户完全不知道它存在过。
    """

    state: str                       # hit / unknown / miss
    reason: str
    unknowns: tuple[str, ...] = ()   # 给人看的，文案会改
    missing: tuple[str, ...] = ()    # 给代码看的，键要稳定

    @property
    def ok(self) -> bool:
        """确定命中。"""
        return self.state == "hit"

    @property
    def worth_showing(self) -> bool:
        """该不该出现在用户眼前。信息不全也要出现，只是要标注出来。"""
        return self.state in ("hit", "unknown")


# 「缺哪一维」的机读键。和 unknowns 里的中文文案分开：文案随时会改，
# 这些键是 --allow-missing 的参数值和测试的断言对象，改了就是破坏接口。
MISSING_DIMS = ("job_family", "recruit_type", "grad_year", "cities")


def city_list(job: dict) -> list[str]:
    """取岗位城市。库里存的是 JSON 字符串，测试和适配器直接给 list，都要认。"""
    raw = job.get("cities")
    if isinstance(raw, str):
        try:
            return list(json.loads(raw) or [])
        except (ValueError, TypeError):
            return []          # 脏数据不该让整条 digest 崩掉
    return list(raw or [])


def load_profile(path: Path | None = None) -> dict:
    p = path or PROFILE_PATH
    if not p.exists():
        raise FileNotFoundError(f"档案不存在：{p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def classify(job: dict, intent: dict) -> Verdict:
    """岗位 × 画像 → 三态判定。

    规则顺序有意为之：先判确定性不命中（排除词、岗位族、类型），再判可能因为
    信息缺失而说不清的（届别、城市）。这样一个「写了排除词、又没写届别」的岗位
    会被判成 miss 而不是 unknown——它确定不该推，不需要让用户再看一眼。
    """
    title = job.get("title") or ""
    for kw in intent.get("exclude_keywords") or []:
        if kw in title:
            return Verdict("miss", f"命中排除词「{kw}」")

    unknowns: list[str] = []
    missing: list[str] = []

    fams = intent.get("families") or []
    if fams:
        fam = job.get("job_family")
        if not fam:
            unknowns.append("岗位族未知")
            missing.append("job_family")
        elif fam not in fams:
            return Verdict("miss", f"岗位族 {fam} 不在 {fams}")

    rtypes = intent.get("recruit_types") or []
    if rtypes:
        rtype = job.get("recruit_type")
        if not rtype:
            unknowns.append("招聘类型未知")
            missing.append("recruit_type")
        elif rtype not in rtypes:
            return Verdict("miss", f"招聘类型 {rtype} 不在 {rtypes}")

    # 届别：want 和 job 两侧都归一到两位，画像里写 "2026" 也能对上库里的 "26"。
    want_years = {str(y)[-2:] for y in (intent.get("grad_years") or [])}
    if want_years:
        job_years = parse_grad_years(job.get("grad_year"))
        from_title = False
        if job_years is None:
            # 结构化字段没给，退到标题。飞书四家的字段里根本没有届别这一列，
            # 但小鹏、蔚来的标题上明写着「【27届校招】」——标题上写着的不算「没写」。
            job_years = grad_years_from_title(title)
            from_title = job_years is not None
        if job_years is None:
            unknowns.append(f"届别未标注（原值 {job.get('grad_year') or '空'}）")
            missing.append("grad_year")
        elif not job_years:
            pass                                    # 明确不限届别
        elif not (set(job_years) & want_years):
            src = "（据标题）" if from_title else ""
            return Verdict("miss", f"届别 {job_years}{src} 不在 {sorted(want_years)}")

    want_cities = set(intent.get("cities") or [])
    if want_cities:
        job_cities = city_list(job)
        if not job_cities:
            unknowns.append("城市未标注")
            missing.append("cities")
        elif any_city_ok(job_cities):
            pass                                    # 全国 / 不限 / 远程，都算命中
        elif not (set(job_cities) & want_cities):
            return Verdict("miss", f"城市 {sorted(job_cities)} 不含目标城市")

    if unknowns:
        return Verdict(
            "unknown", "信息不全：" + "；".join(unknowns), tuple(unknowns), tuple(missing)
        )
    return Verdict("hit", "命中")


def matches(job: dict, intent: dict) -> tuple[bool, str]:
    """兼容旧签名：(是否确定命中, 原因)。

    信息不全的返回 False——它不是确定命中。要把这类岗位捞出来给用户，
    用 partition()，别把 unknown 混进 hit。
    """
    v = classify(job, intent)
    return v.ok, v.reason


def score(job: dict, intent: dict) -> int:
    """轻量优先级。只用来排序，不用来过滤。"""
    s = 0
    title = job.get("title") or ""
    for kw in intent.get("boost_keywords") or []:
        if kw in title:
            s += 10
    job_cities = city_list(job)
    hit_cities = set(job_cities) & set(intent.get("cities") or [])
    if hit_cities:
        s += 2 * len(hit_cities)
    elif any_city_ok(job_cities):
        # 「全国」算够得着，但要严格排在明确写了目标城市的岗位后面：写明「北京」
        # 的岗位确定在北京，写「全国」的实际派到哪还不知道。所以明确命中按 2
        # 计权、通配按 1，命中一个城市也压得住通配。
        s += 1
    if job.get("recruit_type") == "campus":
        s += 3   # 应届优先于实习
    return s


def partition(rows: list[dict], intent: dict) -> tuple[list[dict], list[dict]]:
    """把岗位分成 (确定命中, 信息不全)，各自按优先级排好。

    第二个列表是这次改动的重点：它原来不存在，那些岗位直接消失了。
    调用方应该把它单独展示，并带上 _why 说明缺什么，让用户自己判断。
    """
    hits: list[dict] = []
    unknown: list[dict] = []
    for r in rows:
        v = classify(r, intent)
        if v.state == "hit":
            hits.append(r)
        elif v.state == "unknown":
            unknown.append({**r, "_why": v.reason, "_missing": v.missing})
    key = lambda r: score(r, intent)                          # noqa: E731
    return sorted(hits, key=key, reverse=True), sorted(unknown, key=key, reverse=True)


def filter_jobs(
    rows: list[dict], intent: dict, allow_missing: Iterable[str] | None = None
) -> list[dict]:
    """默认只给确定命中的。allow_missing 里列出的维度，缺了也算能看。

    按维度放宽，不是一个布尔开关。原先只有 include_unknown 一个开关：一按下去
    届别、岗位族、招聘类型、城市四维同时放开，于是「族、类型、城市都已确认、只差
    届别」的 1911 条，和「连是不是运营岗都不知道」的 581 条混在同一个结果里。
    这两种「不确定」的可信度差得远，不该共用一个开关。

    allow_missing=None 与 frozenset() 同义（都只给确定命中的），不制造第三种含义。
    传全部维度 = 老的 include_unknown=True。维度键见 MISSING_DIMS。
    """
    allowed = frozenset(allow_missing or ())
    hits, unknown = partition(rows, intent)
    if not allowed:
        return hits
    return hits + [r for r in unknown if set(r["_missing"]) <= allowed]
