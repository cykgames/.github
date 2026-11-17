"""W2c collector + chain-matcher tests (plan v4.17 W2c). Synthetic fixtures +
real headless 002 chain. Run: python tools/test_w2c.py > out.log 2>&1
Author: cykbot9 (AI agent).
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt_collect as c  # noqa: E402


def ring_line(ring, entries, sid="a" * 16 + ":123:1000"):
    lo, hi = entries[0]["s"], entries[-1]["s"]
    d = {"sid": sid, "ring": ring, "seq_range": [lo, hi], "entries": entries}
    raw = "[CYKTEST-RING] " + json.dumps(d)
    return len(raw.encode("utf-8")), d


def inp(s, k, i, d, x, y, u):
    return {"s": s, "k": k, "i": i, "d": d, "x": x, "y": y, "u": u, "e": 1000 + s}


# dual injector trace: stick id0 (move) + J id1 (jump)
TRACE = [
    dict(action="down", index=0, ids=[0], coords=[[100, 200]], t_before=1000, t_after=1004, sequence=0, edge_seq=0, atom_seq=0),
    dict(action="pointer_down", index=1, ids=[0, 1], coords=[[100, 200], [900, 2100]], t_before=1010, t_after=1014, sequence=1, edge_seq=1, atom_seq=0),
    dict(action="move", index=0, ids=[0, 1], coords=[[150, 200], [900, 2100]], t_before=1040, t_after=1044, sequence=2, edge_seq=2, atom_seq=0),
    dict(action="pointer_up", index=1, ids=[0, 1], coords=[[150, 200], [900, 2100]], t_before=1080, t_after=1084, sequence=3, edge_seq=3, atom_seq=0),
    dict(action="up", index=0, ids=[0], coords=[[150, 200]], t_before=1090, t_after=1094, sequence=4, edge_seq=4, atom_seq=0),
]
# arrival receipts (native touch d=0; emulated mouse d=-1 extra)
INPUT = [
    inp(1, "touch_down", 0, 0, 100, 200, 1004),
    inp(2, "mouse_down", 0, -1, 100, 200, 1003),   # emulation extra
    inp(3, "touch_down", 1, 0, 900, 2100, 1014),
    inp(4, "drag", 0, 0, 150, 200, 1044),
    inp(5, "drag", 1, 0, 900, 2100, 1044),
    inp(6, "touch_up", 1, 0, 900, 2100, 1084),
    inp(7, "touch_up", 0, 0, 150, 200, 1094),
    inp(8, "mouse_up", 0, -1, 150, 200, 1095),     # emulation extra
]
ROLES = {0: "move", 1: "jump"}


class Parse(unittest.TestCase):
    def test_dual_marker(self):
        text = ('01-01 00:00:00.000  123  123 I Godot: [CYKTEST] {"scene":"play","session_id":"x"}\n'
                '01-01 00:00:00.001  123  123 I Godot: [CYKTEST-RING] {"sid":"x","ring":"input_log","seq_range":[1,1],"entries":[{"s":1,"u":1,"k":"drag","i":0,"d":0,"x":0,"y":0,"e":1}]}\n')
        p = c.parse_logcat(text)
        self.assertEqual(len(p["samples"]), 1)
        self.assertEqual(len(p["ring_lines"]), 1)
        self.assertEqual(p["producer_pids"], [123])

    def test_ring_too_big(self):
        big = inp(1, "drag", 0, 0, 0, 0, 1)
        big["pad"] = "x" * 1000
        nb, d = ring_line("input_log", [big])
        with self.assertRaises(c.CollectError):
            c.validate_ring_line(nb, d)

    def test_ring_unknown(self):
        nb, d = ring_line("bogus_ring", [inp(1, "drag", 0, 0, 0, 0, 1)])
        with self.assertRaises(c.CollectError):
            c.validate_ring_line(nb, d)

    def test_ring_missing_keys(self):
        nb, d = ring_line("input_log", [{"s": 1, "u": 1}])
        with self.assertRaises(c.CollectError):
            c.validate_ring_line(nb, d)

    def test_ring_non_monotonic(self):
        nb, d = ring_line("input_log", [inp(2, "drag", 0, 0, 0, 0, 1), inp(1, "drag", 0, 0, 0, 0, 1)])
        with self.assertRaises(c.CollectError):
            c.validate_ring_line(nb, d)

    def test_900B_ok(self):
        e = inp(1, "drag", 0, 0, 0, 0, 1)
        nb, d = ring_line("input_log", [e])
        self.assertLessEqual(nb, 900)
        self.assertTrue(c.validate_ring_line(nb, d))


class Merger(unittest.TestCase):
    def test_dedupe_consistent(self):
        e = inp(1, "drag", 0, 0, 0, 0, 1)
        l1 = ring_line("input_log", [e])
        l2 = ring_line("input_log", [e])
        merged, sids = c.merge_rings([l1, l2])
        self.assertEqual(len(merged["input_log"]), 1)

    def test_dedupe_inconsistent(self):
        l1 = ring_line("input_log", [inp(1, "drag", 0, 0, 0, 0, 1)])
        l2 = ring_line("input_log", [inp(1, "drag", 0, 0, 9, 9, 1)])
        with self.assertRaises(c.CollectError):
            c.merge_rings([l1, l2])


class Session(unittest.TestCase):
    def test_bind_ok(self):
        sid = "a" * 16 + ":123:1000"
        samples = [{"session_id": sid}]
        self.assertEqual(c.bind_session(samples, {sid}, 123), sid)

    def test_wrong_pid(self):
        sid = "a" * 16 + ":123:1000"
        with self.assertRaises(c.CollectError):
            c.bind_session([{"session_id": sid}], {sid}, 999)

    def test_multi_session(self):
        s1 = "a" * 16 + ":123:1000"
        s2 = "b" * 16 + ":123:1000"
        with self.assertRaises(c.CollectError):
            c.bind_session([{"session_id": s1}], {s2}, 123)

    def test_signed_nonce(self):
        sid = "-aaaaaaaaaaaaaaa:123:1000"
        with self.assertRaises(c.CollectError):
            c.bind_session([{"session_id": sid}], {sid}, 123)


class Watermark(unittest.TestCase):
    def _wm(self, ovf, cap=512):
        return {"wm": {r: {"overflow": ovf, "capacity": cap} for r in ("input", "route", "apply", "jump")}}

    def test_overflow_zero(self):
        d = c.check_watermarks([self._wm(0), self._wm(0)])
        self.assertEqual(d["input"], 0)

    def test_overflow_delta(self):
        with self.assertRaises(c.CollectError):
            c.check_watermarks([self._wm(0), self._wm(1)])

    def test_capacity_budget(self):
        with self.assertRaises(c.CollectError):
            c.check_watermarks([self._wm(0, cap=100)], budget={"input": 200})


class Atoms(unittest.TestCase):
    def test_expansion(self):
        atoms = c.expand_atoms(TRACE)
        kinds = [(a["edge_seq"], a["kind"], a["stable_id"]) for a in atoms]
        # id1 (jump) is stationary in the MOVE -> no drag atom for it (Godot
        # emits ScreenDrag only for pointers whose position changed).
        self.assertEqual(kinds, [
            (0, "touch_down", 0), (1, "touch_down", 1),
            (2, "drag", 0),
            (3, "touch_up", 1), (4, "touch_up", 0)])

    def test_match_healthy(self):
        atoms = c.expand_atoms(TRACE)
        cand, emu, clean = c.classify_receipts(INPUT, TRACE)
        self.assertEqual(len(emu), 2)   # mouse_down + mouse_up
        matched, fails = c.match_atoms(atoms, cand)
        self.assertEqual(fails, [])
        self.assertEqual(len(matched), 5)

    def test_unmatched_atom(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(INPUT, TRACE)
        cand = [e for e in cand if not (e["k"] == "drag" and e["i"] == 0)]
        matched, fails = c.match_atoms(atoms, cand)
        self.assertTrue(any(f["reason"] == "no_receipt" for f in fails))

    def test_ambiguous(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(INPUT, TRACE)
        cand.append(inp(99, "touch_down", 0, 0, 100, 200, 1004))  # dup candidate
        matched, fails = c.match_atoms(atoms, cand)
        self.assertTrue(any(f["reason"] == "ambiguous" for f in fails))

    def test_coord_tolerance(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(INPUT, TRACE)
        for e in cand:
            if e["s"] == 1:
                e["x"] = 101.5   # within +/-2
        matched, fails = c.match_atoms(atoms, cand)
        self.assertEqual(fails, [])

    def test_coord_out_of_tol(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(INPUT, TRACE)
        for e in cand:
            if e["s"] == 1:
                e["x"] = 110   # outside +/-2
        matched, fails = c.match_atoms(atoms, cand)
        self.assertTrue(any(f["reason"] == "no_receipt" for f in fails))

    def test_cleanup_not_atom(self):
        atoms = c.expand_atoms(TRACE)
        extra = INPUT + [inp(50, "touch_up", 0, 0, 150, 200, 1200)]  # after cleanup window
        cand, emu, clean = c.classify_receipts(extra, TRACE)
        self.assertEqual(len(clean), 1)
        matched, fails = c.match_atoms(atoms, cand)
        self.assertEqual(fails, [])


class Chain(unittest.TestCase):
    def _chain(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(INPUT, TRACE)
        matched, _ = c.match_atoms(atoms, cand)
        input_by_seq = {e["s"]: e for e in INPUT}
        # move: down id0 arrival s1 -> route move ver1 -> apply move ver1
        # drag id0 arrival s4 -> route move ver2 -> apply move ver2
        # jump: down id1 arrival s3 -> route jump bridge native_meta -> jump_log
        routes = [
            {"s": 1, "u": 1005, "src": 1, "id": 0, "role": "move", "acc": True, "ver": 1, "val": [0, 0]},
            {"s": 2, "u": 1045, "src": 4, "id": 0, "role": "move", "acc": True, "ver": 2, "val": [0.5, 0]},
            {"s": 3, "u": 1015, "src": 3, "id": 1, "role": "jump", "acc": True, "ver": 1, "val": True,
             "bridge": {"path": "native_meta", "status": "finalized", "gui_cyk_seq": 3}},
        ]
        applies = [
            {"s": 1, "u": 1006, "src": 1, "ver": 1, "role": "move", "val": [0, 0], "st": "copied"},
            {"s": 2, "u": 1046, "src": 4, "ver": 2, "role": "move", "val": [0.5, 0], "st": "copied"},
        ]
        jumps = [{"s": 1, "u": 1016, "jumps": 1, "src": 3, "ver": 1, "st": "applied"}]
        return matched, routes, applies, jumps, input_by_seq

    def test_healthy_chain(self):
        matched, routes, applies, jumps, ibs = self._chain()
        # only move-down + jump-down atoms have route/applied here; drag move
        # also links. touch_up atoms have no route (release) -> expect failures
        # for those unless release routes provided. Filter to down/drag atoms.
        links, fails = c.match_route_applied(matched, routes, applies, jumps, ROLES, ibs)
        linked_roles = sorted(l["role"] for l in links)
        self.assertIn("move", linked_roles)
        self.assertIn("jump", linked_roles)
        # jump bridge verified
        jl = [l for l in links if l["role"] == "jump"]
        self.assertEqual(len(jl), 1)

    def test_jump_bridge_bad_seq(self):
        matched, routes, applies, jumps, ibs = self._chain()
        for r in routes:
            if r["role"] == "jump":
                r["bridge"]["gui_cyk_seq"] = 999
        links, fails = c.match_route_applied(matched, routes, applies, jumps, ROLES, ibs)
        self.assertTrue(any("bridge:" in f["reason"] for f in fails))

    def test_apply_order_violation(self):
        matched, routes, applies, jumps, ibs = self._chain()
        for a in applies:
            if a["ver"] == 1:
                a["u"] = 1000   # before route u1005 -> order violation
        links, fails = c.match_route_applied(matched, routes, applies, jumps, ROLES, ibs)
        self.assertTrue(any(f["reason"] == "apply_order" for f in fails))

    def test_jump_latency(self):
        matched, routes, applies, jumps, ibs = self._chain()
        jumps[0]["u"] = 1300   # 1300-1014 > 170
        links, fails = c.match_route_applied(matched, routes, applies, jumps, ROLES, ibs)
        self.assertTrue(any(f["reason"] == "jump_latency" for f in fails))

    def test_mixed_overlap(self):
        with self.assertRaises(c.CollectError):
            c.check_mixed_overlap({"move": "device_uptime", "aim": "legacy_epoch"}, ["move", "aim"])
        self.assertTrue(c.check_mixed_overlap({"move": "device_uptime", "aim": "device_uptime"}, ["move", "aim"]))


class Transform(unittest.TestCase):
    """W2e forward display transform: rigid display->receipt offset derived
    from the run (landscape/stretch surface inset), verified rigid. Uses a
    FRESH receipt base (other tests mutate the shared INPUT global in place)."""
    BASE = [
        inp(1, "touch_down", 0, 0, 100, 200, 1004),
        inp(2, "mouse_down", 0, -1, 100, 200, 1003),
        inp(3, "touch_down", 1, 0, 900, 2100, 1014),
        inp(4, "drag", 0, 0, 150, 200, 1044),
        inp(5, "drag", 1, 0, 900, 2100, 1044),
        inp(6, "touch_up", 1, 0, 900, 2100, 1084),
        inp(7, "touch_up", 0, 0, 150, 200, 1094),
        inp(8, "mouse_up", 0, -1, 150, 200, 1095),
    ]

    def _shifted(self, ox, oy):
        # receipts live in the Godot-screen domain = atom_display - offset
        return [dict(e, x=e["x"] - ox, y=e["y"] - oy) for e in self.BASE]

    def test_derive_identity(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts([dict(e) for e in self.BASE], TRACE)
        ox, oy, n, spread = c.derive_coord_offset(atoms, cand)
        self.assertAlmostEqual(ox, 0.0, places=3)
        self.assertAlmostEqual(oy, 0.0, places=3)
        self.assertGreaterEqual(n, 3)
        self.assertLessEqual(spread, c.COORD_TOL)

    def test_derive_rigid_translation(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(self._shifted(80, 84), TRACE)
        ox, oy, n, spread = c.derive_coord_offset(atoms, cand)
        self.assertAlmostEqual(ox, 80.0, places=3)
        self.assertAlmostEqual(oy, 84.0, places=3)
        self.assertLessEqual(spread, c.COORD_TOL)

    def test_derive_non_rigid_rejected(self):
        atoms = c.expand_atoms(TRACE)
        shifted = self._shifted(80, 84)
        shifted[0]["x"] -= 50   # one receipt off -> non-rigid
        cand, _, _ = c.classify_receipts(shifted, TRACE)
        with self.assertRaises(c.CollectError):
            c.derive_coord_offset(atoms, cand)

    def test_match_with_offset(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(self._shifted(80, 84), TRACE)
        matched, fails = c.match_atoms(atoms, cand, coord_offset=(80, 84))
        self.assertEqual(fails, [])
        self.assertEqual(len(matched), 5)

    def test_match_offset_missing_fails(self):
        atoms = c.expand_atoms(TRACE)
        cand, _, _ = c.classify_receipts(self._shifted(80, 84), TRACE)
        matched, fails = c.match_atoms(atoms, cand)   # default identity
        self.assertTrue(any(f["reason"] == "no_receipt" for f in fails))


if __name__ == "__main__":
    unittest.main(verbosity=2)