"""W2 nested spec blocks (P1+P2 plan v4.17 W2d, B13/luna C1).

Optional nested `dual` and `jump` blocks extend the flat five-key base spec.
Threading contract: state_checks receives the flat base spec PLUS independent
dual/jump parameter dicts (never a merged dict). Unknown nested keys reject;
a present `dual` block still requires all five base keys; top-level unknown
keys still reject. Absence preserves 002 semantics; present-but-malformed or
ignored blocks fail closed.
Author: cykbot9 (AI agent).
"""
import math

BASE_KEYS = {"require_running", "time_increase_min", "stick_peak_min",
             "pos_delta_min", "stick_settle_max"}
DUAL_KEYS = {"aim_peak_min", "aim_settle_max", "simultaneous_min_samples", "fire_delta_min"}
JUMP_KEYS = {"jump_delta_min", "vy_peak_min", "require_grounded_before"}


class SpecError(RuntimeError):
    pass


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse_spec(spec):
    """Validate a spec and split into (base, dual, jump). base is the flat
    five-key dict; dual/jump are independent parameter dicts or None. Raises
    SpecError on any unknown key / malformed value / missing base key."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    top_unknown = set(spec.keys()) - BASE_KEYS - {"dual", "jump"}
    if top_unknown:
        raise SpecError(f"spec unknown top-level keys {top_unknown}")
    if not BASE_KEYS.issubset(spec.keys()):
        raise SpecError(f"spec missing base keys {BASE_KEYS - set(spec.keys())}")
    base = {k: spec[k] for k in BASE_KEYS}
    # base value validation (mirrors release.validate_spec_values)
    if base["require_running"] is not True:
        raise SpecError("require_running must be true")
    if not _finite(base["time_increase_min"]) or base["time_increase_min"] < 1.0:
        raise SpecError("time_increase_min must be finite >= 1.0")
    p = base["stick_peak_min"]
    if not _finite(p) or not (0.1 <= p <= 1.0):
        raise SpecError("stick_peak_min must be within [0.1,1.0]")
    s = base["stick_settle_max"]
    if not _finite(s) or not (0.0 <= s < p):
        raise SpecError("stick_settle_max must be finite >=0 and < stick_peak_min")
    if not _finite(base["pos_delta_min"]) or base["pos_delta_min"] < 0.1:
        raise SpecError("pos_delta_min must be finite >= 0.1")

    dual = None
    if "dual" in spec:
        d = spec["dual"]
        if not isinstance(d, dict):
            raise SpecError("dual block must be an object")
        unk = set(d.keys()) - DUAL_KEYS
        if unk:
            raise SpecError(f"dual unknown keys {unk}")
        if not DUAL_KEYS.issubset(d.keys()):
            raise SpecError(f"dual missing keys {DUAL_KEYS - set(d.keys())}")
        ap = d["aim_peak_min"]
        if not _finite(ap) or not (0.1 <= ap <= 1.0):
            raise SpecError("dual.aim_peak_min must be within [0.1,1.0]")
        asm = d["aim_settle_max"]
        if not _finite(asm) or not (0.0 <= asm < ap):
            raise SpecError("dual.aim_settle_max must be >=0 and < aim_peak_min")
        sms = d["simultaneous_min_samples"]
        if not isinstance(sms, int) or isinstance(sms, bool) or sms < 3:
            raise SpecError("dual.simultaneous_min_samples must be int >= 3")
        fdm = d["fire_delta_min"]
        if not isinstance(fdm, int) or isinstance(fdm, bool) or fdm < 1:
            raise SpecError("dual.fire_delta_min must be int >= 1")
        dual = dict(d)

    jump = None
    if "jump" in spec:
        j = spec["jump"]
        if not isinstance(j, dict):
            raise SpecError("jump block must be an object")
        unk = set(j.keys()) - JUMP_KEYS
        if unk:
            raise SpecError(f"jump unknown keys {unk}")
        if not JUMP_KEYS.issubset(j.keys()):
            raise SpecError(f"jump missing keys {JUMP_KEYS - set(j.keys())}")
        jdm = j["jump_delta_min"]
        if not isinstance(jdm, int) or isinstance(jdm, bool) or jdm < 1:
            raise SpecError("jump.jump_delta_min must be int >= 1")
        vyp = j["vy_peak_min"]
        if not _finite(vyp) or vyp <= 0:
            raise SpecError("jump.vy_peak_min must be finite > 0")
        if j["require_grounded_before"] is not True:
            raise SpecError("jump.require_grounded_before must be true")
        jump = dict(j)

    return base, dual, jump


def declared_roles(dual, jump):
    """Roles a spec declares (for unconsumed-role fail-closed). dual declares
    move+aim+shoot; jump declares jump. The base stick spec always implies
    move."""
    roles = {"move"}
    if dual is not None:
        roles |= {"aim", "shoot"}
    if jump is not None:
        roles |= {"jump"}
    return roles