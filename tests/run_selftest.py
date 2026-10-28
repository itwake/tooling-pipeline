#!/usr/bin/env python3
"""End-to-end self-test of the Python stages on synthetic fixtures (no Maven needed).

    python tests/run_selftest.py [workdir]

A fake Nexus (Maven layout, .sha1 files, Spring Boot BOM, search API, basic auth) runs on a
local port. maven-shade-plugin itself is simulated (tests/make_fixture.py::simulate_shade)
according to the rules the generated project configures; the real Shade run is covered on Jenkins.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import make_fixture as fx  # noqa: E402
from bundlelib import (  # noqa: E402
    AppArtifact,
    BUNDLE_BASE_ATTR,
    BUNDLE_CONTENTS_ATTR,
    BUNDLE_REWORKED_FROM_ATTR,
    gav_str,
    parse_properties,
    properties_values,
    read_json,
    read_manifest,
    significant_lines,
    write_json,
)
from rework_bundle import derive_version  # noqa: E402

PY = sys.executable
passed = 0

SNAPSHOT_METADATA = """<?xml version="1.0" encoding="UTF-8"?>
<metadata modelVersion="1.1.0"><groupId>com.example</groupId><artifactId>app</artifactId><version>1.1.0-SNAPSHOT</version>
<versioning><snapshot><timestamp>20241001.101010</timestamp><buildNumber>3</buildNumber></snapshot><lastUpdated>20241001101010</lastUpdated>
<snapshotVersions>
<snapshotVersion><extension>jar</extension><value>1.1.0-20241001.101010-3</value><updated>20241001101010</updated></snapshotVersion>
<snapshotVersion><classifier>exec</classifier><extension>jar</extension><value>1.1.0-20241001.101010-4</value><updated>20241001101010</updated></snapshotVersion>
<snapshotVersion><extension>pom</extension><value>1.1.0-20241001.101010-3</value><updated>20241001101010</updated></snapshotVersion>
</snapshotVersions></versioning></metadata>
"""


def run(script, *args, expect=0, cwd=None, env=None):
    cmd = [PY, "-I", str(SCRIPTS / script), *map(str, args)]
    p = subprocess.run(cmd, cwd=str(cwd or ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
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


def start_fake_nexus(repo_root: Path, search_index: dict, user="reader", password="secret"):
    token = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()

    class NexusLike(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(repo_root), **k)

        def do_GET(self):
            if self.headers.get("Authorization") != token:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="nexus"')
                self.end_headers()
                return
            url = urlparse(self.path)
            if url.path == "/service/rest/v1/search/assets":
                sha1 = parse_qs(url.query).get("sha1", [""])[0]
                body = json.dumps({"items": [{"maven2": search_index[sha1]}] if sha1 in search_index else []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), NexusLike)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/repository/maven-public/" % server.server_address[1]


def main() -> None:
    work = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mvn-rebundle-selftest-"))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    out = work / "out"
    print("workdir: %s" % work)

    libs = fx.build_libs(work / "fixture" / "libs")
    app = fx.build_app(work / "fixture" / "app.jar", libs)
    search_index = fx.build_fake_nexus(work / "fakenexus", libs, app, SNAPSHOT_METADATA)
    server, repo_url = start_fake_nexus(work / "fakenexus", search_index)
    with_creds = dict(os.environ, SOURCE_USERNAME="reader", SOURCE_PASSWORD="secret")
    without_creds = {k: v for k, v in os.environ.items() if not k.startswith("SOURCE_")}
    bundle = {"groupId": "com.company.platform", "artifactId": "thirdparty-bundle", "version": "1.0.0"}

    try:
        # ---- stage 1 + 2 -----------------------------------------------------------------
        print("[extract + inspect]")
        run("extract_source.py", "--ref", app, "--out", work / "app", "--meta", out / "source.json")
        src = read_json(out / "source.json")
        ok("source metadata", src["local_path"] == str(app) and len(src["sha256"]) == 64 and src["type"] == "file")

        run("inspect_app.py", "--source", out / "source.json", "--no-lookup", "--out", out / "inventory-offline.json")
        inv = read_json(out / "inventory-offline.json")
        by_name = {d["name"]: d for d in inv["dependencies"]}
        ok("layout and count", inv["app"]["layout"] == "boot-jar" and inv["app"]["dependency_count"] == len(libs))
        ok("class-loading order taken from classpath.idx", inv["app"]["classpath_source"] == "BOOT-INF/classpath.idx"
           and [d["name"] for d in inv["dependencies"]][:2] == ["bcprov-jdk15on-1.60.jar", "codec-a-1.0.jar"]
           and by_name["codec-a-1.0.jar"]["classpath_index"] == 1)
        ok("spring boot version detected", inv["app"]["spring_boot_version"] == "3.3.5")
        ok("gav from pom.properties", by_name["commons-io-2.16.1.jar"]["gav"] == {"groupId": "commons-io", "artifactId": "commons-io", "version": "2.16.1"})
        ok("sha1 recorded", by_name["commons-io-2.16.1.jar"]["sha1"] == fx.sha1_of(libs["commons-io"]))
        ok("declared dependencies from the embedded pom (runtime scopes only)",
           [(d["groupId"], d["artifactId"]) for d in by_name["codec-a-1.0.jar"]["declared_dependencies"]] == [("commons-io", "commons-io")]
           and [(d["artifactId"]) for d in by_name["spring-boot-3.3.5.jar"]["declared_dependencies"]] == ["spring-core"])
        unknown = sorted(d["name"] for d in inv["dependencies"] if not d["gav"])
        ok("jars declared by another jar are identified offline, the rest get synthetic coordinates",
           unknown == ["bcprov-jdk15on-1.60.jar", "vendor-sdk-2.0.0.jar"]
           and by_name["tomcat-embed-core-10.1.59.jar"]["gav"] == {"groupId": "org.apache.tomcat.embed", "artifactId": "tomcat-embed-core", "version": "10.1.59"}
           and by_name["tomcat-embed-core-10.1.59.jar"]["gav_source"] == "declared-by:spring-boot-starter-tomcat-3.3.5.jar"
           and by_name["vendor-sdk-2.0.0.jar"]["synthetic"]["groupId"] == "rebundle.unidentified"
           and by_name["vendor-sdk-2.0.0.jar"]["synthetic_hint"] == {"artifactId": "vendor-sdk", "version": "2.0.0"})
        f = by_name["commons-codec-1.17.1.jar"]["flags"]
        ok("flags: multi-release/module-info/spi", f["multi_release"] and f["module_info"] and f["services"] == ["org.apache.commons.codec.Codec"])
        ok("flags: signed", by_name["bcprov-jdk15on-1.60.jar"]["flags"]["signed"])

        text = run("inspect_app.py", "--source", out / "source.json", "--repo-url", repo_url, "--out", out / "inventory.json", env=with_creds)
        inv = read_json(out / "inventory.json")
        by_name = {d["name"]: d for d in inv["dependencies"]}
        ok("Boot BOM and its import loaded from the repository", "Loaded 2 BOM(s)" in text)
        ok("tomcat identified through the Boot BOM and confirmed by sha1",
           by_name["tomcat-embed-core-10.1.59.jar"]["gav"] == {"groupId": "org.apache.tomcat.embed", "artifactId": "tomcat-embed-core", "version": "10.1.59"}
           and by_name["tomcat-embed-core-10.1.59.jar"]["gav_source"] == "spring-boot-bom+sha1"
           and by_name["tomcat-embed-el-10.1.59.jar"]["gav_source"] == "spring-boot-bom+sha1")
        ok("bouncycastle identified through the manifest vendor id and confirmed by sha1",
           by_name["bcprov-jdk15on-1.60.jar"]["gav"] == {"groupId": "org.bouncycastle", "artifactId": "bcprov-jdk15on", "version": "1.60"}
           and by_name["bcprov-jdk15on-1.60.jar"]["gav_source"] == "manifest-vendor-id+sha1")
        ok("vendor jar identified through the Nexus search API",
           by_name["vendor-sdk-2.0.0.jar"]["gav"] == {"groupId": "com.vendor", "artifactId": "vendor-sdk", "version": "2.0.0", "classifier": None}
           and by_name["vendor-sdk-2.0.0.jar"]["gav_source"] == "nexus-search+sha1")
        ok("everything identified with the repository", all(d["gav"] for d in inv["dependencies"]))

        # ---- Spring Boot version: the application manifest first, then the spring-boot jar name --------
        print("[spring boot version]")
        glibs = fx.build_gradle_style_libs(work / "fixture-gradle" / "libs")
        gapp = fx.build_app(work / "fixture-gradle" / "app.jar", glibs)
        for key, g, v in (("spring-boot", "org.springframework.boot", "3.3.5"), ("spring-boot-autoconfigure", "org.springframework.boot", "3.3.5"),
                          ("spring-core", "org.springframework", "6.1.14")):
            fx.put_sha1(work / "fakenexus", g, key, v, glibs[key])
        run("inspect_app.py", "--app", gapp, "--no-lookup", "--out", out / "inv-gradle-offline.json")
        gi = read_json(out / "inv-gradle-offline.json")
        ok("Boot version read from the application manifest although no Spring jar has a descriptor",
           gi["app"]["spring_boot_version"] == "3.3.5" and gi["app"]["spring_boot_version_source"] == "application manifest Spring-Boot-Version"
           and all(not d["gav"] for d in gi["dependencies"]))
        text = run("inspect_app.py", "--app", gapp, "--repo-url", repo_url, "--out", out / "inv-gradle.json", env=with_creds)
        gby = {d["name"]: d for d in read_json(out / "inv-gradle.json")["dependencies"]}
        ok("with the version known up front the Boot BOM identifies the Gradle-built Spring jars",
           "Loaded 2 BOM(s)" in text and gby["spring-boot-3.3.5.jar"]["gav_source"] == "spring-boot-bom+sha1"
           and gby["spring-boot-3.3.5.jar"]["gav"] == {"groupId": "org.springframework.boot", "artifactId": "spring-boot", "version": "3.3.5"}
           and gby["spring-core-6.1.14.jar"]["gav_source"] == "spring-boot-bom+sha1" and gby["spring-boot-autoconfigure-3.3.5.jar"]["gav_source"] == "spring-boot-bom+sha1")
        run("resolve_selection.py", "--inventory", out / "inv-gradle-offline.json", "--select", "*", "--out", out / "sel-gradle.json")
        gsel = read_json(out / "sel-gradle.json")
        ok("spring.factories of several jars merge with the manifest-detected version, offline and unidentified",
           gsel["ok"] and gsel["spring_boot_version"] == "3.3.5" and gsel["transformers"]["props_merge"] == ["META-INF/spring.factories"]
           and any(f["code"] == "props-merge" for f in gsel["findings"]) and not any(f["code"] == "props-merge-no-boot" for f in gsel["findings"]))
        gapp2 = fx.build_app(work / "fixture-gradle" / "app-plain.jar", glibs, boot_version_in_manifest=False)
        run("inspect_app.py", "--app", gapp2, "--no-lookup", "--out", out / "inv-gradle-plain.json")
        gi2 = read_json(out / "inv-gradle-plain.json")["app"]
        ok("without the manifest attribute the spring-boot jar name gives the version",
           gi2["spring_boot_version"] == "3.3.5" and gi2["spring_boot_version_source"] == "spring-boot jar name")
        run("inspect_app.py", "--app", gapp2, "--no-lookup", "--spring-boot-version", "3.2.0", "--out", out / "inv-gradle-override.json")
        ok("--spring-boot-version overrides the detection",
           read_json(out / "inv-gradle-override.json")["app"]["spring_boot_version"] == "3.2.0"
           and read_json(out / "inv-gradle-override.json")["app"]["spring_boot_version_source"] == "--spring-boot-version")

        # ---- stage 3: selection and expansion ------------------------------------------------
        print("[resolve selection]")

        def selection(select, name, expect=0, inventory=out / "inventory.json"):
            run("resolve_selection.py", "--inventory", inventory, "--select", select, "--out", out / ("sel-%s.json" % name), expect=expect)
            return read_json(out / ("sel-%s.json" % name))

        def names(sel):
            return sorted(d["name"] for d in sel["selected"])

        sel = selection("commons-io", "io")
        ok("single jar without relations", names(sel) == ["commons-io-2.16.1.jar"] and sel["ok"] and not sel["auto_added"])
        sel = selection("codec-a", "codec")
        ok("same group+version and declared dependencies are added", names(sel) == ["codec-a-1.0.jar", "codec-b-1.0.jar", "commons-io-2.16.1.jar"]
           and {a["name"]: a["reason"] for a in sel["auto_added"]}["commons-io-2.16.1.jar"] == "declared dependency of codec-a-1.0.jar")
        sel = selection("spring-core", "spring")
        ok("family members and their declared dependencies are added",
           names(sel) == ["spring-boot-3.3.5.jar", "spring-boot-starter-tomcat-3.3.5.jar", "spring-core-6.1.14.jar", "spring-jcl-6.1.14.jar",
                          "tomcat-embed-core-10.1.59.jar", "tomcat-embed-el-10.1.59.jar"]
           and any(f["code"] == "version-coupling" for f in sel["findings"]))
        sel = selection("tomcat-embed-core", "tomcat")
        ok("tomcat family completed", names(sel) == ["tomcat-embed-core-10.1.59.jar", "tomcat-embed-el-10.1.59.jar"])
        sel = selection("bcprov-jdk15on", "bc")
        ok("signed jar is allowed with a warning", sel["ok"] and any(f["code"] == "signed-jar" and "Oracle JDK" in f["message"] for f in sel["findings"]))
        sel = selection("codec-a, -commons-io", "excl")
        ok("exclusion wins over expansion", names(sel) == ["codec-a-1.0.jar", "codec-b-1.0.jar"] and any(f["code"] == "excluded-related" for f in sel["findings"]))
        sel = selection("*", "all")
        ok("star selects every jar; duplicates resolve in class-loading order",
           sel["ok"] and len(sel["selected"]) == len(libs)
           and {"path": "com/dup/Same.class", "winner": "dup-x-1.0.jar", "shadowed": ["dup-y-1.0.jar"]} in sel["overlaps"]["conflicts"]
           and any(f["code"] == "overlap-resolved" and "dup-x-1.0.jar shadows dup-y-1.0.jar" in f["message"] for f in sel["findings"])
           and [d["name"] for d in sel["selected"]] == [d["name"] for d in inv["dependencies"]])
        ok("duplicated resources are graded: IDE metadata is harmless, the rest is listed for review, merged files are not listed",
           any(f["code"] == "resource-copies-harmless" and "META-INF/spring-configuration-metadata.json" in f["message"] for f in sel["findings"])
           and any(f["code"] == "resource-copies-collapse" and "reference.conf" in f["message"] and "Log4j2Plugins" not in f["message"]
                   and "spring-autoconfigure-metadata" not in f["message"] and "license.txt" not in f["message"] for f in sel["findings"])
           and any(f["code"] == "log4j-plugins-merge" for f in sel["findings"]) and sel["transformers"]["log4j_plugins"] is True)
        sel = selection("log4j-core", "log4j")
        ok("Log4j family completed, its plugin caches flagged for merging",
           names(sel) == ["log4j-core-2.23.1.jar", "log4j-layout-template-json-2.23.1.jar"] and sel["transformers"]["log4j_plugins"] is True)
        sel = selection("* -dup-y", "all-excl")
        ok("exclusion removes one side of a duplicate", sel["ok"] and len(sel["selected"]) == len(libs) - 1 and not sel["overlaps"]["conflicts"])
        run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "commons-io", "--policy-mode", "strict", "--out", out / "sel-compat.json")
        ok("old --policy-mode flag is accepted and ignored", read_json(out / "sel-compat.json")["ok"])
        sel = selection("nope-lib", "missing", expect=1)
        ok("unknown token fails", any(f["code"] == "not-found" for f in sel["findings"]))
        sel = selection("tomcat-embed-core", "offline-tomcat", inventory=out / "inventory-offline.json")
        ok("tomcat selectable offline through the starter's declaration", sel["ok"] and names(sel) == ["tomcat-embed-core-10.1.59.jar", "tomcat-embed-el-10.1.59.jar"])
        sel = selection("vendor-sdk", "offline-synthetic", inventory=out / "inventory-offline.json")
        ok("unidentified jar is embedded under synthetic coordinates with a warning",
           sel["ok"] and sel["selected"][0]["gav"]["groupId"] == "rebundle.unidentified" and sel["selected"][0]["gav_source"] == "synthetic"
           and any(f["code"] == "unidentified-synthetic" for f in sel["findings"]))
        sel = selection("org.bouncycastle:bcprov-jdk15on:1.60", "offline-explicit", inventory=out / "inventory-offline.json")
        ok("explicit coordinates replace the synthetic ones", sel["ok"] and sel["selected"][0]["gav"]["groupId"] == "org.bouncycastle")
        sel = selection("*", "offline-all", inventory=out / "inventory-offline.json")
        ok("star works offline, unidentified jars included",
           sel["ok"] and len(sel["selected"]) == len(libs)
           and sorted(d["name"] for d in sel["selected"] if d["gav_source"] == "synthetic") == ["bcprov-jdk15on-1.60.jar", "vendor-sdk-2.0.0.jar"])
        sel = selection("codec-a\ncodec-b\ncommons-codec", "merge")
        ok("merge transformers detected", sel["transformers"]["props_merge"] == ["META-INF/spring.factories"]
           and sel["transformers"]["append"] == ["META-INF/spring-autoconfigure-metadata.properties", "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"]
           and sel["transformers"]["multi_release"] is True and sel["transformers"]["log4j_plugins"] is False)

        # ---- stage 4: project generation ---------------------------------------------------
        print("[generate bundle project]")
        selection_file = out / "sel-main.json"
        run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "commons-io\ncommons-codec\ncodec-a\nspring-core", "--out", selection_file)
        main_sel = read_json(selection_file)
        ok("main selection expanded to 10 jars", len(main_sel["selected"]) == 10)
        proj = work / "bundle"
        run("gen_bundle_project.py", "--selection", selection_file, "--source", out / "source.json", "--group-id", bundle["groupId"],
            "--artifact-id", bundle["artifactId"], "--version", bundle["version"], "--out", proj,
            "--release-url", "https://nexus.example.com/repository/maven-releases/", "--snapshot-url", "https://nexus.example.com/repository/maven-snapshots/",
            "--manifest-out", out / "bundle-manifest.json")
        pom = (proj / "pom.xml").read_text(encoding="utf-8")
        man = read_json(out / "bundle-manifest.json")
        ok("pom pins exact versions", "<artifactId>commons-io</artifactId>\n      <version>2.16.1</version>" in pom and "<version>6.1.14</version>" in pom)
        ok("pom includes only the selected artifacts", pom.count("<include>") == 10 and "<include>org.springframework.boot:spring-boot</include>" in pom)
        ok("pom configures Boot properties transformer", "PropertiesMergingResourceTransformer" in pom and "<version>3.3.5</version>" in pom)
        ok("pom sets Multi-Release and the Boot manifest attribute", "<Multi-Release>true</Multi-Release>" in pom and "<X-Dependency-Bundle-Spring-Boot>3.3.5</X-Dependency-Bundle-Spring-Boot>" in pom)
        ok("pom has distributionManagement", "<snapshotRepository>" in pom and "maven-releases" in pom)
        ok("legal files relocated, lowercase ones included", (proj / "src/main/resources/META-INF/third-party/commons-io-2.16.1/LICENSE.txt").is_file()
           and (proj / "src/main/resources/META-INF/third-party/codec-b-1.0/license.txt").is_file()
           and (proj / "src/main/resources/META-INF/third-party/BUNDLE-CONTENTS.txt").is_file()
           and "<exclude>META-INF/license.*</exclude>" in pom and "Log4j2PluginCacheFileTransformer" not in pom)
        ok("manifest lists 10 components in class-loading order", len(man["components"]) == 10 and man["contents_attr"].startswith("com.example.codec:codec-a:1.0,")
           and not man["unidentified"] and len(man["install"]) == 10 and all(Path(i["file"]).is_file() for i in man["install"]))
        text = run("install_components.py", "--manifest", out / "bundle-manifest.json", "--repo-url", repo_url, "--local-repo", work / "m2", "--dry-run", env=with_creds)
        ok("every component is installed from the application's bytes, with the real POM when the repository has it",
           text.count("install-file") == 10 and "-Dmaven.repo.local=" in text
           and any("-Dfile=" in l and "commons-io-2.16.1.jar" in l and "-DpomFile=" in l for l in text.splitlines())
           and any("codec-a-1.0.jar" in l and "-DgeneratePom=true" in l for l in text.splitlines()))

        # ---- stage 6: bundle verification on a simulated Shade output ----------------------
        print("[verify bundle]")
        selected_jars = [Path(c["path_in_app"]).name for c in man["components"]]
        jar_paths = [work / "fixture" / "libs" / n for n in selected_jars]
        source_attr = "file=app.jar@sha256:" + src["sha256"]
        target = proj / "target"
        good = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "thirdparty-bundle-1.0.0.jar", boot_version="3.3.5")
        # Shade promotes the components' transitive dependencies from their own POMs: snakeyaml at a version
        # the application does not have, and tomcat-annotations-api, which the application never had
        fx.write_reduced_pom(target / "dependency-reduced-pom.xml", bundle, [("org.yaml", "snakeyaml", "2.1"), ("org.apache.tomcat", "tomcat-annotations-api", "10.1.59")])
        run("finalize_bundle_pom.py", "--manifest", out / "bundle-manifest.json", "--report", out / "bundle-pom.json")
        fin = read_json(out / "bundle-pom.json")
        final_pom = (target / "thirdparty-bundle-1.0.0.pom").read_text(encoding="utf-8")
        ok("published POM keeps only promoted dependencies the application had, at the application's version",
           [d["artifactId"] for d in fin["kept"]] == ["snakeyaml"] and [d["artifactId"] for d in fin["pruned"]] == ["tomcat-annotations-api"]
           and fin["aligned"][0]["version"] == "2.2" and fin["aligned"][0]["version_in_reduced_pom"] == "2.1"
           and "tomcat-annotations-api" not in final_pom and "<version>2.2</version>" in final_pom and ET.fromstring(final_pom.encode("utf-8")) is not None)
        run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--out", out / "bundle-verification.json")
        ver = read_json(out / "bundle-verification.json")
        ok("simulated bundle passes every check", ver["ok"] and all(c["status"] == "pass" for c in ver["checks"] if c["check"] != "sources-jar-present"))
        ok("promoted dependency reported from the published POM",
           ver["promoted_dependencies"][0]["artifactId"] == "snakeyaml" and ver["pom_finalized"]
           and any(c["check"] == "pom-promotes-only-application-dependencies" and c["status"] == "pass" for c in ver["checks"]))
        run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--reduced-pom", target / "dependency-reduced-pom.xml", "--out", out / "bv-unfinalized.json")
        ok("an unfinalized POM is a warning naming the foreign dependency",
           any(c["check"] == "pom-promotes-only-application-dependencies" and c["status"] == "warn" and "tomcat-annotations-api" in c["detail"]
               for c in read_json(out / "bv-unfinalized.json")["checks"]))

        bad = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "bad-missing.jar", drop=["org/apache/commons/io/FileUtils.class"], boot_version="3.3.5")
        run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--bundle", bad, "--out", out / "bv-missing.json", expect=1)
        ok("missing class detected", "FileUtils" in next(c["detail"] for c in read_json(out / "bv-missing.json")["checks"] if c["check"] == "classes-and-resources-identical"))
        bad = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "bad-corrupt.jar", corrupt=["org/springframework/util/StringUtils.class"], boot_version="3.3.5")
        run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--bundle", bad, "--out", out / "bv-corrupt.json", expect=1)
        ok("changed class bytes detected", "StringUtils" in next(c["detail"] for c in read_json(out / "bv-corrupt.json")["checks"] if c["check"] == "classes-and-resources-identical"))
        bad = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, False, target / "bad-mr.jar", boot_version="3.3.5")
        run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--bundle", bad, "--out", out / "bv-mr.json", expect=1)
        ok("missing Multi-Release detected", any(c["check"] == "manifest-multi-release" and c["status"] == "fail" for c in read_json(out / "bv-mr.json")["checks"]))
        fx.write_reduced_pom(target / "bad-reduced-pom.xml", bundle, [("commons-io", "commons-io", "2.16.1")])
        run("verify_bundle.py", "--manifest", out / "bundle-manifest.json", "--reduced-pom", target / "bad-reduced-pom.xml", "--out", out / "bv-pom.json", expect=1)
        ok("embedded coordinate still in reduced pom detected", any(c["check"] == "reduced-pom-drops-embedded" and c["status"] == "fail" for c in read_json(out / "bv-pom.json")["checks"]))

        # ---- Log4j plugin caches of several jars are merged by Log4j's transformer ---------------
        print("[log4j plugin cache]")
        l4_sel = out / "sel-log4j.json"
        run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "log4j-core", "--out", l4_sel)
        l4_proj = work / "bundle-log4j"
        run("gen_bundle_project.py", "--selection", l4_sel, "--source", out / "source.json", "--group-id", bundle["groupId"],
            "--artifact-id", "log4j-bundle", "--version", "1.0.0", "--out", l4_proj, "--manifest-out", out / "bundle-manifest-log4j.json")
        l4_pom = (l4_proj / "pom.xml").read_text(encoding="utf-8")
        l4_man = read_json(out / "bundle-manifest-log4j.json")
        ok("pom merges the Log4j plugin caches with Log4j's transformer",
           "Log4j2PluginCacheFileTransformer" in l4_pom
           and "<artifactId>log4j-transform-maven-shade-plugin-extensions</artifactId>\n            <version>0.2.0</version>" in l4_pom
           and "PropertiesMergingResourceTransformer" not in l4_pom and l4_man["log4j_transform_version"] == "0.2.0"
           and ET.fromstring(l4_pom.encode("utf-8")) is not None)
        l4_jars = [work / "fixture" / "libs" / Path(c["path_in_app"]).name for c in l4_man["components"]]
        l4_bundle = {"groupId": bundle["groupId"], "artifactId": "log4j-bundle", "version": "1.0.0"}
        l4_good = fx.simulate_shade(l4_proj, l4_jars, l4_bundle, l4_man["contents_attr"], source_attr, False, l4_proj / "target" / "log4j-bundle-1.0.0.jar", boot_version="3.3.5")
        fx.write_reduced_pom(l4_proj / "target" / "dependency-reduced-pom.xml", l4_bundle, [])
        run("verify_bundle.py", "--manifest", out / "bundle-manifest-log4j.json", "--out", out / "bv-log4j.json")
        l4_checks = {c["check"]: c for c in read_json(out / "bv-log4j.json")["checks"]}
        with zipfile.ZipFile(l4_good) as zf:
            merged = fx.parse_log4j_plugin_cache(zf.read(fx.LOG4J_PLUGIN_CACHE))
        ok("merged plugin cache keeps every plugin of every jar",
           l4_checks["log4j-plugin-cache-merged"]["status"] == "pass" and "5 plugin(s) in 3 categories from 2 jar(s)" in l4_checks["log4j-plugin-cache-merged"]["detail"]
           and l4_checks["no-unexpected-entries"]["status"] == "pass" and read_json(out / "bv-log4j.json")["ok"]
           and set(merged["Core"]) == {"console", "patternlayout", "jsontemplatelayout"} and set(merged) == {"Core", "Lookup", "JsonTemplateResolverFactory"})
        l4_bad = fx.simulate_shade(l4_proj, l4_jars, l4_bundle, l4_man["contents_attr"], source_attr, False, l4_proj / "target" / "bad-log4j.jar",
                                   drop=[fx.LOG4J_PLUGIN_CACHE], boot_version="3.3.5")
        run("verify_bundle.py", "--manifest", out / "bundle-manifest-log4j.json", "--bundle", l4_bad, "--out", out / "bv-log4j-bad.json", expect=1)
        ok("a lost plugin cache fails the verification",
           any(c["check"] == "log4j-plugin-cache-merged" and c["status"] == "fail" and "missing" in c["detail"] for c in read_json(out / "bv-log4j-bad.json")["checks"]))

        # ---- stage 8: publish (dry run) --------------------------------------------------
        print("[publish]")
        (target / "thirdparty-bundle-1.0.0-sources.jar").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        release, snapshot = "https://nexus.example.com/repository/maven-releases/", "https://nexus.example.com/repository/maven-snapshots/"
        run("publish_bundle.py", "--manifest", out / "bundle-manifest.json", "--verification", out / "bundle-verification.json",
            "--release-url", release, "--snapshot-url", snapshot, "--dry-run", "--out", out / "publish.json")
        pub = read_json(out / "publish.json")
        ok("publish uses deploy-file with the verified jar",
           any(a.endswith(":deploy-file") for a in pub["command"])
           and any(a.startswith("-Dfile=") and Path(a[len("-Dfile="):]).resolve() == good.resolve() for a in pub["command"])
           and pub["url"] == release and pub["sources"] and pub["sha256"] == ver["sha256"])
        ok("publish dry run uploads nothing", pub["dry_run"] and not pub["published"])
        write_json(out / "bv-failed.json", dict(ver, ok=False))
        run("publish_bundle.py", "--manifest", out / "bundle-manifest.json", "--verification", out / "bv-failed.json",
            "--release-url", release, "--dry-run", "--out", out / "publish-failed.json", expect=2)
        ok("publish refuses a failed verification", not (out / "publish-failed.json").exists())
        write_json(out / "bv-sha.json", dict(ver, sha256="0" * 64))
        run("publish_bundle.py", "--manifest", out / "bundle-manifest.json", "--verification", out / "bv-sha.json",
            "--release-url", release, "--dry-run", "--out", out / "publish-sha.json", expect=2)
        ok("publish refuses changed bytes", not (out / "publish-sha.json").exists())
        snap_man = dict(man, bundle=dict(man["bundle"], version="1.0.0-SNAPSHOT"), coordinate="com.company.platform:thirdparty-bundle:1.0.0-SNAPSHOT")
        write_json(out / "manifest-snapshot.json", snap_man)
        run("publish_bundle.py", "--manifest", out / "manifest-snapshot.json", "--verification", out / "bundle-verification.json",
            "--release-url", release, "--snapshot-url", snapshot, "--dry-run", "--out", out / "publish-snapshot.json")
        ok("snapshot versions go to the snapshot repository", read_json(out / "publish-snapshot.json")["url"] == snapshot)
        run("publish_bundle.py", "--manifest", out / "manifest-snapshot.json", "--verification", out / "bundle-verification.json",
            "--release-url", release, "--dry-run", "--out", out / "publish-nosnap.json", expect=2)
        ok("missing snapshot repository is refused", not (out / "publish-nosnap.json").exists())

        # ---- rework: add jars to a published bundle and republish it under a new version ---------
        print("[rework bundle]")
        nexus_root = work / "fakenexus"
        fx.put_artifact(nexus_root, bundle["groupId"], bundle["artifactId"], bundle["version"], good, (target / "thirdparty-bundle-1.0.0.pom").read_bytes())
        for key, g, a, v in (("log4j-core", "org.apache.logging.log4j", "log4j-core", "2.23.1"),
                             ("log4j-layout-template-json", "org.apache.logging.log4j", "log4j-layout-template-json", "2.23.1"),
                             ("commons-io", "commons-io", "commons-io", "2.16.1"), ("codec-a", "com.example.codec", "codec-a", "1.0")):
            fx.put_artifact(nexus_root, g, a, v, libs[key])
        run("fetch_bundle.py", "--bundle", "com.company.platform:thirdparty-bundle:1.0.0", "--repo-url", repo_url,
            "--add", "org.apache.logging.log4j:log4j-core:2.23.1", "--add", "org.apache.logging.log4j:log4j-layout-template-json:2.23.1",
            "--out", work / "rework-in", "--added-out", work / "rework-added", "--meta", out / "rework-bundle.json", env=with_creds)
        rmeta = read_json(out / "rework-bundle.json")
        ok("published bundle fetched with its POM, its manifest and contents list read",
           rmeta["sha256"] == ver["sha256"] and Path(rmeta["pom"]).is_file() and rmeta["sources"] is None
           and len(rmeta["contents"]) == 10 and rmeta["manifest"]["spring_boot_version"] == "3.3.5" and rmeta["manifest"]["multi_release"]
           and [d["artifactId"] for d in rmeta["pom_dependencies"]] == ["snakeyaml"]
           and [a["coordinate"] for a in rmeta["added"]] == ["org.apache.logging.log4j:log4j-core:2.23.1", "org.apache.logging.log4j:log4j-layout-template-json:2.23.1"]
           and all(Path(a["jar"]).is_file() and Path(a["pom"]).is_file() for a in rmeta["added"])
           and rmeta["contents_file"]["commons-io:commons-io:2.16.1"]["sha256"] == next(c["sha256"] for c in man["components"] if c["coordinate"] == "commons-io:commons-io:2.16.1"))
        run("fetch_bundle.py", "--bundle", "commons-io:commons-io:2.16.1", "--repo-url", repo_url, "--out", work / "rework-nb", "--meta", out / "rework-notbundle.json", env=with_creds, expect=2)
        ok("a jar that is not a bundle is refused", not (out / "rework-notbundle.json").exists())
        # the same bundle without coordinates to add, for the runs that only upload jars
        run("fetch_bundle.py", "--bundle", "com.company.platform:thirdparty-bundle:1.0.0", "--repo-url", repo_url, "--out", work / "rework-in-plain", "--meta", out / "rework-bundle-plain.json", env=with_creds)

        # a hot fix without coordinates, two Log4j jars by coordinates, a vendor jar identified through the repository
        hotfix = fx.make_jar(work / "upload" / "hotfix.jar", {"org/apache/commons/io/FileUtils.class": b"patched FileUtils",
                                                               "com/example/hotfix/Patch.class": fx.fake_class("Patch"),
                                                               "META-INF/services/com.example.Codec": b"com.example.hotfix.HotCodec\n"},
                             {"Implementation-Title": "hotfix"})
        rproj = work / "rework"
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle.json", "--add-jar", hotfix, "--add-jar", libs["vendor-sdk"], "--repo-url", repo_url,
            "--release-url", release, "--snapshot-url", snapshot, "--out", rproj, "--manifest-out", out / "rework-manifest.json", "--plan-out", out / "rework-plan.json", env=with_creds)
        rman = read_json(out / "rework-manifest.json")
        rplan = read_json(out / "rework-plan.json")
        by_name = {a["name"]: a for a in rplan["added"]}
        ok("version derived from the published one",
           rman["bundle"]["version"] == "1.0.0.1" and rplan["version"] == {"old": "1.0.0", "new": "1.0.0.1", "source": "derived"}
           and rman["coordinate"] == "com.company.platform:thirdparty-bundle:1.0.0.1" and rman["rework"]["base"] == "com.company.platform:thirdparty-bundle:1.0.0")
        ok("added jars identified: by coordinates, by the repository checksum, synthetic for the hot fix",
           by_name["log4j-core-2.23.1.jar"]["action"] == "add" and by_name["log4j-core-2.23.1.jar"]["gav_source"] == "coordinates"
           and by_name["vendor-sdk-2.0.0.jar"]["coordinate"] == "com.vendor:vendor-sdk:2.0.0" and by_name["vendor-sdk-2.0.0.jar"]["gav_source"] == "nexus-search+sha1"
           and by_name["hotfix.jar"]["synthetic"] and by_name["hotfix.jar"]["coordinate"].startswith("rebundle.unidentified:hotfix:0-u")
           and any("synthetic coordinates" in w for w in by_name["hotfix.jar"]["warnings"]))
        ok("hot fix replaces the bundle's copy of a class and adds to a merged resource",
           by_name["hotfix.jar"]["overlap"]["replaced"] == ["org/apache/commons/io/FileUtils.class"]
           and by_name["hotfix.jar"]["overlap"]["merged"] == ["META-INF/services/com.example.Codec"] and any("hot fix" in w for w in by_name["hotfix.jar"]["warnings"]))
        rpom = (rproj / "pom.xml").read_text(encoding="utf-8")
        deps_part = rpom[rpom.find("<dependencies>"):rpom.find("</dependencies>")]
        ok("project merges the added jars first and the bundle last, with the Log4j transformer and the origin in the manifest",
           deps_part.find("log4j-core") < deps_part.find("<artifactId>hotfix</artifactId>") < deps_part.find("vendor-sdk")
           < deps_part.find("<artifactId>thirdparty-bundle</artifactId>\n      <version>1.0.0</version>")
           and "Log4j2PluginCacheFileTransformer" in rpom and "PropertiesMergingResourceTransformer" not in rpom and rpom.count("<include>") == 5
           and "<%s>com.company.platform:thirdparty-bundle:1.0.0</%s>" % (BUNDLE_BASE_ATTR, BUNDLE_BASE_ATTR) in rpom
           and "<%s>com.company.platform:thirdparty-bundle:1.0.0</%s>" % (BUNDLE_REWORKED_FROM_ATTR, BUNDLE_REWORKED_FROM_ATTR) in rpom
           and "<X-Dependency-Bundle-Source>%s</X-Dependency-Bundle-Source>" % source_attr in rpom and ET.fromstring(rpom.encode("utf-8")) is not None)
        ok("contents list the added coordinates then the carried ones; inputs and install items carry the fetched POMs",
           rman["contents_attr"].startswith("org.apache.logging.log4j:log4j-core:2.23.1,org.apache.logging.log4j:log4j-layout-template-json:2.23.1,rebundle.unidentified:hotfix:")
           and rman["contents_attr"].endswith(man["contents_attr"]) and len(rman["components"]) == 14
           and [i["role"] for i in rman["inputs"]] == ["added"] * 4 + ["bundle"]
           and rman["install"][-1]["pom_file"] == rmeta["pom"] and rman["install"][0]["pom_file"] == rmeta["added"][0]["pom"]
           and rman["application_coordinates"] == [{"groupId": "org.yaml", "artifactId": "snakeyaml", "version": "2.2"}])
        with zipfile.ZipFile(rman["inputs"][-1]["file"]) as zf:
            names = zf.namelist()
        ok("bundle input keeps everything but its own descriptors and contents list",
           "META-INF/maven/com.company.platform/thirdparty-bundle/pom.xml" not in names and "META-INF/third-party/BUNDLE-CONTENTS.txt" not in names
           and "org/apache/commons/io/FileUtils.class" in names and "META-INF/third-party/commons-io-2.16.1/LICENSE.txt" in names)
        text = run("install_components.py", "--manifest", out / "rework-manifest.json", "--repo-url", repo_url, "--local-repo", work / "m2", "--dry-run", env=with_creds)
        ok("inputs are installed with the fetched POMs, the hot fix with a generated one",
           text.count("install-file") == 5 and "-DpomFile=%s" % rmeta["pom"] in text and any("hotfix.jar" in l and "-DgeneratePom=true" in l for l in text.splitlines()))
        r_inputs = [Path(i["file"]) for i in rman["inputs"]]
        new_bundle = dict(bundle, version="1.0.0.1")
        r_extra = {BUNDLE_BASE_ATTR: "com.company.platform:thirdparty-bundle:1.0.0", BUNDLE_REWORKED_FROM_ATTR: "com.company.platform:thirdparty-bundle:1.0.0"}
        r_good = fx.simulate_shade(rproj, r_inputs, new_bundle, rman["contents_attr"], source_attr, True, rproj / "target" / "thirdparty-bundle-1.0.0.1.jar",
                                   boot_version="3.3.5", extra_manifest=r_extra)
        # Shade promotes snakeyaml at the version an added jar's POM wants, codec-a (embedded) and a new dependency of an added jar
        fx.write_reduced_pom(rproj / "target" / "dependency-reduced-pom.xml", new_bundle, [("org.yaml", "snakeyaml", "2.3"), ("com.example.codec", "codec-a", "1.0"), ("org.foo", "newdep", "1.0")])
        run("finalize_bundle_pom.py", "--manifest", out / "rework-manifest.json", "--report", out / "rework-pom.json")
        rfin = read_json(out / "rework-pom.json")
        ok("rework POM keeps the previous POM's dependencies at their versions, keeps new ones and prunes embedded ones",
           rfin["rework"] and [d["artifactId"] for d in rfin["new"]] == ["newdep"] and [d["artifactId"] for d in rfin["pruned_embedded"]] == ["codec-a"]
           and rfin["aligned"][0]["version"] == "2.2" and not rfin["pruned"]
           and "codec-a" not in (rproj / "target" / "thirdparty-bundle-1.0.0.1.pom").read_text(encoding="utf-8"))
        run("verify_bundle.py", "--manifest", out / "rework-manifest.json", "--out", out / "rework-verification.json")
        rver = read_json(out / "rework-verification.json")
        rchecks = {c["check"]: c for c in rver["checks"]}
        ok("reworked bundle passes the verification against its inputs; the new dependency is a warning",
           rver["ok"] and rchecks["classes-and-resources-identical"]["status"] == "pass" and rchecks["no-unexpected-entries"]["status"] == "pass"
           and rchecks["log4j-plugin-cache-merged"]["status"] == "pass" and rchecks["legal-files-relocated"]["status"] == "pass"
           and rchecks["pom-promotes-only-application-dependencies"]["status"] == "warn" and "newdep" in rchecks["pom-promotes-only-application-dependencies"]["detail"]
           and rver["rework"]["from"] == "com.company.platform:thirdparty-bundle:1.0.0")
        with zipfile.ZipFile(r_good) as zf:
            r_names = zf.namelist()
            r_services = set(significant_lines(zf.read("META-INF/services/com.example.Codec").decode()))
            r_attrs = read_manifest(zf)
            fileutils = zf.read("org/apache/commons/io/FileUtils.class")
        ok("hot fix bytes win, its service line is merged, the carried legal files stay, the manifest records the origin",
           fileutils == b"patched FileUtils" and r_services == {"com.example.hotfix.HotCodec", "com.example.codec.a.JsonCodec", "com.example.codec.b.XmlCodec"}
           and "META-INF/third-party/commons-io-2.16.1/LICENSE.txt" in r_names and "META-INF/third-party/vendor-sdk-2.0.0/LICENSE.txt" in r_names
           and r_attrs[BUNDLE_CONTENTS_ATTR] == rman["contents_attr"] and r_attrs[BUNDLE_REWORKED_FROM_ATTR] == "com.company.platform:thirdparty-bundle:1.0.0")
        bad = fx.simulate_shade(rproj, r_inputs, new_bundle, rman["contents_attr"], source_attr, True, rproj / "target" / "bad-rework.jar",
                                boot_version="3.3.5", extra_manifest=r_extra, drop=["com/example/hotfix/Patch.class"])
        run("verify_bundle.py", "--manifest", out / "rework-manifest.json", "--bundle", bad, "--out", out / "rework-bv-bad.json", expect=1)
        ok("a lost entry of an added jar fails the verification",
           "Patch" in next(c["detail"] for c in read_json(out / "rework-bv-bad.json")["checks"] if c["check"] == "classes-and-resources-identical"))
        run("publish_bundle.py", "--manifest", out / "rework-manifest.json", "--verification", out / "rework-verification.json",
            "--release-url", release, "--snapshot-url", snapshot, "--dry-run", "--out", out / "rework-publish.json")
        ok("reworked bundle publishes under the new version",
           read_json(out / "rework-publish.json")["coordinate"] == "com.company.platform:thirdparty-bundle:1.0.0.1" and read_json(out / "rework-publish.json")["url"] == release)

        # new versions of embedded components replace them; merged resources lose the lines the old versions contributed
        new_io = fx.dep_jar(work / "upload", "commons-io", "commons-io", "2.17.0",
                            ["org/apache/commons/io/IOUtils.class", "org/apache/commons/io/FileUtils.class", "org/apache/commons/io/file/PathUtils.class"],
                            extra={"org/apache/commons/io/messages.properties": b"hello=world\nbye=world\n"})
        new_codec = fx.dep_jar(work / "upload", "com.example.codec", "codec-a", "1.1", ["com/example/codec/a/JsonCodec2.class"],
                               extra={"META-INF/services/com.example.Codec": b"com.example.codec.a.JsonCodec2\n",
                                      "META-INF/spring.factories": b"com.example.Factory=com.example.codec.a.A2Factory\n",
                                      "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports": b"com.example.codec.a.A2AutoConfiguration\n"},
                               deps=[("commons-io", "commons-io", "compile")])
        uproj = work / "rework-upgrade"
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle-plain.json", "--add-jar", new_io, "--add-jar", new_codec, "--repo-url", repo_url,
            "--new-version", "1.1.0", "--out", uproj, "--manifest-out", out / "upgrade-manifest.json", "--plan-out", out / "upgrade-plan.json", env=with_creds)
        uman = read_json(out / "upgrade-manifest.json")
        uplan = read_json(out / "upgrade-plan.json")
        by_name = {a["name"]: a for a in uplan["added"]}
        io_strip = by_name["commons-io-2.17.0.jar"]["replaces"]["strip"]
        codec_strip = by_name["codec-a-1.1.jar"]["replaces"]["strip"]
        ok("new versions replace the embedded components; the old entries go by the repository jar",
           uman["bundle"]["version"] == "1.1.0" and uplan["version"]["source"] == "parameter"
           and by_name["commons-io-2.17.0.jar"]["action"] == "replace" and by_name["commons-io-2.17.0.jar"]["replaces"]["coordinate"] == "commons-io:commons-io:2.16.1"
           and io_strip["by"] == "repository jar" and "org/apache/commons/io/input/BOMInputStream.class" in io_strip["removed"]
           and io_strip["legal_dir"] == "META-INF/third-party/commons-io-2.16.1/" and not io_strip["kept_other"]
           and by_name["commons-io-2.17.0.jar"]["replaces"]["repository_sha256_matches"] is True)
        ok("merged resources lose the lines the replaced component contributed",
           codec_strip["merged_lines"]["META-INF/services/com.example.Codec"] == ["com.example.codec.a.JsonCodec"]
           and codec_strip["merged_lines"]["META-INF/spring.factories"] == ["com.example.Factory=com.example.codec.a.AFactory"]
           and "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports" in codec_strip["merged_lines"]
           and "com.example.codec:codec-a:1.0" not in uman["contents_attr"] and "com.example.codec:codec-a:1.1" in uman["contents_attr"]
           and "commons-io:commons-io:2.16.1" not in uman["contents_attr"] and len(uman["components"]) == 10
           and uman["transformers"]["props_merge"] == ["META-INF/spring.factories"] and uman["transformers"]["log4j_plugins"] is False
           and set(uman["rework"]["stale"]["lines"]) == {"META-INF/services/com.example.Codec", "META-INF/spring.factories",
                                                          "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports",
                                                          "META-INF/spring-autoconfigure-metadata.properties"})
        u_inputs = [Path(i["file"]) for i in uman["inputs"]]
        u_bundle = dict(bundle, version="1.1.0")
        u_good = fx.simulate_shade(uproj, u_inputs, u_bundle, uman["contents_attr"], source_attr, True, uproj / "target" / "thirdparty-bundle-1.1.0.jar",
                                   boot_version="3.3.5", extra_manifest=r_extra)
        fx.write_reduced_pom(uproj / "target" / "dependency-reduced-pom.xml", u_bundle, [("org.yaml", "snakeyaml", "2.2"), ("commons-io", "commons-io", "2.17.0")])
        run("finalize_bundle_pom.py", "--manifest", out / "upgrade-manifest.json", "--report", out / "upgrade-pom.json")
        run("verify_bundle.py", "--manifest", out / "upgrade-manifest.json", "--out", out / "upgrade-verification.json")
        uver = read_json(out / "upgrade-verification.json")
        uchecks = {c["check"]: c for c in uver["checks"]}
        with zipfile.ZipFile(u_good) as zf:
            u_names = zf.namelist()
            u_services = set(significant_lines(zf.read("META-INF/services/com.example.Codec").decode()))
            u_props = properties_values(parse_properties(zf.read("META-INF/spring.factories").decode()))
        ok("upgraded bundle holds the new versions only and its POM drops the embedded commons-io the new codec declares",
           uver["ok"] and uchecks["replaced-components-removed"]["status"] == "pass" and uchecks["spring-properties-merged"]["status"] == "pass"
           and "org/apache/commons/io/input/BOMInputStream.class" not in u_names and "org/apache/commons/io/file/PathUtils.class" in u_names
           and "com/example/codec/a/JsonCodec.class" not in u_names and "com/example/codec/a/JsonCodec2.class" in u_names
           and "META-INF/third-party/commons-io-2.16.1/LICENSE.txt" not in u_names and "META-INF/third-party/commons-io-2.17.0/LICENSE.txt" in u_names
           and u_services == {"com.example.codec.a.JsonCodec2", "com.example.codec.b.XmlCodec"}
           and set(u_props["com.example.Factory"]) == {"com.example.codec.a.A2Factory", "com.example.codec.b.BFactory", "com.example.codec.b.B2Factory"}
           and [d["artifactId"] for d in read_json(out / "upgrade-pom.json")["pruned_embedded"]] == ["commons-io"])
        stale_src = fx.make_jar(work / "upload" / "stale-line.jar", {"META-INF/services/com.example.Codec": b"com.example.codec.a.JsonCodec\n"})
        stale = fx.simulate_shade(uproj, u_inputs + [stale_src], u_bundle, uman["contents_attr"], source_attr, True, uproj / "target" / "stale.jar",
                                  boot_version="3.3.5", extra_manifest=r_extra)
        run("verify_bundle.py", "--manifest", out / "upgrade-manifest.json", "--bundle", stale, "--out", out / "upgrade-bv-stale.json", expect=1)
        ok("a line of the replaced component that survives the merge fails the verification",
           any(c["check"] == "replaced-components-removed" and c["status"] == "fail" and "JsonCodec" in c["detail"] for c in read_json(out / "upgrade-bv-stale.json")["checks"]))

        # refusals, and the version chain of a bundle that is itself a rework
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle-plain.json", "--add-jar", libs["commons-codec"], "--repo-url", repo_url, "--out", work / "rework-noop",
            "--manifest-out", out / "noop-manifest.json", "--plan-out", out / "noop-plan.json", env=with_creds, expect=1)
        ok("a jar identical to an embedded component leaves nothing to do", not (out / "noop-manifest.json").exists())
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle-plain.json", "--add-jar", libs["snakeyaml"], "--new-version", "1.0.0", "--out", work / "rework-same",
            "--manifest-out", out / "same-manifest.json", "--plan-out", out / "same-plan.json", expect=2)
        ok("republishing a release under its own version is refused", not (out / "same-manifest.json").exists())
        notjar = work / "upload" / "notes.txt"
        notjar.write_text("not a jar", encoding="utf-8")
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle-plain.json", "--add-jar", notjar, "--out", work / "rework-notjar",
            "--manifest-out", out / "notjar-manifest.json", "--plan-out", out / "notjar-plan.json", expect=2)
        ok("an upload that is not a jar is refused", not (out / "notjar-manifest.json").exists())
        fx.put_artifact(nexus_root, bundle["groupId"], bundle["artifactId"], "1.0.0.1", r_good, (rproj / "target" / "thirdparty-bundle-1.0.0.1.pom").read_bytes())
        run("fetch_bundle.py", "--bundle", "com.company.platform:thirdparty-bundle:1.0.0.1", "--repo-url", repo_url, "--out", work / "rework-in2", "--meta", out / "rework-bundle2.json", env=with_creds)
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle2.json", "--add-jar", libs["snakeyaml"], "--repo-url", repo_url, "--out", work / "rework-chain",
            "--manifest-out", out / "chain-manifest.json", "--plan-out", out / "chain-plan.json", env=with_creds)
        cman = read_json(out / "chain-manifest.json")
        ok("a rework of a rework counts the version up and keeps the base",
           cman["bundle"]["version"] == "1.0.0.2" and cman["rework"]["base"] == "com.company.platform:thirdparty-bundle:1.0.0"
           and cman["rework"]["from"] == "com.company.platform:thirdparty-bundle:1.0.0.1" and len(cman["components"]) == 15
           and read_json(out / "rework-bundle2.json")["manifest"]["reworked_from"] == "com.company.platform:thirdparty-bundle:1.0.0"
           and "<%s>com.company.platform:thirdparty-bundle:1.0.0.1</%s>" % (BUNDLE_REWORKED_FROM_ATTR, BUNDLE_REWORKED_FROM_ATTR) in (work / "rework-chain" / "pom.xml").read_text(encoding="utf-8"))
        run("rework_bundle.py", "--bundle-meta", out / "rework-bundle2.json", "--add-jar", libs["snakeyaml"], "--repo-url", repo_url, "--new-version", "1.0.0", "--out", work / "rework-taken",
            "--manifest-out", out / "taken-manifest.json", "--plan-out", out / "taken-plan.json", env=with_creds, expect=2)
        ok("a version the repository already has is refused", not (out / "taken-manifest.json").exists())
        ok("derived versions: .1 for a release, counting up along the base, a SNAPSHOT keeps its version",
           derive_version("1.0.0", "1.0.0") == "1.0.0.1" and derive_version("2.1.7", "2.1") == "2.1.8" and derive_version("2.1.7", "3.0") == "2.1.7.1"
           and derive_version("1.0.0-SNAPSHOT", None) == "1.0.0-SNAPSHOT")

        # ---- stage 7: consumer patch -------------------------------------------------------
        print("[consumer patch]")
        tree = work / "dependency-tree.txt"
        tree.write_text(fx.SAMPLE_TREE, encoding="utf-8")
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest.json", "--tree", tree, "--out", out / "consumer-patch.md", "--out-json", out / "consumer-patch.json")
        patch = read_json(out / "consumer-patch.json")
        mods = {m["module"]: m for m in patch["modules"]}
        app_mod = mods["com.example:app"]
        ok("direct dependency to remove", list(app_mod["remove_direct"]) == ["commons-io:commons-io"])
        web_targets = set(app_mod["exclusions"]["org.springframework.boot:spring-boot-starter-web"]["targets"])
        ok("exclusions per direct dependency",
           web_targets == {"org.springframework.boot:spring-boot", "org.springframework:spring-core", "org.springframework:spring-jcl"}
           and app_mod["exclusions"]["org.apache.commons:commons-text"]["targets"] == ["commons-io:commons-io"])
        ok("second module parsed", mods["com.example:app-batch"]["exclusions"]["com.example:app"]["targets"] == ["commons-io:commons-io"])
        ok("promoted dependency the modules already reach is excluded on the bundle dependency",
           list(app_mod["bundle_exclusions"]) == ["org.yaml:snakeyaml"] and "already reached" in app_mod["bundle_exclusions"]["org.yaml:snakeyaml"]
           and list(mods["com.example:app-batch"]["bundle_exclusions"]) == ["org.yaml:snakeyaml"] and not app_mod["promoted_kept"])
        md = (out / "consumer-patch.md").read_text(encoding="utf-8")
        ok("markdown has enforcer rule", "<exclude>commons-io:commons-io</exclude>" in md and "bannedDependencies" in md)
        ok("boot embedded: plugin alignment instead of a version pin", patch["boot_embedded"] and not patch["boot_line_excludes"] and "spring-boot-maven-plugin" in md)

        run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "commons-io", "--out", out / "sel-io-only.json")
        run("gen_bundle_project.py", "--selection", out / "sel-io-only.json", "--source", out / "source.json", "--group-id", bundle["groupId"],
            "--artifact-id", "io-bundle", "--version", "1.0.0", "--out", work / "bundle-io", "--manifest-out", out / "bundle-manifest-io.json")
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest-io.json", "--out", out / "consumer-patch-io.md", "--out-json", out / "consumer-patch-io.json")
        patch_io = read_json(out / "consumer-patch-io.json")
        md_io = (out / "consumer-patch-io.md").read_text(encoding="utf-8")
        ok("boot not embedded: consumer pinned to the Boot minor line",
           patch_io["boot_line_excludes"] == ["org.springframework.boot:spring-boot:(,3.3)", "org.springframework.boot:spring-boot:[3.4,)"]
           and "<exclude>org.springframework.boot:spring-boot:[3.4,)</exclude>" in md_io and "dependency-tree.txt" in md_io)

        run("gen_consumer_patch.py", "--bundle-jar", good, "--tree", tree, "--out", out / "consumer-patch-jar.md", "--out-json", out / "consumer-patch-jar.json")
        patch_jar = read_json(out / "consumer-patch-jar.json")
        ok("bundle jar mode reads contents and Boot version from the manifest",
           patch_jar["coordinate"] == "com.company.platform:thirdparty-bundle:1.0.0" and patch_jar["spring_boot_version"] == "3.3.5"
           and {m["module"]: m for m in patch_jar["modules"]}["com.example:app"]["remove_direct"].keys() == {"commons-io:commons-io"})
        run("gen_consumer_patch.py", "--bundle", "com.company.platform:thirdparty-bundle:1.0.0",
            "--embedded", "commons-io:commons-io:2.16.1, org.springframework:spring-core:6.1.14", "--tree", tree,
            "--out", out / "consumer-patch-standalone.md", "--out-json", out / "consumer-patch-standalone.json")
        sa = {m["module"]: m for m in read_json(out / "consumer-patch-standalone.json")["modules"]}
        ok("standalone mode from explicit coordinates", list(sa["com.example:app"]["remove_direct"]) == ["commons-io:commons-io"])
        fx.write_reduced_pom(work / "promoted.pom", {"groupId": "com.company.platform", "artifactId": "core-bundle", "version": "1.0.0"},
                             [("org.springframework", "spring-jcl", "6.1.14"), ("org.yaml", "snakeyaml", "2.2"),
                              ("org.apache.tomcat", "tomcat-annotations-api", "10.1.59"), ("org.apache.commons", "commons-text", "1.12.0")])
        run("gen_consumer_patch.py", "--bundle", "com.company.platform:core-bundle:1.0.0", "--embedded", "org.springframework:spring-core:6.1.14",
            "--tree", tree, "--reduced-pom", work / "promoted.pom", "--out", out / "cp-promoted.md", "--out-json", out / "cp-promoted.json")
        pm = {m["module"]: m for m in read_json(out / "cp-promoted.json")["modules"]}["com.example:app"]
        ok("promoted dependencies: kept when reachable only through an embedded coordinate, excluded when absent, already reachable or test-only",
           list(pm["promoted_kept"]) == ["org.springframework:spring-jcl"]
           and set(pm["bundle_exclusions"]) == {"org.yaml:snakeyaml", "org.apache.tomcat:tomcat-annotations-api", "org.apache.commons:commons-text"}
           and "not in this module" in pm["bundle_exclusions"]["org.apache.tomcat:tomcat-annotations-api"]
           and "test scope" in pm["bundle_exclusions"]["org.apache.commons:commons-text"]
           and "<artifactId>tomcat-annotations-api</artifactId>" in (out / "cp-promoted.md").read_text(encoding="utf-8"))
        run("gen_consumer_patch.py", "--bundle", "bad-coordinates", "--embedded", "x:y:1", "--out", out / "cp-bad.md", "--out-json", out / "cp-bad.json", expect=2)
        ok("standalone mode validates the bundle coordinates", not (out / "cp-bad.json").exists())

        # ---- apply the proposal to a consumer project ---------------------------------------
        print("[apply consumer patch]")
        cons = fx.build_consumer_project(work / "consumer")
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", cons, "--out", out / "apply.json", "--diff", out / "apply.diff")
        applied = read_json(out / "apply.json")
        app_pom = (cons / "app" / "pom.xml").read_text(encoding="utf-8")
        root_pom = (cons / "pom.xml").read_text(encoding="utf-8")
        batch_pom = (cons / "app-batch" / "pom.xml").read_text(encoding="utf-8")
        ok("direct dependency removed and bundle added first",
           "<artifactId>commons-io</artifactId>\n            <version>2.16.1</version>" not in app_pom
           and app_pom.index("thirdparty-bundle") < app_pom.index("spring-boot-starter-web")
           and sorted(c["path"] for c in applied["changed_files"]) == ["app-batch/pom.xml", "app/pom.xml", "pom.xml"])
        ok("exclusions merged into the existing block and added where missing",
           app_pom.count("<exclusion>") == 6 and "spring-boot-starter-logging" in app_pom
           and "<artifactId>spring-core</artifactId>" in app_pom and batch_pom.count("<exclusion>") == 2)
        ok("promoted dependency excluded on the bundle dependency itself",
           "<artifactId>snakeyaml</artifactId>" in app_pom.split("thirdparty-bundle")[1].split("</dependency>")[0]
           and "<artifactId>snakeyaml</artifactId>" in batch_pom.split("thirdparty-bundle")[1].split("</dependency>")[0]
           and any("from the bundle dependency" in a for f in applied["changed_files"] for a in f["actions"]))
        ok("enforcer execution added next to the existing one",
           root_pom.count("<execution>") == 2 and "ban-bundled-coordinates" in root_pom and "enforce-versions" in root_pom
           and "<exclude>org.springframework:spring-core</exclude>" in root_pom)
        ok("bundle version managed in the multi-module root",
           "<version>1.0.0</version>" in root_pom.split("<dependencyManagement>")[1] and "<version>" not in app_pom.split("thirdparty-bundle")[1].split("</dependency>")[0])
        ok("formatting, comments and indentation preserved",
           "<!-- web stack -->" in app_pom and "<!-- shared build settings -->" in root_pom
           and "<relativePath/> <!-- lookup parent from repository -->" in root_pom
           and "\n            <groupId>com.company.platform</groupId>" in app_pom
           and all(ET.fromstring(p.encode("utf-8")) is not None for p in (app_pom, root_pom, batch_pom)))
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", cons, "--out", out / "apply2.json", "--diff", out / "apply2.diff")
        ok("applying twice changes nothing", (out / "apply2.diff").read_text(encoding="utf-8") == "" and not read_json(out / "apply2.json")["changed_files"])
        single = fx.build_consumer_project(work / "consumer-single", single=True)
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", single, "--dry-run", "--out", out / "apply-single.json", "--diff", out / "apply-single.diff")
        single_diff = (out / "apply-single.diff").read_text(encoding="utf-8")
        ok("single module: explicit bundle version, build/plugins created, dry run leaves the file untouched",
           "+      <version>1.0.0</version>" in single_diff and "+      <exclusions>" in single_diff and "+  <build>" in single_diff and "maven-enforcer-plugin" in single_diff
           and (single / "pom.xml").read_text(encoding="utf-8") == fx.CONSUMER_SINGLE_POM
           and any("app-batch" in w for w in read_json(out / "apply-single.json")["warnings"]))
        ok("boot plugin not declared in the checkout: warning only",
           any("spring-boot-maven-plugin is not declared" in w for w in read_json(out / "apply-single.json")["warnings"]))

        # ---- spring-boot-maven-plugin aligned to the embedded Boot version -------------------
        print("[boot plugin alignment]")
        ok("boot plugin without a version under spring-boot-starter-parent of the embedded version is left alone",
           "<artifactId>spring-boot-maven-plugin</artifactId>\n            </plugin>" in app_pom
           and any("spring-boot-starter-parent 3.3.5" in n for n in applied["spring_boot_plugin"]["notes"])
           and not any("spring-boot-maven-plugin" in a for f in applied["changed_files"] for a in f["actions"]))
        boot = fx.build_boot_consumer_project(work / "consumer-boot")
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", boot, "--out", out / "apply-boot.json", "--diff", out / "apply-boot.diff")
        applied_boot = read_json(out / "apply-boot.json")
        boot_actions = [a for f in applied_boot["changed_files"] for a in f["actions"]]
        boot_root = (boot / "pom.xml").read_text(encoding="utf-8")
        boot_app = (boot / "app" / "pom.xml").read_text(encoding="utf-8")
        boot_batch = (boot / "app-batch" / "pom.xml").read_text(encoding="utf-8")
        ok("boot plugin: pluginManagement property replaced by the embedded version, the module declaration stays without one",
           "<version>3.3.5</version>" in boot_root.split("<pluginManagement>")[1].split("</pluginManagement>")[0]
           and "<spring-boot.version>3.2.5</spring-boot.version>" in boot_root
           and "<artifactId>spring-boot-maven-plugin</artifactId>\n            </plugin>" in boot_app
           and any("${spring-boot.version} (3.2.5) -> 3.3.5 in pluginManagement" in a for a in boot_actions)
           and any("pluginManagement entry inside the checkout" in n for n in applied_boot["spring_boot_plugin"]["notes"]))
        ok("boot plugin: literal version and the declaration inside a profile aligned",
           boot_batch.count("<version>3.3.5</version>") == 2 and "3.2.5" not in boot_batch
           and any("3.2.5 -> 3.3.5 in build/plugins" in a for a in boot_actions)
           and any("in profile docker build/plugins" in a for a in boot_actions)
           and all(ET.fromstring(p.encode("utf-8")) is not None for p in (boot_root, boot_app, boot_batch)))
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", boot, "--out", out / "apply-boot2.json", "--diff", out / "apply-boot2.diff")
        ok("boot plugin alignment applied twice changes nothing", (out / "apply-boot2.diff").read_text(encoding="utf-8") == "")
        boot_single = fx.build_boot_consumer_project(work / "consumer-boot-single", single=True)
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", boot_single, "--out", out / "apply-boot-single.json", "--diff", out / "apply-boot-single.diff")
        ok("boot plugin: version inserted after the artifactId when the parent manages another Boot version",
           "<artifactId>spring-boot-maven-plugin</artifactId> <!-- version from the parent -->\n        <version>3.3.5</version>\n      </plugin>" in (boot_single / "pom.xml").read_text(encoding="utf-8")
           and any("managed by spring-boot-starter-parent 3.2.5" in a for f in read_json(out / "apply-boot-single.json")["changed_files"] for a in f["actions"]))
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest-io.json", "--tree", tree, "--out", out / "consumer-patch-io-tree.md", "--out-json", out / "consumer-patch-io-tree.json")
        boot_io = fx.build_boot_consumer_project(work / "consumer-boot-io")
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch-io-tree.json", "--project", boot_io, "--out", out / "apply-boot-io.json", "--diff", out / "apply-boot-io.diff")
        ok("boot not embedded: plugin versions untouched",
           "<version>3.2.5</version>" in (boot_io / "app-batch" / "pom.xml").read_text(encoding="utf-8")
           and read_json(out / "apply-boot-io.json")["spring_boot_plugin"] is None)

        # ---- dependencies declared in profiles -----------------------------------------------
        print("[profile dependencies]")
        ok("direct dependencies of each module recorded in the proposal",
           "commons-io:commons-io" in mods["com.example:app"]["direct"] and "org.springframework.boot:spring-boot-starter-web" in mods["com.example:app"]["direct"]
           and mods["com.example:app-batch"]["direct"] == ["com.example:app", "org.yaml:snakeyaml"])
        prof = fx.build_consumer_project(work / "consumer-profiles", profiles=True)
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", prof, "--out", out / "apply-prof.json", "--diff", out / "apply-prof.diff")
        applied_prof = read_json(out / "apply-prof.json")
        prof_actions = {f["path"]: f["actions"] for f in applied_prof["changed_files"]}
        prof_app = (prof / "app" / "pom.xml").read_text(encoding="utf-8")
        prof_root = (prof / "pom.xml").read_text(encoding="utf-8")
        ok("replaced coordinate removed from the main dependencies, the module's profile and the parent's profile",
           "<version>2.16.1</version>" not in prof_app and "<version>2.15.0</version>" not in prof_app and "<version>2.11.0</version>" not in prof_root
           and "removed dependency commons-io:commons-io from profile local" in prof_actions["app/pom.xml"]
           and "removed dependency commons-io:commons-io from profile legacy" in prof_actions["pom.xml"]
           and not any("commons-io:commons-io not found" in w for w in applied_prof["warnings"]))
        ok("exclusions added on the profile's re-declaration as well, merged with its own",
           prof_app.count("<exclusion>") == 10
           and prof_app.split("<id>local</id>")[1].count("<artifactId>spring-core</artifactId>") == 1
           and "spring-boot-starter-json" in prof_app
           and any(a.startswith("excluded ") and a.endswith("from org.springframework.boot:spring-boot-starter-web in profile local") for a in prof_actions["app/pom.xml"])
           and all(ET.fromstring(p.encode("utf-8")) is not None for p in (prof_app, prof_root)))
        ok("profile dependency the tree never showed is reported, the replaced and bundle coordinates are not",
           any("com.h2database:h2 (profile local of app/pom.xml)" in w and "not active" in w for w in applied_prof["warnings"])
           and not any("commons-io" in w or "thirdparty-bundle" in w for w in applied_prof["warnings"]))
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch.json", "--project", prof, "--out", out / "apply-prof2.json", "--diff", out / "apply-prof2.diff")
        ok("profile handling applied twice changes nothing",
           (out / "apply-prof2.diff").read_text(encoding="utf-8") == "" and not read_json(out / "apply-prof2.json")["changed_files"])

        # ---- build plugins whose dependency tree contains embedded coordinates ---------------
        print("[plugin dependencies]")
        eff = work / "effective-pom.xml"
        eff.write_text(fx.SAMPLE_EFFECTIVE_POM, encoding="utf-8")
        run("plugin_trees.py", "--effective-pom", eff, "--reactor", work / "plugin-trees", "--plugins-out", out / "consumer-plugins.json")
        plugins_map = read_json(out / "consumer-plugins.json")
        ids = {p["plugin"]["artifactId"]: p["id"] for p in plugins_map["plugins"]}
        sonar_module_pom = (work / "plugin-trees" / ids["sonar-maven-plugin"] / "pom.xml").read_text(encoding="utf-8")
        ok("effective POM turned into one reactor module per plugin configuration, added dependencies first",
           len(plugins_map["plugins"]) == 5 and sorted(plugins_map["modules"]) == ["com.example:app", "com.example:app-batch"]
           and plugins_map["plugins"][0]["modules"] == ["com.example:app", "com.example:app-batch"] and ids["sonar-maven-plugin"] == "plugin-4"
           and sonar_module_pom.split("<dependencies>")[1].index("vendor-tool") < sonar_module_pom.split("<dependencies>")[1].index("sonar-maven-plugin")
           and "<version>5.8.0.7211</version>" in sonar_module_pom
           and "<module>%s</module>" % ids["gen-maven-plugin"] in (work / "plugin-trees" / "pom.xml").read_text(encoding="utf-8")
           and ET.fromstring(sonar_module_pom.encode("utf-8")) is not None)
        ptree = work / "plugin-trees.txt"
        ptree.write_text(fx.SAMPLE_PLUGIN_TREES, encoding="utf-8")
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest.json", "--tree", tree, "--plugin-tree", ptree, "--plugins", out / "consumer-plugins.json",
            "--out", out / "consumer-patch-plugins.md", "--out-json", out / "consumer-patch-plugins.json")
        pp = read_json(out / "consumer-patch-plugins.json")
        by_plugin = {e["plugin"].split(":")[1]: e for e in pp["plugins"]}
        ok("plugins without hits are only counted, hits are classified by where Maven lets the consumer act",
           pp["plugin_tree_provided"] and pp["plugins_without_hits"] == 1
           and set(by_plugin) == {"maven-surefire-plugin", "spring-boot-maven-plugin", "sonar-maven-plugin", "gen-maven-plugin"}
           and list(by_plugin["sonar-maven-plugin"]["remove_declared"]) == ["commons-io:commons-io"]
           and by_plugin["sonar-maven-plugin"]["declared_exclusions"]["com.vendor:vendor-tool"]["targets"] == ["commons-io:commons-io"]
           and by_plugin["sonar-maven-plugin"]["redeclare"]["org.sonarsource.scanner.lib:sonar-scanner-java-library"] == {"version": "4.1.2.1663", "targets": ["commons-codec:commons-codec"]}
           and by_plugin["maven-surefire-plugin"]["redeclare"]["org.apache.maven.surefire:maven-surefire-common"]["targets"] == ["commons-io:commons-io"]
           and by_plugin["gen-maven-plugin"]["redeclare"]["com.vendor:gen-core"] == {"version": "3.0", "targets": ["commons-io:commons-io"]}
           and by_plugin["spring-boot-maven-plugin"]["shadowed"] == {"org.springframework:spring-core": "6.1.14", "org.springframework:spring-jcl": "6.1.14"}
           and not by_plugin["spring-boot-maven-plugin"]["redeclare"]
           and all(e["action"] == "patch" for e in pp["plugins"])
           and by_plugin["maven-surefire-plugin"]["bundle_exclusions"]["org.yaml:snakeyaml"] == "not in this plugin's dependency tree"
           and by_plugin["maven-surefire-plugin"]["modules"] == ["com.example:app", "com.example:app-batch"])
        md_p = (out / "consumer-patch-plugins.md").read_text(encoding="utf-8")
        ok("proposal explains the plugin changes", "## 6. Build plugins" in md_p and "<pluginManagement>" in md_p and "Shadowed only" in md_p
           and "Re-declare the plugin's own dependency `org.sonarsource.scanner.lib:sonar-scanner-java-library`" in md_p and "## 7. Verification" in md_p)
        pol = work / "policy-skip.json"
        pol.write_text(json.dumps({"plugins": {"direct_hits": "skip", "skip": ["org.apache.maven.plugins:maven-surefire-plugin"]}}), encoding="utf-8")
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest.json", "--tree", tree, "--plugin-tree", ptree, "--plugins", out / "consumer-plugins.json",
            "--policy", pol, "--out", out / "cp-skip.md", "--out-json", out / "cp-skip.json")
        skip_by = {e["plugin"].split(":")[1]: e for e in read_json(out / "cp-skip.json")["plugins"]}
        ok("policy: listed plugins and plugins whose own direct dependencies are embedded are left untouched",
           skip_by["maven-surefire-plugin"]["action"] == "skip" and "policy plugins.skip" in skip_by["maven-surefire-plugin"]["reason"]
           and skip_by["spring-boot-maven-plugin"]["action"] == "skip" and "cannot be excluded" in skip_by["spring-boot-maven-plugin"]["reason"]
           and skip_by["sonar-maven-plugin"]["action"] == "patch" and "Left untouched" in (out / "cp-skip.md").read_text(encoding="utf-8"))
        plug = fx.build_consumer_project(work / "consumer-plugins", plugins=True)
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch-plugins.json", "--project", plug, "--out", out / "apply-plug.json", "--diff", out / "apply-plug.diff")
        applied_plug = read_json(out / "apply-plug.json")
        plug_app = (plug / "app" / "pom.xml").read_text(encoding="utf-8")
        plug_root = (plug / "pom.xml").read_text(encoding="utf-8")
        sonar_decl = plug_app.split("<artifactId>sonar-maven-plugin</artifactId>")[1].split("</plugin>")[0]
        mgmt = plug_root.split("<pluginManagement>")[1].split("</pluginManagement>")[0]
        sonar_mgmt = mgmt.split("<artifactId>sonar-maven-plugin</artifactId>")[1].split("</plugin>")[0]
        boot_mgmt = mgmt.split("<artifactId>spring-boot-maven-plugin</artifactId>")[1].split("</plugin>")[0]
        ok("dependencies the POM adds to a plugin: the embedded one removed, exclusion added on the one that brings it",
           "<version>2.16.1</version>" not in sonar_decl and sonar_decl.count("<dependency>") == 1 and "vendor-tool" in sonar_decl
           and sonar_decl.count("<exclusion>") == 1 and "<artifactId>commons-io</artifactId>" in sonar_decl
           and "plugin sonar-maven-plugin: removed its dependency commons-io:commons-io (build/plugins)" in next(f["actions"] for f in applied_plug["changed_files"] if f["path"] == "app/pom.xml"))
        ok("root pluginManagement: bundle first with its exclusions, the plugin's own dependency re-declared, lifecycle defaults included",
           mgmt.count("<plugin>") == 4 and "maven-compiler-plugin" not in mgmt and "maven-surefire-plugin" in mgmt
           and sonar_mgmt.index("thirdparty-bundle") < sonar_mgmt.index("sonar-scanner-java-library")
           and "<artifactId>commons-codec</artifactId>" in sonar_mgmt and "<version>4.1.2.1663</version>" in sonar_mgmt
           and "<artifactId>maven-surefire-common</artifactId>" in mgmt and "<artifactId>gen-core</artifactId>" in mgmt
           and mgmt.count("<artifactId>thirdparty-bundle</artifactId>") == 4 and mgmt.count("<version>1.0.0</version>") == 4
           and mgmt.count("<artifactId>snakeyaml</artifactId>") == 4 and boot_mgmt.count("<dependency>") == 1
           and all(ET.fromstring(p.encode("utf-8")) is not None for p in (plug_app, plug_root)))
        boot_report = next(p for p in applied_plug["plugins"] if "spring-boot-maven-plugin" in p["plugin"])
        ok("apply reports the shadowed coordinates", len(applied_plug["plugins"]) == 4 and all(p["action"] == "patched" for p in applied_plug["plugins"])
           and set(boot_report["shadowed"]) == {"org.springframework:spring-core", "org.springframework:spring-jcl"}
           and any("stays in its realm" in n for n in boot_report["notes"]))
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch-plugins.json", "--project", plug, "--out", out / "apply-plug2.json", "--diff", out / "apply-plug2.diff")
        ok("plugin dependency changes applied twice change nothing", (out / "apply-plug2.diff").read_text(encoding="utf-8") == "")
        after = work / "plugin-trees-after.txt"
        after.write_text(fx.SAMPLE_PLUGIN_TREES_AFTER, encoding="utf-8")
        run("verify_plugin_trees.py", "--patch", out / "consumer-patch-plugins.json", "--plugins", out / "consumer-plugins.json", "--tree", after,
            "--out", out / "plugin-trees-check.json", "--findings-out", out / "plugin-trees-findings.txt")
        chk = read_json(out / "plugin-trees-check.json")
        ok("post-patch plugin trees: bundle present, only the shadowed coordinates remain, the leftover is a finding",
           not chk["ok"] and len(chk["findings"]) == 1 and "gen-maven-plugin" in chk["findings"][0] and "commons-io:commons-io" in chk["findings"][0]
           and any(p["plugin"].endswith("spring-boot-maven-plugin:3.3.5") and p["status"] == "ok" and p["shadowed"] == ["org.springframework:spring-core", "org.springframework:spring-jcl"] for p in chk["plugins"])
           and any(p["plugin"].endswith("sonar-maven-plugin:5.8.0.7211") and p["status"] == "ok" and not p["remaining"] for p in chk["plugins"])
           and (out / "plugin-trees-findings.txt").read_text(encoding="utf-8").startswith("[plugins] "))
        summary_dir = work / "summary-plugins"
        summary_dir.mkdir()
        shutil.copy(out / "consumer-patch-plugins.json", summary_dir / "consumer-patch.json")
        shutil.copy(out / "apply-plug.json", summary_dir / "consumer-apply.json")
        shutil.copy(out / "plugin-trees-check.json", summary_dir / "plugin-trees-check.json")
        run("write_summary.py", "--out-dir", summary_dir)
        summary_md = (summary_dir / "SUMMARY.md").read_text(encoding="utf-8")
        ok("summary covers the plugin proposal, the applied plugin changes and the check",
           "Build plugins: 4 with an embedded coordinate" in summary_md and "## Plugin dependency check: 1 finding(s) to review" in summary_md
           and "pluginManagement sonar-maven-plugin" in summary_md)
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch-io.json", "--project", cons, "--out", out / "apply-notree.json", "--diff", out / "apply-notree.diff", expect=2)
        ok("a proposal without a tree cannot be applied", not (out / "apply-notree.json").exists())
        (work / "consumer" / "app" / "target").mkdir(parents=True, exist_ok=True)
        rebuilt = fx.build_new_app(app, good, selected_jars, work / "consumer" / "app" / "target" / "app-1.0.0.jar")
        (work / "consumer" / "app" / "target" / "app-1.0.0.jar.original").write_bytes(b"not a jar")
        run("find_app_jar.py", "--project", cons, "--manifest", out / "bundle-manifest.json", "--out", out / "rebuilt-app.json")
        found = read_json(out / "rebuilt-app.json")
        ok("rebuilt boot archive located and the original artifact remembered",
           Path(found["new_jar"]) == rebuilt and found["old_ref"] == str(app) and found["old_type"] == "file")

        # ---- test jars of embedded coordinates and version drift -----------------------------
        print("[test jars and version drift]")
        main_patch = read_json(out / "consumer-patch.json")
        ok("the sample proposal has no test jar, no drift and no include",
           main_patch["enforcer_includes"] == [] and main_patch["version_drift"] == []
           and not any(m["test_variants"] or m["redeclare"] or m["other_variants"] for m in main_patch["modules"]))
        vout = work / "out-variants"
        vout.mkdir(parents=True, exist_ok=True)
        vbundle = {"groupId": "com.company.platform", "artifactId": "orders-thirdparty", "version": "1.0.0"}
        vcomponents = [{"gav": dict({"groupId": g, "artifactId": a, "version": v}, **({"classifier": c} if c else {}))}
                       for g, a, v, c in fx.VARIANT_EMBEDDED]
        write_json(vout / "variant-manifest.json", {"bundle": vbundle, "coordinate": gav_str(vbundle), "components": vcomponents,
                                                    "reduced_pom": "", "pom": "", "spring_boot_version": "3.3.5"})
        (vout / "tree.txt").write_text(fx.VARIANT_TREE, encoding="utf-8")
        run("gen_consumer_patch.py", "--manifest", vout / "variant-manifest.json", "--tree", vout / "tree.txt",
            "--out", vout / "consumer-patch.md", "--out-json", vout / "consumer-patch.json")
        vp = read_json(vout / "consumer-patch.json")
        vm = {m["module"]: m for m in vp["modules"]}
        orders, orders_it = vm["com.example:orders"], vm["com.example:orders-it"]
        ok("a test-scope jar under another classifier is no embedded coordinate, a runtime one still is",
           list(orders["remove_direct"]) == ["org.springframework.kafka:spring-kafka", "com.example:orders-common", "io.netty:netty-transport-native-epoll"]
           and set(orders["test_variants"]) == {"com.example:orders-common:tests", "org.apache.kafka:kafka-clients:test"}
           and list(orders["other_variants"]) == ["io.netty:netty-transport-native-epoll:linux-aarch_64"])
        ok("no exclusions on a deleted direct dependency, the kept test-jar gets the ones below it",
           "org.springframework.kafka:spring-kafka" not in orders["exclusions"]
           and orders["exclusions"]["com.example:orders-common"]["targets"] == ["org.apache.commons:commons-lang3"]
           and orders["exclusions"]["org.springframework.kafka:spring-kafka-test"]["targets"] == [
               "org.springframework.kafka:spring-kafka", "org.springframework:spring-core", "org.apache.kafka:kafka-clients", "com.github.luben:zstd-jni"])
        rd = orders["redeclare"].get("org.apache.kafka:kafka-clients:test") or {}
        ok("a test jar every path to which is cut is re-declared after its dependency, with the exclusions below it",
           list(orders["redeclare"]) == ["org.apache.kafka:kafka-clients:test"] and rd.get("after") == "org.springframework.kafka:spring-kafka-test"
           and rd.get("exclusions") == ["com.github.luben:zstd-jni"] and rd.get("version") == "3.7.1" and rd.get("cut_by") == "org.apache.kafka:kafka-clients")
        ok("declared and still reachable test jars are only kept",
           not orders_it["redeclare"] and orders_it["test_variants"]["org.apache.kafka:kafka-clients:test"]["declared"]
           and orders_it["test_variants"]["com.example:orders-common:tests"]["kept_through"] == "com.example:orders-testkit -> com.example:orders-common"
           and orders_it["exclusions"]["org.apache.kafka:kafka-clients"]["targets"] == ["com.github.luben:zstd-jni"]
           and orders_it["exclusions"]["com.example:orders-testkit"]["targets"] == ["org.apache.commons:commons-lang3"])
        ok("the Enforcer rule lets the test jars through in test scope only",
           vp["enforcer_includes"] == ["com.example:orders-common:*:*:test:tests", "org.apache.kafka:kafka-clients:*:*:test:test"])
        drift = {(d["kind"], d["coordinate"]): d for d in vp["version_drift"]}
        boot_test = drift.get(("train", "org.springframework.boot:spring-boot-test")) or {}
        ok("version drift: embedded coordinates at other versions and jars of an embedded release train, not unrelated groups",
           set(drift) == {("embedded", "org.springframework.boot:spring-boot"), ("embedded", "org.springframework.boot:spring-boot-autoconfigure"),
                          ("train", "org.springframework.boot:spring-boot-starter"), ("train", "org.springframework:spring-test"),
                          ("train", "org.springframework.boot:spring-boot-starter-test"), ("train", "org.springframework.boot:spring-boot-test")}
           and boot_test.get("bundle_version") == "3.3.5" and boot_test.get("version") == "3.3.6" and boot_test.get("scope") == "test"
           and drift[("embedded", "org.springframework.boot:spring-boot")]["modules"] == ["com.example:orders"])
        vfind = (vout / "consumer-patch-findings.txt").read_text(encoding="utf-8").splitlines()
        vmd = (vout / "consumer-patch.md").read_text(encoding="utf-8")
        ok("findings and proposal list the drift, the variant and the test jars",
           sum(1 for f in vfind if f.startswith("[drift]")) == 6 and sum(1 for f in vfind if f.startswith("[variant]")) == 1
           and any(f.startswith("[info] com.example:orders: test jar org.apache.kafka:kafka-clients:test 3.7.1 re-declared") for f in vfind)
           and "## Version drift between the consumer and the bundle" in vmd and "Spring Boot parent or BOM 3.3.5" in vmd
           and "<classifier>test</classifier>" in vmd and "<include>org.apache.kafka:kafka-clients:*:*:test:test</include>" in vmd)
        vcons = fx.build_variant_consumer_project(work / "consumer-variants")
        run("apply_consumer_patch.py", "--patch", vout / "consumer-patch.json", "--project", vcons,
            "--out", vout / "consumer-apply.json", "--diff", vout / "consumer-apply.diff")
        va = read_json(vout / "consumer-apply.json")
        opom = (vcons / "orders" / "pom.xml").read_text(encoding="utf-8")
        ipom = (vcons / "orders-it" / "pom.xml").read_text(encoding="utf-8")
        rpom = (vcons / "pom.xml").read_text(encoding="utf-8")
        tj_start = opom.rindex("<dependency>", 0, opom.index("<type>test-jar</type>"))
        tj = opom[tj_start:opom.index("</dependency>", tj_start)]
        ok("the test-jar declaration stays with its exclusion, the main jar and both natives go",
           "commons-lang3" in tj and opom.count("<artifactId>orders-common</artifactId>") == 1
           and "netty-transport-native-epoll" not in opom)
        note = "mvn-rebundle: the exclusion of org.apache.kafka:kafka-clients on org.springframework.kafka:spring-kafka-test removes this test jar too"
        rd_at = opom.find(note)
        rd_block = opom[rd_at:opom.index("</dependency>", max(rd_at, 0))] if rd_at >= 0 else ""
        ok("the cut test jar is declared right after spring-kafka-test, with test scope and its exclusion",
           opom.index("<artifactId>spring-kafka-test</artifactId>") < rd_at < opom.index("micrometer-observation-test")
           and "<classifier>test</classifier>" in rd_block and "<scope>test</scope>" in rd_block and "<version>3.7.1</version>" in rd_block
           and "<artifactId>zstd-jni</artifactId>" in rd_block)
        ok("a declared test classifier stays next to the removed main jar",
           ipom.count("<artifactId>kafka-clients</artifactId>") == 1 and "<classifier>test</classifier>" in ipom
           and "<artifactId>zstd-jni</artifactId>" in ipom and ipom.count("<artifactId>commons-lang3</artifactId>") == 1)
        ok("the root POM's Enforcer rule carries the includes, one also for a test jar only a profile declares",
           "<include>com.example:orders-common:*:*:test:tests</include>" in rpom and "<include>org.apache.kafka:kafka-clients:*:*:test:test</include>" in rpom
           and "<include>io.netty:netty-transport-native-epoll:*:*:test:linux-aarch_64</include>" in rpom
           and rpom.index("</excludes>") < rpom.index("<includes>") < rpom.index("<message>")
           and len(va["enforcer_includes"]) == 3 and "linux-aarch_64</classifier>" in ipom)
        ok("the apply report lists the kept and declared test jars, without a warning about deleted dependencies",
           va["test_variants"]["declared"] == ["com.example:orders: org.apache.kafka:kafka-clients:test 3.7.1"]
           and sorted(va["test_variants"]["kept"]) == ["com.example:orders-common:tests in com.example:orders", "org.apache.kafka:kafka-clients:test in com.example:orders-it"]
           and not any("not found" in w for w in va["warnings"])
           and all(ET.fromstring(p.encode("utf-8")) is not None for p in (opom, ipom, rpom)))
        run("apply_consumer_patch.py", "--patch", vout / "consumer-patch.json", "--project", vcons,
            "--out", vout / "consumer-apply2.json", "--diff", vout / "consumer-apply2.diff")
        ok("applying the variant proposal twice changes nothing",
           (vout / "consumer-apply2.diff").read_text(encoding="utf-8") == "" and not read_json(vout / "consumer-apply2.json")["changed_files"])
        run("write_summary.py", "--out-dir", vout)
        vsum = (vout / "SUMMARY.md").read_text(encoding="utf-8")
        ok("summary lists the drift, the test jars and the variant",
           "Version drift between the consumer and the bundle" in vsum
           and "- WARNING: `org.springframework.boot:spring-boot-test` 3.3.6 (test scope) belongs to `org.springframework.boot`, embedded at 3.3.5 (com.example:orders)" in vsum
           and "`org.apache.kafka:kafka-clients:test` in com.example:orders (declared directly with test scope)" in vsum
           and "- test jar declared with test scope: com.example:orders: org.apache.kafka:kafka-clients:test 3.7.1" in vsum
           and "goes with the embedded coordinate although the bundle does not contain it" in vsum)

        # ---- unidentified jar end to end: synthetic coordinates ---------------------------
        print("[synthetic coordinates]")
        run("resolve_selection.py", "--inventory", out / "inventory-offline.json", "--select", "vendor-sdk", "--out", out / "sel-syn.json")
        syn_bundle = {"groupId": bundle["groupId"], "artifactId": "syn-bundle", "version": "1.0.0"}
        syn_proj = work / "bundle-syn"
        run("gen_bundle_project.py", "--selection", out / "sel-syn.json", "--source", out / "source.json", "--group-id", syn_bundle["groupId"],
            "--artifact-id", syn_bundle["artifactId"], "--version", syn_bundle["version"], "--out", syn_proj, "--manifest-out", out / "bundle-manifest-syn.json")
        syn_man = read_json(out / "bundle-manifest-syn.json")
        syn_gav = syn_man["unidentified"][0]["gav"]
        ok("synthetic jar copied and declared in the project",
           len(syn_man["unidentified"]) == 1 and Path(syn_man["unidentified"][0]["file"]).is_file()
           and syn_gav["groupId"] == "rebundle.unidentified" and syn_gav["artifactId"] == "vendor-sdk" and syn_gav["version"].startswith("2.0.0-u")
           and "<groupId>rebundle.unidentified</groupId>" in (syn_proj / "pom.xml").read_text(encoding="utf-8"))
        text = run("install_components.py", "--manifest", out / "bundle-manifest-syn.json", "--dry-run")
        ok("install-file command prepared for the synthetic jar", "install-file" in text and "-DgroupId=rebundle.unidentified" in text
           and "-DartifactId=vendor-sdk" in text and "-DgeneratePom=true" in text)

        # ---- duplicates end to end: the first copy in class-loading order wins ---------------
        print("[duplicates]")
        run("resolve_selection.py", "--inventory", out / "inventory.json", "--select", "dup-x\ndup-y", "--out", out / "sel-dup.json")
        dup_bundle = {"groupId": bundle["groupId"], "artifactId": "dup-bundle", "version": "1.0.0"}
        dup_proj = work / "bundle-dup"
        run("gen_bundle_project.py", "--selection", out / "sel-dup.json", "--source", out / "source.json", "--group-id", dup_bundle["groupId"],
            "--artifact-id", dup_bundle["artifactId"], "--version", dup_bundle["version"], "--out", dup_proj, "--manifest-out", out / "bundle-manifest-dup.json")
        dup_man = read_json(out / "bundle-manifest-dup.json")
        ok("duplicate providers ordered like the application's classpath", [c["file"] for c in dup_man["components"]] == ["dup-x-1.0.jar", "dup-y-1.0.jar"])
        dup_jars = [libs["dup-x"], libs["dup-y"]]
        dup_good = fx.simulate_shade(dup_proj, dup_jars, dup_bundle, dup_man["contents_attr"], source_attr, False, dup_proj / "target" / "dup-bundle-1.0.0.jar", boot_version="3.3.5")
        fx.write_reduced_pom(dup_proj / "target" / "dependency-reduced-pom.xml", dup_bundle, [])
        run("verify_bundle.py", "--manifest", out / "bundle-manifest-dup.json", "--out", out / "bv-dup.json")
        ok("bundle keeping the first copy verifies", read_json(out / "bv-dup.json")["ok"])
        dup_bad = fx.simulate_shade(dup_proj, list(reversed(dup_jars)), dup_bundle, dup_man["contents_attr"], source_attr, False, dup_proj / "target" / "dup-bad.jar", boot_version="3.3.5")
        run("verify_bundle.py", "--manifest", out / "bundle-manifest-dup.json", "--bundle", dup_bad, "--out", out / "bv-dup-bad.json", expect=1)
        ok("bundle keeping the wrong copy is rejected", "com/dup/Same.class" in next(c["detail"] for c in read_json(out / "bv-dup-bad.json")["checks"] if c["check"] == "classes-and-resources-identical"))
        new_dup = fx.build_new_app(app, dup_good, ["dup-x-1.0.jar", "dup-y-1.0.jar"], work / "new-dup.jar")
        run("verify_app_equivalence.py", "--old", app, "--new", new_dup, "--out", out / "eq-dup.json")
        ok("effective classpath unchanged when the winning copy is kept", read_json(out / "eq-dup.json")["ok"])
        new_dup_bad = fx.build_new_app(app, dup_bad, ["dup-x-1.0.jar", "dup-y-1.0.jar"], work / "new-dup-bad.jar")
        run("verify_app_equivalence.py", "--old", app, "--new", new_dup_bad, "--out", out / "eq-dup-bad.json")
        ok("effective classpath change reported when the shadowed copy wins",
           "com/dup/Same.class" in next(c["detail"] for c in read_json(out / "eq-dup-bad.json")["checks"] if c["check"] == "classpath-entries-identical"))
        syn_jar = fx.simulate_shade(syn_proj, [libs["vendor-sdk"]], syn_bundle, syn_man["contents_attr"], source_attr, False,
                                    syn_proj / "target" / "syn-bundle-1.0.0.jar", boot_version="3.3.5")
        fx.write_reduced_pom(syn_proj / "target" / "dependency-reduced-pom.xml", syn_bundle, [])
        run("verify_bundle.py", "--manifest", out / "bundle-manifest-syn.json", "--out", out / "bv-syn.json")
        ok("bundle with a synthetic component verifies", read_json(out / "bv-syn.json")["ok"])
        new_syn = fx.build_new_app(app, syn_jar, ["vendor-sdk-2.0.0.jar"], work / "new-syn.jar")
        run("verify_app_equivalence.py", "--old", app, "--new", new_syn, "--out", out / "eq-syn.json")
        eq_syn = read_json(out / "eq-syn.json")
        ok("equivalence matches the unidentified jar by sha256", eq_syn["ok"] and eq_syn["replaced"] == [gav_str(syn_gav)])
        new_syn_left = fx.build_new_app(app, syn_jar, ["vendor-sdk-2.0.0.jar"], work / "new-syn-left.jar", keep_replaced=True)
        run("verify_app_equivalence.py", "--old", app, "--new", new_syn_left, "--out", out / "eq-syn-left.json")
        ok("leftover unidentified jar detected by sha256", any(c["check"] == "original-coordinates-removed" and c["status"] == "diff" for c in read_json(out / "eq-syn-left.json")["checks"]))
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest-syn.json", "--tree", tree, "--out", out / "cp-syn.md", "--out-json", out / "cp-syn.json")
        cp_syn = read_json(out / "cp-syn.json")
        ok("consumer tree resolves the real coordinates of the synthetic jar",
           cp_syn["synthetic"][0]["resolved"] == "com.vendor:vendor-sdk" and "com.vendor:vendor-sdk" in cp_syn["banned"]
           and "<exclude>com.vendor:vendor-sdk</exclude>" in (out / "cp-syn.md").read_text(encoding="utf-8")
           and {m["module"]: m for m in cp_syn["modules"]}["com.example:app"]["remove_direct"].keys() == {"com.vendor:vendor-sdk"})
        run("gen_consumer_patch.py", "--manifest", out / "bundle-manifest-syn.json", "--out", out / "cp-syn-notree.md", "--out-json", out / "cp-syn-notree.json")
        ok("without a tree the proposal explains how to find the real coordinates",
           read_json(out / "cp-syn-notree.json")["synthetic"][0]["resolved"] is None
           and "artifactId `vendor-sdk`" in (out / "cp-syn-notree.md").read_text(encoding="utf-8"))

        # ---- verify pipeline: application equivalence ------------------------------------
        print("[verify app equivalence]")
        new_ok = fx.build_new_app(app, good, selected_jars, work / "new-ok.jar")
        run("verify_app_equivalence.py", "--old", app, "--new", new_ok, "--out", out / "equivalence.json")
        eq = read_json(out / "equivalence.json")
        ok("rebuilt app is equivalent", eq["ok"] and sorted(eq["replaced"])[0] == "com.example.codec:codec-a:1.0")
        ok("build-info / pom changes ignored, nothing to review",
           all(c["status"] in ("pass", "info") for c in eq["checks"]) and eq["findings"] == []
           and not (out / "equivalence-findings.txt").read_text(encoding="utf-8").strip())
        new_bad = fx.build_new_app(app, good, selected_jars, work / "new-leftover.jar", keep_replaced=True)
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-leftover.json")
        ok("original coordinates left on classpath reported as a dependency difference",
           any(c["check"] == "original-coordinates-removed" and c["status"] == "diff" and c["scope"] == "dependencies" for c in read_json(out / "eq-leftover.json")["checks"])
           and "[dependencies] original-coordinates-removed" in (out / "eq-leftover-findings.txt").read_text(encoding="utf-8"))
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--fail-on", "dependencies", "--out", out / "eq-leftover-gate.json", expect=1)
        ok("--fail-on dependencies gates it", not read_json(out / "eq-leftover-gate.json")["ok"])
        new_bad = fx.build_new_app(app, good, selected_jars, work / "new-class.jar", change_class=True)
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-class.json")
        eq_class = read_json(out / "eq-class.json")
        findings_txt = (out / "eq-class-findings.txt").read_text(encoding="utf-8")
        ok("changed application class is an application finding, listed for the pipeline",
           eq_class["ok"] and eq_class["findings"] == [{"check": "application-classes-unchanged", "scope": "application"}]
           and findings_txt.startswith("[info] source revision unknown") and "[application] application-classes-unchanged" in findings_txt
           and "Greeter.class" in findings_txt)
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--fail-on", "dependencies", "--out", out / "eq-class-deps.json")
        ok("--fail-on dependencies leaves application findings alone", read_json(out / "eq-class-deps.json")["ok"])
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--fail-on", "any", "--out", out / "eq-class-strict.json", expect=1)
        ok("--fail-on any turns the finding into a failure",
           any(c["check"] == "application-classes-unchanged" and c["status"] == "fail" for c in read_json(out / "eq-class-strict.json")["checks"]))
        new_bad = fx.build_new_app(app, good, selected_jars, work / "new-droplib.jar", drop_lib="snakeyaml-2.2.jar")
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-droplib.json")
        checks = {c["check"]: c for c in read_json(out / "eq-droplib.json")["checks"]}
        ok("lost unrelated dependency reported", checks["dependency-set-unchanged"]["status"] == "diff" and "snakeyaml" in checks["dependency-set-unchanged"]["detail"])
        bad_bundle = fx.simulate_shade(proj, jar_paths, bundle, man["contents_attr"], source_attr, True, target / "bad-for-app.jar", drop=["org/apache/commons/io/IOUtils.class"], boot_version="3.3.5")
        new_bad = fx.build_new_app(app, bad_bundle, selected_jars, work / "new-badbundle.jar")
        run("verify_app_equivalence.py", "--old", app, "--new", new_bad, "--out", out / "eq-badbundle.json")
        ok("missing class on new classpath reported", "IOUtils" in next(c["detail"] for c in read_json(out / "eq-badbundle.json")["checks"] if c["check"] == "classpath-entries-identical"))
        exploded = work / "new-exploded"
        with zipfile.ZipFile(new_ok) as zf:
            zf.extractall(exploded)
        run("verify_app_equivalence.py", "--old", app, "--new", exploded, "--out", out / "eq-exploded.json")
        ok("exploded layout supported", read_json(out / "eq-exploded.json")["ok"])

        # ---- differences outside the swap are warnings; a leaked third-party jar still fails ----
        print("[baseline differences]")
        stamped_old, stamped_new = fx.build_stamped_apps(work / "stamped", libs, good, selected_jars)
        run("verify_app_equivalence.py", "--old", stamped_old, "--new", stamped_new, "--out", out / "eq-stamped.json")
        eq_st = read_json(out / "eq-stamped.json")
        st = {c["check"]: c for c in eq_st["checks"]}
        ok("project modules rebuilt under the CI-stamped version are application findings, the dependency checks pass",
           eq_st["ok"] and st["project-version-differs"]["status"] == "diff" and st["project-version-differs"]["scope"] == "application"
           and "1.0.0-20240101 -> 1.0.0" in st["project-version-differs"]["detail"] and "app-common" in st["project-version-differs"]["detail"]
           and st["dependency-set-unchanged"]["status"] == "pass" and st["classpath-entries-identical"]["status"] == "pass"
           and st["kept-jars-byte-identical"]["status"] == "pass" and all(f["scope"] == "application" for f in eq_st["findings"]))
        ok("entries that embed the build version are marked, the archive signature is ignored",
           st["project-module-entries-changed"]["status"] == "diff" and "(1 embed the build version)" in st["project-module-entries-changed"]["detail"]
           and st["application-classes-unchanged"]["status"] == "diff" and "all embed the build version" in st["application-classes-unchanged"]["detail"]
           and st["application-resources-unchanged"]["status"] == "pass" and eq_st["project_version"] == {"old": "1.0.0-20240101", "new": "1.0.0"})
        ok("the source commits of both builds are read and compared",
           st["same-source-revision"]["status"] == "diff" and "1a2b3c4d5e6f" in st["same-source-revision"]["detail"] and "9f8e7d6" in st["same-source-revision"]["detail"]
           and eq_st["source_revision"]["same"] is False and eq_st["source_revision"]["old"]["branch"] == "main"
           and (out / "eq-stamped-findings.txt").read_text(encoding="utf-8").startswith("[application] same-source-revision")
           and eq["source_revision"]["same"] is None and any(c["check"] == "same-source-revision" and c["status"] == "info" for c in eq["checks"]))
        leaked = fx.build_new_app(app, good, selected_jars, work / "new-leaked.jar", extra_libs=[fx.leaked_jar(work / "stamped")],
                                  classpath_first=["tomcat-annotations-api-10.1.59.jar"])
        run("verify_app_equivalence.py", "--old", app, "--new", leaked, "--out", out / "eq-leaked.json")
        lk = {c["check"]: c for c in read_json(out / "eq-leaked.json")["checks"]}
        ok("a jar the old build never had is a dependency finding, with the changed entries grouped by provider",
           lk["dependency-set-unchanged"]["status"] == "diff" and "added: file:tomcat-annotations-api-10.1.59.jar" in lk["dependency-set-unchanged"]["detail"]
           and lk["classpath-entries-identical"]["status"] == "diff" and "-> tomcat-annotations-api-10.1.59.jar: 1 changed" in lk["classpath-entries-identical"]["detail"]
           and lk["classpath-entries-identical"]["items"][0]["path"] == "org/apache/commons/logging/LogFactory.class"
           and [f["scope"] for f in read_json(out / "eq-leaked.json")["findings"]] == ["dependencies", "dependencies", "dependencies"])
        # the consumer moved on: the old build carried an embedded coordinate at another version
        libs_old = dict(libs)
        libs_old["commons-io"] = fx.dep_jar(work / "older", "commons-io", "commons-io", "2.15.0",
                                            ["org/apache/commons/io/IOUtils.class", "org/apache/commons/io/FileUtils.class", "org/apache/commons/io/input/BOMInputStream.class"],
                                            extra={"org/apache/commons/io/messages.properties": b"hello=world\n"})
        older = fx.build_app(work / "older" / "app.jar", libs_old)
        moved = fx.build_new_app(older, good, [n if n != "commons-io-2.16.1.jar" else "commons-io-2.15.0.jar" for n in selected_jars], work / "new-moved.jar")
        run("verify_app_equivalence.py", "--old", older, "--new", moved, "--out", out / "eq-moved.json")
        mv = {c["check"]: c for c in read_json(out / "eq-moved.json")["checks"]}
        ok("an embedded coordinate at another version in the old build is reported once, with its changed entries",
           mv["embedded-coordinates-match-old-build"]["status"] == "diff"
           and "commons-io:commons-io 2.15.0 in the old build, 2.16.1 in the bundle" in mv["embedded-coordinates-match-old-build"]["detail"]
           and mv["dependency-set-unchanged"]["status"] == "pass"
           and mv["classpath-entries-identical"]["status"] == "diff" and "commons-io-2.15.0.jar -> thirdparty-bundle-1.0.0.jar: 3 changed" in mv["classpath-entries-identical"]["detail"])

        # ---- source detection and the fake Nexus -----------------------------------------
        print("[source detection]")
        import extract_source as es
        ok("ecr region derived from the registry host",
           es.ecr_region("123456789012.dkr.ecr.eu-west-1.amazonaws.com") == "eu-west-1"
           and es.ecr_region("123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn") == "cn-north-1"
           and es.ecr_region("123456789012.dkr.ecr-fips.us-gov-west-1.amazonaws.com") == "us-gov-west-1"
           and es.ecr_region("nexus-docker.example.com", "ap-southeast-1") == "ap-southeast-1"
           and es.ecr_region("nexus-docker.example.com") is None)
        ok("registry host of an image reference",
           es.registry_host("123456789012.dkr.ecr.eu-west-1.amazonaws.com/team/app:1.2.3") == "123456789012.dkr.ecr.eu-west-1.amazonaws.com"
           and es.registry_host("nexus.example.com:5000/team/app@sha256:abc") == "nexus.example.com:5000"
           and es.registry_host("library/nginx:1.25") == "index.docker.io")
        ok("snapshot version resolution",
           es.resolve_snapshot_version(SNAPSHOT_METADATA, "1.1.0-SNAPSHOT", "jar", None) == "1.1.0-20241001.101010-3"
           and es.resolve_snapshot_version(SNAPSHOT_METADATA, "1.1.0-SNAPSHOT", "jar", "exec") == "1.1.0-20241001.101010-4"
           and es.resolve_snapshot_version(SNAPSHOT_METADATA, "1.1.0-SNAPSHOT", "war", None) == "1.1.0-20241001.101010-3"
           and es.resolve_snapshot_version("<metadata><versioning><snapshot><localCopy>true</localCopy></snapshot></versioning></metadata>",
                                           "1.1.0-SNAPSHOT", "jar", None) == "1.1.0-SNAPSHOT")
        ok("maven artifact url follows g:a:v[:packaging[:classifier]]",
           es.maven_artifact_url("com.example:app:1.2.3:jar:exec", "https://nexus.example.com/repository/maven-public")
           == ("https://nexus.example.com/repository/maven-public/com/example/app/1.2.3/app-1.2.3-exec.jar", "app-1.2.3-exec.jar", "1.2.3")
           and es.maven_artifact_url("com.example:app:1.2.3:war", "https://nexus.example.com/repository/maven-public")[1] == "app-1.2.3.war"
           and es.maven_artifact_url("com.example:app:1.2.3", "https://nexus.example.com/repository/maven-public")[1] == "app-1.2.3.jar")
        nx = es.parse_hosts("nexus-docker.example.com:5000, https://nexus.example.com/repository/maven-public/, ,")
        ok("nexus hosts parsed from hosts and urls", nx == {"nexus-docker.example.com:5000", "nexus.example.com"})
        ok("detection: urls",
           es.detect_source("https://nexus.example.com/repository/raw/app.jar", nx)[:2] == ("url", "nexus")
           and es.detect_source("https://files.example.org/app.jar", nx)[:2] == ("url", "none"))
        ok("detection: local paths",
           es.detect_source(str(app), nx)[:2] == ("file", "none") and es.detect_source(str(app.parent), nx)[:2] == ("dir", "none"))
        ok("detection: maven coordinates",
           es.detect_source("com.example:app:1.2.3", nx)[:2] == ("maven", "nexus")
           and es.detect_source("com.example:app:1.2.3:jar:exec", nx)[:2] == ("maven", "nexus"))
        ok("detection: images by registry host",
           es.detect_source("123456789012.dkr.ecr.eu-west-1.amazonaws.com/team/app:1.2.3", nx) == ("image", "ecr", "123456789012.dkr.ecr.eu-west-1.amazonaws.com")
           and es.detect_source("public.ecr.aws/team/app:1", nx)[:2] == ("image", "ecr")
           and es.detect_source("nexus-docker.example.com:5000/team/app:1.2.3", nx) == ("image", "nexus", "nexus-docker.example.com:5000")
           and es.detect_source("ghcr.io/team/app:1.2.3", nx) == ("image", "none", "ghcr.io")
           and es.detect_source("nginx:1.25", nx) == ("image", "none", "index.docker.io"))
        ok("reference#path splits only image references",
           es.split_ref("nexus-docker.example.com/team/app:1.2.3#/workspace") == ("nexus-docker.example.com/team/app:1.2.3", "/workspace")
           and es.split_ref("https://files.example.org/app.jar#frag") == ("https://files.example.org/app.jar#frag", None)
           and es.split_ref(str(app)) == (str(app), None))
        detected = run("extract_source.py", "--detect", "--ref", "nexus-docker.example.com:5000/team/app:1.2.3#/workspace",
                       "--nexus-hosts", "nexus-docker.example.com:5000,https://nexus.example.com/repository/maven-public/").strip()
        ok("--detect prints kind, auth and host", detected == "image nexus nexus-docker.example.com:5000")
        run("extract_source.py", "--ref", "123456789012.dkr.ecr.eu-west-1.amazonaws.com/team/app:1", "--out", work / "ecr", "--meta", out / "ecr.json", expect=2)
        ok("ecr image without docker, crane or aws fails cleanly", not (out / "ecr.json").exists())

        run("extract_source.py", "--ref", "com.example:app:1.0.0", "--maven-repo-url", repo_url,
            "--out", work / "mvn-rel", "--meta", out / "mvn-rel.json", env=with_creds)
        rel_meta = read_json(out / "mvn-rel.json")
        ok("maven coordinates detected and downloaded with basic auth",
           rel_meta["type"] == "maven" and rel_meta["detected"] and rel_meta["sha256"] == src["sha256"]
           and rel_meta["auth"] == "nexus" and rel_meta["maven"]["resolved_version"] == "1.0.0")
        run("extract_source.py", "--ref", "com.example:app:1.1.0-SNAPSHOT", "--maven-repo-url", repo_url,
            "--out", work / "mvn-snap", "--meta", out / "mvn-snap.json", env=with_creds)
        snap_meta = read_json(out / "mvn-snap.json")
        ok("maven snapshot coordinates resolved through maven-metadata.xml",
           snap_meta["maven"]["resolved_version"] == "1.1.0-20241001.101010-3"
           and Path(snap_meta["local_path"]).name == "app-1.1.0-20241001.101010-3.jar" and snap_meta["sha256"] == src["sha256"])
        run("extract_source.py", "--ref", "com.example:app:1.0.0", "--maven-repo-url", repo_url,
            "--out", work / "mvn-anon", "--meta", out / "mvn-anon.json", env=without_creds, expect=2)
        ok("maven coordinates without credentials are rejected by the repository", not (out / "mvn-anon.json").exists())
        url_ref = repo_url + "com/example/app/1.0.0/app-1.0.0.jar"
        run("extract_source.py", "--ref", url_ref, "--nexus-hosts", repo_url, "--out", work / "url-nexus", "--meta", out / "url-nexus.json", env=with_creds)
        ok("url on a nexus host downloaded with basic auth", read_json(out / "url-nexus.json")["auth"] == "nexus" and read_json(out / "url-nexus.json")["sha256"] == src["sha256"])
        run("extract_source.py", "--ref", url_ref, "--out", work / "url-anon", "--meta", out / "url-anon.json", env=with_creds, expect=2)
        ok("url on an unknown host never sends the nexus credentials", not (out / "url-anon.json").exists())
        run("extract_source.py", "--type", "maven", "--ref", "com.example:app", "--maven-repo-url", repo_url,
            "--out", work / "mvn-bad", "--meta", out / "mvn-bad.json", env=with_creds, expect=2)
        ok("malformed maven coordinates fail", not (out / "mvn-bad.json").exists())

        # ---- extract job: the application archive as one build artifact -------------------------
        print("[export app]")
        run("export_app.py", "--source", out / "source.json", "--out-dir", out / "artifact", "--meta", out / "app-artifact.json")
        exp = read_json(out / "app-artifact.json")
        ok("a jar source is copied byte for byte with its checksum",
           exp["name"] == "app.jar" and exp["kind"] == "copy" and exp["executable"] and exp["layout"] == "file" and exp["sha256"] == src["sha256"]
           and (out / "artifact" / "app.jar").read_bytes() == app.read_bytes()
           and (out / "artifact" / "app.jar.sha256").read_text(encoding="utf-8").split() == [src["sha256"], "app.jar"]
           and exp["source"]["ref"] == src["ref"])
        run("export_app.py", "--source", out / "source.json", "--out-dir", out / "artifact-named", "--name", "orders-1.2.3.jar", "--meta", out / "app-artifact-named.json")
        ok("--name renames the artifact", (out / "artifact-named" / "orders-1.2.3.jar").is_file() and read_json(out / "app-artifact-named.json")["name"] == "orders-1.2.3.jar")
        run("export_app.py", "--source", out / "source.json", "--out-dir", out / "artifact-badname", "--name", "../x.jar", "--meta", out / "app-artifact-badname.json", expect=2)
        ok("a name with a path is refused", not (out / "app-artifact-badname.json").exists())
        image_meta = dict(src, type="image", ref="nexus-docker.example.com:5000/team/orders:1.2.3", app_path="/app/app.jar", digest="sha256:" + "ab" * 32)
        write_json(out / "image-source.json", image_meta)
        run("export_app.py", "--source", out / "image-source.json", "--out-dir", out / "artifact-image", "--meta", out / "app-artifact-image.json")
        ok("an image artifact is named after the repository and tag", read_json(out / "app-artifact-image.json")["name"] == "orders-1.2.3.jar")
        write_json(out / "image-digest-source.json", dict(image_meta, ref="nexus-docker.example.com:5000/team/orders@sha256:" + "ab" * 32))
        run("export_app.py", "--source", out / "image-digest-source.json", "--out-dir", out / "artifact-digest", "--meta", out / "app-artifact-digest.json")
        ok("without a tag the digest prefix names it", read_json(out / "app-artifact-digest.json")["name"] == "orders-abababababab.jar")
        # an exploded Boot layout (a buildpacks image's /workspace) is packed back into an executable jar
        exploded = work / "exploded" / "workspace"
        with zipfile.ZipFile(app) as zf:
            zf.extractall(exploded)
        write_json(out / "exploded-source.json", dict(image_meta, ref="nexus-docker.example.com:5000/team/orders:2.0.0", app_path="/workspace", local_path=str(exploded), sha256=None))
        run("export_app.py", "--source", out / "exploded-source.json", "--out-dir", out / "artifact-exploded", "--meta", out / "app-artifact-exploded.json")
        rexp = read_json(out / "app-artifact-exploded.json")
        repacked = out / "artifact-exploded" / "orders-2.0.0.jar"
        with zipfile.ZipFile(repacked) as zf, zipfile.ZipFile(app) as orig:
            infos = zf.infolist()
            same = {n: zf.read(n) for n in zf.namelist()} == {n: orig.read(n) for n in orig.namelist() if not n.endswith("/")}
            stored = all(i.compress_type == zipfile.ZIP_STORED for i in infos if i.filename.startswith("BOOT-INF/lib/"))
            deflated = any(i.compress_type == zipfile.ZIP_DEFLATED for i in infos if i.filename.startswith("BOOT-INF/classes/"))
        ok("exploded Boot layout repacked: same entries, manifest first, nested jars stored, classes deflated",
           rexp["kind"] == "repack" and rexp["executable"] and rexp["layout"] == "boot-exploded" and infos[0].filename == "META-INF/MANIFEST.MF"
           and same and stored and deflated and rexp["nested_jars_stored"] == len(libs) and "stored without compression" in rexp["note"])
        repacked_app = AppArtifact(repacked)
        ok("the repacked jar reads as a Boot jar with every dependency", repacked_app.layout == "boot-jar" and len(repacked_app.dependency_jars()) == len(libs)
           and repacked_app.manifest().get("Spring-Boot-Version") == "3.3.5")
        repacked_app.close()
        # a Jib layout has no archive to repack: it is zipped and marked as not executable
        jib = work / "jib" / "app"
        (jib / "libs").mkdir(parents=True)
        (jib / "classes" / "com").mkdir(parents=True)
        shutil.copy(libs["commons-io"], jib / "libs" / libs["commons-io"].name)
        (jib / "classes" / "com" / "App.class").write_bytes(fx.fake_class("App"))
        (jib / "jib-classpath-file").write_text("/app/resources:/app/classes:/app/libs/*", encoding="utf-8")
        write_json(out / "jib-source.json", dict(image_meta, ref="registry.example.com/team/orders:3.0.0", app_path="/app", local_path=str(jib), sha256=None))
        run("export_app.py", "--source", out / "jib-source.json", "--out-dir", out / "artifact-jib", "--meta", out / "app-artifact-jib.json")
        jexp = read_json(out / "app-artifact-jib.json")
        with zipfile.ZipFile(out / "artifact-jib" / "orders-3.0.0.zip") as zf:
            jib_names = set(zf.namelist())
        ok("a Jib layout is zipped and marked as not executable",
           jexp["kind"] == "zip" and not jexp["executable"] and jexp["layout"] == "lib-dir"
           and jib_names == {"libs/commons-io-2.16.1.jar", "classes/com/App.class", "jib-classpath-file"} and "not an executable jar" in jexp["note"])
        notjar = work / "notjar.bin"
        notjar.write_bytes(b"\x7fELF not a jar")
        write_json(out / "notjar-source.json", dict(src, local_path=str(notjar), sha256=None))
        run("export_app.py", "--source", out / "notjar-source.json", "--out-dir", out / "artifact-notjar", "--meta", out / "app-artifact-notjar.json", expect=2)
        ok("a file that is not an archive is refused", not (out / "app-artifact-notjar.json").exists())

        # ---- summary + settings -----------------------------------------------------------
        print("[summary]")
        run("write_summary.py", "--out-dir", out)
        summary = (out / "SUMMARY.md").read_text(encoding="utf-8")
        ok("summary written", (out / "SUMMARY.md").is_file() and "Bundle verification: passed" in summary)
        ok("summary describes the exported artifact", "## Application artifact" in summary and "- File: `app.jar`" in summary
           and "- Content: the archive as it was fetched, byte for byte" in summary)
        ok("summary describes the rework", "## Rework of `com.company.platform:thirdparty-bundle:1.0.0`" in summary
           and "- New coordinates: `com.company.platform:thirdparty-bundle:1.0.0.1` (version derived)" in summary
           and "`hotfix.jar` (upload): add as `rebundle.unidentified:hotfix:" in summary and "replaces 1 entry of the bundle: `org/apache/commons/io/FileUtils.class`" in summary)
        run("gen_maven_settings.py", "--mirror-url", "https://nexus.example.com/repository/maven-public/", "--out", work / "settings.xml")
        ok("settings with mirror", "<mirrorOf>*</mirrorOf>" in (work / "settings.xml").read_text(encoding="utf-8"))
        run("gen_maven_settings.py", "--out", work / "settings-plain.xml")
        ok("settings without mirror", "<mirrors>" not in (work / "settings-plain.xml").read_text(encoding="utf-8"))
    finally:
        server.shutdown()

    print("\nSELFTEST PASSED: %d assertions (workdir %s)" % (passed, work))


if __name__ == "__main__":
    main()
