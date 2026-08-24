"""Asia/Shanghai 工作日弹性采集窗口。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
CATCH_UP_GRACE = timedelta(hours=1)
WINDOW_MINUTES: tuple[tuple[str, int, int], ...] = (
    ("morning", 8 * 60, 12 * 60),
    ("afternoon", 12 * 60, 17 * 60),
    ("evening", 17 * 60, 22 * 60),
)


@dataclass(frozen=True)
class Window:
    workday: str
    key: Literal["morning", "afternoon", "evening", "technical-trial"]
    opens_at: datetime
    closes_at: datetime


def _at_minute(local: datetime, minute: int) -> datetime:
    return local.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)


def _require_aware(now_utc: datetime, caller: str) -> datetime:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError(f"{caller} requires a timezone-aware datetime")
    return now_utc.astimezone(SHANGHAI)


def windows_for_day(now_utc: datetime) -> tuple[Window, ...]:
    local = _require_aware(now_utc, "windows_for_day")
    if local.weekday() >= 5:
        return ()
    return tuple(
        Window(
            workday=local.date().isoformat(),
            key=key,
            opens_at=_at_minute(local, start),
            closes_at=_at_minute(local, end),
        )
        for key, start, end in WINDOW_MINUTES
    )


def active_window(now_utc: datetime) -> Window | None:
    local = _require_aware(now_utc, "active_window")
    for window in windows_for_day(now_utc):
        if window.opens_at <= local < window.closes_at:
            return window
    return None


def catch_up_windows(now_utc: datetime) -> tuple[Window, ...]:
    """返回当前窗口，以及刚结束、仍可安全补跑的上一个窗口。"""
    local = _require_aware(now_utc, "catch_up_windows")
    windows = windows_for_day(now_utc)
    eligible = [
        window
        for window in windows
        if window.opens_at <= local < window.closes_at + CATCH_UP_GRACE
    ]
    return tuple(eligible[-2:])


def expired_windows(now_utc: datetime) -> tuple[Window, ...]:
    """返回补跑宽限期已结束的正式窗口。"""
    local = _require_aware(now_utc, "expired_windows")
    return tuple(
        window
        for window in windows_for_day(now_utc)
        if window.closes_at + CATCH_UP_GRACE <= local
    )


def technical_trial_window(now_utc: datetime) -> Window:
    """建立独立技术试运行窗口，绝不回填正式早/午/晚验收事实。"""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("technical_trial_window requires a timezone-aware datetime")
    local = now_utc.astimezone(SHANGHAI)
    return Window(
        workday=local.date().isoformat(),
        key="technical-trial",
        opens_at=local,
        closes_at=local + timedelta(hours=1),
    )
