"""W2 host multi-touch injector driver (P1+P2 plan v4.17 W2d, B' transport).

Compiles an mt recipe (contacts with slot/role/path/hold) into a validated
timeline program JSON, deploys the canonical jar + program to a private
shell-owned run directory (push rc + device sha256 verify), runs a SINGLE
app_process invocation device-side (no per-event adb RTT), and verifies
rc + MTRESULT all_ok + trace + cleanup_ok. The host wrapper's finally runs an
independent release (superset, per-id status) to cover child SIGKILL. Rotation
is locked for the run and restored; a neutral preflight guards residual input.

Recipe coordinates are FRACTIONS (fx, fy); the host converts to display pixels
(pixel constants never enter the recipe/repo). Injection rate <= 30Hz/contact.
mt common time domain is uptime; ZERO epoch<->uptime conversion. mixed
mt+legacy overlap is refused by the collector (mt_collect.check_mixed_overlap).
Author: cykbot9 (AI agent).
"""
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

CONTACT_KEYS = {"slot", "role", "path", "hold"}
ROLES = {"move", "aim", "jump"}
PERIOD_MS = 34                 # <= 30Hz/contact (33.3ms floor, +1 margin)
DOWN_GAP_MS = 30               # spacing between successive pointer downs


class InjectError(RuntimeError):
    pass


def validate_recipe(recipe):
    if not isinstance(recipe, dict):
        raise InjectError("recipe must be an object")
    contacts = recipe.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        raise InjectError("recipe.contacts must be a non-empty list")
    slots = set()
    for ct in contacts:
        if not isinstance(ct, dict):
            raise InjectError("contact must be an object")
        extra = set(ct.keys()) - CONTACT_KEYS
        if extra:
            raise InjectError(f"contact unknown keys {extra}")
        if not CONTACT_KEYS.issubset(ct.keys()):
            raise InjectError(f"contact missing keys {CONTACT_KEYS - set(ct.keys())}")
        slot = ct["slot"]
        if not isinstance(slot, int) or slot < 0:
            raise InjectError(f"bad slot {slot!r}")
        if slot in slots:
            raise InjectError(f"duplicate slot {slot}")
        slots.add(slot)
        if ct["role"] not in ROLES:
            raise InjectError(f"unknown role {ct['role']!r}")
        path = ct["path"]
        if not isinstance(path, list) or not path:
            raise InjectError("contact.path must be a non-empty list")
        for pt in path:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise InjectError(f"path point must be [fx,fy]: {pt!r}")
            for v in pt:
                if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                    raise InjectError(f"path fraction out of [0,1]: {pt!r}")
        hold = ct["hold"]
        if not isinstance(hold, (int, float)) or hold <= 0:
            raise InjectError(f"hold must be > 0: {hold!r}")
    return True


def compile_timeline(recipe, display_w, display_h, run_id):
    """Compile contacts -> synchronized timeline frames. First contact DOWN,
    others POINTER_DOWN (append index); synchronized MOVE through path points
    at <=30Hz; per-contact hold -> staggered POINTER_UP/UP groups (equal holds
    reproduce the legacy reverse-order layout byte-for-byte). stable id ==
    slot. Coordinates converted fraction -> display px. edge_seq/atom_seq
    annotated."""
    validate_recipe(recipe)
    contacts = sorted(recipe["contacts"], key=lambda c: c["slot"])
    n = len(contacts)
    ids = [c["slot"] for c in contacts]
    paths = [[[pt[0] * display_w, pt[1] * display_h] for pt in c["path"]] for c in contacts]
    maxlen = max(len(p) for p in paths)
    frames = []
    edge = 0

    def ptrs(positions):
        return [{"id": ids[i], "x": positions[i][0], "y": positions[i][1]} for i in range(n)]

    def pos_at(k):
        return [paths[i][min(k, len(paths[i]) - 1)] for i in range(n)]

    # DOWN phase
    frames.append(dict(delay_ms=0, action="down", index=0,
                       pointers=[{"id": ids[0], "x": paths[0][0][0], "y": paths[0][0][1]}],
                       edge_seq=edge, atom_seq=0))
    edge += 1
    for i in range(1, n):
        partial = [{"id": ids[j], "x": paths[j][0][0], "y": paths[j][0][1]} for j in range(i + 1)]
        frames.append(dict(delay_ms=DOWN_GAP_MS, action="pointer_down", index=i,
                           pointers=partial, edge_seq=edge, atom_seq=0))
        edge += 1
    # MOVE phase (synchronized, <=30Hz)
    for k in range(1, maxlen):
        frames.append(dict(delay_ms=PERIOD_MS, action="move", index=0,
                           pointers=ptrs(pos_at(k)), edge_seq=edge, atom_seq=0))
        edge += 1
    # HOLD + UP phase: per-contact hold (plan schema {slot,role,path,hold}).
    # Each contact lifts hold_i ms after the MOVE phase ends; contacts sharing
    # a hold lift together in reverse slot order. The hold is an UP-phase
    # delay, NOT a redundant same-position MOVE (Godot drag-accumulate would
    # coalesce it -> missing atom receipt, G-m7). A contact lifting while
    # others stay down uses POINTER_UP at its current array index (legal for
    # index 0 too; the jar validator + Android allow non-primary lifts); the
    # last remaining contact uses UP. Equal holds reproduce the legacy frame
    # layout byte-for-byte (single group, reverse order, final UP).
    active = list(ids)
    path_idx = {c["slot"]: i for i, c in enumerate(contacts)}
    groups = {}
    for c in contacts:
        groups.setdefault(int(c["hold"]), []).append(c["slot"])
    prev_hold = 0
    for g_hold in sorted(groups):
        first_in_group = True
        for sid in sorted(groups[g_hold], reverse=True):
            idx = active.index(sid)
            if len(active) == 1:
                action, aidx = "up", 0
                partial = [{"id": sid, "x": paths[path_idx[sid]][-1][0],
                            "y": paths[path_idx[sid]][-1][1]}]
            else:
                action, aidx = "pointer_up", idx
                partial = [{"id": active[j], "x": paths[path_idx[active[j]]][-1][0],
                            "y": paths[path_idx[active[j]]][-1][1]} for j in range(len(active))]
            delay = (g_hold - prev_hold) if first_in_group else 0
            frames.append(dict(delay_ms=delay, action=action, index=aidx,
                               pointers=partial, edge_seq=edge, atom_seq=0))
            first_in_group = False
            edge += 1
            active.pop(idx)
        prev_hold = g_hold
    prog = dict(run_id=run_id,
                contacts=[{"slot": c["slot"], "role": c["role"]} for c in contacts],
                frames=frames)
    return prog


def roles_by_id(recipe):
    return {c["slot"]: c["role"] for c in recipe["contacts"]}


def declared_down_ids(prog):
    """Ids that receive a DOWN/POINTER_DOWN in the compiled timeline (for the
    independent release superset; never-DOWN ids are noop)."""
    down = set()
    for fr in prog["frames"]:
        if fr["action"] in ("down", "pointer_down"):
            idx = fr["index"] if fr["action"] == "pointer_down" else 0
            down.add(fr["pointers"][idx]["id"])
    return sorted(down)


def last_coords(prog):
    """Last coords per id (for independent release)."""
    coords = {}
    for fr in prog["frames"]:
        for p in fr["pointers"]:
            coords[p["id"]] = [p["x"], p["y"]]
    return coords


class MtInjector:
    """Host driver around the canonical jar (deployment + run + cleanup)."""

    def __init__(self, dev, jar_path: Path, display_w, display_h):
        self.dev = dev
        self.jar = Path(jar_path)
        self.w = display_w
        self.h = display_h
        self.jar_sha = hashlib.sha256(self.jar.read_bytes()).hexdigest()

    def _shell(self, cmd, timeout=120):
        return self.dev.adb("shell", cmd, timeout=timeout)

    def _rotation_lock(self):
        prev = {}
        for key in ("accelerometer_rotation", "user_rotation"):
            prev[key] = self._shell("settings get system %s" % key).strip()
        self._shell("settings put system accelerometer_rotation 0")
        self._shell("settings put system user_rotation 0")
        return prev

    def _rotation_restore(self, prev):
        for key, val in prev.items():
            if val and val != "null":
                self._shell("settings put system %s %s" % (key, val))

    def run(self, recipe, run_id=None):
        run_id = run_id or ("mt-%d" % int(time.time() * 1000))
        prog = compile_timeline(recipe, self.w, self.h, run_id)
        devdir = "/data/local/tmp/mtinject-%s" % run_id
        # private shell-owned dir, unique run id (never reuse an existing dir)
        chk = self._shell("[ -d %s ] && echo EXISTS || echo OK" % devdir).strip()
        if chk != "OK":
            raise InjectError(f"run dir {devdir} already exists (run-id must be unique)")
        self._shell("mkdir -p %s && chmod 700 %s" % (devdir, devdir))
        # write program locally then push
        tmp = Path(tempfile.mkdtemp(prefix="mtinject_"))
        local_prog = tmp / "prog.json"
        local_prog.write_text(json.dumps(prog), encoding="utf-8")
        prev_rot = self._rotation_lock()
        result = dict(run_id=run_id, devdir=devdir, jar_sha256=self.jar_sha)
        original_error = None
        try:
            self._push_verify(self.jar, devdir, "mtinject.jar")
            self._push_verify(local_prog, devdir, "prog.json")
            out = self._shell(
                "CLASSPATH=%s/mtinject.jar app_process /system/bin "
                "com.cykagent.mtinject.Main timeline %s/prog.json" % (devdir, devdir),
                timeout=int(sum(f["delay_ms"] for f in prog["frames"]) / 1000) + 30)
            mt = self._parse_mtresult(out)
            result["mtresult"] = mt
            if mt is None:
                raise InjectError("no MTRESULT in injector output")
            if not mt.get("all_ok"):
                raise InjectError(f"injector all_ok=false: {mt.get('error')}")
            if not mt.get("cleanup_ok"):
                raise InjectError("injector cleanup_ok=false")
            if mt.get("mode") != 2:
                raise InjectError(f"injector mode {mt.get('mode')} != 2")
            result["ok"] = True
        except Exception as e:
            original_error = e
            result["ok"] = False
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            # independent release (superset of declared DOWN ids) -- covers
            # child SIGKILL where the JVM finally may not have run.
            try:
                rel = self._independent_release(prog, devdir)
                result["independent_release"] = rel
                if not rel.get("cleanup_ok", False):
                    result["ok"] = False
                    result["release_error"] = "independent release cleanup_ok=false"
            except Exception as re:
                result["ok"] = False
                result["release_error"] = f"{type(re).__name__}: {re}"
            self._rotation_restore(prev_rot)
            self._shell("rm -rf %s" % devdir)
            shutil.rmtree(tmp, ignore_errors=True)
        if original_error is not None and not result.get("ok"):
            result["original_error"] = str(original_error)
        return result

    def _push_verify(self, local: Path, devdir, name):
        out = self.dev.adb("push", str(local), "%s/%s" % (devdir, name))
        if "1 file pushed" not in out:
            raise InjectError(f"push {name} failed: {out[-200:]}")
        want = hashlib.sha256(local.read_bytes()).hexdigest()
        got = self._shell("sha256sum %s/%s" % (devdir, name)).split()[0]
        if got != want:
            raise InjectError(f"device sha256 mismatch for {name}: {got} != {want}")

    def _independent_release(self, prog, devdir):
        down_ids = declared_down_ids(prog)
        coords = last_coords(prog)
        rel = {"pointers": [{"id": i, "x": coords[i][0], "y": coords[i][1]} for i in down_ids]}
        tmp = Path(tempfile.mkdtemp(prefix="mtrel_"))
        lp = tmp / "rel.json"
        lp.write_text(json.dumps(rel), encoding="utf-8")
        try:
            self._push_verify(lp, devdir, "rel.json")
            out = self._shell(
                "CLASSPATH=%s/mtinject.jar app_process /system/bin "
                "com.cykagent.mtinject.Main release %s/rel.json" % (devdir, devdir))
            mt = self._parse_mtresult(out)
            return mt or {"cleanup_ok": False, "error": "no MTRESULT"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _parse_mtresult(out):
        for l in out.splitlines():
            if l.startswith("MTRESULT "):
                try:
                    return json.loads(l[len("MTRESULT "):])
                except Exception:
                    return None
        return None