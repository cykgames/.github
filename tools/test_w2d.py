"""W2d mt_inject compiler + recipe validation tests (plan v4.17 W2d).
Includes a Python mirror of the jar's program transition validator to prove
the compiler emits legal pointer-array sequences. Run: python tools/test_w2d.py
Author: cykbot9 (AI agent).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt_inject as m  # noqa: E402


def validate_program_mirror(frames):
    """Mirror of Main.java validateProgram (legal pointer-array transitions)."""
    sim = []
    started = False
    for i, fr in enumerate(frames):
        action = fr["action"]
        index = fr.get("index", 0)
        ids = [p["id"] for p in fr["pointers"]]
        n = len(ids)
        assert n > 0, f"frame {i} empty"
        if action == "down":
            assert not started and not sim, f"frame {i} down while active"
            assert n == 1, f"frame {i} down needs 1"
            sim.append(ids[0]); started = True
        elif action == "pointer_down":
            assert started, f"frame {i} pointer_down before down"
            assert index == len(sim), f"frame {i} index != append"
            assert n == len(sim) + 1, f"frame {i} count"
            assert ids[:len(sim)] == sim, f"frame {i} prefix"
            assert ids[index] not in sim, f"frame {i} dup id"
            sim.append(ids[index])
        elif action == "move":
            assert started and n == len(sim) and ids == sim, f"frame {i} move mismatch"
        elif action == "pointer_up":
            assert started and n == len(sim) and ids == sim, f"frame {i} pointer_up mismatch"
            assert 0 <= index < n, f"frame {i} index range"
            sim.pop(index)
        elif action == "up":
            assert started and n == 1 and len(sim) == 1 and ids[0] == sim[0], f"frame {i} up"
            sim.clear(); started = False
        elif action == "cancel":
            assert started and n == len(sim) and ids == sim, f"frame {i} cancel"
            sim.clear(); started = False
        else:
            raise AssertionError(f"frame {i} unknown action {action}")
    return True


DUAL = {"contacts": [
    {"slot": 0, "role": "move", "path": [[0.14, 0.87], [0.24, 0.8]], "hold": 1200},
    {"slot": 1, "role": "aim", "path": [[0.85, 0.87], [0.85, 0.75]], "hold": 1200},
]}
SINGLE = {"contacts": [{"slot": 0, "role": "move", "path": [[0.14, 0.87], [0.24, 0.8]], "hold": 500}]}
JUMP = {"contacts": [
    {"slot": 0, "role": "move", "path": [[0.14, 0.87], [0.24, 0.8]], "hold": 800},
    {"slot": 1, "role": "jump", "path": [[0.85, 0.93]], "hold": 200},
]}


class Validation(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(m.validate_recipe(DUAL))

    def test_unknown_key(self):
        r = {"contacts": [{"slot": 0, "role": "move", "path": [[0.1, 0.1]], "hold": 1, "bogus": 1}]}
        with self.assertRaises(m.InjectError):
            m.validate_recipe(r)

    def test_unknown_role(self):
        r = {"contacts": [{"slot": 0, "role": "fire", "path": [[0.1, 0.1]], "hold": 1}]}
        with self.assertRaises(m.InjectError):
            m.validate_recipe(r)

    def test_empty_path(self):
        r = {"contacts": [{"slot": 0, "role": "move", "path": [], "hold": 1}]}
        with self.assertRaises(m.InjectError):
            m.validate_recipe(r)

    def test_dup_slot(self):
        r = {"contacts": [{"slot": 0, "role": "move", "path": [[0.1, 0.1]], "hold": 1},
                          {"slot": 0, "role": "aim", "path": [[0.2, 0.2]], "hold": 1}]}
        with self.assertRaises(m.InjectError):
            m.validate_recipe(r)

    def test_hold_zero(self):
        r = {"contacts": [{"slot": 0, "role": "move", "path": [[0.1, 0.1]], "hold": 0}]}
        with self.assertRaises(m.InjectError):
            m.validate_recipe(r)

    def test_fraction_range(self):
        r = {"contacts": [{"slot": 0, "role": "move", "path": [[1.5, 0.1]], "hold": 1}]}
        with self.assertRaises(m.InjectError):
            m.validate_recipe(r)

    def test_empty_contacts(self):
        with self.assertRaises(m.InjectError):
            m.validate_recipe({"contacts": []})


class Compiler(unittest.TestCase):
    def test_single_legal(self):
        prog = m.compile_timeline(SINGLE, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))

    def test_equal_hold_byte_identical_legacy(self):
        # per-contact hold extension must not change equal-hold programs:
        # single UP group, reverse slot order, hold delay on the first UP.
        prog = m.compile_timeline(DUAL, 1080, 2340, "t")
        fr = prog["frames"]
        ups = [f for f in fr if f["action"] in ("pointer_up", "up")]
        self.assertEqual(len(ups), 2)
        self.assertEqual(ups[0]["action"], "pointer_up")
        self.assertEqual(ups[0]["index"], 1)
        self.assertEqual(ups[0]["delay_ms"], 1200)
        self.assertEqual([p["id"] for p in ups[0]["pointers"]], [0, 1])
        self.assertEqual(ups[1]["action"], "up")
        self.assertEqual(ups[1]["index"], 0)
        self.assertEqual(ups[1]["delay_ms"], 0)
        self.assertEqual([p["id"] for p in ups[1]["pointers"]], [0])
        self.assertEqual([f["edge_seq"] for f in fr], list(range(len(fr))))

    def test_staggered_higher_slot_first(self):
        # JUMP: jump hold 200 < move hold 800 -> jump lifts first via
        # pointer_up index 1, move UP 600ms later.
        prog = m.compile_timeline(JUMP, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))
        ups = [f for f in prog["frames"] if f["action"] in ("pointer_up", "up")]
        self.assertEqual((ups[0]["action"], ups[0]["index"], ups[0]["delay_ms"]),
                         ("pointer_up", 1, 200))
        self.assertEqual((ups[1]["action"], ups[1]["index"], ups[1]["delay_ms"]),
                         ("up", 0, 600))

    def test_staggered_slot0_first(self):
        # independent release (L3 row 2): move lifts at 1500 while aim holds
        # to 3500 -> POINTER_UP at index 0 (non-primary lift, legal), then UP
        # for the last remaining pointer id 1.
        r = {"contacts": [
            {"slot": 0, "role": "move", "path": [[0.14, 0.87], [0.24, 0.8]], "hold": 1500},
            {"slot": 1, "role": "aim", "path": [[0.85, 0.87], [0.9, 0.75]], "hold": 3500}]}
        prog = m.compile_timeline(r, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))
        ups = [f for f in prog["frames"] if f["action"] in ("pointer_up", "up")]
        self.assertEqual((ups[0]["action"], ups[0]["index"], ups[0]["delay_ms"]),
                         ("pointer_up", 0, 1500))
        self.assertEqual([p["id"] for p in ups[0]["pointers"]], [0, 1])
        self.assertEqual((ups[1]["action"], ups[1]["index"], ups[1]["delay_ms"]),
                         ("up", 0, 2000))
        self.assertEqual([p["id"] for p in ups[1]["pointers"]], [1])
        # aim keeps its drag coords through the move lift
        self.assertAlmostEqual(ups[0]["pointers"][1]["x"], 0.9 * 1080, places=3)

    def test_three_staggered_groups(self):
        r = {"contacts": [
            {"slot": 0, "role": "move", "path": [[0.1, 0.8]], "hold": 100},
            {"slot": 1, "role": "aim", "path": [[0.8, 0.8]], "hold": 300},
            {"slot": 2, "role": "jump", "path": [[0.9, 0.9]], "hold": 200}]}
        prog = m.compile_timeline(r, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))
        ups = [f for f in prog["frames"] if f["action"] in ("pointer_up", "up")]
        self.assertEqual([(u["action"], u["index"], u["delay_ms"]) for u in ups],
                         [("pointer_up", 0, 100), ("pointer_up", 1, 100), ("up", 0, 100)])
        self.assertEqual([p["id"] for p in ups[-1]["pointers"]], [1])

    def test_noncontiguous_slots(self):
        r = {"contacts": [
            {"slot": 3, "role": "move", "path": [[0.1, 0.8], [0.2, 0.8]], "hold": 400},
            {"slot": 7, "role": "aim", "path": [[0.8, 0.8], [0.85, 0.7]], "hold": 400}]}
        prog = m.compile_timeline(r, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))
        self.assertEqual(m.declared_down_ids(prog), [3, 7])
        ups = [f for f in prog["frames"] if f["action"] in ("pointer_up", "up")]
        self.assertEqual([p["id"] for p in ups[0]["pointers"]], [3, 7])
        self.assertAlmostEqual(ups[0]["pointers"][1]["x"], 0.85 * 1080, places=3)
        self.assertEqual([p["id"] for p in ups[1]["pointers"]], [3])
        self.assertAlmostEqual(ups[1]["pointers"][0]["x"], 0.2 * 1080, places=3)

    def test_dual_legal(self):
        prog = m.compile_timeline(DUAL, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))
        self.assertEqual(m.roles_by_id(DUAL), {0: "move", 1: "aim"})
        self.assertEqual(m.declared_down_ids(prog), [0, 1])

    def test_jump_legal(self):
        prog = m.compile_timeline(JUMP, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))

    def test_coords_display_px(self):
        prog = m.compile_timeline(SINGLE, 1080, 2340, "t")
        down = prog["frames"][0]["pointers"][0]
        self.assertAlmostEqual(down["x"], 0.14 * 1080, places=3)
        self.assertAlmostEqual(down["y"], 0.87 * 2340, places=3)

    def test_rate_30hz(self):
        prog = m.compile_timeline(DUAL, 1080, 2340, "t")
        moves = [f for f in prog["frames"] if f["action"] == "move"]
        # move frames spaced >= 33ms (hold frame may be larger)
        for f in moves[:-1]:
            self.assertGreaterEqual(f["delay_ms"], 33)

    def test_three_contacts_legal(self):
        r = {"contacts": [
            {"slot": 0, "role": "move", "path": [[0.1, 0.8], [0.2, 0.8]], "hold": 500},
            {"slot": 1, "role": "aim", "path": [[0.8, 0.8], [0.8, 0.7]], "hold": 500},
            {"slot": 2, "role": "jump", "path": [[0.9, 0.9]], "hold": 200}]}
        prog = m.compile_timeline(r, 1080, 2340, "t")
        self.assertTrue(validate_program_mirror(prog["frames"]))
        self.assertEqual(m.declared_down_ids(prog), [0, 1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)