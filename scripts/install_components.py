#!/usr/bin/env python3
"""Stage 4b: install every component of the bundle into the build's local Maven repository.

The files come from the application itself, so the Shade build embeds exactly the bytes that run
in production, whatever the repository holds under those coordinates, and whether it holds them
at all. With --repo-url the real POM of each identified component is fetched and installed next
to the jar, which keeps its declared dependencies for the dependency-reduced POM; otherwise a
minimal POM is generated. Jars under synthetic coordinates always get a generated POM.
--local-repo points the install (and must point the following build) at a job-local repository,
so nothing is written into the agent's shared ~/.m2.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, read_json, which  # noqa: E402
from identify import Repository  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json")
    ap.add_argument("--settings", default="")
    ap.add_argument("--repo-url", default="", help="repository to fetch the components' real POMs from")
    ap.add_argument("--local-repo", default="", help="maven.repo.local for the install and the build")
    ap.add_argument("--install-plugin-version", default="3.1.2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    man = read_json(args.manifest)
    items = man.get("install") or man.get("unidentified") or []
    if not items:
        log("Nothing to install")
        return
    repo = Repository(args.repo_url) if args.repo_url else None
    mvn = which("mvn") or "mvn"
    with_pom = 0
    for item in items:
        gav = item["gav"]
        pom_file = None
        if repo is not None and not item.get("synthetic"):
            text = repo.pom(gav["groupId"], gav["artifactId"], gav["version"])
            if text:
                pom_file = Path(item["file"]).with_suffix(".pom")
                pom_file.write_text(text, encoding="utf-8")
                with_pom += 1
        cmd = [mvn, "-B", "-ntp"]
        if args.settings:
            cmd += ["-s", args.settings]
        if args.local_repo:
            cmd.append("-Dmaven.repo.local=%s" % args.local_repo)
        cmd += [
            "org.apache.maven.plugins:maven-install-plugin:%s:install-file" % args.install_plugin_version,
            "-Dfile=%s" % item["file"],
            "-DgroupId=%s" % gav["groupId"],
            "-DartifactId=%s" % gav["artifactId"],
            "-Dversion=%s" % gav["version"],
            "-Dpackaging=jar",
        ]
        if gav.get("classifier"):
            cmd.append("-Dclassifier=%s" % gav["classifier"])
        cmd.append("-DpomFile=%s" % pom_file if pom_file else "-DgeneratePom=true")
        log("Installing %s as %s:%s:%s%s" % (Path(item["file"]).name, gav["groupId"], gav["artifactId"], gav["version"],
                                             " (with its POM)" if pom_file else " (generated POM)"))
        log("  $ " + " ".join(cmd))
        if args.dry_run:
            continue
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            fail("install-file failed with exit code %d" % rc, 1)
    log("%d component(s) %s, %d with their real POM" % (len(items), "would be installed (dry run)" if args.dry_run else "installed", with_pom))


if __name__ == "__main__":
    main()
