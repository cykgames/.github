"""W2d closed-loop fight-mode navigation (P1+P2 plan v4.17, 009-shooter).

The 009 mt dual measurement requires running == fight_mode AND !dead AND
!paused; fight_mode is set by ArenaStarter.body_entered when the player
physically crosses an ArenaEntry strip. Open-loop recipe drags cannot
reliably traverse title -> room_start -> Stairs door -> room_0 -> arena
(door entry relocates the player; acceleration + axis multiplier make timed
drags fragile). This navigator closes the loop on the game's OWN [CYKTEST]
telemetry (pos/running/scene, uptime domain) and steers with REAL B' mt move
runs (single app_process timeline per iteration, mode=2, <=30Hz).

Contract:
  - Navigation runs are legacy-equivalent: they are NOT recorded into
    timeline mt_runs/mt_windows; release.py clears logcat before the measured
    mt run so navigation receipts never pollute the measurement window.
  - Fail-CLOSED: schema violations, missing telemetry/xform, a calibration
    tap that does not grab the stick, a non-rigid display offset, a leg that
    never settles/reaches its goal within budget -> NavError (never a silent
    fallback, never a pixel-only guess).
  - Coordinate mapping is derived at runtime, never hardcoded device pixels:
    design -> surface from the telemetry xform basis; surface -> physical
    display offset measured from the calibration run itself via
    mt_collect.derive_coord_offset (rigidity enforced).
  - Steering compensates the game's axis multiplier: world velocity =
    max_speed * (out.x * am.x, out.y * am.y), so the stick direction for a
    world delta d is normalize(d.x / am.x, d.y / am.y). knob_throw > KNOB_MAX
    pins the knob at the rim -> full-magnitude output (Joystick._steer).

Game constants live in the RECIPE (data), not here: stick_home (TouchControls
MoveStick offset_top 120 + home_center (66,82) = (66,202) design units),
axis_mult (axis_multiplication_resource.tres), waypoint world coordinates
(room scenes). This module is generic closed-loop machinery.
Author: cykbot9 (AI agent).
"""
import math
import time

import mt_collect
import mt_inject

NAV_KEYS = {"stick_home", "axis_mult", "legs", "budget_s", "hold_ms", "knob_throw"}
LEG_KEYS = {"to", "tol", "jump_advance", "jump_only", "settle_expect", "settle_tol", "running_goal"}
JUMP_DETECT = 80.0        # world units; a door transition teleports the player
SETTLE_STABLE = 3.0       # two reads <= this apart == settled
SETTLE_POLL = 0.35
PLAY_WAIT = 25.0
SETTLE_WAIT = 25.0
GOAL_GRACE = 4.0
MAX_ITERS = 60
KNOB_MAX = 26.0           # Joystick.gd KNOB_MAX (full output at/beyond rim)


class NavError(RuntimeError):
    pass


def _fin(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _vec2(v):
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_fin(x) for x in v)


def validate_nav(spec):
    if not isinstance(spec, dict):
        raise NavError("nav spec must be an object")
    unknown = set(spec) - NAV_KEYS
    if unknown:
        raise NavError(f"nav unknown keys {sorted(unknown)}")
    for k in ("stick_home", "axis_mult", "legs"):
        if k not in spec:
            raise NavError(f"nav missing key {k}")
    if not _vec2(spec["stick_home"]):
        raise NavError("nav stick_home must be [x,y] finite design units")
    am = spec["axis_mult"]
    if not _vec2(am) or am[0] <= 0 or am[1] <= 0:
        raise NavError("nav axis_mult must be [x,y] > 0")
    legs = spec["legs"]
    if not isinstance(legs, list) or not legs:
        raise NavError("nav legs must be a non-empty list")
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise NavError(f"nav leg {i} must be an object")
        extra = set(leg) - LEG_KEYS
        if extra:
            raise NavError(f"nav leg {i} unknown keys {sorted(extra)}")
        if "to" not in leg or not _vec2(leg["to"]):
            raise NavError(f"nav leg {i} to must be [x,y] finite world units")
        tol = leg.get("tol", 10.0)
        if not _fin(tol) or tol <= 0:
            raise NavError(f"nav leg {i} tol must be > 0")
        for bk in ("jump_advance", "jump_only", "running_goal"):
            if bk in leg and not isinstance(leg[bk], bool):
                raise NavError(f"nav leg {i} {bk} must be bool")
        if leg.get("jump_only") and not leg.get("jump_advance"):
            raise NavError(f"nav leg {i} jump_only requires jump_advance")
        if "settle_expect" in leg:
            if not _vec2(leg["settle_expect"]):
                raise NavError(f"nav leg {i} settle_expect must be [x,y]")
            st = leg.get("settle_tol", 40.0)
            if not _fin(st) or st <= 0:
                raise NavError(f"nav leg {i} settle_tol must be > 0")
        if leg.get("running_goal") and i != len(legs) - 1:
            raise NavError("nav running_goal only allowed on the final leg")
    budget = spec.get("budget_s", 90.0)
    if not _fin(budget) or budget <= 0:
        raise NavError("nav budget_s must be > 0")
    hold = spec.get("hold_ms", 700)
    if not _fin(hold) or not (100 <= hold <= 5000):
        raise NavError("nav hold_ms must be in [100,5000]")
    throw = spec.get("knob_throw", 35.0)
    if not _fin(throw) or throw <= KNOB_MAX:
        raise NavError(f"nav knob_throw must be > {KNOB_MAX} (rim-pin full output)")
    return True


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _design_to_physical(p, xf, off):
    """design -> surface via xform [ox,oy,xx,xy,yx,yy], then + display offset."""
    x = xf[2] * p[0] + xf[3] * p[1] + xf[0] + off[0]
    y = xf[4] * p[0] + xf[5] * p[1] + xf[1] + off[1]
    return x, y


def _frac(x, y, w, h):
    return [min(max(x / w, 0.002), 0.998), min(max(y / h, 0.002), 0.998)]


def _parse(dev):
    text = dev.adb("logcat", "-d", "-v", "threadtime", timeout=120)
    return mt_collect.parse_logcat(text)


def _latest_play(parsed):
    best = None
    for s in parsed["samples"]:
        if s.get("scene") != "play":
            continue
        if not _fin(s.get("up")):
            continue
        pos = s.get("pos")
        if not (isinstance(pos, list) and len(pos) >= 2
                and all(_fin(v) for v in pos[:2])):
            continue
        if best is None or s["up"] > best["up"]:
            best = s
    return best


def _wait_play(dev, deadline):
    while time.monotonic() < deadline:
        s = _latest_play(_parse(dev))
        if s is not None:
            return s
        time.sleep(0.4)
    raise NavError("no [CYKTEST] play-scene sample (title entry failed or "
                   "telemetry channel broken)")


def _merged_input_route(parsed):
    merged, _sids = mt_collect.merge_rings(parsed["ring_lines"])
    inp = sorted(merged.get("input_log", {}).values(), key=lambda e: e["s"])
    route = sorted(merged.get("route_log", {}).values(), key=lambda e: e["s"])
    return inp, route


def _steer_recipe(home, am, target, pos, xf, off, w, h, throw, hold):
    dx, dy = target[0] - pos[0], target[1] - pos[1]
    # compensate axis multiplier so world motion points at the target
    sx, sy = dx / am[0], dy / am[1]
    n = math.hypot(sx, sy)
    if n < 1e-9:
        sx, sy, n = 1.0, 0.0, 1.0
    design = [home[0] + sx / n * throw, home[1] + sy / n * throw]
    px, py = _design_to_physical(design, xf, off)
    fx, fy = _frac(px, py, w, h)
    return {"contacts": [{"slot": 0, "role": "move",
                          "path": [[fx, fy]], "hold": hold}]}


def navigate(dev, inj, spec, w, h, out_dir=None, label="nav", log=None):
    """Closed-loop navigation. Returns an evidence dict; raises NavError on
    any failure (fail-closed). inj is an mt_inject.MtInjector bound to the
    live screen size."""
    log = log or (lambda *a: None)
    validate_nav(spec)
    home = spec["stick_home"]
    am = spec["axis_mult"]
    hold = spec.get("hold_ms", 700)
    throw = spec.get("knob_throw", 35.0)
    budget = spec.get("budget_s", 90.0)
    t0 = time.monotonic()

    def remaining():
        return budget - (time.monotonic() - t0)

    def check_budget(where):
        if remaining() <= 0:
            raise NavError(f"budget {budget}s exhausted at {where}")

    # isolate the navigation window (title-entry receipts are legacy; the
    # measured mt run gets its own logcat -c in release.py afterwards)
    dev.adb("logcat", "-c")
    s = _wait_play(dev, t0 + PLAY_WAIT)
    xf = s.get("xform")
    if not (isinstance(xf, list) and len(xf) == 6 and all(_fin(v) for v in xf)
            and xf[2] > 0 and xf[5] > 0):
        raise NavError(f"telemetry xform missing/invalid: {xf!r} (required for "
                       "the design->display mapping)")
    log(f"  nav: play scene at pos={s['pos'][:2]} xform={xf}")

    # ---- calibration: measure the surface->display offset from a real tap --
    # Approximate mapping (offset 0) still lands inside the move stick zone
    # (the inset error is far below the 60-unit grab radius), so the tap grabs
    # the stick; its atom->receipt correspondence yields the exact rigid
    # offset. The brief off-center press moves the player a few units at most;
    # closed-loop steering corrects it.
    px, py = _design_to_physical(home, xf, (0.0, 0.0))
    fx, fy = _frac(px, py, w, h)
    cal = {"contacts": [{"slot": 0, "role": "move", "path": [[fx, fy]], "hold": 90}]}
    cal_id = "nav-cal-%s-%d" % (label, int(time.time() * 1000))
    res = inj.run(cal, run_id=cal_id)
    if not res.get("ok"):
        raise NavError(f"calibration injection failed: {res.get('error')} "
                       f"(release_error={res.get('release_error')})")
    trace = res["mtresult"]["trace"]
    parsed = _parse(dev)
    try:
        inp, route = _merged_input_route(parsed)
        atoms = mt_collect.expand_atoms(trace)
        cand, _emu, _clean = mt_collect.classify_receipts(inp, trace)
        ox, oy, n_off, spread = mt_collect.derive_coord_offset(atoms, cand)
    except mt_collect.CollectError as e:
        raise NavError(f"calibration transform: {e}")
    if not any(r.get("role") == "move" and r.get("acc") and _fin(r.get("src"))
               and r["src"] >= 0 for r in route):
        raise NavError("calibration tap did not grab the move stick (no "
                       "accepted move route receipt)")
    off = (ox, oy)
    log(f"  nav: display offset=({ox:.1f},{oy:.1f}) spread={spread:.2f} "
        f"n={n_off} (rigid)")

    legs_ev = []
    for li, leg in enumerate(spec["legs"]):
        check_budget(f"leg {li} start")
        target = leg["to"]
        tol = leg.get("tol", 10.0)
        jump_advance = leg.get("jump_advance", False)
        jump_only = leg.get("jump_only", False)
        running_goal = leg.get("running_goal", False)
        # ---- settle: wait for the (possibly just-transitioned) room --------
        settle_deadline = time.monotonic() + min(SETTLE_WAIT, max(remaining(), 1.0))
        expect = leg.get("settle_expect")
        expect_tol = leg.get("settle_tol", 40.0)
        prev = None
        settled = None
        while time.monotonic() < settle_deadline:
            cur = _latest_play(_parse(dev))
            if cur is not None:
                p = cur["pos"][:2]
                ok_expect = expect is None or _dist(p, expect) <= expect_tol
                if ok_expect and prev is not None and _dist(p, prev) <= SETTLE_STABLE:
                    settled = cur
                    break
                prev = p
            time.sleep(SETTLE_POLL)
        if settled is None:
            raise NavError(f"leg {li} never settled (expect={expect} "
                           f"tol={expect_tol}); last pos={prev}")
        pos = settled["pos"][:2]
        log(f"  nav leg {li}: settled at {pos} -> target {target}")
        # ---- steer ----------------------------------------------------------
        pos_trace = [list(pos)]
        iters = 0
        done_by = None
        last_running = bool(settled.get("running"))
        while True:
            check_budget(f"leg {li} iter {iters}")
            if iters >= MAX_ITERS:
                raise NavError(f"leg {li} iteration cap {MAX_ITERS} at pos={pos}")
            cur = _latest_play(_parse(dev))
            if cur is None:
                time.sleep(0.3)
                continue
            p = cur["pos"][:2]
            last_running = cur.get("running") is True
            if running_goal and cur.get("running") is True:
                pos, done_by = p, "running_goal"
                break
            if jump_advance and _dist(p, pos) > JUMP_DETECT:
                pos, done_by = p, "door_jump"
                break
            if not running_goal and not jump_only and _dist(p, target) <= tol:
                pos, done_by = p, "tolerance"
                break
            if _dist(p, pos) > JUMP_DETECT and not jump_advance:
                raise NavError(f"leg {li} unexpected position jump "
                               f"{pos} -> {p} (wrong door/transition?)")
            pos = p
            # Distance-proportional hold (undershoot -> monotonic convergence).
            # A fixed full-speed run covers ~36 units + deceleration coast and
            # overshoots nearby targets, oscillating until budget burns. Scale
            # the hold by distance (ms ~= 12/unit, below the ~45 u/s effective
            # speed) so each run lands short and the loop converges in a few iters.
            dist_t = _dist(p, target)
            dyn_hold = max(100.0, min(float(hold), dist_t * 12.0))
            recipe = _steer_recipe(home, am, target, pos, xf, off, w, h, throw, dyn_hold)
            rid = "nav-%s-l%d-i%d-%d" % (label, li, iters, int(time.time() * 1000))
            r = inj.run(recipe, run_id=rid)
            if not r.get("ok"):
                raise NavError(f"leg {li} iter {iters} injection failed: "
                               f"{r.get('error')} (release_error={r.get('release_error')})")
            iters += 1
            pos_trace.append(list(pos))
        legs_ev.append(dict(leg=li, to=list(target), tol=tol, iterations=iters,
                            done_by=done_by, final_pos=list(pos),
                            running=last_running,
                            pos_trace=pos_trace[-12:]))
        log(f"  nav leg {li}: done_by={done_by} iters={iters} pos={pos}")

    # ---- final goal grace: fight_mode must be observable --------------------
    final_running = False
    if spec["legs"][-1].get("running_goal"):
        grace = time.monotonic() + GOAL_GRACE
        while time.monotonic() < grace:
            cur = _latest_play(_parse(dev))
            if cur is not None and cur.get("running") is True:
                final_running = True
                break
            time.sleep(0.3)
        if not final_running:
            raise NavError("fight_mode not reached: no running=true sample "
                           "after the final leg (ArenaStarter not triggered)")
    ev = dict(ok=True, coord_offset=[round(off[0], 2), round(off[1], 2)],
              offset_spread=round(spread, 3), offset_samples=n_off,
              xform=xf, legs=legs_ev, final_running=final_running,
              budget_used_s=round(time.monotonic() - t0, 2),
              calibration_run_id=cal_id)
    if out_dir is not None:
        try:
            text = dev.adb("logcat", "-d", "-v", "threadtime", timeout=120)
            (out_dir / f"{label}_nav_logcat.log").write_text(text, encoding="utf-8")
        except Exception:
            pass    # evidence dump is best-effort; the gates above are authoritative
    return ev
