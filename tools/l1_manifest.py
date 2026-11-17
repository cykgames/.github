"""W1 L1 qualification-suite manifest (P1+P2 plan v4.17 W1 gate integration).

Scene classification, tested-source snapshot binding, producer-clean
attestation, and strict reuse validation. Pure functions over a repo directory
so the CLI integration tests can exercise them on scratch repos without any
GitHub side effect.

Contract (plan v4.17 W1 / luna C3 / sol confirmation blocker):
  * scene classification: tools/test_levels_l1_*.tscn -> suite "l1";
    every other test scene -> "default".
  * 002 publish defaults to an INLINE fresh l1 run (no reuse ambiguity).
  * optional --l1-manifest reuse must pass ALL of:
      (a) repo + exact HEAD + scene-set + all-PASS match;
      (b) producer-clean attestation (produced without --allow-dirty, git
          status clean except the recorded export_presets.cfg bump diff);
      (c) tested-source post-bump snapshot sha256 binding: the consumer
          recomputes the expected post-bump hash from HEAD + the fresh version
          (bump only changes export_presets.cfg versionName/versionCode, which
          is deterministic) and requires equality. A dirty producer that
          modified planner/test/game source, produced PASS, then reverted is
          rejected because the clean consumer's content hash differs.
  * stale / missing / wrong-HEAD / dirty-producer / content-mismatch ->
    ManifestError (release.py maps to ReleaseError).

Line endings: core.autocrlf may make the working tree CRLF while git blobs are
LF. Both producer (working-tree bytes) and consumer (HEAD blobs) normalize
CRLF -> LF before hashing so a clean producer matches regardless of autocrlf.
Author: cykbot9 (AI agent).
"""
import hashlib
import re
import subprocess
from pathlib import Path

SCHEMA = "cykgames-l1-manifest/1"
CFG = "export_presets.cfg"


class ManifestError(RuntimeError):
    pass


def _git(repo_dir, *args, timeout=120):
    p = subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def _git_bytes(repo_dir, *args, timeout=120):
    """Raw-byte git read (binary-safe). `git show` decoded as text would
    corrupt binary blobs (png/glb) via utf-8 replacement chars."""
    p = subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True,
                       timeout=timeout)
    return p.returncode, p.stdout or b"", p.stderr or b""


def _norm(b):
    return b.replace(b"\r\n", b"\n")


def scene_suite(scene_name):
    """test_levels_l1_*.tscn -> qualification suite 'l1'; else 'default'."""
    if scene_name.startswith("test_levels_l1_") and scene_name.endswith(".tscn"):
        return "l1"
    return "default"


def l1_scenes(repo_dir):
    tools = Path(repo_dir) / "tools"
    if not tools.exists():
        return []
    return sorted(p.name for p in tools.glob("test_levels_l1_*.tscn"))


def _tracked_files(repo_dir):
    rc, out, err = _git(repo_dir, "ls-files", "-z")
    if rc != 0:
        raise ManifestError(f"git ls-files failed (rc={rc}): {err[-200:]}")
    return [x for x in out.split("\0") if x]


def _accumulate(h, path, content):
    ch = hashlib.sha256(_norm(content)).hexdigest()
    h.update(path.encode("utf-8") + b"\0" + ch.encode("ascii") + b"\n")


def working_tree_hash(repo_dir):
    """Producer side: sha256 over ALL tracked files as they exist in the
    working tree (post-bump, as actually tested). Captures any source dirt."""
    files = _tracked_files(repo_dir)
    h = hashlib.sha256()
    for path in sorted(files):
        content = (Path(repo_dir) / path).read_bytes()
        _accumulate(h, path, content)
    return h.hexdigest()


def _bump_cfg_text(cfg_text, new_ver, new_code):
    cfg = re.sub(r'^version/name="[^"]+"', f'version/name="{new_ver}"',
                 cfg_text, count=1, flags=re.M)
    cfg = re.sub(r'^version/code=\d+', f"version/code={new_code}",
                 cfg, count=1, flags=re.M)
    return cfg


def head_post_bump_hash(repo_dir, new_ver, new_code):
    """Consumer side: recompute the expected post-bump snapshot from HEAD +
    the fresh version, independent of working-tree dirt. Every file comes from
    its HEAD blob; export_presets.cfg gets the deterministic bump applied."""
    files = _tracked_files(repo_dir)
    rc, cfg_raw, err = _git_bytes(repo_dir, "show", f"HEAD:{CFG}")
    if rc != 0:
        raise ManifestError(f"git show HEAD:{CFG} failed (rc={rc}): {err[-200:]}")
    cfg_text = cfg_raw.decode("utf-8")
    bumped = _bump_cfg_text(cfg_text, new_ver, new_code).encode("utf-8")
    h = hashlib.sha256()
    for path in sorted(files):
        if path == CFG:
            content = bumped
        else:
            rc, fb, err = _git_bytes(repo_dir, "show", f"HEAD:{path}")
            if rc != 0:
                raise ManifestError(f"git show HEAD:{path} failed (rc={rc}): {err[-200:]}")
            content = fb
        _accumulate(h, path, content)
    return h.hexdigest()


def produce_manifest(repo_dir, repo_name, head, version, version_code,
                     suite_results, allow_dirty=False):
    """Build a manifest from a completed l1 suite run on the post-bump tree.
    Records the producer-clean attestation (git status clean except the bump)."""
    rc, out, err = _git(repo_dir, "status", "--porcelain")
    if rc != 0:
        raise ManifestError(f"git status failed (rc={rc}): {err[-200:]}")
    dirty = [l for l in out.splitlines() if l.strip()]
    non_cfg = [l for l in dirty if CFG not in l]
    producer_clean = (not allow_dirty) and (len(non_cfg) == 0)
    results = [dict(r) for r in suite_results]
    return {
        "schema": SCHEMA,
        "repo": repo_name,
        "head": head,
        "version": version,
        "version_code": version_code,
        "source_sha256": working_tree_hash(repo_dir),
        "producer_clean": producer_clean,
        "producer_dirty": dirty,
        "allow_dirty": bool(allow_dirty),
        "suite": "l1",
        "scenes": [r["scene"] for r in results],
        "results": results,
        "all_pass": bool(results) and all(r.get("ok") and r.get("rc") == 0 for r in results),
    }


def validate_manifest(man, repo_dir, repo_name, new_ver, new_code, current_head):
    """Strict reuse validation. Raises ManifestError on any binding failure.
    Returns True only when every contract clause holds."""
    if not isinstance(man, dict) or man.get("schema") != SCHEMA:
        raise ManifestError("not a cykgames l1 manifest (bad/missing schema)")
    if man.get("repo") != repo_name:
        raise ManifestError(f"manifest repo {man.get('repo')!r} != {repo_name!r}")
    if man.get("head") != current_head:
        raise ManifestError(f"manifest HEAD {man.get('head')!r} != current "
                            f"{current_head!r} (wrong-HEAD / stale)")
    if man.get("version") != new_ver or man.get("version_code") != new_code:
        raise ManifestError(f"manifest version {man.get('version')}/"
                            f"{man.get('version_code')} != fresh bump "
                            f"{new_ver}/{new_code} (stale)")
    if not man.get("producer_clean") or man.get("allow_dirty"):
        raise ManifestError("manifest lacks producer-clean attestation "
                            "(dirty producer / --allow-dirty)")
    scenes = l1_scenes(repo_dir)
    if man.get("scenes") != scenes:
        raise ManifestError(f"manifest scene-set {man.get('scenes')} != "
                            f"current l1 scenes {scenes}")
    results = man.get("results") or []
    if not man.get("all_pass") or not results or not all(
            r.get("ok") and r.get("rc") == 0 for r in results):
        raise ManifestError("manifest results are not all PASS")
    expected = head_post_bump_hash(repo_dir, new_ver, new_code)
    if man.get("source_sha256") != expected:
        raise ManifestError("tested-source content mismatch: manifest "
                            f"{str(man.get('source_sha256'))[:12]} != expected "
                            f"post-bump {expected[:12]} (dirty-producer revert "
                            "or source drift)")
    return True