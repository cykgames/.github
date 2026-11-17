"""W1 stability sweeps: 1 clean + 3 jittered runs of the 4 L1 shards (16 levels).
Jitter is planner-only (start offset / decision spacing / seed); game physics
untouched (LevelGen zero RNG + fixed-fps). Each sweep's jitter params are
recorded in the evidence. Every shard asserts save-hash unchanged internally.
Usage: python l1_sweeps.py [repo_dir] [out_json]
"""
import subprocess, json, sys, time
from pathlib import Path

HOME = Path.home()
GODOT = HOME / "tools" / "godot-4.7.2" / "Godot_v4.7.2-stable_win64_console.exe"
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else HOME / "projects" / "002-obby"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("l1_sweeps_evidence.json")
SHARDS = ["test_levels_l1_s0", "test_levels_l1_s1", "test_levels_l1_s2", "test_levels_l1_s3"]
SWEEPS = [
    dict(sweep=0, seed=0, start_offset=0, decision_jitter=0),   # clean
    dict(sweep=1, seed=1, start_offset=5, decision_jitter=1),
    dict(sweep=2, seed=2, start_offset=11, decision_jitter=2),
    dict(sweep=3, seed=3, start_offset=3, decision_jitter=3),
]

def run_shard(sc, params):
    cmd = [GODOT, "--headless", "--fixed-fps", "60", "--path", str(REPO),
           "res://tools/%s.tscn" % sc, "++"]
    cmd += ["%s=%d" % (k, v) for k, v in params.items()]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=400)
    wall = time.time() - t0
    t = p.stdout + p.stderr
    meta = None
    results = []
    for l in t.splitlines():
        if l.startswith("SHARD_META "):
            meta = json.loads(l[len("SHARD_META "):])
        elif l.startswith("LEVEL_RESULT "):
            results.append(json.loads(l[len("LEVEL_RESULT "):]))
    verdict = "SHARD_PASS" if "SHARD_PASS" in t else "SHARD_FAIL"
    return dict(scene=sc, rc=p.returncode, verdict=verdict, wall_s=round(wall, 2),
                meta=meta, results=results,
                fails=[l for l in t.splitlines() if "W1_FAIL" in l])

evidence = dict(repo=str(REPO), generated=time.strftime("%Y-%m-%dT%H:%M:%S"),
                fixed_fps=60, sweeps=[])
all_ok = True
for params in SWEEPS:
    sweep_rec = dict(params=params, shards=[])
    print("== sweep %d params=%s" % (params["sweep"], params))
    for sc in SHARDS:
        r = run_shard(sc, params)
        sweep_rec["shards"].append(r)
        ok = r["verdict"] == "SHARD_PASS" and r["rc"] == 0
        all_ok = all_ok and ok
        lv = ", ".join("L%d:%s" % (x["level"], x["result"]) for x in r["results"])
        print("   %s %s wall=%.1fs | %s" % (sc, r["verdict"], r["wall_s"], lv))
        for f in r["fails"]:
            print("      ", f)
    levels = [x for s in sweep_rec["shards"] for x in s["results"]]
    sweep_rec["level_count"] = len(levels)
    sweep_rec["all_pass"] = all(x["result"].endswith("PASS") for x in levels)
    sweep_rec["falls_total"] = sum(x["falls"] for x in levels)
    evidence["sweeps"].append(sweep_rec)
evidence["all_sweeps_pass"] = all_ok
evidence["clean_sweep_levels"] = evidence["sweeps"][0]["level_count"]
OUT.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print("ALL_SWEEPS_PASS", all_ok, "->", OUT)
sys.exit(0 if all_ok else 1)