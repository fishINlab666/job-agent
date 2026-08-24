from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


@dataclass
class FakeResult:
    results: list[dict]
    meta: SimpleNamespace
    success: bool = True


class FakePrepared:
    def __init__(self, database: "FakeD1", sql: str, params: tuple = ()) -> None:
        self.database = database
        self.sql = sql
        self.params = params

    def bind(self, *params):
        return FakePrepared(self.database, self.sql, tuple(params))

    async def run(self) -> FakeResult:
        return self.database.execute(self.sql, self.params)

    async def first(self):
        result = await self.run()
        return result.results[0] if result.results else None


class FakeD1:
    def __init__(self, migration: Path) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(migration.read_text(encoding="utf-8"))

    def prepare(self, sql: str) -> FakePrepared:
        return FakePrepared(self, sql)

    def execute(self, sql: str, params: tuple = ()) -> FakeResult:
        cursor = self.conn.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        self.conn.commit()
        return FakeResult(
            results=rows,
            meta=SimpleNamespace(
                last_row_id=cursor.lastrowid,
                changes=max(cursor.rowcount, 0),
            ),
        )

    async def batch(self, statements: list[FakePrepared]) -> list[FakeResult]:
        results = []
        self.conn.execute("BEGIN")
        try:
            for statement in statements:
                cursor = self.conn.execute(statement.sql, statement.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(
                    FakeResult(
                        results=rows,
                        meta=SimpleNamespace(
                            last_row_id=cursor.lastrowid,
                            changes=max(cursor.rowcount, 0),
                        ),
                    )
                )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return results
