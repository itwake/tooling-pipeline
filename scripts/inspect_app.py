#!/usr/bin/env python3
"""Stage 2: inventory of the application's dependency jars.

For every jar under BOOT-INF/lib (or the equivalent of the detected layout) records the
Maven coordinates, SHA-1 / SHA-256, the dependencies declared in its embedded pom.xml and
the facts that matter for merging: signatures, module-info, multi-release, SPI files, Spring
metadata, native libraries, nested jars.

Coordinates come from the jar's pom.properties. Jars without one (Tomcat, BouncyCastle, ...)
are identified through the Spring Boot BOM, the manifest vendor id and the Nexus search API,
and every candidate is confirmed against the repository's .sha1 (--repo-url).
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    declared_dependencies,
    fail,
    gav_str,
    identify_gav,
    log,
    read_json,
    read_manifest,
    scan_jar,
    sha1_bytes,
    spring_boot_version,
    utc_now_iso,
    write_json,
)
from identify import Repository, SpringBootBom, identify_jar, synthetic_coordinates  # noqa: E402


def flag_summary(flags: dict) -> str:
    bits = []
    if flags.get("signed"):
        bits.append("SIGNED")
    if flags.get("module_info"):
        bits.append("module-info")
    if flags.get("multi_release"):
        bits.append("multi-release")
    if flags.get("services"):
        bits.append("spi:%d" % len(flags["services"]))
    if flags.get("append_resources"):
        bits.append("spring-append")
    if flags.get("props_merge_resources"):
        bits.append("spring-props")
    if flags.get("native_libs"):
        bits.append("native:%d" % len(flags["native_libs"]))
    if flags.get("nested_jars"):
        bits.append("nested-jars")
    return ",".join(bits)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="metadata JSON written by extract_source.py")
    ap.add_argument("--app", help="application jar/war or directory (instead of --source)")
    ap.add_argument("--repo-url", default="", help="Maven repository used to identify jars without pom.properties (Nexus group URL)")
    ap.add_argument("--central", action="store_true", help="also query search.maven.org by checksum")
    ap.add_argument("--no-lookup", action="store_true", help="never touch the network")
    ap.add_argument("--out", default="out/inventory.json")
    args = ap.parse_args()

    if not args.app and not args.source:
        fail("--source or --app is required")
    path = args.app or read_json(args.source)["local_path"]
    try:
        app = AppArtifact(path)
    except (ValueError, FileNotFoundError, zipfile.BadZipFile) as e:
        fail(str(e))

    order_names, order_source = app.classpath_order()
    log("Inspecting %s (layout: %s, class-loading order from %s)" % (path, app.layout, order_source))
    deps = []
    for index, dj in enumerate(app.dependency_jars()):
        rec = {
            "name": dj.name,
            "path_in_app": dj.path_in_app,
            "classpath_index": index,
            "size": dj.size,
            "sha256": dj.sha256,
            "sha1": sha1_bytes(dj.data),
            "gav": None,
            "gav_source": "unknown",
            "identification_note": None,
            "candidates": [],
            "manifest": {},
            "declared_dependencies": [],
            "synthetic": None,
            "synthetic_hint": None,
            "flags": {},
            "error": None,
        }
        try:
            with dj.open() as zf:
                gav, src, cands = identify_gav(zf, dj.name)
                man = read_manifest(zf)
                rec.update(gav=gav, gav_source=src, candidates=cands, flags=scan_jar(zf),
                           manifest={k: man[k] for k in ("Implementation-Title", "Implementation-Version", "Implementation-Vendor-Id",
                                                         "Bundle-SymbolicName", "Bundle-Version", "Automatic-Module-Name") if k in man})
                if gav:
                    rec["declared_dependencies"] = declared_dependencies(zf, gav)
        except zipfile.BadZipFile as e:
            rec["error"] = "not a valid zip: %s" % e
        deps.append(rec)

    boot = spring_boot_version(deps)
    unknown = [d for d in deps if not d["gav"] and not d["error"]]
    if unknown:
        # what the other jars declare in their own pom.xml identifies jars without a descriptor offline
        declared_index = {}
        for d in deps:
            for dep in d.get("declared_dependencies", []):
                declared_index.setdefault(dep["artifactId"], set()).add((dep["groupId"], d["name"]))
        repo, bom = None, None
        if args.repo_url and not args.no_lookup:
            repo = Repository(args.repo_url, central=args.central)
            if boot:
                bom = SpringBootBom(repo, boot)
                if bom.load():
                    log("Loaded %d BOM(s) for Spring Boot %s: %s" % (len(bom.boms), boot, ", ".join(bom.boms)))
                else:
                    log("Spring Boot BOM %s not available from %s" % (boot, args.repo_url))
        for d in unknown:
            gav, src, note, cands = identify_jar(d["name"], d["sha1"], d["manifest"], repo, bom, declared_index)
            d["identification_note"] = note if (repo or gav) else "no repository configured for checksum lookup (--repo-url)"
            if gav:
                d["gav"], d["gav_source"] = gav, src
                log("  identified %s as %s (%s)" % (d["name"], gav_str(gav), src))
            else:
                d["candidates"] = (d["candidates"] or []) + [c for c in cands if c not in d["candidates"]]
                d["synthetic"], d["synthetic_hint"] = synthetic_coordinates(d["name"], d["sha1"])

    inventory = {
        "app": {
            "local_path": str(Path(path).resolve()),
            "layout": app.layout,
            "sha256": app.file_sha256(),
            "spring_boot_version": boot,
            "dependency_count": len(deps),
            "classpath_source": order_source,
            "repo_url": args.repo_url or None,
            "inspected_at": utc_now_iso(),
        },
        "dependencies": deps,
    }
    app.close()
    write_json(args.out, inventory)

    log("Spring Boot version: %s" % (boot or "not detected"))
    log("Dependencies: %d" % len(deps))
    width = max([len(d["name"]) for d in deps] + [10])
    for d in deps:
        coord = gav_str(d["gav"]) if d["gav"] else "?? (%s)" % ", ".join(
            "%s:%s:%s" % (c.get("groupId") or "?", c.get("artifactId"), c.get("version")) for c in d["candidates"][:3])
        log("  %-*s  %s  %s" % (width, d["name"], coord, flag_summary(d["flags"])))
    still_unknown = [d["name"] for d in deps if not d["gav"] and not d["error"]]
    if still_unknown:
        log("Unidentified coordinates: %s" % ", ".join(still_unknown))
        log("  they can still be bundled (under synthetic coordinates); select them as groupId:artifactId:version to name them, or configure --repo-url to look them up")
    broken = [d["name"] for d in deps if d["error"]]
    if broken:
        log("Unreadable jars: %s" % ", ".join(broken))
    log("Inventory written to %s" % args.out)


if __name__ == "__main__":
    main()
