from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)
from .triage import TRIAGE_LABELS, normalize_triage

# 后送业务角色：现场卫生员登记/复查，后勤点确认批次与发车，viewer只读
TRIAGE_ROLES = {"field_commander", "incident_commander", "logistics"}
DISPATCH_ROLES = {"logistics", "incident_commander"}
MED_VIEW_ROLES = VIEW_ROLES | TRIAGE_ROLES
MED_ENTITY = '后送伤员'
BATCH_ENTITY = '后送批次'


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

    # ================= 后送排队 =================

    @property
    def ledger(self):
        return self.repository.evacuation

    def _med_actor(self, actor: str) -> str:
        return require_text(actor, "actor", 100)

    def register_casualty(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        # 请求入口：分诊判定落在triage模块，台账落在evacuation模块
        ensure_role(role, TRIAGE_ROLES)
        actor = self._med_actor(actor)
        case_ref = require_text(payload.get("case_ref"), "case_ref", 100)
        triage = normalize_triage(payload.get("triage"))
        name = payload.get("name")
        if name is not None:
            name = require_text(name, "name", 100)
        source = require_text(payload.get("source", "field"), "source", 100)
        casualty = self.ledger.register(case_ref, triage, actor, name, source)
        self.repository.append_audit(
            "med_register_duplicate" if casualty.get("duplicate") else "med_register",
            MED_ENTITY, casualty["id"], actor, {
                "case_ref": case_ref,
                "reported_triage": triage,
                "applied_triage": casualty["applied_triage"],
            })
        return casualty

    def recheck_casualty(self, casualty_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRIAGE_ROLES)
        actor = self._med_actor(actor)
        new_triage = normalize_triage(payload.get("triage"))
        casualty = self.ledger.recheck(casualty_id, new_triage, actor)
        self.repository.append_audit("med_recheck", MED_ENTITY, casualty_id, actor, {
            "triage": new_triage,
            "triage_label": TRIAGE_LABELS[new_triage],
        })
        return casualty

    def waiting_queue(self, role: str) -> list:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.waiting_queue()

    def get_casualty(self, casualty_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.get_casualty(casualty_id)

    def casualty_reports(self, casualty_id: int, role: str) -> list:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.reports(casualty_id)

    def add_vehicle(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = self._med_actor(actor)
        plate = require_text(payload.get("plate"), "plate", 50)
        seats = payload.get("seats")
        if not isinstance(seats, int) or isinstance(seats, bool) or seats < 0:
            from .domain import ValidationError
            raise ValidationError("seats必须是非负整数")
        vehicle = self.ledger.add_vehicle(plate, seats)
        self.repository.append_audit("med_vehicle", BATCH_ENTITY, vehicle["id"], actor, {
            "plate": plate, "seats": seats,
        })
        return vehicle

    def list_vehicles(self, role: str) -> list:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.list_vehicles()

    def add_receiver(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = self._med_actor(actor)
        name = require_text(payload.get("name"), "name", 100)
        beds = payload.get("total_beds")
        if not isinstance(beds, int) or isinstance(beds, bool) or beds < 0:
            from .domain import ValidationError
            raise ValidationError("total_beds必须是非负整数")
        receiver = self.ledger.add_receiver(name, beds)
        self.repository.append_audit("med_receiver", BATCH_ENTITY, receiver["id"],
                                     actor, {"name": name, "total_beds": beds})
        return receiver

    def list_receivers(self, role: str) -> list:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.list_receivers()

    def confirm_batch(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = self._med_actor(actor)
        vehicle_id = payload.get("vehicle_id")
        seats = payload.get("seats")
        if vehicle_id is None and seats is None:
            from .domain import ValidationError
            raise ValidationError("必须提供vehicle_id或seats")
        batch = self.ledger.confirm_batch(vehicle_id, seats, actor)
        self.repository.append_audit("med_batch_confirm", BATCH_ENTITY, batch["id"],
                                     actor, {
                                         "member_ids": [m["id"] for m in batch["members"]],
                                         "seat_gap": batch["seat_gap"],
                                     })
        return batch

    def dispatch_batch(self, batch_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = self._med_actor(actor)
        receiver_id = payload.get("receiver_id")
        if not isinstance(receiver_id, int) or receiver_id < 1:
            from .domain import ValidationError
            raise ValidationError("receiver_id必须是正整数")
        batch = self.ledger.dispatch_batch(batch_id, receiver_id, actor)
        self.repository.append_audit("med_dispatch", BATCH_ENTITY, batch_id, actor, {
            "receiver_id": receiver_id,
            "member_ids": [m["id"] for m in batch["members"]],
        })
        return batch

    def list_batches(self, role: str) -> list:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.list_batches()

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, MED_VIEW_ROLES)
        return self.ledger.get_batch(batch_id)
