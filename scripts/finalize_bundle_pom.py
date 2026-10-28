#!/usr/bin/env python3
"""Stage 5b: derive the POM that is published with the bundle from Shade's dependency-reduced POM.

Shade removes the embedded coordinates from the POM and, with promoteTransitiveDependencies, turns
their transitive dependencies into direct dependencies of the bundle. That promotion follows the
components' own POMs, not the application: a dependency the application had excluded (Spring Boot's
starter excludes tomcat-annotations-api from tomcat-embed-core, for example) is promoted all the
same and would enter every consumer through the bundle, ahead of jakarta.annotation-api on the
classpath. This step keeps only the promoted dependencies whose groupId:artifactId the source
application contained, at the application's version, and drops the rest. The result is the POM
that verify_bundle.py checks and publish_bundle.py uploads.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pomedit  # noqa: E402
from bundlelib import fail, ga_str, log, read_json, write_json  # noqa: E402


def dependency_record(dep, src: str, project_group) -> dict:
    g, a = pomedit.dependency_ga(dep, src, project_group)
    return {"groupId": g, "artifactId": a, "version": dep.child_text(src, "version"),
            "scope": dep.child_text(src, "scope") or "compile"}


def finalize(src: str, embedded_ga: set, app_versions: dict, prune_unknown: bool = True, prune_embedded: bool = False) -> tuple:
    """Returns (new source, kept, pruned, aligned, embedded still declared, new, pruned because embedded).

    The main job prunes promoted dependencies the application never had (prune_unknown) and leaves an embedded
    coordinate visible so verify_bundle.py fails on it. The rework job keeps the dependencies promoted from the
    added jars' POMs (they are reported as new) and prunes an embedded coordinate, which the added jar's POM may
    well declare although the bundle holds its classes."""
    root = pomedit.parse(src)
    project_group = pomedit.project_group_id(root, src)
    kept, pruned, aligned, still, new, pruned_embedded = [], [], [], [], [], []
    handled = set()
    while True:
        root = pomedit.parse(src)
        deps = root.child("dependencies")
        action = None
        for dep in (deps.children_named("dependency") if deps is not None else []):
            rec = dependency_record(dep, src, project_group)
            ga = "%s:%s" % (rec["groupId"], rec["artifactId"])
            if ga in handled:
                continue
            if ga in embedded_ga:
                if prune_embedded:
                    action = ("prune-embedded", dep, rec, ga)
                    break
                handled.add(ga)
                still.append(rec)  # verify_bundle.py fails on these; they stay visible in the POM
                continue
            if not app_versions or ga not in app_versions:
                if app_versions and prune_unknown:
                    action = ("prune", dep, rec, ga)
                    break
                handled.add(ga)
                kept.append(rec)
                if not prune_unknown:
                    new.append(rec)
                continue
            version_el = dep.child("version")
            app_version = app_versions[ga]
            if version_el is not None and app_version and version_el.text(src) != app_version:
                action = ("align", dep, rec, ga, version_el, app_version)
                break
            handled.add(ga)
            kept.append(rec)
        if action is None:
            break
        if action[0] == "prune":
            _, dep, rec, ga = action
            src = pomedit.remove_element(src, dep)
            pruned.append(rec)
        elif action[0] == "prune-embedded":
            _, dep, rec, ga = action
            src = pomedit.remove_element(src, dep)
            pruned_embedded.append(rec)
        else:
            _, dep, rec, ga, version_el, app_version = action
            rec["version_in_reduced_pom"] = rec["version"]
            rec["version"] = app_version
            src = src[:version_el.inner_start] + app_version + src[version_el.inner_end:]
            aligned.append(rec)
            kept.append(rec)
            handled.add(ga)
    return src, kept, pruned, aligned, still, new, pruned_embedded


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json", help="written by gen_bundle_project.py")
    ap.add_argument("--reduced-pom", default="", help="Shade's dependency-reduced POM (default: manifest.reduced_pom)")
    ap.add_argument("--out", default="", help="POM to publish (default: manifest.pom)")
    ap.add_argument("--report", default="out/bundle-pom.json")
    args = ap.parse_args()

    man = read_json(args.manifest)
    reduced = Path(args.reduced_pom or man.get("reduced_pom") or "")
    out = Path(args.out or man.get("pom") or "")
    if not str(reduced) or not reduced.is_file():
        fail("dependency-reduced POM not found: %s (run the Shade build first)" % reduced)
    if not str(out):
        fail("--out is required when the manifest has no 'pom' entry")

    embedded_ga = {ga_str(c["gav"]) for c in man["components"]}
    app_versions = {}
    for g in man.get("application_coordinates") or []:
        app_versions.setdefault(ga_str(g), g.get("version"))

    # rework job: the known versions are the previous bundle POM's; dependencies the added jars' POMs bring are kept
    # and reported, an embedded coordinate they declare is pruned
    rework = man.get("rework")
    src = reduced.read_text(encoding="utf-8")
    src, kept, pruned, aligned, still, new, pruned_embedded = finalize(src, embedded_ga, app_versions,
                                                                         prune_unknown=not rework, prune_embedded=bool(rework))
    pomedit.parse(src)  # must still be well-formed
    ET.fromstring(src.encode("utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(src, encoding="utf-8")

    report = {
        "reduced_pom": str(reduced),
        "pom": str(out),
        "application_coordinates_known": bool(app_versions),
        "rework": bool(rework),
        "kept": kept,
        "new": new,
        "pruned": pruned,
        "pruned_embedded": pruned_embedded,
        "aligned": aligned,
        "embedded_still_declared": still,
    }
    write_json(args.report, report)

    def fmt(d):
        return "%s:%s:%s%s" % (d["groupId"], d["artifactId"], d.get("version"), (" (%s)" % d["scope"]) if d.get("scope") not in (None, "compile") else "")

    log("Published POM written to %s" % out)
    log("  kept    : %s" % (", ".join(fmt(d) for d in kept if d not in new) if [d for d in kept if d not in new] else "no promoted dependencies"))
    if new:
        log("  new     : %s (promoted from the added jars' POMs; make sure the consumer has or wants them)" % ", ".join(fmt(d) for d in new))
    if pruned:
        log("  pruned  : %s (not in the application: Shade promoted them from the components' own POMs)" % ", ".join(fmt(d) for d in pruned))
    if pruned_embedded:
        log("  pruned  : %s (declared by an added jar's POM but embedded in the bundle)" % ", ".join(fmt(d) for d in pruned_embedded))
    for d in aligned:
        log("  aligned : %s:%s %s -> %s (%s)" % (d["groupId"], d["artifactId"], d["version_in_reduced_pom"], d["version"],
                                                "the previous bundle POM's version" if rework else "the application's version"))
    if still:
        log("  WARN: embedded coordinates still declared, verify_bundle.py will fail: %s" % ", ".join(fmt(d) for d in still))
    if not app_versions and not rework:
        log("  WARN: the manifest carries no application coordinates (inventory missing when the project was generated); nothing was pruned")


if __name__ == "__main__":
    main()
