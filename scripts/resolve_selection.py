#!/usr/bin/env python3
"""Stage 3: resolve the requested dependencies to exact jars and apply the merge policy.

Selection tokens (one per line, or comma separated):
  commons-io                       artifactId only
  commons-io:commons-io            groupId:artifactId
  commons-io:commons-io:2.16.1     exact coordinates (required when the jar has no pom.properties)

Hard errors (always fatal): token not found / ambiguous, coordinates not identifiable,
same class path with different bytes across the selected jars.
Policy errors (fatal in strict mode, warnings in warn mode): denied groups, signed jars,
properties resources that need Spring Boot's merging transformer when no Boot version is known.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    classify_entry,
    fail,
    ga_str,
    gav_str,
    log,
    match_token,
    parse_selection_text,
    read_json,
    sha256_bytes,
    write_json,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="out/inventory.json")
    ap.add_argument("--select", help="selection tokens")
    ap.add_argument("--select-file", help="file with selection tokens")
    ap.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "config" / "policy.json"))
    ap.add_argument("--policy-mode", choices=["strict", "warn"], default="strict")
    ap.add_argument("--out", default="out/selection.json")
    args = ap.parse_args()

    text = args.select or ""
    if args.select_file:
        text += "\n" + Path(args.select_file).read_text(encoding="utf-8")
    tokens = parse_selection_text(text)
    if not tokens:
        fail("empty selection")

    inv = read_json(args.inventory)
    deps = inv["dependencies"]
    policy = read_json(args.policy)
    strict = args.policy_mode == "strict"
    policy_level = "error" if strict else "warn"

    findings = []

    def add(level, code, message, jar=None):
        findings.append({"level": level, "code": code, "message": message, "jar": jar})

    # ---- token matching ------------------------------------------------------------------
    selected = {}
    for tok in tokens:
        matches, explicit = match_token(tok, deps)
        if not matches:
            hint = sorted({d["gav"]["artifactId"] for d in deps if d.get("gav") and tok.split(":")[-1].lower() in d["gav"]["artifactId"].lower()})
            add("error", "not-found", "'%s' matches no dependency of the application%s" % (
                tok, (" (similar: %s)" % ", ".join(hint[:8])) if hint else ""))
            continue
        if len(matches) > 1:
            add("error", "ambiguous", "'%s' matches several jars, use groupId:artifactId:version: %s" % (
                tok, "; ".join("%s -> %s" % (m["name"], gav_str(m["gav"]) if m["gav"] else "?") for m in matches)))
            continue
        d = dict(matches[0])
        if d.get("error"):
            add("error", "unreadable", "%s: %s" % (d["name"], d["error"]), d["name"])
            continue
        if not d.get("gav"):
            if explicit:
                d["gav"] = explicit
                d["gav_source"] = "selection"
                add("warn", "gav-from-selection", "%s has no usable pom.properties; coordinates taken from the selection token" % d["name"], d["name"])
            else:
                add("error", "unidentified", "%s: coordinates cannot be identified from the jar; select it as groupId:artifactId:version (candidates: %s)" % (
                    d["name"], ", ".join("%s:%s:%s" % (c.get("groupId") or "?", c.get("artifactId"), c.get("version")) for c in d.get("candidates", []))), d["name"])
                continue
        d["token"] = tok
        selected[d["path_in_app"]] = d

    selected_list = sorted(selected.values(), key=lambda d: ga_str(d["gav"]))

    # ---- per-jar policy ------------------------------------------------------------------
    deny_groups = policy.get("deny_groups", {})
    deny_artifacts = policy.get("deny_artifacts", {})
    warn_groups = policy.get("warn_groups", {})
    for d in selected_list:
        gav, flags, name = d["gav"], d.get("flags", {}), d["name"]
        g = gav["groupId"]
        for prefix, reason in deny_groups.items():
            if g == prefix or g.startswith(prefix + "."):
                add(policy_level, "policy-deny-group", "%s is in denied group '%s': %s" % (gav_str(gav), prefix, reason), name)
        if ga_str(gav) in deny_artifacts:
            add(policy_level, "policy-deny-artifact", "%s is denied: %s" % (gav_str(gav), deny_artifacts[ga_str(gav)]), name)
        for prefix, reason in warn_groups.items():
            if g == prefix or g.startswith(prefix + "."):
                add("warn", "policy-warn-group", "%s: %s" % (gav_str(gav), reason), name)
        if flags.get("signed"):
            add(policy_level if policy.get("refuse_signed_jars", True) else "warn", "signed-jar",
                "%s is signed; signatures will be stripped, which breaks JCE providers and anything verifying its own signature" % name, name)
        if flags.get("module_info"):
            add("info", "module-info", "%s contains module-info.class; it is dropped (bundle is a classpath library)" % name, name)
        if flags.get("native_libs") and policy.get("warn_native_libs", True):
            add("warn", "native-libs", "%s ships native libraries (%d); verify the library does not locate them via its own jar" % (name, len(flags["native_libs"])), name)
        if flags.get("nested_jars") and policy.get("warn_nested_jars", True):
            add("warn", "nested-jars", "%s contains nested jars: %s" % (name, ", ".join(flags["nested_jars"][:5])), name)
        if flags.get("multi_release"):
            add("info", "multi-release", "%s is a multi-release jar; the bundle manifest will carry Multi-Release: true" % name, name)
        pom_candidates = [c for c in d.get("candidates", []) if c.get("source") == "pom.properties"]
        if len(pom_candidates) > 1:
            add("warn", "shaded-input", "%s already embeds other components: %s" % (
                name, ", ".join("%s:%s:%s" % (c["groupId"], c["artifactId"], c["version"]) for c in pom_candidates if c["artifactId"] != gav["artifactId"])), name)

    # ---- cross-jar checks (needs the bytes) ----------------------------------------------
    conflicts, identical = [], []
    append_present, props_counts, services_counts = set(), defaultdict(int), defaultdict(int)
    multi_release = any(d.get("flags", {}).get("multi_release") for d in selected_list)
    if selected_list:
        app = AppArtifact(inv["app"]["local_path"])
        by_path = {j.path_in_app: j for j in app.dependency_jars()}
        entries = defaultdict(lambda: defaultdict(list))  # path -> sha -> [jar]
        for d in selected_list:
            dj = by_path.get(d["path_in_app"])
            if dj is None:
                fail("%s disappeared from the application" % d["path_in_app"])
            if dj.sha256 != d["sha256"]:
                fail("%s changed since the inventory was taken" % d["path_in_app"])
            with dj.open() as zf:
                for n in zf.namelist():
                    cat = classify_entry(n)
                    if cat in ("class", "resource"):
                        entries[n][sha256_bytes(zf.read(n))].append(dj.name)
                    elif cat == "services":
                        services_counts[n] += 1
                    elif cat == "append":
                        append_present.add(n)
                    elif cat == "props-merge":
                        props_counts[n] += 1
        app.close()
        for path, shas in sorted(entries.items()):
            if len(shas) > 1:
                conflicts.append({"path": path, "providers": {s: j for s, j in shas.items()}})
            elif len(next(iter(shas.values()))) > 1:
                identical.append(path)
        if conflicts:
            add("error", "overlap-conflict", "%d entries exist in several selected jars with different bytes, e.g. %s" % (
                len(conflicts), ", ".join(c["path"] for c in conflicts[:5])))
        if identical:
            add("info", "overlap-identical", "%d entries are duplicated with identical bytes (first copy is kept)" % len(identical))

    boot_version = inv["app"].get("spring_boot_version")
    props_merge = sorted(r for r, c in props_counts.items() if c > 1)
    if props_merge:
        if boot_version:
            add("info", "props-merge", "%s occur in several selected jars; merged with Spring Boot %s PropertiesMergingResourceTransformer" % (", ".join(props_merge), boot_version))
        else:
            add(policy_level, "props-merge-no-boot", "%s occur in several selected jars but no Spring Boot version was detected to merge them" % ", ".join(props_merge))
    for r, c in sorted(services_counts.items()):
        if c > 1:
            add("info", "services-merge", "%s provided by %d jars; merged by ServicesResourceTransformer" % (r, c))

    # ---- output --------------------------------------------------------------------------
    errors = [f for f in findings if f["level"] == "error"]
    result = {
        "policy_mode": args.policy_mode,
        "tokens": tokens,
        "selected": selected_list,
        "findings": findings,
        "overlaps": {"conflicts": conflicts, "identical": identical},
        "transformers": {
            "services": True,
            "append": sorted(append_present),
            "props_merge": props_merge,
            "multi_release": multi_release,
        },
        "spring_boot_version": boot_version,
        "app": inv["app"],
        "ok": not errors,
    }
    write_json(args.out, result)

    log("Selected %d jar(s):" % len(selected_list))
    for d in selected_list:
        log("  %-45s %s" % (gav_str(d["gav"]), d["name"]))
    for f in findings:
        log("  [%s] %s: %s" % (f["level"].upper(), f["code"], f["message"]))
    log("Selection written to %s" % args.out)
    if errors:
        fail("%d error(s); fix the selection or rerun with --policy-mode warn where the policy allows it" % len(errors), 1)


if __name__ == "__main__":
    main()
