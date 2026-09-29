import unittest

from src import rules
from src.domain import ConflictError, ValidationError


class EvacuationRulesTest(unittest.TestCase):
    def c(self, cid, triage, seq):
        return {"id": cid, "triage": triage, "registered_seq": seq}

    def test_dispatch_order_is_critical_serious_minor_then_seq(self):
        queue = [self.c(1, "minor", 1), self.c(2, "critical", 2),
                 self.c(3, "serious", 3), self.c(4, "critical", 4),
                 self.c(5, "serious", 5)]
        order = [c["id"] for c in rules.order_for_dispatch(queue)]
        # 危重优先，同等级按现场登记先后；重伤、轻伤依次
        self.assertEqual(order, [2, 4, 3, 5, 1])

    def test_recheck_only_accepts_upgrade(self):
        self.assertTrue(rules.is_upgrade("minor", "critical"))
        self.assertTrue(rules.is_upgrade("serious", "critical"))
        self.assertFalse(rules.is_upgrade("critical", "minor"))
        self.assertFalse(rules.is_upgrade("serious", "serious"))
        with self.assertRaises(ConflictError):
            rules.validate_recheck("critical", "minor", "planned")  # 调低
        with self.assertRaises(ConflictError):
            rules.validate_recheck("serious", "serious", "planned")  # 平级

    def test_departed_batch_never_changes(self):
        with self.assertRaises(ConflictError):
            rules.validate_recheck("serious", "critical", "departed")

    def test_batch_transition_guard(self):
        rules.validate_batch_transition("planned", "confirmed")
        rules.validate_batch_transition("confirmed", "departed")
        with self.assertRaises(ConflictError):
            rules.validate_batch_transition("planned", "departed")
        with self.assertRaises(ConflictError):
            rules.validate_batch_transition("departed", "planned")

    def test_capacity_gap_report(self):
        members = [self.c(i, "serious", i) for i in range(5)]
        gap = rules.capacity_gap(members, seats=3, beds=4)
        self.assertEqual(gap["needed"], 5)
        self.assertEqual(gap["seat_shortfall"], 2)
        self.assertEqual(gap["bed_shortfall"], 1)
        ok, no_gap = rules.can_lock_batch(members[:2], seats=2, beds=4)
        self.assertTrue(ok)
        self.assertEqual(no_gap["seat_shortfall"], 0)
        with self.assertRaises(ValidationError):
            rules.triage_rank("walking")


if __name__ == "__main__":
    unittest.main()
