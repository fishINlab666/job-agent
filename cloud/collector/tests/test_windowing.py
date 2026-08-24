from datetime import datetime, timezone

import pytest

from cloud.collector.src.windowing import active_window


@pytest.mark.parametrize(
    ("utc_text", "expected"),
    [
        ("2026-08-24T00:00:00+00:00", "morning"),
        ("2026-08-24T03:59:59+00:00", "morning"),
        ("2026-08-24T04:00:00+00:00", "afternoon"),
        ("2026-08-24T08:59:59+00:00", "afternoon"),
        ("2026-08-24T09:00:00+00:00", "evening"),
        ("2026-08-24T13:59:59+00:00", "evening"),
        ("2026-08-24T14:00:00+00:00", None),
    ],
)
def test_active_window_uses_shanghai_boundaries(utc_text: str, expected: str | None) -> None:
    window = active_window(datetime.fromisoformat(utc_text))

    assert (window.key if window else None) == expected
    if window:
        assert window.workday == "2026-08-24"
        assert window.opens_at.utcoffset().total_seconds() == 8 * 3600


@pytest.mark.parametrize("day", ["2026-08-22T02:00:00+00:00", "2026-08-23T02:00:00+00:00"])
def test_weekends_have_no_collection_window(day: str) -> None:
    assert active_window(datetime.fromisoformat(day)) is None


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        active_window(datetime(2026, 8, 24, 9, 30))


def test_utc_datetime_is_accepted() -> None:
    assert active_window(datetime(2026, 8, 24, 1, tzinfo=timezone.utc)).key == "morning"
