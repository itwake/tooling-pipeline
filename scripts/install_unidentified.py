#!/usr/bin/env python3
"""Stage 4b: make jars without known coordinates resolvable for the bundle build.

Jars the inventory could not identify are embedded under synthetic coordinates
(rebundle.unidentified:<artifactId>:<version>-u<sha1>). Before the Shade build runs, this
installs the original jar files from the application into the local Maven repository under
those coordinates, so the generated project resolves them like any other dependency and no
mirror or remote repository is involved.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, read_json, which  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json")
    ap.add_argument("--settings", default="")
    ap.add_argument("--install-plugin-version", default="3.1.2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    man = read_json(args.manifest)
    items = man.get("unidentified", [])
    if not items:
        log("No unidentified jars; nothing to install")
        return
    mvn = which("mvn") or "mvn"
    for item in items:
        gav = item["gav"]
        cmd = [mvn, "-B", "-ntp"]
        if args.settings:
            cmd += ["-s", args.settings]
        cmd += [
            "org.apache.maven.plugins:maven-install-plugin:%s:install-file" % args.install_plugin_version,
            "-Dfile=%s" % item["file"],
            "-DgroupId=%s" % gav["groupId"],
            "-DartifactId=%s" % gav["artifactId"],
            "-Dversion=%s" % gav["version"],
            "-Dpackaging=jar",
        ]
        log("Installing %s as %s:%s:%s" % (Path(item["file"]).name, gav["groupId"], gav["artifactId"], gav["version"]))
        log("  $ " + " ".join(cmd))
        if args.dry_run:
            continue
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            fail("install-file failed with exit code %d" % rc, 1)
    log("%d unidentified jar(s) %s" % (len(items), "would be installed (dry run)" if args.dry_run else "installed"))


if __name__ == "__main__":
    main()
