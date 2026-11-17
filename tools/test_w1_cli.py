"""W1 CLI integration tests (P1+P2 plan v4.17 W1 gate integration).

Covers the executable release contract without any GitHub/device side effect:
  * scene classification + default/l1/all suite selection
  * l1 manifest strict reuse: clean acceptance, missing/stale/wrong-HEAD/
    dirty-producer/content-mismatch/scene-set/not-all-pass rejection
  * the sol confirmation blocker: dirty producer modifies planner, produces
    PASS, reverts -> clean consumer publish rejected by content-hash binding
  * producer/consumer hash equality on the REAL 002 repo (core.autocrlf=true:
    working tree CRLF vs HEAD blob LF must normalize to the same snapshot)
  * 002 publish without l1 evidence -> ReleaseError (require_l1_evidence)
  * one real inline l1 suite run on 002 (end-to-end rc=0)

Run: python tools/test_w1_cli.py > out.log 2>&1   (unittest writes to stderr)
Author: cykbot9 (AI agent).
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_manifest as m  # noqa: E402
import release as r  # noqa: E402

REPO_002 = r.PROJECTS / "002-obby"


def git(d, *a):
    p = subprocess.run(["git", *a], cwd=str(d), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {a} rc={p.returncode}: {p.stderr[-300:]}")
    return p.stdout.strip()


def make_scratch(root):
    d = root / "repo"
    (d / "tools").mkdir(parents=True)
    git(d, "init", "-q")
    git(d, "config", "user.email", "t@t")
    git(d, "config", "user.name", "t")
    git(d, "config", "core.autocrlf", "true")
    (d / "tools" / "test_levels_l1_s0.tscn").write_text("[gd_scene]\n", encoding="utf-8")
    (d / "tools" / "test_levels_l1_s1.tscn").write_text("[gd_scene]\n", encoding="utf-8")
    (d / "tools" / "test_levels_struct.tscn").write_text("[gd_scene]\n", encoding="utf-8")
    (d / "tools" / "planner.gd").write_text("# planner v1\n", encoding="utf-8")
    (d / "export_presets.cfg").write_text('version/name="0.1.0"\nversion/code=100\n',
                                          encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "init")
    return d


def bump_cfg(d, ver, code):
    p = d / "export_presets.cfg"
    txt = p.read_text(encoding="utf-8")
    p.write_text(m._bump_cfg_text(txt, ver, code), encoding="utf-8")


def fake_results(scenes):
    return [dict(scene=s, rc=0, ok=True, seconds=1.0, suite="l1") for s in scenes]


class ManifestContract(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="w1cli_"))
        self.d = make_scratch(self.root)
        self.head = git(self.d, "rev-parse", "--short", "HEAD")
        self.cfg_backup = (self.d / "export_presets.cfg").read_bytes()
        self.scenes = m.l1_scenes(self.d)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _produce(self, ver="0.1.1", code=101, results=None, allow_dirty=False):
        bump_cfg(self.d, ver, code)
        res = results if results is not None else fake_results(self.scenes)
        man = m.produce_manifest(self.d, "scratch", self.head, ver, code, res,
                                 allow_dirty=allow_dirty)
        (self.d / "export_presets.cfg").write_bytes(self.cfg_backup)
        return man

    def test_clean_reuse_accepted(self):
        man = self._produce()
        self.assertTrue(man["producer_clean"])
        self.assertTrue(m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head))

    def test_missing_manifest_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(None, self.d, "scratch", "0.1.1", 101, self.head)

    def test_bad_schema_rejected(self):
        man = self._produce()
        man["schema"] = "bogus"
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)

    def test_wrong_head_rejected(self):
        man = self._produce()
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head + "x")

    def test_stale_version_rejected(self):
        man = self._produce()
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.2", 102, self.head)

    def test_wrong_repo_rejected(self):
        man = self._produce()
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "other", "0.1.1", 101, self.head)

    def test_dirty_producer_attestation_rejected(self):
        (self.d / "tools" / "planner.gd").write_text("# DIRTY\n", encoding="utf-8")
        man = self._produce()  # producer_clean False (non-cfg dirt)
        (self.d / "tools" / "planner.gd").write_text("# planner v1\n", encoding="utf-8")
        self.assertFalse(man["producer_clean"])
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)

    def test_allow_dirty_rejected(self):
        man = self._produce(allow_dirty=True)
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)

    def test_dirty_producer_revert_content_mismatch(self):
        # sol confirmation blocker: dirty producer edits planner, produces a
        # PASS manifest (lying producer_clean=True), then reverts. The clean
        # consumer recomputes the post-bump hash from HEAD and must reject.
        (self.d / "tools" / "planner.gd").write_text("# planner v2 DIRTY\n", encoding="utf-8")
        man = self._produce()
        man["producer_clean"] = True  # simulate a forged attestation
        man["allow_dirty"] = False
        (self.d / "tools" / "planner.gd").write_text("# planner v1\n", encoding="utf-8")
        with self.assertRaises(m.ManifestError) as ctx:
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)
        self.assertIn("content mismatch", str(ctx.exception))

    def test_content_tamper_rejected(self):
        man = self._produce()
        man["source_sha256"] = "0" * 64
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)

    def test_scene_set_mismatch_rejected(self):
        man = self._produce()
        (self.d / "tools" / "test_levels_l1_s1.tscn").unlink()
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)

    def test_not_all_pass_rejected(self):
        res = fake_results(self.scenes)
        res[0]["rc"] = 1
        res[0]["ok"] = False
        man = self._produce(results=res)
        with self.assertRaises(m.ManifestError):
            m.validate_manifest(man, self.d, "scratch", "0.1.1", 101, self.head)


class SuiteSelection(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(m.scene_suite("test_levels_l1_s0.tscn"), "l1")
        self.assertEqual(m.scene_suite("test_levels_l1_neg_jump.tscn"), "l1")
        self.assertEqual(m.scene_suite("test_levels_struct.tscn"), "default")
        self.assertEqual(m.scene_suite("test_joystick.tscn"), "default")
        self.assertEqual(m.scene_suite("headless_test.tscn"), "default")

    def test_002_suite_filters(self):
        scenes = [sc.name for sc in r.headless_test_scenes("002-obby")]
        default = [s for s in scenes if m.scene_suite(s) == "default"]
        l1 = [s for s in scenes if m.scene_suite(s) == "l1"]
        self.assertTrue(all(not s.startswith("test_levels_l1_") for s in default))
        self.assertTrue(all(s.startswith("test_levels_l1_") for s in l1))
        self.assertEqual(sorted(default + l1), sorted(scenes))
        self.assertIn("test_levels_struct.tscn", default)
        self.assertIn("test_levels_clear_s0.tscn", default)
        self.assertIn("test_levels_l1_s0.tscn", l1)
        self.assertEqual(len(l1), 7)

    def test_002_fixed_fps_wildcard(self):
        flags = r.TEST_FLAGS.get(("002-obby", "test_levels_l1_s0.tscn")) or \
            r.TEST_FLAGS.get(("002-obby", "*"))
        self.assertEqual(flags, ["--fixed-fps", "60"])


class PublishGuard(unittest.TestCase):
    def test_002_missing_l1_evidence_fails(self):
        with self.assertRaises(r.ReleaseError):
            r.require_l1_evidence("002-obby", {})

    def test_002_with_l1_evidence_ok(self):
        r.require_l1_evidence("002-obby", {"l1": {"source": "inline"}})

    def test_repo_without_l1_scenes_ok(self):
        # 001-parkour has no test_levels_l1_* scenes -> no l1 requirement
        self.assertEqual(m.l1_scenes(r.PROJECTS / "001-parkour"), [])
        r.require_l1_evidence("001-parkour", {})


class Real002Hash(unittest.TestCase):
    def test_producer_consumer_hash_match_autocrlf(self):
        # On the real 002 repo (core.autocrlf=true), a clean producer's
        # working-tree post-bump hash must equal the consumer's HEAD+bump hash.
        head = r.git_head("002-obby")
        cfg = REPO_002 / "export_presets.cfg"
        backup = cfg.read_bytes()
        ver, code = r.read_version("002-obby")
        major, minor, patch = (int(x) for x in ver.split("."))
        new_ver = f"{major}.{minor}.{patch + 1}"
        new_code = r.version_code(major, minor, patch + 1)
        try:
            bump_cfg(REPO_002, new_ver, new_code)
            scenes = m.l1_scenes(REPO_002)
            man = m.produce_manifest(REPO_002, "002-obby", head, new_ver, new_code,
                                     fake_results(scenes))
            self.assertTrue(man["producer_clean"])
        finally:
            cfg.write_bytes(backup)
        # tree restored clean -> consumer recompute must match
        self.assertTrue(m.validate_manifest(man, REPO_002, "002-obby",
                                            new_ver, new_code, head))
        self.assertEqual(r.git_dirty("002-obby"), [])


class InlineL1Run(unittest.TestCase):
    def test_real_inline_l1_suite_pass(self):
        tmp = Path(tempfile.mkdtemp(prefix="w1l1_"))
        try:
            res = r.run_headless_tests("002-obby", tmp, suite="l1")
            self.assertEqual(len(res), 7)
            self.assertTrue(all(x["ok"] and x["rc"] == 0 for x in res))
            self.assertTrue(all(x["suite"] == "l1" for x in res))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)