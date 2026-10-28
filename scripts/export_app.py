#!/usr/bin/env python3
"""Extract job: turn the fetched application into one build artifact.

Reads the metadata extract_source.py wrote and produces, under --out-dir, a single file named after the
source (or --name) together with its .sha256:

  * a jar or war file is copied as it is, byte for byte;
  * an exploded Spring Boot layout (a buildpacks image's /workspace, a directory from `jar -xf`) is packed
    back into an executable archive: META-INF/MANIFEST.MF first, the nested jars under BOOT-INF/lib or
    WEB-INF/lib stored without compression as the Boot launcher requires, everything else deflated;
  * any other directory (a Jib image's /app with libs and classes) is zipped as it is; that zip is not an
    executable jar and the metadata says so.

Default name: for an image <repository name>-<tag>.<ext>, or <repository name>-<digest prefix>.<ext> when
the reference has no tag; for Maven coordinates the artifact's file name (SNAPSHOT versions resolved); for a
URL or a path the file name. Writes --meta for the summary and for downstream jobs.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, read_json, sha256_file, utc_now_iso, write_json  # noqa: E402

MANIFEST = "META-INF/MANIFEST.MF"
NESTED_JAR_PREFIXES = ("BOOT-INF/lib/", "WEB-INF/lib/", "WEB-INF/lib-provided/")


def image_name_and_tag(ref: str, digest: Optional[str]) -> Tuple[str, Optional[str]]:
    """('orders', '1.2.3') for registry:5000/team/orders:1.2.3; the digest prefix stands in for a missing tag."""
    without_digest, _, ref_digest = ref.partition("@")
    last = without_digest.rsplit("/", 1)[-1]
    name, _, tag = last.partition(":")
    if not tag:
        hexdigest = ""
        for candidate in (ref_digest, digest or ""):
            m = re.search(r"sha256:([0-9a-f]{12,})", candidate)
            if m:
                hexdigest = m.group(1)
                break
        tag = hexdigest[:12] or None
    return name or "app", tag


def layout_of(local: Path) -> str:
    if local.is_file():
        return "file"
    if (local / "BOOT-INF").is_dir():
        return "boot-exploded"
    if (local / "WEB-INF").is_dir():
        return "war-exploded"
    return "lib-dir"


def default_name(meta: dict, local: Path, ext: str) -> str:
    if meta.get("type") == "image":
        name, tag = image_name_and_tag(meta.get("ref") or "", meta.get("digest"))
        return "%s-%s%s" % (name, tag, ext) if tag else name + ext
    if local.is_file():
        return local.name
    return local.name + ext


def repack(src: Path, dest: Path) -> Tuple[int, int]:
    """Exploded Boot layout -> executable archive. Returns (entries, nested jars stored)."""
    files = sorted(p for p in src.rglob("*") if p.is_file())
    rel = {p: p.relative_to(src).as_posix() for p in files}
    manifest = [p for p in files if rel[p] == MANIFEST]
    if not manifest:
        fail("%s has no %s; it is not an exploded archive" % (src, MANIFEST))
    ordered = manifest + [p for p in files if rel[p] != MANIFEST]
    stored = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in ordered:
            name = rel[p]
            if name.startswith(NESTED_JAR_PREFIXES) and name.lower().endswith(".jar"):
                zf.write(p, name, compress_type=zipfile.ZIP_STORED)  # the Boot launcher opens nested jars in place
                stored += 1
            else:
                zf.write(p, name, compress_type=zipfile.ZIP_DEFLATED)
    return len(ordered), stored


def zip_dir(src: Path, dest: Path) -> int:
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(src).as_posix())
    return len(files)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="out/source.json", help="metadata written by extract_source.py")
    ap.add_argument("--out-dir", default="out/artifact", help="directory receiving the artifact and its .sha256")
    ap.add_argument("--name", default="", help="file name of the artifact (default: derived from the source)")
    ap.add_argument("--meta", default="out/app-artifact.json")
    args = ap.parse_args()

    meta = read_json(args.source)
    local = Path(meta.get("local_path") or "")
    if not str(local) or not local.exists():
        fail("the fetched application is missing: %s (run extract_source.py first)" % local)
    layout = layout_of(local)
    if layout == "file" and not zipfile.is_zipfile(local):
        fail("%s is not a jar or war (not a zip archive); check the path inside the image (DEFAULT_APP_PATH or #/path in the reference)" % local)
    ext = local.suffix if layout == "file" else (".war" if layout == "war-exploded" else ".jar" if layout == "boot-exploded" else ".zip")
    name = args.name.strip() or default_name(meta, local, ext)
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        fail("--name must be a file name, got '%s'" % name)

    out_dir = Path(args.out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    dest = out_dir / name
    note = None
    entries = stored = None
    if layout == "file":
        shutil.copyfile(local, dest)
        kind, executable = "copy", True
        log("Copied %s -> %s" % (local, dest))
    elif layout in ("boot-exploded", "war-exploded"):
        entries, stored = repack(local, dest)
        kind, executable = "repack", True
        note = "packed back from the exploded layout at %s: %d entries, %d nested jars stored without compression" % (meta.get("app_path") or local, entries, stored)
        log("Repacked %s -> %s (%s)" % (local, dest, note))
    else:
        entries = zip_dir(local, dest)
        kind, executable = "zip", False
        note = "the application at %s is a directory of classes and libraries (Jib layout), not an archive; this zip holds it as it is and is not an executable jar" % (meta.get("app_path") or local)
        log("Zipped %s -> %s (%d entries)" % (local, dest, entries))

    sha = sha256_file(dest)
    (out_dir / (name + ".sha256")).write_text("%s  %s\n" % (sha, name), encoding="utf-8")
    result = {
        "name": name,
        "file": str(dest),
        "sha256": sha,
        "size": dest.stat().st_size,
        "kind": kind,
        "executable": executable,
        "layout": layout,
        "entries": entries,
        "nested_jars_stored": stored,
        "note": note,
        "source": {k: meta.get(k) for k in ("type", "ref", "app_path", "digest", "sha256", "maven", "registry", "fetched_at")},
        "created_at": utc_now_iso(),
    }
    write_json(args.meta, result)
    log("Artifact %s (%d bytes, sha256 %s)%s" % (name, result["size"], sha, "" if executable else "; not an executable archive"))
    if meta.get("type") == "image":
        log("  from image %s%s" % (meta.get("ref"), (" (%s)" % meta["digest"]) if meta.get("digest") else ""))
    elif meta.get("maven"):
        log("  from %s (resolved %s)" % (meta.get("ref"), meta["maven"].get("resolved_version")))
    else:
        log("  from %s %s" % (meta.get("type"), meta.get("ref")))


if __name__ == "__main__":
    main()
