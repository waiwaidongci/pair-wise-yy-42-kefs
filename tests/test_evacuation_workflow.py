import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class EvacuationWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "evac.db"))
        self.svc = Service(self.repo)
        self.FC, self.LG = "field_commander", "logistics"

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def register(self, ref, triage):
        return self.svc.register_casualty(
            {"site_ref": ref, "triage": triage}, "medic", self.FC)

    def prepare(self):
        for ref, triage in [("S-1", "minor"), ("S-2", "critical"),
                            ("S-3", "serious"), ("S-4", "critical")]:
            self.register(ref, triage)
        self.svc.create_vehicle({"plate": "V-1", "seats": 2}, "log", self.LG)
        self.svc.create_receiver({"code": "R-1", "name": "医院", "beds": 10},
                                 "log", self.LG)

    def test_duplicate_report_keeps_first_triage(self):
        first = self.register("X-1", "minor")
        with self.assertRaises(ConflictError) as ctx:
            self.register("X-1", "critical")
        self.assertEqual(ctx.exception.details["casualty"]["triage"], "minor")
        self.assertEqual(ctx.exception.details["casualty"]["id"], first["id"])

    def test_queue_order_and_batch_priority(self):
        self.prepare()
        queue = [c["site_ref"] for c in self.svc.waiting_queue("viewer")]
        self.assertEqual(queue, ["S-2", "S-4", "S-3", "S-1"])
        batch = self.svc.plan_batch(
            {"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.assertEqual([c["site_ref"] for c in batch["casualties"]],
                         ["S-2", "S-4"])  # 危重先走，同等级按登记先后
        self.assertEqual(batch["queued_remaining"], 2)

    def test_confirmed_batch_cannot_be_split_or_double_confirmed(self):
        self.prepare()
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.svc.confirm_batch(1, "log", self.LG)
        with self.assertRaises(ConflictError):
            self.svc.confirm_batch(1, "log", self.LG)
        batch = self.svc.get_batch(1, "viewer")
        self.assertTrue(batch["locked"])
        self.assertEqual(len(batch["casualties"]), 2)

    def test_same_casualty_not_in_two_batches(self):
        self.prepare()
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.svc.create_vehicle({"plate": "V-2", "seats": 10}, "log", self.LG)
        second = self.svc.plan_batch(
            {"vehicle_id": 2, "receiver_id": 1}, "log", self.LG)
        refs = {c["site_ref"] for c in second["casualties"]}
        self.assertEqual(refs, {"S-3", "S-1"})  # 已在批次1的两人不会再被收下

    def test_bed_shortfall_blocks_confirm_and_keeps_queue(self):
        # 两车各3座、接收点4床、5名伤员：确认前两个批次先后组好（各按当时空床组批），
        # 批次1先确认占走3床；批次2再确认时空床仅1、需2 -> 缺1，确认被拒、批次保留
        for ref in ["A", "B", "C", "D", "E"]:
            self.register(ref, "serious")
        self.svc.create_vehicle({"plate": "V-1", "seats": 3}, "log", self.LG)
        self.svc.create_vehicle({"plate": "V-2", "seats": 3}, "log", self.LG)
        self.svc.create_receiver({"code": "R", "name": "院", "beds": 4},
                                 "log", self.LG)
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.svc.plan_batch({"vehicle_id": 2, "receiver_id": 1}, "log", self.LG)
        self.svc.confirm_batch(1, "log", self.LG)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_batch(2, "log", self.LG)
        self.assertEqual(ctx.exception.details["gap"]["bed_shortfall"], 1)
        self.assertEqual(self.svc.get_batch(2, "viewer")["status"], "planned")
        # 被拒批次成员未被打散
        self.assertEqual(len(self.svc.get_batch(2, "viewer")["casualties"]), 2)

    def test_recheck_upgrades_and_reorders_undispatched_only(self):
        self.prepare()
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.svc.confirm_batch(1, "log", self.LG)
        self.svc.depart_batch(1, "log", self.LG)
        # 已发车记录不动
        departed_id = next(c["id"] for c in self.svc.list_casualties("viewer")
                           if c["status"] == "dispatched")
        with self.assertRaises(ConflictError):
            self.svc.recheck_casualty(departed_id, {"triage": "critical"},
                                      "medic", self.FC)
        # 待后送轻伤调高为危重，仍可在新批次中排到重伤之前
        s1 = next(c for c in self.svc.list_casualties("viewer")
                  if c["site_ref"] == "S-1")
        self.svc.recheck_casualty(s1["id"], {"triage": "critical"},
                                  "medic", self.FC)
        queue = [c["site_ref"] for c in self.svc.waiting_queue("viewer")]
        self.assertEqual(queue[0], "S-1")
        with self.assertRaises(ConflictError):  # 不允许调低
            self.svc.recheck_casualty(s1["id"], {"triage": "serious"},
                                      "medic", self.FC)

    def test_admission_is_unique_per_casualty(self):
        self.prepare()
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.svc.confirm_batch(1, "log", self.LG)
        self.svc.depart_batch(1, "log", self.LG)
        target = self.svc.get_batch(1, "viewer")["casualties"][0]["id"]
        self.svc.admit_casualty(target, "rcv", self.LG)
        with self.assertRaises(ConflictError):
            self.svc.admit_casualty(target, "rcv", self.LG)

    def test_role_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.svc.register_casualty({"site_ref": "Z", "triage": "minor"},
                                       "m", "viewer")
        self.register("Z", "minor")
        self.svc.create_vehicle({"plate": "V", "seats": 1}, "log", self.LG)
        self.svc.create_receiver({"code": "R", "name": "院", "beds": 1},
                                 "log", self.LG)
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_batch(1, "f", self.FC)  # 只有后勤能确认发车

    def test_audit_chain_covers_evacuation(self):
        self.prepare()
        self.svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", self.LG)
        self.svc.confirm_batch(1, "log", self.LG)
        self.svc.depart_batch(1, "log", self.LG)
        self.assertTrue(self.repo.verify_audit_chain())
        actions = {e["action"] for e in self.repo.list_audit()}
        self.assertIn("register", actions)
        self.assertIn("plan_batch", actions)
        self.assertIn("depart_batch", actions)


if __name__ == "__main__":
    unittest.main()
