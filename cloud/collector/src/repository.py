"""Cloudflare D1 persistence adapter for public collection truth."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jobagent.collection import snapshot_digest

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
    ) -> bool:
        result = await self.database.prepare(
            """UPDATE collection_windows SET
                   status='running', lease_owner=?, lease_expires_at=?
               WHERE id=? AND status<>'complete'
                 AND (
                   lease_owner IS NULL OR lease_expires_at IS NULL
                   OR lease_expires_at<=? OR lease_owner=?
                 )
               RETURNING id"""
        ).bind(owner, expires_at, window_id, now, owner).run()
        return len(_rows(result)) == 1

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
    ) -> ApplyResult:
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
        ]
        results = await self.database.batch(statements)
        counts = [int(_rows(result)[0]["n"]) for result in results]
        return ApplyResult(*counts)

    async def finalize_snapshot(
        self,
        run_id: int,
        window_id: int,
        source_key: str,
        expected_digest: str,
        expected_count: int,
        now: str,
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
        counts = await self._change_counts(run_id, source_key)

        statements = [
            self.database.prepare(
                """INSERT OR IGNORE INTO job_changes(
                       run_id, source_key, external_id, kind, payload_json, occurred_at
                   )
                   SELECT ?, s.source_key, s.external_id, 'opened', s.payload_json, ?
                   FROM staged_jobs s
                   LEFT JOIN cloud_jobs c
                     ON c.source_key=s.source_key AND c.external_id=s.external_id
                   WHERE s.run_id=? AND s.source_key=? AND c.external_id IS NULL"""
            ).bind(run_id, now, run_id, source_key),
            self.database.prepare(
                """INSERT OR IGNORE INTO job_changes(
                       run_id, source_key, external_id, kind, payload_json, occurred_at
                   )
                   SELECT ?, s.source_key, s.external_id, 'updated', s.payload_json, ?
                   FROM staged_jobs s
                   JOIN cloud_jobs c
                     ON c.source_key=s.source_key AND c.external_id=s.external_id
                   WHERE s.run_id=? AND s.source_key=?
                     AND (c.fingerprint<>s.fingerprint OR c.closed_at IS NOT NULL)"""
            ).bind(run_id, now, run_id, source_key),
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
                     )"""
            ).bind(run_id, now, source_key, run_id),
            self.database.prepare(
                """INSERT INTO cloud_jobs(
                       source_key, external_id, company, fingerprint, payload_json,
                       first_seen_at, last_seen_at, closed_at
                   )
                   SELECT source_key, external_id,
                          json_extract(payload_json, '$.company'),
                          fingerprint, payload_json, ?, ?, NULL
                   FROM staged_jobs WHERE run_id=? AND source_key=? AND 1
                   ON CONFLICT(source_key, external_id) DO UPDATE SET
                       company=excluded.company,
                       fingerprint=excluded.fingerprint,
                       payload_json=excluded.payload_json,
                       last_seen_at=excluded.last_seen_at,
                       closed_at=NULL"""
            ).bind(now, now, run_id, source_key),
            self.database.prepare(
                """UPDATE cloud_jobs SET closed_at=?, last_seen_at=?
                   WHERE source_key=? AND closed_at IS NULL
                     AND NOT EXISTS(
                         SELECT 1 FROM staged_jobs s
                         WHERE s.run_id=? AND s.source_key=cloud_jobs.source_key
                           AND s.external_id=cloud_jobs.external_id
                     )"""
            ).bind(now, now, source_key, run_id),
            self.database.prepare(
                """UPDATE source_runs SET
                       status='success', completed_at=?, fetched_count=?,
                       snapshot_sha256=?, error_kind=NULL, error_message=NULL
                   WHERE id=? AND window_id=? AND source_key=? AND status='running'"""
            ).bind(
                now,
                expected_count,
                expected_digest,
                run_id,
                window_id,
                source_key,
            ),
            self.database.prepare(
                "DELETE FROM staged_jobs WHERE run_id=? AND source_key=?"
            ).bind(run_id, source_key),
        ]
        await self.database.batch(statements)
        return counts

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

    async def finish_window(
        self,
        window_id: int,
        expected_sources: set[str],
        now: str,
        owner: str | None = None,
    ) -> str:
        successful = await self.successful_sources(window_id)
        status = "complete" if successful == expected_sources else "partial"
        completed_at = now if status == "complete" else None
        result = await self.database.prepare(
            """UPDATE collection_windows SET
                   status=?, completed_at=?, last_error=?,
                   lease_owner=NULL, lease_expires_at=NULL
               WHERE id=? AND (? IS NULL OR lease_owner=?)
               RETURNING status"""
        ).bind(
            status,
            completed_at,
            None if status == "complete" else "one or more sources incomplete",
            window_id,
            owner,
            owner,
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
