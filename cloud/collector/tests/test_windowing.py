import importlib
import sys
import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest

from cloud.collector.src.windowing import active_window, catch_up_windows, expired_windows


def test_windowing_imports_without_system_timezone_database(monkeypatch) -> None:
    def unavailable(_key: str):
        raise zoneinfo.ZoneInfoNotFoundError("timezone database unavailable")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", unavailable)
    sys.modules.pop("cloud.collector.src.windowing", None)

    module = importlib.import_module("cloud.collector.src.windowing")

    assert module.SHANGHAI.utcoffset(None) == timedelta(hours=8)


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


def test_just_ended_window_remains_available_for_one_hour() -> None:
    windows = catch_up_windows(datetime.fromisoformat("2026-08-24T04:30:00+00:00"))
    assert [window.key for window in windows] == ["morning", "afternoon"]

    after_evening = catch_up_windows(
        datetime.fromisoformat("2026-08-24T14:30:00+00:00")
    )
    assert [window.key for window in after_evening] == ["evening"]


def test_window_becomes_missed_only_after_catch_up_grace() -> None:
    before = expired_windows(datetime.fromisoformat("2026-08-24T04:59:59+00:00"))
    assert before == ()

    after = expired_windows(datetime.fromisoformat("2026-08-24T05:00:00+00:00"))
    assert [window.key for window in after] == ["morning"]
