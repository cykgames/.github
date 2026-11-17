"""Canonical production build of mtinject.jar (classes.dex) for the B'
app_process transport (cykgames W2a, plan v4.17). PURE build: no device serial,
no push (deployment is the host driver's job, mt_inject.py). SDK/JDK paths are
configurable via env (ANDROID_SDK / JAVA_HOME) or flags.

Output: tools/mtinject/mtinject.jar + printed sha256/bytes (the host driver
records jar sha256 + source commit in evidence and verifies the device copy).

Usage: python tools/mtinject/build_jar.py [--sdk C:\\Android] [--jdk <bin>]
Author: cykbot9 (AI agent).
"""
import argparse
import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(cmd):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SystemExit(f"FAILED rc={p.returncode}: {' '.join(str(c) for c in cmd)}\n"
                         f"{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk", default=os.environ.get("ANDROID_SDK", r"C:\Android"))
    ap.add_argument("--jdk", default=os.environ.get("JAVA_HOME",
                    r"C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot"))
    ap.add_argument("--bt-version", default="36.0.0")
    ap.add_argument("--platform", default="android-36")
    args = ap.parse_args()

    sdk = Path(args.sdk)
    bt = sdk / "build-tools" / args.bt_version
    android_jar = sdk / "platforms" / args.platform / "android.jar"
    jdk = Path(args.jdk)
    javac = jdk / "bin" / "javac.exe"
    if not javac.exists():
        javac = jdk / "javac.exe"
    if not android_jar.exists():
        raise SystemExit(f"android.jar missing: {android_jar}")

    classes = ROOT / "classes"
    dexout = ROOT / "dexout"
    classes.mkdir(exist_ok=True)
    dexout.mkdir(exist_ok=True)
    src = ROOT / "src" / "com" / "cykagent" / "mtinject" / "Main.java"
    run([javac, "-source", "8", "-target", "8", "-bootclasspath", android_jar,
         "-classpath", android_jar, "-d", classes, src])
    run(["cmd", "/c", bt / "d8.bat", "--release", "--lib", android_jar,
         "--output", dexout, *classes.rglob("*.class")])
    jar = ROOT / "mtinject.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(dexout / "classes.dex", "classes.dex")
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    print("JAR_BYTES", jar.stat().st_size)
    print("JAR_SHA256", digest)
    print("JAR_PATH", jar)


if __name__ == "__main__":
    main()