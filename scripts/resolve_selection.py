#!/usr/bin/env python3
"""Stage 3: resolve the selection, pull in everything related to it, check that the merge is possible.

Selection tokens (one per line, or comma separated):
  commons-io                       artifactId only
  commons-io:commons-io            groupId:artifactId
  commons-io:commons-io:2.16.1     exact coordinates (required when the jar could not be identified)
  *                                every dependency jar of the application
  -bcprov-jdk15on                  exclusion (same forms as above)

Related jars are added automatically, so the bundle never splits something that must stay
together: jars of the same family (config/policy.json), jars with the same groupId and version,
and the dependencies declared in the selected jars' own pom.xml, as long as they are present in
the application. Nothing is refused for policy reasons; the report explains every addition and
warns about the consequences (Boot version coupling, stripped signatures).

Two jars holding the same class path with different bytes are kept the way the application
resolves them: the copy that comes first on the application's classpath (BOOT-INF/classpath.idx)
wins, exactly as the JVM picks it at runtime; the report lists what was shadowed.

Hard errors, the only ones left: a token that matches nothing or several jars, an unreadable jar,
and properties resources that need Spring Boot's merging transformer when no Boot version is known.
"""
from __future__ import annotations

import argparse
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    COLLAPSE_HARMLESS,
    LOG4J_PLUGIN_CACHE,
    classify_entry,
    match_any,
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


def family_of(group_id: str, families) -> str:
    for prefix in families:
        if group_id == prefix or group_id.startswith(prefix + "."):
            return prefix
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="out/inventory.json")
    ap.add_argument("--select", help="selection tokens")
    ap.add_argument("--select-file", help="file with selection tokens")
    ap.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "config" / "policy.json"))
    ap.add_argument("--no-expand", action="store_true", help="do not add related jars automatically")
    ap.add_argument("--policy-mode", default=None, help=argparse.SUPPRESS)  # accepted for old callers, ignored
    ap.add_argument("--out", default="out/selection.json")
    args = ap.parse_args()

    text = args.select or ""
    if args.select_file:
        text += "\n" + Path(args.select_file).read_text(encoding="utf-8")
    tokens = parse_selection_text(text)
    if not tokens:
        fail("empty selection")
    include_tokens = [t for t in tokens if not t.startswith("-")]
    exclude_tokens = [t[1:] for t in tokens if t.startswith("-") and len(t) > 1]
    if not include_tokens:
        fail("selection contains only exclusions")

    inv = read_json(args.inventory)
    deps = inv["dependencies"]
    policy = read_json(args.policy) if Path(args.policy).is_file() else {}
    families = policy.get("families", {})

    findings = []

    def add_finding(level, code, message, jar=None):
        findings.append({"level": level, "code": code, "message": message, "jar": jar})

    # ---- exclusions ----------------------------------------------------------------------
    excluded = {}
    for tok in exclude_tokens:
        matches, _ = match_token(tok, deps)
        if not matches:
            add_finding("warn", "exclusion-unmatched", "'-%s' matches no dependency" % tok)
        for m in matches:
            excluded[m["path_in_app"]] = tok

    # ---- explicit selection --------------------------------------------------------------
    selected: "OrderedDict[str, dict]" = OrderedDict()

    def select(d, reason):
        if d["path_in_app"] in selected:
            return False
        rec = dict(d)
        rec["reasons"] = [reason]
        selected[d["path_in_app"]] = rec
        return True

    for tok in include_tokens:
        if tok in ("*", "all"):
            for d in deps:
                if d["path_in_app"] not in excluded and not d.get("error"):
                    select(d, "selected: *")
            continue
        matches, explicit = match_token(tok, deps)
        if not matches:
            hint = sorted({d["gav"]["artifactId"] for d in deps if d.get("gav") and tok.split(":")[-1].lower() in d["gav"]["artifactId"].lower()})
            add_finding("error", "not-found", "'%s' matches no dependency of the application%s" % (
                tok, (" (similar: %s)" % ", ".join(hint[:8])) if hint else ""))
            continue
        if len(matches) > 1:
            add_finding("error", "ambiguous", "'%s' matches several jars, use groupId:artifactId:version: %s" % (
                tok, "; ".join("%s -> %s" % (m["name"], gav_str(m["gav"]) if m["gav"] else "?") for m in matches)))
            continue
        d = matches[0]
        if d.get("error"):
            add_finding("error", "unreadable", "%s: %s" % (d["name"], d["error"]), d["name"])
            continue
        if d["path_in_app"] in excluded:
            add_finding("warn", "selected-and-excluded", "'%s' is both selected and excluded; it stays excluded" % tok)
            continue
        if not d.get("gav") and explicit:
            d = dict(d, gav=explicit, gav_source="selection")
            add_finding("warn", "gav-from-selection", "%s has no usable pom.properties; coordinates taken from the selection token" % d["name"], d["name"])
        select(d, "selected: %s" % tok)

    # ---- automatic expansion -------------------------------------------------------------
    by_ga = defaultdict(list)
    for d in deps:
        if d.get("gav"):
            by_ga[ga_str(d["gav"])].append(d)
    if not args.no_expand:
        changed = True
        while changed:
            changed = False
            for rec in list(selected.values()):
                gav = rec.get("gav")
                if not gav:
                    continue
                fam = family_of(gav["groupId"], families)
                for o in deps:
                    if o["path_in_app"] in selected or o.get("error"):
                        continue
                    og = o.get("gav")
                    if not og:
                        continue
                    reason = None
                    if fam and family_of(og["groupId"], families) == fam:
                        reason = "family %s, with %s" % (fam, rec["name"])
                    elif og["groupId"] == gav["groupId"] and og["version"] == gav["version"]:
                        reason = "same groupId and version as %s" % rec["name"]
                    if reason:
                        if o["path_in_app"] in excluded:
                            add_finding("warn", "excluded-related", "%s is related to %s (%s) but excluded by '-%s'" % (
                                o["name"], rec["name"], reason.split(",")[0], excluded[o["path_in_app"]]), o["name"])
                            continue
                        if select(o, reason):
                            changed = True
                for dep in rec.get("declared_dependencies", []):
                    key = "%s:%s" % (dep["groupId"], dep["artifactId"])
                    targets = by_ga.get(key, [])
                    if not targets:
                        # a declared dependency whose jar could not be identified: still related, taken along
                        targets = [o for o in deps if not o.get("gav") and not o.get("error")
                                   and (any(c.get("artifactId") == dep["artifactId"] for c in o.get("candidates", []))
                                        or (o.get("synthetic_hint") or {}).get("artifactId") == dep["artifactId"])]
                    for o in targets:
                        if o["path_in_app"] in selected or o.get("error"):
                            continue
                        if o["path_in_app"] in excluded:
                            add_finding("warn", "excluded-related", "%s is a declared dependency of %s but excluded by '-%s'" % (
                                o["name"], rec["name"], excluded[o["path_in_app"]]), o["name"])
                            continue
                        if select(o, "declared dependency of %s" % rec["name"]):
                            changed = True

    # ---- unidentified jars: embedded anyway, under synthetic coordinates -----------------
    for d in selected.values():
        if d.get("gav"):
            continue
        candidates = ", ".join("%s:%s:%s" % (c.get("groupId") or "?", c.get("artifactId"), c.get("version")) for c in d.get("candidates", [])) or "none"
        if d.get("synthetic"):
            d["gav"], d["gav_source"] = d["synthetic"], "synthetic"
            add_finding("warn", "unidentified-synthetic", "%s: coordinates unknown%s; embedded under synthetic coordinates %s. Name it as groupId:artifactId:version in SELECT to use the real ones, or exclude it with -%s (candidates: %s)" % (
                d["name"], (" (%s)" % d["identification_note"]) if d.get("identification_note") else "", gav_str(d["gav"]), d["name"][:-4], candidates), d["name"])
        else:
            add_finding("error", "unidentified", "%s: coordinates cannot be identified%s; select it as groupId:artifactId:version or exclude it with -%s (candidates: %s)" % (
                d["name"], (" (%s)" % d["identification_note"]) if d.get("identification_note") else "", d["name"][:-4], candidates), d["name"])

    # class-loading order of the application: the bundle keeps duplicates the same way the JVM does
    selected_list = sorted(selected.values(), key=lambda d: (d.get("classpath_index", 10 ** 9), d["name"]))
    auto_added = [d for d in selected_list if not d["reasons"][0].startswith("selected:")]

    # ---- notes about the selection -------------------------------------------------------
    signed_note = policy.get("signed_jar_note", "signatures are stripped")
    coupled = sorted({family_of(d["gav"]["groupId"], families) for d in selected_list if d.get("gav") and family_of(d["gav"]["groupId"], families)})
    for d in selected_list:
        gav, flags, name = d.get("gav"), d.get("flags", {}), d["name"]
        if flags.get("signed"):
            add_finding("warn", "signed-jar", "%s is signed; %s" % (name, signed_note), name)
        if flags.get("module_info"):
            add_finding("info", "module-info", "%s contains module-info.class; it is dropped (bundle is a classpath library)" % name, name)
        if flags.get("native_libs") and policy.get("warn_native_libs", True):
            add_finding("warn", "native-libs", "%s ships native libraries (%d); verify the library does not locate them via its own jar" % (name, len(flags["native_libs"])), name)
        if flags.get("nested_jars") and policy.get("warn_nested_jars", True):
            add_finding("warn", "nested-jars", "%s contains nested jars: %s" % (name, ", ".join(flags["nested_jars"][:5])), name)
        if flags.get("multi_release"):
            add_finding("info", "multi-release", "%s is a multi-release jar; the bundle manifest will carry Multi-Release: true" % name, name)
        pom_candidates = [c for c in d.get("candidates", []) if c.get("source") == "pom.properties"]
        if gav and len(pom_candidates) > 1:
            add_finding("warn", "shaded-input", "%s already embeds other components: %s" % (
                name, ", ".join("%s:%s:%s" % (c["groupId"], c["artifactId"], c["version"]) for c in pom_candidates if c["artifactId"] != gav["artifactId"])), name)
    for d in auto_added:
        add_finding("info", "added-related", "%s added: %s" % (d["name"], d["reasons"][0]), d["name"])
    boot_version = inv["app"].get("spring_boot_version")
    if coupled:
        add_finding("warn", "version-coupling", "the bundle freezes %s at the versions of this build%s; rebuild it when those versions change" % (
            ", ".join("%s (%s)" % (f, families.get(f, "")) if families.get(f) else f for f in coupled),
            " (Spring Boot %s)" % boot_version if boot_version else ""))

    # ---- cross-jar checks (needs the bytes) ----------------------------------------------
    conflicts, identical = [], []
    append_present, props_counts, services_counts, log4j_counts = set(), defaultdict(int), defaultdict(int), 0
    multi_release = any(d.get("flags", {}).get("multi_release") for d in selected_list)
    if selected_list:
        app = AppArtifact(inv["app"]["local_path"])
        by_path = {j.path_in_app: j for j in app.dependency_jars()}
        entries = defaultdict(lambda: defaultdict(list))
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
                    elif cat == "log4j-plugins":
                        log4j_counts += 1
        app.close()
        position = {d["name"]: d.get("classpath_index", 10 ** 9) for d in deps}
        for path, shas in sorted(entries.items()):
            if len(shas) > 1:
                providers = sorted((j for jars in shas.values() for j in jars), key=lambda j: (position.get(j, 10 ** 9), j))
                conflicts.append({"path": path, "winner": providers[0], "shadowed": providers[1:]})
            elif len(next(iter(shas.values()))) > 1:
                identical.append(path)
        if conflicts:
            pairs = sorted({(c["winner"], s) for c in conflicts for s in c["shadowed"]})
            add_finding("warn", "overlap-resolved", "%d entries exist in several selected jars with different bytes; the bundle keeps the copy that comes first on the application's classpath, as the JVM does: %s" % (
                len(conflicts), "; ".join("%s shadows %s" % (w, s) for w, s in pairs[:6])))
            resources = [c["path"] for c in conflicts if not c["path"].endswith(".class")]
            harmless = [r for r in resources if match_any(r, COLLAPSE_HARMLESS)]
            review = [r for r in resources if r not in harmless]
            if harmless:
                add_finding("info", "resource-copies-harmless", "%d of them are IDE or documentation metadata nothing reads at run time; the first copy is kept: %s" % (
                    len(harmless), ", ".join(harmless[:6])))
            if review:
                add_finding("warn", "resource-copies-collapse", "%d of them are resources that only keep their first copy; a library that reads every copy (getResources) loses the others. SPI files, the Spring metadata and the Log4j plugin cache are merged, decide for these: %s" % (
                    len(review), ", ".join(review[:6])))
        if identical:
            add_finding("info", "overlap-identical", "%d entries are duplicated with identical bytes (first copy is kept)" % len(identical))

    props_merge = sorted(r for r, c in props_counts.items() if c > 1)
    if props_merge:
        if boot_version:
            add_finding("info", "props-merge", "%s occur in several selected jars; merged with Spring Boot %s PropertiesMergingResourceTransformer" % (", ".join(props_merge), boot_version))
        else:
            add_finding("error", "props-merge-no-boot", "%s occur in several selected jars but no Spring Boot version is known to merge them (the application manifest has no Spring-Boot-Version, no spring-boot jar is named or identified); rerun inspect_app.py with --spring-boot-version" % ", ".join(props_merge))
    if log4j_counts > 1:
        add_finding("info", "log4j-plugins-merge", "%s provided by %d jars; merged by Log4j's Log4j2PluginCacheFileTransformer" % (LOG4J_PLUGIN_CACHE, log4j_counts))
    for r, c in sorted(services_counts.items()):
        if c > 1:
            add_finding("info", "services-merge", "%s provided by %d jars; merged by ServicesResourceTransformer" % (r, c))

    # ---- output --------------------------------------------------------------------------
    errors = [f for f in findings if f["level"] == "error"]
    result = {
        "tokens": tokens,
        "selected": selected_list,
        "auto_added": [{"name": d["name"], "reason": d["reasons"][0]} for d in auto_added],
        "excluded": sorted(set(excluded.values())),
        "findings": findings,
        "overlaps": {"conflicts": conflicts, "identical": identical},
        "transformers": {
            "services": True,
            "append": sorted(append_present),
            "props_merge": props_merge,
            "log4j_plugins": log4j_counts > 1,
            "multi_release": multi_release,
        },
        "spring_boot_version": boot_version,
        "families": coupled,
        "classpath_source": inv["app"].get("classpath_source"),
        "app": inv["app"],
        "ok": not errors,
    }
    write_json(args.out, result)

    log("Selected %d jar(s) (%d requested, %d added as related):" % (len(selected_list), len(selected_list) - len(auto_added), len(auto_added)))
    for d in selected_list:
        log("  %-50s %s  [%s]" % (gav_str(d["gav"]) if d.get("gav") else "?", d["name"], d["reasons"][0]))
    for f in findings:
        log("  [%s] %s: %s" % (f["level"].upper(), f["code"], f["message"]))
    log("Selection written to %s" % args.out)
    if errors:
        fail("%d error(s); fix the selection" % len(errors), 1)


if __name__ == "__main__":
    main()
