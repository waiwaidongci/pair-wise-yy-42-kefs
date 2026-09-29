from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, normalize_triage, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    VIEW_ROLES, capacity_gap, completion_blockers,
                    escalation_required, order_for_dispatch, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_batch_transition, validate_transition)

# 后送业务角色：现场医护登记分诊、后勤组车并确认发车、指挥角色可查
TRIAGE_ROLES=set(['field_commander', 'logistics'])
DISPATCH_ROLES=set(['logistics'])
RECEIVE_ROLES=set(['logistics', 'field_commander'])


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ------------------------------------------------------------------
    # 伤员后送排队
    # ------------------------------------------------------------------
    def register_casualty(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        # 伤员按现场编号登记；重复上报沿用首次分诊（返回既有记录，409）
        ensure_role(role, TRIAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        site_ref = require_text(payload.get("site_ref"), "site_ref", 100)
        name = require_text(payload.get("name", ""), "name", 200) if str(payload.get("name", "")).strip() else ""
        triage = normalize_triage(payload.get("triage"))
        existing = self.repository.get_casualty_by_ref(site_ref)
        if existing is not None:
            from .domain import ConflictError
            raise ConflictError("现场编号已登记，重复上报沿用首次分诊",
                                details={"casualty": self._casualty_view(existing)})
        casualty = self.repository.register_casualty(site_ref, name, triage, actor)
        self.repository.append_audit("register", "后送伤员", casualty["id"], actor, {
            "site_ref": site_ref, "triage": triage, "registered_seq": casualty["registered_seq"]})
        return self._casualty_view(casualty)

    def list_casualties(self, role: str) -> list:
        self._view(role)
        return [self._casualty_view(c) for c in self.repository.list_casualties()]

    def waiting_queue(self, role: str) -> list:
        self._view(role)
        queue = self.repository.waiting_casualties()
        return [self._casualty_view(c) for c in order_for_dispatch(queue)]

    def recheck_casualty(self, casualty_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        # 复查调高等级；未发车批次重排，已发车记录不动
        ensure_role(role, TRIAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        triage = normalize_triage(payload.get("triage"))
        casualty = self.repository.recheck_casualty(casualty_id, triage, actor)
        return self._casualty_view(casualty)

    def create_vehicle(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        require_text(actor, "actor", 100)
        plate = require_text(payload.get("plate"), "plate", 50)
        seats = int(require_number(payload.get("seats"), "seats", 1))
        vehicle = self.repository.create_vehicle(plate, seats)
        self.repository.append_audit("create_vehicle", "后送车辆", vehicle["id"], actor, {
            "plate": plate, "seats": seats})
        return vehicle

    def list_vehicles(self, role: str) -> list:
        self._view(role)
        return self.repository.list_vehicles()

    def create_receiver(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 50)
        name = require_text(payload.get("name"), "name", 200)
        beds = int(require_number(payload.get("beds"), "beds", 0))
        receiver = self.repository.create_receiver(code, name, beds)
        self.repository.append_audit("create_receiver", "接收点", receiver["id"], actor, {
            "code": code, "beds": beds})
        return receiver

    def list_receivers(self, role: str) -> list:
        self._view(role)
        return self.repository.list_receivers()

    def plan_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        # 组批：从待后送队列按危重、重伤、轻伤取 min(座位,空床) 人；运力不足时说明缺口并保留原队列
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        vehicle_id = int(require_number(payload.get("vehicle_id"), "vehicle_id", 1))
        receiver_id = int(require_number(payload.get("receiver_id"), "receiver_id", 1))
        vehicle = self.repository.get_vehicle(vehicle_id)
        receiver = self.repository.get_receiver(receiver_id)
        if vehicle["batch_id"] is not None:
            from .domain import ConflictError
            raise ConflictError("该车已绑定批次，不能重复组批")
        queue = order_for_dispatch(self.repository.waiting_casualties())
        beds_free = int(receiver["beds"]) - self.repository.beds_used(receiver_id)
        capacity = min(int(vehicle["seats"]), beds_free)
        if capacity <= 0:
            from .domain import ConflictError
            raise ConflictError("车辆座位或接收点床位不足，原队列保留", details={
                "gap": capacity_gap(queue[:1], int(vehicle["seats"]), beds_free),
                "waiting": len(queue)})
        chosen = queue[:capacity]
        leftover = queue[capacity:]
        batch = self.repository.create_batch_plan(vehicle_id, receiver_id, chosen, actor)
        result = self._batch_view(batch)
        result["queued_remaining"] = len(leftover)
        result["gap"] = capacity_gap(leftover, int(vehicle["seats"]), beds_free)
        result["gap"]["note"] = "本车或空床装下首批后剩余的待后送人数"
        return result

    def confirm_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        # 确认前复核运力：缺口存在则拒绝，原队列/批次保留；确认后批次不能打散
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        validate_batch_transition(batch["status"], 'confirmed')
        members = self.repository.batch_casualties(batch_id)
        vehicle = self.repository.get_vehicle(batch["vehicle_id"])
        receiver = self.repository.get_receiver(batch["receiver_id"])
        beds_free = int(receiver["beds"]) - self.repository.beds_used(
            receiver["id"], exclude_batch_id=batch_id)
        gap = capacity_gap(members, int(vehicle["seats"]), beds_free)
        if gap["seat_shortfall"] or gap["bed_shortfall"]:
            from .domain import ConflictError
            raise ConflictError("车辆座位或接收点床位不足，已确认操作取消、批次保留",
                                details={"gap": gap})
        updated = self.repository.confirm_batch(batch_id, actor)
        return self._batch_view(updated)

    def depart_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        validate_batch_transition(batch["status"], 'departed')
        updated = self.repository.depart_batch(batch_id, actor)
        return self._batch_view(updated)

    def list_batches(self, role: str) -> list:
        self._view(role)
        return [self._batch_view(b) for b in self.repository.list_batches()]

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._batch_view(self.repository.get_batch(batch_id))

    def admit_casualty(self, casualty_id: int, actor: str, role: str) -> Dict[str, Any]:
        # 同一伤员只能被一个接收点收一次（收治唯一约束在台账层兜底）
        ensure_role(role, RECEIVE_ROLES)
        actor = require_text(actor, "actor", 100)
        admission = self.repository.admit_casualty(casualty_id, actor)
        return admission

    def list_admissions(self, role: str) -> list:
        self._view(role)
        return self.repository.list_admissions()

    def _casualty_view(self, casualty: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(casualty)
        if casualty.get("batch_id") is not None:
            batch = self.repository.get_batch(casualty["batch_id"])
            result["batch_status"] = batch["status"]
        else:
            result["batch_status"] = None
        return result

    def _batch_view(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        members = self.repository.batch_casualties(batch["id"])
        vehicle = self.repository.get_vehicle(batch["vehicle_id"])
        receiver = self.repository.get_receiver(batch["receiver_id"])
        result["casualties"] = [self._casualty_view(c) for c in members]
        result["seats"] = vehicle["seats"]
        result["beds_total"] = receiver["beds"]
        result["beds_free"] = int(receiver["beds"]) - self.repository.beds_used(
            receiver["id"], exclude_batch_id=batch["id"])
        result["locked"] = batch["status"] in ('confirmed', 'departed')
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
