"""W2 mt state assertions (P1+P2 plan v4.17 W2d, uptime domain).

mt path: ALL time predicates use `up` (SystemClock.uptimeMillis), never epoch
ts (ts is report-only + independent collection ceiling). Per-contact active
windows are [DOWN t_after, UP t_before] (delivery endpoints absorbed). role ->
channel: move->stick, aim->aim, jump->jumps/vy. drag_windows_covered + settle
freshness stay MANDATORY (fail-closed). Every declared role must be consumed
(unconsumed -> fail). dual/jump causal windows per plan.
Author: cykbot9 (AI agent).
"""
import math

UNC_MOVE = 100     # move/aim causal window uncertainty (ms; plan 0.10s)
UNC_JUMP = 170     # jump causal window uncertainty (ms; JUMP_BUFFER 0.12+0.05s)
SETTLE_MARGIN = 200  # ms


class AssertError(RuntimeError):
    pass


def _mag(v):
    return math.sqrt(float(v[0]) ** 2 + float(v[1]) ** 2)


def _running(samples):
    return [s for s in samples if s.get("scene") == "play" and s.get("running")]


def _distinct_up(samples):
    return len(set(s["up"] for s in samples if "up" in s))


def mt_state_checks(samples, windows, base, dual, jump):
    """Returns list of {name, ok, detail}. windows: [{role, t0, t1}] uptime
    active windows. base/dual/jump from mt_spec.parse_spec (dual/jump may be
    None). Raises AssertError on corrupt series; individual checks fail closed."""
    run = _running(samples)
    ups = [s["up"] for s in samples if "up" in s]
    if any(b < a for a, b in zip(ups, ups[1:])):
        raise AssertError("mt: up timestamps out of order -- corrupt series")
    checks = []

    def add(name, ok, detail):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))

    # run gate: >=3 distinct-up running samples
    add("run_gate", _distinct_up(run) >= 3,
        f"{_distinct_up(run)} distinct-up running samples")

    # timer monotonic increase during run (uptime ordered)
    times = [float(s["time"]) for s in run]
    if len(times) >= 2:
        inc = times[-1] - times[0]
        mono = all(b >= a - 0.05 for a, b in zip(times, times[1:]))
        add("timer_increase", mono and inc >= float(base["time_increase_min"]),
            f"increase={inc:.2f} monotonic={mono}")
    else:
        add("timer_increase", False, f"only {len(times)} running samples")

    move_wins = [w for w in windows if w["role"] == "move"]
    aim_wins = [w for w in windows if w["role"] == "aim"]
    jump_wins = [w for w in windows if w["role"] == "jump"]

    # move coverage + drag_windows_covered (MANDATORY)
    peak_min = float(base["stick_peak_min"])
    if move_wins:
        covered = 0
        peak = 0.0
        for w in move_wins:
            lo, hi = w["t0"] + UNC_MOVE, w["t1"] - UNC_MOVE
            if hi <= lo:
                continue
            inside = [s for s in run if lo <= s["up"] <= hi]
            act = [s for s in inside if _mag(s["stick"]) >= peak_min]
            peak = max(peak, max((_mag(s["stick"]) for s in inside), default=0.0))
            if len(act) >= 1:
                covered += 1
        add("drag_windows_covered", covered == len(move_wins) and covered > 0,
            f"{covered}/{len(move_wins)} move windows covered")
        add("stick_input_reached", peak >= peak_min, f"peak={peak:.3f}")
    else:
        add("drag_windows_covered", False, "no move window (fail-closed)")
        add("stick_input_reached", False, "no move window")

    # displacement during active move episode
    active_segs = [s for s in run if _mag(s["stick"]) >= peak_min]
    if len(active_segs) >= 2:
        p0 = active_segs[0]["pos"]
        exc = max(math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(p0, s["pos"])))
                  for s in active_segs)
        add("player_displacement", exc >= float(base["pos_delta_min"]),
            f"excursion={exc:.2f}")
    else:
        add("player_displacement", False, "no active move segment")

    # settle freshness (MANDATORY): fresh neutral stick after last move window
    if move_wins:
        last_t1 = max(w["t1"] for w in move_wins)
        thresh = last_t1 + UNC_MOVE + SETTLE_MARGIN
        fresh = [s for s in run if s["up"] >= thresh]
        settled = any(_mag(s["stick"]) <= float(base["stick_settle_max"]) for s in fresh)
        add("stick_settle_fresh", settled,
            f"fresh samples after {thresh:.0f}: {len(fresh)} settled={settled}")
    else:
        add("stick_settle_fresh", False, "no move window")

    # dual block
    if dual is not None:
        ap = float(dual["aim_peak_min"])
        aim_peak = 0.0
        aim_covered = 0
        for w in aim_wins:
            lo, hi = w["t0"] + UNC_MOVE, w["t1"] - UNC_MOVE
            if hi <= lo:
                continue
            inside = [s for s in run if lo <= s["up"] <= hi]
            act = [s for s in inside if _mag(s.get("aim", [0, 0])) >= ap]
            aim_peak = max(aim_peak, max((_mag(s.get("aim", [0, 0])) for s in inside), default=0.0))
            if act:
                aim_covered += 1
        add("aim_coverage", aim_covered == len(aim_wins) and aim_covered > 0 and aim_peak >= ap,
            f"{aim_covered}/{len(aim_wins)} aim windows peak={aim_peak:.3f}")
        # simultaneous overlap interior (unc-shrunk move∩aim)
        overlap_samples = []
        for mw in move_wins:
            for aw in aim_wins:
                lo = max(mw["t0"], aw["t0"]) + UNC_MOVE
                hi = min(mw["t1"], aw["t1"]) - UNC_MOVE
                if hi <= lo:
                    continue
                for s in run:
                    if lo <= s["up"] <= hi and _mag(s["stick"]) >= peak_min \
                            and _mag(s.get("aim", [0, 0])) >= ap:
                        overlap_samples.append(s)
        n_overlap = _distinct_up(overlap_samples)
        add("dual_simultaneous_overlap", n_overlap >= int(dual["simultaneous_min_samples"]),
            f"{n_overlap} distinct-up simultaneous samples (min {dual['simultaneous_min_samples']})")
        # fire_count delta
        fc = [int(s.get("fire_count", 0)) for s in run]
        fdelta = (fc[-1] - fc[0]) if len(fc) >= 2 else 0
        add("fire_delta", fdelta >= int(dual["fire_delta_min"]),
            f"fire_count delta={fdelta} (min {dual['fire_delta_min']})")
        # end dual settle: aim neutral + shoot false fresh
        last_t1 = max([w["t1"] for w in aim_wins] or [0])
        thresh = last_t1 + UNC_MOVE + SETTLE_MARGIN
        fresh = [s for s in run if s["up"] >= thresh]
        end_settle = any(_mag(s.get("aim", [0, 0])) <= float(dual["aim_settle_max"])
                         and s.get("shoot") is False for s in fresh)
        add("dual_end_settle", end_settle, f"fresh aim-neutral+shoot-false={end_settle}")

    # jump block
    if jump is not None:
        jumps = [int(s.get("jumps", 0)) for s in run]
        jdelta = (jumps[-1] - jumps[0]) if len(jumps) >= 2 else 0
        add("jump_delta", jdelta >= int(jump["jump_delta_min"]),
            f"jumps delta={jdelta} (min {jump['jump_delta_min']})")
        vy_peak = max((float(s.get("vy", 0)) for s in run), default=0.0)
        add("vy_peak", vy_peak >= float(jump["vy_peak_min"]),
            f"vy_peak={vy_peak:.2f} (min {jump['vy_peak_min']})")
        # grounded before: a floor=true sample before the first jump window
        if jump_wins:
            first_t0 = min(w["t0"] for w in jump_wins)
            grounded_before = any(s.get("floor") is True and s["up"] < first_t0 for s in run)
            add("jump_grounded_before", grounded_before,
                f"floor sample before jump window={grounded_before}")
        else:
            add("jump_grounded_before", False, "no jump window")

    # unconsumed roles fail-closed
    declared = {"move"}
    if dual is not None:
        declared |= {"aim", "shoot"}
    if jump is not None:
        declared |= {"jump"}
    consumed = set(w["role"] for w in windows)
    if dual is not None:
        consumed |= {"shoot"}   # shoot consumed via fire_delta + end_settle
    unconsumed = declared - consumed
    add("roles_consumed", not unconsumed, f"unconsumed={unconsumed}")

    return checks


def all_pass(checks):
    return all(c["ok"] for c in checks)