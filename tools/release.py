#!/usr/bin/env python3
"""cykgames release harness v2 -- one-shot, fail-fast release pipeline.

Pipeline per game:
    bump (patch) -> export build from current HEAD -> install on device
    -> smoke (5 hard asserts) -> tag + GitHub release -> catalog row update

Hard asserts (any failure aborts the game AND the run):
  A1 foreground: the resumed activity owns the game's package
  A2 orientation: project.godot -> APK manifest -> live screenshot all agree
  A3 gameplay liveness: two screenshots inside gameplay differ (HUD timer /
     score / world actually animating) AND the gameplay frame is not the menu
  A4 crash-free: neither the crash buffer nor AndroidRuntime:E has a FATAL
  A5 publish integrity: GitHub release asset size == local APK size

Usage:
  python tools/release.py 001-parkour 002-obby
  python tools/release.py --all --publish
  python tools/release.py --all --publish --catalog
Flags:
  --all            every game in the roster (default: the repo given on argv,
                   or none)
  --skip-bump      do not touch export_presets.cfg (verify an existing build)
  --skip-build     reuse the APK that is already in build/ (never with publish)
  --publish        git tag + push + GitHub release + asset verify
  --catalog        update profile/README.md rows, commit and push the org repo
  --report-dir     override the artifact root
Author: cykbot9 (AI agent). Lives in cykgames/.github/tools/.
"""
import argparse
import io
import json
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


def bump_patch(repo):
    cfg_path = PROJECTS / repo / "export_presets.cfg"
    cfg = cfg_path.read_text(encoding="utf-8", errors="replace")
    name, code = read_version(repo)
    major, minor, patch = (int(x) for x in name.split("."))
    patch += 1
    new_name = f"{major}.{minor}.{patch}"
    new_code = major * 100 + minor * 10 + patch
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
    out = dev.adb("shell", "dumpsys", "activity", "activities")
    for line in out.splitlines():
        if "ResumedActivity" in line:
            return line.strip()
    return ""


def crash_log(dev):
    out = dev.adb("logcat", "-d", "-b", "crash")
    out += dev.adb("logcat", "-d", "-s", "AndroidRuntime:E")
    return out


def do_step(dev, step, w, h, shots, out_dir, label):
    if "tap" in step:
        fx, fy = step["tap"]
        dev.adb("shell", "input", "tap", str(int(fx * w)), str(int(fy * h)))
    elif "drag" in step:
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
    elif "shot" in step:
        name = step["shot"]
        shots[name] = dev.shot(out_dir / f"{label}_{name}.png")
    if step.get("wait"):
        time.sleep(float(step["wait"]))


def smoke(dev, repo, recipe, out_dir):
    """Run the recipe and enforce A1..A4. Returns an evidence dict."""
    pkg = GAMES[repo]["pkg"]
    dev.adb("shell", "am", "force-stop", pkg)
    time.sleep(0.6)
    dev.adb("logcat", "-c")
    dev.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(float(recipe.get("launch_wait", 9)))

    evidence = {}
    # A1 foreground
    resumed = resumed_activity(dev)
    evidence["resumed_activity"] = resumed
    if pkg not in resumed:
        raise ReleaseError(f"A1 foreground: resumed='{resumed}' does not own {pkg}")

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
    for i, step in enumerate(recipe["steps"]):
        do_step(dev, step, w, h, shots, out_dir, f"{repo}_{i:02d}")
    for key in ("gp1", "gp2"):
        if key not in shots:
            raise ReleaseError(f"recipe for {repo} never captured {key}")

    # A1 again -- the game must still be the foreground app after input
    resumed2 = resumed_activity(dev)
    evidence["resumed_activity_after"] = resumed2
    if pkg not in resumed2:
        raise ReleaseError(f"A1 foreground after input: '{resumed2}'")

    # A3 gameplay liveness
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
                    menu_threshold=menu_min)
    if frac_gt < menu_min:
        raise ReleaseError(f"A3 gameplay: frame barely differs from the title "
                           f"({frac_gt} < {menu_min}) -- never reached gameplay")
    if hud_frac < HUD_DIFF_MIN and frac_gg < GAMEPLAY_DIFF_MIN:
        raise ReleaseError("A3 gameplay: HUD strip frozen and the world static "
                           f"(hud={hud_frac} < {HUD_DIFF_MIN}, world={frac_gg} < "
                           f"{GAMEPLAY_DIFF_MIN}) -- the run is not live")

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


def publish(dev, repo, tag, apk, size, out_dir, notes=None):
    d = PROJECTS / repo
    # commit the version bump so the tagged tree matches the built APK
    run(["git", "add", "export_presets.cfg"], cwd=d, check=True)
    rc, out = run(["git", "commit", "-m", f"chore(release): {tag}"], cwd=d)
    if rc != 0 and "nothing to commit" not in out:
        raise ReleaseError(f"version commit failed: {out[-400:]}")
    # notes are written from the committed build state, not the parent
    run(["git", "fetch", "--tags", "--quiet"], cwd=d, timeout=300)
    if notes is None:
        _, tags = run(["git", "tag", "--sort=-creatordate"], cwd=d)
        prev = [t for t in tags.split() if t and t != tag][:1]
        notes = release_notes(repo, tag, prev[0] if prev else None, head=git_head(repo))
    # tag + push
    rc, out = run(["git", "tag", "-a", tag, "-m", f"{GAMES[repo]['title']} {tag}"], cwd=d)
    if rc != 0 and "already exists" not in out:
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
    elif status != 200:
        raise ReleaseError(f"release lookup failed {status}: {rel}")

    asset_name = f"{repo}-{tag}.apk"
    existing = [a for a in rel.get("assets", []) if a.get("name") == asset_name]
    for a in existing:
        gh_api(f"/repos/{GH_ORG}/{repo}/releases/assets/{a['id']}", method="DELETE")
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

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(args.report_dir) if args.report_dir else (
        Path(os.environ.get("CYKGAMES_ARTIFACTS",
                            HOME / "cykagent9" / "workspace" / "artifacts")))
    out_root = root / f"release_verify_{stamp}"
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"== release harness v2  device/reports -> {out_root}")

    dev = Device()
    log(f"== device {dev.serial}")
    recipes = load_recipes()

    results, failures = {}, []
    for repo in repos:
        log(f"\n=== {repo}")
        rec = dict(repo=repo, pkg=GAMES[repo]["pkg"], started=datetime.now().isoformat(timespec="seconds"))
        out_dir = out_root / repo
        out_dir.mkdir(exist_ok=True)
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
                ver, code = bump_patch(repo)
            rec.update(version=ver, version_code=code)
            tag = f"v{ver}"

            # preset code formula
            major, minor, patch = (int(x) for x in ver.split("."))
            want_code = major * 100 + minor * 10 + patch
            if code != want_code:
                raise ReleaseError(f"versionCode {code} != major*100+minor*10+patch ({want_code})")

            if args.skip_build:
                apk = PROJECTS / repo / GAMES[repo]["apk"]
                size = apk.stat().st_size
            else:
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
                rec["publish"] = publish(dev, repo, tag, apk, size, out_dir)
            rec["ok"] = True
            log(f"  PASS {repo} v{ver}")
        except ReleaseError as e:
            rec["ok"] = False
            rec["error"] = str(e)
            failures.append((repo, str(e)))
            log(f"  FAIL {repo}: {e}")
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