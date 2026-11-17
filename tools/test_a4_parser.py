#!/usr/bin/env python3
"""W4 A4 parser fixtures (plan v4.2 §W4). Pure-parser + collector integrity.

Run: python tools/test_a4_parser.py   (from cykgames-org repo root)
Covers: target/foreign FATAL, interleaved, similar-prefix, target/foreign
SCRIPT ERROR, PID generations (restart), ANR target/foreign/similar-prefix,
chatty overflow, malformed target marker, unrelated garbage ignored,
unknown-pid conservative attribution, unreadable buffer (collector).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

PKG = "com.cykgames.obby"
PIDS = [100]


def tt(pid, tag, msg, level="E", tid=None):
    tid = tid or pid
    return f"11-17 12:00:00.000  {pid}  {tid} {level} {tag}: {msg}"


class ParseCrashLog(unittest.TestCase):
    def parse(self, text, pkg=PKG, pids=PIDS):
        return release.parse_crash_log(text, pkg, pids)

    def test_empty_clean(self):
        r = self.parse("--------- beginning of main\n")
        self.assertTrue(r["clean"])
        self.assertEqual(r["fatal"], [])

    def test_target_fatal(self):
        text = "\n".join([
            tt(100, "AndroidRuntime", "FATAL EXCEPTION: main"),
            tt(100, "AndroidRuntime", f"Process: {PKG}, PID: 100"),
            tt(100, "AndroidRuntime", "java.lang.RuntimeException: boom"),
        ])
        r = self.parse(text)
        self.assertFalse(r["clean"])
        self.assertEqual(len(r["fatal"]), 1)

    def test_foreign_fatal_ignored(self):
        text = "\n".join([
            tt(200, "AndroidRuntime", "FATAL EXCEPTION: main"),
            tt(200, "AndroidRuntime", "Process: com.other.app, PID: 200"),
        ])
        r = self.parse(text)
        self.assertTrue(r["clean"])
        self.assertEqual(r["foreign"]["fatal"], 1)

    def test_interleaved_fatal_attribution(self):
        text = "\n".join([
            tt(200, "AndroidRuntime", "FATAL EXCEPTION: main"),
            tt(100, "AndroidRuntime", "FATAL EXCEPTION: main"),
            tt(200, "AndroidRuntime", "Process: com.other.app, PID: 200"),
            tt(100, "AndroidRuntime", f"Process: {PKG}, PID: 100"),
        ])
        r = self.parse(text)
        self.assertFalse(r["clean"])
        self.assertEqual(len(r["fatal"]), 1)
        self.assertEqual(r["fatal"][0]["pid"], 100)
        self.assertEqual(r["foreign"]["fatal"], 1)

    def test_similar_prefix_process_not_convicted(self):
        text = "\n".join([
            tt(300, "AndroidRuntime", "FATAL EXCEPTION: main"),
            tt(300, "AndroidRuntime", f"Process: {PKG}2, PID: 300"),
        ])
        r = self.parse(text)
        self.assertTrue(r["clean"])
        self.assertEqual(r["foreign"]["fatal"], 1)

    def test_target_script_error(self):
        r = self.parse(tt(100, "godot", "SCRIPT ERROR: Invalid call. Nonexistent function 'foo' in base 'Nil'."))
        self.assertFalse(r["clean"])
        self.assertEqual(len(r["script_errors"]), 1)

    def test_foreign_godot_script_error_ignored(self):
        r = self.parse(tt(400, "godot", "SCRIPT ERROR: something else"))
        self.assertTrue(r["clean"])
        self.assertEqual(r["foreign"]["script_errors"], 1)

    def test_pid_generations_restart(self):
        # old pid 100 died, new pid 500; both generations attributed
        r = self.parse(tt(500, "godot", "SCRIPT ERROR: after restart"),
                       pids=[100, 500])
        self.assertFalse(r["clean"])
        self.assertEqual(r["script_errors"][0]["pid"], 500)

    def test_anr_target(self):
        # ActivityManager pid is system_server (not in target pids);
        # attribution is by exact package token, not pid.
        r = self.parse(tt(999, "ActivityManager", f"ANR in {PKG} (com.cykgames.obby/.GodotApp)", level="I"))
        self.assertFalse(r["clean"])
        self.assertEqual(len(r["anr"]), 1)

    def test_anr_foreign_ignored(self):
        r = self.parse(tt(999, "ActivityManager", "ANR in com.other.app", level="I"))
        self.assertTrue(r["clean"])
        self.assertEqual(r["foreign"]["anr"], 1)

    def test_anr_similar_prefix_ignored(self):
        r = self.parse(tt(999, "ActivityManager", f"ANR in {PKG}2", level="I"))
        self.assertTrue(r["clean"])

    def test_chatty_overflow_fails(self):
        text = "11-17 12:00:00.000   999   999 I chatty  : uid=1000 system_server expire 12 lines"
        r = self.parse(text)
        self.assertFalse(r["clean"])
        self.assertTrue(r["overflow"])

    def test_malformed_target_marker_fails(self):
        r = self.parse("SCRIPT ERROR: broken line without threadtime prefix")
        self.assertFalse(r["clean"])
        self.assertEqual(len(r["malformed_target"]), 1)

    def test_unrelated_garbage_ignored(self):
        r = self.parse("some random malformed main-buffer noise\n" + tt(100, "godot", "all good", level="I"))
        self.assertTrue(r["clean"])

    def test_unknown_pid_conservative(self):
        r = self.parse(tt(777, "godot", "SCRIPT ERROR: who am i"), pids=[])
        self.assertFalse(r["clean"])
        self.assertTrue(r["pid_unknown_conservative"])
        self.assertEqual(len(r["script_errors"]), 1)

    def test_tag_with_spaces_and_colons_parses(self):
        line = "11-17 12:00:00.000  100  100 E weird tag [x]: SCRIPT ERROR: inside"
        r = self.parse(line)
        self.assertFalse(r["clean"])
        self.assertEqual(r["script_errors"][0]["pid"], 100)


class CollectorIntegrity(unittest.TestCase):
    def test_unreadable_buffer_fails(self):
        dev = mock.Mock(serial="TEST")
        with mock.patch.object(release, "run_split", return_value=(1, "", "device offline")):
            r = release.crash_scan(dev, PKG, PIDS)
        self.assertFalse(r["clean"])
        self.assertEqual(r["read_integrity"], "failed")

    def test_system_buffer_fallback_recorded(self):
        dev = mock.Mock(serial="TEST")
        clean = "--------- beginning of main\n"
        with mock.patch.object(release, "run_split", side_effect=[
                (1, "Unable to open system buffer", ""), (0, clean, "")]):
            r = release.crash_scan(dev, PKG, PIDS)
        self.assertTrue(r["clean"])
        self.assertIn("fallback", r["buffers"])

    def test_overflow_read_integrity(self):
        dev = mock.Mock(serial="TEST")
        text = "11-17 12:00:00.000   999   999 I chatty  : expire 5 lines"
        with mock.patch.object(release, "run_split", return_value=(0, text, "")):
            r = release.crash_scan(dev, PKG, PIDS)
        self.assertFalse(r["clean"])
        self.assertEqual(r["read_integrity"], "overflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)