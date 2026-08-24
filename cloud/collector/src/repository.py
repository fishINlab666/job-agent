"""Cloudflare D1 persistence adapter for public collection truth."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jobagent.collection import snapshot_digest

from .windowing import Window


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
