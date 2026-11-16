#!/usr/bin/env python3
"""One-shot versionCode migration for the 9 cykgames repos.

Old formula major*100+minor*10+patch collides (0.1.10 == 0.2.0 == 20).
New formula (release.py v3 `version_code`): major*10000+minor*100+patch,
collision-free while minor,patch < 100. This script recomputes every repo's
export_presets.cfg code WITHOUT touching version/name, verifies the new code
is strictly greater than the old one (Android only accepts upgrades), records
the old->new mapping, and optionally commits each repo.

Usage:
  python tools/migrate_version_codes.py            # dry run, print mapping
  python tools/migrate_version_codes.py --apply    # rewrite cfg files
  python tools/migrate_version_codes.py --apply --commit
Mapping record: tools/migration_version_codes_20261116.json (committed).
Author: cykbot9 (AI agent).
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / "projects"
REPOS = ["001-parkour", "002-obby", "003-outpost", "004-match3", "005-marble",
         "006-tomb", "007-merge", "008-shmup", "009-shooter"]
RECORD = Path(__file__).with_name("migration_version_codes_20261116.json")


def new_code(major, minor, patch):
    if not (0 <= minor < 100 and 0 <= patch < 100):
        raise SystemExit(f"overflow: {major}.{minor}.{patch}")
    return major * 10000 + minor * 100 + patch


def run(cmd, cwd):
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if args.commit and not args.apply:
        ap.error("--commit requires --apply")

    mapping = []
    for repo in REPOS:
        cfg_path = PROJECTS / repo / "export_presets.cfg"
        cfg = cfg_path.read_text(encoding="utf-8", errors="replace")
        name = re.search(r'^version/name="([^"]+)"', cfg, re.M).group(1)
        old = int(re.search(r'^version/code=(\d+)', cfg, re.M).group(1))
        major, minor, patch = (int(x) for x in name.split("."))
        new = new_code(major, minor, patch)
        if new <= old:
            raise SystemExit(f"{repo}: new code {new} <= old {old} -- "
                             "Android would reject the upgrade; bump first")
        mapping.append(dict(repo=repo, version=name, old_code=old, new_code=new))
        print(f"{repo}: {name} code {old} -> {new}")
        if args.apply:
            out = re.sub(r'^version/code=\d+', f"version/code={new}", cfg,
                         count=1, flags=re.M)
            cfg_path.write_text(out, encoding="utf-8")
            got = int(re.search(r'^version/code=(\d+)',
                                cfg_path.read_text(encoding="utf-8"), re.M).group(1))
            if got != new:
                raise SystemExit(f"{repo}: rewrite did not stick ({got})")
            if args.commit:
                rc, o = run(["git", "add", "export_presets.cfg"], PROJECTS / repo)
                if rc != 0:
                    raise SystemExit(f"{repo}: git add failed {o}")
                msg = (f"chore(version): migrate versionCode to "
                       f"major*10000+minor*100+patch ({old} -> {new})")
                rc, o = run(["git", "commit", "-m", msg], PROJECTS / repo)
                if rc != 0 and "nothing to commit" not in o:
                    raise SystemExit(f"{repo}: commit failed {o}")
                print(f"  committed: {o.strip().splitlines()[0] if o.strip() else '(nothing)'}")

    record = dict(date=datetime.now().isoformat(timespec="seconds"),
                  formula="major*10000+minor*100+patch",
                  reason="old major*100+minor*10+patch collides (0.1.10==0.2.0==20)",
                  applied=bool(args.apply), committed=bool(args.commit),
                  mapping=mapping)
    if args.apply:
        RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"mapping recorded -> {RECORD}")
    print("DRY RUN (no files written)" if not args.apply else "APPLIED")


if __name__ == "__main__":
    main()