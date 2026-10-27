#!/usr/bin/env python3
"""Stage 8: publish the verified bundle to Nexus without rebuilding it.

Uploads the exact jar that verify_bundle.py checked (its SHA-256 is compared with the
verification report) together with the POM it checked (the one finalize_bundle_pom.py wrote,
or Shade's dependency-reduced POM when that step did not run) and, when present, the sources
jar, through maven-deploy-plugin's deploy-file goal. Refuses to publish when the verification
report is missing, failed, or describes different bytes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, read_json, sha256_file, utc_now_iso, which, write_json  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json")
    ap.add_argument("--verification", default="out/bundle-verification.json")
    ap.add_argument("--settings", default="", help="Maven settings.xml with the server credentials")
    ap.add_argument("--release-url", default="")
    ap.add_argument("--snapshot-url", default="")
    ap.add_argument("--server-id", default="nexus")
    ap.add_argument("--local-repo", default="", help="maven.repo.local used by the build")
    ap.add_argument("--deploy-plugin-version", default="3.1.2")
    ap.add_argument("--dry-run", action="store_true", help="print the command, upload nothing")
    ap.add_argument("--out", default="out/publish.json")
    args = ap.parse_args()

    man = read_json(args.manifest)
    if not Path(args.verification).is_file():
        fail("verification report %s not found; run verify_bundle.py first" % args.verification)
    ver = read_json(args.verification)
    if not ver.get("ok"):
        fail("bundle verification did not pass; refusing to publish")

    jar = Path(ver.get("bundle") or man["expected_jar"])
    if not jar.is_file():
        fail("bundle jar not found: %s" % jar)
    sha = sha256_file(jar)
    if sha != ver.get("sha256"):
        fail("%s changed since verification (sha256 %s, verified %s)" % (jar.name, sha, ver.get("sha256")))
    pom = Path(ver.get("pom") or man.get("pom") or "")
    if not str(pom) or not pom.is_file():
        pom = Path(man["reduced_pom"])
        log("WARN: publishing Shade's dependency-reduced POM as is (finalize_bundle_pom.py did not run)")
    if not pom.is_file():
        fail("POM not found: %s" % pom)

    version = man["bundle"]["version"]
    snapshot = version.endswith("-SNAPSHOT")
    url = args.snapshot_url if snapshot else args.release_url
    if not url:
        fail("no %s repository URL configured" % ("snapshot" if snapshot else "release"))
    sources = jar.with_name(jar.stem + "-sources.jar")
    sources_path = sources if sources.is_file() else None

    mvn = which("mvn") or "mvn"
    cmd = [mvn, "-B", "-ntp"]
    if args.settings:
        cmd += ["-s", args.settings]
    if args.local_repo:
        cmd.append("-Dmaven.repo.local=%s" % args.local_repo)
    cmd += [
        "org.apache.maven.plugins:maven-deploy-plugin:%s:deploy-file" % args.deploy_plugin_version,
        "-Dfile=%s" % jar,
        "-DpomFile=%s" % pom,
        "-Durl=%s" % url,
        "-DrepositoryId=%s" % args.server_id,
    ]
    if sources_path:
        cmd.append("-Dsources=%s" % sources_path)

    result = {
        "coordinate": man["coordinate"],
        "url": url,
        "repository_id": args.server_id,
        "jar": str(jar),
        "sha256": sha,
        "pom": str(pom),
        "sources": str(sources_path) if sources_path else None,
        "command": cmd,
        "dry_run": args.dry_run,
        "published": False,
        "at": utc_now_iso(),
    }
    log("Publishing %s to %s" % (man["coordinate"], url))
    log("  $ " + " ".join(cmd))
    if args.dry_run:
        write_json(args.out, result)
        log("Dry run: nothing uploaded")
        return
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        write_json(args.out, result)
        fail("deploy-file failed with exit code %d" % rc, 1)
    result["published"] = True
    write_json(args.out, result)
    log("Published %s (sha256 %s)" % (man["coordinate"], sha))


if __name__ == "__main__":
    main()
