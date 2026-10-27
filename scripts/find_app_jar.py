#!/usr/bin/env python3
"""Locate the Spring Boot archive a consumer build produced, and the artifact to compare it with.

Scans <project>/**/target/*.jar|*.war for Boot archives (BOOT-INF/lib or WEB-INF/lib) and prefers
the one that contains a bundle jar (manifest attribute X-Dependency-Bundle-Contents). When the
bundle manifest of the mvn-rebundle build is available, the reference of the artifact the bundle
was built from is reported as the thing to compare with.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import AppArtifact, BUNDLE_CONTENTS_ATTR, fail, log, read_json, read_manifest, write_json  # noqa: E402

SKIP_SUFFIXES = ("-sources.jar", "-javadoc.jar", "-tests.jar", ".jar.original", ".war.original")


def contains_bundle(app: AppArtifact, artifact_id: str) -> bool:
    for dj in app.dependency_jars():
        if artifact_id and not dj.name.startswith(artifact_id + "-"):
            continue
        try:
            with dj.open() as zf:
                if read_manifest(zf).get(BUNDLE_CONTENTS_ATTR):
                    return True
        except zipfile.BadZipFile:
            continue
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="checkout of the consumer project after the build")
    ap.add_argument("--manifest", default="out/bundle-manifest.json", help="bundle manifest (for the original artifact reference)")
    ap.add_argument("--bundle-artifact-id", default="", help="restrict bundle detection to this artifactId")
    ap.add_argument("--out", default="out/rebuilt-app.json")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    candidates, chosen = [], None
    for p in sorted(project.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".jar", ".war") or "target" not in p.parts:
            continue
        if any(p.name.endswith(s) for s in SKIP_SUFFIXES):
            continue
        try:
            app = AppArtifact(p)
        except (ValueError, zipfile.BadZipFile):
            continue
        if app.layout not in ("boot-jar", "boot-war"):
            app.close()
            continue
        has_bundle = contains_bundle(app, args.bundle_artifact_id)
        app.close()
        candidates.append({"path": str(p), "layout": app.layout, "contains_bundle": has_bundle})
        if has_bundle and chosen is None:
            chosen = str(p)
    if chosen is None and len(candidates) == 1:
        chosen = candidates[0]["path"]

    old_ref, old_type = None, None
    manifest_path = Path(args.manifest)
    if manifest_path.is_file():
        source = read_json(manifest_path).get("source") or {}
        if source.get("ref"):
            old_type = source.get("type")
            old_ref = source["ref"]
            if old_type == "image" and source.get("app_path"):
                old_ref = "%s#%s" % (old_ref, source["app_path"])
            elif old_type in ("file", "jar", "dir"):
                old_ref = source.get("local_path") or source["ref"]

    result = {"new_jar": chosen, "candidates": candidates, "old_ref": old_ref, "old_type": old_type}
    write_json(args.out, result)
    if chosen is None:
        fail("no Spring Boot archive containing the bundle was found under %s (candidates: %s)" % (
            project, ", ".join(c["path"] for c in candidates) or "none"))
    log("Rebuilt application: %s" % chosen)
    log("Compare with: %s" % (old_ref or "(unknown; pass VERIFY_AGAINST)"))


if __name__ == "__main__":
    main()
