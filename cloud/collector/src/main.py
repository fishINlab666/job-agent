"""Cloudflare Worker 入口：定时采集、补采与只读状态。"""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

try:  # 本地单测不加载 Workers 运行时。
    from workers import Response, WorkerEntrypoint
except ImportError:  # pragma: no cover - 只用于 CPython 单测导入
    Response = None

    class WorkerEntrypoint:  # type: ignore[no-redef]
        pass

if __package__:
    from .collector import Collector
    from .repository import D1Repository
    from .windowing import SHANGHAI, active_window, technical_trial_window
else:  # Cloudflare 把 src/main.py 作为顶层模块加载。
    from collector import Collector
    from repository import D1Repository
    from windowing import SHANGHAI, active_window, technical_trial_window


CLIENT_ACK_PATH = re.compile(r"^/v1/clients/([A-Za-z0-9_-]{1,64})/ack$")


def _authorized(request, env) -> bool:
    expected = str(getattr(env, "JOBAGENT_SYNC_TOKEN", ""))
    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    return bool(expected) and supplied.startswith(prefix) and hmac.compare_digest(
        supplied[len(prefix) :], expected
    )


async def route_request(
    request,
    env,
    *,
    now: datetime | None = None,
    repository: D1Repository | None = None,
) -> tuple[dict, int]:
    if not _authorized(request, env):
        return {"error": "unauthorized"}, 401

    current = now or datetime.now(timezone.utc)
    parsed = urlsplit(request.url)
    path = parsed.path
    repo = repository or D1Repository(env.DB)

    if request.method == "GET" and path == "/v1/status/today":
        workday = current.astimezone(SHANGHAI).date().isoformat()
        return await repo.status_for_day(workday), 200

    if request.method == "POST" and path == "/v1/catch-up":
        try:
            body = await request.json()
        except Exception:
            return {"error": "request body must be JSON"}, 400
        if body != {}:
            return {"error": "catch-up body must be an empty object"}, 400
        window = active_window(current)
        if window is None:
            return {"error": "no active collection window"}, 409
        return await Collector(repo).run_window(window, trigger="catch-up"), 200

    if request.method == "POST" and path == "/v1/technical-trial":
        try:
            body = await request.json()
        except Exception:
            return {"error": "request body must be JSON"}, 400
        if body != {}:
            return {"error": "technical-trial body must be an empty object"}, 400
        window = technical_trial_window(current)
        return await Collector(repo).run_window(window, trigger="technical-trial"), 200

    if request.method == "GET" and path == "/v1/changes":
        params = parse_qs(parsed.query, keep_blank_values=True)
        if set(params) != {"after"} or len(params["after"]) != 1:
            return {"error": "after must be one non-negative integer"}, 400
        try:
            after = int(params["after"][0])
            if after < 0:
                raise ValueError
        except ValueError:
            return {"error": "after must be one non-negative integer"}, 400
        return await repo.changes_after(after), 200

    ack_match = CLIENT_ACK_PATH.fullmatch(path)
    if request.method == "POST" and ack_match:
        try:
            body = await request.json()
        except Exception:
            return {"error": "request body must be JSON"}, 400
        if set(body) != {"cursor"} or type(body["cursor"]) is not int or body["cursor"] < 0:
            return {"error": "ack body must contain one non-negative integer cursor"}, 400
        client_id = ack_match.group(1)
        try:
            cursor = await repo.ack_client(client_id, body["cursor"], current.isoformat())
        except ValueError as exc:
            return {"error": str(exc)}, 409
        return {"client_id": client_id, "acknowledged_cursor": cursor}, 200

    return {"error": "not found"}, 404


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        payload, status = await route_request(request, self.env)
        return Response.json(payload, status=status)

    async def scheduled(self, controller, env, ctx):
        del ctx
        scheduled_ms = getattr(controller, "scheduledTime", None)
        now = (
            datetime.fromtimestamp(scheduled_ms / 1000, tz=timezone.utc)
            if scheduled_ms is not None
            else datetime.now(timezone.utc)
        )
        window = active_window(now)
        if window is not None:
            await Collector(D1Repository(env.DB)).run_window(window, trigger="cron")
