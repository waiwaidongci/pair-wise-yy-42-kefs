from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (EVACUATION_ENTITY as EVAC_ENTITY, ID_PREFIX, STATES,
                    order_for_dispatch)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS casualties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_ref TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    triage TEXT NOT NULL CHECK(triage IN ('critical','serious','minor')),
                    registered_seq INTEGER NOT NULL,
                    batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL,
                    position INTEGER,
                    status TEXT NOT NULL DEFAULT 'waiting'
                        CHECK(status IN ('waiting','dispatched','admitted')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, position)
                );
                CREATE TABLE IF NOT EXISTS vehicles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plate TEXT NOT NULL UNIQUE,
                    seats INTEGER NOT NULL CHECK(seats > 0),
                    batch_id INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receivers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    beds INTEGER NOT NULL CHECK(beds >= 0),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vehicle_id INTEGER REFERENCES vehicles(id),
                    receiver_id INTEGER REFERENCES receivers(id),
                    status TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','confirmed','departed')),
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    departed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS admissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    casualty_id INTEGER NOT NULL UNIQUE REFERENCES casualties(id),
                    receiver_id INTEGER NOT NULL REFERENCES receivers(id),
                    batch_id INTEGER NOT NULL REFERENCES batches(id),
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ------------------------------------------------------------------
    # 伤员后送台账
    # ------------------------------------------------------------------
    def register_casualty(self, site_ref: str, name: str, triage: str,
                          actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            seq_row = self.conn.execute(
                "SELECT COALESCE(MAX(registered_seq),0)+1 AS next_seq FROM casualties"
            ).fetchone()
            try:
                cur = self.conn.execute(
                    """INSERT INTO casualties(site_ref, name, triage, registered_seq,
                       status, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,'waiting',?,?,?)""",
                    (site_ref, name, triage, int(seq_row["next_seq"]), actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("现场编号已登记，重复上报沿用首次分诊") from exc
        return self.get_casualty(int(cur.lastrowid))

    def get_casualty(self, casualty_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM casualties WHERE id=?", (casualty_id,)).fetchone()
        if row is None:
            raise NotFoundError("伤员不存在")
        return dict(row)

    def get_casualty_by_ref(self, site_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM casualties WHERE site_ref=?", (site_ref,)).fetchone()
        return dict(row) if row else None

    def waiting_casualties(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM casualties WHERE batch_id IS NULL ORDER BY registered_seq"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_casualties(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM casualties ORDER BY registered_seq").fetchall()
        return [dict(row) for row in rows]

    def create_vehicle(self, plate: str, seats: int) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO vehicles(plate, seats, active, created_at) VALUES(?,?,1,?)",
                    (plate, seats, now))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("车牌已登记") from exc
        return self.get_vehicle(int(cur.lastrowid))

    def get_vehicle(self, vehicle_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise NotFoundError("车辆不存在")
        return dict(row)

    def list_vehicles(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def create_receiver(self, code: str, name: str, beds: int) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO receivers(code, name, beds, created_at) VALUES(?,?,?,?)",
                    (code, name, beds, now))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("接收点编号已存在") from exc
        return self.get_receiver(int(cur.lastrowid))

    def get_receiver(self, receiver_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM receivers WHERE id=?", (receiver_id,)).fetchone()
        if row is None:
            raise NotFoundError("接收点不存在")
        return dict(row)

    def list_receivers(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM receivers ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def beds_used(self, receiver_id: int, exclude_batch_id: Optional[int] = None) -> int:
        # 床位占用：已确认/已发车批次中的伤员（含已收治）；统计当前批次运力时排除自身
        sql = """SELECT COUNT(*) AS n FROM casualties c
                   JOIN batches b ON c.batch_id=b.id
                   WHERE b.receiver_id=? AND b.status IN ('confirmed','departed')"""
        params: tuple = (receiver_id,)
        if exclude_batch_id is not None:
            sql += " AND b.id<>?"
            params = (receiver_id, exclude_batch_id)
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return int(row["n"])

    def create_batch_plan(self, vehicle_id: int, receiver_id: int,
                          casualties: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """组批：按发车顺序写入。已在批次中的伤员不会被再次写入（同一伤员不进两个批次）。"""
        now = utc_now()
        with self._lock, self.conn:
            vehicle = self.conn.execute(
                "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if vehicle is None:
                raise NotFoundError("车辆不存在")
            if vehicle["batch_id"] is not None:
                raise ConflictError("该车已绑定批次，不能重复组批")
            receiver = self.conn.execute(
                "SELECT * FROM receivers WHERE id=?", (receiver_id,)).fetchone()
            if receiver is None:
                raise NotFoundError("接收点不存在")
            cur = self.conn.execute(
                """INSERT INTO batches(vehicle_id, receiver_id, status, created_at)
                   VALUES(?,?, 'planned', ?)""", (vehicle_id, receiver_id, now))
            batch_id = int(cur.lastrowid)
            for position, casualty in enumerate(casualties):
                upd = self.conn.execute(
                    """UPDATE casualties SET batch_id=?, position=?, updated_at=?
                       WHERE id=? AND batch_id IS NULL AND status='waiting'""",
                    (batch_id, position, now, casualty["id"]))
                if upd.rowcount == 0:
                    raise ConflictError("伤员已在其他批次或已后送，不能重复接收")
            self.conn.execute(
                "UPDATE vehicles SET batch_id=? WHERE id=?", (batch_id, vehicle_id))
            self.append_audit("plan_batch", "后送批次", batch_id, actor, {
                "vehicle_id": vehicle_id, "receiver_id": receiver_id,
                "casualty_ids": [c["id"] for c in casualties]})
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def batch_casualties(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM casualties WHERE batch_id=? ORDER BY position",
                (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def confirm_batch(self, batch_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFoundError("批次不存在")
            if row["status"] != 'planned':
                raise ConflictError("只有未确认批次可以确认，已确认批次不能打散")
            cur = self.conn.execute(
                "UPDATE batches SET status='confirmed', confirmed_at=? WHERE id=? AND status='planned'",
                (now, batch_id))
            if cur.rowcount == 0:
                raise ConflictError("批次状态已变化，请刷新后重试")
            self.append_audit("confirm_batch", "后送批次", batch_id, actor, {})
        return self.get_batch(batch_id)

    def reorder_planned_batch(self, batch_id: int) -> List[Dict[str, Any]]:
        # 未发车批次重排：仅改位置，成员不打散；已确认批次同样允许复查后顺位调整
        with self._lock, self.conn:
            members = self.batch_casualties(batch_id)
            ordered = order_for_dispatch(members)
            self._write_positions(batch_id, ordered)
        return self.batch_casualties(batch_id)

    def _write_positions(self, batch_id: int, ordered: List[Dict[str, Any]]) -> None:
        # UNIQUE(batch_id,position) 逐条改位会临时撞约束：先全部挪到负偏移再落位
        now = utc_now()
        for temp, casualty in enumerate(ordered):
            self.conn.execute(
                "UPDATE casualties SET position=? WHERE id=?",
                (-temp - 1, casualty["id"]))
        for position, casualty in enumerate(ordered):
            self.conn.execute(
                "UPDATE casualties SET position=?, updated_at=? WHERE id=?",
                (position, now, casualty["id"]))

    def depart_batch(self, batch_id: int, actor: str) -> Dict[str, Any]:
        """发车：原子校验车辆座位与接收点床位；不足则整批不动并返回缺口。"""
        now = utc_now()
        with self._lock, self.conn:
            batch = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFoundError("批次不存在")
            if batch["status"] != 'confirmed':
                raise ConflictError("只有已确认批次可以发车，已确认批次不能打散")
            members = self.batch_casualties(batch_id)
            vehicle = self.conn.execute(
                "SELECT * FROM vehicles WHERE id=?", (batch["vehicle_id"],)).fetchone()
            receiver = self.conn.execute(
                "SELECT * FROM receivers WHERE id=?", (batch["receiver_id"],)).fetchone()
            seats = int(vehicle["seats"])
            beds_free = int(receiver["beds"]) - self.beds_used(
                receiver_id=batch["receiver_id"], exclude_batch_id=batch_id)
            gap = {"needed": len(members), "seats": seats, "beds_free": beds_free,
                   "seat_shortfall": max(0, len(members) - seats),
                   "bed_shortfall": max(0, len(members) - beds_free)}
            if gap["seat_shortfall"] or gap["bed_shortfall"]:
                raise ConflictError("车辆座位或接收点床位不足，批次保留", details={"gap": gap})
            self.conn.execute(
                "UPDATE batches SET status='departed', departed_at=? WHERE id=? AND status='confirmed'",
                (now, batch_id))
            self.conn.execute(
                "UPDATE casualties SET status='dispatched', updated_at=? WHERE batch_id=?",
                (now, batch_id))
            self.append_audit("depart_batch", "后送批次", batch_id, actor, {
                "count": len(members), "vehicle_id": batch["vehicle_id"],
                "receiver_id": batch["receiver_id"]})
        return self.get_batch(batch_id)

    def admit_casualty(self, casualty_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            casualty = self.conn.execute(
                "SELECT * FROM casualties WHERE id=?", (casualty_id,)).fetchone()
            if casualty is None:
                raise NotFoundError("伤员不存在")
            if casualty["batch_id"] is None:
                raise ConflictError("伤员尚未随批次后送")
            batch = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (casualty["batch_id"],)).fetchone()
            if batch["status"] != 'departed':
                raise ConflictError("批次尚未发车，不能收治")
            try:
                cur = self.conn.execute(
                    """INSERT INTO admissions(casualty_id, receiver_id, batch_id,
                       actor, created_at) VALUES(?,?,?,?,?)""",
                    (casualty_id, batch["receiver_id"], batch["id"], actor, now))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该伤员已被接收点收下，不能重复收治") from exc
            self.conn.execute(
                "UPDATE casualties SET status='admitted', updated_at=? WHERE id=?",
                (now, casualty_id))
            admission_id = int(cur.lastrowid)
            self.append_audit("admit", EVAC_ENTITY, casualty_id, actor, {
                "receiver_id": batch["receiver_id"], "batch_id": batch["id"]})
            row = self.conn.execute(
                "SELECT * FROM admissions WHERE id=?", (admission_id,)).fetchone()
        return dict(row)

    def list_admissions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM admissions ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def recheck_casualty(self, casualty_id: int, new_triage: str,
                         actor: str) -> Dict[str, Any]:
        # 调高分诊等级；未发车批次顺位重排，已发车记录不动
        now = utc_now()
        with self._lock, self.conn:
            casualty = self.conn.execute(
                "SELECT * FROM casualties WHERE id=?", (casualty_id,)).fetchone()
            if casualty is None:
                raise NotFoundError("伤员不存在")
            batch_state = None
            if casualty["batch_id"] is not None:
                batch = self.conn.execute(
                    "SELECT * FROM batches WHERE id=?", (casualty["batch_id"],)).fetchone()
                batch_state = batch["status"]
            from .rules import validate_recheck
            validate_recheck(casualty["triage"], new_triage, batch_state)
            self.conn.execute(
                "UPDATE casualties SET triage=?, updated_at=? WHERE id=?",
                (new_triage, now, casualty_id))
            self.append_audit("recheck", EVAC_ENTITY, casualty_id, actor, {
                "from": casualty["triage"], "to": new_triage,
                "batch_id": casualty["batch_id"], "batch_state": batch_state})
            if casualty["batch_id"] is not None and batch_state in ('planned', 'confirmed'):
                members = [dict(r) for r in self.conn.execute(
                    "SELECT * FROM casualties WHERE batch_id=? ORDER BY position",
                    (casualty["batch_id"],)).fetchall()]
                self._write_positions(casualty["batch_id"], order_for_dispatch(members))
        return self.get_casualty(casualty_id)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
