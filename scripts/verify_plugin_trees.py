#!/usr/bin/env python3
"""Stage 7c: check the plugin dependency trees of the patched consumer against the proposal.

After apply_consumer_patch.py has changed the POMs, plugin_trees.py runs again on them and the reactor's
`dependency:tree -Dverbose` output is compared with what consumer-patch.json promised for the build
plugins: a patched plugin's realm must contain the bundle, and the only embedded coordinates left in it
are the plugin's own direct dependencies (and what is reached only through them), which no POM
configuration can exclude; the bundle precedes them on the realm classpath. Anything else, and any plugin
that shows embedded coordinates although the proposal listed none for it, is a finding.

Report only: the exit code is 0 unless the inputs are unusable. --findings-out gets one line per finding
(prefixed [plugins]) for the pipeline, which marks the build UNSTABLE and lists them before the push.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, parse_dependency_trees, read_json, write_json  # noqa: E402
from gen_consumer_patch import classify_plugin_tree  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", default="out/consumer-patch.json", help="proposal the POMs were changed with")
    ap.add_argument("--plugins", required=True, help="consumer-plugins.json plugin_trees.py wrote from the changed POMs")
    ap.add_argument("--tree", action="append", default=[], required=True, help="dependency:tree output of that reactor (repeatable)")
    ap.add_argument("--out", default="out/plugin-trees-check.json")
    ap.add_argument("--findings-out", default="", help="one finding per line (default: <out>-findings.txt)")
    args = ap.parse_args()

    patch = read_json(args.patch)
    if not patch.get("plugin_tree_provided"):
        log("the proposal was generated without plugin trees; nothing to check against")
        write_json(args.out, {"ok": True, "findings": [], "plugins": [], "checked": 0, "patched": 0, "skipped": "no plugin trees in the proposal"})
        return
    embedded_ga = set(patch.get("banned") or [])
    bundle = patch["bundle"]
    bundle_ga = "%s:%s" % (bundle["groupId"], bundle["artifactId"])
    expected = {e["plugin"]: e for e in patch.get("plugins", [])}
    plugins_map = read_json(args.plugins)
    by_id = {p["id"]: p for p in plugins_map.get("plugins", [])}

    texts = []
    for t in args.tree:
        p = Path(t)
        if not p.is_file():
            fail("plugin tree file not found: %s" % t)
        texts.append(p.read_text(encoding="utf-8", errors="replace"))

    findings, plugins, seen = [], [], set()
    for text in texts:
        for tree in parse_dependency_trees(text):
            mapping = by_id.get(tree["root"]["artifactId"])
            if mapping is None:
                continue
            plugin = mapping["plugin"]
            gav = "%s:%s:%s" % (plugin["groupId"], plugin["artifactId"], plugin["version"])
            plugin_ga = "%s:%s" % (plugin["groupId"], plugin["artifactId"])
            seen.add(gav)
            c = classify_plugin_tree(tree, plugin_ga, embedded_ga)
            bundle_present = bundle_ga in c["direct"]
            remaining = list(c["remove_declared"]) + [t for info in c["declared_exclusions"].values() for t in info["targets"]] \
                + [t for info in c["redeclare"].values() for t in info["targets"]]
            entry = {"plugin": gav, "modules": mapping.get("modules", []), "bundle_present": bundle_present,
                     "remaining": sorted(set(remaining)), "shadowed": list(c["shadowed"]), "paths": c["paths"], "status": "ok"}
            promised = expected.get(gav)
            if promised is None:
                if c["hits"]:
                    entry["status"] = "finding"
                    findings.append("%s brings embedded coordinates although the proposal listed none for it (a profile activated it, or the tree changed): %s" % (
                        gav, "; ".join(c["paths"])))
            elif promised.get("action") != "patch":
                entry["status"] = "untouched"
            else:
                if not bundle_present:
                    entry["status"] = "finding"
                    findings.append("%s: the bundle %s is not among its dependencies after the patch" % (gav, bundle_ga))
                if remaining:
                    entry["status"] = "finding"
                    findings.append("%s still brings %s: %s" % (gav, ", ".join(sorted(set(remaining))),
                                                                   "; ".join(p for p in c["paths"] if any(r in p for r in set(remaining)))))
                unexpected = [ga for ga in c["shadowed"] if ga not in (promised.get("shadowed") or {})]
                if unexpected:
                    entry["status"] = "finding"
                    findings.append("%s: %s reached through its own direct dependencies, which the proposal did not list as shadowed" % (gav, ", ".join(unexpected)))
            plugins.append(entry)
    for gav, promised in expected.items():
        if gav not in seen and promised.get("action") == "patch":
            findings.append("%s was patched but no longer appears among the build plugins (changed effective POM?)" % gav)

    result = {"ok": not findings, "findings": findings, "plugins": plugins, "bundle": bundle_ga,
              "patched": sum(1 for e in expected.values() if e.get("action") == "patch"), "checked": len(plugins)}
    write_json(args.out, result)
    findings_path = Path(args.findings_out) if args.findings_out else Path(args.out).with_name(Path(args.out).stem + "-findings.txt")
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text("".join("[plugins] %s\n" % f for f in findings), encoding="utf-8")
    for e in plugins:
        tag = {"ok": "OK", "untouched": "SKIP", "finding": "DIFF"}[e["status"]]
        log("[%s] %s%s%s" % (tag, e["plugin"], (" shadowed: " + ", ".join(e["shadowed"])) if e["shadowed"] else "",
                             (" remaining: " + ", ".join(e["remaining"])) if e["remaining"] else ""))
    if findings:
        log("Plugin dependency check: %d finding(s) to review (details in %s)" % (len(findings), findings_path))
    else:
        log("Plugin dependency check: every patched plugin sees the bundle and no embedded coordinate beyond the shadowed ones")


if __name__ == "__main__":
    main()
