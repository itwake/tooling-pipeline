#!/usr/bin/env python3
"""Stage 6: prove that the built bundle contains exactly the selected jars' content.

Compared against the jars taken from the application (not against whatever Maven
resolved), so a repository jar that differs from the one running in production fails here.

  * every class / resource of every selected jar is present with identical bytes
  * SPI files, Spring append resources and properties resources contain every original entry
  * LICENSE / NOTICE files were relocated under META-INF/third-party/<artifact>-<version>/
  * no signature files, module-info or INDEX.LIST survived; no unexpected extra entries
  * manifest carries the contents attribute (and Multi-Release when needed)
  * the published POM no longer declares the embedded coordinates and promotes only transitive
    dependencies the application contained (finalize_bundle_pom.py)
"""
from __future__ import annotations

import argparse
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import (  # noqa: E402
    AppArtifact,
    BUNDLE_CONTENTS_ATTR,
    THIRD_PARTY_DIR,
    classify_entry,
    fail,
    ga_str,
    log,
    parse_properties,
    properties_values,
    read_json,
    read_manifest,
    sha256_bytes,
    sha256_file,
    significant_lines,
    write_json,
)


def component_dir(gav: dict) -> str:
    d = "%s-%s" % (gav["artifactId"], gav["version"])
    if gav.get("classifier"):
        d += "-" + gav["classifier"]
    return d


def local_tag(el) -> str:
    return el.tag.split("}", 1)[-1]


def reduced_pom_dependencies(path: Path):
    deps = []
    root = ET.parse(path).getroot()
    for el in root:
        if local_tag(el) != "dependencies":
            continue
        for dep in el:
            if local_tag(dep) != "dependency":
                continue
            rec = {}
            for child in dep:
                rec[local_tag(child)] = (child.text or "").strip()
            deps.append(rec)
    return deps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json", help="written by gen_bundle_project.py")
    ap.add_argument("--bundle", help="built bundle jar (default: manifest.expected_jar)")
    ap.add_argument("--pom", help="published POM written by finalize_bundle_pom.py (default: manifest.pom when it exists)")
    ap.add_argument("--reduced-pom", help="check Shade's dependency-reduced POM instead (default: manifest.reduced_pom)")
    ap.add_argument("--out", default="out/bundle-verification.json")
    args = ap.parse_args()

    man = read_json(args.manifest)
    bundle_path = Path(args.bundle or man["expected_jar"])
    final_pom = Path(args.pom or man.get("pom") or "")
    if args.pom or (not args.reduced_pom and str(final_pom) and final_pom.is_file()):
        pom_path, finalized = final_pom, True
    else:
        pom_path, finalized = Path(args.reduced_pom or man["reduced_pom"]), False
    if not bundle_path.is_file():
        fail("bundle jar not found: %s" % bundle_path)
    app_path = (man.get("source") or {}).get("local_path")
    if not app_path:
        fail("manifest has no source.local_path")

    checks = []

    def check(name, ok, detail="", level="fail"):
        checks.append({"check": name, "status": "pass" if ok else level, "detail": detail})

    # ---- collect expectations from the original jars -------------------------------------
    app = AppArtifact(app_path)
    by_path = {j.path_in_app: j for j in app.dependency_jars()}
    expected = {}  # entry -> sha
    services_exp = defaultdict(set)
    append_exp = defaultdict(set)
    props_exp = defaultdict(lambda: defaultdict(set))
    legal_exp = {}  # bundle path -> sha
    for comp in man["components"]:
        dj = by_path.get(comp["path_in_app"])
        if dj is None:
            fail("%s disappeared from the application" % comp["path_in_app"])
        if dj.sha256 != comp["sha256"]:
            fail("%s changed since the bundle was generated" % comp["path_in_app"])
        cdir = THIRD_PARTY_DIR + component_dir(comp["gav"]) + "/"
        with dj.open() as zf:
            for n in zf.namelist():
                cat = classify_entry(n)
                if cat in ("dir", "manifest", "signature", "index", "module-info"):
                    continue
                data = zf.read(n)
                if cat == "legal":
                    legal_exp[cdir + n[len("META-INF/"):]] = sha256_bytes(data)
                elif cat == "services":
                    services_exp[n].update(significant_lines(data.decode("utf-8", "replace")))
                elif cat == "append":
                    append_exp[n].update(significant_lines(data.decode("utf-8", "replace")))
                elif cat == "props-merge":
                    for k, vals in properties_values(parse_properties(data.decode("utf-8", "replace"))).items():
                        props_exp[n][k].update(vals)
                elif n not in expected:  # first copy wins, as in the application's class-loading order
                    expected[n] = sha256_bytes(data)
    app.close()

    # ---- bundle contents -----------------------------------------------------------------
    bz = zipfile.ZipFile(bundle_path)
    bnames = [n for n in bz.namelist() if not n.endswith("/")]
    bset = set(bnames)

    missing = [p for p in expected if p not in bset]
    changed = [p for p in expected if p in bset and sha256_bytes(bz.read(p)) != expected[p]]
    check("classes-and-resources-identical", not missing and not changed,
          "expected %d entries; missing %d (%s); changed %d (%s)" % (
              len(expected), len(missing), ", ".join(missing[:10]), len(changed), ", ".join(changed[:10])))

    def containment(kind, exp_map, parser):
        problems = []
        for path, lines in exp_map.items():
            if path not in bset:
                problems.append("%s missing" % path)
                continue
            have = parser(bz.read(path).decode("utf-8", "replace"))
            lost = sorted(lines - have)
            if lost:
                problems.append("%s lost %s" % (path, ", ".join(lost[:5])))
        check(kind, not problems, "; ".join(problems[:10]) if problems else "%d file(s) verified" % len(exp_map))

    containment("spi-services-merged", services_exp, lambda t: set(significant_lines(t)))
    containment("spring-append-resources-merged", append_exp, lambda t: set(significant_lines(t)))

    props_problems = []
    for path, keys in props_exp.items():
        if path not in bset:
            props_problems.append("%s missing" % path)
            continue
        have = properties_values(parse_properties(bz.read(path).decode("utf-8", "replace")))
        for k, vals in keys.items():
            lost = sorted(vals - set(have.get(k, [])))
            if lost:
                props_problems.append("%s key %s lost %s" % (path, k, ", ".join(lost[:5])))
    check("spring-properties-merged", not props_problems,
          "; ".join(props_problems[:10]) if props_problems else "%d file(s) verified" % len(props_exp))

    legal_missing = [p for p, s in legal_exp.items() if p not in bset or sha256_bytes(bz.read(p)) != s]
    check("legal-files-relocated", not legal_missing,
          "missing/changed: %s" % ", ".join(legal_missing[:10]) if legal_missing else "%d file(s) under %s" % (len(legal_exp), THIRD_PARTY_DIR))

    forbidden = [n for n in bnames if classify_entry(n) in ("signature", "index", "module-info")]
    check("no-signatures-or-module-info", not forbidden, ", ".join(forbidden[:10]))

    b = man["bundle"]
    own_maven = "META-INF/maven/%s/%s/" % (b["groupId"], b["artifactId"])
    allowed_extra = lambda n: (  # noqa: E731
        n == "META-INF/MANIFEST.MF"
        or n.startswith(own_maven)
        or n.startswith(THIRD_PARTY_DIR)
        or n in services_exp
        or n in append_exp
        or n in props_exp
    )
    extras = [n for n in bnames if n not in expected and not allowed_extra(n) and classify_entry(n) not in ("signature", "index", "module-info")]
    check("no-unexpected-entries", not extras, ", ".join(extras[:15]))

    attrs = read_manifest(bz)
    contents_attr = attrs.get(BUNDLE_CONTENTS_ATTR, "")
    check("manifest-contents-attribute", contents_attr == man["contents_attr"],
          "found '%s', expected '%s'" % (contents_attr, man["contents_attr"]))
    want_mr = bool(man.get("transformers", {}).get("multi_release"))
    has_mr = attrs.get("Multi-Release", "").lower() == "true"
    check("manifest-multi-release", want_mr == has_mr, "Multi-Release=%s, expected %s" % (has_mr, want_mr))

    promoted = []
    if pom_path.is_file():
        deps = reduced_pom_dependencies(pom_path)
        embedded_ga = {ga_str(c["gav"]) for c in man["components"]}
        still = [d for d in deps if "%s:%s" % (d.get("groupId"), d.get("artifactId")) in embedded_ga]
        promoted = [d for d in deps if d not in still]
        check("reduced-pom-drops-embedded", not still,
              "still declared: %s" % ", ".join("%s:%s" % (d.get("groupId"), d.get("artifactId")) for d in still) if still
              else "%d remaining dependencies: %s" % (len(promoted), ", ".join("%s:%s:%s" % (d.get("groupId"), d.get("artifactId"), d.get("version")) for d in promoted)))
        app_ga = {ga_str(g) for g in man.get("application_coordinates") or []}
        foreign = [d for d in promoted if "%s:%s" % (d.get("groupId"), d.get("artifactId")) not in app_ga] if app_ga else []
        if not finalized:
            check("pom-promotes-only-application-dependencies", False,
                  "finalize_bundle_pom.py did not run: Shade's dependency-reduced POM would be published as is%s" % (
                      " (it promotes %s, which the application never had)" % ", ".join("%s:%s" % (d.get("groupId"), d.get("artifactId")) for d in foreign) if foreign else ""),
                  level="warn")
        elif not app_ga:
            check("pom-promotes-only-application-dependencies", False, "application coordinates unknown (inventory missing when the project was generated); nothing was pruned", level="warn")
        else:
            check("pom-promotes-only-application-dependencies", not foreign,
                  "promoted although the application never had them: %s" % ", ".join("%s:%s" % (d.get("groupId"), d.get("artifactId")) for d in foreign) if foreign
                  else "%d promoted dependencies, all present in the application" % len(promoted))
    else:
        check("reduced-pom-drops-embedded", False, "POM not found at %s" % pom_path, level="warn")

    if man.get("sources_jar"):
        src_jar = bundle_path.with_name(bundle_path.stem + "-sources.jar")
        check("sources-jar-present", src_jar.is_file(), str(src_jar), level="warn")

    failed = [c for c in checks if c["status"] == "fail"]
    result = {
        "bundle": str(bundle_path),
        "sha256": sha256_file(bundle_path),
        "size": bundle_path.stat().st_size,
        "entry_count": len(bnames),
        "expected_entry_count": len(expected),
        "pom": str(pom_path) if pom_path.is_file() else None,
        "pom_finalized": finalized and pom_path.is_file(),
        "checks": checks,
        "promoted_dependencies": promoted,
        "ok": not failed,
    }
    write_json(args.out, result)
    log("Bundle %s (%d entries, sha256 %s)" % (bundle_path.name, len(bnames), result["sha256"][:16]))
    log("POM %s%s" % (pom_path, "" if finalized else " (dependency-reduced, not finalized)"))
    for c in checks:
        log("  [%s] %s%s" % (c["status"].upper(), c["check"], (": " + c["detail"]) if c["detail"] else ""))
    if failed:
        fail("%d check(s) failed" % len(failed), 1)
    log("Bundle verification passed")


if __name__ == "__main__":
    main()
