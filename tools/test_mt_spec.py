"""W2 nested spec tests (plan v4.17 W2d B13). Run: python tools/test_mt_spec.py
Author: cykbot9 (AI agent).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt_spec as ms  # noqa: E402

BASE = {"require_running": True, "time_increase_min": 2.0, "stick_peak_min": 0.3,
        "pos_delta_min": 0.5, "stick_settle_max": 0.2}
DUAL = {"aim_peak_min": 0.3, "aim_settle_max": 0.1, "simultaneous_min_samples": 3,
        "fire_delta_min": 1}
JUMP = {"jump_delta_min": 1, "vy_peak_min": 1.0, "require_grounded_before": True}


class Spec(unittest.TestCase):
    def test_base_only(self):
        base, dual, jump = ms.parse_spec(BASE)
        self.assertIsNone(dual)
        self.assertIsNone(jump)
        self.assertEqual(set(base), ms.BASE_KEYS)

    def test_dual(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL))
        self.assertEqual(dual, DUAL)
        self.assertIsNone(jump)

    def test_jump(self):
        base, dual, jump = ms.parse_spec(dict(BASE, jump=JUMP))
        self.assertEqual(jump, JUMP)

    def test_both(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        self.assertEqual(dual, DUAL)
        self.assertEqual(jump, JUMP)

    def test_unknown_top(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, bogus=1))

    def test_unknown_nested_dual(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=dict(DUAL, bogus=1)))

    def test_unknown_nested_jump(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, jump=dict(JUMP, bogus=1)))

    def test_dual_requires_base(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec({"dual": DUAL})

    def test_dual_missing_key(self):
        d = dict(DUAL); del d["fire_delta_min"]
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=d))

    def test_dual_bad_aim_peak(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=dict(DUAL, aim_peak_min=0.05)))

    def test_dual_settle_above_peak(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=dict(DUAL, aim_settle_max=0.5)))

    def test_dual_simultaneous_lt3(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=dict(DUAL, simultaneous_min_samples=2)))

    def test_dual_fire_delta_zero(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=dict(DUAL, fire_delta_min=0)))

    def test_jump_vy_zero(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, jump=dict(JUMP, vy_peak_min=0)))

    def test_jump_grounded_false(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, jump=dict(JUMP, require_grounded_before=False)))

    def test_bool_not_int(self):
        with self.assertRaises(ms.SpecError):
            ms.parse_spec(dict(BASE, dual=dict(DUAL, fire_delta_min=True)))

    def test_declared_roles(self):
        self.assertEqual(ms.declared_roles(None, None), {"move"})
        self.assertEqual(ms.declared_roles(DUAL, None), {"move", "aim", "shoot"})
        self.assertEqual(ms.declared_roles(None, JUMP), {"move", "jump"})
        self.assertEqual(ms.declared_roles(DUAL, JUMP), {"move", "aim", "shoot", "jump"})


if __name__ == "__main__":
    unittest.main(verbosity=2)