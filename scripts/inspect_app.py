#!/usr/bin/env python3
"""Stage 2: inventory of the application's dependency jars.

For every jar under BOOT-INF/lib (or the equivalent of the detected layout) records
the Maven coordinates (from the jar's own pom.properties), SHA-256 and the facts
that matter for merging: signatures, module-info, multi-release, SPI files, Spring
metadata, native libraries, nested jars.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    fail,
    gav_str,
    identify_gav,
    log,
    read_json,
    scan_jar,
    spring_boot_version,
    utc_now_iso,
    write_json,
)


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
    ap.add_argument("--out", default="out/inventory.json")
    args = ap.parse_args()

    if not args.app and not args.source:
        fail("--source or --app is required")
    path = args.app or read_json(args.source)["local_path"]
    try:
        app = AppArtifact(path)
    except (ValueError, FileNotFoundError, zipfile.BadZipFile) as e:
        fail(str(e))

    log("Inspecting %s (layout: %s)" % (path, app.layout))
    deps = []
    for dj in app.dependency_jars():
        rec = {
            "name": dj.name,
            "path_in_app": dj.path_in_app,
            "size": dj.size,
            "sha256": dj.sha256,
            "gav": None,
            "gav_source": "unknown",
            "candidates": [],
            "flags": {},
            "error": None,
        }
        try:
            with dj.open() as zf:
                gav, src, cands = identify_gav(zf, dj.name)
                rec.update(gav=gav, gav_source=src, candidates=cands, flags=scan_jar(zf))
        except zipfile.BadZipFile as e:
            rec["error"] = "not a valid zip: %s" % e
        deps.append(rec)

    boot = spring_boot_version(deps)
    inventory = {
        "app": {
            "local_path": str(Path(path).resolve()),
            "layout": app.layout,
            "sha256": app.file_sha256(),
            "spring_boot_version": boot,
            "dependency_count": len(deps),
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
            "%s:%s" % (c.get("artifactId"), c.get("version")) for c in d["candidates"][:3]
        )
        log("  %-*s  %s  %s" % (width, d["name"], coord, flag_summary(d["flags"])))
    unknown = [d["name"] for d in deps if not d["gav"]]
    if unknown:
        log("Unidentified coordinates (select them as groupId:artifactId:version): %s" % ", ".join(unknown))
    broken = [d["name"] for d in deps if d["error"]]
    if broken:
        log("Unreadable jars: %s" % ", ".join(broken))
    log("Inventory written to %s" % args.out)


if __name__ == "__main__":
    main()
