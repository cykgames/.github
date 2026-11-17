"""W2 collector + receipt-chain matcher (P1+P2 plan v4.17 W2c).

Dual-marker logcat parse ([CYKTEST] state samples + [CYKTEST-RING] receipt
lines), ring schema validation (malformed -> fail-closed, never relaxed),
(session, ring, seq) merger dedupe (same seq inconsistent -> reject), session
nonce/pid binding (logcat producer PID == run pid), watermark/capacity/overflow
validation, and the atom -> arrival -> route -> applied injective
order-preserving chain matcher with three receipt families (atom / mouse
emulation / cleanup) and per-role applied semantics.

mt common time domain is SystemClock.uptimeMillis (F21); ZERO epoch<->uptime
window conversion. mixed mt+legacy overlap -> ReleaseError. Pure functions over
parsed data so CLI tests can use synthetic fixtures.
Author: cykbot9 (AI agent).
"""
import json
import re

EMULATION = -1                 # Godot InputEvent.DEVICE_ID_EMULATION
ARRIVAL_BOUND_MS = 50          # receipt_up - t_after <= 50ms (margin max(0,.))
APPLY_BOUND_MS = 50            # move/aim/shoot applied leg
JUMP_BOUND_MS = 170            # JUMP_BUFFER 0.12 + 0.05 (jump applied leg)
DELIVERY_CAP_MS = 250          # injector per-event hard cap
COORD_TOL = 2.0                # +/-2 display px
CLEANUP_MARGIN_MS = 50         # atom candidate window tail / cleanup separation
RING_MAX_BYTES = 900

RING_KEYS = {
    "input_log": {"s", "u", "k", "i", "d", "x", "y", "e"},
    "route_log": {"s", "u", "src", "id", "role", "acc", "ver", "val"},
    "apply_log": {"s", "u", "src", "ver", "role", "val", "st"},
    "jump_log": {"s", "u", "jumps", "src", "ver", "st"},
}
INPUT_KINDS = {"touch_down", "touch_up", "drag", "mouse_down", "mouse_up", "mouse_motion"}
ROLES = {"move", "aim", "shoot", "jump"}


class CollectError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# dual-marker parse
# --------------------------------------------------------------------------- #
_PID_RE = re.compile(r"\s(\d+)\s+\d+\s+[VDIWEF]\s+")


def parse_logcat(text):
    """Parse [CYKTEST] samples and [CYKTEST-RING] lines. Returns dict with
    samples, ring_lines [(nbytes, dict|None)], producer_pids, malformed count.
    Ring lines are validated separately (malformed -> fail-closed)."""
    samples = []
    ring_lines = []
    producer_pids = set()
    for l in text.splitlines():
        m = _PID_RE.search(l)
        pid = int(m.group(1)) if m else None
        if "[CYKTEST] " in l and "[CYKTEST-RING]" not in l:
            js = l.split("[CYKTEST] ", 1)[1]
            try:
                d = json.loads(js)
            except Exception:
                continue
            samples.append(d)
            if pid:
                producer_pids.add(pid)
        elif "[CYKTEST-RING] " in l:
            js = l.split("[CYKTEST-RING] ", 1)[1]
            raw = "[CYKTEST-RING] " + js
            nb = len(raw.encode("utf-8"))
            try:
                d = json.loads(js)
            except Exception:
                d = None
            ring_lines.append((nb, d))
    return dict(samples=samples, ring_lines=ring_lines,
                producer_pids=sorted(producer_pids))


def validate_ring_line(nb, d):
    """A ring line must be <=900 bytes, well-formed, with a known ring and
    entries carrying the required keys + monotonic seq. malformed -> error."""
    if nb > RING_MAX_BYTES:
        raise CollectError(f"ring line {nb}B > {RING_MAX_BYTES} (truncation risk)")
    if not isinstance(d, dict):
        raise CollectError("ring line malformed JSON")
    ring = d.get("ring")
    if ring not in RING_KEYS:
        raise CollectError(f"unknown ring {ring!r}")
    if "sid" not in d or "seq_range" not in d or "entries" not in d:
        raise CollectError(f"ring {ring} missing envelope fields")
    entries = d["entries"]
    if not isinstance(entries, list) or not entries:
        raise CollectError(f"ring {ring} empty/non-list entries")
    req = RING_KEYS[ring]
    last = None
    for e in entries:
        if not isinstance(e, dict) or not req.issubset(e.keys()):
            raise CollectError(f"ring {ring} entry missing keys {req - set(e.keys())}")
        s = e["s"]
        if last is not None and s <= last:
            raise CollectError(f"ring {ring} seq non-monotonic {last}->{s}")
        last = s
    lo, hi = d["seq_range"]
    if entries[0]["s"] != lo or entries[-1]["s"] != hi:
        raise CollectError(f"ring {ring} seq_range mismatch")
    return True


def merge_rings(ring_lines):
    """Validate every ring line and merge entries by (sid, ring, seq). Same seq
    with inconsistent content -> reject. Returns {ring: {seq: entry}} + sids."""
    merged = {}
    sids = set()
    for nb, d in ring_lines:
        validate_ring_line(nb, d)
        sid = d["sid"]
        sids.add(sid)
        ring = d["ring"]
        bucket = merged.setdefault(ring, {})
        for e in d["entries"]:
            s = e["s"]
            if s in bucket:
                if bucket[s] != e:
                    raise CollectError(f"ring {ring} seq {s} inconsistent duplicate")
            else:
                bucket[s] = e
    return merged, sids


def bind_session(samples, sids, run_pid):
    """Every sample/ring must come from one session whose pid == run producer
    pid. Old session / nonce mismatch / old pid -> reject."""
    sample_sids = set(s.get("session_id") for s in samples if "session_id" in s)
    all_sids = sids | sample_sids
    if len(all_sids) != 1:
        raise CollectError(f"multiple/zero sessions {all_sids} (nonce collision or stale)")
    sid = next(iter(all_sids))
    parts = sid.split(":")
    if len(parts) != 3:
        raise CollectError(f"bad session_id format {sid!r}")
    nonce, pid_s, ready = parts
    if len(nonce) != 16 or nonce.startswith("-"):
        raise CollectError(f"nonce not unsigned 64-bit hex {nonce!r}")
    try:
        sess_pid = int(pid_s)
    except ValueError:
        raise CollectError(f"session pid not int {pid_s!r}")
    if run_pid is not None and sess_pid != run_pid:
        raise CollectError(f"session pid {sess_pid} != run producer pid {run_pid}")
    return sid


# --------------------------------------------------------------------------- #
# watermark / capacity / overflow
# --------------------------------------------------------------------------- #
def check_watermarks(samples, budget=None):
    """Run-window overflow delta must be 0 for every ring; capacity >= budget;
    no holes in the matched seq coverage. Returns per-ring delta dict."""
    wms = [s["wm"] for s in samples if "wm" in s]
    if not wms:
        raise CollectError("no ring watermarks in samples")
    rings = wms[0].keys()
    delta = {}
    for r in rings:
        first = wms[0][r]["overflow"]
        last = wms[-1][r]["overflow"]
        delta[r] = last - first
        if delta[r] != 0:
            raise CollectError(f"ring {r} overflow delta {delta[r]} != 0 (data loss)")
        if budget and r in budget:
            cap = wms[-1][r]["capacity"]
            if cap < budget[r]:
                raise CollectError(f"ring {r} capacity {cap} < budget {budget[r]}")
    return delta


def check_coverage(entries_by_seq, expected_lo, expected_hi):
    """Matched receipt seq coverage [lo,hi] must have no holes."""
    for s in range(expected_lo, expected_hi + 1):
        if s not in entries_by_seq:
            raise CollectError(f"receipt coverage hole at seq {s}")
    return True


# --------------------------------------------------------------------------- #
# atom expansion (injector trace -> expected atoms)
# --------------------------------------------------------------------------- #
def expand_atoms(trace):
    """Frame expansion table: DOWN/POINTER_DOWN -> 1 touch atom (pressed=true,
    new id); MOVE -> n drag atoms (one per pointer id); POINTER_UP/UP -> 1 touch
    atom (pressed=false, that id). stable id == slot == Android pointer id ==
    Godot touch index (NOT action-index). Each atom carries the injector
    t_before/t_after delivery window + edge_seq/atom_seq for ordering."""
    atoms = []
    prev_coords = {}
    for ev in trace:
        action = ev["action"]
        ids = ev["ids"]
        coords = ev["coords"]
        tb, ta = ev["t_before"], ev["t_after"]
        edge = ev.get("edge_seq", ev.get("sequence"))
        if action in ("down", "pointer_down"):
            idx = ev["index"] if action == "pointer_down" else 0
            atoms.append(dict(kind="touch_down", stable_id=ids[idx],
                              x=coords[idx][0], y=coords[idx][1], pressed=True,
                              t_before=tb, t_after=ta, edge_seq=edge,
                              atom_seq=ev.get("atom_seq", 0), sequence=ev["sequence"]))
            for i, sid in enumerate(ids):
                prev_coords[sid] = (coords[i][0], coords[i][1])
        elif action == "move":
            # Godot emits ScreenDrag only for pointers whose position CHANGED;
            # a stationary pointer in a multi-pointer MOVE yields no drag
            # receipt, so no expected drag atom is created for it.
            atom_i = 0
            for i, sid in enumerate(ids):
                cur = (coords[i][0], coords[i][1])
                prev = prev_coords.get(sid)
                if prev is None or abs(cur[0] - prev[0]) > 0.01 or abs(cur[1] - prev[1]) > 0.01:
                    atoms.append(dict(kind="drag", stable_id=sid,
                                      x=cur[0], y=cur[1], pressed=None,
                                      t_before=tb, t_after=ta, edge_seq=edge,
                                      atom_seq=atom_i, sequence=ev["sequence"]))
                    atom_i += 1
                prev_coords[sid] = cur
        elif action in ("pointer_up", "up"):
            idx = ev["index"] if action == "pointer_up" else 0
            atoms.append(dict(kind="touch_up", stable_id=ids[idx],
                              x=coords[idx][0], y=coords[idx][1], pressed=False,
                              t_before=tb, t_after=ta, edge_seq=edge,
                              atom_seq=ev.get("atom_seq", 0), sequence=ev["sequence"]))
            prev_coords.pop(ids[idx], None)
        elif action == "cancel":
            for i, sid in enumerate(ids):
                atoms.append(dict(kind="touch_up", stable_id=sid,
                                  x=coords[i][0], y=coords[i][1], pressed=False,
                                  t_before=tb, t_after=ta, edge_seq=edge,
                                  atom_seq=i, sequence=ev["sequence"], cleanup=True))
                prev_coords.pop(sid, None)
    atoms.sort(key=lambda a: (a["edge_seq"], a["atom_seq"]))
    return atoms


def _cleanup_window_start(trace):
    """cleanup phase window start = max(atom t_after) + 50ms + margin (derived
    from trace, not a fixed lastUP+0.10; avoids slow-ack UP candidate overlap)."""
    if not trace:
        return None
    return max(ev["t_after"] for ev in trace) + CLEANUP_MARGIN_MS


def classify_receipts(input_entries, trace):
    """Three families: cleanup (up >= cleanup window start), mouse emulation
    (device == EMULATION), atom candidates (native touch/drag). Returns
    (atom_candidates, emulation, cleanup)."""
    cw = _cleanup_window_start(trace)
    atom_cand, emulation, cleanup = [], [], []
    for e in input_entries:
        if cw is not None and e["u"] >= cw:
            cleanup.append(e)
        elif e["d"] == EMULATION:
            emulation.append(e)
        else:
            atom_cand.append(e)
    return atom_cand, emulation, cleanup


def derive_coord_offset(atoms, atom_candidates):
    """W2e forward display transform: measure the rigid display->receipt
    translation from the run itself. Preliminary injective correspondence by
    kind/stable_id/time-window (coords ignored); each uniquely-matched atom
    yields offset=(atom_display - receipt). The transform must be a RIGID
    translation: all per-atom offsets agree within COORD_TOL, else fail-closed
    (a non-rigid/scale mismatch is a transform finding, not absorbed). Returns
    (ox, oy, n, spread). Identity (0,0) when receipts already match display."""
    receipts = sorted(atom_candidates, key=lambda e: (e["u"], e["s"]))
    consumed = set()
    offs = []
    for a in sorted(atoms, key=lambda a: (a["edge_seq"], a["atom_seq"])):
        if a.get("cleanup"):
            continue
        cands = [e for e in receipts
                 if e["s"] not in consumed and e["k"] == a["kind"]
                 and e["i"] == a["stable_id"]
                 and a["t_before"] <= e["u"] <= a["t_after"] + CLEANUP_MARGIN_MS]
        if len(cands) != 1:
            continue
        e = cands[0]
        consumed.add(e["s"])
        offs.append((a["x"] - e["x"], a["y"] - e["y"]))
    if not offs:
        raise CollectError("transform: no unique atom->receipt correspondence "
                           "to derive the display offset")
    ox = sum(o[0] for o in offs) / len(offs)
    oy = sum(o[1] for o in offs) / len(offs)
    spread = max(max(abs(o[0] - ox), abs(o[1] - oy)) for o in offs)
    if spread > COORD_TOL:
        raise CollectError(f"transform: non-rigid display offset (spread "
                           f"{spread:.2f}px > {COORD_TOL}); offsets={offs[:4]}")
    return ox, oy, len(offs), spread


def match_atoms(atoms, atom_candidates, coord_offset=(0.0, 0.0)):
    """Injective order-preserving greedy match: atoms sorted (edge_seq,
    atom_seq), receipts sorted (up, seq); candidate = kind/stable_id/coords±2 +
    up in [t_before, t_after+50ms] + unconsumed. >1 candidate -> ambiguity
    reject; unmatched atom -> FAIL. Extra emulation/cleanup receipts are NOT
    failures (handled by classify). Returns matched pairs + failures.

    coord_offset=(ox,oy) is the rigid display->receipt translation measured by
    derive_coord_offset (W2e forward transform): atom display coords are
    shifted by -offset into the receipt (Godot-screen) domain before the ±2px
    compare. Default (0,0) preserves the identity-transform path (002)."""
    ox, oy = coord_offset
    receipts = sorted(atom_candidates, key=lambda e: (e["u"], e["s"]))
    consumed = set()
    matched = []
    failures = []
    for a in atoms:
        if a.get("cleanup"):
            continue
        ax, ay = a["x"] - ox, a["y"] - oy
        cands = []
        for e in receipts:
            if e["s"] in consumed:
                continue
            if e["k"] != a["kind"] or e["i"] != a["stable_id"]:
                continue
            if abs(e["x"] - ax) > COORD_TOL or abs(e["y"] - ay) > COORD_TOL:
                continue
            if not (a["t_before"] <= e["u"] <= a["t_after"] + CLEANUP_MARGIN_MS):
                continue
            cands.append(e)
        if len(cands) == 0:
            failures.append(dict(atom=a, reason="no_receipt"))
        elif len(cands) > 1:
            failures.append(dict(atom=a, reason="ambiguous",
                                 candidates=[c["s"] for c in cands]))
        else:
            e = cands[0]
            consumed.add(e["s"])
            latency = max(0, e["u"] - a["t_after"])
            if latency > ARRIVAL_BOUND_MS:
                failures.append(dict(atom=a, reason="arrival_latency", latency=latency))
            matched.append((a, e))
    return matched, failures


# --------------------------------------------------------------------------- #
# arrival -> route -> applied (per-role namespace + ordering + latency)
# --------------------------------------------------------------------------- #
def _verify_jump_bridge(route, input_by_seq, expected_kind="touch_down"):
    """Host re-verification of the J bridge receipt (never trusts a pre-pasted
    source). native_meta: gui_cyk_seq == src == matched touch arrival seq.
    emulated_adjacency: mouse row device==EMULATION/kind/coords + seq_touch ==
    seq_mouse+1 + touch row match + finalized. Returns (ok, reason)."""
    br = route.get("bridge")
    if not br:
        return False, "no_bridge"
    if br.get("status") != "finalized":
        return False, "bridge_not_finalized"
    path = br.get("path")
    src = route.get("src", -1)
    if path == "native_meta":
        if br.get("gui_cyk_seq") != src:
            return False, "native_meta_seq_mismatch"
        arr = input_by_seq.get(src)
        if arr is None or arr["k"] != expected_kind:
            return False, "native_meta_no_touch_arrival"
        return True, "ok"
    if path == "emulated_adjacency":
        mouse_seq = br.get("mouse_seq", -1)
        m = input_by_seq.get(mouse_seq)
        if m is None or m["d"] != EMULATION or m["k"] != "mouse_down":
            return False, "emulated_mouse_provenance"
        if src != mouse_seq + 1:
            return False, "emulated_adjacency"
        t = input_by_seq.get(src)
        if t is None or t["k"] != expected_kind or t["i"] != 0:
            return False, "emulated_touch_match"
        if abs(t["x"] - m["x"]) > COORD_TOL or abs(t["y"] - m["y"]) > COORD_TOL:
            return False, "emulated_coords"
        return True, "ok"
    return False, "unknown_bridge_path"


def match_route_applied(matched, route_entries, apply_entries, jump_entries,
                        roles_by_id, input_by_seq):
    """For each matched atom->arrival, link route token (src==arrival seq +
    role + accepted) then applied receipt (move/aim/shoot -> apply_log copied
    per-role version==route version; jump -> jump_log applied, no apply leg).
    Per-role ordering + latency bounds; J bridge host re-verification. Returns
    (links, failures)."""
    route_by_src_role = {}
    for r in route_entries:
        route_by_src_role.setdefault((r["src"], r["role"]), []).append(r)
    apply_by_role_ver = {}
    for a in apply_entries:
        apply_by_role_ver.setdefault((a["role"], a["ver"]), []).append(a)
    jump_by_src = {}
    for j in jump_entries:
        jump_by_src.setdefault(j["src"], []).append(j)
    links = []
    failures = []
    used_route = set()
    for a, arr in matched:
        role = roles_by_id.get(a["stable_id"])
        if role is None:
            failures.append(dict(atom=a, reason="unconsumed_role"))
            continue
        rcands = [r for r in route_by_src_role.get((arr["s"], role), [])
                  if r["acc"] and r["s"] not in used_route]
        if len(rcands) != 1:
            failures.append(dict(atom=a, role=role, reason="route_link",
                                 n=len(rcands)))
            continue
        r = rcands[0]
        used_route.add(r["s"])
        if role == "jump":
            is_release = (a["kind"] == "touch_up")
            expected_kind = "touch_up" if is_release else "touch_down"
            ok, reason = _verify_jump_bridge(r, input_by_seq, expected_kind)
            if not ok:
                failures.append(dict(atom=a, role=role, reason="bridge:" + reason))
                continue
            if is_release:
                # UP release predicate: route-release token (bridge.release) +
                # ordering; NO jump_log requirement (fresh jump_held=false is
                # verified host-side from telemetry, not here).
                if not r.get("bridge", {}).get("release"):
                    failures.append(dict(atom=a, role=role, reason="release_not_marked"))
                    continue
                if not (arr["u"] <= r["u"]):
                    failures.append(dict(atom=a, role=role, reason="release_order"))
                    continue
                links.append(dict(atom=a, arrival=arr, route=r, applied=None,
                                  role=role, release=True))
                continue
            jcands = jump_by_src.get(r["src"], [])
            if len(jcands) != 1:
                failures.append(dict(atom=a, role=role, reason="jump_applied_link",
                                     n=len(jcands)))
                continue
            j = jcands[0]
            if not (arr["u"] <= r["u"] <= j["u"]):
                failures.append(dict(atom=a, role=role, reason="jump_order"))
                continue
            if j["u"] - arr["u"] > JUMP_BOUND_MS:
                failures.append(dict(atom=a, role=role, reason="jump_latency",
                                     latency=j["u"] - arr["u"]))
                continue
            links.append(dict(atom=a, arrival=arr, route=r, applied=j, role=role))
        else:
            acands = apply_by_role_ver.get((role, r["ver"]), [])
            if len(acands) != 1:
                failures.append(dict(atom=a, role=role, reason="apply_link",
                                     n=len(acands)))
                continue
            ap = acands[0]
            if ap["st"] != "copied":
                failures.append(dict(atom=a, role=role, reason="apply_status"))
                continue
            if not (arr["u"] <= r["u"] <= ap["u"]):
                failures.append(dict(atom=a, role=role, reason="apply_order"))
                continue
            if ap["u"] - arr["u"] > ARRIVAL_BOUND_MS + APPLY_BOUND_MS:
                failures.append(dict(atom=a, role=role, reason="apply_latency",
                                     latency=ap["u"] - arr["u"]))
                continue
            links.append(dict(atom=a, arrival=arr, route=r, applied=ap, role=role))
    return links, failures


def check_mixed_overlap(contacts_provenance, overlap_roles):
    """W2 mt overlap requires ALL participating contacts mt (device_uptime).
    A mixed mt+legacy overlap -> fail-closed (zero epoch conversion)."""
    for role in overlap_roles:
        prov = contacts_provenance.get(role)
        if prov != "device_uptime":
            raise CollectError(f"mixed mt+legacy overlap on role {role} "
                               f"(provenance {prov!r}) -- refusing conversion")
    return True