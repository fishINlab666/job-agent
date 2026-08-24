"""Cloudflare D1 persistence adapter for public collection truth."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jobagent.collection import (
    CLOSE_GUARD_MIN_COUNT,
    CLOSE_GUARD_RATIO,
    close_guard_tripped,
    payload_fingerprint,
    snapshot_digest,
)

if __package__:
    from .windowing import Window
else:  # Cloudflare 把 src/main.py 作为顶层模块加载。
    from windowing import Window


@dataclass(frozen=True)
class StagedJob:
    external_id: str
    fingerprint: str
    payload: dict


@dataclass(frozen=True)
class ApplyResult:
    opened: int
    updated: int
    closed: int


def _to_python(value: Any) -> Any:
    converter = getattr(value, "to_py", None)
    return converter() if callable(converter) else value


def _rows(result: Any) -> list[dict]:
    values = _to_python(getattr(result, "results", None)) or []
    return [dict(_to_python(row)) for row in values]


def _changes(result: Any) -> int:
    meta = _to_python(getattr(result, "meta", None))
    value = (
        meta.get("changes", 0)
        if isinstance(meta, dict)
        else getattr(meta, "changes", 0)
    )
    return int(_to_python(value) or 0)


class D1Repository:
    def __init__(self, database: Any) -> None:
        self.database = database

    async def get_or_create_window(self, window: Window, now: str) -> int:
        statements = [
            self.database.prepare(
                """INSERT INTO collection_windows(
                       workday, window_key, opens_at, closes_at, created_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(workday, window_key) DO NOTHING"""
            ).bind(
                window.workday,
                window.key,
                window.opens_at.isoformat(),
                window.closes_at.isoformat(),
                now,
            ),
            self.database.prepare(
                "SELECT id FROM collection_windows WHERE workday=? AND window_key=?"
            ).bind(window.workday, window.key),
        ]
        results = await self.database.batch(statements)
        rows = _rows(results[1])
        if len(rows) != 1:
            raise RuntimeError("window readback did not return exactly one row")
        return int(rows[0]["id"])

    async def acquire_window(
        self,
        window_id: int,
        owner: str,
        now: str,
        expires_at: str,
    ) -> int | None:
        result = await self.database.prepare(
            """UPDATE collection_windows SET
                   status='running',
                   lease_generation=CASE
                     WHEN lease_owner=? THEN lease_generation
                     ELSE lease_generation + 1
                   END,
                   lease_owner=?, lease_expires_at=?
               WHERE id=? AND status<>'complete'
                 AND (
                   lease_owner IS NULL OR lease_expires_at IS NULL
                   OR lease_expires_at<=? OR lease_owner=?
                 )
               RETURNING lease_generation"""
        ).bind(owner, owner, expires_at, window_id, now, owner).run()
        rows = _rows(result)
        return int(rows[0]["lease_generation"]) if len(rows) == 1 else None

    async def start_run(self, window_id: int, source_key: str, now: str) -> int:
        result = await self.database.prepare(
            """INSERT INTO source_runs(
                   window_id, source_key, attempt, status, started_at
               ) VALUES(
                   ?, ?,
                   COALESCE((SELECT MAX(attempt) + 1 FROM source_runs
                             WHERE window_id=? AND source_key=?), 1),
                   'running', ?
               ) RETURNING id"""
        ).bind(window_id, source_key, window_id, source_key, now).run()
        rows = _rows(result)
        if len(rows) != 1:
            raise RuntimeError("source run insert did not return exactly one id")
        return int(rows[0]["id"])

    async def stage_jobs(
        self,
        run_id: int,
        source_key: str,
        jobs: list[StagedJob],
        *,
        chunk_size: int = 100,
    ) -> None:
        insert = self.database.prepare(
            """INSERT INTO staged_jobs(
                   run_id, source_key, external_id, fingerprint, payload_json
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(run_id, source_key, external_id) DO UPDATE SET
                   fingerprint=excluded.fingerprint,
                   payload_json=excluded.payload_json"""
        )
        for start in range(0, len(jobs), chunk_size):
            statements = []
            for job in jobs[start : start + chunk_size]:
                payload_json = json.dumps(
                    job.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                statements.append(
                    insert.bind(
                        run_id,
                        source_key,
                        job.external_id,
                        job.fingerprint,
                        payload_json,
                    )
                )
            if statements:
                await self.database.batch(statements)

    async def _staged_payloads(self, run_id: int, source_key: str) -> list[dict]:
        result = await self.database.prepare(
            """SELECT payload_json FROM staged_jobs
               WHERE run_id=? AND source_key=?
               ORDER BY source_key, external_id"""
        ).bind(run_id, source_key).run()
        return [json.loads(row["payload_json"]) for row in _rows(result)]

    async def _change_counts(
        self, run_id: int, source_key: str
    ) -> tuple[ApplyResult, int]:
        statements = [
            self.database.prepare(
                """SELECT COUNT(*) AS n FROM staged_jobs s
                   LEFT JOIN cloud_jobs c
                     ON c.source_key=s.source_key AND c.external_id=s.external_id
                   WHERE s.run_id=? AND s.source_key=? AND c.external_id IS NULL"""
            ).bind(run_id, source_key),
            self.database.prepare(
                """SELECT COUNT(*) AS n FROM staged_jobs s
                   JOIN cloud_jobs c
                     ON c.source_key=s.source_key AND c.external_id=s.external_id
                   WHERE s.run_id=? AND s.source_key=?
                     AND (c.fingerprint<>s.fingerprint OR c.closed_at IS NOT NULL)"""
            ).bind(run_id, source_key),
            self.database.prepare(
                """SELECT COUNT(*) AS n FROM cloud_jobs c
                   WHERE c.source_key=? AND c.closed_at IS NULL
                     AND NOT EXISTS(
                         SELECT 1 FROM staged_jobs s
                         WHERE s.run_id=? AND s.source_key=c.source_key
                           AND s.external_id=c.external_id
                     )"""
            ).bind(source_key, run_id),
            self.database.prepare(
                """SELECT COUNT(*) AS n FROM cloud_jobs
                   WHERE source_key=? AND closed_at IS NULL"""
            ).bind(source_key),
        ]
        results = await self.database.batch(statements)
        counts = [int(_rows(result)[0]["n"]) for result in results]
        return ApplyResult(*counts[:3]), counts[3]

    async def finalize_snapshot(
        self,
        run_id: int,
        window_id: int,
        source_key: str,
        expected_digest: str,
        expected_count: int,
        now: str,
        *,
        lease_owner: str,
        lease_generation: int,
        fence_now: str,
        remote_session_id: str | None = None,
        collection_id: str | None = None,
        expected_source_count: int | None = None,
    ) -> ApplyResult:
        payloads = await self._staged_payloads(run_id, source_key)
        if len(payloads) != expected_count:
            raise ValueError(
                f"staged {len(payloads)} jobs but expected {expected_count}"
            )
        actual_digest = snapshot_digest(payloads)
        if actual_digest != expected_digest:
            raise ValueError(
                f"staged snapshot digest {actual_digest} != expected {expected_digest}"
            )
        owns_source_head = """EXISTS(
            SELECT 1 FROM source_heads h
            WHERE h.source_key=? AND h.run_id=? AND h.window_id=?
        )"""
        remote_commit_gate = ""
        remote_commit_bindings: tuple[str, ...] = ()
        if remote_session_id is not None:
            remote_commit_gate = """AND EXISTS(
                       SELECT 1 FROM remote_ingest_sessions rs
                       JOIN remote_collection_rounds cr
                         ON cr.id=rs.collection_id
                       WHERE rs.id=? AND rs.run_id=r.id
                         AND rs.status='committing'
                         AND cr.status='pending' AND cr.expires_at>?
                     )"""
            remote_commit_bindings = (remote_session_id, fence_now)
        statements = [
            self.database.prepare(
                """INSERT INTO source_heads(
                       source_key, window_id, window_opens_at, run_id
                   )
                   SELECT ?, w.id, w.opens_at, r.id
                   FROM collection_windows w
                   JOIN source_runs r ON r.window_id=w.id
                   WHERE w.id=? AND r.id=? AND r.source_key=?
                     AND r.status='running'
                     AND w.lease_owner=? AND w.lease_generation=?
                     AND w.lease_expires_at>?
                     """ + remote_commit_gate + """
                     AND NOT (
                       (SELECT COUNT(*) FROM cloud_jobs c
                        WHERE c.source_key=? AND c.closed_at IS NULL
                          AND NOT EXISTS(
                            SELECT 1 FROM staged_jobs s
                            WHERE s.run_id=? AND s.source_key=c.source_key
                              AND s.external_id=c.external_id
                          )) >= ?
                       AND (SELECT COUNT(*) FROM cloud_jobs c
                            WHERE c.source_key=? AND c.closed_at IS NULL) > 0
                       AND CAST((SELECT COUNT(*) FROM cloud_jobs c
                                 WHERE c.source_key=? AND c.closed_at IS NULL
                                   AND NOT EXISTS(
                                     SELECT 1 FROM staged_jobs s
                                     WHERE s.run_id=? AND s.source_key=c.source_key
                                       AND s.external_id=c.external_id
                                   )) AS REAL)
                           / (SELECT COUNT(*) FROM cloud_jobs c
                              WHERE c.source_key=? AND c.closed_at IS NULL) > ?
                     )
                   ON CONFLICT(source_key) DO UPDATE SET
                     window_id=excluded.window_id,
                     window_opens_at=excluded.window_opens_at,
                     run_id=excluded.run_id
                   WHERE source_heads.window_opens_at<=excluded.window_opens_at
                   RETURNING run_id"""
            ).bind(
                source_key,
                window_id,
                run_id,
                source_key,
                lease_owner,
                lease_generation,
                fence_now,
                *remote_commit_bindings,
                source_key,
                run_id,
                CLOSE_GUARD_MIN_COUNT,
                source_key,
                source_key,
                run_id,
                source_key,
                CLOSE_GUARD_RATIO,
            ),
            self.database.prepare(
                """INSERT OR IGNORE INTO job_changes(
                       run_id, source_key, external_id, kind, payload_json, occurred_at
                   )
                   SELECT ?, s.source_key, s.external_id, 'opened', s.payload_json, ?
                   FROM staged_jobs s
                   LEFT JOIN cloud_jobs c
                     ON c.source_key=s.source_key AND c.external_id=s.external_id
                   WHERE s.run_id=? AND s.source_key=? AND c.external_id IS NULL
                     AND """ + owns_source_head
            ).bind(
                run_id, now, run_id, source_key,
                source_key, run_id, window_id,
            ),
            self.database.prepare(
                """INSERT OR IGNORE INTO job_changes(
                       run_id, source_key, external_id, kind, payload_json, occurred_at
                   )
                   SELECT ?, s.source_key, s.external_id, 'updated', s.payload_json, ?
                   FROM staged_jobs s
                   JOIN cloud_jobs c
                     ON c.source_key=s.source_key AND c.external_id=s.external_id
                   WHERE s.run_id=? AND s.source_key=?
                     AND (c.fingerprint<>s.fingerprint OR c.closed_at IS NOT NULL)
                     AND """ + owns_source_head
            ).bind(
                run_id, now, run_id, source_key,
                source_key, run_id, window_id,
            ),
            self.database.prepare(
                """INSERT OR IGNORE INTO job_changes(
                       run_id, source_key, external_id, kind, payload_json, occurred_at
                   )
                   SELECT ?, c.source_key, c.external_id, 'closed', c.payload_json, ?
                   FROM cloud_jobs c
                   WHERE c.source_key=? AND c.closed_at IS NULL
                     AND NOT EXISTS(
                         SELECT 1 FROM staged_jobs s
                         WHERE s.run_id=? AND s.source_key=c.source_key
                           AND s.external_id=c.external_id
                     ) AND """ + owns_source_head
            ).bind(
                run_id, now, source_key, run_id,
                source_key, run_id, window_id,
            ),
            self.database.prepare(
                """INSERT INTO cloud_jobs(
                       source_key, external_id, company, fingerprint, payload_json,
                       first_seen_at, last_seen_at, closed_at
                   )
                   SELECT source_key, external_id,
                          json_extract(payload_json, '$.company'),
                          fingerprint, payload_json, ?, ?, NULL
                   FROM staged_jobs WHERE run_id=? AND source_key=?
                     AND """ + owns_source_head + """
                   ON CONFLICT(source_key, external_id) DO UPDATE SET
                       company=excluded.company,
                       fingerprint=excluded.fingerprint,
                       payload_json=excluded.payload_json,
                       last_seen_at=excluded.last_seen_at,
                       closed_at=NULL"""
            ).bind(
                now, now, run_id, source_key,
                source_key, run_id, window_id,
            ),
            self.database.prepare(
                """UPDATE cloud_jobs SET closed_at=?, last_seen_at=?
                   WHERE source_key=? AND closed_at IS NULL
                     AND NOT EXISTS(
                         SELECT 1 FROM staged_jobs s
                         WHERE s.run_id=? AND s.source_key=cloud_jobs.source_key
                           AND s.external_id=cloud_jobs.external_id
                     ) AND """ + owns_source_head
            ).bind(
                now, now, source_key, run_id,
                source_key, run_id, window_id,
            ),
            self.database.prepare(
                """UPDATE source_runs SET
                       status='success', completed_at=?, fetched_count=?,
                       snapshot_sha256=?, error_kind=NULL, error_message=NULL
                   WHERE id=? AND window_id=? AND source_key=? AND status='running'
                     AND """ + owns_source_head + """
                   RETURNING id"""
            ).bind(
                now,
                expected_count,
                expected_digest,
                run_id,
                window_id,
                source_key,
                source_key,
                run_id,
                window_id,
            ),
            self.database.prepare(
                """DELETE FROM staged_jobs WHERE run_id=? AND source_key=?
                   AND """ + owns_source_head
            ).bind(
                run_id, source_key,
                source_key, run_id, window_id,
            ),
        ]
        run_result_index = 6
        session_result_index = None
        window_result_index = None
        round_result_index = None
        if remote_session_id is not None:
            if collection_id is None or expected_source_count is None:
                raise ValueError("remote finalize requires collection metadata")
            session_result_index = len(statements)
            statements.append(
                self.database.prepare(
                    """UPDATE remote_ingest_sessions
                       SET status='committed', committed_at=?
                       WHERE id=? AND run_id=? AND status='committing'
                         AND EXISTS(
                           SELECT 1 FROM source_runs sr
                           WHERE sr.id=? AND sr.status='success'
                         )
                       RETURNING id"""
                ).bind(now, remote_session_id, run_id, run_id)
            )
            window_result_index = len(statements)
            statements.append(
                self.database.prepare(
                    """UPDATE collection_windows SET
                           status=CASE WHEN (
                             SELECT COUNT(DISTINCT source_key) FROM source_runs
                             WHERE window_id=? AND status='success'
                           )=? THEN 'complete' ELSE 'partial' END,
                           completed_at=CASE WHEN (
                             SELECT COUNT(DISTINCT source_key) FROM source_runs
                             WHERE window_id=? AND status='success'
                           )=? THEN ? ELSE NULL END,
                           last_error=CASE WHEN (
                             SELECT COUNT(DISTINCT source_key) FROM source_runs
                             WHERE window_id=? AND status='success'
                           )=? THEN NULL ELSE 'one or more sources incomplete' END,
                           lease_owner=NULL, lease_expires_at=NULL
                       WHERE id=? AND lease_owner=? AND lease_generation=?
                         AND EXISTS(
                           SELECT 1 FROM remote_ingest_sessions rs
                           WHERE rs.id=? AND rs.status='committed'
                         )
                       RETURNING status"""
                ).bind(
                    window_id,
                    expected_source_count,
                    window_id,
                    expected_source_count,
                    now,
                    window_id,
                    expected_source_count,
                    window_id,
                    lease_owner,
                    lease_generation,
                    remote_session_id,
                )
            )
            round_result_index = len(statements)
            statements.append(
                self.database.prepare(
                    """UPDATE remote_collection_rounds SET
                           status=CASE WHEN (
                             SELECT status FROM collection_windows WHERE id=?
                           )='complete' THEN 'complete' ELSE 'pending' END,
                           completed_at=CASE WHEN (
                             SELECT status FROM collection_windows WHERE id=?
                           )='complete' THEN ? ELSE NULL END
                       WHERE id=? AND window_id=?
                         AND status='pending' AND expires_at>?
                         AND EXISTS(
                           SELECT 1 FROM remote_ingest_sessions rs
                           WHERE rs.id=? AND rs.status='committed'
                         )
                       RETURNING status"""
                ).bind(
                    window_id,
                    window_id,
                    now,
                    collection_id,
                    window_id,
                    fence_now,
                    remote_session_id,
                )
            )

        results = await self.database.batch(statements)
        if _rows(results[0]) != [{"run_id": run_id}] or _rows(
            results[run_result_index]
        ) != [
            {"id": run_id}
        ]:
            counts, live_before = await self._change_counts(run_id, source_key)
            if close_guard_tripped(
                live_before=live_before, disappeared=counts.closed
            ):
                raise RuntimeError(
                    f"关闭守卫触发：{counts.closed}/{live_before} 个岗位消失，"
                    "拒绝发布云端关闭事实"
                )
            raise RuntimeError("source publication lease is stale")
        if remote_session_id is not None:
            if _rows(results[session_result_index]) != [{"id": remote_session_id}]:
                raise RuntimeError("remote session changed before atomic commit")
            if len(_rows(results[window_result_index])) != 1:
                raise RuntimeError("window lease changed before atomic commit")
            if len(_rows(results[round_result_index])) != 1:
                raise RuntimeError("collection round changed before atomic commit")
        return ApplyResult(
            opened=_changes(results[1]),
            updated=_changes(results[2]),
            closed=_changes(results[3]),
        )

    async def mark_failed(
        self,
        run_id: int,
        error_kind: str,
        error_message: str,
        now: str,
    ) -> None:
        await self.database.batch(
            [
                self.database.prepare(
                    """UPDATE source_runs SET
                           status='failed', completed_at=?, error_kind=?,
                           error_message=?
                       WHERE id=? AND status='running'"""
                ).bind(now, error_kind[:120], error_message[:1000], run_id),
                self.database.prepare(
                    "DELETE FROM staged_jobs WHERE run_id=?"
                ).bind(run_id),
            ]
        )

    async def successful_sources(self, window_id: int) -> set[str]:
        result = await self.database.prepare(
            """SELECT DISTINCT source_key FROM source_runs
               WHERE window_id=? AND status='success'"""
        ).bind(window_id).run()
        return {str(row["source_key"]) for row in _rows(result)}

    async def has_official_window(self, workday: str) -> bool:
        result = await self.database.prepare(
            """SELECT 1 FROM collection_windows
               WHERE workday=? AND window_key<>'technical-trial' LIMIT 1"""
        ).bind(workday).run()
        return bool(_rows(result))

    async def mark_missed(
        self,
        window: Window,
        expected_sources: set[str],
        now: str,
    ) -> str:
        window_id = await self.get_or_create_window(window, now)
        successful = await self.successful_sources(window_id)
        if successful == expected_sources:
            return "complete"
        result = await self.database.prepare(
            """UPDATE collection_windows SET
                   status='missed', completed_at=?,
                   last_error='collection grace elapsed before all sources succeeded',
                   lease_owner=NULL, lease_expires_at=NULL
               WHERE id=? AND status<>'complete'
                 AND (
                   lease_owner IS NULL OR lease_expires_at IS NULL
                   OR lease_expires_at<=?
                 )
               RETURNING status"""
        ).bind(now, window_id, now).run()
        rows = _rows(result)
        if rows:
            return str(rows[0]["status"])
        summary = await self.window_summary(window_id)
        return str(summary["status"])

    async def finish_window(
        self,
        window_id: int,
        expected_sources: set[str],
        now: str,
        owner: str | None = None,
        lease_generation: int | None = None,
    ) -> str:
        successful = await self.successful_sources(window_id)
        status = "complete" if successful == expected_sources else "partial"
        completed_at = now if status == "complete" else None
        result = await self.database.prepare(
            """UPDATE collection_windows SET
                   status=?, completed_at=?, last_error=?,
                   lease_owner=NULL, lease_expires_at=NULL
               WHERE id=?
                 AND (? IS NULL OR lease_owner=?)
                 AND (? IS NULL OR lease_generation=?)
               RETURNING status"""
        ).bind(
            status,
            completed_at,
            None if status == "complete" else "one or more sources incomplete",
            window_id,
            owner,
            owner,
            lease_generation,
            lease_generation,
        ).run()
        if len(_rows(result)) != 1:
            raise RuntimeError("window lease changed before final readback")
        return status

    async def window_summary(self, window_id: int) -> dict:
        window_result = await self.database.prepare(
            """SELECT id, workday, window_key, opens_at, closes_at, status,
                      completed_at
               FROM collection_windows WHERE id=?"""
        ).bind(window_id).run()
        windows = _rows(window_result)
        if len(windows) != 1:
            raise RuntimeError("window summary did not return exactly one row")

        runs_result = await self.database.prepare(
            """SELECT id, source_key, attempt, status, started_at, completed_at,
                      fetched_count, snapshot_sha256, error_kind, error_message
               FROM source_runs WHERE window_id=?
               ORDER BY source_key, attempt DESC"""
        ).bind(window_id).run()
        latest: dict[str, dict] = {}
        for row in _rows(runs_result):
            latest.setdefault(str(row["source_key"]), row)
        window = windows[0]
        return {
            "id": int(window["id"]),
            "workday": window["workday"],
            "window_key": window["window_key"],
            "opens_at": window["opens_at"],
            "closes_at": window["closes_at"],
            "status": window["status"],
            "completed_at": window["completed_at"],
            "sources": [latest[key] for key in sorted(latest)],
        }

    async def status_for_day(self, workday: str) -> dict:
        result = await self.database.prepare(
            """SELECT id FROM collection_windows
               WHERE workday=? ORDER BY opens_at"""
        ).bind(workday).run()
        windows = [await self.window_summary(int(row["id"])) for row in _rows(result)]
        return {"workday": workday, "windows": windows}

    async def claim_catch_up(self, now: str, allowed_after: str) -> bool:
        result = await self.database.prepare(
            """INSERT INTO catch_up_gate(id, last_requested_at) VALUES(1, ?)
               ON CONFLICT(id) DO UPDATE SET
                 last_requested_at=excluded.last_requested_at
               WHERE catch_up_gate.last_requested_at<=?
               RETURNING id"""
        ).bind(now, allowed_after).run()
        return _rows(result) == [{"id": 1}]

    async def changes_after(self, after: int, *, limit: int = 200) -> dict:
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid change cursor or limit")
        result = await self.database.prepare(
            """SELECT cursor, source_key, external_id, kind, payload_json,
                      occurred_at
               FROM job_changes WHERE cursor>?
               ORDER BY cursor LIMIT ?"""
        ).bind(after, limit + 1).run()
        rows = _rows(result)
        has_more = len(rows) > limit
        rows = rows[:limit]
        changes = [
            {
                "cursor": int(row["cursor"]),
                "source_key": row["source_key"],
                "external_id": row["external_id"],
                "kind": row["kind"],
                "job": json.loads(row["payload_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]
        return {
            "changes": changes,
            "next_cursor": int(changes[-1]["cursor"]) if changes else after,
            "has_more": has_more,
        }

    async def ack_client(self, client_id: str, cursor: int, now: str) -> int:
        if cursor < 0:
            raise ValueError("ack cursor must be non-negative")
        max_result = await self.database.prepare(
            "SELECT COALESCE(MAX(cursor), 0) AS cursor FROM job_changes"
        ).run()
        maximum = int(_rows(max_result)[0]["cursor"])
        if cursor > maximum:
            raise ValueError("ack cursor is ahead of cloud history")
        await self.database.prepare(
            """INSERT INTO sync_clients(client_id, acknowledged_cursor, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(client_id) DO UPDATE SET
                 acknowledged_cursor=MAX(
                   sync_clients.acknowledged_cursor,
                   excluded.acknowledged_cursor
                 ),
                 updated_at=excluded.updated_at"""
        ).bind(client_id, cursor, now).run()
        result = await self.database.prepare(
            "SELECT acknowledged_cursor FROM sync_clients WHERE client_id=?"
        ).bind(client_id).run()
        rows = _rows(result)
        if len(rows) != 1:
            raise RuntimeError("client ack readback did not return exactly one row")
        return int(rows[0]["acknowledged_cursor"])

    async def _remote_session(self, session_id: str) -> dict | None:
        result = await self.database.prepare(
            """SELECT s.*, w.window_key, w.workday,
                      w.lease_owner AS window_lease_owner,
                      w.lease_generation AS window_lease_generation,
                      w.lease_expires_at AS window_lease_expires_at
               FROM remote_ingest_sessions s
               JOIN collection_windows w ON w.id=s.window_id
               WHERE s.id=?"""
        ).bind(session_id).run()
        rows = _rows(result)
        return rows[0] if len(rows) == 1 else None

    async def _remote_round(self, collection_id: str) -> dict | None:
        result = await self.database.prepare(
            """SELECT r.*, w.window_key, w.workday
               FROM remote_collection_rounds r
               JOIN collection_windows w ON w.id=r.window_id
               WHERE r.id=?"""
        ).bind(collection_id).run()
        rows = _rows(result)
        return rows[0] if len(rows) == 1 else None

    async def expire_remote_sessions(self, now: str) -> None:
        """收口已过期上传，避免 running run 和分块永久堆积。"""
        expired_runs = """SELECT run_id FROM remote_ingest_sessions
                            WHERE status IN ('pending','committing')
                              AND (
                                expires_at<=? OR collection_id IN (
                                  SELECT id FROM remote_collection_rounds
                                  WHERE status='failed' OR expires_at<=?
                                )
                              )"""
        expired_sessions = """SELECT id FROM remote_ingest_sessions
                                WHERE status IN ('pending','committing')
                                  AND (
                                    expires_at<=? OR collection_id IN (
                                      SELECT id FROM remote_collection_rounds
                                      WHERE status='failed' OR expires_at<=?
                                    )
                                  )"""
        await self.database.batch(
            [
                self.database.prepare(
                    f"""UPDATE source_runs SET status='failed', completed_at=?,
                               error_kind='RemoteIngestExpired',
                               error_message='remote ingest session expired'
                         WHERE id IN ({expired_runs}) AND status='running'"""
                ).bind(now, now, now),
                self.database.prepare(
                    f"DELETE FROM staged_jobs WHERE run_id IN ({expired_runs})"
                ).bind(now, now),
                self.database.prepare(
                    f"""DELETE FROM remote_ingest_chunks
                        WHERE session_id IN ({expired_sessions})"""
                ).bind(now, now),
                self.database.prepare(
                    """UPDATE remote_ingest_sessions SET status='failed'
                       WHERE status IN ('pending','committing') AND (
                         expires_at<=? OR collection_id IN (
                           SELECT id FROM remote_collection_rounds
                           WHERE status='failed' OR expires_at<=?
                         )
                       )"""
                ).bind(now, now),
                self.database.prepare(
                    """UPDATE remote_collection_rounds SET status='failed'
                       WHERE status='pending' AND expires_at<=?"""
                ).bind(now),
            ]
        )

    async def begin_remote_session(
        self,
        *,
        session_id: str,
        collection_id: str,
        window: Window | None,
        source_key: str,
        mode: str,
        expected_count: int,
        expected_bytes: int,
        snapshot_sha256: str,
        now: str,
        expires_at: str,
    ) -> tuple[dict, bool]:
        await self.expire_remote_sessions(now)
        existing_round = await self._remote_round(collection_id)
        if existing_round is not None and str(existing_round["mode"]) != mode:
            raise ValueError("collection_id 已绑定到不同模式")
        if existing_round is not None:
            expires_at = min(expires_at, str(existing_round["expires_at"]))
        if existing_round is None:
            if window is None:
                raise RuntimeError("no active or just-ended collection window")
            if mode == "technical-trial":
                window = Window(
                    workday=f"trial:{collection_id}",
                    key=window.key,
                    opens_at=window.opens_at,
                    closes_at=window.closes_at,
                )
            candidate_window_id = await self.get_or_create_window(window, now)
        else:
            candidate_window_id = int(existing_round["window_id"])

        prior_success = await self.database.prepare(
            """SELECT fetched_count, snapshot_sha256 FROM source_runs
               WHERE window_id=? AND source_key=? AND status='success'"""
        ).bind(candidate_window_id, source_key).run()
        successes = _rows(prior_success)
        if successes:
            success = successes[0]
            window_key = (
                str(existing_round["window_key"])
                if existing_round is not None
                else str(window.key)
            )
            workday = (
                str(existing_round["workday"])
                if existing_round is not None
                else str(window.workday)
            )
            return {
                "id": session_id,
                "collection_id": collection_id,
                "window_id": candidate_window_id,
                "window_key": window_key,
                "workday": workday,
                "source_key": source_key,
                "mode": mode,
                "expected_count": int(success["fetched_count"]),
                "expected_bytes": expected_bytes,
                "snapshot_sha256": str(success["snapshot_sha256"]),
                "status": "committed",
            }, False
        if existing_round is not None and (
            str(existing_round["status"]) != "pending"
            or str(existing_round["expires_at"]) <= now
        ):
            raise RuntimeError("collection round is expired or closed")

        statements = [
            self.database.prepare(
                """INSERT INTO remote_collection_rounds(
                       id, window_id, mode, expected_source_count,
                       status, created_at, expires_at
                   )
                   SELECT ?,?,?,5,'pending',?,?
                   WHERE COALESCE((
                     SELECT SUM(expected_count) FROM remote_ingest_sessions
                     WHERE window_id=?
                       AND status IN ('pending','committing','committed')
                   ),0) + ? <= 30000
                     AND COALESCE((
                       SELECT SUM(expected_bytes) FROM remote_ingest_sessions
                       WHERE window_id=?
                         AND status IN ('pending','committing','committed')
                     ),0) + ? <= 75000000
                   ON CONFLICT(id) DO NOTHING"""
            ).bind(
                collection_id,
                candidate_window_id,
                mode,
                now,
                expires_at,
                candidate_window_id,
                expected_count,
                candidate_window_id,
                expected_bytes,
            ),
            self.database.prepare(
                """UPDATE collection_windows SET
                       status='running',
                       lease_generation=CASE
                         WHEN lease_owner=? THEN lease_generation
                         ELSE lease_generation + 1
                       END,
                       lease_owner=?, lease_expires_at=?
                   WHERE id=(SELECT window_id FROM remote_collection_rounds WHERE id=?)
                     AND status<>'complete'
                     AND NOT EXISTS(
                       SELECT 1 FROM remote_ingest_sessions
                       WHERE id=? AND status='committed'
                     )
                     AND NOT EXISTS(
                       SELECT 1 FROM source_runs
                       WHERE window_id=collection_windows.id
                         AND source_key=? AND status='success'
                     )
                     AND NOT EXISTS(
                       SELECT 1 FROM remote_ingest_sessions
                       WHERE id=? AND (
                         collection_id<>? OR source_key<>? OR mode<>?
                         OR expected_count<>? OR expected_bytes<>?
                         OR snapshot_sha256<>?
                       )
                     )
                     AND NOT EXISTS(
                       SELECT 1 FROM remote_ingest_sessions
                       WHERE collection_id=? AND source_key=? AND id<>?
                     )
                     AND COALESCE((
                       SELECT SUM(expected_count) FROM remote_ingest_sessions
                       WHERE window_id=collection_windows.id
                         AND status IN ('pending','committing','committed')
                     ),0) + ? <= 30000
                     AND COALESCE((
                       SELECT SUM(expected_bytes) FROM remote_ingest_sessions
                       WHERE window_id=collection_windows.id
                         AND status IN ('pending','committing','committed')
                     ),0) + ? <= 75000000
                     AND EXISTS(
                       SELECT 1 FROM remote_collection_rounds r
                       WHERE r.id=? AND r.window_id=collection_windows.id
                         AND r.mode=? AND r.status='pending' AND r.expires_at>?
                     )
                     AND (
                       lease_owner IS NULL OR lease_expires_at IS NULL
                       OR lease_expires_at<=? OR lease_owner=?
                     )
                   RETURNING lease_generation"""
            ).bind(
                session_id,
                session_id,
                expires_at,
                collection_id,
                session_id,
                source_key,
                session_id,
                collection_id,
                source_key,
                mode,
                expected_count,
                expected_bytes,
                snapshot_sha256,
                collection_id,
                source_key,
                session_id,
                expected_count,
                expected_bytes,
                collection_id,
                mode,
                now,
                now,
                session_id,
            ),
            self.database.prepare(
                """INSERT INTO source_runs(
                       window_id, source_key, attempt, status, started_at,
                       remote_session_id
                   )
                   SELECT r.window_id, ?,
                          COALESCE((SELECT MAX(attempt)+1 FROM source_runs
                                    WHERE window_id=r.window_id AND source_key=?),1),
                          'running', ?, ?
                   FROM remote_collection_rounds r
                   JOIN collection_windows w ON w.id=r.window_id
                   WHERE r.id=? AND r.mode=?
                     AND w.lease_owner=? AND w.lease_expires_at>?
                     AND NOT EXISTS(
                       SELECT 1 FROM remote_ingest_sessions WHERE id=?
                     )
                     AND NOT EXISTS(
                       SELECT 1 FROM source_runs
                       WHERE window_id=r.window_id AND source_key=? AND status='success'
                     )
                     AND COALESCE((
                       SELECT SUM(expected_count) FROM remote_ingest_sessions
                       WHERE window_id=r.window_id
                         AND status IN ('pending','committing','committed')
                     ),0) + ? <= 30000
                     AND COALESCE((
                       SELECT SUM(expected_bytes) FROM remote_ingest_sessions
                       WHERE window_id=r.window_id
                         AND status IN ('pending','committing','committed')
                     ),0) + ? <= 75000000
                     AND r.status='pending' AND r.expires_at>?
                   ON CONFLICT DO NOTHING
                   RETURNING id"""
            ).bind(
                source_key,
                source_key,
                now,
                session_id,
                collection_id,
                mode,
                session_id,
                now,
                session_id,
                source_key,
                expected_count,
                expected_bytes,
                now,
            ),
            self.database.prepare(
                """INSERT INTO remote_ingest_sessions(
                       id, collection_id, window_id, run_id, source_key, mode,
                       expected_count, expected_bytes, snapshot_sha256, lease_owner,
                       lease_generation, expires_at, status, created_at
                   )
                   SELECT ?, r.id, r.window_id, sr.id, ?, ?, ?, ?, ?, ?,
                          w.lease_generation, ?, 'pending', ?
                   FROM remote_collection_rounds r
                   JOIN collection_windows w ON w.id=r.window_id
                   JOIN source_runs sr ON sr.remote_session_id=?
                   WHERE r.id=? AND r.mode=?
                   ON CONFLICT DO NOTHING
                   RETURNING id"""
            ).bind(
                session_id,
                source_key,
                mode,
                expected_count,
                expected_bytes,
                snapshot_sha256,
                session_id,
                expires_at,
                now,
                session_id,
                collection_id,
                mode,
            ),
            self.database.prepare(
                """SELECT s.*, w.window_key, w.workday,
                          w.lease_owner AS window_lease_owner,
                          w.lease_generation AS window_lease_generation,
                          w.lease_expires_at AS window_lease_expires_at
                   FROM remote_ingest_sessions s
                   JOIN collection_windows w ON w.id=s.window_id
                   WHERE s.id=?"""
            ).bind(session_id),
            self.database.prepare(
                """SELECT sr.fetched_count, sr.snapshot_sha256,
                          r.window_id, w.window_key, w.workday
                   FROM remote_collection_rounds r
                   JOIN collection_windows w ON w.id=r.window_id
                   JOIN source_runs sr ON sr.window_id=r.window_id
                   WHERE r.id=? AND sr.source_key=? AND sr.status='success'"""
            ).bind(collection_id, source_key),
            self.database.prepare(
                """SELECT COALESCE(SUM(expected_count),0) AS total,
                          COALESCE(SUM(expected_bytes),0) AS total_bytes
                   FROM remote_ingest_sessions WHERE window_id=?
                     AND status IN ('pending','committing','committed')"""
            ).bind(candidate_window_id),
            self.database.prepare(
                """SELECT id FROM remote_ingest_sessions
                   WHERE collection_id=? AND source_key=?"""
            ).bind(collection_id, source_key),
            self.database.prepare(
                """SELECT r.mode, r.window_id, w.window_key, w.workday
                   FROM remote_collection_rounds r
                   JOIN collection_windows w ON w.id=r.window_id
                   WHERE r.id=?"""
            ).bind(collection_id),
        ]
        results = await self.database.batch(statements)
        rounds = _rows(results[8])
        totals = _rows(results[6])[0]
        total = int(totals["total"])
        total_bytes = int(totals["total_bytes"])
        if not rounds and (
            total + expected_count > 30000
            or total_bytes + expected_bytes > 75000000
        ):
            raise ValueError("五源岗位总量超过服务端安全上限")
        if len(rounds) != 1 or str(rounds[0]["mode"]) != mode:
            raise ValueError("collection_id 已绑定到不同模式")

        sessions = _rows(results[4])
        if sessions:
            session = sessions[0]
            expected = (
                collection_id,
                source_key,
                mode,
                expected_count,
                expected_bytes,
                snapshot_sha256,
            )
            observed = (
                str(session["collection_id"]),
                str(session["source_key"]),
                str(session["mode"]),
                int(session["expected_count"]),
                int(session["expected_bytes"]),
                str(session["snapshot_sha256"]),
            )
            if observed != expected:
                raise ValueError("session_id 已绑定到不同的快照输入")
            return session, _changes(results[3]) == 1

        successes = _rows(results[5])
        if successes:
            success = successes[0]
            return {
                "id": session_id,
                "collection_id": collection_id,
                "window_id": int(success["window_id"]),
                "window_key": str(success["window_key"]),
                "workday": str(success["workday"]),
                "source_key": source_key,
                "mode": mode,
                "expected_count": int(success["fetched_count"]),
                "snapshot_sha256": str(success["snapshot_sha256"]),
                "status": "committed",
            }, False
        if (
            total + expected_count > 30000
            or total_bytes + expected_bytes > 75000000
        ):
            raise ValueError("五源岗位总量超过服务端安全上限")
        bound = _rows(results[7])
        if bound:
            raise ValueError("同一 collection 的来源已绑定到其他 session")
        if not _rows(results[1]):
            raise RuntimeError("collection window is busy")
        raise RuntimeError("remote ingest session could not be created")

    async def stage_remote_chunk(
        self,
        *,
        session_id: str,
        chunk_index: int,
        chunk_sha256: str,
        payloads: list[dict],
        company: str,
        now: str,
    ) -> str:
        session = await self._remote_session(session_id)
        if session is None:
            raise LookupError("remote ingest session not found")
        if session["status"] != "pending":
            raise ValueError("remote ingest session is not pending")
        if (
            str(session["window_lease_owner"]) != session_id
            or int(session["window_lease_generation"])
            != int(session["lease_generation"])
            or not session["window_lease_expires_at"]
            or str(session["window_lease_expires_at"]) <= now
        ):
            raise RuntimeError("remote ingest lease is stale")

        if not 1 <= len(payloads) <= 40:
            raise ValueError("remote ingest chunk must contain 1-40 jobs")
        if snapshot_digest(payloads) != chunk_sha256:
            raise ValueError("chunk digest does not match jobs")
        source_key = str(session["source_key"])
        staged: list[StagedJob] = []
        chunk_byte_count = 0
        for payload in payloads:
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload_bytes = len(payload_json.encode("utf-8"))
            if payload_bytes > 1_000_000:
                raise ValueError("单岗位 JSON 超过 D1 安全上限")
            chunk_byte_count += payload_bytes
            if payload.get("source_key") != source_key:
                raise ValueError("job source_key does not match session")
            if payload.get("company") != company:
                raise ValueError("job company does not match approved source")
            staged.append(
                StagedJob(
                    external_id=str(payload.get("external_id") or ""),
                    fingerprint=payload_fingerprint(payload),
                    payload=payload,
                )
            )
        if len({job.external_id for job in staged}) != len(staged):
            raise ValueError("chunk contains duplicate external_id values")
        if any(not job.external_id.strip() for job in staged):
            raise ValueError("chunk contains empty external_id")
        if chunk_byte_count > 1_500_000:
            raise ValueError("remote ingest chunk exceeds byte limit")
        statements = [
            self.database.prepare(
                """INSERT INTO remote_ingest_chunks(
                       session_id, chunk_index, chunk_sha256, row_count,
                       byte_count, received_at
                   )
                   SELECT ?,?,?,?,?,?
                   FROM remote_ingest_sessions s
                   JOIN collection_windows w ON w.id=s.window_id
                   JOIN remote_collection_rounds r ON r.id=s.collection_id
                   WHERE s.id=? AND s.status='pending' AND s.expires_at>?
                     AND r.status='pending' AND r.expires_at>?
                     AND w.lease_owner=s.lease_owner
                     AND w.lease_generation=s.lease_generation
                     AND w.lease_expires_at>?
                     AND COALESCE((
                       SELECT SUM(c.row_count) FROM remote_ingest_chunks c
                       WHERE c.session_id=s.id
                     ),0) + ? <= s.expected_count
                     AND COALESCE((
                       SELECT SUM(c.byte_count) FROM remote_ingest_chunks c
                       WHERE c.session_id=s.id
                     ),0) + ? <= s.expected_bytes
                   ON CONFLICT(session_id, chunk_index) DO NOTHING
                   RETURNING chunk_index"""
            ).bind(
                session_id,
                chunk_index,
                chunk_sha256,
                len(staged),
                chunk_byte_count,
                now,
                session_id,
                now,
                now,
                now,
                len(staged),
                chunk_byte_count,
            )
        ]
        insert = self.database.prepare(
            """INSERT INTO staged_jobs(
                   run_id, source_key, external_id, fingerprint, payload_json
               )
               SELECT ?,?,?,?,?
               WHERE EXISTS(
                 SELECT 1 FROM remote_ingest_chunks c
                 JOIN remote_ingest_sessions rs ON rs.id=c.session_id
                 JOIN collection_windows w ON w.id=rs.window_id
                 JOIN remote_collection_rounds r ON r.id=rs.collection_id
                 WHERE c.session_id=? AND c.chunk_index=?
                   AND c.chunk_sha256=? AND c.row_count=? AND c.byte_count=?
                   AND rs.status='pending' AND rs.expires_at>?
                   AND r.status='pending' AND r.expires_at>?
                   AND w.lease_owner=rs.lease_owner
                   AND w.lease_generation=rs.lease_generation
                   AND w.lease_expires_at>?
               )
               ON CONFLICT(run_id, source_key, external_id) DO UPDATE SET
                 fingerprint=excluded.fingerprint,
                 payload_json=excluded.payload_json"""
        )
        for job in staged:
            statements.append(
                insert.bind(
                    int(session["run_id"]),
                    source_key,
                    job.external_id,
                    job.fingerprint,
                    json.dumps(
                        job.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    session_id,
                    chunk_index,
                    chunk_sha256,
                    len(staged),
                    chunk_byte_count,
                    now,
                    now,
                    now,
                )
            )
        statements.append(
            self.database.prepare(
                """SELECT c.chunk_sha256, c.row_count, c.byte_count
                   FROM remote_ingest_chunks c
                   JOIN remote_ingest_sessions s ON s.id=c.session_id
                   JOIN collection_windows w ON w.id=s.window_id
                   JOIN remote_collection_rounds r ON r.id=s.collection_id
                   WHERE c.session_id=? AND c.chunk_index=?
                     AND s.status='pending' AND s.expires_at>?
                     AND r.status='pending' AND r.expires_at>?
                     AND w.lease_owner=s.lease_owner
                     AND w.lease_generation=s.lease_generation
                     AND w.lease_expires_at>?"""
            ).bind(session_id, chunk_index, now, now, now)
        )
        results = await self.database.batch(statements)
        marker = _rows(results[-1])
        if len(marker) != 1:
            raise RuntimeError("remote ingest chunk readback failed")
        if (
            str(marker[0]["chunk_sha256"]) != chunk_sha256
            or int(marker[0]["row_count"]) != len(staged)
            or int(marker[0]["byte_count"]) != chunk_byte_count
        ):
            raise ValueError("chunk index is already bound to different content")
        return "stored" if _rows(results[0]) else "already-stored"

    async def _seal_remote_session(self, session_id: str, now: str) -> dict:
        """原子冻结分块集合；seal 后迟到的 PUT 不再能写入。"""
        result = await self.database.prepare(
            """UPDATE remote_ingest_sessions SET status='committing'
               WHERE id=? AND status='pending' AND expires_at>?
                 AND EXISTS(
                   SELECT 1 FROM collection_windows w
                   JOIN remote_collection_rounds r
                     ON r.id=remote_ingest_sessions.collection_id
                   WHERE w.id=remote_ingest_sessions.window_id
                     AND w.lease_owner=remote_ingest_sessions.lease_owner
                     AND w.lease_generation=remote_ingest_sessions.lease_generation
                     AND w.lease_expires_at>?
                     AND r.status='pending' AND r.expires_at>?
                 )
               RETURNING id"""
        ).bind(session_id, now, now, now).run()
        if _rows(result) != [{"id": session_id}]:
            current = await self._remote_session(session_id)
            if current is None or current["status"] != "committing":
                raise RuntimeError("remote ingest session could not be sealed")
            return current
        sealed = await self._remote_session(session_id)
        if sealed is None or sealed["status"] != "committing":
            raise RuntimeError("remote ingest session seal readback failed")
        return sealed

    async def commit_remote_session(
        self,
        *,
        session_id: str,
        now: str,
        expected_sources: set[str],
    ) -> dict:
        session = await self._remote_session(session_id)
        if session is None:
            raise LookupError("remote ingest session not found")
        if session["status"] == "committed":
            summary = await self.window_summary(int(session["window_id"]))
            return {
                "status": "committed",
                "collection_id": str(session["collection_id"]),
                "source_key": session["source_key"],
                "fetched_count": int(session["expected_count"]),
                "snapshot_sha256": session["snapshot_sha256"],
                "window_status": summary["status"],
                "window_id": int(session["window_id"]),
                "window_key": str(session["window_key"]),
            }
        if session["status"] not in {"pending", "committing"}:
            raise ValueError("remote ingest session is not pending or committing")
        if (
            str(session["window_lease_owner"]) != session_id
            or int(session["window_lease_generation"])
            != int(session["lease_generation"])
            or not session["window_lease_expires_at"]
            or str(session["window_lease_expires_at"]) <= now
        ):
            raise RuntimeError("remote ingest lease is stale")

        session = await self._seal_remote_session(session_id, now)

        chunk_result = await self.database.prepare(
            """SELECT chunk_index, row_count, byte_count FROM remote_ingest_chunks
               WHERE session_id=? ORDER BY chunk_index"""
        ).bind(session_id).run()
        chunks = _rows(chunk_result)
        if [int(row["chunk_index"]) for row in chunks] != list(range(len(chunks))):
            raise ValueError("remote ingest chunks are not contiguous from zero")
        if sum(int(row["row_count"]) for row in chunks) != int(
            session["expected_count"]
        ):
            raise ValueError("remote ingest chunk row count is incomplete")
        if sum(int(row["byte_count"]) for row in chunks) != int(
            session["expected_bytes"]
        ):
            raise ValueError("remote ingest chunk byte count is incomplete")

        await self.finalize_snapshot(
            int(session["run_id"]),
            int(session["window_id"]),
            str(session["source_key"]),
            str(session["snapshot_sha256"]),
            int(session["expected_count"]),
            now,
            lease_owner=session_id,
            lease_generation=int(session["lease_generation"]),
            fence_now=now,
            remote_session_id=session_id,
            collection_id=str(session["collection_id"]),
            expected_source_count=len(expected_sources),
        )
        window_status = (await self.window_summary(int(session["window_id"])))[
            "status"
        ]
        return {
            "status": "committed",
            "collection_id": str(session["collection_id"]),
            "source_key": session["source_key"],
            "fetched_count": int(session["expected_count"]),
            "snapshot_sha256": session["snapshot_sha256"],
            "window_status": window_status,
            "window_id": int(session["window_id"]),
            "window_key": str(session["window_key"]),
        }
