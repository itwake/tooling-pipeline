#!/usr/bin/env python3
"""Change job, stage 1: fetch a published bundle, and the jars to add to it, from the repository.

Downloads the bundle jar, its POM and, when the repository has one, its sources jar by coordinates (SNAPSHOT
versions resolved through maven-metadata.xml; groupId:artifactId alone means the newest release), checks that
the jar is an mvn-rebundle bundle, reads its manifest (contents, source, Spring Boot version, origin) and its
published POM, and writes what rework_bundle.py needs to --meta. The CHANGES parameter (--changes-file) holds
one token per line: groupId:artifactId:version[:classifier] is a jar to add, downloaded the same way (jar and
POM, sources when present), -token names a component to remove (resolved by rework_bundle.py against the
bundle's contents). With --adds-from-source the add tokens are not coordinates but a selection in an
application (resolve_selection.py reads them from --adds-out); only the removals are kept here. Basic auth uses
SOURCE_USERNAME / SOURCE_PASSWORD, as for every other access to the repository.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    BUNDLE_BASE_ATTR,
    BUNDLE_BOOT_ATTR,
    BUNDLE_CONTENTS_ATTR,
    BUNDLE_CONTENTS_FILE,
    BUNDLE_REWORKED_FROM_ATTR,
    BUNDLE_SOURCE_ATTR,
    fail,
    gav_str,
    identify_gav,
    log,
    parse_contents_attr,
    parse_contents_file,
    parse_selection_text,
    read_manifest,
    sha256_file,
    utc_now_iso,
    write_json,
)
from extract_source import maven_artifact_url  # noqa: E402
from identify import Repository, resolve_bundle_ref  # noqa: E402


def parse_add_token(tok: str) -> dict:
    """groupId:artifactId:version[:classifier], the form SELECT tokens and the contents attribute use."""
    p = tok.split(":")
    if len(p) not in (3, 4) or not all(p[:3]):
        fail("--add must be groupId:artifactId:version[:classifier], got '%s'" % tok)
    gav = {"groupId": p[0], "artifactId": p[1], "version": p[2]}
    if len(p) == 4 and p[3]:
        gav["classifier"] = p[3]
    return gav


def maven_ref(gav: dict, packaging: str = "jar", classifier: Optional[str] = None) -> str:
    """extract_source's reference form: groupId:artifactId:version[:packaging[:classifier]]."""
    ref = "%s:%s:%s:%s" % (gav["groupId"], gav["artifactId"], gav["version"], packaging)
    if classifier:
        ref += ":" + classifier
    return ref


class Fetcher:
    def __init__(self, repo: Repository):
        self.repo = repo
        self._metadata = {}

    def _snapshot_metadata(self, gav: dict) -> Optional[str]:
        key = (gav["groupId"], gav["artifactId"], gav["version"])
        if key not in self._metadata:
            url = "%s/%s/%s/%s/maven-metadata.xml" % (self.repo.repo_url, gav["groupId"].replace(".", "/"), gav["artifactId"], gav["version"])
            data = self.repo.get(url)
            if data is None:
                fail("%s: SNAPSHOT metadata not found at %s" % (gav_str(gav), url))
            self._metadata[key] = data.decode("utf-8", "replace")
        return self._metadata[key]

    def fetch(self, gav: dict, out_dir: Path, packaging: str = "jar", classifier: Optional[str] = None,
              optional: bool = False):
        """Download one file of an artifact; returns (path, resolved version) or (None, None) when optional and absent."""
        metadata = self._snapshot_metadata(gav) if gav["version"].endswith("-SNAPSHOT") else None
        url, name, resolved = maven_artifact_url(maven_ref(gav, packaging, classifier), self.repo.repo_url, packaging, metadata)
        data = self.repo.get(url)
        if data is None:
            if optional:
                return None, None
            fail("%s: not found in the repository (%s)" % (gav_str(gav) + (":" + packaging if packaging != "jar" else ""), url))
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / name
        dest.write_bytes(data)
        log("  fetched %s (%d bytes)" % (url, len(data)))
        return dest, resolved


def pom_dependencies(path: Path) -> list:
    deps = []
    root = ET.parse(path).getroot()
    for el in root:
        if el.tag.split("}", 1)[-1] != "dependencies":
            continue
        for dep in el:
            if dep.tag.split("}", 1)[-1] != "dependency":
                continue
            rec = {child.tag.split("}", 1)[-1]: (child.text or "").strip() for child in dep}
            deps.append({"groupId": rec.get("groupId"), "artifactId": rec.get("artifactId"), "version": rec.get("version"),
                         "scope": rec.get("scope") or "compile"})
    return deps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True, help="groupId:artifactId:version of the published bundle, or groupId:artifactId for its newest release")
    ap.add_argument("--add", action="append", default=[], help="groupId:artifactId:version[:classifier] to add from the repository (repeatable)")
    ap.add_argument("--remove", action="append", default=[], help="component to remove (passed on to rework_bundle.py; repeatable)")
    ap.add_argument("--changes-file", default="", help="file with one token per line: a coordinate to add, or -token to remove (the CHANGES parameter)")
    ap.add_argument("--adds-from-source", action="store_true", help="the add tokens select jars in an application instead of naming coordinates")
    ap.add_argument("--adds-out", default="", help="with --adds-from-source: file that receives the add tokens for resolve_selection.py")
    ap.add_argument("--repo-url", required=True, help="repository the bundle and the coordinates are fetched from (Nexus group URL)")
    ap.add_argument("--out", default="work/bundle-in", help="directory for the bundle's files")
    ap.add_argument("--added-out", default="work/added", help="directory for the added coordinates' files")
    ap.add_argument("--meta", default="out/rework-bundle.json")
    args = ap.parse_args()

    repo = Repository(args.repo_url)
    gav = resolve_bundle_ref(repo, args.bundle)
    fetcher = Fetcher(repo)

    log("Fetching bundle %s from %s" % (gav_str(gav), repo.repo_url))
    jar, resolved = fetcher.fetch(gav, Path(args.out))
    pom, _ = fetcher.fetch(gav, Path(args.out), "pom")
    sources, _ = fetcher.fetch(gav, Path(args.out), "jar", "sources", optional=True)
    if sources is None:
        log("  no sources jar published for %s" % gav_str(gav))

    with zipfile.ZipFile(jar) as zf:
        attrs = read_manifest(zf)
        own, _, _ = identify_gav(zf, jar.name)
        contents_text = zf.read(BUNDLE_CONTENTS_FILE).decode("utf-8", "replace") if BUNDLE_CONTENTS_FILE in zf.namelist() else ""
    contents = parse_contents_attr(attrs.get(BUNDLE_CONTENTS_ATTR, ""))
    if not contents or not own or (own["groupId"], own["artifactId"]) != (gav["groupId"], gav["artifactId"]):
        fail("%s is not an mvn-rebundle bundle: it needs its own pom.properties and the %s manifest attribute" % (jar.name, BUNDLE_CONTENTS_ATTR))

    added = []
    tokens = list(args.add)
    removals = list(args.remove)
    if args.changes_file and Path(args.changes_file).is_file():
        for tok in parse_selection_text(Path(args.changes_file).read_text(encoding="utf-8")):
            if tok.startswith("-"):
                if len(tok) > 1:
                    removals.append(tok[1:])
            else:
                tokens.append(tok)
    source_adds = []
    if args.adds_from_source:
        source_adds, tokens = tokens, []
        if args.adds_out:
            Path(args.adds_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.adds_out).write_text("".join(t + "\n" for t in source_adds), encoding="utf-8")
    seen = set()
    for tok in tokens:
        g = parse_add_token(tok)
        if gav_str(g) in seen:
            continue
        seen.add(gav_str(g))
        log("Fetching %s" % gav_str(g))
        a_jar, a_resolved = fetcher.fetch(g, Path(args.added_out), "jar", g.get("classifier"))
        a_pom, _ = fetcher.fetch(g, Path(args.added_out), "pom", optional=True)
        a_sources, _ = fetcher.fetch(g, Path(args.added_out), "jar", "sources", optional=True)
        if a_pom is None:
            log("  WARN: %s has no POM in the repository; a POM without dependencies will be generated for the build" % gav_str(g))
        added.append({"coordinate": gav_str(g), "gav": g, "jar": str(a_jar), "pom": str(a_pom) if a_pom else None,
                      "sources": str(a_sources) if a_sources else None, "resolved_version": a_resolved, "sha256": sha256_file(a_jar)})

    meta = {
        "bundle": gav,
        "coordinate": gav_str(gav),
        "resolved_version": resolved,
        "repository": repo.repo_url,
        "jar": str(jar),
        "pom": str(pom),
        "sources": str(sources) if sources else None,
        "sha256": sha256_file(jar),
        "size": jar.stat().st_size,
        "manifest": {
            "contents": attrs.get(BUNDLE_CONTENTS_ATTR),
            "source": attrs.get(BUNDLE_SOURCE_ATTR),
            "spring_boot_version": attrs.get(BUNDLE_BOOT_ATTR),
            "base": attrs.get(BUNDLE_BASE_ATTR),
            "reworked_from": attrs.get(BUNDLE_REWORKED_FROM_ATTR),
            "multi_release": attrs.get("Multi-Release", "").strip().lower() == "true",
        },
        "contents": contents,
        "contents_file": parse_contents_file(contents_text),
        "pom_dependencies": pom_dependencies(pom),
        "added": added,
        "add_tokens": source_adds,
        "adds_from_source": args.adds_from_source,
        "remove_tokens": removals,
        "fetched_at": utc_now_iso(),
    }
    write_json(args.meta, meta)
    log("Bundle %s: %d embedded coordinate(s), Spring Boot %s, %d promoted dependenc%s in its POM%s" % (
        gav_str(gav), len(contents), attrs.get(BUNDLE_BOOT_ATTR) or "unknown", len(meta["pom_dependencies"]),
        "y" if len(meta["pom_dependencies"]) == 1 else "ies",
        (", reworked from %s" % attrs.get(BUNDLE_REWORKED_FROM_ATTR)) if attrs.get(BUNDLE_REWORKED_FROM_ATTR) else ""))
    if added:
        log("%d coordinate(s) fetched to add: %s" % (len(added), ", ".join(a["coordinate"] for a in added)))
    if source_adds:
        log("%d token(s) to resolve in the application: %s" % (len(source_adds), ", ".join(source_adds)))
    if removals:
        log("%d component(s) to remove: %s" % (len(removals), ", ".join(removals)))
    log("Metadata written to %s" % args.meta)


if __name__ == "__main__":
    main()
