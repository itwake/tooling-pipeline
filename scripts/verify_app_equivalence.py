#!/usr/bin/env python3
"""Verify pipeline: the rebuilt application is equivalent to the original.

Compares an application built before the POM change (old) with one built after it (new):

  * exactly one bundle jar appears in the new build; it declares which coordinates it embeds
  * those coordinates disappear from the dependency set, nothing else is removed or added
  * every class / resource on the dependency classpath exists in both with identical bytes
    (merged SPI / Spring resources are compared as sets of entries)
  * application classes (BOOT-INF/classes) are unchanged; changed resources are reported
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    BUNDLE_CONTENTS_ATTR,
    THIRD_PARTY_DIR,
    classify_entry,
    fail,
    ga_str,
    gav_str,
    identify_gav,
    log,
    match_any,
    parse_properties,
    properties_values,
    read_json,
    read_manifest,
    sha256_bytes,
    significant_lines,
    write_json,
)


def parse_contents_attr(value: str):
    out = []
    for tok in value.split(","):
        parts = tok.strip().split(":")
        if len(parts) >= 3:
            g = {"groupId": parts[0], "artifactId": parts[1], "version": parts[2]}
            if len(parts) > 3 and parts[3]:
                g["classifier"] = parts[3]
            out.append(g)
    return out


def parse_contents_file(text):
    """BUNDLE-CONTENTS.txt lines 'coordinate  sha256  file' -> {coordinate: sha256}."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def load_jars(app: AppArtifact):
    jars = []
    for dj in app.dependency_jars():
        with dj.open() as zf:
            gav, _, cands = identify_gav(zf, dj.name)
            attrs = read_manifest(zf)
            contents_sha = {}
            if attrs.get(BUNDLE_CONTENTS_ATTR):
                try:
                    contents_sha = parse_contents_file(zf.read(THIRD_PARTY_DIR + "BUNDLE-CONTENTS.txt").decode("utf-8", "replace"))
                except KeyError:
                    contents_sha = {}
        jars.append({"dep": dj, "name": dj.name, "sha256": dj.sha256, "gav": gav, "candidates": cands,
                     "contents_attr": attrs.get(BUNDLE_CONTENTS_ATTR), "contents_sha": contents_sha})
    return jars


def collect_entries(jars, skip_prefixes):
    """jars must be in class-loading order: the first provider of a path is the effective one."""
    plain = defaultdict(lambda: defaultdict(list))  # path -> sha -> [jar], insertion order = loading order
    merged_lines = defaultdict(set)                 # services / append -> lines
    merged_props = defaultdict(lambda: defaultdict(set))
    for j in jars:
        with j["dep"].open() as zf:
            for n in zf.namelist():
                cat = classify_entry(n)
                if cat in ("dir", "manifest", "signature", "index", "module-info", "legal"):
                    continue
                if any(n.startswith(p) for p in skip_prefixes):
                    continue
                data = zf.read(n)
                if cat in ("services", "append"):
                    merged_lines[n].update(significant_lines(data.decode("utf-8", "replace")))
                elif cat == "props-merge":
                    for k, vals in properties_values(parse_properties(data.decode("utf-8", "replace"))).items():
                        merged_props[n][k].update(vals)
                else:
                    plain[n][sha256_bytes(data)].append(j["name"])
    return plain, merged_lines, merged_props


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", help="original application (jar/war/dir)")
    ap.add_argument("--new", help="rebuilt application (jar/war/dir)")
    ap.add_argument("--old-source", help="metadata JSON from extract_source.py (instead of --old)")
    ap.add_argument("--new-source", help="metadata JSON from extract_source.py (instead of --new)")
    ap.add_argument("--bundle-artifact-id", default="", help="restrict bundle detection to this artifactId")
    ap.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "config" / "policy.json"))
    ap.add_argument("--out", default="out/equivalence.json")
    args = ap.parse_args()

    old_path = args.old or (read_json(args.old_source)["local_path"] if args.old_source else None)
    new_path = args.new or (read_json(args.new_source)["local_path"] if args.new_source else None)
    if not old_path or not new_path:
        fail("--old/--new (or --old-source/--new-source) are required")
    ignore_app = read_json(args.policy).get("equivalence", {}).get("ignore_app_entries", []) if Path(args.policy).exists() else []

    checks = []

    def check(name, ok, detail="", level="fail"):
        checks.append({"check": name, "status": "pass" if ok else level, "detail": detail})

    old_app, new_app = AppArtifact(old_path), AppArtifact(new_path)
    log("old: %s (%s)" % (old_path, old_app.layout))
    log("new: %s (%s)" % (new_path, new_app.layout))
    old_jars, new_jars = load_jars(old_app), load_jars(new_app)

    # ---- locate the bundle ---------------------------------------------------------------
    bundles = [j for j in new_jars if j["contents_attr"]]
    if args.bundle_artifact_id:
        bundles = [j for j in bundles if j["gav"] and j["gav"]["artifactId"] == args.bundle_artifact_id]
    if len(bundles) != 1:
        fail("expected exactly one bundle jar in the new application, found %d: %s" % (
            len(bundles), ", ".join(j["name"] for j in bundles)))
    bundle = bundles[0]
    embedded = parse_contents_attr(bundle["contents_attr"])
    embedded_ga = {ga_str(g) for g in embedded}
    log("bundle: %s embeds %s" % (bundle["name"], ", ".join(gav_str(g) for g in embedded)))
    check("bundle-present", True, "%s (%s)" % (bundle["name"], gav_str(bundle["gav"]) if bundle["gav"] else "coordinates unknown"))

    # ---- dependency set ------------------------------------------------------------------
    def key(j):
        return gav_str(j["gav"]) if j["gav"] else "file:" + j["name"]

    old_by_key = {key(j): j for j in old_jars}
    new_by_key = {key(j): j for j in new_jars}
    old_by_sha = {j["sha256"]: j for j in old_jars}
    embedded_sha = bundle.get("contents_sha") or {}   # coordinate -> sha256 from BUNDLE-CONTENTS.txt
    replaced, replaced_keys, problems = [], set(), []
    for g in embedded:
        k = gav_str(g)
        match = old_by_sha.get(embedded_sha.get(k, "")) or old_by_key.get(k)
        if match is not None:
            replaced.append(k)
            replaced_keys.add(key(match))
        else:
            same_ga = [kk for kk, j in old_by_key.items() if j["gav"] and ga_str(j["gav"]) == ga_str(g)]
            problems.append("%s not in old build%s" % (k, " (old has %s)" % ", ".join(same_ga) if same_ga else ""))
    check("embedded-coordinates-match-old-build", not problems, "; ".join(problems) if problems else ", ".join(replaced))

    embedded_shas = set(embedded_sha.values())
    leftovers = [key(j) for j in new_jars if j is not bundle
                 and ((j["gav"] and ga_str(j["gav"]) in embedded_ga) or j["sha256"] in embedded_shas)]
    check("original-coordinates-removed", not leftovers,
          "still on the classpath: %s (exclusions incomplete)" % ", ".join(leftovers) if leftovers else "none of the embedded coordinates remain")

    expected_new = (set(old_by_key) - replaced_keys) | {key(bundle)}
    removed = sorted(set(old_by_key) - set(new_by_key) - replaced_keys)
    added = sorted(set(new_by_key) - set(old_by_key) - {key(bundle)})
    check("dependency-set-unchanged", not removed and not added,
          "removed: %s; added: %s" % (", ".join(removed) or "-", ", ".join(added) or "-") if removed or added
          else "%d jars kept, %d replaced by the bundle" % (len(expected_new) - 1, len(replaced)))

    differing = [k for k in set(old_by_key) & set(new_by_key) if old_by_key[k]["sha256"] != new_by_key[k]["sha256"]]
    check("kept-jars-byte-identical", not differing,
          "rebuilt from the repository with different bytes (entries compared below): %s" % ", ".join(sorted(differing)[:10]) if differing
          else "all kept jars identical", level="warn")

    # ---- classpath contents --------------------------------------------------------------
    own_maven = "META-INF/maven/%s/%s/" % (bundle["gav"]["groupId"], bundle["gav"]["artifactId"]) if bundle["gav"] else "META-INF/maven/__none__/"
    skip = [THIRD_PARTY_DIR, own_maven]
    old_plain, old_lines, old_props = collect_entries(old_jars, skip)
    new_plain, new_lines, new_props = collect_entries(new_jars, skip)

    # effective classpath: for every path, the copy the class loader sees first
    old_eff = {p: next(iter(shas)) for p, shas in old_plain.items()}
    new_eff = {p: next(iter(shas)) for p, shas in new_plain.items()}
    missing = sorted(set(old_eff) - set(new_eff))
    extra = sorted(set(new_eff) - set(old_eff))
    changed = sorted(p for p in set(old_eff) & set(new_eff) if old_eff[p] != new_eff[p])
    check("classpath-entries-identical", not missing and not extra and not changed,
          "missing %d (%s); extra %d (%s); changed %d (%s)" % (
              len(missing), ", ".join(missing[:8]), len(extra), ", ".join(extra[:8]), len(changed), ", ".join(changed[:8]))
          if missing or extra or changed else "%d entries compared, first copy in class-loading order" % len(old_eff))
    collapsed = sorted(p for p in old_plain if len(old_plain[p]) > 1 and len(new_plain.get(p, {})) < len(old_plain[p]) and not p.endswith(".class"))
    check("duplicate-resources-collapsed", not collapsed,
          "resources that existed in several copies now have fewer; libraries reading every copy (getResources) see less: %s" % ", ".join(collapsed[:10])
          if collapsed else "no multi-copy resource lost a copy", level="warn")

    line_problems = []
    for p in sorted(set(old_lines) | set(new_lines)):
        if old_lines.get(p, set()) != new_lines.get(p, set()):
            line_problems.append("%s (-%s +%s)" % (p, ",".join(sorted(old_lines.get(p, set()) - new_lines.get(p, set()))[:3]),
                                                     ",".join(sorted(new_lines.get(p, set()) - old_lines.get(p, set()))[:3])))
    for p in sorted(set(old_props) | set(new_props)):
        o, n = old_props.get(p, {}), new_props.get(p, {})
        for k in sorted(set(o) | set(n)):
            if o.get(k, set()) != n.get(k, set()):
                line_problems.append("%s key %s" % (p, k))
    check("merged-resources-identical", not line_problems, "; ".join(line_problems[:10]) if line_problems else "%d merged resources compared" % (len(old_lines) + len(old_props)))

    dup_new = sorted(p for p, shas in new_plain.items() if len(shas) > 1)
    dup_old = {p for p, shas in old_plain.items() if len(shas) > 1}
    new_dups = [p for p in dup_new if p not in dup_old]
    check("no-new-duplicate-classes", not new_dups,
          "same path, different bytes, introduced by the change: %s" % ", ".join(new_dups[:10]) if new_dups
          else ("%d pre-existing duplicates unchanged" % len(dup_old) if dup_old else "no duplicates"), level="warn")

    # ---- application's own entries --------------------------------------------------------
    def app_map(app: AppArtifact):
        classes, others = {}, {}
        for n, loader in app.app_entries():
            if match_any(n, ignore_app):
                continue
            if app.classes_prefix and n.startswith(app.classes_prefix):
                rel = n[len(app.classes_prefix):]
                (classes if rel.endswith(".class") else others)["classes:" + rel] = sha256_bytes(loader())
            elif app.layout == new_app.layout == old_app.layout:
                others[n] = sha256_bytes(loader())
        return classes, others

    old_classes, old_others = app_map(old_app)
    new_classes, new_others = app_map(new_app)
    class_diff = sorted(set(old_classes) ^ set(new_classes)) + sorted(k for k in set(old_classes) & set(new_classes) if old_classes[k] != new_classes[k])
    check("application-classes-unchanged", not class_diff,
          "%d differing class entries: %s" % (len(class_diff), ", ".join(class_diff[:10])) if class_diff else "%d classes identical" % len(old_classes))
    other_diff = sorted(set(old_others) ^ set(new_others)) + sorted(k for k in set(old_others) & set(new_others) if old_others[k] != new_others[k])
    check("application-resources-unchanged", not other_diff,
          "%d differing entries: %s" % (len(other_diff), ", ".join(other_diff[:10])) if other_diff else "%d entries identical" % len(old_others), level="warn")
    if old_app.layout != new_app.layout:
        check("same-layout", False, "old %s vs new %s: only classes compared outside the dependency jars" % (old_app.layout, new_app.layout), level="warn")

    old_app.close()
    new_app.close()

    failed = [c for c in checks if c["status"] == "fail"]
    result = {
        "old": str(old_path), "new": str(new_path),
        "bundle": {"file": bundle["name"], "gav": bundle["gav"], "embedded": embedded},
        "replaced": replaced,
        "checks": checks,
        "ok": not failed,
    }
    write_json(args.out, result)
    for c in checks:
        log("  [%s] %s%s" % (c["status"].upper(), c["check"], (": " + c["detail"]) if c["detail"] else ""))
    if failed:
        fail("%d check(s) failed" % len(failed), 1)
    log("Equivalence verification passed")


if __name__ == "__main__":
    main()
