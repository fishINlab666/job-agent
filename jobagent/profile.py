"""画像映射层：把嵌套的 profile.yaml 摊平成表单字段。

为什么要单独一层：profile.yaml 是给人看的，按语义分组（identity /
education / narrative）；表单要的是「姓名」「手机」「学校」这种平铺字段。
M6 之前直接拿 yaml 顶层 key 去填表，而真实文件里这些 key 都在 identity.*
底下，于是每个字段都取到 None、`if name := profile.get("name")` 全部跳过、
表单一个字没填，代码还是照样点了提交。这是比匹配层丢岗位更严重的问题。

除了摊平，这一层还记住每个值**来自哪里**（identity.name、education[0].school）。
确认界面要念给用户听的正是这个：不是「姓名=张三」，而是
「姓名 ← identity.name = 张三」——用户才知道填错了该回去改哪一行。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# 表单字段：内部名、中文名、是否敏感。顺序就是确认界面的展示顺序。
FIELD_SPECS: list[tuple[str, str, bool]] = [
    ("name", "姓名", False),
    ("gender", "性别", False),
    ("phone", "手机号", True),
    ("email", "邮箱", True),
    ("id_card", "身份证号", True),
    ("school", "学校", False),
    ("major", "专业", False),
    ("degree", "学历", False),
    ("grad_year", "毕业年份", False),
    ("grad_month", "毕业月份", False),
    ("gpa", "GPA", False),
    ("school_city", "学校城市", False),
    ("resume_path", "简历文件", False),
]


def mask(value: str) -> str:
    """敏感值打码。确认界面和截图都会被存下来，明文身份证不该到处躺着。"""
    v = (value or "").strip()
    if not v:
        return "（空）"
    if "@" in v:
        local, _, domain = v.partition("@")
        return f"{local[:1] or '*'}{'*' * max(len(local) - 1, 1)}@{domain}"
    if len(v) <= 4:
        return "*" * len(v)
    if len(v) == 11 and v.isdigit():
        return f"{v[:3]}****{v[-4:]}"
    return f"{v[:2]}{'*' * (len(v) - 4)}{v[-2:]}"


class ProfileField(BaseModel):
    """一个摊平后的字段，带来源。确认界面逐行念的就是它。"""

    name: str
    label: str
    value: str = ""
    source: str = ""          # 画像里的路径，如 education[0].school
    sensitive: bool = False

    @property
    def present(self) -> bool:
        return bool(self.value.strip())

    @property
    def display(self) -> str:
        """给人看的值。敏感字段打码——确认界面会被截图存档。"""
        if not self.present:
            return "（空）"
        return mask(self.value) if self.sensitive else self.value


class FormProfile(BaseModel):
    """摊平后的画像。submitter 只认这个，不再直接碰 yaml。"""

    fields: dict[str, ProfileField] = Field(default_factory=dict)
    intent: dict[str, Any] = Field(default_factory=dict)
    narrative: dict[str, str] = Field(default_factory=dict)
    source_path: str = ""

    def get(self, name: str) -> str | None:
        """取值。空字符串按 None 返回，让 submitter 的 `if x :=` 语义正确。"""
        f = self.fields.get(name)
        if f is None or not f.present:
            return None
        return f.value.strip()

    def field(self, name: str) -> ProfileField | None:
        return self.fields.get(name)

    def source_of(self, name: str) -> str:
        f = self.fields.get(name)
        return f.source if f else ""

    @property
    def missing(self) -> list[ProfileField]:
        """空字段。prepare 用它提醒用户「这些页面必填但画像里没有」。"""
        return [f for f in self.fields.values() if not f.present]

    @property
    def grad_term(self) -> str | None:
        """毕业年份 → 届别（2027 → "27"），和 jobs.grad_year 对齐。"""
        y = self.get("grad_year")
        return y[-2:] if y and len(y) >= 2 and y.isdigit() else None


SPEC_BY_NAME = {name: (label, sens) for name, label, sens in FIELD_SPECS}


def _walk(raw: Any, path: str) -> Any:
    """按 "identity.name" / "education[0].school" 取值，取不到返回 None。"""
    cur = raw
    for part in path.replace("[", ".").replace("]", "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return None
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def _latest_education_index(raw: dict) -> int:
    """选最近一段教育经历：end 大的优先，没写 end 的排最后。

    填表要的是当前/最高学历，不是列表里第一条。硕士在读的人如果把本科
    写在前面，填成本科就错了。
    """
    edu = raw.get("education")
    if not isinstance(edu, list) or not edu:
        return 0
    def rank(pair: tuple[int, Any]) -> tuple[bool, str, int]:
        i, item = pair
        end = str((item or {}).get("end") or "").strip()
        return (bool(end), end, i)
    return max(enumerate(edu), key=rank)[0]


def _candidate_paths(raw: dict) -> dict[str, list[str]]:
    """每个表单字段的候选来源路径，按优先级排。

    嵌套路径在前、扁平 key 在后：profile.yaml 是嵌套的，
    profile.yaml.example 和老测试是扁平的，两种都得能读。
    """
    i = _latest_education_index(raw)
    return {
        "name":        ["identity.name", "name"],
        "gender":      ["identity.gender", "gender"],
        "phone":       ["identity.phone", "phone"],
        "email":       ["identity.email", "email"],
        "id_card":     ["identity.id_card", "id_card"],
        "school":      [f"education[{i}].school", "school"],
        "major":       [f"education[{i}].major", "major"],
        "degree":      [f"education[{i}].degree", "degree"],
        "gpa":         [f"education[{i}].gpa", "gpa"],
        "school_city": [f"education[{i}].city", "school_city", "city"],
        "grad_year":   [f"education[{i}].end", "grad_year"],
        "grad_month":  [f"education[{i}].end", "grad_month"],
        "resume_path": ["identity.resume_path", "resume_path"],
    }


def _split_end(value: str) -> tuple[str, str]:
    """"2027-06" → ("2027", "6")。只写年份也接受。"""
    text = str(value or "").strip()
    if not text:
        return "", ""
    parts = text.replace("/", "-").replace(".", "-").split("-")
    year = parts[0].strip()
    month = parts[1].strip().lstrip("0") if len(parts) > 1 else ""
    return (year if year.isdigit() else ""), month


def _flatten(raw: dict) -> dict[str, ProfileField]:
    """按候选路径逐字段解析，记下真正命中的那个路径。"""
    paths = _candidate_paths(raw)
    out: dict[str, ProfileField] = {}

    for name, label, sensitive in FIELD_SPECS:
        value, source = "", ""
        for path in paths.get(name, []):
            hit = _walk(raw, path)
            if hit is None or str(hit).strip() == "":
                continue
            text = str(hit).strip()
            # education[i].end 是 "YYYY-MM"，要拆成年和月两个表单字段
            if path.endswith(".end"):
                year, month = _split_end(text)
                text = year if name == "grad_year" else month
                if not text:
                    continue
            value, source = text, path
            break

        if name == "resume_path" and value:
            value = str(Path(value).expanduser())

        out[name] = ProfileField(
            name=name, label=label, value=value,
            source=source, sensitive=sensitive,
        )
    return out


def intent_from_dict(raw: dict) -> dict[str, Any]:
    """求职意图。嵌套 intent 优先，退回扁平顶层 key。"""
    nested = raw.get("intent")
    if isinstance(nested, dict) and nested:
        return dict(nested)

    flat: dict[str, Any] = {}
    for key in ("families", "cities", "recruit_types",
                "boost_keywords", "exclude_keywords"):
        if raw.get(key):
            flat[key] = raw[key]
    if raw.get("grad_years"):
        flat["grad_years"] = [str(y)[-2:] for y in raw["grad_years"]]
    elif raw.get("grad_year"):
        flat["grad_years"] = [str(raw["grad_year"])[-2:]]
    return flat


def from_dict(raw: dict | None, source_path: str = "") -> FormProfile:
    """从已解析的 dict 构造。测试和 MCP 传 dict 时走这里。"""
    raw = raw or {}
    narrative = raw.get("narrative") if isinstance(raw.get("narrative"), dict) else {}
    return FormProfile(
        fields=_flatten(raw),
        intent=intent_from_dict(raw),
        narrative={k: str(v or "") for k, v in (narrative or {}).items()},
        source_path=source_path,
    )


def load_profile(path: str | Path) -> FormProfile:
    """读 profile.yaml 并摊平。

    文件不存在直接抛——代投前画像必须在，静默用空画像去填表是最坏的结果。
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"画像文件不存在：{p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if raw is not None and not isinstance(raw, dict):
        raise ValueError(f"画像文件格式不对，顶层应该是映射：{p}")
    return from_dict(raw, source_path=str(p))
