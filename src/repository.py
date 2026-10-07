"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    owner_org TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delegations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    target_org TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    reason TEXT NOT NULL DEFAULT '',
                    granted_by TEXT NOT NULL,
                    granted_org TEXT NOT NULL DEFAULT '',
                    ended_by TEXT NOT NULL DEFAULT '',
                    ended_org TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_org TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_delegations_record ON delegations(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_delegations_status ON delegations(status, valid_until);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            if "owner_org" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN owner_org TEXT NOT NULL DEFAULT ''")
            audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)")}
            if "actor_org" not in audit_columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN actor_org TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _delegation_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @contextmanager
    def tx(self):
        """立即写锁事务：串行化所有写操作，保证同一时刻只有一个分局写入。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, owner_org: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,owner_org,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), owner_org, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,actor_org,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, "created", actor_id, owner_org, 1,
                     json.dumps({"state": state, "owner_org": owner_org}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def get_conn(self, connection: sqlite3.Connection, record_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    # ---- 委托 ----

    def expire_due(self, connection: Optional[sqlite3.Connection] = None, now_iso: str = None,
                   record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """到期委托立即失效：幂等翻转并写入交接审计。"""
        now_iso = now_iso or _now()
        own_connection = connection is None
        connection = connection or self._connect()
        try:
            sql = "SELECT * FROM delegations WHERE status='active' AND valid_until<=?"
            params: List[Any] = [now_iso]
            if record_id is not None:
                sql += " AND record_id=?"
                params.append(record_id)
            due = [self._delegation_row(row) for row in connection.execute(sql, params).fetchall()]
            expired: List[Dict[str, Any]] = []
            for delegation in due:
                cursor = connection.execute(
                    "UPDATE delegations SET status='expired',ended_by=?,ended_org=?,updated_at=? WHERE id=? AND status='active'",
                    ("system", "", now_iso, delegation["id"]),
                )
                if cursor.rowcount == 0:
                    continue
                version_row = connection.execute("SELECT version FROM records WHERE id=?", (delegation["record_id"],)).fetchone()
                version = int(version_row["version"]) if version_row else 0
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,actor_org,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (delegation["record_id"], "delegation_expired", "system", "", version,
                     json.dumps({"delegation_id": delegation["id"], "target_org": delegation["target_org"],
                                 "stage": delegation["stage"], "valid_until": delegation["valid_until"]},
                                ensure_ascii=False, sort_keys=True), now_iso),
                )
                delegation["status"] = "expired"
                expired.append(delegation)
            if own_connection:
                connection.commit()
        finally:
            if own_connection:
                connection.close()
        return expired

    def active_delegation_conn(self, connection: sqlite3.Connection, record_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM delegations WHERE record_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        return self._delegation_row(row) if row else None

    def active_delegation_map(self, record_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not record_ids:
            return {}
        result: Dict[int, Dict[str, Any]] = {}
        with self._connect() as connection:
            placeholders = ",".join("?" for _ in record_ids)
            rows = connection.execute(
                "SELECT * FROM delegations WHERE status='active' AND record_id IN (%s) ORDER BY id" % placeholders,
                record_ids,
            ).fetchall()
        for row in rows:
            result[int(row["record_id"])] = self._delegation_row(row)
        return result

    def list_delegations(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delegations WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [self._delegation_row(row) for row in rows]

    def delegation_conn(self, connection: sqlite3.Connection, delegation_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone()
        return self._delegation_row(row) if row else None

    def grant_delegation_conn(self, connection: sqlite3.Connection, record_id: int, fields: Dict[str, Any],
                              actor_id: str, actor_org: str, now_iso: str) -> Dict[str, Any]:
        cursor = connection.execute(
            """INSERT INTO delegations(record_id,target_org,stage,valid_from,valid_until,status,reason,
               granted_by,granted_org,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?,?,?,?)""",
            (record_id, fields["target_org"], fields["stage"], fields["valid_from"], fields["valid_until"],
             fields["reason"], actor_id, actor_org, now_iso, now_iso),
        )
        return self.delegation_conn(connection, int(cursor.lastrowid))

    def end_delegation_conn(self, connection: sqlite3.Connection, delegation_id: int, status: str,
                            actor_id: str, actor_org: str, now_iso: str, reason: str = "") -> Dict[str, Any]:
        cursor = connection.execute(
            "UPDATE delegations SET status=?,ended_by=?,ended_org=?,updated_at=? WHERE id=? AND status='active'",
            (status, actor_id, actor_org, now_iso, delegation_id),
        )
        if cursor.rowcount == 0:
            raise Conflict("委托已失效，无法重复操作")
        return self.delegation_conn(connection, delegation_id)

    # ---- 记录写入（事务内） ----

    def version_conn(self, connection: sqlite3.Connection, record_id: int) -> int:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return int(row["version"])

    def bump_version_conn(self, connection: sqlite3.Connection, record_id: int, expected_version: int,
                          updated_by: str, now_iso: str) -> int:
        cursor = connection.execute(
            "UPDATE records SET version=version+1, updated_by=?, updated_at=? WHERE id=? AND version=?",
            (updated_by, now_iso, record_id, expected_version),
        )
        if cursor.rowcount == 0:
            raise Conflict("版本冲突，请刷新后重试")
        return int(expected_version) + 1

    def update_state_conn(self, connection: sqlite3.Connection, record_id: int, expected_version: int,
                          state: str, payload: Dict[str, Any], actor_id: str, now_iso: str) -> int:
        cursor = connection.execute(
            "UPDATE records SET state=?,version=version+1,payload=?,updated_by=?,updated_at=? WHERE id=? AND version=?",
            (state, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now_iso,
             record_id, expected_version),
        )
        if cursor.rowcount == 0:
            raise Conflict("版本冲突，请刷新后重试")
        return int(expected_version) + 1

    def add_audit_conn(self, connection: sqlite3.Connection, record_id: int, actor_id: str, actor_org: str,
                       action: str, version: int, details: Dict[str, Any], now_iso: str = None) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,actor_org,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, action, actor_id, actor_org, version,
             json.dumps(details, ensure_ascii=False, sort_keys=True), now_iso or _now()),
        )

    def add_audit(self, record_id: int, actor_id: str, actor_org: str, action: str,
                  details: Dict[str, Any]) -> None:
        """写入失败后仍保留凭据：独立事务记录冲突动作。"""
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            self.add_audit_conn(connection, record_id, actor_id, actor_org, action,
                                int(row["version"]), details)

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
