"""Scaffold the next numbered game repo in the cykgames org.

Usage:
  python new_game.py <slug> [--local DIR] [--private] [--desc "..."]

Requires GH_TOKEN env (repo scope). Creates NNN-<slug> with the next free
serial, then optionally initializes DIR as a local starter repo (README +
.gitignore) and pushes it to the new remote.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ORG = "cykgames"
API = "https://api.github.com"

README_TMPL = """# {num} · {title}

Status: prototype
Engine: TBD
Platform: TBD

Part of the [cykgames](https://github.com/cykgames) catalog.
"""
GITIGNORE = ".godot/\nbuild/\n*.tmp\n"


def api(method, url, body=None, token=None):
    headers = {"User-Agent": "cykagent", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def next_number(token):
    nums, page = [], 1
    while True:
        st, repos = api("GET", f"{API}/orgs/{ORG}/repos?per_page=100&page={page}",
                        token=token)
        if st != 200 or not repos:
            break
        for r in repos:
            m = re.match(r"^(\d{3})-", r["name"])
            if m:
                nums.append(int(m.group(1)))
        page += 1
    return max(nums, default=0) + 1


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="short game slug, e.g. space-runner")
    ap.add_argument("--local", help="directory to scaffold + push (created if missing)")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--desc", default="")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        sys.exit("slug must be lowercase letters/digits/hyphens")
    token = os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("GH_TOKEN env not set")

    num = next_number(token)
    name = f"{num:03d}-{args.slug}"
    st, d = api("POST", f"{API}/orgs/{ORG}/repos", {
        "name": name,
        "description": args.desc or f"{num:03d} - {args.slug} (cykgames)",
        "private": args.private, "has_issues": True, "has_wiki": False,
        "auto_init": False,
    }, token=token)
    if st != 201:
        sys.exit(f"create failed {st}: {d.get('message')}")
    url = d["html_url"]
    print("created:", url)

    if args.local:
        loc = Path(args.local).expanduser().resolve()
        loc.mkdir(parents=True, exist_ok=True)
        git(loc, "init", "-b", "main")
        (loc / "README.md").write_text(
            README_TMPL.format(num=f"{num:03d}", title=args.slug.replace("-", " ").title()),
            encoding="utf-8")
        (loc / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        git(loc, "add", "-A")
        git(loc, "commit", "-m", f"{name}: scaffold")
        git(loc, "remote", "add", "origin", f"https://github.com/{ORG}/{name}.git")
        git(loc, "push", "-u", "origin", "main")
        print("scaffolded + pushed:", loc)
    disp_title = args.slug.replace("-", " ").title()
    print(f"display name: when the Godot project exists, set config/name and")
    print(f"  export_presets.cfg package/name to '{num:03d} {disp_title}'")
    print(f"  (serial prefix; keep package/unique_name '{args.slug}' stable, no serial)")
    print("next: update the catalog in cykgames/.github profile/README.md")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
