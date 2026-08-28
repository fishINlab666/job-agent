"""投递前届别一致性判定。只使用虚构画像和岗位。"""
from __future__ import annotations

import pytest

from jobagent import eligibility, profile


def form(end: str | None = "2027-06") -> profile.FormProfile:
    education = [] if end is None else [{"end": end}]
    return profile.from_dict({"education": education})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2027", "27"),
        ("2027-06", "27"),
        ("20270", None),
        ("123", None),
        ("0000", None),
        ("20２７", None),
        ("20٢٧", None),
        ("not-a-year", None),
    ],
)
def test_grad_term_requires_strict_four_digit_20xx(raw, expected):
    assert form(raw).grad_term == expected


def test_explicit_mismatch_is_ineligible():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "26届", "title": "产品运营", "recruit_type": "campus"},
        form("2027-06"),
    )

    assert verdict.state is eligibility.EligibilityState.INELIGIBLE
    assert verdict.reason == "term_mismatch"
    assert verdict.basis == "structured"
    assert verdict.profile_term == "27"
    assert verdict.job_terms == ("26",)


@pytest.mark.parametrize(
    "raw",
    ["27", "26-28届", "2026-2028年毕业", "26/27届", "2026/2027届"],
)
def test_matching_term_is_eligible(raw):
    verdict = eligibility.assess_grad_year(
        {"grad_year": raw, "title": "产品运营", "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.ELIGIBLE
    assert verdict.reason == "term_match"


def test_unlimited_is_eligible_without_profile_term():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "不限", "title": "产品运营", "recruit_type": "campus"},
        form(None),
    )

    assert verdict.state is eligibility.EligibilityState.ELIGIBLE
    assert verdict.reason == "unlimited"
    assert verdict.job_terms == ()


def test_structured_term_wins_over_conflicting_title():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "27届", "title": "26届产品运营", "recruit_type": "campus"},
        form("2026-06"),
    )

    assert verdict.state is eligibility.EligibilityState.INELIGIBLE
    assert verdict.basis == "structured"
    assert verdict.job_terms == ("27",)


def test_title_term_requires_manual_confirmation_when_structured_term_is_missing():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "【27届校招】产品运营", "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"
    assert verdict.job_terms is None


def test_missing_job_term_is_unknown():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "产品运营", "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_missing"


def test_missing_profile_term_is_unknown():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "27届", "title": "产品运营", "recruit_type": "campus"},
        form(None),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "profile_term_missing"


def test_unicode_digits_in_profile_term_are_unknown():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "27届", "title": "产品运营", "recruit_type": "campus"},
        form("20２７"),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "profile_term_missing"


@pytest.mark.parametrize(
    "raw",
    [
        "非2027届",
        "不是2027届",
        "不招2027届",
        "不招收2027届",
        "不接受2027届",
        "不接收2027届",
        "不要2027届",
        "not 2027",
        "non-2027",
        "other than 2027",
        "疑似2027届",
        "2027届除外",
        "2027-2026年",
        "2027届-2026届",
        "2027～2026届",
        "2027年6月-2026年6月毕业",
        "2020-2035年毕业",
        "26～28届",
        "2026～2028年毕业",
        "所有2026届",
        "26-2028届",
        "2026-28届",
        "2026/27届",
        "26/2027届",
        "２７届",
    ],
)
def test_ambiguous_structured_term_is_unknown(raw):
    verdict = eligibility.assess_grad_year(
        {"grad_year": raw, "title": "2027届产品运营", "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "structured"


@pytest.mark.parametrize(
    "title",
    [
        "非2027届产品运营",
        "疑似2027届产品运营",
        "疑似 2027届产品运营",
        "可能-2027届产品运营",
        "所有2026届产品运营",
        "2027届除外-产品运营",
        "2027-2026届产品运营",
        "2027届勿投-产品运营",
        "2027届不接受-产品运营",
        "2027届不面向-产品运营",
        "【2027届？】产品运营",
        "2027届（待定）产品运营",
        "2027届疑似产品运营",
        "【校招27届】产品运营",
        "【２７届校招】产品运营",
        "【²⁷届校招】产品运营",
        "【②⑦届校招】产品运营",
        "【٢٧届校招】产品运营",
        "【۲۷届校招】产品运营",
        "【२७届校招】产品运营",
        "27届校招 not recruiting",
        "27届校招暂不开放",
        "27届校招停止招聘",
        "27届校招不适用",
        "【27届校招】not recruiting",
    ],
)
def test_ambiguous_title_term_is_unknown(title):
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": title, "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"


def test_non_technical_title_still_requires_manual_confirmation():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "27届非技术产品经理", "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"


def test_bracketed_leading_title_term_still_requires_manual_confirmation():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "【2027届校招】产品运营", "recruit_type": "campus"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"


def test_social_with_mismatching_structured_term_is_ineligible():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "27届", "title": "产品运营", "recruit_type": "social"},
        form("2026-06"),
    )

    assert verdict.state is eligibility.EligibilityState.INELIGIBLE


def test_social_with_matching_term_is_eligible():
    verdict = eligibility.assess_grad_year(
        {"grad_year": "27届", "title": "产品运营", "recruit_type": "social"},
        form(),
    )

    assert verdict.state is eligibility.EligibilityState.ELIGIBLE


def test_social_with_title_only_term_requires_manual_confirmation():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "【27届校招】产品运营", "recruit_type": "social"},
        form("2026-06"),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"


def test_social_with_fullwidth_title_term_requires_manual_confirmation():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "【２７届校招】产品运营", "recruit_type": "social"},
        form(None),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"


@pytest.mark.parametrize(
    "title",
    [
        "【²⁷届校招】产品运营",
        "【②⑦届校招】产品运营",
        "【٢٧届校招】产品运营",
        "【۲۷届校招】产品运营",
        "【२७届校招】产品运营",
    ],
)
def test_social_with_styled_unicode_title_term_requires_manual_confirmation(title):
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": title, "recruit_type": "social"},
        form(None),
    )

    assert verdict.state is eligibility.EligibilityState.UNKNOWN
    assert verdict.reason == "job_term_ambiguous"
    assert verdict.basis == "title"


def test_social_without_term_is_not_applicable():
    verdict = eligibility.assess_grad_year(
        {"grad_year": None, "title": "产品运营", "recruit_type": "social"},
        form(None),
    )

    assert verdict.state is eligibility.EligibilityState.ELIGIBLE
    assert verdict.reason == "not_applicable"
    assert verdict.basis is None
