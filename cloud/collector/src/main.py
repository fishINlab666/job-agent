"""Cloudflare Worker 入口：定时采集、补采与只读状态。"""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from jobagent.targets import OBSERVATION_SOURCES

try:  # 本地单测不加载 Workers 运行时。
    from workers import Response, WorkerEntrypoint
except ImportError:  # pragma: no cover - 只用于 CPython 单测导入
    Response = None

    class WorkerEntrypoint:  # type: ignore[no-redef]
        pass

if __package__:
    from .repository import D1Repository
    from .windowing import (
        SHANGHAI,
        active_window,
        catch_up_windows,
        expired_windows,
        previous_workday_windows,
        technical_trial_window,
    )
else:  # Cloudflare 把 src/main.py 作为顶层模块加载。
    from repository import D1Repository
    from windowing import (
        SHANGHAI,
        active_window,
        catch_up_windows,
        expired_windows,
        previous_workday_windows,
        technical_trial_window,
    )


CLIENT_ACK_PATH = re.compile(r"^/v1/clients/([A-Za-z0-9_-]{1,64})/ack$")
INGEST_CHUNK_PATH = re.compile(
    r"^/v1/ingest/sessions/([0-9a-f]{64})/chunks/(0|[1-9][0-9]{0,2})$"
)
INGEST_COMMIT_PATH = re.compile(
    r"^/v1/ingest/sessions/([0-9a-f]{64})/commit$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_BY_KEY = {str(spec["source_key"]): spec for spec in OBSERVATION_SOURCES}
MAX_CHUNK_HTTP_BYTES = 1_600_000
MAX_JOB_JSON_BYTES = 1_000_000


def _authorized(request, env, credential: str = "JOBAGENT_SYNC_TOKEN") -> bool:
    expected = str(getattr(env, credential, ""))
    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    return bool(expected) and supplied.startswith(prefix) and hmac.compare_digest(
        supplied[len(prefix) :], expected
    )


def _accepts_small_json(request) -> bool:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return False
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return True
    try:
        return 0 <= int(raw_length) <= 1024
    except ValueError:
        return False


def _accepts_bounded_json(request, maximum: int) -> bool:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return False
    raw_length = request.headers.get("content-length")
    try:
        return raw_length is not None and 0 <= int(raw_length) <= maximum
    except ValueError:
        return False


def _ingest_window(current: datetime, mode: str):
    if mode == "technical-trial":
        return technical_trial_window(current)
    candidates = catch_up_windows(current)
    return candidates[-1] if candidates else None


async def route_request(
    request,
    env,
    *,
    now: datetime | None = None,
    repository: D1Repository | None = None,
) -> tuple[dict, int]:
    current = now or datetime.now(timezone.utc)
    parsed = urlsplit(request.url)
    path = parsed.path
    repo = repository or D1Repository(env.DB)
    is_ingest = path == "/v1/ingest/sessions" or path.startswith(
        "/v1/ingest/sessions/"
    )
    credential = "JOBAGENT_INGEST_TOKEN" if is_ingest else "JOBAGENT_SYNC_TOKEN"
    if not _authorized(request, env, credential):
        return {"error": "unauthorized"}, 401

    if request.method == "POST" and path == "/v1/ingest/sessions":
        if not _accepts_bounded_json(request, 4096):
            return {"error": "ingest session requires bounded application/json"}, 415
        try:
            body = await request.json()
        except Exception:
            return {"error": "request body must be JSON"}, 400
        if not isinstance(body, dict) or set(body) != {
            "session_id",
            "collection_id",
            "source_key",
            "mode",
            "expected_count",
            "expected_bytes",
            "snapshot_sha256",
        }:
            return {"error": "invalid ingest session shape"}, 400
        session_id = str(body["session_id"])
        collection_id = str(body["collection_id"])
        source_key = str(body["source_key"])
        mode = str(body["mode"])
        count = body["expected_count"]
        expected_bytes = body["expected_bytes"]
        digest = str(body["snapshot_sha256"])
        if (
            not SHA256.fullmatch(session_id)
            or not SHA256.fullmatch(collection_id)
            or source_key not in SOURCE_BY_KEY
            or mode not in {"official", "technical-trial"}
            or type(count) is not int
            or not 1 <= count <= 20000
            or type(expected_bytes) is not int
            or not 1 <= expected_bytes <= 75_000_000
            or not SHA256.fullmatch(digest)
        ):
            return {"error": "invalid ingest session values"}, 400
        window = _ingest_window(current, mode)
        expires_at = (current + timedelta(minutes=25)).isoformat()
        try:
            if mode == "official":
                previous = previous_workday_windows(current)
                if previous and await repo.has_official_window(previous[0].workday):
                    for expired in previous:
                        await repo.mark_missed(
                            expired, set(SOURCE_BY_KEY), current.isoformat()
                        )
                for expired in expired_windows(current):
                    await repo.mark_missed(expired, set(SOURCE_BY_KEY), current.isoformat())
            session, created = await repo.begin_remote_session(
                session_id=session_id,
                collection_id=collection_id,
                window=window,
                source_key=source_key,
                mode=mode,
                expected_count=count,
                expected_bytes=expected_bytes,
                snapshot_sha256=digest,
                now=current.isoformat(),
                expires_at=expires_at,
            )
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}, 409
        return {
            "session_id": session_id,
            "collection_id": collection_id,
            "source_key": source_key,
            "status": session["status"],
            "expected_count": int(session["expected_count"]),
            "snapshot_sha256": str(session["snapshot_sha256"]),
            "window_id": int(session["window_id"]),
            "window_key": str(session["window_key"]),
        }, (201 if created else 200)

    chunk_match = INGEST_CHUNK_PATH.fullmatch(path)
    if request.method == "PUT" and chunk_match:
        if not _accepts_bounded_json(request, MAX_CHUNK_HTTP_BYTES):
            return {"error": "ingest chunk requires bounded application/json"}, 415
        try:
            body = await request.json()
        except Exception:
            return {"error": "request body must be JSON"}, 400
        if not isinstance(body, dict) or set(body) != {"chunk_sha256", "jobs"}:
            return {"error": "invalid ingest chunk shape"}, 400
        digest = str(body["chunk_sha256"])
        jobs = body["jobs"]
        if (
            not SHA256.fullmatch(digest)
            or not isinstance(jobs, list)
            or any(not isinstance(job, dict) for job in jobs)
        ):
            return {"error": "invalid ingest chunk values"}, 400
        if any(
            len(
                json.dumps(
                    job,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ) > MAX_JOB_JSON_BYTES
            for job in jobs
        ):
            return {"error": "单岗位 JSON 超过 D1 安全上限"}, 400
        session_id = chunk_match.group(1)
        session = await repo._remote_session(session_id)
        if session is None:
            return {"error": "remote ingest session not found"}, 404
        spec = SOURCE_BY_KEY.get(str(session["source_key"]))
        if spec is None:
            return {"error": "session source is not approved"}, 409
        try:
            stored = await repo.stage_remote_chunk(
                session_id=session_id,
                chunk_index=int(chunk_match.group(2)),
                chunk_sha256=digest,
                payloads=jobs,
                company=str(spec["company"]),
                now=current.isoformat(),
            )
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        except ValueError as exc:
            status = (
                409
                if "already bound" in str(exc) or "not pending" in str(exc)
                else 400
            )
            return {"error": str(exc)}, status
        return {
            "chunk_index": int(chunk_match.group(2)),
            "row_count": len(jobs),
            "status": stored,
        }, 200

    commit_match = INGEST_COMMIT_PATH.fullmatch(path)
    if request.method == "POST" and commit_match:
        if not _accepts_bounded_json(request, 1024):
            return {"error": "ingest commit requires bounded application/json"}, 415
        try:
            body = await request.json()
        except Exception:
            return {"error": "request body must be JSON"}, 400
        if body != {}:
            return {"error": "ingest commit body must be an empty object"}, 400
        try:
            result = await repo.commit_remote_session(
                session_id=commit_match.group(1),
                now=current.isoformat(),
                expected_sources=set(SOURCE_BY_KEY),
            )
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}, 409
        return result, 200

    if request.method == "GET" and path == "/v1/status/today":
        workday = current.astimezone(SHANGHAI).date().isoformat()
        payload = await repo.status_for_day(workday)
        window = active_window(current)
        payload["active_window"] = window.key if window else None
        payload["catch_up_windows"] = [
            candidate.key for candidate in catch_up_windows(current)
        ]
        return payload, 200

    if request.method == "POST" and path == "/v1/catch-up":
        return {"error": "direct Cloudflare collection is disabled"}, 410

    if request.method == "POST" and path == "/v1/technical-trial":
        return {"error": "direct Cloudflare collection is disabled"}, 410

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
        if not _accepts_small_json(request):
            return {"error": "content-type must be application/json and body <= 1 KiB"}, 415
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
        del controller, env, ctx
        return None
