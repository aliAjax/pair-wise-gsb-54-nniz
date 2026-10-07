"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


_SKIP_GUARD = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delegations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    from_org TEXT NOT NULL,
                    to_org TEXT NOT NULL,
                    phases TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_by TEXT NOT NULL DEFAULT '',
                    closed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_delegations_record ON delegations(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_delegations_active ON delegations(status, record_id);
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(records)")}
            if "jurisdiction_org" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN jurisdiction_org TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _delegation_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        item = dict(row)
        item["phases"] = json.loads(item["phases"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, jurisdiction_org: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,jurisdiction_org) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now, jurisdiction_org),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "jurisdiction_org": jurisdiction_org}, ensure_ascii=False, sort_keys=True), now),
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

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def list_visible_records(self, org: str, now: str, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = (
            "SELECT * FROM records WHERE (jurisdiction_org='' OR jurisdiction_org=? "
            "OR id IN (SELECT record_id FROM delegations WHERE status='active' AND to_org=? AND valid_from<=? AND valid_until>=?))"
        )
        params: List[Any] = [org, org, now, now]
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def active_delegation(self, record_id: int, now: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM delegations WHERE record_id=? AND status='active' AND valid_from<=? AND valid_until>=? ORDER BY id DESC LIMIT 1",
                (record_id, now, now),
            ).fetchone()
        return self._delegation_row(row)

    def active_delegations_map(self, now: str) -> Dict[int, Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delegations WHERE status='active' AND valid_from<=? AND valid_until>=? ORDER BY id",
                (now, now),
            ).fetchall()
        result: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            result[int(row["record_id"])] = self._delegation_row(row)
        return result

    def delegations_for(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM delegations WHERE record_id=? ORDER BY id DESC", (record_id,)).fetchall()
        return [self._delegation_row(row) for row in rows]

    def _sweep_expired_tx(self, connection: sqlite3.Connection, now: str, record_id: Optional[int] = None) -> int:
        sql = (
            "SELECT d.id AS delegation_id, d.record_id AS record_id, d.to_org AS to_org, d.valid_until AS valid_until, r.version AS version "
            "FROM delegations d JOIN records r ON r.id=d.record_id WHERE d.status='active' AND d.valid_until<?"
        )
        params: List[Any] = [now]
        if record_id is not None:
            sql += " AND d.record_id=?"
            params.append(record_id)
        rows = connection.execute(sql, params).fetchall()
        for row in rows:
            connection.execute("UPDATE delegations SET status='expired', closed_by='system', closed_at=? WHERE id=?", (now, row["delegation_id"]))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    row["record_id"],
                    "delegation_expired",
                    "system",
                    int(row["version"]),
                    json.dumps({"delegation_id": int(row["delegation_id"]), "to_org": row["to_org"], "valid_until": row["valid_until"]}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return len(rows)

    def sweep_expired(self, now: str) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = self._sweep_expired_tx(connection, now)
            connection.commit()
        return expired

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], expected_delegation_id: Any = _SKIP_GUARD) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if expected_delegation_id is not _SKIP_GUARD:
                current = connection.execute("SELECT id FROM delegations WHERE record_id=? AND status='active' ORDER BY id DESC LIMIT 1", (record_id,)).fetchone()
                current_id = int(current["id"]) if current is not None else None
                if current_id != expected_delegation_id:
                    connection.rollback()
                    raise Conflict("受托状态已变化，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def delegate(self, record_id: int, expected_version: int, from_org: str, spec: Dict[str, Any], actor_id: str, actor_org: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(row["version"])
            if version != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            self._sweep_expired_tx(connection, now, record_id=record_id)
            active = connection.execute("SELECT id FROM delegations WHERE record_id=? AND status='active'", (record_id,)).fetchone()
            if active is not None:
                connection.rollback()
                raise Conflict("已存在生效中的委托，请先退回或撤回")
            cursor = connection.execute(
                "INSERT INTO delegations(record_id,from_org,to_org,phases,valid_from,valid_until,status,reason,created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?,?)",
                (record_id, from_org, spec["to_org"], json.dumps(spec["phases"], ensure_ascii=False), spec["valid_from"], spec["valid_until"], spec["reason"], actor_id, now),
            )
            delegation_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    "delegate",
                    actor_id,
                    version,
                    json.dumps(
                        {"delegation_id": delegation_id, "from_org": from_org, "to_org": spec["to_org"], "phases": spec["phases"], "valid_from": spec["valid_from"], "valid_until": spec["valid_until"], "reason": spec["reason"], "org": actor_org},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone()
            connection.commit()
        return self._delegation_row(row)

    def close_delegation(self, record_id: int, expected_version: int, status: str, actor_id: str, actor_org: str, reason: str) -> Dict[str, Any]:
        now = _now()
        action = "return" if status == "returned" else "revoke"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(row["version"])
            if version != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            self._sweep_expired_tx(connection, now, record_id=record_id)
            active = connection.execute("SELECT * FROM delegations WHERE record_id=? AND status='active' ORDER BY id DESC LIMIT 1", (record_id,)).fetchone()
            if active is None:
                connection.rollback()
                raise Conflict("当前没有生效中的委托")
            connection.execute("UPDATE delegations SET status=?, closed_by=?, closed_at=?, reason=? WHERE id=?", (status, actor_id, now, reason, int(active["id"])))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    action,
                    actor_id,
                    version,
                    json.dumps(
                        {"delegation_id": int(active["id"]), "from_org": active["from_org"], "to_org": active["to_org"], "phases": json.loads(active["phases"]), "reason": reason, "org": actor_org},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM delegations WHERE id=?", (int(active["id"]),)).fetchone()
            connection.commit()
        return self._delegation_row(row)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

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
