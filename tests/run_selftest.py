#!/usr/bin/env python3
"""End-to-end self-test of the Python stages on synthetic fixtures (no Maven needed).

    python tests/run_selftest.py [workdir]

maven-shade-plugin itself is simulated (tests/make_fixture.py::simulate_shade) according to
the rules the generated project configures; the real Shade run is covered on Jenkins.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import make_fixture as fx  # noqa: E402
from bundlelib import read_json  # noqa: E402

PY = sys.executable
passed = 0


def run(script, *args, expect=0, cwd=None):
    cmd = [PY, "-I", str(SCRIPTS / script), *map(str, args)]
    p = subprocess.run(cmd, cwd=str(cwd or ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    if p.returncode != expect:
        print(p.stdout)
        raise AssertionError("%s exited %d, expected %d" % (script, p.returncode, expect))
    return p.stdout


def ok(name, cond, detail=""):
    global passed
    if not cond:
        raise AssertionError("%s: %s" % (name, detail))
    passed += 1
    print("  ok  %s" % name)


def main() -> None:
    work = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="dep-bundle-selftest-"))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    out = work / "out"
    print("workdir: %s" % work)

    libs = fx.build_libs(work / "fixture" / "libs")
    app = fx.build_app(work / "fixture" / "app.jar", libs)

    # ---- stage 1 + 2 ---------------------------------------------------------------------
    print("[extract + inspect]")
    run("extract_source.py", "--type", "jar", "--ref", app, "--out", work / "app", "--meta", out / "source.json")
    src = read_json(out / "source.json")
    ok("source metadata", src["local_path"] == str(app) and len(src["sha256"]) == 64)

    run("inspect_app.py", "--source", out / "source.json", "--out", out / "inventory.json")
    inv = read_json(out / "inventory.json")
    by_name = {d["name"]: d for d in inv["dependencies"]}
    ok("layout and count", inv["app"]["layout"] == "boot-jar" and inv["app"]["dependency_count"] == len(libs))
    ok("spring boot version detected", inv["app"]["spring_boot_version"] == "3.3.5")
    ok("gav from pom.properties", by_name["commons-io-2.16.1.jar"]["gav"] == {"groupId": "commons-io", "artifactId": "commons-io", "version": "2.16.1"})
    ok("unidentified jar reported", by_name["vendor-sdk-2.0.0.jar"]["gav"] is None and any(c["artifactId"] == "vendor-sdk" for c in by_name["vendor-sdk-2.0.0.jar"]["candidates"]))
    f = by_name["commons-codec-1.17.1.jar"]["flags"]
    ok("flags: multi-release/module-info/spi", f["multi_release"] and f["module_info"] and f["services"] == ["org.apache.commons.codec.Codec"])
    ok("flags: signed", by_name["signed-lib-1.0.jar"]["flags"]["signed"])

    # ---- stage 3: policy ------------------------------------------------------------------
    print("[resolve selection]")
    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "commons-io", "--out", out / "sel-ok.json")
    sel = read_json(out / "sel-ok.json")
    ok("simple selection", [d["gav"]["artifactId"] for d in sel["selected"]] == ["commons-io"] and sel["ok"])

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "spring-core", "--out", out / "sel-deny.json", expect=1)
    sel = read_json(out / "sel-deny.json")
    ok("spring-core denied in strict mode", any(f["code"] == "policy-deny-group" and f["level"] == "error" for f in sel["findings"]))

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "spring-core", "--policy-mode", "warn", "--out", out / "sel-warn.json")
    sel = read_json(out / "sel-warn.json")
    ok("spring-core allowed in warn mode", sel["ok"] and any(f["code"] == "policy-deny-group" and f["level"] == "warn" for f in sel["findings"]))

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "signed-lib", "--out", out / "sel-signed.json", expect=1)
    ok("signed jar refused", any(f["code"] == "signed-jar" for f in read_json(out / "sel-signed.json")["findings"]))

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "vendor-sdk", "--out", out / "sel-unknown.json", expect=1)
    ok("unidentified jar needs explicit coordinates", any(f["code"] == "unidentified" for f in read_json(out / "sel-unknown.json")["findings"]))
    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "com.vendor:vendor-sdk:2.0.0", "--out", out / "sel-explicit.json")
    ok("explicit coordinates accepted", read_json(out / "sel-explicit.json")["selected"][0]["gav"]["groupId"] == "com.vendor")

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "nope-lib", "--out", out / "sel-missing.json", expect=1)
    ok("unknown token fails", any(f["code"] == "not-found" for f in read_json(out / "sel-missing.json")["findings"]))

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "dup-x, dup-y", "--policy-mode", "warn", "--out", out / "sel-dup.json", expect=1)
    sel = read_json(out / "sel-dup.json")
    ok("conflicting class bytes are a hard error", any(f["code"] == "overlap-conflict" for f in sel["findings"]) and sel["overlaps"]["conflicts"][0]["path"] == "com/dup/Same.class")

    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "codec-a\ncodec-b\ncommons-codec", "--out", out / "sel-merge.json")
    sel = read_json(out / "sel-merge.json")
    ok("merge transformers detected", sel["transformers"]["props_merge"] == ["META-INF/spring.factories"]
       and sel["transformers"]["append"] == ["META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"]
       and sel["transformers"]["multi_release"] is True)

    # ---- stage 4: project generation -----------------------------------------------------
    print("[generate bundle project]")
    bundle = {"groupId": "com.company.platform", "artifactId": "thirdparty-bundle", "version": "1.0.0"}
    selection = out / "sel-main.json"
    run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "commons-io\ncommons-codec\ncodec-a\ncodec-b\nspring-core",
        "--policy-mode", "warn", "--out", selection)
    proj = work / "bundle"
    run("gen_bundle_project.py", "--selection", selection, "--source", out / "source.json", "--group-id", bundle["groupId"],
        "--artifact-id", bundle["artifactId"], "--version", bundle["version"], "--out", proj,
        "--release-url", "https://nexus.example.com/repository/maven-releases/", "--snapshot-url", "https://nexus.example.com/repository/maven-snapshots/",
        "--manifest-out", out / "bundle-manifest.json")
    pom = (proj / "pom.xml").read_text(encoding="utf-8")
    man = read_json(out / "bundle-manifest.json")
    ok("pom pins exact versions", "<artifactId>commons-io</artifactId>\n      <version>2.16.1</version>" in pom and "<version>6.1.14</version>" in pom)
    ok("pom includes only the selected artifacts", pom.count("<include>") == 5 and "<include>org.springframework:spring-core</include>" in pom)
    ok("pom configures Boot properties transformer", "PropertiesMergingResourceTransformer" in pom and "<version>3.3.5</version>" in pom)
    ok("pom sets Multi-Release", "<Multi-Release>true</Multi-Release>" in pom)
    ok("pom has distributionManagement", "<snapshotRepository>" in pom and "maven-releases" in pom)
    ok("legal files relocated", (proj / "src/main/resources/META-INF/third-party/commons-io-2.16.1/LICENSE.txt").is_file()
       and (proj / "src/main/resources/META-INF/third-party/BUNDLE-CONTENTS.txt").is_file())
    ok("manifest lists 5 components", len(man["components"]) == 5 and man["contents_attr"].startswith("com.example.codec:codec-a:1.0,"))

    # ---- stage 6: bundle verification on a simulated Shade output ------------------------
    print("[verify bundle]")
    selected_jars = [Path(c["path_in_app"]).name for c in man["components"]]
    jar_paths = [work / "fixture" / "libs" / n for n in selected_jars]
    source_attr = "jar=app.jar@sha256:" + src["sha256"]
    target = proj / "target"
    good = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "thirdparty-bundle-1.0.0.jar")
    fx.write_reduced_pom(target / "dependency-reduced-pom.xml", bundle, [("org.springframework", "spring-jcl", "6.1.14")])
    run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--out", out / "bundle-verification.json")
    ver = read_json(out / "bundle-verification.json")
    ok("simulated bundle passes every check", ver["ok"] and all(c["status"] == "pass" for c in ver["checks"] if c["check"] != "sources-jar-present"))
    ok("promoted dependency reported", ver["promoted_dependencies"][0]["artifactId"] == "spring-jcl")

    bad = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "bad-missing.jar", drop=["org/apache/commons/io/FileUtils.class"])
    run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--bundle", bad, "--out", out / "bv-missing.json", expect=1)
    ok("missing class detected", "FileUtils" in next(c["detail"] for c in read_json(out / "bv-missing.json")["checks"] if c["check"] == "classes-and-resources-identical"))

    bad = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "bad-corrupt.jar", corrupt=["org/springframework/util/StringUtils.class"])
    run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--bundle", bad, "--out", out / "bv-corrupt.json", expect=1)
    ok("changed class bytes detected", "StringUtils" in next(c["detail"] for c in read_json(out / "bv-corrupt.json")["checks"] if c["check"] == "classes-and-resources-identical"))

    bad = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, False, target / "bad-mr.jar")
    run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--bundle", bad, "--out", out / "bv-mr.json", expect=1)
    ok("missing Multi-Release detected", any(c["check"] == "manifest-multi-release" and c["status"] == "fail" for c in read_json(out / "bv-mr.json")["checks"]))

    fx.write_reduced_pom(target / "bad-reduced-pom.xml", bundle, [("commons-io", "commons-io", "2.16.1")])
    run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--reduced-pom", target / "bad-reduced-pom.xml", "--out", out / "bv-pom.json", expect=1)
    ok("embedded coordinate still in reduced pom detected", any(c["check"] == "reduced-pom-drops-embedded" and c["status"] == "fail" for c in read_json(out / "bv-pom.json")["checks"]))

    # ---- stage 7: consumer patch ----------------------------------------------------------
    print("[consumer patch]")
    tree = work / "dependency-tree.txt"
    tree.write_text(fx.SAMPLE_TREE, encoding="utf-8")
    run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest.json", "--tree", tree, "--out", out / "consumer-patch.md", "--out-json", out / "consumer-patch.json")
    patch = read_json(out / "consumer-patch.json")
    mods = {m["module"]: m for m in patch["modules"]}
    app_mod = mods["com.example:app"]
    ok("direct dependency to remove", list(app_mod["remove_direct"]) == ["commons-io:commons-io"])
    ok("exclusions per direct dependency", app_mod["exclusions"]["org.springframework.boot:spring-boot-starter-web"]["targets"] == ["org.springframework:spring-core"]
       and app_mod["exclusions"]["org.apache.commons:commons-text"]["targets"] == ["commons-io:commons-io"])
    ok("second module parsed", mods["com.example:app-batch"]["exclusions"]["com.example:app"]["targets"] == ["commons-io:commons-io"])
    md = (out / "consumer-patch.md").read_text(encoding="utf-8")
    ok("markdown has enforcer rule", "<exclude>commons-io:commons-io</exclude>" in md and "bannedDependencies" in md)
    run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest.json", "--out", out / "consumer-patch-notree.md", "--out-json", out / "consumer-patch-notree.json")
    ok("no-tree proposal explains how to get one", "dependency-tree.txt" in (out / "consumer-patch-notree.md").read_text(encoding="utf-8"))

    # ---- verify pipeline: application equivalence ---------------------------------------
    print("[verify app equivalence]")
    new_ok = fx.build_new_app(app, good, selected_jars, work / "new-ok.jar")
    run("verify_app_equivalence.py", "--old", app, "--new", new_ok, "--out", out / "equivalence.json")
    eq = read_json(out / "equivalence.json")
    ok("rebuilt app is equivalent", eq["ok"] and sorted(eq["replaced"])[0] == "com.example.codec:codec-a:1.0")
    ok("build-info / pom changes ignored", all(c["status"] == "pass" for c in eq["checks"]))

    new_bad = fx.build_new_app(app, good, selected_jars, work / "new-leftover.jar", keep_replaced=True)
    run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-leftover.json", expect=1)
    ok("original coordinates left on classpath detected", any(c["check"] == "original-coordinates-removed" and c["status"] == "fail" for c in read_json(out / "eq-leftover.json")["checks"]))

    new_bad = fx.build_new_app(app, good, selected_jars, work / "new-class.jar", change_class=True)
    run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-class.json", expect=1)
    ok("changed application class detected", any(c["check"] == "application-classes-unchanged" and c["status"] == "fail" for c in read_json(out / "eq-class.json")["checks"]))

    new_bad = fx.build_new_app(app, good, selected_jars, work / "new-droplib.jar", drop_lib="snakeyaml-2.2.jar")
    run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-droplib.json", expect=1)
    checks = {c["check"]: c for c in read_json(out / "eq-droplib.json")["checks"]}
    ok("lost unrelated dependency detected", checks["dependency-set-unchanged"]["status"] == "fail" and "snakeyaml" in checks["dependency-set-unchanged"]["detail"])

    bad_bundle = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "bad-for-app.jar", drop=["org/apache/commons/io/IOUtils.class"])
    new_bad = fx.build_new_app(app, bad_bundle, selected_jars, work / "new-badbundle.jar")
    run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-badbundle.json", expect=1)
    ok("missing class on new classpath detected", "IOUtils" in next(c["detail"] for c in read_json(out / "eq-badbundle.json")["checks"] if c["check"] == "classpath-entries-identical"))

    # exploded layout of the new build must compare the same way
    exploded = work / "new-exploded"
    import zipfile
    with zipfile.ZipFile(new_ok) as zf:
        zf.extractall(exploded)
    run("verify_app_equivalence.py", "--old", app, "--new", exploded, "--out", out / "eq-exploded.json")
    ok("exploded layout supported", read_json(out / "eq-exploded.json")["ok"])

    # ---- summary + settings ---------------------------------------------------------------
    print("[summary]")
    run("write_summary.py", "--out-dir", out)
    ok("summary written", (out / "SUMMARY.md").is_file() and "Bundle verification: passed" in (out / "SUMMARY.md").read_text(encoding="utf-8"))
    run("gen_maven_settings.py", "--mirror-url", "https://nexus.example.com/repository/maven-public/", "--out", work / "settings.xml")
    ok("settings with mirror", "<mirrorOf>*</mirrorOf>" in (work / "settings.xml").read_text(encoding="utf-8"))
    run("gen_maven_settings.py", "--out", work / "settings-plain.xml")
    ok("settings without mirror", "<mirrors>" not in (work / "settings-plain.xml").read_text(encoding="utf-8"))

    print("\nSELFTEST PASSED: %d assertions (workdir %s)" % (passed, work))


if __name__ == "__main__":
    main()
