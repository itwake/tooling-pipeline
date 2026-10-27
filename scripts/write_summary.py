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
        lines.append("| %s | %s | %s |" % (c["check"], c["status"].upper(), (c.get("detail") or "").replace("|", "\\|")[:300]))
    return "\n".join(lines)


def outcome(report) -> str:
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
    publish = load(out / "publish.json")

    md = ["# Run summary", ""]
    if source:
        md += ["## Input", "", "- Type: %s" % source["type"], "- Reference: `%s`" % source["ref"]]
        if source.get("app_path"):
            md.append("- Path inside the image: `%s`" % source["app_path"])
        if source.get("digest"):
            md.append("- Image digest: `%s`" % source["digest"])
        if source.get("sha256"):
            md.append("- Artifact SHA-256: `%s`" % source["sha256"])
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
        md += ["## Published POM", "", "- Kept: %s" % (", ".join(coord(d) for d in bundle_pom.get("kept", [])) or "no promoted dependencies")]
        if bundle_pom.get("pruned"):
            md.append("- Pruned (the application never had them): " + ", ".join(coord(d) for d in bundle_pom["pruned"]))
        if bundle_pom.get("aligned"):
            md.append("- Version aligned to the application: " + ", ".join("`%s:%s` %s -> %s" % (d.get("groupId"), d.get("artifactId"), d.get("version_in_reduced_pom"), d.get("version")) for d in bundle_pom["aligned"]))
        if not bundle_pom.get("application_coordinates_known"):
            md.append("- Application coordinates unknown: nothing pruned")
        md.append("")
    if patch:
        md += ["## Consumer patch proposal", "", "See consumer-patch.md. %s" % (
            "Exact per-module change list derived from the dependency tree." if patch.get("tree_provided")
            else "No consumer dependency tree was provided; generic instructions only."), ""]
    if applied:
        md += ["## Consumer POMs %s" % ("(dry run)" if applied.get("dry_run") else "changed"), "", "- Branch: `%s`" % applied.get("branch")]
        for f in applied.get("changed_files", []):
            md.append("- `%s`: %s" % (f["path"], "; ".join(f.get("actions", []))))
        for w in applied.get("warnings", []):
            md.append("- WARNING: %s" % w)
        md.append("")
    if equivalence:
        md += ["## Application equivalence: %s" % outcome(equivalence), "",
               "- Old build: `%s`" % equivalence["old"], "- New build: `%s`" % equivalence["new"],
               "- Bundle: %s" % equivalence["bundle"]["file"]]
        pv = equivalence.get("project_version") or {}
        if pv.get("old") or pv.get("new"):
            md.append("- Application version: %s -> %s" % (pv.get("old") or "unknown", pv.get("new") or "unknown"))
        md += ["", checks_table(equivalence["checks"]), ""]

    (out / "SUMMARY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    log("Summary written to %s" % (out / "SUMMARY.md"))


if __name__ == "__main__":
    main()
