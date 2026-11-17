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