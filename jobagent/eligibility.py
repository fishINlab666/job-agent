"""投递前的届别一致性判定。

这里不做 I/O、路由或数据库写入。调用方拿到三态结论后，必须在任何投递副作用
之前决定停止或要求用户明确确认。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .normalize import parse_grad_years
from .profile import FormProfile


_YEAR_TWO = r"[0-9]{2}"
_YEAR_FOUR = r"20[0-9]{2}"
_YEAR_TOKEN = rf"(?:{_YEAR_FOUR}|{_YEAR_TWO})"
_RANGE_SEPARATOR = r"[-~—－至]"
_LIST_SEPARATOR = r"[/、,，+&和或]"
_RANGE_LIKE = re.compile(
    rf"(?<![0-9])(?:20)?([0-9]{{2}})\s*{_RANGE_SEPARATOR}\s*"
    rf"(?:20)?([0-9]{{2}})(?![0-9])"
)
_RANGE_WITH_BOTH_SUFFIXES = re.compile(
    rf"(?<![0-9])(?:20)?([0-9]{{2}})\s*届\s*{_RANGE_SEPARATOR}\s*"
    rf"(?:20)?([0-9]{{2}})\s*届"
)
_STRUCTURED_POSITIVE = (
    re.compile(rf"{_YEAR_TOKEN}\s*届?"),
    re.compile(
        rf"{_YEAR_FOUR}\s*{_RANGE_SEPARATOR}\s*{_YEAR_FOUR}"
        rf"\s*(?:届|年毕业)?|"
        rf"{_YEAR_TWO}\s*{_RANGE_SEPARATOR}\s*{_YEAR_TWO}\s*届"
    ),
    re.compile(
        rf"{_YEAR_FOUR}(?:\s*{_LIST_SEPARATOR}\s*{_YEAR_FOUR})+\s*届?|"
        rf"{_YEAR_TWO}(?:\s*{_LIST_SEPARATOR}\s*{_YEAR_TWO})+\s*届"
    ),
)
_UNLIMITED_EXACT = frozenset(
    {"不限", "不限届别", "届别不限", "任意届别", "所有届别", "全部届别", "any"}
)
_TITLE_ANY_TERM = re.compile(r"\d{2,4}\s*届")


class EligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EligibilityVerdict:
    state: EligibilityState
    reason: str
    basis: str | None
    profile_term: str | None
    job_terms: tuple[str, ...] | None


def _has_invalid_range(text: str) -> bool:
    for pattern in (_RANGE_LIKE, _RANGE_WITH_BOTH_SUFFIXES):
        for match in pattern.finditer(text):
            start, end = int(match.group(1)), int(match.group(2))
            if start > end or end - start > 10:
                return True
    return False


def _parse_positive(text: str) -> list[str] | None:
    parsed = parse_grad_years(text)
    if parsed is None or parsed == []:
        return None
    return parsed


def _structured_terms(raw: object) -> tuple[list[str] | None, bool]:
    if raw is None or not str(raw).strip():
        return None, False
    text = str(raw).strip()
    if text.lower() in _UNLIMITED_EXACT:
        return [], False
    if _has_invalid_range(text):
        return None, True
    if not any(pattern.fullmatch(text) for pattern in _STRUCTURED_POSITIVE):
        return None, True
    parsed = _parse_positive(text)
    return parsed, parsed is None


def _title_terms(raw: object) -> tuple[list[str] | None, bool]:
    if raw is None or not str(raw).strip():
        return None, False
    text = unicodedata.normalize("NFKC", str(raw).strip())
    # 标题是自由文本。它可以提示“可能有届别证据”，但不能独自成为上传真实资料
    # 的硬依据；任何数字届别都统一交给预填前的人工确认。
    return (None, True) if _TITLE_ANY_TERM.search(text) else (None, False)


def assess_grad_year(
    job: Mapping[str, object], form: FormProfile
) -> EligibilityVerdict:
    """比较当前岗位届别和本次实际填表画像，返回三态判定。"""
    parsed, ambiguous = _structured_terms(job.get("grad_year"))
    structured_present = bool(str(job.get("grad_year") or "").strip())
    basis: str | None = "structured" if structured_present else None
    if ambiguous:
        return EligibilityVerdict(
            EligibilityState.UNKNOWN,
            "job_term_ambiguous",
            basis,
            form.grad_term,
            None,
        )
    if not structured_present:
        parsed, ambiguous = _title_terms(job.get("title"))
        basis = "title" if parsed is not None or ambiguous else None
        if ambiguous:
            return EligibilityVerdict(
                EligibilityState.UNKNOWN,
                "job_term_ambiguous",
                basis,
                form.grad_term,
                None,
            )

    job_terms = tuple(parsed) if parsed is not None else None
    profile_term = form.grad_term

    if job_terms == ():
        return EligibilityVerdict(
            EligibilityState.ELIGIBLE,
            "unlimited",
            basis,
            profile_term,
            job_terms,
        )

    if job_terms is not None:
        if profile_term is None:
            return EligibilityVerdict(
                EligibilityState.UNKNOWN,
                "profile_term_missing",
                basis,
                profile_term,
                job_terms,
            )
        state = (
            EligibilityState.ELIGIBLE
            if profile_term in job_terms
            else EligibilityState.INELIGIBLE
        )
        return EligibilityVerdict(
            state,
            "term_match" if state is EligibilityState.ELIGIBLE else "term_mismatch",
            basis,
            profile_term,
            job_terms,
        )

    if job.get("recruit_type") == "social":
        return EligibilityVerdict(
            EligibilityState.ELIGIBLE,
            "not_applicable",
            None,
            profile_term,
            None,
        )

    return EligibilityVerdict(
        EligibilityState.UNKNOWN,
        "job_term_missing",
        None,
        profile_term,
        None,
    )
