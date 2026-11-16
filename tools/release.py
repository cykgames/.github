#!/usr/bin/env python3
"""cykgames release harness v3 -- one-shot release pipeline, per-game isolation.

Pipeline per game:
    headless test gate -> bump (patch) -> export build from current HEAD
    -> install on device -> smoke -> tag + GitHub release -> catalog row update

Hard asserts:
  A1 foreground: the EXACT package parsed from the ResumedActivity line equals
     the game's package (no substring match; topResumedActivity is not
     confused with ResumedActivity)
  A2 orientation: project.godot -> APK manifest -> live screenshot all agree
  A3 gameplay reached + live: gameplay frame differs from the title frame and
     the world/HUD animates. PIXEL EVIDENCE ONLY -- proves liveness, NEVER a
     touch pass by itself.
  A4 crash-free: neither the crash buffer nor AndroidRuntime:E has a FATAL
  A5 publish integrity: GitHub release asset size == local APK size
  A6 touch state (when the game emits [CYKTEST] telemetry): the parsed logcat
     state series must satisfy the recipe's state_assert spec (run gate
     passed, HUD timer monotonic increase, stick output peak, player
     displacement, stick settles to zero after release). When [CYKTEST] is
     present the state assertions are MANDATORY and pixel metrics demote to
     warnings. Without the channel smoke records touch_pass=false plus a
     warning -- pixel liveness alone is never touch verification.

Version rule: version/code = major*10000 + minor*100 + patch (minor,patch<100).
Failure isolation: any exception (not just ReleaseError) fails THAT game only;
an uncommitted version bump is rolled back so the worktree stays clean for
the next bot; the run continues and the catalog is gated on zero failures.
--skip-build refuses to run unless the device's installed versionCode equals
the preset code (stale-APK false passes are impossible).

Usage:
  python tools/release.py 001-parkour 002-obby
  python tools/release.py --all --publish --catalog
  python tools/release.py 002-obby --skip-bump --skip-build   # verify installed
Flags:
  --all            every game in the roster (default: the repo given on argv,
                   or none)
  --skip-bump      do not touch export_presets.cfg (verify an existing build)
  --skip-build     reuse the APK in build/ AND require the device to already
                   run exactly that versionCode (never with publish)
  --skip-tests     skip the pre-build headless test gate (iteration only)
  --publish        git tag + push + GitHub release + asset verify
  --catalog        update profile/README.md rows, commit and push the org repo
  --report-dir     override the artifact root
Author: cykbot9 (AI agent). Lives in cykgames/.github/tools/.
"""
import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops

HOME = Path.home()
PROJECTS = HOME / "projects"
ORG_DIR = PROJECTS / "cykgames-org"
GODOT = HOME / "tools" / "godot-4.7.2" / "Godot_v4.7.2-stable_win64_console.exe"
ADB = Path(r"C:\Android\platform-tools\adb.exe")
AAPT2 = Path(r"C:\Android\build-tools\36.0.0\aapt2.exe")
GH_ORG = "cykgames"

# gameplay liveness thresholds
GAMEPLAY_DIFF_MIN = 0.004     # two frames 3.5s apart inside gameplay (world motion)
HUD_DIFF_MIN = 0.0002         # full-res top 6% strip (timer/score must tick)
MENU_EXIT_DIFF_MIN = 0.05     # gameplay frame vs title frame

# repo -> release metadata. apk path is relative to the repo root.
GAMES = {
    "001-parkour": dict(pkg="com.cykagent.parkour", apk="build/godot-parkour.apk",
                        title="Voxel Parkour", author="cykbot9", slug="001-parkour"),
    "002-obby": dict(pkg="com.cykagent.obby", apk="build/002-obby.apk",
                     title="Obby World", author="cykbot9", slug="002-obby"),
    "003-outpost": dict(pkg="com.cykagent.outpost", apk="build/003-outpost.apk",
                        title="Outpost Assault", author="cykbot12", slug="003-outpost"),
    "004-match3": dict(pkg="com.cykagent.match3", apk="build/match3.apk",
                       title="Match3", author="cykbot4", slug="004-match3"),
    "005-marble": dict(pkg="com.cykagent.marble", apk="build/marble.apk",
                       title="Marble Runner", author="cykbot4", slug="005-marble"),
    "006-tomb": dict(pkg="com.cykgames.tomb", apk="build/006-tomb.apk",
                     title="Tomb Explorer", author="cykbot7", slug="006-tomb"),
    "007-merge": dict(pkg="com.cykgames.fruitmerge", apk="build/007-merge.apk",
                      title="Fruit Merge", author="cykbot6", slug="007-merge"),
    "008-shmup": dict(pkg="com.cykagent.shmup", apk="build/shmup.apk",
                      title="Space Shooter", author="cykbot4", slug="008-shmup"),
    "009-shooter": dict(pkg="com.cykagent.shooter", apk="build/009-shooter.apk",
                        title="CykShooter", author="cykbot1", slug="009-shooter"),
}

# project.godot window/handheld/orientation value -> (android screenOrientation,
# live screen shape). Godot: 0 landscape, 1 portrait, 2 reverse landscape,
# 3 reverse portrait, 4 sensor landscape, 5 sensor portrait, 6 sensor.
ORIENT_MAP = {
    0: (0, "landscape"), 1: (1, "portrait"), 2: (8, "landscape"),
    3: (9, "portrait"), 4: (6, "landscape"), 5: (7, "portrait"), 6: (4, "any"),
}


class ReleaseError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# shell helpers
# --------------------------------------------------------------------------- #
def run(cmd, cwd=None, timeout=900, check=False):
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise ReleaseError(f"command failed ({p.returncode}): "
                           f"{' '.join(str(c) for c in cmd)}\n{out[-2000:]}")
    return p.returncode, out


def run_split(cmd, cwd=None, timeout=120):
    """Like run() but keeps stdout and stderr SEPARATE (review r14 I18:
    existence queries must not mistake a stderr warning for a ref)."""
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def log(msg):
    print(msg, flush=True)


class Device:
    def __init__(self):
        self.serial = self._pick()

    @staticmethod
    def _pick():
        _, out = run([ADB, "devices"], timeout=60)
        serials = [l.split()[0] for l in out.splitlines()[1:]
                   if l.strip() and "device" in l.split()[1:2]]
        if not serials:
            raise ReleaseError("no adb device online")
        # prefer the USB-attached A54 (its serial is the transport id)
        for s in serials:
            if ":" not in s:
                return s
        return serials[0]

    def adb(self, *args, timeout=180):
        _, out = run([ADB, "-s", self.serial, *args], timeout=timeout)
        return out

    def shot(self, path=None):
        p = subprocess.run([str(ADB), "-s", self.serial, "exec-out",
                            "screencap", "-p"], capture_output=True, timeout=180)
        img = Image.open(io.BytesIO(p.stdout)).convert("RGB")
        if path:
            img.save(path)
        return img


def diff_frac(a, b):
    sa = a.resize((270, 585)).convert("L")
    sb = b.resize((270, 585)).convert("L")
    hist = ImageChops.difference(sa, sb).histogram()
    total = sum(hist)
    mean = sum(i * n for i, n in enumerate(hist)) / total
    frac = sum(hist[21:]) / total
    return round(mean, 3), round(frac, 4)


# --------------------------------------------------------------------------- #
# version / build
# --------------------------------------------------------------------------- #
def read_version(repo):
    cfg = (PROJECTS / repo / "export_presets.cfg").read_text(encoding="utf-8", errors="replace")
    name = re.search(r'^version/name="([^"]+)"', cfg, re.M).group(1)
    code = int(re.search(r'^version/code=(\d+)', cfg, re.M).group(1))
    return name, code


def version_code(major, minor, patch):
    """Collision-free Android versionCode: major*10000 + minor*100 + patch.
    The old major*100+minor*10+patch collided (0.1.10 == 0.2.0 == 20)."""
    if not (0 <= minor < 100 and 0 <= patch < 100):
        raise ReleaseError(f"version {major}.{minor}.{patch} overflows the "
                           "versionCode formula (minor and patch must be < 100)")
    return major * 10000 + minor * 100 + patch


def bump_patch(repo):
    cfg_path = PROJECTS / repo / "export_presets.cfg"
    cfg = cfg_path.read_text(encoding="utf-8", errors="replace")
    name, code = read_version(repo)
    major, minor, patch = (int(x) for x in name.split("."))
    patch += 1
    new_name = f"{major}.{minor}.{patch}"
    new_code = version_code(major, minor, patch)
    cfg = re.sub(r'^version/name="[^"]+"', f'version/name="{new_name}"', cfg, count=1, flags=re.M)
    cfg = re.sub(r'^version/code=\d+', f"version/code={new_code}", cfg, count=1, flags=re.M)
    cfg_path.write_text(cfg, encoding="utf-8")
    # read back
    got = read_version(repo)
    if got != (new_name, new_code):
        raise ReleaseError(f"version bump did not stick: {got} != {(new_name, new_code)}")
    log(f"  bump {name}({code}) -> {new_name}({new_code})")
    return new_name, new_code


def git_head(repo):
    _, out = run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECTS / repo)
    return out.strip()


def git_dirty(repo):
    _, out = run(["git", "status", "--porcelain"], cwd=PROJECTS / repo)
    return [l for l in out.splitlines() if l.strip()]


def build_apk(repo):
    d = PROJECTS / repo
    apk = d / GAMES[repo]["apk"]
    apk.parent.mkdir(exist_ok=True)
    if apk.exists():
        apk.unlink()                      # stale APKs must never be published
    t0 = time.time()
    run([GODOT, "--headless", "--path", d, "--import"], timeout=600)
    rc, out = run([GODOT, "--headless", "--path", d, "--export-debug", "Android", apk],
                  timeout=900)
    if not apk.exists() or apk.stat().st_size < 1_000_000:
        raise ReleaseError(f"export produced no usable APK (rc={rc})\n{out[-1500:]}")
    size = apk.stat().st_size
    log(f"  build {size/1e6:.2f}MB in {time.time()-t0:.0f}s")
    return apk, size


def manifest_info(apk):
    rc, out = run([AAPT2, "dump", "xmltree", "--file", "AndroidManifest.xml", apk], timeout=180)
    if rc != 0:
        raise ReleaseError(f"aapt2 failed on {apk}\n{out[-800:]}")
    pkg = re.search(r'package="([^"]+)"', out)
    vname = re.search(r':versionName\(0x[0-9a-f]+\)="([^"]+)"', out)
    vcode = re.search(r':versionCode\(0x[0-9a-f]+\)=(\d+)', out)
    orient = re.search(r':screenOrientation\(0x[0-9a-f]+\)=(\d+)', out)
    return dict(package=pkg.group(1) if pkg else None,
                versionName=vname.group(1) if vname else None,
                versionCode=int(vcode.group(1)) if vcode else None,
                screenOrientation=int(orient.group(1)) if orient else None)


def project_orientation(repo):
    txt = (PROJECTS / repo / "project.godot").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^window/handheld/orientation=(\d+)', txt, re.M)
    return int(m.group(1)) if m else 0     # Godot default is landscape


def install(dev, apk, pkg, expect_code):
    out = dev.adb("install", "-r", apk, timeout=300)
    if "Success" not in out:
        raise ReleaseError(f"install failed: {out.strip()[-400:]}")
    info = dev.adb("shell", "dumpsys", "package", pkg)
    got = re.search(r"versionCode=(\d+)", info)
    got = int(got.group(1)) if got else -1
    if got != expect_code:
        raise ReleaseError(f"installed versionCode {got} != preset {expect_code}")
    log(f"  install ok (versionCode={got})")


def index_entry(repo):
    """Snapshot the FULL stage-0 index entry for export_presets.cfg:
    returns dict(mode, oid), or None when the file has no index entry
    (untracked, or staged deletion). Fail-CLOSED before any modification:
    a git read failure or an unmerged/multi-stage index raises, so rollback
    never has to guess the original state."""
    rc, out = run(["git", "ls-files", "-s", "export_presets.cfg"],
                  cwd=PROJECTS / repo)
    if rc != 0:
        raise ReleaseError(f"index snapshot failed (rc={rc}): {out.strip()[-200:]}")
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise ReleaseError("index has unmerged/multiple entries for "
                           "export_presets.cfg; resolve before release")
    parts = lines[0].split()
    mode, oid, stage = parts[0], parts[1], parts[2]
    if stage != "0":
        raise ReleaseError("index entry for export_presets.cfg is unmerged; "
                           "resolve before release")
    return dict(mode=mode, oid=oid)


def restore_index(repo, entry):
    """Restore the EXACT pre-bump index entry (existence + mode + oid).
    Returns (ok, detail). entry None means 'no entry originally': keep it
    absent (re-stage the deletion when the file is tracked in HEAD; do
    nothing when untracked). Never substitute a reset-to-HEAD."""
    d = PROJECTS / repo
    if entry is None:
        # Absence must be GUARANTEED, not inferred from HEAD: read the
        # current index, force-remove any entry (worktree untouched), and
        # read back. Any git read failure is a failure, never success.
        rcC, outC = run(["git", "ls-files", "-s", "export_presets.cfg"], cwd=d)
        if rcC != 0:
            return False, f"ls-files read failed rc={rcC}"
        if not outC.strip():
            return True, "absent-entry preserved"
        rc, out = run(["git", "update-index", "--force-remove",
                       "export_presets.cfg"], cwd=d)
        if rc != 0:
            return False, f"force-remove rc={rc}: {out.strip()[-150:]}"
        rc2, out2 = run(["git", "ls-files", "-s", "export_presets.cfg"], cwd=d)
        if rc2 != 0 or out2.strip():
            return False, f"absent read-back failed: {out2.strip()!r}"
        return True, "absent-entry restored (verified)"
    rc, out = run(["git", "update-index", "--cacheinfo",
                   f"{entry['mode']},{entry['oid']},export_presets.cfg"], cwd=d)
    if rc != 0:
        return False, f"update-index rc={rc}: {out.strip()[-150:]}"
    rc2, out2 = run(["git", "ls-files", "-s", "export_presets.cfg"], cwd=d)
    want = f"{entry['mode']} {entry['oid']} 0\texport_presets.cfg"
    if rc2 != 0 or out2.strip() != want:
        return False, f"read-back {out2.strip()!r} != {want!r}"
    return True, "entry restored (mode+oid verified)"


def installed_version(dev, pkg):
    """(versionCode, versionName) currently installed on the device; -1/None
    when the package is absent."""
    info = dev.adb("shell", "dumpsys", "package", pkg)
    code = re.search(r"versionCode=(\d+)", info)
    name = re.search(r"versionName=(\S+)", info)
    return (int(code.group(1)) if code else -1,
            name.group(1) if name else None)


# --------------------------------------------------------------------------- #
# smoke
# --------------------------------------------------------------------------- #
def recipes_path():
    return Path(__file__).with_name("smoke_recipes.json")


def load_recipes():
    p = recipes_path()
    if not p.exists():
        raise ReleaseError(f"missing {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def resumed_activity(dev):
    """The exact ResumedActivity line. Android 16 also prints
    topResumedActivity=... FIRST -- a substring scan grabs the wrong line."""
    out = dev.adb("shell", "dumpsys", "activity", "activities")
    lines = [l.strip() for l in out.splitlines()]
    for l in lines:
        if l.startswith("ResumedActivity:") or l.startswith("mResumedActivity"):
            return l
    for l in lines:                      # fallback for exotic dumpsys layouts
        if l.startswith("topResumedActivity="):
            return l
    return ""


def activity_package(line):
    """Exact package from an ActivityRecord line (u0 <pkg>/<activity>)."""
    m = re.search(r"u\d+\s+([A-Za-z_][\w.]*)/", line)
    return m.group(1) if m else None


def crash_log(dev):
    out = dev.adb("logcat", "-d", "-b", "crash")
    out += dev.adb("logcat", "-d", "-s", "AndroidRuntime:E")
    return out


def do_step(dev, step, w, h, shots, out_dir, label, timeline=None):
    if "tap" in step:
        fx, fy = step["tap"]
        dev.adb("shell", "input", "tap", str(int(fx * w)), str(int(fy * h)))
    elif "drag" in step:
        t0 = time.time()
        (x0, y0), (x1, y1) = step["drag"]
        x0, y0, x1, y1 = int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)
        steps = int(step.get("steps", 8))
        dev.adb("shell", "input", "motionevent", "DOWN", str(x0), str(y0))
        for i in range(1, steps + 1):
            dev.adb("shell", "input", "motionevent", "MOVE",
                    str(x0 + (x1 - x0) * i // steps), str(y0 + (y1 - y0) * i // steps))
            time.sleep(0.05)
        time.sleep(float(step.get("hold", 1.0)))
        dev.adb("shell", "input", "motionevent", "UP", str(x1), str(y1))
        if timeline is not None:
            timeline.setdefault("drags", []).append([t0, time.time()])
    elif "shot" in step:
        name = step["shot"]
        shots[name] = dev.shot(out_dir / f"{label}_{name}.png")
    if step.get("wait"):
        time.sleep(float(step["wait"]))


def _reject_constant(x):
    raise ValueError(f"non-finite JSON constant: {x}")


def _finite_num(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _valid_sample(d):
    """Schema gate for [CYKTEST] payloads. Missing/wrong-typed fields are
    malformed, NEVER silently coerced to success values (an unobserved stick
    is not an observed zero; running must be a real boolean; vectors must be
    fixed-dimension finite numbers; ts must be a finite epoch for timeline
    alignment)."""
    if not _finite_num(d.get("ts")):
        return False
    scene = d.get("scene")
    if scene not in ("play", "select"):
        return False
    if scene == "select":
        return True
    if not isinstance(d.get("running"), bool):
        return False
    t = d.get("time")
    if not _finite_num(t) or t < 0:
        return False
    st = d.get("stick")
    if not (isinstance(st, list) and len(st) == 2
            and all(_finite_num(v) for v in st)):
        return False
    # stick is a normalized control: components and magnitude in [-1,1].
    # CykTest snaps to 0.001, which can round the magnitude up to ~1.0013,
    # so allow 1% tolerance. An impossible value like 1e154 or 1.5 is
    # corrupt, not "very strong input".
    if any(abs(v) > 1.0 + 0.01 for v in st):
        return False
    if (st[0] ** 2 + st[1] ** 2) ** 0.5 > 1.0 + 0.01:
        return False
    pos = d.get("pos")
    if not (isinstance(pos, list) and len(pos) == 3
            and all(_finite_num(v) for v in pos)):
        return False
    return True


def collect_cyktest(dev):
    """Parse [CYKTEST] {json} telemetry from logcat. Marker-based, tag-agnostic
    (the Godot logcat tag is not stable across versions). Returns
    (samples, stats): stats counts marker lines and malformed payloads so a
    corrupt channel can never silently fall back to pixel-only. Payloads must
    pass _valid_sample; NaN/Infinity literals are rejected at parse time."""
    out = dev.adb("logcat", "-d", "-v", "epoch", timeout=120)
    samples = []
    marker_lines = 0
    malformed = 0
    for line in out.splitlines():
        i = line.find("[CYKTEST]")
        if i < 0:
            continue
        marker_lines += 1
        try:
            d = json.loads(line[i + len("[CYKTEST]"):].strip(),
                           parse_constant=_reject_constant)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if isinstance(d, dict) and _valid_sample(d):
            samples.append(d)
        else:
            malformed += 1
    return samples, dict(marker_lines=marker_lines, malformed=malformed)


def _stick_mag(s):
    st = s.get("stick")
    if not (isinstance(st, list) and len(st) == 2
            and all(_finite_num(v) for v in st)):
        raise ReleaseError(f"A6: sample lacks a valid stick vector: {s}")
    return (float(st[0]) ** 2 + float(st[1]) ** 2) ** 0.5


ALLOWED_SPEC_KEYS = {"require_running", "time_increase_min", "stick_peak_min",
                     "pos_delta_min", "stick_settle_max"}
# Minimum strength for a declared spec: ALL FIVE keys are mandatory -- run
# gate + monotonic timer + input reaching the control + displacement during
# active input + the release negative control. A weak spec (e.g. only
# require_running, or omitting pos_delta_min) must not earn touch_pass.
REQUIRED_SPEC_KEYS = {"require_running", "time_increase_min", "stick_peak_min",
                      "stick_settle_max", "pos_delta_min"}


def validate_spec_values(spec):
    """Key presence is not strength: the VALUES must be meaningful, or a
    spec like {require_running: false, time_increase_min: 0, stick_peak_min: 0,
    stick_settle_max: 1e300} would let a fully static series earn touch_pass.
    Stick output is normalized to [-1,1]; thresholds live in that domain."""
    if spec["require_running"] is not True:
        raise ReleaseError("A6: require_running must be true (the run gate "
                           "cannot be disabled)")
    t = spec["time_increase_min"]
    if not _finite_num(t) or t < 1.0:
        raise ReleaseError(f"A6: time_increase_min must be a finite number "
                           f">= 1.0s (got {t!r})")
    p = spec["stick_peak_min"]
    if not _finite_num(p) or not (0.1 <= p <= 1.0):
        raise ReleaseError(f"A6: stick_peak_min must be within [0.1, 1.0] "
                           f"(normalized stick domain, got {p!r})")
    s = spec["stick_settle_max"]
    if not _finite_num(s) or not (0.0 <= s < p):
        raise ReleaseError(f"A6: stick_settle_max must be finite, >= 0 and "
                           f"strictly below stick_peak_min ({p!r}); got {s!r}")
    d = spec["pos_delta_min"]
    if not _finite_num(d) or d < 0.1:
        raise ReleaseError(f"A6: pos_delta_min must be a finite number "
                           f">= 0.1 world units (got {d!r})")


def state_checks(samples, spec, drags=None, end_dev=None, unc=0.1, ts_max=None):
    """A6 assertions over the parsed [CYKTEST] series. Returns a list of
    {name, ok, detail}. Causal-chain requirements (no cross-phase stitching):
      - run gate passed; HUD timer monotonic DURING the run
      - stick input reached the control DURING the run, and (when the drag
        timeline is available) strictly INSIDE each injection window interior
        (clock uncertainty shrinks the window; tolerance alone never counts)
      - player displaced WHILE INPUT WAS ACTIVE (excursion across active-stick
        running samples; motion after release is not input evidence)
      - stick settles to zero on a FRESH sample after the last release, and
        telemetry is still alive at recipe end (negative control).
    Series sanity: timestamps must be non-decreasing and not in the future
    relative to the collection clock (ts_max) -- a forged 1e300 ts cannot
    fake freshness. drags/end_dev/ts_max are DEVICE epoch seconds; unc is the
    clock alignment uncertainty (seconds)."""
    play = [s for s in samples if s.get("scene") == "play"]
    running = [s for s in play if s.get("running")]
    ts_all = [float(s["ts"]) for s in samples]
    if any(b < a - 1e-9 for a, b in zip(ts_all, ts_all[1:])):
        raise ReleaseError("A6: telemetry timestamps out of order -- "
                           "corrupt series, refusing to assert")
    if ts_max is not None:
        if not math.isfinite(ts_max):
            raise ReleaseError("A6: non-finite collection ceiling -- "
                               "corrupt clock, refusing to assert")
        if any(t > ts_max for t in ts_all):
            raise ReleaseError(f"A6: telemetry timestamp in the future "
                               f"(max={max(ts_all):.2f} > collection ceiling "
                               f"{ts_max:.2f}) -- forged/corrupt clock")
    checks = []

    def add(name, ok, detail):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))

    add("run_gate", len(running) > 0,
        f"{len(running)}/{len(play)} play samples running")

    times = [float(s["time"]) for s in running]
    if len(times) >= 2:
        inc = times[-1] - times[0]
        mono = all(b >= a - 0.05 for a, b in zip(times, times[1:]))
        add("timer_monotonic_increase",
            mono and inc >= float(spec["time_increase_min"]),
            f"increase={inc:.2f}s monotonic={mono} samples={len(times)}")
    else:
        add("timer_monotonic_increase", False,
            f"only {len(times)} running samples")

    # stick is measured ONLY inside the run episode: input before the gate,
    # or motion while not running, is not gameplay evidence.
    mags = [_stick_mag(s) for s in running]
    peak = max(mags) if mags else 0.0
    active = sum(1 for m in mags if m >= float(spec["stick_peak_min"]))
    add("stick_input_reached",
        peak >= float(spec["stick_peak_min"]) and active >= 3,
        f"peak={peak:.3f} running_samples_above_threshold={active}")

    # displacement must happen INSIDE a continuous active-input segment,
    # and (when drags are known) inside the uncertainty-shrunk injection
    # window interior. Segmentation runs on the FULL ordered series: a
    # non-play sample, running=false, stick below peak, or a sampling gap
    # all flush the current segment -- motion during a neutral/non-running
    # gap can never be stitched into "input evidence".
    peak_min = float(spec["stick_peak_min"])
    segs, cur = [], []
    for s in samples:
        if s.get("scene") != "play":
            if cur:
                segs.append(cur)
                cur = []
            continue
        if s.get("running") is True and _stick_mag(s) >= peak_min:
            # epoch-scale float tolerance: at unix ~1.76e9 the ULP is ~2.4e-7,
            # so a legal 0.6s spacing can compute as 0.60000014 -- a fixed
            # 1e-9 tolerance would falsely break the segment.
            gap_tol = 0.6 + max(1e-9, 8.0 * math.ulp(float(s["ts"])))
            if cur and float(s["ts"]) - float(cur[-1]["ts"]) > gap_tol:
                segs.append(cur)
                cur = []
            cur.append(s)
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)

    def seg_excursion(seg):
        poss = [s["pos"] for s in seg]
        return max(sum((float(x) - float(y)) ** 2
                       for x, y in zip(poss[0], q)) ** 0.5 for q in poss)

    if drags:
        best = 0.0
        for (t0, t1) in drags:
            lo, hi = t0 + unc, t1 - unc
            for seg in segs:
                inside = [s for s in seg if lo <= float(s["ts"]) <= hi]
                if len(inside) >= 2:
                    best = max(best, seg_excursion(inside))
        add("player_displacement", best >= float(spec["pos_delta_min"]),
            f"max within-drag-interior active-segment excursion={best:.2f}")
    elif segs:
        qualifying = [seg for seg in segs if len(seg) >= 2]
        best = max((seg_excursion(seg) for seg in qualifying), default=0.0)
        add("player_displacement", best >= float(spec["pos_delta_min"]),
            f"max active-segment excursion={best:.2f}")
    else:
        add("player_displacement", False, "no active-input running segments")

    # per-drag coverage: active samples strictly INSIDE the window interior.
    # The uncertainty margin shrinks the window; samples in the tolerance
    # band alone can never satisfy coverage (no pre-input alibi).
    if drags:
        covered = 0
        for (t0, t1) in drags:
            lo, hi = t0 + unc, t1 - unc
            if hi <= lo:
                continue        # window too short to prove -- not covered
            win = [s for s in running if lo <= float(s["ts"]) <= hi]
            n_act = sum(1 for s in win
                        if _stick_mag(s) >= float(spec["stick_peak_min"]))
            if n_act >= 2:
                covered += 1
        add("drag_windows_covered", covered == len(drags),
            f"{covered}/{len(drags)} windows with >=2 active samples inside "
            f"the interior (unc={unc:.2f}s)")

    # settle: fresh sample after the last release (conservative: the true UP
    # may be as late as last_up + unc) + channel alive at the bounded end.
    if play:
        last = play[-1]
        m = _stick_mag(last)
        fresh = True
        fdetail = ""
        if drags:
            last_up = max(t1 for _, t1 in drags)
            fresh = float(last["ts"]) >= last_up + unc + 0.2
            fdetail = (f"last_ts={float(last['ts']):.2f} "
                       f"last_up={last_up:.2f} unc={unc:.2f}")
        if end_dev is not None:
            alive = float(last["ts"]) >= end_dev + unc - 1.0
            fresh = fresh and alive
            fdetail += f" recipe_end={end_dev:.2f}"
        add("stick_settles_after_release",
            m <= float(spec["stick_settle_max"]) and fresh,
            f"last |stick|={m:.3f} fresh={fresh} {fdetail}")
    else:
        add("stick_settles_after_release", False, "no play samples")
    return checks


def device_clock_sample(dev):
    """Bracketed device-clock read: host midpoint cancels symmetric ADB
    one-way latency; rtt bounds the alignment uncertainty. None when the
    clock is unreadable OR non-finite (a NaN clock would poison the ts
    ceiling) -- callers decide fail-closed."""
    h0 = time.time()
    out = dev.adb("shell", "date", "+%s.%N")
    h1 = time.time()
    try:
        d = float(out.strip())
    except (ValueError, TypeError):
        return None
    if not math.isfinite(d) or not math.isfinite(h1 - h0):
        return None
    return dict(dev=d, host=(h0 + h1) / 2.0, rtt=h1 - h0)


def smoke(dev, repo, recipe, out_dir):
    """Run the recipe and enforce A1..A4 (+A6 when [CYKTEST] is present).
    Returns an evidence dict; touch_pass is True ONLY with state assertions."""
    pkg = GAMES[repo]["pkg"]
    dev.adb("shell", "am", "force-stop", pkg)
    time.sleep(0.6)
    dev.adb("logcat", "-c")
    dev.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(float(recipe.get("launch_wait", 9)))

    evidence = {}
    # A1 foreground (exact package equality, never substring)
    resumed = resumed_activity(dev)
    evidence["resumed_activity"] = resumed
    evidence["resumed_package"] = activity_package(resumed)
    if evidence["resumed_package"] != pkg:
        raise ReleaseError(f"A1 foreground: resumed package "
                           f"'{evidence['resumed_package']}' != {pkg} "
                           f"(line='{resumed}')")

    title = dev.shot(out_dir / f"{repo}_00_title.png")
    evidence["title_size"] = list(title.size)

    # A2 orientation (live screen)
    want = ORIENT_MAP[project_orientation(repo)][1]
    w, h = title.size
    live = "portrait" if h > w else "landscape"
    evidence["live_orientation"] = live
    if want != "any" and want != live:
        raise ReleaseError(f"A2 orientation: preset wants {want}, live screen is {live}")

    shots = {}
    timeline = {}
    c0 = device_clock_sample(dev)
    for i, step in enumerate(recipe["steps"]):
        do_step(dev, step, w, h, shots, out_dir, f"{repo}_{i:02d}", timeline)
    c1 = device_clock_sample(dev)
    evidence["input_timeline"] = timeline
    for key in ("gp1", "gp2"):
        if key not in shots:
            raise ReleaseError(f"recipe for {repo} never captured {key}")

    # A1 again -- the game must still be the foreground app after input
    resumed2 = resumed_activity(dev)
    evidence["resumed_activity_after"] = resumed2
    evidence["resumed_package_after"] = activity_package(resumed2)
    if evidence["resumed_package_after"] != pkg:
        raise ReleaseError(f"A1 foreground after input: package "
                           f"'{evidence['resumed_package_after']}' != {pkg} "
                           f"(line='{resumed2}')")

    # A6 touch state FIRST: [CYKTEST] telemetry is authoritative when present.
    # Fail-CLOSED: corrupt telemetry, an empty/weak/unknown-key spec, or a
    # declared-but-missing channel all raise -- none of them may silently fall
    # back to pixel-only. Declaration is keyed on PRESENCE, not truthiness
    # ({} and null are declarations and must be rejected).
    samples, tstats = collect_cyktest(dev)
    declared = "state_assert" in recipe
    spec = recipe.get("state_assert")
    evidence["cyktest_samples"] = len(samples)
    evidence["cyktest_stats"] = tstats
    if tstats["malformed"]:
        raise ReleaseError(f"A6: {tstats['malformed']} malformed [CYKTEST] "
                           "line(s) -- corrupt telemetry, refusing pixel fallback")
    if declared:
        if not isinstance(spec, dict):
            raise ReleaseError("A6: state_assert must be a JSON object")
        unknown = set(spec) - ALLOWED_SPEC_KEYS
        if unknown:
            raise ReleaseError(f"A6: unknown state_assert keys {sorted(unknown)}")
        missing = REQUIRED_SPEC_KEYS - set(spec)
        if missing:
            raise ReleaseError(f"A6: state_assert below minimum strength, "
                               f"missing keys {sorted(missing)}")
        validate_spec_values(spec)
    if samples:
        if not declared:
            raise ReleaseError(f"A6: {repo} emits [CYKTEST] but the recipe "
                               "has no state_assert spec")
        # Align the host injection timeline with device telemetry timestamps.
        # Bracketed clock reads: offset uses the host midpoint (cancels
        # symmetric ADB latency); rtt/2 bounds the uncertainty used for the
        # drag-window interior and the timestamp sanity ceiling.
        if c0 is None or c1 is None:
            raise ReleaseError("A6: device clock unreadable -- cannot align "
                               "drag timeline / verify telemetry freshness")
        c2 = device_clock_sample(dev)      # clock at telemetry collection
        if c2 is None:
            raise ReleaseError("A6: device clock unreadable at collection")
        if max(c0["rtt"], c1["rtt"], c2["rtt"]) > 4.0:
            raise ReleaseError("A6: device clock query too slow (>4s RTT) -- "
                               "alignment untrustworthy")
        offset = ((c0["dev"] - c0["host"]) + (c1["dev"] - c1["host"])) / 2.0
        unc = max(c0["rtt"], c1["rtt"], c2["rtt"]) / 2.0 + 0.05
        drags_dev = [[a + offset, b + offset]
                     for a, b in timeline.get("drags", [])]
        ts_max = c2["dev"] + c2["rtt"] / 2.0 + 0.5
        evidence["clock_offset"] = round(offset, 3)
        evidence["clock_uncertainty"] = round(unc, 3)
        checks = state_checks(samples, spec, drags_dev, c1["dev"], unc, ts_max)
        if not checks:
            raise ReleaseError("A6: state_assert produced zero checks -- "
                               "refusing vacuous touch_pass")
        evidence["state_checks"] = checks
        bad = [c for c in checks if not c["ok"]]
        if bad:
            raise ReleaseError("A6 touch state failed: " + "; ".join(
                f"{c['name']} ({c['detail']})" for c in bad))
        evidence["touch_pass"] = True
        log(f"  A6 touch state PASS ({len(samples)} samples, "
            f"{len(checks)} checks)")
    else:
        if declared:
            raise ReleaseError(f"A6: {repo} recipe declares state_assert but "
                               "no [CYKTEST] telemetry found -- channel broken")
        evidence["touch_pass"] = False
        evidence["touch_warning"] = ("no [CYKTEST] telemetry: pixel liveness "
                                     "only, NOT a touch proof")
        log("  WARNING: no [CYKTEST] channel -- touch_pass=false "
            "(pixel liveness only)")

    # A3 gameplay reached + live. Pixel evidence is a LIVENESS gate, never a
    # touch pass. With a passing A6 state series, pixel problems demote to
    # warnings (state is authoritative); without the channel they stay hard.
    mean_gg, frac_gg = diff_frac(shots["gp1"], shots["gp2"])
    mean_gt, frac_gt = diff_frac(title, shots["gp2"])
    # HUD strip measured at FULL resolution: a single timer digit flip is only
    # ~0.1% of the strip, but a frozen HUD is exactly 0.0, so the signal is
    # clean even with a tiny threshold.
    hud_h = int(h * 0.06)
    a = shots["gp1"].crop((0, 0, w, hud_h)).convert("L")
    b = shots["gp2"].crop((0, 0, w, hud_h)).convert("L")
    hist = ImageChops.difference(a, b).histogram()
    hud_frac = round(sum(hist[21:]) / sum(hist), 6)
    menu_min = float(recipe.get("menu_diff_min", MENU_EXIT_DIFF_MIN))
    evidence.update(gp_diff_mean=mean_gg, gp_diff_frac=frac_gg,
                    menu_to_game_frac=frac_gt, hud_diff_frac=hud_frac,
                    gp_threshold=GAMEPLAY_DIFF_MIN, hud_threshold=HUD_DIFF_MIN,
                    menu_threshold=menu_min,
                    pixel_role="liveness-only (never a touch pass)")
    pixel_problems = []
    if frac_gt < menu_min:
        pixel_problems.append(f"frame barely differs from the title "
                              f"({frac_gt} < {menu_min})")
    if hud_frac < HUD_DIFF_MIN and frac_gg < GAMEPLAY_DIFF_MIN:
        pixel_problems.append(f"HUD strip frozen and world static "
                              f"(hud={hud_frac}, world={frac_gg})")
    if pixel_problems:
        if evidence["touch_pass"]:
            evidence["pixel_warnings"] = pixel_problems
            log("  WARNING: pixel liveness: " + "; ".join(pixel_problems)
                + " (A6 state passed, demoted to warning)")
        else:
            raise ReleaseError("A3 gameplay: " + "; ".join(pixel_problems)
                               + " -- never reached a live gameplay frame")

    # A4 crash free
    crashes = crash_log(dev)
    evidence["crash_buffer_clean"] = "FATAL EXCEPTION" not in crashes and "ANR in" not in crashes
    evidence["crash_tail"] = crashes.strip().splitlines()[-6:] if crashes.strip() else []
    if not evidence["crash_buffer_clean"]:
        raise ReleaseError(f"A4 crash: FATAL/ANR in buffers\n{crashes[-800:]}")
    dev.adb("shell", "am", "force-stop", pkg)
    evidence["ok"] = True
    return evidence


# --------------------------------------------------------------------------- #
# headless test gate (pre-build)
# --------------------------------------------------------------------------- #
# Scan rule: tools/test_*.tscn + tools/headless_test.tscn + tools/regression.tscn
# (005/008 name their harness headless_test, 003 names it regression -- the
# old "every repo has tools/test_*.gd" claim was false). Probes / shot tools /
# autoplay drivers are NOT tests and are excluded. All test scenes exit 0/1.
TEST_FLAGS = {
    ("008-shmup", "headless_test.tscn"): ["--fixed-fps", "60"],
    ("009-shooter", "test_touch_events.tscn"): ["--fixed-fps", "60"],
}


def headless_test_scenes(repo):
    tools = PROJECTS / repo / "tools"
    scenes = sorted(tools.glob("test_*.tscn"))
    for extra in ("headless_test.tscn", "regression.tscn"):
        p = tools / extra
        if p.exists() and p not in scenes:
            scenes.append(p)
    return scenes


def run_headless_tests(repo, out_dir):
    scenes = headless_test_scenes(repo)
    if not scenes:
        log(f"  WARNING: {repo} has no headless test scenes under tools/")
        return []
    run([GODOT, "--headless", "--path", PROJECTS / repo, "--import"], timeout=600)
    results = []
    for sc in scenes:
        flags = TEST_FLAGS.get((repo, sc.name), [])
        t0 = time.time()
        rc, out = run([GODOT, "--headless", *flags, "--path", PROJECTS / repo,
                       f"res://tools/{sc.name}"], timeout=420)
        (out_dir / f"headless_{sc.stem}.log").write_text(out, encoding="utf-8")
        dt = time.time() - t0
        results.append(dict(scene=sc.name, rc=rc, ok=(rc == 0),
                            seconds=round(dt, 1)))
        if rc != 0:
            raise ReleaseError(f"headless test {sc.name} failed (rc={rc}), "
                               f"refusing to build\n{out[-1200:]}")
        log(f"  headless {sc.name} PASS ({dt:.0f}s)")
    return results


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def gh_api(path, method="GET", payload=None, raw_body=None, content_type=None,
           timeout=300):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ReleaseError("GH_TOKEN not set")
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = None
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "cykgames-release-harness"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        headers["Content-Type"] = content_type or "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body and body[:1] in b"{}[]" else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def release_notes(repo, tag, prev_tag, head=None):
    rng = f"{prev_tag}..HEAD" if prev_tag else "HEAD"
    _, out = run(["git", "log", "--oneline", "--no-decorate", rng], cwd=PROJECTS / repo)
    lines = [l for l in out.splitlines() if l.strip()][:12]
    head = head or git_head(repo)
    body = [f"Built from `{head}` (current main), arm64, debug-signed.", ""]
    if lines:
        body.append("Changes since " + (f"`{prev_tag}`" if prev_tag else "the start") + ":")
        body.append("")
        body += [f"- {l}" for l in lines]
    return "\n".join(body)


def publish(dev, repo, tag, apk, size, out_dir, notes=None, rec=None,
            head_built=None):
    d = PROJECTS / repo
    # NEVER silently replace an existing public release: the tag and the
    # release must both be new. Replacing an existing release requires an
    # explicit manual workflow (remove it first), not a harness side effect.
    # review r14 (sol I17/I18, luna): existence queries must check rc (a failed
    # query is UNKNOWN, not "absent") and read stdout only (a stderr warning is
    # not a ref).
    rc, out, _err = run_split(["git", "tag", "-l", tag], cwd=d)
    if rc != 0:
        raise ReleaseError(f"cannot verify local tag {tag} (rc={rc}) -- refusing publish")
    if out.strip():
        raise ReleaseError(f"tag {tag} already exists locally -- refusing to "
                           "replace an existing release (explicit force "
                           "workflow required)")
    rc, out, _err = run_split(["git", "ls-remote", "--tags", "origin",
                               f"refs/tags/{tag}"], cwd=d, timeout=120)
    if rc != 0:
        raise ReleaseError(f"cannot verify remote tag {tag} (rc={rc}) -- refusing publish")
    want = f"refs/tags/{tag}"
    if any(ln.split("\t")[-1].strip() == want for ln in out.splitlines() if ln.strip()):
        raise ReleaseError(f"tag {tag} already exists on origin -- refusing to "
                           "replace an existing release (explicit force "
                           "workflow required)")
    status, rel = gh_api(f"/repos/{GH_ORG}/{repo}/releases/tags/{tag}")
    if status == 200:
        raise ReleaseError(f"release {tag} already exists -- refusing to "
                           "replace its assets (explicit force workflow required)")
    if status != 404:
        raise ReleaseError(f"release pre-check failed {status}: {rel}")
    # commit the version bump so the tagged tree matches the built APK
    run(["git", "add", "export_presets.cfg"], cwd=d, check=True)
    rc, out = run(["git", "commit", "-m", f"chore(release): {tag}"], cwd=d)
    if rc != 0 and "nothing to commit" not in out:
        raise ReleaseError(f"version commit failed: {out[-400:]}")
    if rec is not None:
        rec["version_committed"] = True   # past this point rollback must not
                                          # rewrite a committed file
    if head_built:
        _, out = run(["git", "diff", "--name-only", head_built, "HEAD"], cwd=d)
        changed = [l.strip() for l in out.splitlines() if l.strip()]
        if changed and changed != ["export_presets.cfg"]:
            raise ReleaseError("tagged tree differs from the built tree "
                               f"beyond the version bump: {changed}")
    # notes are written from the committed build state, not the parent
    run(["git", "fetch", "--tags", "--quiet"], cwd=d, timeout=300)
    if notes is None:
        _, tags = run(["git", "tag", "--sort=-creatordate"], cwd=d)
        prev = [t for t in tags.split() if t and t != tag][:1]
        notes = release_notes(repo, tag, prev[0] if prev else None, head=git_head(repo))
    # tag + push (tag must be new -- pre-checked above; no tolerance for
    # "already exists" here)
    rc, out = run(["git", "tag", "-a", tag, "-m", f"{GAMES[repo]['title']} {tag}"], cwd=d)
    if rc != 0:
        raise ReleaseError(f"git tag failed: {out[-400:]}")
    for ref in ("HEAD:main", tag):
        rc, out = run(["git", "push", "origin", ref], cwd=d, timeout=300)
        if rc != 0 and "up-to-date" not in out and "Everything up-to-date" not in out:
            raise ReleaseError(f"git push {ref} failed: {out[-500:]}")
    head = git_head(repo)

    status, rel = gh_api(f"/repos/{GH_ORG}/{repo}/releases/tags/{tag}")
    if status == 404:
        status, rel = gh_api(f"/repos/{GH_ORG}/{repo}/releases", method="POST",
                             payload={"tag_name": tag,
                                      "name": f"{tag} — {GAMES[repo]['title']}",
                                      "body": notes, "draft": False,
                                      "prerelease": False})
        if status not in (200, 201):
            raise ReleaseError(f"release create failed {status}: {rel}")
        rel = json.loads(rel) if isinstance(rel, (bytes, str)) else rel
    else:
        # 200 here means a release appeared after the pre-check (race):
        # refuse rather than touch its assets.
        raise ReleaseError(f"release {tag} appeared during publish (status "
                           f"{status}) -- refusing to replace assets")

    asset_name = f"{repo}-{tag}.apk"
    existing = [a for a in rel.get("assets", []) if a.get("name") == asset_name]
    if existing:
        raise ReleaseError(f"asset {asset_name} already exists on the new "
                           "release -- refusing to delete/replace it")
    payload = apk.read_bytes()
    up = (f"https://uploads.github.com/repos/{GH_ORG}/{repo}/releases/"
          f"{rel['id']}/assets?name={urllib.parse.quote(asset_name)}")
    status, asset = gh_api(up, method="POST", raw_body=payload,
                           content_type="application/vnd.android.package-archive")
    if status not in (200, 201):
        raise ReleaseError(f"asset upload failed {status}: {asset}")
    if isinstance(asset, (bytes, str)):
        asset = json.loads(asset)

    # A5 asset size == local APK
    status, rel2 = gh_api(f"/repos/{GH_ORG}/{repo}/releases/tags/{tag}")
    remote = {a["name"]: a for a in rel2.get("assets", [])}
    got = remote.get(asset_name, {}).get("size")
    if got != size:
        raise ReleaseError(f"A5 asset size {got} != local APK {size}")
    # Content-Length over the public download URL
    req = urllib.request.Request(remote[asset_name]["browser_download_url"], method="HEAD",
                                 headers={"User-Agent": "cykgames-release-harness"})
    with urllib.request.urlopen(req, timeout=180) as r:
        clen = int(r.headers.get("Content-Length", -1))
    if clen != size:
        raise ReleaseError(f"A5 download Content-Length {clen} != local APK {size}")

    # public repo?
    status, info = gh_api(f"/repos/{GH_ORG}/{repo}")
    if not isinstance(info, dict) or info.get("private"):
        raise ReleaseError("repo is not public")

    log(f"  release {tag} live ({size/1e6:.2f}MB, head {head})")
    return dict(tag=tag, url=rel2["html_url"], asset=asset_name, size=size,
                download=remote[asset_name]["browser_download_url"], head=head,
                public=True, content_length=clen)


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def catalog_update(results):
    """Rewrite the profile/README.md rows for the released games."""
    readme = ORG_DIR / "profile" / "README.md"
    text = readme.read_text(encoding="utf-8")
    changed = []
    for repo, r in results.items():
        pub = r.get("publish")
        if not pub:
            continue
        num = repo.split("-")[0]
        ver = pub["tag"]
        row_re = re.compile(rf"^\| {num} \|.*$", re.M)
        m = row_re.search(text)
        if not m:
            raise ReleaseError(f"catalog row for {repo} not found")
        old = m.group(0)
        # normalize whatever the row used before to the canonical [APK vX](url)
        new = re.sub(r"\[APK v[\d.]+\]\([^)]+\)",
                     f"[APK {ver}]({pub['download']})", old)
        if new == old:
            new = re.sub(r"\[v[\d.]+\]\([^)]+\)",
                         f"[APK {ver}]({pub['download']})", old)
        if new == old:
            raise ReleaseError(f"catalog row for {repo} has no release link to update")
        text = text.replace(old, new)
        changed.append(f"{repo}: {ver}")
    readme.write_text(text, encoding="utf-8")
    return changed


def catalog_commit_push(changed):
    d = ORG_DIR
    if not changed:
        return
    run(["git", "add", "profile/README.md"], cwd=d, check=True)
    msg = "catalog: release wave " + ", ".join(c.replace(":", " ") for c in changed)
    rc, out = run(["git", "commit", "-m", msg], cwd=d)
    if rc != 0 and "nothing to commit" not in out:
        raise ReleaseError(f"catalog commit failed: {out[-400:]}")
    rc, out = run(["git", "push", "origin", "HEAD:main"], cwd=d, timeout=300)
    if rc != 0:
        raise ReleaseError(f"catalog push failed: {out[-400:]}")
    log("  catalog pushed")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--skip-bump", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-tests", action="store_true",
                    help="skip the pre-build headless gate (iteration only)")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--catalog", action="store_true")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="build with uncommitted changes (iteration only, never with --publish)")
    ap.add_argument("--report-dir", default=None)
    args = ap.parse_args()

    repos = list(GAMES) if args.all else args.repos
    if not repos:
        ap.error("give at least one repo, or --all")
    for r in repos:
        if r not in GAMES:
            ap.error(f"unknown repo {r}")
    if args.publish and args.skip_build:
        ap.error("--publish never reuses a stale build")
    if args.publish and args.skip_tests:
        ap.error("--publish requires the headless test gate (no --skip-tests)")
    if args.publish and args.skip_bump:
        ap.error("--publish requires a fresh version bump (no --skip-bump); "
                 "replacing an existing public release needs an explicit "
                 "force workflow, not a silent asset swap")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(args.report_dir) if args.report_dir else (
        Path(os.environ.get("CYKGAMES_ARTIFACTS",
                            HOME / "cykagent9" / "workspace" / "artifacts")))
    out_root = root / f"release_verify_{stamp}"
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"== release harness v3  device/reports -> {out_root}")

    dev = Device()
    log(f"== device {dev.serial}")
    recipes = load_recipes()

    results, failures = {}, []
    for repo in repos:
        log(f"\n=== {repo}")
        rec = dict(repo=repo, pkg=GAMES[repo]["pkg"], started=datetime.now().isoformat(timespec="seconds"))
        out_dir = out_root / repo
        out_dir.mkdir(exist_ok=True)
        cfg_path = PROJECTS / repo / "export_presets.cfg"
        cfg_backup = None
        idx_backup = "unset"
        try:
            if not (PROJECTS / repo).exists():
                raise ReleaseError("repo missing on disk")
            if not args.skip_build:
                dirty = git_dirty(repo)
                if dirty and not args.allow_dirty:
                    raise ReleaseError(f"working tree not clean, refusing to build: {dirty[:5]}")
                if dirty and args.publish:
                    raise ReleaseError("refusing to publish from a dirty working tree")
            rec["head_before"] = git_head(repo)
            if args.skip_bump:
                ver, code = read_version(repo)
            else:
                cfg_backup = cfg_path.read_bytes()
                idx_backup = index_entry(repo)   # fail-closed BEFORE bump
                ver, code = bump_patch(repo)
            rec.update(version=ver, version_code=code)
            tag = f"v{ver}"

            # preset code formula (collision-free since the v3 migration)
            major, minor, patch = (int(x) for x in ver.split("."))
            want_code = version_code(major, minor, patch)
            if code != want_code:
                raise ReleaseError(f"versionCode {code} != "
                                   f"major*10000+minor*100+patch ({want_code})")

            if args.skip_build:
                apk = PROJECTS / repo / GAMES[repo]["apk"]
                if not apk.exists():
                    raise ReleaseError(f"skip-build: {apk} missing")
                size = apk.stat().st_size
                got_code, got_name = installed_version(dev, GAMES[repo]["pkg"])
                rec["installed_version"] = dict(code=got_code, name=got_name)
                if got_code != code:
                    raise ReleaseError(f"skip-build: device versionCode "
                                       f"{got_code} != preset {code} -- stale "
                                       "APK on device; install the matching "
                                       "build first (no touch pass on a "
                                       "version mismatch)")
            else:
                if not args.skip_tests:
                    rec["headless"] = run_headless_tests(repo, out_dir)
                apk, size = build_apk(repo)
            rec.update(apk=str(apk), apk_size=size, head_built=git_head(repo))

            man = manifest_info(apk)
            rec["manifest"] = man
            want_orient = ORIENT_MAP[project_orientation(repo)][0]
            if man["package"] != GAMES[repo]["pkg"]:
                raise ReleaseError(f"manifest package {man['package']} != {GAMES[repo]['pkg']}")
            if (man["versionName"], man["versionCode"]) != (ver, code):
                raise ReleaseError(f"manifest version {man['versionName']}/{man['versionCode']}"
                                   f" != preset {ver}/{code}")
            if man["screenOrientation"] != want_orient:
                raise ReleaseError(f"A2 orientation: manifest {man['screenOrientation']}"
                                   f" != project.godot-derived {want_orient}")

            if not args.skip_build:
                install(dev, apk, GAMES[repo]["pkg"], code)
            rec["smoke"] = smoke(dev, repo, recipes[repo], out_dir)

            if args.publish:
                if git_head(repo) != rec["head_built"]:
                    raise ReleaseError("HEAD moved since the build -- refusing "
                                       "to publish an APK that does not match "
                                       "the tagged tree")
                rec["publish"] = publish(dev, repo, tag, apk, size, out_dir,
                                         rec=rec, head_built=rec["head_built"])
            rec["ok"] = True
            log(f"  PASS {repo} v{ver}")
        except Exception as e:
            # Any exception fails THIS game only. Roll back an uncommitted
            # bump so the worktree stays clean for the next bot/run.
            if cfg_backup is not None and not rec.get("version_committed"):
                # Cleanup must never raise out of the per-game handler.
                # Index restore only runs when the snapshot SUCCEEDED
                # ("unset" = unknown original state -> leave index alone).
                if idx_backup != "unset":
                    try:
                        ok_i, det_i = restore_index(repo, idx_backup)
                        rec["rollback_index"] = det_i
                        if ok_i:
                            log(f"  rollback: index state restored ({det_i})")
                        else:
                            log(f"  rollback: index restore FAILED: {det_i}")
                    except Exception as re:
                        rec["rollback_index"] = f"{type(re).__name__}: {re}"
                        log(f"  rollback: index restore raised "
                            f"{type(re).__name__}: {re}")
                else:
                    log("  rollback: index snapshot unavailable -- index "
                        "left untouched (fail-closed)")
                try:
                    # raw bytes restore: read_text/write_text would normalize
                    # CRLF on Windows and make the read-back compare lie.
                    cfg_path.write_bytes(cfg_backup)
                    if cfg_path.read_bytes() != cfg_backup:
                        log("  rollback FAILED: read-back mismatch")
                    else:
                        log("  rollback: export_presets.cfg restored to pre-bump")
                except Exception as re:
                    log(f"  rollback FAILED: {type(re).__name__}: {re}")
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
            failures.append((repo, rec["error"]))
            log(f"  FAIL {repo}: {rec['error']}")
        results[repo] = rec
        (out_root / f"{repo}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    summary = dict(stamp=stamp, device=dev.serial, repos=repos,
                   results={k: {kk: v.get(kk) for kk in
                                ("ok", "version", "version_code", "apk_size", "error",
                                 "smoke", "publish")}
                            for k, v in results.items()},
                   failures=[{"repo": r, "error": e} for r, e in failures])
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.catalog and not failures:
        changed = catalog_update(results)
        catalog_commit_push(changed)
        log("  catalog: " + ("; ".join(changed) if changed else "no row changed"))

    log(f"\n== {len(results) - len(failures)}/{len(results)} passed, reports in {out_root}")
    if failures:
        for r, e in failures:
            log(f"  FAILED {r}: {e.splitlines()[0]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())