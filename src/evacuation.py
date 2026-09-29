from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .triage import (RANK, TRIAGE_LEVELS, batch_urgency_rank, bed_gap,
                     describe_gap, is_upgrade, normalize_triage, order_queue)

ENTITY = '后送批次'
CASUALTY = '后送伤员'


class EvacuationLedger:
    """后送台账：登记、批次冻结与发车记账。批次一旦确认成员不可打散，
    已发车记录只读；容量不足时抛出缺口，调用方保留原队列。"""

    def __init__(self, conn: sqlite3.Connection, lock):
        self.conn = conn
        self._lock = lock

    # ---------- 基础资源：车辆与接收点 ----------

    def add_vehicle(self, plate: str, seats: int) -> Dict[str, Any]:
        if not isinstance(seats, int) or isinstance(seats, bool) or seats < 0:
            raise ValidationError("车辆座位必须是非负整数")
        if not isinstance(plate, str) or not plate.strip():
            raise ValidationError("车牌不能为空")
        plate = plate.strip()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO med_vehicles(plate, seats) VALUES(?,?)",
                    (plate, seats))
                vehicle_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("车辆已存在") from exc
        return self.get_vehicle(vehicle_id)

    def get_vehicle(self, vehicle_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM med_vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise NotFoundError("车辆不存在")
        return dict(row)

    def _vehicle_free_for_batch(self, vehicle_id: int, conn) -> bool:
        # 同一车辆只要挂在未发车批次上就算占用，避免一车两批
        row = conn.execute(
            """SELECT 1 FROM med_batches WHERE vehicle_id=? AND status='confirmed'
               LIMIT 1""", (vehicle_id,)).fetchone()
        return row is None

    def list_vehicles(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM med_vehicles ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def add_receiver(self, name: str, total_beds: int) -> Dict[str, Any]:
        if not isinstance(total_beds, int) or isinstance(total_beds, bool) or total_beds < 0:
            raise ValidationError("床位数必须是非负整数")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("接收点名称不能为空")
        name = name.strip()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO med_receivers(name, total_beds, used_beds) VALUES(?,?,0)",
                    (name, total_beds))
                receiver_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("接收点已存在") from exc
        return self.get_receiver(receiver_id)

    def get_receiver(self, receiver_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM med_receivers WHERE id=?", (receiver_id,)).fetchone()
        if row is None:
            raise NotFoundError("接收点不存在")
        result = dict(row)
        result["free_beds"] = result["total_beds"] - result["used_beds"]
        return result

    def list_receivers(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM med_receivers ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["free_beds"] = item["total_beds"] - item["used_beds"]
            result.append(item)
        return result

    # ---------- 伤员登记与复查 ----------

    def register(self, case_ref: str, triage: str, actor: str,
                 name: Optional[str] = None, source: str = "field") -> Dict[str, Any]:
        """按现场编号登记；编号已存在即为重复上报，沿用首次分诊。"""
        now = utc_now()
        triage = normalize_triage(triage)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM med_casualties WHERE case_ref=?", (case_ref,)).fetchone()
            if row is not None:
                # 重复上报：登记一条被驳回的上报记录，分诊不变
                self.conn.execute(
                    """INSERT INTO med_reports(casualty_id, triage, source, accepted,
                       created_by, created_at) VALUES(?,?,?,0,?,?)""",
                    (row["id"], triage, source, actor, now))
                casualty = dict(row)
                casualty["duplicate"] = True
                casualty["applied_triage"] = row["first_triage"]
                return casualty
            cur = self.conn.execute(
                """INSERT INTO med_casualties(case_ref, first_triage, current_triage,
                   name, registered_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (case_ref, triage, triage, name, actor, now, now))
            casualty_id = int(cur.lastrowid)
            self.conn.execute(
                """INSERT INTO med_reports(casualty_id, triage, source, accepted,
                   created_by, created_at) VALUES(?,?,?,1,?,?)""",
                (casualty_id, triage, source, actor, now))
        result = self.get_casualty(casualty_id)
        result["duplicate"] = False
        result["applied_triage"] = triage
        return result

    def get_casualty(self, casualty_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM med_casualties WHERE id=?", (casualty_id,)).fetchone()
        if row is None:
            raise NotFoundError("伤员不存在")
        return dict(row)

    def _waiting_ids(self, conn) -> List[int]:
        rows = conn.execute(
            """SELECT c.id, c.current_triage FROM med_casualties c
               WHERE NOT EXISTS (
                   SELECT 1 FROM med_batch_members m WHERE m.casualty_id=c.id)
               ORDER BY c.id""").fetchall()
        ordered = order_queue([dict(r) for r in rows])
        return [c["id"] for c in ordered]

    def waiting_queue(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT c.* FROM med_casualties c
                   WHERE NOT EXISTS (
                       SELECT 1 FROM med_batch_members m WHERE m.casualty_id=c.id)
                   ORDER BY c.id""").fetchall()
        return order_queue([dict(r) for r in rows])

    def recheck(self, casualty_id: int, new_triage: str,
                actor: str) -> Dict[str, Any]:
        """复查调高等级：未发车批次按紧急度重排（成员不动），已发车记录不动。"""
        now = utc_now()
        new_triage = normalize_triage(new_triage)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM med_casualties WHERE id=?", (casualty_id,)).fetchone()
            if row is None:
                raise NotFoundError("伤员不存在")
            old = row["current_triage"]
            batch_row = self.conn.execute(
                """SELECT b.id, b.status FROM med_batch_members m
                   JOIN med_batches b ON b.id=m.batch_id
                   WHERE m.casualty_id=?""", (casualty_id,)).fetchone()
            if batch_row is not None and batch_row["status"] == "dispatched":
                # 已发车记录不动（最强约束）：先留复查轨迹再拒绝任何改写
                self.conn.execute(
                    """INSERT INTO med_reports(casualty_id, triage, source, accepted,
                       created_by, created_at) VALUES(?,?,?,0,?,?)""",
                    (casualty_id, new_triage, "recheck", actor, now))
                raise ConflictError("伤员已发车，后送记录不能改动")
            if not is_upgrade(old, new_triage):
                from .triage import validate_upgrade
                validate_upgrade(old, new_triage)
            self.conn.execute(
                """UPDATE med_casualties SET current_triage=?, updated_at=? WHERE id=?""",
                (new_triage, now, casualty_id))
            self.conn.execute(
                """INSERT INTO med_reports(casualty_id, triage, source, accepted,
                   created_by, created_at) VALUES(?,?,?,1,?,?)""",
                (casualty_id, new_triage, "recheck", actor, now))
            self._rerank_confirmed(self.conn)
        return self.get_casualty(casualty_id)

    def _rerank_confirmed(self, conn) -> None:
        # 已确认未发车批次不能打散，只按批次紧急度（含同等级先确认优先）重排发车轮次
        rows = conn.execute(
            """SELECT b.id FROM med_batches b WHERE b.status='confirmed'
               ORDER BY b.dispatch_seq IS NULL, b.dispatch_seq, b.id""").fetchall()
        ranked = []
        for r in rows:
            triages = [row["current_triage"] for row in conn.execute(
                """SELECT c.current_triage FROM med_batch_members m
                   JOIN med_casualties c ON c.id=m.casualty_id
                   WHERE m.batch_id=?""", (r["id"],)).fetchall()]
            ranked.append((batch_urgency_rank(triages), r["id"]))
        ranked.sort(key=lambda item: (item[0], item[1]))
        for seq, (_, batch_id) in enumerate(ranked, start=1):
            conn.execute(
                "UPDATE med_batches SET dispatch_seq=? WHERE id=?", (seq, batch_id))

    # ---------- 批次：确认即冻结 ----------

    def confirm_batch(self, vehicle_id: Optional[int], seats: Optional[int],
                      actor: str) -> Dict[str, Any]:
        """从等待队列队首（危重优先）截一组发车前名单并冻结为批次。
        车辆座位不足时在返回中带缺口，多出人员保留原队列。"""
        del actor
        now = utc_now()
        with self._lock, self.conn:
            if vehicle_id is not None:
                vrow = self.conn.execute(
                    "SELECT * FROM med_vehicles WHERE id=?", (vehicle_id,)).fetchone()
                if vrow is None:
                    raise NotFoundError("车辆不存在")
                if not self._vehicle_free_for_batch(vehicle_id, self.conn):
                    raise ConflictError("车辆已挂在未发车批次上")
                capacity = int(vrow["seats"])
            else:
                if not isinstance(seats, int) or isinstance(seats, bool) or seats <= 0:
                    raise ValidationError("无车辆时必须提供正整数座位数")
                capacity = seats
            waiting = self._waiting_ids(self.conn)
            if not waiting:
                raise ConflictError("等待队列为空，没有可入批的伤员")
            members = waiting[:capacity]
            leftover = waiting[capacity:]
            cur = self.conn.execute(
                """INSERT INTO med_batches(vehicle_id, status, created_at)
                   VALUES(?,'confirmed',?)""", (vehicle_id, now))
            batch_id = int(cur.lastrowid)
            for position, casualty_id in enumerate(members, start=1):
                self.conn.execute(
                    """INSERT INTO med_batch_members(batch_id, casualty_id, position)
                       VALUES(?,?,?)""", (batch_id, casualty_id, position))
            self._rerank_confirmed(self.conn)
        result = self.get_batch(batch_id)
        result["seat_gap"] = len(leftover)
        result["seat_gap_message"] = (
            describe_gap("seat", len(waiting), capacity) if leftover else None)
        return result

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM med_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        result = dict(row)
        with self._lock:
            members = self.conn.execute(
                """SELECT c.* FROM med_batch_members m
                   JOIN med_casualties c ON c.id=m.casualty_id
                   WHERE m.batch_id=? ORDER BY m.position""",
                (batch_id,)).fetchall()
        result["members"] = [dict(r) for r in members]
        return result

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM med_batches
                   ORDER BY status='dispatched', dispatch_seq, id""").fetchall()
        return [self.get_batch(r["id"]) for r in rows]

    def dispatch_batch(self, batch_id: int, receiver_id: int,
                       actor: str) -> Dict[str, Any]:
        """发车送往接收点：床位不足以整体收下时报缺口，台账与队列原样保留。
        成功后批次不可再改，伤员被该接收点唯一收下。"""
        del actor
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM med_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFoundError("批次不存在")
            if row["status"] != "confirmed":
                raise ConflictError("批次已发车，已确认记录不能打散或修改")
            receiver = self.conn.execute(
                "SELECT * FROM med_receivers WHERE id=?", (receiver_id,)).fetchone()
            if receiver is None:
                raise NotFoundError("接收点不存在")
            members = self.conn.execute(
                """SELECT c.id FROM med_batch_members m
                   JOIN med_casualties c ON c.id=m.casualty_id
                   WHERE m.batch_id=? ORDER BY m.position""", (batch_id,)).fetchall()
            party_size = len(members)
            free_beds = int(receiver["total_beds"]) - int(receiver["used_beds"])
            gap = bed_gap(party_size, free_beds)
            if gap > 0:
                # 说明缺口并保留原队列：不动批次、不动床位
                raise ConflictError(describe_gap("bed", party_size, free_beds))
            self.conn.execute(
                """UPDATE med_batches SET status='dispatched', receiver_id=?,
                   dispatched_at=? WHERE id=?""",
                (receiver_id, now, batch_id))
            self.conn.execute(
                "UPDATE med_receivers SET used_beds=used_beds+? WHERE id=?",
                (party_size, receiver_id))
            self._rerank_confirmed(self.conn)
        return self.get_batch(batch_id)

    def reports(self, casualty_id: int) -> List[Dict[str, Any]]:
        self.get_casualty(casualty_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM med_reports WHERE casualty_id=? ORDER BY id",
                (casualty_id,)).fetchall()
        return [dict(r) for r in rows]
