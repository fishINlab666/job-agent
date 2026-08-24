"""Asia/Shanghai 工作日弹性采集窗口。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
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


def active_window(now_utc: datetime) -> Window | None:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("active_window requires a timezone-aware datetime")
    local = now_utc.astimezone(SHANGHAI)
    if local.weekday() >= 5:
        return None
    minute = local.hour * 60 + local.minute
    for key, start, end in WINDOW_MINUTES:
        if start <= minute < end:
            return Window(
                workday=local.date().isoformat(),
                key=key,
                opens_at=_at_minute(local, start),
                closes_at=_at_minute(local, end),
            )
    return None


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
