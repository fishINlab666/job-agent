from __future__ import annotations

import httpx
import pytest

from jobagent import network


def test_retry_timeout_once_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary")
        return "ok"

    result = network.retry_timeouts(
        operation,
        attempts=2,
        delay_seconds=1.5,
        sleep=sleeps.append,
    )

    assert result == "ok"
    assert calls == 2
    assert sleeps == [1.5]


def test_retry_timeout_stops_after_the_approved_second_attempt() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("still unavailable")

    with pytest.raises(TimeoutError, match="still unavailable"):
        network.retry_timeouts(operation, attempts=2, delay_seconds=0)

    assert calls == 2


def test_non_timeout_is_never_retried() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("invalid response")

    with pytest.raises(RuntimeError, match="invalid response"):
        network.retry_timeouts(operation, attempts=2, delay_seconds=0)

    assert calls == 1


@pytest.mark.parametrize("attempts", [0, 1, 3])
def test_retry_policy_rejects_any_attempt_budget_other_than_two(attempts: int) -> None:
    with pytest.raises(ValueError, match="固定为 2"):
        network.retry_timeouts(lambda: "unused", attempts=attempts)
