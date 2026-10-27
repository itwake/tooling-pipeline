#!/usr/bin/env python3
"""Verify pipeline: compare the application built before the POM change with the one built after it.

The check reports, it does not decide. The two builds may come from different commits (the consumer
moved on since the artifact the bundle was built from was made), so every difference is listed with
where it sits, and the person approving the change judges:

  dependencies   third-party jars removed, added or at another version; an embedded coordinate still
                 on the classpath; classes or resources of the third-party jars missing, new or with
                 different bytes in class-loading order (merged SPI / Spring resources compared as
                 sets of entries). With both builds from the same commit, any of these means the swap
                 itself is wrong.
  application    the project's own modules rebuilt under another version (CI version stamping) and
                 their entries, the application's own classes and resources; entries that embed the
                 build version are marked. Expected when the commits differ.

The source commit of each build is read from git.properties / build-info.properties when present, so
the report says whether the builds come from the same commit. The exit code is 0 whenever the
comparison ran; --fail-on dependencies|any turns findings into failures for callers that want a gate.
Findings are also written one per line to a text file next to the JSON report, for the pipeline.
"""
from __future__ import annotations

import argparse
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    BUNDLE_CONTENTS_ATTR,
    THIRD_PARTY_DIR,
    classify_entry,
    fail,
    ga_str,
    gav_str,
    identify_gav,
    log,
    match_any,
    parse_manifest,
    parse_properties,
    properties_values,
    read_json,
    read_manifest,
    sha256_bytes,
    significant_lines,
    write_json,
)

DEPS, APP = "dependencies", "application"
SHOW_GROUPS = 12   # provider pairs printed per check
SHOW_PATHS = 6     # paths printed per provider pair
LINE_LIMIT = 300   # characters per line of the findings file


def parse_contents_attr(value: str):
    out = []
    for tok in value.split(","):
        parts = tok.strip().split(":")
        if len(parts) >= 3:
            g = {"groupId": parts[0], "artifactId": parts[1], "version": parts[2]}
            if len(parts) > 3 and parts[3]:
                g["classifier"] = parts[3]
            out.append(g)
    return out


def parse_contents_file(text):
    """BUNDLE-CONTENTS.txt lines 'coordinate  sha256  file' -> {coordinate: sha256}."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def app_identity(app: AppArtifact) -> dict:
    """What identifies a build: the application's Maven coordinates (META-INF/maven/<g>/<a>/pom.properties
    at the root, else build-info.properties), the package of its Start-Class, and the source commit
    (git.properties of the git-commit-id plugin) and build time (build-info.properties) when present."""
    ident = {"groupId": None, "artifactId": None, "version": None, "start_package": None,
             "commit": None, "commit_time": None, "branch": None, "build_time": None}
    build_info = None
    for n, loader in app.app_entries():
        if n.startswith("META-INF/maven/") and n.endswith("/pom.properties") and not ident["groupId"]:
            props = parse_properties(loader().decode("utf-8", "replace"))
            if props.get("groupId") and props.get("artifactId") and props.get("version"):
                ident.update({"groupId": props["groupId"], "artifactId": props["artifactId"], "version": props["version"]})
        elif n.endswith("META-INF/build-info.properties") and build_info is None:
            build_info = parse_properties(loader().decode("utf-8", "replace"))
        elif n.endswith("git.properties") and n.count("/") <= 2 and not ident["commit"]:
            props = parse_properties(loader().decode("utf-8", "replace"))
            ident["commit"] = props.get("git.commit.id.full") or props.get("git.commit.id") or props.get("git.commit.id.abbrev")
            ident["commit_time"] = props.get("git.commit.time")
            ident["branch"] = props.get("git.branch")
        elif n == "META-INF/MANIFEST.MF":
            start = parse_manifest(loader()).get("Start-Class")
            if start and "." in start:
                ident["start_package"] = start.rsplit(".", 1)[0]
    if build_info:
        ident["build_time"] = build_info.get("build.time")
        if not ident["groupId"] and build_info.get("build.group"):
            ident.update({"groupId": build_info.get("build.group"), "artifactId": build_info.get("build.artifact"),
                          "version": build_info.get("build.version")})
    return ident


def same_commit(old_id: dict, new_id: dict):
    """True / False when both commits are known (abbreviated ids compare by prefix), None otherwise."""
    a, b = old_id.get("commit"), new_id.get("commit")
    if not a or not b:
        return None
    return a == b or a.startswith(b) or b.startswith(a)


def describe_build(ident: dict) -> str:
    parts = []
    if ident.get("commit"):
        parts.append("commit %s" % ident["commit"][:12])
        if ident.get("branch"):
            parts.append("branch %s" % ident["branch"])
        if ident.get("commit_time"):
            parts.append("committed %s" % ident["commit_time"])
    if ident.get("build_time"):
        parts.append("built %s" % ident["build_time"])
    return ", ".join(parts) if parts else "no git.properties or build-info.properties"


def is_project_module(gav, ident: dict) -> bool:
    """A dependency jar that is one of the project's own modules: same groupId as the application,
    the application's version, or (when the coordinates are unknown) a groupId that is a prefix of
    the Start-Class package."""
    if not gav:
        return False
    g = gav.get("groupId") or ""
    if ident.get("groupId"):
        return g == ident["groupId"] or (bool(ident.get("version")) and gav.get("version") == ident["version"])
    pkg = ident.get("start_package")
    return bool(pkg and g and (pkg == g or pkg.startswith(g + ".")))


def load_jars(app: AppArtifact, ident: dict):
    jars = []
    for dj in app.dependency_jars():
        with dj.open() as zf:
            gav, _, cands = identify_gav(zf, dj.name)
            attrs = read_manifest(zf)
            contents_sha = {}
            if attrs.get(BUNDLE_CONTENTS_ATTR):
                try:
                    contents_sha = parse_contents_file(zf.read(THIRD_PARTY_DIR + "BUNDLE-CONTENTS.txt").decode("utf-8", "replace"))
                except KeyError:
                    contents_sha = {}
        jars.append({"dep": dj, "name": dj.name, "sha256": dj.sha256, "gav": gav, "candidates": cands,
                     "contents_attr": attrs.get(BUNDLE_CONTENTS_ATTR), "contents_sha": contents_sha,
                     "project": is_project_module(gav, ident)})
    return jars


def collect_entries(jars, skip_prefixes):
    """jars must be in class-loading order: the first provider of a path is the effective one."""
    plain = defaultdict(lambda: defaultdict(list))  # path -> sha -> [jar], insertion order = loading order
    merged_lines = defaultdict(set)                 # services / append -> lines
    merged_props = defaultdict(lambda: defaultdict(set))
    for j in jars:
        with j["dep"].open() as zf:
            for n in zf.namelist():
                cat = classify_entry(n)
                if cat in ("dir", "manifest", "signature", "index", "module-info", "legal"):
                    continue
                if any(n.startswith(p) for p in skip_prefixes):
                    continue
                data = zf.read(n)
                if cat in ("services", "append"):
                    merged_lines[n].update(significant_lines(data.decode("utf-8", "replace")))
                elif cat == "props-merge":
                    for k, vals in properties_values(parse_properties(data.decode("utf-8", "replace"))).items():
                        merged_props[n][k].update(vals)
                else:
                    plain[n][sha256_bytes(data)].append(j["name"])
    return plain, merged_lines, merged_props


def embeds(data: bytes, version) -> bool:
    return bool(version) and version.encode("utf-8") in data


def shorten(text: str, limit: int = LINE_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def group_items(items):
    """{(kind, old provider, new provider): [item]} in first-seen order."""
    groups = OrderedDict()
    for i in items:
        groups.setdefault((i["kind"], i.get("old"), i.get("new")), []).append(i)
    return groups


def group_label(kind, old, new) -> str:
    if kind == "changed":
        return "%s -> %s" % (old or "application", new or "application") if (old or new) else "changed"
    return "%s (%s)" % (old if kind == "missing" else new, kind) if (old or new) else kind


def item_name(i) -> str:
    return i["path"] + (" (embeds the build version)" if i.get("version_stamped") else "")


def grouped_detail(items, groups_limit=SHOW_GROUPS, paths_limit=SHOW_PATHS) -> str:
    parts = []
    groups = group_items(items)
    for (kind, old, new), its in list(groups.items())[:groups_limit]:
        names = [item_name(i) for i in its[:paths_limit]]
        parts.append("%s: %d %s (%s%s)" % (group_label(kind, old, new), len(its), kind, ", ".join(names), ", ..." if len(its) > paths_limit else ""))
    if len(groups) > groups_limit:
        parts.append("... %d more provider pairs" % (len(groups) - groups_limit))
    return "; ".join(parts)


def item_lines(items, paths_limit=SHOW_PATHS):
    lines = []
    groups = group_items(items)
    for (kind, old, new), its in list(groups.items())[:SHOW_GROUPS]:
        names = [item_name(i) for i in its[:paths_limit]]
        lines.append("%s: %d %s: %s%s" % (group_label(kind, old, new), len(its), kind, ", ".join(names),
                                           " (+%d more)" % (len(its) - paths_limit) if len(its) > paths_limit else ""))
    if len(groups) > SHOW_GROUPS:
        lines.append("... %d more provider pairs (see the JSON report)" % (len(groups) - SHOW_GROUPS))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", help="original application (jar/war/dir)")
    ap.add_argument("--new", help="rebuilt application (jar/war/dir)")
    ap.add_argument("--old-source", help="metadata JSON from extract_source.py (instead of --old)")
    ap.add_argument("--new-source", help="metadata JSON from extract_source.py (instead of --new)")
    ap.add_argument("--bundle-artifact-id", default="", help="restrict bundle detection to this artifactId")
    ap.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "config" / "policy.json"))
    ap.add_argument("--fail-on", choices=["none", "dependencies", "any"], default="none",
                    help="exit 1 when findings of this scope exist (default none: report only)")
    ap.add_argument("--out", default="out/equivalence.json")
    ap.add_argument("--findings-out", default="", help="findings one per line (default: <out>-findings.txt)")
    args = ap.parse_args()

    old_path = args.old or (read_json(args.old_source)["local_path"] if args.old_source else None)
    new_path = args.new or (read_json(args.new_source)["local_path"] if args.new_source else None)
    if not old_path or not new_path:
        fail("--old/--new (or --old-source/--new-source) are required")
    ignore_app = read_json(args.policy).get("equivalence", {}).get("ignore_app_entries", []) if Path(args.policy).exists() else []

    checks = []

    def check(name, ok, detail="", scope=DEPS, items=None):
        rec = {"check": name, "status": "pass" if ok else "diff", "scope": scope, "detail": detail}
        if items:
            rec["items"] = items
        checks.append(rec)

    def info(name, detail, scope=APP):
        checks.append({"check": name, "status": "info", "scope": scope, "detail": detail})

    old_app, new_app = AppArtifact(old_path), AppArtifact(new_path)
    log("old: %s (%s)" % (old_path, old_app.layout))
    log("new: %s (%s)" % (new_path, new_app.layout))
    old_id, new_id = app_identity(old_app), app_identity(new_app)
    old_version, new_version = old_id.get("version"), new_id.get("version")
    if old_id.get("groupId") or new_id.get("groupId"):
        log("application: %s:%s %s -> %s" % (old_id.get("groupId") or new_id.get("groupId"), old_id.get("artifactId") or new_id.get("artifactId"),
                                            old_version or "?", new_version or "?"))
    log("old build: %s" % describe_build(old_id))
    log("new build: %s" % describe_build(new_id))
    old_jars, new_jars = load_jars(old_app, old_id), load_jars(new_app, new_id)

    # ---- locate the bundle ---------------------------------------------------------------
    bundles = [j for j in new_jars if j["contents_attr"]]
    if args.bundle_artifact_id:
        bundles = [j for j in bundles if j["gav"] and j["gav"]["artifactId"] == args.bundle_artifact_id]
    if len(bundles) != 1:
        fail("expected exactly one bundle jar in the new application, found %d: %s" % (
            len(bundles), ", ".join(j["name"] for j in bundles)))
    bundle = bundles[0]
    bundle["project"] = False
    embedded = parse_contents_attr(bundle["contents_attr"])
    embedded_ga = {ga_str(g) for g in embedded}
    log("bundle: %s embeds %s" % (bundle["name"], ", ".join(gav_str(g) for g in embedded)))
    check("bundle-present", True, "%s (%s)" % (bundle["name"], gav_str(bundle["gav"]) if bundle["gav"] else "coordinates unknown"))

    # ---- same source commit? ---------------------------------------------------------------
    same = same_commit(old_id, new_id)
    if same is True:
        check("same-source-revision", True, "both builds come from commit %s: a difference in the dependencies below means the swap itself is wrong" % old_id["commit"][:12], scope=APP)
    elif same is False:
        check("same-source-revision", False, "old build: %s; new build: %s: the differences below may come from the code change rather than from the swap" % (
            describe_build(old_id), describe_build(new_id)), scope=APP)
    else:
        info("same-source-revision", "unknown (old build: %s; new build: %s): with both builds from the same commit a difference in the dependencies means the swap is wrong, otherwise it may come from the code change" % (
            describe_build(old_id), describe_build(new_id)))

    # ---- dependency set ------------------------------------------------------------------
    def key(j):
        return gav_str(j["gav"]) if j["gav"] else "file:" + j["name"]

    old_by_key = {key(j): j for j in old_jars}
    new_by_key = {key(j): j for j in new_jars}
    old_by_sha = {j["sha256"]: j for j in old_jars}
    embedded_sha = bundle.get("contents_sha") or {}   # coordinate -> sha256 from BUNDLE-CONTENTS.txt
    replaced, replaced_keys, missing_in_old, other_version = [], set(), [], []
    for g in embedded:
        k = gav_str(g)
        match = old_by_sha.get(embedded_sha.get(k, "")) or old_by_key.get(k)
        if match is None:
            # the old build may carry the coordinate at another version (the consumer moved on)
            same_ga = [j for j in old_jars if j["gav"] and ga_str(j["gav"]) == ga_str(g) and key(j) not in replaced_keys]
            if same_ga:
                match = same_ga[0]
                other_version.append("%s %s in the old build, %s in the bundle" % (ga_str(g), match["gav"]["version"], g["version"]))
            else:
                missing_in_old.append(k)
        if match is not None:
            replaced.append(k)
            replaced_keys.add(key(match))
    parts = []
    if missing_in_old:
        parts.append("not in the old build: " + ", ".join(missing_in_old))
    if other_version:
        parts.append("at another version in the old build: " + "; ".join(other_version))
    check("embedded-coordinates-match-old-build", not parts, "; ".join(parts) if parts else ", ".join(replaced))

    embedded_shas = set(embedded_sha.values())
    leftovers = [key(j) for j in new_jars if j is not bundle
                 and ((j["gav"] and ga_str(j["gav"]) in embedded_ga) or j["sha256"] in embedded_shas)]
    check("original-coordinates-removed", not leftovers,
          "still on the classpath next to the bundle: %s (exclusions incomplete)" % ", ".join(leftovers) if leftovers else "none of the embedded coordinates remain")

    removed = sorted(set(old_by_key) - set(new_by_key) - replaced_keys)
    added = sorted(set(new_by_key) - set(old_by_key) - {key(bundle)})
    # a jar removed and one added with the same groupId:artifactId is a version change; for the
    # project's own modules that is the CI version stamp of the original build
    rebuilt, version_changed = [], []
    for rk in list(removed):
        oj = old_by_key[rk]
        if not oj["gav"]:
            continue
        ga = ga_str(oj["gav"])
        ak = next((k for k in added if new_by_key[k]["gav"] and ga_str(new_by_key[k]["gav"]) == ga), None)
        if ak is None:
            continue
        nj = new_by_key[ak]
        removed.remove(rk)
        added.remove(ak)
        if oj["project"] and nj["project"]:
            rebuilt.append((oj, nj))
        else:
            version_changed.append("%s %s -> %s" % (ga, oj["gav"]["version"], nj["gav"]["version"]))
    parts = []
    if removed:
        parts.append("removed: " + ", ".join(removed))
    if added:
        parts.append("added: " + ", ".join(added))
    if version_changed:
        parts.append("version changed: " + ", ".join(version_changed))
    check("dependency-set-unchanged", not parts,
          "; ".join(parts) if parts else "%d jars kept, %d replaced by the bundle%s" % (
              len(old_by_key) - len(replaced_keys), len(replaced), (", %d project modules rebuilt" % len(rebuilt)) if rebuilt else ""))
    if rebuilt or (old_version and new_version and old_version != new_version):
        parts = []
        if old_version != new_version:
            parts.append("application version %s -> %s" % (old_version or "unknown", new_version or "unknown"))
        if rebuilt:
            parts.append("project modules rebuilt under another version: " + ", ".join(
                "%s %s -> %s" % (ga_str(oj["gav"]), oj["gav"]["version"], nj["gav"]["version"]) for oj, nj in rebuilt))
        check("project-version-differs", False,
              "; ".join(parts) + " (the original build was version-stamped by its CI? entries that embed the version differ below)", scope=APP)

    differing = [k for k in set(old_by_key) & set(new_by_key) if old_by_key[k]["sha256"] != new_by_key[k]["sha256"]]
    check("kept-jars-byte-identical", not differing,
          "same coordinates, different bytes (rebuilt or taken from another repository; entries compared below): %s" % ", ".join(sorted(differing)[:10]) if differing
          else "all kept jars identical")

    # ---- classpath contents --------------------------------------------------------------
    # Maven descriptors inside the jars are not compared: they differ for every rebuilt module
    # (version, the edited pom.xml) and nothing reads them at run time
    skip = [THIRD_PARTY_DIR, "META-INF/maven/"]
    old_plain, old_lines, old_props = collect_entries(old_jars, skip)
    new_plain, new_lines, new_props = collect_entries(new_jars, skip)
    old_by_name, new_by_name = {}, {}
    for j in old_jars:
        old_by_name.setdefault(j["name"], j)
    for j in new_jars:
        new_by_name.setdefault(j["name"], j)

    def first_provider(plain, p):
        return plain[p][next(iter(plain[p]))][0]

    zips = {}

    def entry_bytes(side, jar, path) -> bytes:
        z = zips.get((side, jar["name"]))
        if z is None:
            z = zips[(side, jar["name"])] = jar["dep"].open()
        return z.read(path)

    # effective classpath: for every path, the copy the class loader sees first
    old_eff = {p: next(iter(shas)) for p, shas in old_plain.items()}
    new_eff = {p: next(iter(shas)) for p, shas in new_plain.items()}
    missing = sorted(set(old_eff) - set(new_eff))
    extra = sorted(set(new_eff) - set(old_eff))
    changed = sorted(p for p in set(old_eff) & set(new_eff) if old_eff[p] != new_eff[p])
    items = []
    for kind, paths in (("missing", missing), ("extra", extra), ("changed", changed)):
        for p in paths:
            oj = old_by_name.get(first_provider(old_plain, p)) if kind != "extra" else None
            nj = new_by_name.get(first_provider(new_plain, p)) if kind != "missing" else None
            project = (oj is None or oj["project"]) and (nj is None or nj["project"])
            stamped = False
            if kind == "changed" and project and old_version and new_version and old_version != new_version:
                stamped = embeds(entry_bytes("old", oj, p), old_version) and embeds(entry_bytes("new", nj, p), new_version)
            items.append({"kind": kind, "path": p, "old": oj["name"] if oj else None, "new": nj["name"] if nj else None,
                          "project": project, "version_stamped": stamped})
    third_party = [i for i in items if not i["project"]]
    project_items = [i for i in items if i["project"]]
    check("classpath-entries-identical", not third_party,
          grouped_detail(third_party) if third_party else "%d entries compared, first copy in class-loading order" % len(old_eff),
          items=third_party)
    if project_items:
        stamped_count = sum(1 for i in project_items if i["version_stamped"])
        check("project-module-entries-changed", False,
              "%d entries of the project's own modules differ%s: %s" % (
                  len(project_items), (" (%d embed the build version)" % stamped_count) if stamped_count else "", grouped_detail(project_items)),
              scope=APP, items=project_items)
    collapsed = sorted(p for p in old_plain if len(old_plain[p]) > 1 and len(new_plain.get(p, {})) < len(old_plain[p]) and not p.endswith(".class"))
    check("duplicate-resources-collapsed", not collapsed,
          "resources that existed in several copies now have fewer; libraries reading every copy (getResources) see less: %s" % ", ".join(collapsed[:10])
          if collapsed else "no multi-copy resource lost a copy")

    line_problems = []
    for p in sorted(set(old_lines) | set(new_lines)):
        if old_lines.get(p, set()) != new_lines.get(p, set()):
            line_problems.append("%s (-%s +%s)" % (p, ",".join(sorted(old_lines.get(p, set()) - new_lines.get(p, set()))[:3]),
                                                     ",".join(sorted(new_lines.get(p, set()) - old_lines.get(p, set()))[:3])))
    for p in sorted(set(old_props) | set(new_props)):
        o, n = old_props.get(p, {}), new_props.get(p, {})
        for k in sorted(set(o) | set(n)):
            if o.get(k, set()) != n.get(k, set()):
                line_problems.append("%s key %s" % (p, k))
    check("merged-resources-identical", not line_problems, "; ".join(line_problems[:10]) if line_problems else "%d merged resources compared" % (len(old_lines) + len(old_props)))

    dup_new = sorted(p for p, shas in new_plain.items() if len(shas) > 1)
    dup_old = {p for p, shas in old_plain.items() if len(shas) > 1}
    new_dups = [p for p in dup_new if p not in dup_old]
    check("no-new-duplicate-classes", not new_dups,
          "same path, different bytes, introduced by the change: %s" % ", ".join(new_dups[:10]) if new_dups
          else ("%d pre-existing duplicates unchanged" % len(dup_old) if dup_old else "no duplicates"))

    # ---- application's own entries --------------------------------------------------------
    def app_map(app: AppArtifact):
        classes, others, loaders = {}, {}, {}
        for n, loader in app.app_entries():
            if match_any(n, ignore_app) or classify_entry(n) == "signature":
                continue
            if app.classes_prefix and n.startswith(app.classes_prefix):
                rel = n[len(app.classes_prefix):]
                k = "classes:" + rel
                (classes if rel.endswith(".class") else others)[k] = sha256_bytes(loader())
                loaders[k] = loader
            elif app.layout == new_app.layout == old_app.layout:
                others[n] = sha256_bytes(loader())
                loaders[n] = loader
        return classes, others, loaders

    old_classes, old_others, old_load = app_map(old_app)
    new_classes, new_others, new_load = app_map(new_app)

    def app_diff(old_map, new_map):
        out = []
        for k in sorted(set(old_map) ^ set(new_map)):
            out.append({"kind": "missing" if k in old_map else "extra", "path": k, "version_stamped": False})
        for k in sorted(k for k in set(old_map) & set(new_map) if old_map[k] != new_map[k]):
            stamped = bool(old_version and new_version and old_version != new_version
                           and embeds(old_load[k](), old_version) and embeds(new_load[k](), new_version))
            out.append({"kind": "changed", "path": k, "version_stamped": stamped})
        return out

    def app_detail(diff, what):
        stamped_count = sum(1 for i in diff if i["version_stamped"])
        hint = "all embed the build version" if stamped_count == len(diff) else (
            "%d embed the build version; the rest come from another commit, another JDK or a build-time stamp" % stamped_count if stamped_count
            else "another commit, another JDK or a build-time stamp; compare with javap -v")
        return "%d differing %s (%s): %s%s" % (len(diff), what, hint, ", ".join(item_name(i) for i in diff[:10]), ", ..." if len(diff) > 10 else "")

    class_diff = app_diff(old_classes, new_classes)
    check("application-classes-unchanged", not class_diff,
          app_detail(class_diff, "class entries") if class_diff else "%d classes identical" % len(old_classes), scope=APP, items=class_diff)
    other_diff = app_diff(old_others, new_others)
    check("application-resources-unchanged", not other_diff,
          app_detail(other_diff, "entries") if other_diff else "%d entries identical" % len(old_others), scope=APP, items=other_diff)
    if old_app.layout != new_app.layout:
        check("same-layout", False, "old %s vs new %s: only classes compared outside the dependency jars" % (old_app.layout, new_app.layout), scope=APP)

    for z in zips.values():
        z.close()
    old_app.close()
    new_app.close()

    # ---- report ----------------------------------------------------------------------------
    gated = {"none": (), "dependencies": (DEPS,), "any": (DEPS, APP)}[args.fail_on]
    for c in checks:
        if c["status"] == "diff" and c["scope"] in gated:
            c["status"] = "fail"
    findings = [c for c in checks if c["status"] in ("diff", "fail")]
    failed = [c for c in checks if c["status"] == "fail"]
    deps_count = sum(1 for c in findings if c["scope"] == DEPS)
    result = {
        "old": str(old_path), "new": str(new_path),
        "bundle": {"file": bundle["name"], "gav": bundle["gav"], "embedded": embedded},
        "replaced": replaced,
        "project_version": {"old": old_version, "new": new_version},
        "source_revision": {"old": {k: old_id.get(k) for k in ("commit", "commit_time", "branch", "build_time")},
                            "new": {k: new_id.get(k) for k in ("commit", "commit_time", "branch", "build_time")}, "same": same},
        "checks": checks,
        "findings": [{"check": c["check"], "scope": c["scope"]} for c in findings],
        "failed": [c["check"] for c in failed],
        "fail_on": args.fail_on,
        "ok": not failed,
    }
    write_json(args.out, result)
    findings_path = Path(args.findings_out) if args.findings_out else Path(args.out).with_name(Path(args.out).stem + "-findings.txt")
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    revision = next(c for c in checks if c["check"] == "same-source-revision")
    if findings and revision["status"] != "diff":
        lines.append("[info] source revision %s" % shorten(revision["detail"]))
    for c in findings:
        lines.append("[%s] %s: %s" % (c["scope"], c["check"], shorten(c["detail"])))
    findings_path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    for c in checks:
        tag = c["status"].upper() + ((" " + c["scope"]) if c["status"] in ("diff", "fail") else "")
        log("  [%s] %s%s" % (tag, c["check"], (": " + c["detail"]) if c["detail"] else ""))
        if c.get("items") and c["check"] not in ("application-classes-unchanged", "application-resources-unchanged"):
            for line in item_lines(c["items"]):
                log("        " + line)
    if failed:
        fail("%d finding(s) in the gated scope (--fail-on %s): %s" % (len(failed), args.fail_on, ", ".join(c["check"] for c in failed)), 1)
    if findings:
        log("Comparison done: %d difference(s) to review, %d in the dependencies and %d in the application (details in %s)" % (
            len(findings), deps_count, len(findings) - deps_count, args.out))
    else:
        log("Comparison done: the rebuilt application is equivalent, only the embedded jars were replaced by the bundle")


if __name__ == "__main__":
    main()
