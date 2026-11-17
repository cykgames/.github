"""W2d mt_nav closed-loop navigator tests (offline scripted device, plan
v4.17 W2d fight-mode navigation). Covers schema fail-closed, steering math
(axis-multiplier compensation + xform/offset mapping), and the full navigate
loop against a scripted telemetry/injection fake: door-jump advance,
running-goal completion, calibration transform derivation, and the negative
paths (budget, cap, no grab, non-rigid offset, unexpected jump, no play).
Run: python tools/test_mt_nav.py
Author: cykbot9 (AI agent).
"""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt_nav as nv  # noqa: E402

W, H = 2340, 1080
XF = [0.0, 0.0, 3.6892, 0.0, 0.0, 3.6889]
OFF = (80.0, 84.0)
SID = "abcdef0123456789:4242:100000"

SPEC = {
    "stick_home": [66, 202], "axis_mult": [1.0, 0.5], "knob_throw": 35.0,
    "hold_ms": 700, "budget_s": 120,
    "legs": [
        {"to": [157, 118], "tol": 6, "settle_expect": [120, 121], "settle_tol": 25},
        {"to": [181, 131], "tol": 6},
        {"to": [203, 141], "tol": 6},
        {"to": [207, 119], "tol": 8, "jump_advance": True, "jump_only": True},
        {"to": [600, 265], "tol": 10, "running_goal": True,
         "settle_expect": [650, 286], "settle_tol": 45},
    ],
}
# scripted world states consumed one per injection run (index 0 = pre-run)
STATES = [
    ((120, 121), False),   # title entry done, room_start spawn
    ((118, 119), False),   # calibration tap nudges a couple of units
    ((155, 118), False),   # pickup 1 collected (within tol 6)
    ((180, 130), False),   # pickup 2
    ((202, 140), False),   # pickup 3
    ((206, 123), False),   # at door, within tol 8 but jump_only waits
    ((650, 286), False),   # door transition -> room_0 entry Stairs4
    ((615, 272), False),   # leg4 steer 1 (approaching CP3 strip)
    ((600, 265), True),    # crossed ArenaEntry -> fight_mode -> running
]


class FakeDev:
    def __init__(self, states, emit_route=True, offset_jitter=0.0):
        self.up = 100000
        self.seq = {"input_log": 0, "route_log": 0}
        self.samples = []
        self.rings = []
        self.emit_route = emit_route
        self.offset_jitter = offset_jitter
        self._append_sample(states[0], self.up)

    def _append_sample(self, state, up):
        pos, running = state
        self.samples.append(dict(ts=1763400000.0 + up / 1000.0, up=up,
                                 session_id=SID, scene="play", running=running,
                                 time=1.0, stick=[0.0, 0.0], aim=[0.0, 0.0],
                                 shoot=False, fire_count=0,
                                 pos=[pos[0], pos[1], 0.0], fps=60.0, xform=XF))

    def push_ring(self, ring, entries):
        lo, hi = entries[0]["s"], entries[-1]["s"]
        self.rings.append(dict(sid=SID, ring=ring, seq_range=[lo, hi],
                               entries=entries))

    def adb(self, *a, timeout=None):
        if a[0] == "logcat" and "-c" in a:
            return ""
        if a[0] == "logcat":
            lines = []
            for s in self.samples:
                lines.append("11-17 22:00:00.000  4242  4242 I Godot   : "
                             "[CYKTEST] " + json.dumps(s))
            for r in self.rings:
                lines.append("11-17 22:00:00.000  4242  4242 I Godot   : "
                             "[CYKTEST-RING] " + json.dumps(r))
            return "\n".join(lines)
        return ""


class FakeInjector:
    """Scripted B' injector: consumes one world state per run, emits a legal
    down/up trace plus matching input/route receipts at physical-OFF coords
    (the rigid display offset the navigator must derive)."""

    def __init__(self, dev, states, ok=True):
        self.dev = dev
        self.states = states
        self.i = 0
        self.ok = ok
        self.recipes = []

    def run(self, recipe, run_id=None):
        self.recipes.append(copy.deepcopy(recipe))
        if not self.ok:
            return {"ok": False, "error": "scripted failure"}
        self.i += 1
        state = self.states[min(self.i, len(self.states) - 1)]
        ct = recipe["contacts"][0]
        px, py = ct["path"][0][0] * W, ct["path"][0][1] * H
        hold = ct["hold"]
        d = self.dev
        t_down = d.up
        t_up = d.up + hold
        d.up = t_up + 100
        trace = [
            dict(phase="business", run_id=run_id, sequence=0, action="down",
                 index=0, n=1, ids=[0], coords=[[px, py]], event_time=t_down,
                 t_before=t_down, t_after=t_down + 2, delivery_ms=2, mode=2,
                 ok=True, edge_seq=0, atom_seq=0),
            dict(phase="business", run_id=run_id, sequence=1, action="up",
                 index=0, n=1, ids=[0], coords=[[px, py]], event_time=t_up,
                 t_before=t_up, t_after=t_up + 2, delivery_ms=2, mode=2,
                 ok=True, edge_seq=1, atom_seq=0),
        ]
        jitter = d.offset_jitter
        d.seq["input_log"] += 1
        s_down = d.seq["input_log"]
        d.push_ring("input_log", [dict(s=s_down, u=t_down + 1, k="touch_down",
                                       i=0, d=0, x=px - OFF[0], y=py - OFF[1],
                                       e=9000 + s_down)])
        d.seq["input_log"] += 1
        s_up = d.seq["input_log"]
        d.push_ring("input_log", [dict(s=s_up, u=t_up + 1, k="touch_up", i=0,
                                       d=0, x=px - OFF[0] + jitter,
                                       y=py - OFF[1], e=9000 + s_up)])
        if d.emit_route:
            d.seq["route_log"] += 1
            d.push_ring("route_log", [dict(s=d.seq["route_log"], u=t_down + 2,
                                           src=s_down, id=0, role="move",
                                           acc=True, ver=d.seq["route_log"],
                                           val=[0.0, 0.0])])
        d._append_sample(state, t_up + 60)
        return {"ok": True, "run_id": run_id,
                "mtresult": {"trace": trace, "all_ok": True,
                             "cleanup_ok": True, "mode": 2},
                "recipe_contacts": recipe["contacts"]}


def run_nav(states=None, spec=None, **fakekw):
    states = states or STATES
    dev = FakeDev(states, **fakekw)
    inj = FakeInjector(dev, states)
    with mock.patch.object(nv.time, "sleep", lambda s: None):
        ev = nv.navigate(dev, inj, spec or SPEC, W, H)
    return ev, dev, inj


class Schema(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(nv.validate_nav(SPEC))

    def test_unknown_key(self):
        s = dict(SPEC, bogus=1)
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)

    def test_missing_legs(self):
        s = {k: v for k, v in SPEC.items() if k != "legs"}
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)

    def test_bad_axis_mult(self):
        s = dict(SPEC, axis_mult=[1.0, 0.0])
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)

    def test_running_goal_nonfinal(self):
        s = copy.deepcopy(SPEC)
        s["legs"][0]["running_goal"] = True
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)

    def test_knob_throw_too_small(self):
        s = dict(SPEC, knob_throw=26.0)
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)

    def test_hold_range(self):
        with self.assertRaises(nv.NavError):
            nv.validate_nav(dict(SPEC, hold_ms=50))

    def test_leg_unknown_key(self):
        s = copy.deepcopy(SPEC)
        s["legs"][0]["warp"] = True
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)

    def test_leg_bad_tol(self):
        s = copy.deepcopy(SPEC)
        s["legs"][0]["tol"] = 0
        with self.assertRaises(nv.NavError):
            nv.validate_nav(s)


class SteeringMath(unittest.TestCase):
    def test_right_target_compensates_axis(self):
        # pure world RIGHT -> stick right, full throw, y stays at home
        r = nv._steer_recipe([66, 202], [1.0, 0.5], [207, 119], [120, 121],
                             XF, OFF, W, H, 35.0, 700)
        fx, fy = r["contacts"][0]["path"][0]
        px, py = fx * W, fy * H
        dx = (px - OFF[0]) / XF[2] - 66
        dy = (py - OFF[1]) / XF[5] - 202
        self.assertGreater(dx, 30)
        # world dy=-2 doubles through axis_mult.y=0.5 -> stick dy ~ -1.6
        self.assertAlmostEqual(dy, -2.0 / 0.5 / (87 ** 2 + 4 ** 2) ** 0.5 * 35,
                               delta=0.1)

    def test_up_target_double_y_output(self):
        # world UP needs 2x stick y (axis_mult.y=0.5): dir ~ (0,-1)
        r = nv._steer_recipe([66, 202], [1.0, 0.5], [120, 61], [120, 121],
                             XF, OFF, W, H, 35.0, 700)
        fx, fy = r["contacts"][0]["path"][0]
        dx = (fx * W - OFF[0]) / XF[2] - 66
        dy = (fy * H - OFF[1]) / XF[5] - 202
        self.assertAlmostEqual(dx, 0.0, delta=0.5)
        self.assertAlmostEqual(dy, -35.0, delta=0.5)

    def test_diagonal_world_direction(self):
        # world delta (-50,-21) -> stick dir normalize(-50,-42)
        r = nv._steer_recipe([66, 202], [1.0, 0.5], [600, 265], [650, 286],
                             XF, OFF, W, H, 35.0, 700)
        fx, fy = r["contacts"][0]["path"][0]
        dx = (fx * W - OFF[0]) / XF[2] - 66
        dy = (fy * H - OFF[1]) / XF[5] - 202
        n = (dx * dx + dy * dy) ** 0.5
        self.assertAlmostEqual(n, 35.0, delta=0.2)
        self.assertAlmostEqual(dx / n, -50 / (50 * 50 + 42 * 42) ** 0.5, delta=0.01)
        self.assertAlmostEqual(dy / n, -42 / (50 * 50 + 42 * 42) ** 0.5, delta=0.01)

    def test_fraction_clamped(self):
        r = nv._steer_recipe([66, 202], [1.0, 0.5], [-5000, -5000], [120, 121],
                             XF, OFF, W, H, 35.0, 700)
        fx, fy = r["contacts"][0]["path"][0]
        self.assertTrue(0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0)


class NavigateLoop(unittest.TestCase):
    def test_full_route(self):
        ev, dev, inj = run_nav()
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["coord_offset"], [80.0, 84.0])
        self.assertEqual(ev["offset_spread"], 0.0)
        self.assertTrue(ev["final_running"])
        self.assertEqual([l["done_by"] for l in ev["legs"]],
                         ["tolerance", "tolerance", "tolerance",
                          "door_jump", "running_goal"])
        self.assertEqual(ev["legs"][4]["final_pos"], [600, 265])
        # calibration + 1+1+1 pickup steers + 2 door steers + 2 arena steers
        self.assertEqual(len(inj.recipes), 8)
        # first pickup steer aims RIGHT of the stick home
        fx, fy = inj.recipes[1]["contacts"][0]["path"][0]
        self.assertGreater(fx, 0.137)
        # arena first steer aims UP-LEFT (toward the CP3 strip)
        fx, fy = inj.recipes[6]["contacts"][0]["path"][0]
        self.assertLess(fx, 0.137)
        self.assertLess(fy, 0.768)

    def test_budget_exhausted(self):
        with self.assertRaises(nv.NavError) as cm:
            run_nav(spec=dict(SPEC, budget_s=0.001))
        self.assertIn("budget", str(cm.exception))

    def test_calibration_inject_fail(self):
        dev = FakeDev(STATES)
        inj = FakeInjector(dev, STATES, ok=False)
        with mock.patch.object(nv.time, "sleep", lambda s: None):
            with self.assertRaises(nv.NavError) as cm:
                nv.navigate(dev, inj, SPEC, W, H)
        self.assertIn("calibration injection failed", str(cm.exception))

    def test_calibration_no_grab(self):
        with self.assertRaises(nv.NavError) as cm:
            run_nav(emit_route=False)
        self.assertIn("did not grab", str(cm.exception))

    def test_non_rigid_offset_rejected(self):
        with self.assertRaises(nv.NavError) as cm:
            run_nav(offset_jitter=20.0)
        self.assertIn("transform", str(cm.exception))

    def test_unexpected_jump_rejected(self):
        spec = copy.deepcopy(SPEC)
        # door leg without jump_advance + tiny tol (so it steers into the
        # transition rather than exiting on proximity): jump becomes unexpected
        spec["legs"][3] = {"to": [207, 119], "tol": 0.1, "jump_advance": False}
        with self.assertRaises(nv.NavError) as cm:
            run_nav(spec=spec)
        self.assertIn("unexpected position jump", str(cm.exception))

    def test_jump_only_requires_jump_advance(self):
        spec = copy.deepcopy(SPEC)
        spec["legs"][3] = {"to": [207, 119], "tol": 8, "jump_only": True}
        with self.assertRaises(nv.NavError):
            nv.validate_nav(spec)

    def test_iteration_cap(self):
        stuck = [((120, 121), False)] * 80
        with self.assertRaises(nv.NavError) as cm:
            run_nav(states=stuck)
        self.assertIn("iteration cap", str(cm.exception))

    def test_no_play_scene(self):
        dev = FakeDev(STATES)
        dev.samples[0]["scene"] = "menu"
        inj = FakeInjector(dev, STATES)
        with mock.patch.object(nv.time, "sleep", lambda s: None):
            with mock.patch.object(nv, "PLAY_WAIT", 0.3):
                with self.assertRaises(nv.NavError) as cm:
                    nv.navigate(dev, inj, SPEC, W, H)
        self.assertIn("play-scene", str(cm.exception))

    def test_missing_xform_rejected(self):
        dev = FakeDev(STATES)
        del dev.samples[0]["xform"]
        inj = FakeInjector(dev, STATES)
        with mock.patch.object(nv.time, "sleep", lambda s: None):
            with self.assertRaises(nv.NavError) as cm:
                nv.navigate(dev, inj, SPEC, W, H)
        self.assertIn("xform", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
