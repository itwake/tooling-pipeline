#!/usr/bin/env python3
"""Collate the JSON outputs of a run into out/SUMMARY.md."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import gav_str, log, read_json  # noqa: E402


def load(path: Path):
    return read_json(path) if path.is_file() else None


def checks_table(checks):
    lines = ["| Check | Result | Detail |", "|---|---|---|"]
    for c in checks:
        status = c["status"].upper() + ((" (%s)" % c["scope"]) if c.get("scope") and c["status"] in ("diff", "fail") else "")
        lines.append("| %s | %s | %s |" % (c["check"], status, (c.get("detail") or "").replace("|", "\\|")[:300]))
    return "\n".join(lines)


def outcome(report) -> str:
    if "findings" in report:  # the comparison with the original artifact reports; the reviewer decides
        if not report.get("ok"):
            return "FAILED (--fail-on %s)" % report.get("fail_on")
        findings = report["findings"]
        if not findings:
            return "equivalent, only the embedded jars were replaced"
        deps = sum(1 for f in findings if f.get("scope") == "dependencies")
        return "%d difference(s) to review (%d in the dependencies, %d in the application)" % (len(findings), deps, len(findings) - deps)
    if not report.get("ok"):
        return "FAILED"
    warned = [c["check"] for c in report.get("checks", []) if c.get("status") == "warn"]
    return "passed with %d warning(s): %s" % (len(warned), ", ".join(warned)) if warned else "passed"


def coord(d) -> str:
    return "`%s:%s:%s`" % (d.get("groupId"), d.get("artifactId"), d.get("version"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--published", default="", help="'true' when the bundle was deployed")
    args = ap.parse_args()
    out = Path(args.out_dir)

    source = load(out / "source.json")
    inventory = load(out / "inventory.json")
    selection = load(out / "selection.json")
    manifest = load(out / "bundle-manifest.json")
    verification = load(out / "bundle-verification.json")
    bundle_pom = load(out / "bundle-pom.json")
    patch = load(out / "consumer-patch.json")
    applied = load(out / "consumer-apply.json")
    equivalence = load(out / "equivalence.json")
    plugin_check = load(out / "plugin-trees-check.json")
    publish = load(out / "publish.json")
    rework = load(out / "rework-plan.json")
    artifact = load(out / "app-artifact.json")

    md = ["# Run summary", ""]
    if rework:
        v = rework["version"]
        md += ["## Rework of `%s`" % rework["bundle"], "",
               "- New coordinates: `%s` (version %s)" % (rework["new_coordinate"], "given" if v.get("source") == "parameter" else "derived"),
               "- Descends from: `%s`" % rework.get("base")]
        for a in rework.get("added", []):
            if a.get("action") == "skip":
                md.append("- `%s` (%s): skipped, %s" % (a["name"], a.get("origin"), a.get("reason")))
                continue
            line = "- `%s` (%s): %s as `%s` (%s)" % (a["name"], a.get("origin"), a.get("action"), a.get("coordinate"), a.get("gav_source"))
            rep = a.get("replaces") or {}
            if rep:
                strip = rep.get("strip") or {}
                line += "; replaces `%s`: %d entries removed by %s%s%s" % (
                    rep.get("coordinate"), len(strip.get("removed") or []), strip.get("by"),
                    (", %d kept as another component's copy" % len(strip["kept_other"])) if strip.get("kept_other") else "",
                    (", lines trimmed in %s" % ", ".join("`%s`" % p for p in strip["merged_lines"])) if strip.get("merged_lines") else "")
            ov = a.get("overlap") or {}
            if ov.get("replaced"):
                line += "; replaces %d entr%s of the bundle: %s" % (len(ov["replaced"]), "y" if len(ov["replaced"]) == 1 else "ies",
                                                                   ", ".join("`%s`" % e for e in ov["replaced"][:10]))
            md.append(line)
            for w in a.get("warnings") or []:
                md.append("  - WARNING: %s" % w)
        for w in rework.get("warnings") or []:
            md.append("- WARNING: %s" % w)
        md.append("")
    if source:
        md += ["## Input", "", "- Type: %s" % source["type"], "- Reference: `%s`" % source["ref"]]
        if source.get("app_path"):
            md.append("- Path inside the image: `%s`" % source["app_path"])
        if source.get("digest"):
            md.append("- Image digest: `%s`" % source["digest"])
        if source.get("sha256"):
            md.append("- Artifact SHA-256: `%s`" % source["sha256"])
        md.append("")
    if artifact:
        kind = {"copy": "the archive as it was fetched, byte for byte", "repack": "packed back from the exploded layout into an executable archive",
                "zip": "a zip of the directory, not an executable archive"}.get(artifact.get("kind"), artifact.get("kind"))
        md += ["## Application artifact", "", "- File: `%s` (%d bytes)" % (artifact["name"], artifact.get("size") or 0),
               "- SHA-256: `%s`" % artifact["sha256"], "- Content: %s" % kind]
        if artifact.get("note"):
            md.append("- Note: %s" % artifact["note"])
        md.append("")
    if inventory:
        app = inventory["app"]
        md += ["## Application inventory", "", "- Layout: %s" % app["layout"], "- Spring Boot: %s" % (app.get("spring_boot_version") or "not detected"),
               "- Dependency jars: %d" % app["dependency_count"]]
        unknown = [d["name"] for d in inventory["dependencies"] if not d.get("gav")]
        if unknown:
            md.append("- Unidentified coordinates: %s" % ", ".join(unknown))
        md.append("")
    if selection:
        md += ["## Selection", ""]
        for d in selection["selected"]:
            md.append("- `%s` from %s (%s)" % (gav_str(d["gav"]) if d.get("gav") else "?", d["name"], (d.get("reasons") or ["selected"])[0]))
        md.append("")
        if selection["findings"]:
            md += ["| Level | Code | Message |", "|---|---|---|"]
            for f in selection["findings"]:
                md.append("| %s | %s | %s |" % (f["level"].upper(), f["code"], f["message"].replace("|", "\\|")))
            md.append("")
    if manifest:
        if publish and publish.get("published"):
            publishing = "deployed to %s (sha256 `%s`)" % (publish["url"], publish["sha256"])
        elif publish and publish.get("dry_run"):
            publishing = "dry run only, nothing uploaded"
        elif publish:
            publishing = "deploy-file FAILED, see the build log"
        elif args.published == "true":
            publishing = "deployed to Nexus"
        else:
            publishing = "not published (build and verification only)"
        md += ["## Bundle", "", "- Coordinates: `%s`" % manifest["coordinate"], "- Contents: `%s`" % manifest["contents_attr"],
               "- Source: %s" % manifest["source_desc"], "- Publishing: %s" % publishing, ""]
    if verification:
        md += ["## Bundle verification: %s" % outcome(verification), "", checks_table(verification["checks"]), ""]
        if verification.get("promoted_dependencies"):
            md.append("Transitive dependencies kept in the published POM: " + ", ".join(coord(d) for d in verification["promoted_dependencies"]))
            md.append("")
    if bundle_pom:
        new = bundle_pom.get("new") or []
        kept = [d for d in bundle_pom.get("kept", []) if d not in new]
        md += ["## Published POM", "", "- Kept: %s" % (", ".join(coord(d) for d in kept) or "no promoted dependencies")]
        if new:
            md.append("- New, promoted from the added jars' POMs (make sure the consumer has or wants them): " + ", ".join(coord(d) for d in new))
        if bundle_pom.get("pruned"):
            md.append("- Pruned (the application never had them): " + ", ".join(coord(d) for d in bundle_pom["pruned"]))
        if bundle_pom.get("pruned_embedded"):
            md.append("- Pruned (declared by an added jar but embedded in the bundle): " + ", ".join(coord(d) for d in bundle_pom["pruned_embedded"]))
        if bundle_pom.get("aligned"):
            md.append("- Version aligned to %s: " % ("the previous bundle POM" if bundle_pom.get("rework") else "the application")
                      + ", ".join("`%s:%s` %s -> %s" % (d.get("groupId"), d.get("artifactId"), d.get("version_in_reduced_pom"), d.get("version")) for d in bundle_pom["aligned"]))
        if not bundle_pom.get("application_coordinates_known") and not bundle_pom.get("rework"):
            md.append("- Application coordinates unknown: nothing pruned")
        md.append("")
    if patch:
        md += ["## Consumer patch proposal", "", "See consumer-patch.md. %s" % (
            "Exact per-module change list derived from the dependency tree." if patch.get("tree_provided")
            else "No consumer dependency tree was provided; generic instructions only.")]
        if patch.get("plugin_tree_provided"):
            entries = patch.get("plugins") or []
            md.append("")
            md.append("Build plugins: %d with an embedded coordinate in their dependency tree (%d to patch, %d skipped), %d without." % (
                len(entries), sum(1 for e in entries if e.get("action") == "patch"), sum(1 for e in entries if e.get("action") != "patch"),
                patch.get("plugins_without_hits", 0)))
            for e in entries:
                md.append("- `%s` (%s): remove %d, exclude on %d, re-declare %d, shadowed %s%s" % (
                    e["plugin"], e.get("action"), len(e.get("remove_declared") or {}), len(e.get("declared_exclusions") or {}),
                    len(e.get("redeclare") or {}), ", ".join("`%s`" % ga for ga in (e.get("shadowed") or {})) or "none",
                    (": " + e["reason"]) if e.get("reason") else ""))
        drift = patch.get("version_drift") or []
        if drift:
            md += ["", "Version drift between the consumer and the bundle: after the switch the embedded libraries run at the bundle's versions, everything else keeps the consumer's."]
            for d in drift:
                where = ", ".join(d.get("modules") or [])
                if d.get("kind") == "embedded":
                    md.append("- WARNING: `%s` resolved at %s by the consumer, the bundle embeds %s (%s)" % (d["coordinate"], d["version"], d["bundle_version"], where))
                else:
                    md.append("- WARNING: `%s` %s (%s scope) belongs to `%s`, embedded at %s (%s)" % (
                        d["coordinate"], d["version"], d.get("scope"), d.get("train"), d["bundle_version"], where))
        variants = [(m["module"], k, k in (m.get("redeclare") or {})) for m in patch.get("modules") or [] for k in (m.get("test_variants") or {})]
        if variants:
            md += ["", "Test jars under another classifier of an embedded coordinate, not inside the bundle: " + ", ".join(
                "`%s` in %s (%s)" % (k, mod, "declared directly with test scope" if redeclared else "kept") for mod, k, redeclared in variants)]
        for m in patch.get("modules") or []:
            for k, v in (m.get("other_variants") or {}).items():
                md.append("- WARNING: `%s` %s (%s scope) in %s goes with the embedded coordinate although the bundle does not contain it" % (
                    k, v.get("version"), v.get("scope"), m["module"]))
        md.append("")
    if applied:
        md += ["## Consumer POMs %s" % ("(dry run)" if applied.get("dry_run") else "changed"), "", "- Branch: `%s`" % applied.get("branch")]
        for f in applied.get("changed_files", []):
            md.append("- `%s`: %s" % (f["path"], "; ".join(f.get("actions", []))))
        for w in applied.get("warnings", []):
            md.append("- WARNING: %s" % w)
        plugin = applied.get("spring_boot_plugin") or {}
        if plugin:
            md.append("- `spring-boot-maven-plugin` aligned to %s, Spring Boot is inside the bundle%s" % (
                plugin.get("version"), (": " + "; ".join(plugin["notes"])) if plugin.get("notes") else ""))
        for p in applied.get("plugins") or []:
            md.append("- plugin `%s`: %s%s" % (p["plugin"], p.get("action"), ("; " + "; ".join(p["notes"])) if p.get("notes") else ""))
        tv = applied.get("test_variants") or {}
        for k in tv.get("declared") or []:
            md.append("- test jar declared with test scope: %s" % k)
        for k in tv.get("kept") or []:
            md.append("- test jar kept: %s" % k)
        md.append("")
    if plugin_check:
        md += ["## Plugin dependency check: %s" % (
            "ok, every patched plugin sees the bundle and no embedded coordinate beyond the shadowed ones" if plugin_check.get("ok")
            else "%d finding(s) to review" % len(plugin_check.get("findings") or [])), ""]
        for f in plugin_check.get("findings") or []:
            md.append("- %s" % f)
        for p in plugin_check.get("plugins") or []:
            md.append("- `%s`: %s%s%s" % (p["plugin"], p.get("status"),
                                          (", shadowed: " + ", ".join("`%s`" % s for s in p["shadowed"])) if p.get("shadowed") else "",
                                          (", remaining: " + ", ".join("`%s`" % r for r in p["remaining"])) if p.get("remaining") else ""))
        md.append("")
    if equivalence:
        md += ["## Application equivalence: %s" % outcome(equivalence), "",
               "- Old build: `%s`" % equivalence["old"], "- New build: `%s`" % equivalence["new"],
               "- Bundle: %s" % equivalence["bundle"]["file"]]
        pv = equivalence.get("project_version") or {}
        if pv.get("old") or pv.get("new"):
            md.append("- Application version: %s -> %s" % (pv.get("old") or "unknown", pv.get("new") or "unknown"))
        rev = equivalence.get("source_revision") or {}
        if rev:
            o, n = rev.get("old") or {}, rev.get("new") or {}
            md.append("- Source commit: old %s, new %s (%s)" % ((o.get("commit") or "unknown")[:12], (n.get("commit") or "unknown")[:12],
                                                               {True: "same", False: "different", None: "unknown"}[rev.get("same")]))
        md += ["", checks_table(equivalence["checks"]), ""]

    (out / "SUMMARY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    log("Summary written to %s" % (out / "SUMMARY.md"))


if __name__ == "__main__":
    main()
