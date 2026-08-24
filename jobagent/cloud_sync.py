"""云端公开岗位的本机增量同步与缺口兜底。"""
from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import db, ingest
from .adapters.base import RawJob
from .targets import OBSERVATION_SOURCES


CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "jobagent" / "cloud.json"
SOURCE_BY_KEY = {str(spec["source_key"]): spec for spec in OBSERVATION_SOURCES}


@dataclass(frozen=True)
class CloudConfig:
    base_url: str
    token: str
    client_id: str

    @classmethod
    def load(cls, path: Path | None = None) -> "CloudConfig":
        config_path = Path(path or DEFAULT_CONFIG_PATH)
        if config_path.is_symlink() or not config_path.is_file():
            raise ValueError(f"云端配置必须是普通文件：{config_path}")
        if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
            raise ValueError(f"云端配置权限必须是 0600：{config_path}")
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if set(raw) != {"base_url", "token", "client_id"}:
            raise ValueError("云端配置只能包含 base_url、token、client_id")
        base_url = str(raw["base_url"]).rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path:
            raise ValueError("base_url 必须是无路径的 HTTPS 地址")
        token = str(raw["token"])
        client_id = str(raw["client_id"])
        if len(token) < 32:
            raise ValueError("云端 token 长度不足")
        if not CLIENT_ID.fullmatch(client_id):
            raise ValueError("client_id 只能包含字母、数字、下划线或短横线")
        return cls(base_url, token, client_id)


class CloudAPI:
    def __init__(self, config: CloudConfig, *, transport=None) -> None:
        self.config = config
        self.client = httpx.Client(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=30,
            transport=transport,
        )

    def _json(self, method: str, path: str, **kwargs) -> dict:
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("云端响应必须是 JSON object")
        return payload

    def close(self) -> None:
        self.client.close()

    def status(self) -> dict:
        return self._json("GET", "/v1/status/today")

    def changes(self, after: int) -> dict:
        return self._json("GET", "/v1/changes", params={"after": after})

    def ack(self, cursor: int) -> int:
        result = self._json(
            "POST",
            f"/v1/clients/{self.config.client_id}/ack",
            json={"cursor": cursor},
        )
        return int(result["acknowledged_cursor"])


class _CacheAdapter:
    empty_is_authoritative = True
    complete_snapshot_is_authoritative = True

    def __init__(self, conn, source_key: str) -> None:
        spec = SOURCE_BY_KEY[source_key]
        self.conn = conn
        self.source_key = source_key
        self.company = str(spec["company"])
        self.system = str(spec["system"])
        self.entry_url = str(spec["entry_url"])
        self.tenant = spec.get("tenant")

    def fetch(self) -> list[RawJob]:
        rows = self.conn.execute(
            """SELECT external_id, payload_json, is_open FROM cloud_job_cache
               WHERE source_key=? ORDER BY external_id""",
            (self.source_key,),
        ).fetchall()
        known_ids = {str(row["external_id"]) for row in rows}
        jobs = []
        for row in rows:
            if not row["is_open"]:
                continue
            payload = json.loads(row["payload_json"])
            jobs.append(
                RawJob(
                    external_id=str(payload["external_id"]),
                    title=str(payload["title"]),
                    raw_json=payload,
                    job_family=payload.get("job_family"),
                    raw_category=payload.get("raw_category"),
                    cities=list(payload.get("cities") or []),
                    raw_location=payload.get("raw_location"),
                    country=payload.get("country"),
                    department=payload.get("department"),
                    recruit_type=payload.get("recruit_type"),
                    grad_year=payload.get("grad_year"),
                    apply_url=payload.get("apply_url"),
                    apply_system=payload.get("apply_system"),
                    description=payload.get("description"),
                )
            )
        # 云端从部署时建立新基线，历史本机岗位可能早于这条基线。没有收到
        # explicit closed 事件前，不得仅凭“云端 cache 里没见过”就把它关闭。
        for row in self.conn.execute(
            """SELECT * FROM jobs
               WHERE source_key=? AND closed_at IS NULL ORDER BY external_id""",
            (self.source_key,),
        ).fetchall():
            if str(row["external_id"]) in known_ids:
                continue
            jobs.append(
                RawJob(
                    external_id=str(row["external_id"]),
                    title=str(row["title"]),
                    raw_json={},
                    job_family=row["job_family"],
                    raw_category=row["raw_category"],
                    cities=json.loads(row["cities"] or "[]"),
                    raw_location=row["raw_location"],
                    country=row["country"],
                    department=row["department"],
                    recruit_type=row["recruit_type"],
                    grad_year=row["grad_year"],
                    apply_url=row["apply_url"],
                    apply_system=row["apply_system"],
                    description=row["description"],
                )
            )
        return jobs


class CloudSync:
    def __init__(self, conn, api, *, client_id: str) -> None:
        if not CLIENT_ID.fullmatch(client_id):
            raise ValueError("invalid cloud client_id")
        self.conn = conn
        self.api = api
        self.client_id = client_id

    def _cursor(self) -> int:
        row = self.conn.execute(
            "SELECT cursor FROM cloud_sync_state WHERE client_id=?",
            (self.client_id,),
        ).fetchone()
        return int(row["cursor"]) if row else 0

    def _apply_changes(self, changes: list[dict]) -> set[str]:
        affected: set[str] = set()
        for change in changes:
            source_key = str(change.get("source_key", ""))
            external_id = str(change.get("external_id", ""))
            kind = change.get("kind")
            payload = change.get("job")
            if source_key not in SOURCE_BY_KEY or kind not in {"opened", "updated", "closed"}:
                raise ValueError("云端变化包含未批准的来源或类型")
            if not isinstance(payload, dict) or (
                payload.get("source_key") != source_key
                or str(payload.get("external_id", "")) != external_id
            ):
                raise ValueError("云端变化身份与岗位载荷不一致")
            payload_json = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            self.conn.execute(
                """INSERT INTO cloud_job_cache(
                       source_key, external_id, payload_json, is_open, updated_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(source_key, external_id) DO UPDATE SET
                     payload_json=excluded.payload_json,
                     is_open=excluded.is_open,
                     updated_at=excluded.updated_at""",
                (
                    source_key,
                    external_id,
                    payload_json,
                    int(kind != "closed"),
                    str(change["occurred_at"]),
                ),
            )
            affected.add(source_key)
        self.conn.commit()
        return affected

    def sync(self) -> dict:
        cursor = self._cursor()
        # 只有成功提交了正游标，才算云端基线建立完成。首轮分页中断时 cache
        # 可能已有部分行，但 cursor 仍为 0；重试仍必须静默重建完整基线。
        initial_sync = cursor == 0
        total = 0
        affected_total: set[str] = set()
        while True:
            page = self.api.changes(cursor)
            changes = page.get("changes")
            if not isinstance(changes, list):
                raise ValueError("云端 changes 必须是 list")
            page_cursors = [int(change["cursor"]) for change in changes]
            if page_cursors != sorted(page_cursors) or any(value <= cursor for value in page_cursors):
                raise ValueError("云端变化游标没有严格递增")
            next_cursor = int(page.get("next_cursor", cursor))
            expected_next = page_cursors[-1] if page_cursors else cursor
            if next_cursor != expected_next:
                raise ValueError("云端 next_cursor 与本页末项不一致")
            if bool(page.get("has_more")) and next_cursor == cursor:
                raise ValueError("云端分页声明未结束但游标没有推进")

            affected = self._apply_changes(changes)
            cursor = next_cursor
            total += len(changes)
            affected_total.update(affected)
            if not bool(page.get("has_more")):
                break

        # 一页只是变化传输分片，不是某个来源的完整岗位快照。必须等全部页落入
        # cache 后再让既有 ingest 看一次完整来源，否则首轮同步会误关掉后续页岗位。
        notifiable_changes = 0
        for source_key in SOURCE_BY_KEY:
            if source_key in affected_total:
                stats = ingest.sync(self.conn, _CacheAdapter(self.conn, source_key))
                if not initial_sync and not stats["bootstrap"]:
                    notifiable_changes += sum(
                        int(stats[key]) for key in ("opened", "updated", "closed")
                    )
        self.conn.execute(
            """INSERT INTO cloud_sync_state(client_id, cursor, last_synced_at)
               VALUES(?,?,?)
               ON CONFLICT(client_id) DO UPDATE SET
                 cursor=excluded.cursor, last_synced_at=excluded.last_synced_at""",
            (self.client_id, cursor, db.now()),
        )
        if notifiable_changes:
            self.conn.execute(
                """INSERT OR IGNORE INTO cloud_notifications(
                       cursor, change_count, status, created_at
                   ) VALUES(?,?,'pending',?)""",
                (cursor, notifiable_changes, db.now()),
            )
        self.conn.commit()
        acknowledged = self.api.ack(cursor)
        if acknowledged < cursor:
            raise RuntimeError("云端确认游标落后于本机")
        return {
            "applied": total,
            "cursor": cursor,
            "affected_sources": len(affected_total),
            "notifiable_changes": notifiable_changes,
            "bootstrap": initial_sync and bool(total),
        }

    def check(self) -> dict:
        status = self.api.status()
        synced = self.sync()
        return {
            "status": status,
            # 招聘门户拒绝 Cloudflare 机房请求。补采由 GitHub 远端主任务和
            # 本机独立观察承担；cloud-check 只读云端事实，绝不重启旧 405 路径。
            "catch_up_requested": False,
            "catch_up_result": None,
            "sync": synced,
        }
