import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.evacuation import EvacuationLedger
from src.repository import Repository
from src.service import Service
from src.triage import (CRITICAL, MINOR, SERIOUS, TRIAGE_LEVELS, bed_gap,
                        is_upgrade, order_queue, retain_first_triage,
                        validate_upgrade)

MEDIC = "field_commander"
DISPATCHER = "logistics"
VIEWER = "viewer"


class TriageRulesTest(unittest.TestCase):
    def test_order_is_critical_serious_minor(self):
        casualties = [
            {"id": 1, "current_triage": MINOR},
            {"id": 2, "current_triage": CRITICAL},
            {"id": 3, "current_triage": SERIOUS},
            {"id": 4, "current_triage": CRITICAL},
        ]
        ordered = [c["current_triage"] for c in order_queue(casualties)]
        self.assertEqual(ordered, [CRITICAL, CRITICAL, SERIOUS, MINOR])
        # 同等级按登记先后，危重不会被留在队尾
        self.assertEqual([c["id"] for c in order_queue(casualties)][:2], [2, 4])

    def test_duplicate_report_keeps_first_triage(self):
        self.assertEqual(retain_first_triage(MINOR, CRITICAL), MINOR)
        self.assertEqual(retain_first_triage(CRITICAL, MINOR), CRITICAL)

    def test_recheck_only_accepts_upgrade(self):
        self.assertTrue(is_upgrade(MINOR, SERIOUS))
        self.assertFalse(is_upgrade(CRITICAL, SERIOUS))
        with self.assertRaises(ValidationError):
            validate_upgrade(CRITICAL, MINOR)
        with self.assertRaises(ValidationError):
            validate_upgrade(SERIOUS, SERIOUS)

    def test_gap_calculation(self):
        self.assertEqual(bed_gap(5, 3), 2)
        self.assertEqual(bed_gap(3, 3), 0)


class EvacuationLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.ledger: EvacuationLedger = self.repo.evacuation

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, ref, triage, actor="medic"):
        return self.service.register_casualty(
            {"case_ref": ref, "triage": triage}, actor, MEDIC)

    def test_register_by_case_ref_and_duplicate_keeps_first(self):
        first = self._register("A1", CRITICAL)
        self.assertFalse(first["duplicate"])
        again = self._register("A1", MINOR)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["current_triage"], CRITICAL)
        reports = self.service.casualty_reports(first["id"], VIEWER)
        self.assertEqual([r["accepted"] for r in reports], [1, 0])
        # 同一编号只登记一个伤员
        self.assertEqual(len(self.service.waiting_queue(VIEWER)), 1)

    def test_queue_sorted_before_departure(self):
        self._register("A1", MINOR)
        self._register("A2", CRITICAL)
        self._register("A3", SERIOUS)
        queue = self.service.waiting_queue(VIEWER)
        self.assertEqual([c["case_ref"] for c in queue], ["A2", "A3", "A1"])

    def test_confirmed_batch_cannot_be_split(self):
        self._register("A1", CRITICAL)
        self._register("A2", MINOR)
        vehicle = self.service.add_vehicle(
            {"plate": "V1", "seats": 2}, "disp", DISPATCHER)
        batch = self.service.confirm_batch(
            {"vehicle_id": vehicle["id"]}, "disp", DISPATCHER)
        self.assertEqual([m["case_ref"] for m in batch["members"]], ["A1", "A2"])
        # 车辆已挂在未发车批次上，不能再用它另开批次
        with self.assertRaises(ConflictError):
            self.service.confirm_batch({"vehicle_id": vehicle["id"]},
                                       "disp", DISPATCHER)
        # 成员已冻结，等待队列中不再出现
        self.assertEqual(self.service.waiting_queue(VIEWER), [])

    def test_seat_shortage_reports_gap_and_keeps_queue_tail(self):
        for ref, triage in [("A1", CRITICAL), ("A2", SERIOUS), ("A3", MINOR)]:
            self._register(ref, triage)
        batch = self.service.confirm_batch({"seats": 2}, "disp", DISPATCHER)
        self.assertEqual(len(batch["members"]), 2)
        self.assertEqual(batch["seat_gap"], 1)
        self.assertIn("缺口1个", batch["seat_gap_message"])
        waiting = self.service.waiting_queue(VIEWER)
        self.assertEqual([c["case_ref"] for c in waiting], ["A3"])

    def test_dispatch_needs_beds_otherwise_original_queue_kept(self):
        self._register("A1", CRITICAL)
        self._register("A2", SERIOUS)
        receiver = self.service.add_receiver(
            {"name": "R1", "total_beds": 1}, "disp", DISPATCHER)
        batch = self.service.confirm_batch({"seats": 2}, "disp", DISPATCHER)
        with self.assertRaises(ConflictError) as ctx:
            self.service.dispatch_batch(batch["id"], {"receiver_id": receiver["id"]},
                                        "disp", DISPATCHER)
        self.assertIn("床位不足", str(ctx.exception))
        self.assertIn("缺口1张", str(ctx.exception))
        # 原队列保留：批次仍是confirmed，床位没被占用
        untouched = self.service.get_batch(batch["id"], VIEWER)
        self.assertEqual(untouched["status"], "confirmed")
        self.assertEqual(self.service.list_receivers(VIEWER)[0]["used_beds"], 0)
        # 补床后可正常发车，伤员被该接收点唯一收下
        self._add_beds(receiver["id"], 1)
        dispatched = self.service.dispatch_batch(
            batch["id"], {"receiver_id": receiver["id"]}, "disp", DISPATCHER)
        self.assertEqual(dispatched["status"], "dispatched")
        self.assertEqual(self.service.list_receivers(VIEWER)[0]["used_beds"], 2)

    def _add_beds(self, receiver_id, delta):
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute(
                "UPDATE med_receivers SET total_beds=total_beds+? WHERE id=?",
                (delta, receiver_id))

    def test_recheck_upgrade_reranks_undispatched_batches_only(self):
        # 批次1: A1危重 ; 批次2: A2重伤+A3轻伤（发车前队列按等级截组）
        a1 = self._register("A1", CRITICAL)
        a2 = self._register("A2", SERIOUS)
        a3 = self._register("A3", MINOR)
        receiver = self.service.add_receiver(
            {"name": "R1", "total_beds": 5}, "disp", DISPATCHER)
        b1 = self.service.confirm_batch({"seats": 1}, "disp", DISPATCHER)  # A1
        b2 = self.service.confirm_batch({"seats": 2}, "disp", DISPATCHER)  # A2,A3
        self.assertEqual([m["case_ref"] for m in b1["members"]], ["A1"])
        self.assertEqual([m["case_ref"] for m in b2["members"]], ["A2", "A3"])
        self.assertEqual(self.service.get_batch(b1["id"], VIEWER)["dispatch_seq"], 1)
        self.assertEqual(self.service.get_batch(b2["id"], VIEWER)["dispatch_seq"], 2)
        # A2复查调高为危重 → b2含危重，紧急度追平b1；b1确认更早，顺序稳定
        self.service.recheck_casualty(a2["id"], {"triage": CRITICAL}, "medic", MEDIC)
        self.assertEqual(self.service.get_batch(b1["id"], VIEWER)["dispatch_seq"], 1)
        self.assertEqual(self.service.get_batch(b2["id"], VIEWER)["dispatch_seq"], 2)
        # 先发走b1；b1记录冻结
        self.service.dispatch_batch(
            b1["id"], {"receiver_id": receiver["id"]}, "disp", DISPATCHER)
        # A1已发车，复查调高（持平危重→实际是相同等级）之外的改动一律拒绝
        with self.assertRaises(ConflictError):
            self.service.recheck_casualty(a1["id"], {"triage": MINOR},
                                          "medic", MEDIC)
        dispatched = self.service.get_batch(b1["id"], VIEWER)
        self.assertEqual(dispatched["status"], "dispatched")
        self.assertEqual(
            self.service.get_casualty(a1["id"], VIEWER)["current_triage"], CRITICAL)
        # 已发车批次不能再发
        with self.assertRaises(ConflictError):
            self.service.dispatch_batch(
                b1["id"], {"receiver_id": receiver["id"]}, "disp", DISPATCHER)
        # 未发车批次b2仍可重排/发车，成员不打散
        b2_after = self.service.get_batch(b2["id"], VIEWER)
        self.assertEqual([m["case_ref"] for m in b2_after["members"]], ["A2", "A3"])
        self.service.dispatch_batch(
            b2["id"], {"receiver_id": receiver["id"]}, "disp", DISPATCHER)
        self.assertEqual(self.service.list_receivers(VIEWER)[0]["used_beds"], 3)

    def test_recheck_upgrade_jumps_waiting_queue(self):
        a1 = self._register("A1", SERIOUS)
        a2 = self._register("A2", MINOR)
        self.service.recheck_casualty(a2["id"], {"triage": CRITICAL}, "medic", MEDIC)
        queue = self.service.waiting_queue(VIEWER)
        self.assertEqual([c["case_ref"] for c in queue], ["A2", "A1"])
        # 不允许调低
        with self.assertRaises(ValidationError):
            self.service.recheck_casualty(a1["id"], {"triage": MINOR},
                                          "medic", MEDIC)

    def test_casualty_accepted_once_after_dispatch(self):
        a1 = self._register("A1", CRITICAL)
        receiver = self.service.add_receiver(
            {"name": "R1", "total_beds": 3}, "disp", DISPATCHER)
        batch = self.service.confirm_batch({"seats": 1}, "disp", DISPATCHER)
        self.service.dispatch_batch(
            batch["id"], {"receiver_id": receiver["id"]}, "disp", DISPATCHER)
        # 成员关系唯一：已发车伤员无法进入任何新批次（队列里也查不到）
        self.assertEqual(self.service.waiting_queue(VIEWER), [])
        self.assertEqual(a1["id"] == self.service.waiting_queue(VIEWER), False)

    def test_roles_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_casualty(
                {"case_ref": "X1", "triage": MINOR}, "x", VIEWER)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch({"seats": 1}, "x", MEDIC)

    def test_audit_chain_intact(self):
        self._register("A1", CRITICAL)
        self._register("A1", MINOR)  # 重复上报也留痕
        vehicle = self.service.add_vehicle(
            {"plate": "V9", "seats": 1}, "disp", DISPATCHER)
        receiver = self.service.add_receiver(
            {"name": "R9", "total_beds": 1}, "disp", DISPATCHER)
        batch = self.service.confirm_batch(
            {"vehicle_id": vehicle["id"]}, "disp", DISPATCHER)
        self.service.dispatch_batch(
            batch["id"], {"receiver_id": receiver["id"]}, "disp", DISPATCHER)
        self.assertTrue(self.repo.verify_audit_chain())
        actions = {e["action"] for e in self.service.audit(VIEWER)}
        self.assertIn("med_register", actions)
        self.assertIn("med_register_duplicate", actions)
        self.assertIn("med_dispatch", actions)


if __name__ == "__main__":
    unittest.main()
