#!/usr/bin/env python3
"""The transition between two versions of a bundle, and the version a consumer project uses today.

A bundle is self-describing: its manifest lists the embedded coordinates (X-Dependency-Bundle-Contents), the Spring
Boot version and where it came from, its published POM the dependencies it promotes. Two such states, A (what the
consumer POM declares) and B (the target), give the transition the consumer job applies: coordinates added to the
bundle, removed from it, or embedded at another version, promoted dependencies that appeared or went, a Boot
version change. The first switch is the transition from nothing, leaving the bundle the one to nothing.

  --detect   the bundle version the consumer uses: the dependency on --bundle's groupId:artifactId in the effective
             POM (help:effective-pom), else the one the tool's record (.mvn/rebundle.json) names
  otherwise  the diff of --previous-jar [--previous-pom] and --manifest | --bundle-jar [--pom] | --unbundle
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    BUNDLE_BASE_ATTR,
    BUNDLE_BOOT_ATTR,
    BUNDLE_CONTENTS_ATTR,
    BUNDLE_REWORKED_FROM_ATTR,
    fail,
    ga_str,
    gav_str,
    identify_gav,
    log,
    parse_contents_attr,
    read_json,
    read_manifest,
    variant_key,
    write_json,
)


def local_tag(el) -> str:
    return el.tag.split("}", 1)[-1]


def pom_dependencies(path) -> List[dict]:
    """The dependencies a published bundle POM declares (groupId, artifactId, version, scope)."""
    deps: List[dict] = []
    if not path or not Path(path).is_file():
        return deps
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError):
        return deps
    for el in root:
        if local_tag(el) != "dependencies":
            continue
        for dep in el:
            if local_tag(dep) == "dependency":
                rec = {local_tag(c): (c.text or "").strip() for c in dep}
                deps.append({"groupId": rec.get("groupId"), "artifactId": rec.get("artifactId"), "version": rec.get("version"),
                             "scope": rec.get("scope") or "compile"})
    return deps


def state_from_jar(jar_path, pom_path=None) -> dict:
    """The state of a published bundle: coordinates, embedded coordinates, promoted dependencies, Boot version."""
    with zipfile.ZipFile(str(jar_path)) as zf:
        attrs = read_manifest(zf)
        gav, _, _ = identify_gav(zf, Path(str(jar_path)).name)
    contents = parse_contents_attr(attrs.get(BUNDLE_CONTENTS_ATTR, ""))
    if not gav or not contents:
        fail("%s is not an mvn-rebundle bundle (needs its own pom.properties and the %s manifest attribute)" % (jar_path, BUNDLE_CONTENTS_ATTR))
    return {"bundle": gav, "coordinate": gav_str(gav), "components": contents, "pom_dependencies": pom_dependencies(pom_path),
            "spring_boot_version": attrs.get(BUNDLE_BOOT_ATTR), "base": attrs.get(BUNDLE_BASE_ATTR), "reworked_from": attrs.get(BUNDLE_REWORKED_FROM_ATTR)}


def state_from_manifest(man: dict, pom_path=None) -> dict:
    """The same state from a bundle-manifest.json (an upstream build, or gen_consumer_patch's manifest dict)."""
    candidates = [pom_path, man.get("pom"), man.get("reduced_pom")]
    pom = next((c for c in candidates if c and Path(c).is_file()), None)
    return {"bundle": man["bundle"], "coordinate": man["coordinate"], "components": [c["gav"] for c in man["components"]],
            "pom_dependencies": pom_dependencies(pom) if pom else list(man.get("promoted_dependencies") or []),
            "spring_boot_version": man.get("spring_boot_version"), "base": None, "reworked_from": (man.get("rework") or {}).get("from")}


def empty_state() -> dict:
    return {"bundle": None, "coordinate": None, "components": [], "pom_dependencies": [], "spring_boot_version": None, "base": None, "reworked_from": None}


def diff(prev: dict, new: dict) -> dict:
    """What changes for a consumer going from prev to new (either may be the empty state)."""
    old = OrderedDict((variant_key(g), g) for g in prev.get("components") or [])
    cur = OrderedDict((variant_key(g), g) for g in new.get("components") or [])
    added = [g for k, g in cur.items() if k not in old]
    removed = [g for k, g in old.items() if k not in cur]
    replaced = [{"old": old[k], "new": g} for k, g in cur.items() if k in old and old[k].get("version") != g.get("version")]
    unchanged = [k for k in cur if k in old and old[k].get("version") == cur[k].get("version")]
    old_pom = {"%s:%s" % (d.get("groupId"), d.get("artifactId")): d for d in prev.get("pom_dependencies") or []}
    new_pom = {"%s:%s" % (d.get("groupId"), d.get("artifactId")): d for d in new.get("pom_dependencies") or []}
    return {
        "from": prev.get("coordinate"),
        "to": new.get("coordinate"),
        "kind": "switch" if not prev.get("coordinate") else ("unbundle" if not new.get("coordinate") else "upgrade"),
        "added": added,
        "removed": removed,
        "replaced": replaced,
        "unchanged": len(unchanged),
        "pom_added": [d for ga, d in new_pom.items() if ga not in old_pom],
        "pom_removed": [d for ga, d in old_pom.items() if ga not in new_pom],
        "spring_boot": {"from": prev.get("spring_boot_version"), "to": new.get("spring_boot_version")},
    }


def describe(t: dict) -> List[str]:
    lines = []
    if t["added"]:
        lines.append("added to the bundle: " + ", ".join(gav_str(g) for g in t["added"]))
    if t["removed"]:
        lines.append("removed from the bundle: " + ", ".join(gav_str(g) for g in t["removed"]))
    if t["replaced"]:
        lines.append("embedded at another version: " + ", ".join("%s %s -> %s" % (ga_str(r["new"]), r["old"].get("version"), r["new"].get("version")) for r in t["replaced"]))
    if t["pom_added"]:
        lines.append("new dependencies of the bundle POM: " + ", ".join("%s:%s:%s" % (d.get("groupId"), d.get("artifactId"), d.get("version")) for d in t["pom_added"]))
    if t["pom_removed"]:
        lines.append("dependencies the bundle POM no longer declares: " + ", ".join("%s:%s" % (d.get("groupId"), d.get("artifactId")) for d in t["pom_removed"]))
    sb = t["spring_boot"]
    if sb["from"] and sb["to"] and sb["from"] != sb["to"]:
        lines.append("Spring Boot %s -> %s" % (sb["from"], sb["to"]))
    return lines


def detect_current(effective_pom, bundle_ga: str, record_path=None) -> Optional[dict]:
    """The bundle version a consumer uses: {"coordinate", "source", "modules"} or None."""
    g, a = bundle_ga.split(":", 1)
    modules: Dict[str, str] = OrderedDict()
    if effective_pom and Path(effective_pom).is_file():
        try:
            root = ET.parse(str(effective_pom)).getroot()
        except ET.ParseError as e:
            fail("%s cannot be parsed: %s" % (effective_pom, e))
        projects = [root] if local_tag(root) == "project" else [p for p in root if local_tag(p) == "project"]
        for proj in projects:
            fields = {local_tag(c): (c.text or "").strip() for c in proj if local_tag(c) in ("groupId", "artifactId")}
            parent = next((c for c in proj if local_tag(c) == "parent"), None)
            if "groupId" not in fields and parent is not None:
                fields["groupId"] = next(((c.text or "").strip() for c in parent if local_tag(c) == "groupId"), "")
            name = "%s:%s" % (fields.get("groupId"), fields.get("artifactId"))
            deps = next((c for c in proj if local_tag(c) == "dependencies"), None)
            for dep in (deps if deps is not None else []):
                rec = {local_tag(c): (c.text or "").strip() for c in dep}
                if rec.get("groupId") == g and rec.get("artifactId") == a and rec.get("version"):
                    modules[name] = rec["version"]
    if modules:
        versions = sorted(set(modules.values()))
        if len(versions) > 1:
            fail("the modules use different versions of %s: %s; align them before the transition" % (bundle_ga, ", ".join("%s=%s" % kv for kv in modules.items())))
        return {"coordinate": "%s:%s" % (bundle_ga, versions[0]), "source": "effective POM", "modules": list(modules)}
    if record_path and Path(record_path).is_file():
        rec = read_json(record_path)
        if rec.get("bundle") and rec["bundle"].startswith(bundle_ga + ":"):
            return {"coordinate": rec["bundle"], "source": "record %s" % record_path, "modules": list((rec.get("modules") or {}).keys())}
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detect", action="store_true", help="find the bundle version the consumer uses instead of diffing")
    ap.add_argument("--effective-pom", default="", help="--detect: help:effective-pom output of the consumer project")
    ap.add_argument("--bundle", default="", help="--detect: groupId:artifactId[:version] of the bundle (the version is ignored)")
    ap.add_argument("--manifest", default="", help="--detect: bundle-manifest.json naming the bundle; otherwise the new bundle's manifest")
    ap.add_argument("--record", default="", help="--detect: the consumer's .mvn/rebundle.json, consulted when the effective POM has no bundle dependency")
    ap.add_argument("--previous-jar", default="", help="the bundle the consumer uses today")
    ap.add_argument("--previous-pom", default="", help="its published POM")
    ap.add_argument("--bundle-jar", default="", help="the target bundle (instead of --manifest)")
    ap.add_argument("--pom", default="", help="the target bundle's published POM")
    ap.add_argument("--unbundle", action="store_true", help="the target is no bundle at all")
    ap.add_argument("--out", default="out/bundle-transition.json")
    args = ap.parse_args()

    if args.detect:
        if args.bundle and args.bundle.strip().lower() == "none":
            rec = read_json(args.record) if args.record and Path(args.record).is_file() else {}
            if not rec.get("bundle") or rec["bundle"].count(":") < 2:
                fail("'none' alone needs the record %s to say which bundle the project leaves; use groupId:artifactId:none" % (args.record or ".mvn/rebundle.json"))
            ga = ":".join(rec["bundle"].split(":")[:2])
        elif args.bundle:
            parts = args.bundle.split(":")
            if len(parts) < 2 or not all(parts[:2]):
                fail("--bundle must be groupId:artifactId[:version]")
            ga = "%s:%s" % (parts[0], parts[1])
        elif args.manifest:
            b = read_json(args.manifest)["bundle"]
            ga = "%s:%s" % (b["groupId"], b["artifactId"])
        else:
            fail("--detect needs --bundle or --manifest to know which bundle to look for")
        current = detect_current(args.effective_pom, ga, args.record)
        write_json(args.out, {"bundle_ga": ga, "current": current})
        if current:
            log("The consumer uses %s (%s%s)" % (current["coordinate"], current["source"], (", modules " + ", ".join(current["modules"])) if current["modules"] else ""))
            print(current["coordinate"])
        else:
            log("The consumer does not use %s yet" % ga)
        return

    prev = state_from_jar(args.previous_jar, args.previous_pom) if args.previous_jar else empty_state()
    if args.unbundle:
        new = empty_state()
    elif args.bundle_jar:
        new = state_from_jar(args.bundle_jar, args.pom)
    elif args.manifest:
        new = state_from_manifest(read_json(args.manifest), args.pom)
    else:
        fail("the target is --manifest, --bundle-jar or --unbundle")
    t = diff(prev, new)
    write_json(args.out, t)
    log("Transition %s -> %s (%s): %s" % (t["from"] or "no bundle", t["to"] or "no bundle", t["kind"], "; ".join(describe(t)) or "no change in contents"))


if __name__ == "__main__":
    main()
