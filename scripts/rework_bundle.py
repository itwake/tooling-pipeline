#!/usr/bin/env python3
"""Change job, stage 2: plan the change to a published bundle and generate the Shade project that builds the new one.

Inputs are the bundle fetch_bundle.py downloaded (jar, POM, metadata), the jars to add (files uploaded to the job,
--add-jar; coordinates fetch_bundle.py fetched; a resolve_selection.py selection taken out of an application,
--selection) and the components to remove (--remove, and the -tokens of CHANGES). The new bundle is Shade's
merge of the added jars followed by a copy of the current bundle, with the transformers of the original build:

  * an entry only an added jar has is added;
  * an entry an added jar and the bundle both have is taken from the added jar, so a hot fix wins over the
    copy inside the bundle; the plan lists those entries as replaced;
  * an added jar whose groupId:artifactId is already embedded replaces that component: the component's
    entries are removed from the bundle copy first (its jar is fetched from the repository; only entries with
    identical bytes are removed, and merged resources lose the lines it contributed), then the new jar is
    merged like any other addition. When the repository does not have the old jar, only the entries the new
    jar also has are replaced, and the plan warns that entries the new version dropped stay in the bundle;
  * an added jar identical to an embedded component is skipped;
  * a removed component leaves the bundle copy the same way (its jar comes from the repository) and becomes an
    ordinary dependency of the published POM, so the consumer keeps it on the classpath, managed by Maven
    again. Related components leave together, as they enter together (the family, the same groupId and
    version). An entry the component shared with others stays when another component carries the same
    bytes, and is put back from that component's jar when the removed copy had shadowed it; the bundle's
    overlaps record (BUNDLE-OVERLAPS.txt) says which copies a component the repository lacks carried.

Coordinates of an uploaded jar come from its pom.properties, else from the repository (checksum search,
confirmed by the .sha1), else synthetic coordinates as in the main job. The version is --new-version or
derived: <version>.1 for a release, counting up when the bundle is itself a change (<base>.N), the same
version for a SNAPSHOT. The manifest written for the following stages has the shape of the main job's, so
install_components.py, finalize_bundle_pom.py, verify_bundle.py and publish_bundle.py run unchanged.
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import zipfile
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    BUNDLE_BASE_ATTR,
    BUNDLE_BOOT_ATTR,
    BUNDLE_CONTENTS_ATTR,
    BUNDLE_CONTENTS_FILE,
    BUNDLE_OVERLAPS_FILE,
    BUNDLE_REWORKED_FROM_ATTR,
    BUNDLE_SOURCE_ATTR,
    THIRD_PARTY_DIR,
    classify_entry,
    fail,
    ga_str,
    gav_str,
    identify_gav,
    log,
    parse_contents_attr,
    overlaps_file_text,
    parse_contents_file,
    parse_log4j_plugin_cache,
    parse_overlaps_file,
    parse_properties,
    properties_values,
    read_json,
    read_manifest,
    scan_jar,
    sha1_bytes,
    sha256_bytes,
    significant_lines,
    utc_now_iso,
    write_json,
    write_log4j_plugin_cache,
)
from gen_bundle_project import component_dir, render_pom  # noqa: E402
from identify import SYNTHETIC_GROUP, Repository, SpringBootBom, identify_jar, synthetic_coordinates, synthetic_hint  # noqa: E402
from resolve_selection import family_of  # noqa: E402

SKIPPED_CATEGORIES = ("dir", "manifest", "signature", "index", "module-info")


def derive_version(old: str, base_version: Optional[str]) -> str:
    """<old>.1 for a release, <base>.N+1 when old is already <base>.N, the same version for a SNAPSHOT."""
    if old.endswith("-SNAPSHOT"):
        return old
    if base_version and old != base_version and old.startswith(base_version + "."):
        rest = old[len(base_version) + 1:]
        if rest.isdigit():
            return "%s.%d" % (base_version, int(rest) + 1)
    return old + ".1"


def ga_key(g: dict) -> str:
    return ga_str(g) + ((":" + g["classifier"]) if g.get("classifier") else "")


def describe_data(name: str, data: bytes, origin: str, file: Optional[str] = None) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise zipfile.BadZipFile("empty archive")
            flags = scan_jar(zf)
            manifest = read_manifest(zf)
    except zipfile.BadZipFile as e:
        fail("%s is not a jar file (%s)" % (name, e))
    return {"name": name, "file": file, "origin": origin, "data": data, "sha1": sha1_bytes(data),
            "sha256": sha256_bytes(data), "size": len(data), "flags": flags, "manifest": manifest, "warnings": []}


def describe_jar(path: Path, origin: str) -> dict:
    if not path.is_file():
        fail("%s is not a file" % path)
    return describe_data(path.name, path.read_bytes(), origin, str(path))


def match_contents(tok: str, contents: List[dict]) -> List[dict]:
    """Embedded coordinates a removal token names: artifactId (the hint's artifactId for a synthetic one),
    groupId:artifactId, groupId:artifactId:version or groupId:artifactId:version:classifier."""
    p = tok.split(":")
    out = []
    for g in contents:
        hint = synthetic_hint(g) if g.get("groupId") == SYNTHETIC_GROUP else None
        if len(p) == 1:
            if g["artifactId"] == tok or (hint and hint["artifactId"] == tok):
                out.append(g)
        elif len(p) == 2:
            if (g["groupId"], g["artifactId"]) == (p[0], p[1]):
                out.append(g)
        elif (g["groupId"], g["artifactId"], g.get("version")) == (p[0], p[1], p[2]) and (len(p) < 4 or g.get("classifier") == p[3]):
            out.append(g)
    return out


def expand_removals(named: List[dict], contents: List[dict], families: dict, expand: bool = True) -> "OrderedDict[str, dict]":
    """Related components leave the bundle together, as they enter it: the rest of the family and every
    component with the same groupId and version. Synthetic components are never pulled along; with expand
    False only the named components leave."""
    out: "OrderedDict[str, dict]" = OrderedDict((ga_key(g), {"gav": g, "reason": "named in CHANGES"}) for g in named)
    changed = expand
    while changed:
        changed = False
        for rec in list(out.values()):
            g = rec["gav"]
            fam = family_of(g["groupId"], families)
            for o in contents:
                k = ga_key(o)
                if k in out or o.get("groupId") == SYNTHETIC_GROUP:
                    continue
                if fam and family_of(o["groupId"], families) == fam:
                    reason = "family %s, with %s" % (fam, gav_str(g))
                elif o["groupId"] == g["groupId"] and o.get("version") == g.get("version"):
                    reason = "same groupId and version as %s" % gav_str(g)
                else:
                    continue
                out[k] = {"gav": o, "reason": reason}
                changed = True
    return out


class ComponentIndex:
    """The components that stay embedded, as the repository has them: which of them carry a given entry, and
    with which bytes. Loaded on first use, when a removed or replaced component shares entries with others. A
    component the repository lacks (synthetic coordinates, say) is known only through the overlaps record."""

    def __init__(self, repo, components: List[dict], contents_file: dict, record: dict):
        self.repo, self.components, self.contents_file, self.record = repo, list(components), contents_file or {}, record or {}
        self._jars = None
        self._names: Dict[str, set] = {}
        self.warnings: List[str] = []

    def _load(self) -> None:
        if self._jars is not None:
            return
        self._jars = OrderedDict()
        for g in self.components:
            coord = gav_str(g)
            data = None
            if self.repo is not None and g.get("groupId") != SYNTHETIC_GROUP:
                data = self.repo.artifact(g["groupId"], g["artifactId"], g["version"], "jar", g.get("classifier"))
            if data is None:
                self._jars[coord] = None
                if g.get("groupId") != SYNTHETIC_GROUP:
                    self.warnings.append("%s is not in the repository; which shared entries it carries is known only from the bundle's overlaps record" % coord)
                continue
            known = (self.contents_file.get(coord) or {}).get("sha256")
            if known and sha256_bytes(data) != known:
                self.warnings.append("the repository's %s differs from the copy the bundle was built from" % coord)
            zf = zipfile.ZipFile(io.BytesIO(data))
            self._jars[coord] = zf
            self._names[coord] = set(zf.namelist())

    def providers(self, path: str) -> list:
        """[(coordinate, bytes or None)] of the remaining components carrying path, in class-loading order; the
        bytes are None for a component whose jar is unavailable but which the overlaps record names."""
        self._load()
        out = []
        for coord, zf in self._jars.items():
            if zf is not None:
                if path in self._names[coord]:
                    out.append((coord, zf.read(path)))
            else:
                rec = self.record.get(path)
                if rec and coord in [rec["kept"]] + rec["others"]:
                    out.append((coord, None))
        return out

    def close(self) -> None:
        for zf in (self._jars or {}).values():
            if zf is not None:
                zf.close()


def _trim_merged(entries: Dict[str, bytes], n: str, cat: str, data: bytes, report: dict) -> None:
    """Take out of a merged resource of the bundle what one component's copy (data) contributed."""
    if cat in ("services", "append"):
        old_lines = set(significant_lines(data.decode("utf-8", "replace")))
        have = significant_lines(entries[n].decode("utf-8", "replace"))
        removed = [ln for ln in have if ln in old_lines]
        keep = [ln for ln in have if ln not in old_lines]
        if removed:
            report["merged_lines"][n] = removed
        if keep:
            entries[n] = ("\n".join(keep) + "\n").encode("utf-8")
        else:
            del entries[n]
            report["removed"].append(n)
    elif cat == "props-merge":
        old_props = properties_values(parse_properties(data.decode("utf-8", "replace")))
        have = properties_values(parse_properties(entries[n].decode("utf-8", "replace")))
        removed_vals = {}
        for k, vals in list(have.items()):
            drop = [v for v in vals if v in old_props.get(k, [])]
            if drop:
                removed_vals[k] = drop
            have[k] = [v for v in vals if v not in drop]
        have = {k: v for k, v in have.items() if v}
        if removed_vals:
            report["merged_lines"][n] = ["%s=%s" % (k, ",".join(v)) for k, v in removed_vals.items()]
        if have:
            entries[n] = "".join("%s=%s\n" % (k, ",".join(v)) for k, v in have.items()).encode("utf-8")
        else:
            del entries[n]
            report["removed"].append(n)
    elif cat == "log4j-plugins":
        try:
            old_cache = parse_log4j_plugin_cache(data)
            have = parse_log4j_plugin_cache(entries[n])
        except Exception as e:  # noqa: BLE001
            fail("%s cannot be parsed: %s" % (n, e))
        removed_keys = []
        for category, plugins in old_cache.items():
            for key in plugins:
                if key in have.get(category, {}):
                    del have[category][key]
                    removed_keys.append("%s/%s" % (category, key))
        have = {c: p for c, p in have.items() if p}
        if removed_keys:
            report["merged_lines"][n] = removed_keys
        if have:
            entries[n] = write_log4j_plugin_cache(have)
        else:
            del entries[n]
            report["removed"].append(n)


def strip_component(entries: Dict[str, bytes], old: Optional[zipfile.ZipFile], new: Optional[zipfile.ZipFile], old_gav: dict,
                    index: Optional[ComponentIndex] = None, record: Optional[dict] = None) -> dict:
    """Remove an embedded component from the bundle's entries, in place.

    With the component's own jar (old) every entry it carried is handled by kind: merged resources lose the lines it
    contributed, its legal files go, a class or resource whose bytes differ from the bundle's copy stays (another
    component's copy had won). One holding the bundle's bytes goes, unless the index knows a remaining component
    that carries the same path: an identical copy stays (kept_identical), a copy the component had shadowed is put
    back from that component's jar (restored), and one whose jar is unavailable is reported (unrestorable). Without
    the component's jar (old None), the entries the new jar carries too are removed, nothing else."""
    report = {"by": "repository jar" if old is not None else "entries of the new jar", "removed": [], "kept_other": [], "kept_identical": [],
              "restored": {}, "unrestorable": [], "merged_lines": {}, "legal_dir": None, "absent": []}
    legal_dir = THIRD_PARTY_DIR + component_dir(old_gav) + "/"
    gone = [n for n in entries if n.startswith(legal_dir)]
    for n in gone:
        del entries[n]
    if gone:
        report["legal_dir"] = legal_dir
    if old is None:
        for n in (new.namelist() if new is not None else []):
            if classify_entry(n) in ("class", "resource") and n in entries:
                del entries[n]
                report["removed"].append(n)
        return report
    record = record or {}
    for n in old.namelist():
        cat = classify_entry(n)
        if cat in SKIPPED_CATEGORIES or cat == "legal":
            continue
        if n not in entries:
            report["absent"].append(n)
            continue
        data = old.read(n)
        if cat in ("services", "append", "props-merge", "log4j-plugins"):
            _trim_merged(entries, n, cat, data, report)
        elif entries[n] != data:
            report["kept_other"].append(n)
        else:
            others = index.providers(n) if index is not None else []
            if not others:
                del entries[n]
                report["removed"].append(n)
                continue
            coord, copy = others[0]
            if copy is None:
                rec = record.get(n)
                if rec and rec.get("identical"):
                    report["kept_identical"].append(n)
                else:
                    del entries[n]
                    report["removed"].append(n)
                    report["unrestorable"].append("%s (copy of %s)" % (n, coord))
            elif copy == data:
                report["kept_identical"].append(n)
            else:
                entries[n] = copy
                report["restored"][n] = coord
    return report


def overlap(entries: Dict[str, bytes], zf: zipfile.ZipFile) -> dict:
    """Classes and resources an added jar shares with the bundle: replaced (other bytes) and identical."""
    replaced, identical, merged = [], [], []
    for n in zf.namelist():
        cat = classify_entry(n)
        if n not in entries or cat in SKIPPED_CATEGORIES or cat == "legal":
            continue
        if cat in ("services", "append", "props-merge", "log4j-plugins"):
            merged.append(n)
        elif entries[n] == zf.read(n):
            identical.append(n)
        else:
            replaced.append(n)
    return {"replaced": replaced, "identical": identical, "merged": merged}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle-meta", default="out/rework-bundle.json", help="written by fetch_bundle.py")
    ap.add_argument("--add-jar", action="append", default=[], help="jar file uploaded to the job (repeatable)")
    ap.add_argument("--selection", default="", help="resolve_selection.py's selection (--bundle-meta mode) of jars to add from an application")
    ap.add_argument("--remove", action="append", default=[],
                    help="component to remove: artifactId, groupId:artifactId or groupId:artifactId:version[:classifier] (repeatable; -tokens of CHANGES arrive through the metadata)")
    ap.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "config" / "policy.json"),
                    help="policy.json: the families whose members leave the bundle together")
    ap.add_argument("--no-expand", action="store_true", help="remove only the named components, not their family and siblings")
    ap.add_argument("--new-version", default="", help="version of the new bundle (default: derived from the current one)")
    ap.add_argument("--repo-url", default="", help="repository that identifies uploaded jars and holds the removed and replaced components")
    ap.add_argument("--release-url", default="")
    ap.add_argument("--snapshot-url", default="")
    ap.add_argument("--server-id", default="nexus")
    ap.add_argument("--sources-jar", choices=["true", "false"], default="true")
    ap.add_argument("--timestamp", default="2024-01-01T00:00:00Z", help="project.build.outputTimestamp")
    ap.add_argument("--shade-version", default="3.6.0")
    ap.add_argument("--spring-boot-version", default="", help="override the Spring Boot version recorded in the bundle")
    ap.add_argument("--log4j-transform-version", default="0.2.0")
    ap.add_argument("--out", default="work/rework", help="generated Maven project directory")
    ap.add_argument("--manifest-out", default="out/bundle-manifest.json")
    ap.add_argument("--plan-out", default="out/rework-plan.json")
    args = ap.parse_args()

    meta = read_json(args.bundle_meta)
    bundle_gav = meta["bundle"]
    old_coordinate = meta["coordinate"]
    repo = Repository(args.repo_url) if args.repo_url else None
    warnings: List[str] = []

    bundle_path = Path(meta["jar"])
    if not bundle_path.is_file():
        fail("bundle jar not found: %s" % bundle_path)
    bundle_data = bundle_path.read_bytes()
    if sha256_bytes(bundle_data) != meta.get("sha256"):
        fail("%s changed since fetch_bundle.py downloaded it" % bundle_path.name)
    bz = zipfile.ZipFile(io.BytesIO(bundle_data))
    attrs = read_manifest(bz)
    contents = parse_contents_attr(attrs.get(BUNDLE_CONTENTS_ATTR, "")) or meta.get("contents") or []
    if not contents:
        fail("%s carries no %s attribute; not an mvn-rebundle bundle" % (bundle_path.name, BUNDLE_CONTENTS_ATTR))
    contents_file = parse_contents_file(bz.read(BUNDLE_CONTENTS_FILE).decode("utf-8", "replace")) if BUNDLE_CONTENTS_FILE in bz.namelist() else {}
    record = parse_overlaps_file(bz.read(BUNDLE_OVERLAPS_FILE).decode("utf-8", "replace")) if BUNDLE_OVERLAPS_FILE in bz.namelist() else {}
    boot_version = args.spring_boot_version or attrs.get(BUNDLE_BOOT_ATTR) or meta.get("manifest", {}).get("spring_boot_version")
    source_attr = attrs.get(BUNDLE_SOURCE_ATTR) or "bundle=%s" % old_coordinate
    base = attrs.get(BUNDLE_BASE_ATTR) or old_coordinate
    base_version = base.split(":")[2] if base.count(":") >= 2 else None
    if args.new_version:
        new_version, version_source = args.new_version, "parameter"
    else:
        new_version, version_source = derive_version(bundle_gav["version"], base_version), "derived"
    if new_version == bundle_gav["version"] and not new_version.endswith("-SNAPSHOT"):
        fail("the new version equals the current one (%s); release repositories do not accept a redeploy, choose another NEW_VERSION" % new_version)
    if repo is not None and not new_version.endswith("-SNAPSHOT") and repo.exists(bundle_gav["groupId"], bundle_gav["artifactId"], new_version, "pom"):
        fail("%s:%s:%s is already published; choose another NEW_VERSION" % (bundle_gav["groupId"], bundle_gav["artifactId"], new_version))

    # ---- the components to remove ----------------------------------------------------------
    policy = read_json(args.policy) if Path(args.policy).is_file() else {}
    named: List[dict] = []
    for tok in list(args.remove) + list(meta.get("remove_tokens") or []):
        hits = match_contents(tok, contents)
        if not hits:
            fail("'-%s' matches no component of %s; it embeds %s" % (tok, old_coordinate, ", ".join(gav_str(g) for g in contents)))
        if len(hits) > 1:
            fail("'-%s' matches several components of %s: %s; use groupId:artifactId:version" % (tok, old_coordinate, ", ".join(gav_str(g) for g in hits)))
        if hits[0] not in named:
            named.append(hits[0])
    removals = expand_removals(named, contents, policy.get("families", {}), expand=not args.no_expand)
    if removals and len(removals) == len(contents):
        fail("removing %s leaves nothing in the bundle; to stop using it, run the consumer job with BUNDLE_COORDINATES=%s:%s:none" % (
            ", ".join(removals), bundle_gav["groupId"], bundle_gav["artifactId"]))
    for rec in removals.values():
        if rec["gav"].get("groupId") == SYNTHETIC_GROUP:
            fail("%s cannot be removed: it is embedded under synthetic coordinates, so no repository jar tells its entries apart; rebuild the bundle with the main job (SELECT: @%s and -%s)" % (
                gav_str(rec["gav"]), old_coordinate, rec["gav"]["artifactId"]))
    if removals and repo is None:
        fail("removing a component needs --repo-url: its jar is fetched from the repository to tell its entries apart")

    # ---- the jars to add ----------------------------------------------------------------
    added: List[dict] = []
    for item in meta.get("added") or []:
        d = describe_jar(Path(item["jar"]), "repository")
        d.update({"gav": item["gav"], "gav_source": "coordinates", "synthetic": False, "pom_file": item.get("pom"), "sources": item.get("sources")})
        added.append(d)
    bom = None
    if repo is not None and boot_version:
        bom = SpringBootBom(repo, boot_version)
        if not bom.load():
            bom = None
    for p in args.add_jar:
        d = describe_jar(Path(p), "upload")
        d.update({"pom_file": None, "sources": None, "synthetic": False})
        with zipfile.ZipFile(io.BytesIO(d["data"])) as zf:
            gav, src, _ = identify_gav(zf, d["name"])
        if gav is None:
            gav, src, note, _ = identify_jar(d["name"], d["sha1"], d["manifest"], repo, bom, None)
            if gav is None:
                gav, hint = synthetic_coordinates(d["name"], d["sha1"])
                src = "synthetic"
                d["synthetic"], d["hint"] = True, hint
                d["warnings"].append("coordinates unknown (%s); embedded under synthetic coordinates %s, the consumer proposal looks it up by artifactId" % (note, gav_str(gav)))
        d["gav"], d["gav_source"] = gav, src
        added.append(d)
    # jars taken out of an application (SOURCE_REF): the selection already skipped what the bundle embeds
    source = None
    if args.selection:
        sel = read_json(args.selection)
        if not sel.get("ok", True):
            fail("the selection of jars to add has errors; fix CHANGES")
        app_info = sel.get("app") or {}
        source = {"ref": app_info.get("ref") or app_info.get("local_path"), "type": app_info.get("type"), "sha256": app_info.get("sha256"),
                  "spring_boot_version": sel.get("spring_boot_version"), "tokens": sel.get("tokens"),
                  "skipped": (sel.get("embedded_mode") or {}).get("skipped") or [], "conflicts": (sel.get("embedded_mode") or {}).get("conflicts") or []}
        origin = "application %s" % Path(str(source["ref"] or "")).name
        app = AppArtifact(app_info["local_path"])
        by_path = {j.path_in_app: j for j in app.dependency_jars()}
        for s in sel.get("selected", []):
            dj = by_path.get(s["path_in_app"])
            if dj is None:
                fail("%s disappeared from the application" % s["path_in_app"])
            if dj.sha256 != s["sha256"]:
                fail("%s changed since the selection was made" % s["path_in_app"])
            d = describe_data(dj.name, dj.data, origin)
            synthetic = s.get("gav_source") == "synthetic"
            d.update({"gav": s["gav"], "gav_source": s.get("gav_source") or "pom.properties", "synthetic": synthetic,
                      "hint": s.get("synthetic_hint") if synthetic else None, "pom_file": None, "sources": None,
                      "selection_reason": (s.get("reasons") or ["selected"])[0]})
            if synthetic:
                d["warnings"].append("coordinates unknown; embedded under synthetic coordinates %s, the consumer proposal looks it up by artifactId" % gav_str(s["gav"]))
            added.append(d)
        app.close()
        warnings += [f["message"] for f in sel.get("findings", []) if f["level"] == "warn"]
        if source["spring_boot_version"] and boot_version and source["spring_boot_version"] != boot_version:
            warnings.append("the application the jars come from was built with Spring Boot %s, the bundle with %s: the jars may expect other versions of the embedded libraries (version drift)" % (
                source["spring_boot_version"], boot_version))
    if not added and not removals:
        fail("nothing to change: upload a jar (ADD_JAR), or put jars to add and -components to remove into CHANGES")
    seen: Dict[str, str] = {}
    for d in added:
        key = ga_key(d["gav"])
        if key in seen:
            fail("%s and %s are both %s; add one of them" % (seen[key], d["name"], key))
        seen[key] = d["name"]
        if ga_str(d["gav"]) == ga_str(bundle_gav):
            fail("%s has the bundle's own coordinates %s" % (d["name"], ga_str(bundle_gav)))
        if key in removals:
            fail("%s is both added and removed (%s)" % (d["name"], key))
        if d["flags"].get("signed"):
            d["warnings"].append("signed jar; its signature is stripped by the merge")
        if d["flags"].get("native_libs"):
            d["warnings"].append("ships %d native librar%s; verify the library does not locate them through its own jar" % (
                len(d["flags"]["native_libs"]), "y" if len(d["flags"]["native_libs"]) == 1 else "ies"))
        if d["flags"].get("nested_jars"):
            d["warnings"].append("contains nested jars: %s" % ", ".join(d["flags"]["nested_jars"][:5]))

    # ---- what each addition does to the embedded components --------------------------------
    embedded_by_key = {ga_key(g): g for g in contents}
    for d in added:
        old = embedded_by_key.get(ga_key(d["gav"]))
        d["replaces"] = None
        if old is None:
            d["action"] = "add"
            continue
        known = contents_file.get(gav_str(old), {}).get("sha256")
        if old.get("version") == d["gav"].get("version") and known == d["sha256"]:
            d["action"] = "skip"
            d["reason"] = "already embedded with identical bytes"
            continue
        d["action"] = "replace"
        d["replaces"] = {"gav": old, "coordinate": gav_str(old), "same_version": old.get("version") == d["gav"].get("version"),
                         "embedded_sha256": known}
        if d["replaces"]["same_version"]:
            d["warnings"].append("same coordinates as the embedded %s but other bytes (a patched build); the embedded copy is replaced" % gav_str(old))
    active = [d for d in added if d["action"] != "skip"]
    if not active and not removals:
        fail("nothing to do: %s" % "; ".join("%s is %s" % (d["name"], d["reason"]) for d in added), 1)

    # ---- the bundle copy Shade sees: the bundle minus its own descriptors, the removed and the replaced components -----
    entries: Dict[str, bytes] = {n: bz.read(n) for n in bz.namelist() if not n.endswith("/")}
    own_maven = "META-INF/maven/%s/%s/" % (bundle_gav["groupId"], bundle_gav["artifactId"])
    for n in [n for n in entries if n.startswith(own_maven) or n in (BUNDLE_CONTENTS_FILE, BUNDLE_OVERLAPS_FILE)]:
        del entries[n]
    replaced_keys = {ga_key(d["replaces"]["gav"]) for d in active if d["replaces"]}
    remaining = [g for g in contents if ga_key(g) not in removals and ga_key(g) not in replaced_keys]
    index = ComponentIndex(repo, remaining, contents_file, record)
    stale_entries: List[str] = []
    stale_lines: Dict[str, List[str]] = {}
    for key, rec in removals.items():
        g = rec["gav"]
        data = repo.artifact(g["groupId"], g["artifactId"], g["version"], "jar", g.get("classifier"))
        if data is None:
            fail("%s cannot be removed: its jar is not in the repository, so its entries cannot be told from the others'; rebuild the bundle with the main job" % gav_str(g))
        known = (contents_file.get(gav_str(g)) or {}).get("sha256")
        if known and sha256_bytes(data) != known:
            warnings.append("the repository's %s differs from the copy the bundle was built from; entries whose bytes differ stay in the bundle" % gav_str(g))
        with zipfile.ZipFile(io.BytesIO(data)) as old_zip:
            report = strip_component(entries, old_zip, None, g, index, record)
        if report["unrestorable"]:
            fail("%s cannot be removed: it shadowed copies that cannot be restored because their jar is not in the repository (%s); rebuild the bundle with the main job" % (
                gav_str(g), ", ".join(report["unrestorable"][:5])))
        rec.update({"strip": report, "jar": data, "sha256": sha256_bytes(data), "sha1": sha1_bytes(data),
                    "pom": repo.pom(g["groupId"], g["artifactId"], g["version"])})
        stale_entries += report["removed"]
        for n, lines in report["merged_lines"].items():
            stale_lines.setdefault(n, []).extend(lines)
    for d in active:
        if not d["replaces"]:
            continue
        old_gav = d["replaces"]["gav"]
        old_zip = None
        if repo is not None:
            old_bytes = repo.artifact(old_gav["groupId"], old_gav["artifactId"], old_gav["version"], "jar", old_gav.get("classifier"))
            if old_bytes is not None:
                old_zip = zipfile.ZipFile(io.BytesIO(old_bytes))
                known = d["replaces"]["embedded_sha256"]
                d["replaces"]["repository_sha256_matches"] = (sha256_bytes(old_bytes) == known) if known else None
                if known and sha256_bytes(old_bytes) != known:
                    d["warnings"].append("the repository's %s differs from the copy the bundle was built from; entries whose bytes differ stay in the bundle" % gav_str(old_gav))
        if old_zip is None:
            d["warnings"].append("%s is not in the repository: only the entries the new jar carries too are replaced; entries the new version dropped stay in the bundle" % gav_str(old_gav))
        # the index (every remaining component, fetched) is consulted only when the record says the component shared entries
        shared = any(gav_str(old_gav) in [o["kept"]] + o["others"] for o in record.values())
        with zipfile.ZipFile(io.BytesIO(d["data"])) as new_zip:
            report = strip_component(entries, old_zip, new_zip, old_gav, index if shared else None, record)
        if old_zip is not None:
            old_zip.close()
        d["replaces"]["strip"] = report
        if report["restored"]:
            d["warnings"].append("restored %d entr%s the replaced component had shadowed: %s" % (
                len(report["restored"]), "y" if len(report["restored"]) == 1 else "ies", ", ".join("%s from %s" % kv for kv in list(report["restored"].items())[:5])))
        stale_entries += report["removed"]
        for n, lines in report["merged_lines"].items():
            stale_lines.setdefault(n, []).extend(lines)
    warnings += index.warnings
    for d in active:
        with zipfile.ZipFile(io.BytesIO(d["data"])) as zf:
            d["overlap"] = overlap(entries, zf)
        if d["overlap"]["replaced"]:
            d["warnings"].append("replaces %d entr%s the bundle already holds with other bytes (a hot fix): %s" % (
                len(d["overlap"]["replaced"]), "y" if len(d["overlap"]["replaced"]) == 1 else "ies", ", ".join(d["overlap"]["replaced"][:10])))
    for i, d in enumerate(active):  # added jars overlapping each other: the first declared wins, as in the main job
        for e in active[i + 1:]:
            with zipfile.ZipFile(io.BytesIO(d["data"])) as a, zipfile.ZipFile(io.BytesIO(e["data"])) as b:
                shared = sorted(n for n in set(a.namelist()) & set(b.namelist())
                                if classify_entry(n) in ("class", "resource") and a.read(n) != b.read(n))
            if shared:
                warnings.append("%s and %s both carry %d entr%s with different bytes; %s comes first and wins: %s" % (
                    d["name"], e["name"], len(shared), "y" if len(shared) == 1 else "ies", d["name"], ", ".join(shared[:10])))

    # ---- the overlaps record of the new bundle: the old one minus what left, plus what the added jars share -----
    gone = {gav_str(rec["gav"]) for rec in removals.values()} | {d["replaces"]["coordinate"] for d in active if d["replaces"]}
    new_overlaps: Dict[str, dict] = {}
    for path, o in record.items():
        left = [p for p in [o["kept"]] + o["others"] if p not in gone]
        if len(left) >= 2 and path in entries:
            new_overlaps[path] = {"kept": left[0], "others": left[1:], "identical": o["identical"]}
    for d in active:
        coord = gav_str(d["gav"])
        for kind, identical in (("replaced", False), ("identical", True)):
            for n in d["overlap"][kind]:
                prev = new_overlaps.get(n) or record.get(n)
                others = ([prev["kept"]] + prev["others"]) if prev else [old_coordinate]
                new_overlaps[n] = {"kept": coord, "others": [p for p in others if p != coord and p not in gone] or [old_coordinate], "identical": identical}
    index.close()

    # ---- transformers: a merged resource present in several inputs -----------------------------
    counts: Counter = Counter()
    multi_release = attrs.get("Multi-Release", "").strip().lower() == "true"
    for n in entries:
        if classify_entry(n) in ("append", "props-merge", "log4j-plugins"):
            counts[n] += 1
    for d in active:
        with zipfile.ZipFile(io.BytesIO(d["data"])) as zf:
            for n in zf.namelist():
                if classify_entry(n) in ("append", "props-merge", "log4j-plugins"):
                    counts[n] += 1
        multi_release = multi_release or bool(d["flags"].get("multi_release"))
    transformers = {
        "services": True,
        "append": sorted(n for n, c in counts.items() if c > 1 and classify_entry(n) == "append"),
        "props_merge": sorted(n for n, c in counts.items() if c > 1 and classify_entry(n) == "props-merge"),
        "log4j_plugins": any(c > 1 for n, c in counts.items() if classify_entry(n) == "log4j-plugins"),
        "multi_release": multi_release,
    }
    if transformers["props_merge"] and not boot_version:
        fail("%s occur in the bundle and an added jar, but the bundle records no Spring Boot version to merge them; pass --spring-boot-version" % ", ".join(transformers["props_merge"]))

    # ---- the project ---------------------------------------------------------------------
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    components_dir = out / "components"
    components_dir.mkdir(parents=True)
    res_root = out / "src" / "main" / "resources" / THIRD_PARTY_DIR.rstrip("/")
    res_root.mkdir(parents=True)

    bundle_input = components_dir / bundle_path.name
    with zipfile.ZipFile(bundle_input, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, data in entries.items():
            zf.writestr(n, data)
    bundle_input_sha = sha256_bytes(bundle_input.read_bytes())
    components, inputs, install, unidentified = [], [], [], []
    for d in active:
        target = components_dir / d["name"]
        target.write_bytes(d["data"])
        copied = []
        with zipfile.ZipFile(io.BytesIO(d["data"])) as zf:
            for n in zf.namelist():
                if classify_entry(n) == "legal":
                    dest = res_root / component_dir(d["gav"]) / n[len("META-INF/"):]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(n))
                    copied.append(n)
        components.append({"gav": d["gav"], "coordinate": gav_str(d["gav"]), "file": d["name"], "path_in_app": None, "sha256": d["sha256"],
                           "size": d["size"], "legal_files": copied, "synthetic": d["synthetic"], "hint": d.get("hint"), "flags": d["flags"],
                           "origin": d["origin"], "action": d["action"], "replaces": d["replaces"]["coordinate"] if d["replaces"] else None})
        inputs.append({"name": d["name"], "file": str(target), "sha256": d["sha256"], "gav": d["gav"], "role": "added",
                       "third_party_dir": component_dir(d["gav"])})
        install.append({"name": d["name"], "file": str(target), "gav": d["gav"], "synthetic": d["synthetic"], "sha1": d["sha1"], "sha256": d["sha256"],
                        "pom_file": d.get("pom_file"), "sources": d.get("sources")})
        if d["synthetic"]:
            unidentified.append({"name": d["name"], "file": str(target), "gav": d["gav"], "hint": d.get("hint"), "sha1": d["sha1"], "sha256": d["sha256"]})
    for g in contents:
        if ga_key(g) in replaced_keys or ga_key(g) in removals:
            continue
        rec = contents_file.get(gav_str(g), {})
        components.append({"gav": g, "coordinate": gav_str(g), "file": rec.get("file"), "path_in_app": None, "sha256": rec.get("sha256"),
                           "origin": "carried"})
    inputs.append({"name": bundle_input.name, "file": str(bundle_input), "sha256": bundle_input_sha, "gav": bundle_gav, "role": "bundle",
                   "third_party_dir": None})
    install.append({"name": bundle_input.name, "file": str(bundle_input), "gav": bundle_gav, "synthetic": False,
                    "sha1": sha1_bytes(bundle_input.read_bytes()), "sha256": bundle_input_sha, "pom_file": meta.get("pom"), "sources": meta.get("sources")})
    # a removed component becomes an ordinary dependency of the published POM: the consumer keeps it on the
    # classpath, Maven manages it again. Its jar and POM are installed too, so the Shade build resolves them locally
    unbundled_dir = out / "unbundled"
    for key, rec in removals.items():
        g = rec["gav"]
        unbundled_dir.mkdir(exist_ok=True)
        name = "%s-%s%s.jar" % (g["artifactId"], g["version"], ("-" + g["classifier"]) if g.get("classifier") else "")
        target = unbundled_dir / name
        target.write_bytes(rec["jar"])
        pom_file = None
        if rec.get("pom"):
            pom_file = unbundled_dir / ("%s-%s.pom" % (g["artifactId"], g["version"]))
            pom_file.write_text(rec["pom"], encoding="utf-8")
        else:
            warnings.append("%s has no POM in the repository; a POM without dependencies is generated for the build, and the published POM declares it without its own dependencies" % gav_str(g))
        install.append({"name": name, "file": str(target), "gav": g, "synthetic": False, "sha1": rec["sha1"], "sha256": rec["sha256"],
                        "pom_file": str(pom_file) if pom_file else None, "sources": None, "role": "unbundled"})

    contents_attr = ",".join(c["coordinate"] for c in components)
    dependencies = [d["gav"] for d in active] + [bundle_gav] + [rec["gav"] for rec in removals.values()]
    includes = []
    for g in [d["gav"] for d in active] + [bundle_gav]:
        if ga_str(g) not in includes:
            includes.append(ga_str(g))
    new_coordinate = "%s:%s:%s" % (bundle_gav["groupId"], bundle_gav["artifactId"], new_version)
    change = "; ".join(["removed %s" % gav_str(rec["gav"]) for rec in removals.values()]
                       + [("%s %s" % ("replaced %s by" % d["replaces"]["coordinate"] if d["replaces"] else "added", gav_str(d["gav"]))) for d in active])
    source_desc = "bundle %s, changed: %s" % (old_coordinate, change)
    pom = render_pom(bundle_gav["groupId"], bundle_gav["artifactId"], new_version, dependencies, includes, transformers, boot_version,
                     args.log4j_transform_version, args.shade_version, args.sources_jar, args.timestamp, source_desc, source_attr, contents_attr,
                     args.release_url, args.snapshot_url, args.server_id,
                     extra_manifest={BUNDLE_BASE_ATTR: base, BUNDLE_REWORKED_FROM_ATTR: old_coordinate})
    (out / "pom.xml").write_text(pom, encoding="utf-8")

    lines = ["# Dependency bundle %s" % new_coordinate,
             "# Changed from %s on %s: %s" % (old_coordinate, utc_now_iso()[:10], change),
             "# Source: %s" % source_attr,
             "# coordinate  sha256  original-file"]
    for c in components:
        lines.append("%s  %s  %s" % (c["coordinate"], c.get("sha256") or "unknown", c.get("file") or "unknown"))
    (res_root / "BUNDLE-CONTENTS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (res_root / "BUNDLE-OVERLAPS.txt").write_text(overlaps_file_text(
        ["Dependency bundle %s" % new_coordinate, "Entries carried by several components; the bundle keeps the first copy in class-loading order"],
        new_overlaps), encoding="utf-8")

    plan_added = []
    for d in added:
        rec = {k: d.get(k) for k in ("name", "origin", "action", "reason", "gav", "gav_source", "synthetic", "hint", "sha256", "size",
                                     "replaces", "overlap", "warnings", "selection_reason")}
        rec["coordinate"] = gav_str(d["gav"])
        plan_added.append(rec)
    plan_removed = []
    for key, rec in removals.items():
        s = rec["strip"]
        plan_removed.append({"coordinate": gav_str(rec["gav"]), "gav": rec["gav"], "reason": rec["reason"], "removed_entries": len(s["removed"]),
                             "kept_other": len(s["kept_other"]), "kept_identical": len(s["kept_identical"]), "restored": s["restored"],
                             "merged_lines": sorted(s["merged_lines"]), "legal_dir": s["legal_dir"], "pom_in_repository": bool(rec.get("pom"))})
    rework = {
        "from": old_coordinate,
        "from_version": bundle_gav["version"],
        "base": base,
        "new_version": new_version,
        "version_source": version_source,
        "added": [gav_str(d["gav"]) for d in active if not d["replaces"]],
        "replaced": [{"old": d["replaces"]["coordinate"], "new": gav_str(d["gav"])} for d in active if d["replaces"]],
        "removed": plan_removed,
        "skipped": [{"name": d["name"], "coordinate": gav_str(d["gav"]), "reason": d["reason"]} for d in added if d["action"] == "skip"],
        "stale": {"entries": sorted(set(stale_entries)), "lines": stale_lines},
        "pom_dependencies": meta.get("pom_dependencies") or [],
        "source": source,
    }
    manifest = {
        "bundle": {"groupId": bundle_gav["groupId"], "artifactId": bundle_gav["artifactId"], "version": new_version},
        "coordinate": new_coordinate,
        "contents_attr": contents_attr,
        "source": {"type": "bundle", "ref": old_coordinate, "sha256": meta.get("sha256"), "local_path": str(bundle_path), "repository": meta.get("repository")},
        "source_desc": source_desc,
        "components": components,
        "inputs": inputs,
        "install": install,
        "unidentified": unidentified,
        "classpath_source": "change order: added jars first, then the bundle",
        "transformers": transformers,
        "spring_boot_version": boot_version,
        "shade_version": args.shade_version,
        "log4j_transform_version": args.log4j_transform_version if transformers["log4j_plugins"] else None,
        "sources_jar": args.sources_jar == "true",
        "timestamp": args.timestamp,
        "project_dir": str(out),
        "expected_jar": str(out / "target" / ("%s-%s.jar" % (bundle_gav["artifactId"], new_version))),
        "reduced_pom": str(out / "target" / "dependency-reduced-pom.xml"),
        "pom": str(out / "target" / ("%s-%s.pom" % (bundle_gav["artifactId"], new_version))),
        "application_coordinates": [{"groupId": d.get("groupId"), "artifactId": d.get("artifactId"), "version": d.get("version")}
                                    for d in (meta.get("pom_dependencies") or []) if d.get("groupId") and d.get("artifactId")],
        "rework": rework,
        "generated_at": utc_now_iso(),
    }
    write_json(args.manifest_out, manifest)
    plan = {"bundle": old_coordinate, "new_coordinate": new_coordinate, "version": {"old": bundle_gav["version"], "new": new_version, "source": version_source},
            "base": base, "added": plan_added, "removed": plan_removed, "source": source, "transformers": transformers, "spring_boot_version": boot_version,
            "warnings": warnings, "stale": rework["stale"], "contents_attr": contents_attr, "project_dir": str(out), "generated_at": manifest["generated_at"]}
    write_json(args.plan_out, plan)

    log("Change of %s -> %s (version %s)" % (old_coordinate, new_coordinate, version_source))
    for r in plan_removed:
        log("  [REMOVE] %s (%s): %d entries removed%s%s%s; it becomes a dependency of the published POM" % (
            r["coordinate"], r["reason"], r["removed_entries"],
            (", %d kept as identical copies of other components" % r["kept_identical"]) if r["kept_identical"] else "",
            (", %d restored from the components they had shadowed" % len(r["restored"])) if r["restored"] else "",
            (", %d merged resource(s) trimmed" % len(r["merged_lines"])) if r["merged_lines"] else ""))
    for d in added:
        tag = d["action"].upper()
        if d["action"] == "skip":
            log("  [%s] %s: %s" % (tag, d["name"], d["reason"]))
            continue
        extra = ""
        if d["replaces"]:
            s = d["replaces"]["strip"]
            extra = " replaces %s (removed %d entr%s%s%s, by %s)" % (
                d["replaces"]["coordinate"], len(s["removed"]), "y" if len(s["removed"]) == 1 else "ies",
                (", %d kept as another component's copy" % len(s["kept_other"])) if s["kept_other"] else "",
                (", %d merged resource(s) trimmed" % len(s["merged_lines"])) if s["merged_lines"] else "", s["by"])
        ov = d["overlap"]
        log("  [%s] %s as %s (%s, %s)%s; %d replaced, %d identical, %d merged" % (
            tag, d["name"], gav_str(d["gav"]), d["gav_source"], d["origin"], extra, len(ov["replaced"]), len(ov["identical"]), len(ov["merged"])))
        for w in d["warnings"]:
            log("         WARN: %s" % w)
    for w in warnings:
        log("  WARN: %s" % w)
    log("  contents   : %s" % contents_attr)
    log("  transformers: services%s%s%s%s" % (
        ", append(%d)" % len(transformers["append"]) if transformers["append"] else "",
        ", props-merge(%d) via spring-boot-maven-plugin %s" % (len(transformers["props_merge"]), boot_version) if transformers["props_merge"] else "",
        ", Multi-Release" if transformers["multi_release"] else "",
        ", log4j-plugins" if transformers["log4j_plugins"] else ""))
    log("Change project generated in %s; plan in %s" % (out, args.plan_out))


if __name__ == "__main__":
    main()
