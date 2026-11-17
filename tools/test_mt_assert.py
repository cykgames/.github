"""W2 mt state assertion tests (plan v4.17 W2d). Synthetic uptime-domain
healthy dual+jump + negative fixtures. Run: python tools/test_mt_assert.py
Author: cykbot9 (AI agent).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt_assert as ma  # noqa: E402
import mt_spec as ms  # noqa: E402

BASE = {"require_running": True, "time_increase_min": 2.0, "stick_peak_min": 0.3,
        "pos_delta_min": 0.5, "stick_settle_max": 0.2}
DUAL = {"aim_peak_min": 0.3, "aim_settle_max": 0.1, "simultaneous_min_samples": 3,
        "fire_delta_min": 1}
JUMP = {"jump_delta_min": 1, "vy_peak_min": 1.0, "require_grounded_before": True}
WINDOWS = [{"role": "move", "t0": 1000, "t1": 3000},
           {"role": "aim", "t0": 1200, "t1": 2800},
           {"role": "jump", "t0": 1500, "t1": 1700}]


def gen_samples(fire=True, overlap=True, settle=True, jump=True):
    out = []
    fc = 0
    for up in range(500, 4001, 250):
        stick_on = 1100 <= up <= 2900
        aim_on = (1300 <= up <= 2700) if overlap else False
        if aim_on and fire:
            fc += 1
        s = {
            "up": up, "ts": 1700000000 + up / 1000.0, "scene": "play",
            "running": True, "time": (up - 500) / 1000.0,
            "stick": [0.9, 0.0] if stick_on else [0.0, 0.0],
            "aim": [0.8, 0.0] if aim_on else [0.0, 0.0],
            "shoot": aim_on,
            "fire_count": fc,
            "pos": [(up - 500) * 0.01 if stick_on else 0.0, 0.0, 0.0],
            "vy": 8.0 if (jump and 1500 <= up <= 1700) else 0.0,
            "floor": up < 1500,
            "jumps": 1 if (jump and up >= 1700) else 0,
            "jump_held": jump and 1500 <= up <= 1700,
        }
        if not settle and up >= 3300:
            s["stick"] = [0.9, 0.0]   # never settles
        out.append(s)
    return out


class Healthy(unittest.TestCase):
    def test_dual_jump_all_pass(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        checks = ma.mt_state_checks(gen_samples(), WINDOWS, base, dual, jump)
        failed = [c for c in checks if not c["ok"]]
        self.assertEqual(failed, [], msg=str(failed))
        self.assertTrue(ma.all_pass(checks))

    def test_move_only(self):
        base, dual, jump = ms.parse_spec(BASE)
        wins = [{"role": "move", "t0": 1000, "t1": 3000}]
        checks = ma.mt_state_checks(gen_samples(overlap=False, jump=False), wins, base, None, None)
        failed = [c for c in checks if not c["ok"]]
        self.assertEqual(failed, [], msg=str(failed))


class Negatives(unittest.TestCase):
    def test_no_overlap(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        checks = ma.mt_state_checks(gen_samples(overlap=False), WINDOWS, base, dual, jump)
        d = {c["name"]: c["ok"] for c in checks}
        self.assertFalse(d["dual_simultaneous_overlap"])
        self.assertFalse(d["aim_coverage"])

    def test_no_fire(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        checks = ma.mt_state_checks(gen_samples(fire=False), WINDOWS, base, dual, jump)
        d = {c["name"]: c["ok"] for c in checks}
        self.assertFalse(d["fire_delta"])

    def test_stale_settle(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        checks = ma.mt_state_checks(gen_samples(settle=False), WINDOWS, base, dual, jump)
        d = {c["name"]: c["ok"] for c in checks}
        self.assertFalse(d["stick_settle_fresh"])

    def test_no_jump(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        checks = ma.mt_state_checks(gen_samples(jump=False), WINDOWS, base, dual, jump)
        d = {c["name"]: c["ok"] for c in checks}
        self.assertFalse(d["jump_delta"])
        self.assertFalse(d["vy_peak"])

    def test_unconsumed_role(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        wins = [{"role": "move", "t0": 1000, "t1": 3000}]  # aim/jump declared but no window
        checks = ma.mt_state_checks(gen_samples(), wins, base, dual, jump)
        d = {c["name"]: c["ok"] for c in checks}
        self.assertFalse(d["roles_consumed"])

    def test_no_move_window_fail_closed(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        wins = [{"role": "aim", "t0": 1200, "t1": 2800}, {"role": "jump", "t0": 1500, "t1": 1700}]
        checks = ma.mt_state_checks(gen_samples(), wins, base, dual, jump)
        d = {c["name"]: c["ok"] for c in checks}
        self.assertFalse(d["drag_windows_covered"])

    def test_corrupt_up_order(self):
        base, dual, jump = ms.parse_spec(dict(BASE, dual=DUAL, jump=JUMP))
        s = gen_samples()
        s[5]["up"] = 100   # out of order
        with self.assertRaises(ma.AssertError):
            ma.mt_state_checks(s, WINDOWS, base, dual, jump)


if __name__ == "__main__":
    unittest.main(verbosity=2)