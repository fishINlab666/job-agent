"""公开招聘接口的最小网络容错。"""
from __future__ import annotations

from collections.abc import Callable
import time
from typing import TypeVar

import httpx


T = TypeVar("T")


def retry_timeouts(
    operation: Callable[[], T],
    *,
    attempts: int = 2,
    delay_seconds: float = 1.0,
    sleep: Callable[[float], None] | None = None,
) -> T:
    if attempts != 2:
        raise ValueError("超时重试总尝试次数固定为 2")

    sleeper = sleep or time.sleep
    for attempt in range(attempts):
        try:
            return operation()
        except (httpx.TimeoutException, TimeoutError):
            if attempt + 1 == attempts:
                raise
            sleeper(delay_seconds)

    raise AssertionError("unreachable")
