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
from bundlelib import gav_str, read_json, write_json  # noqa: E402

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
           and sel["overlaps"]["conflicts"] == [{"path": "com/dup/Same.class", "winner": "dup-x-1.0.jar", "shadowed": ["dup-y-1.0.jar"]}]
           and any(f["code"] == "overlap-resolved" and "dup-x-1.0.jar shadows dup-y-1.0.jar" in f["message"] for f in sel["findings"])
           and [d["name"] for d in sel["selected"]] == [d["name"] for d in inv["dependencies"]])
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
           and sel["transformers"]["append"] == ["META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"]
           and sel["transformers"]["multi_release"] is True)

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
        ok("legal files relocated", (proj / "src/main/resources/META-INF/third-party/commons-io-2.16.1/LICENSE.txt").is_file()
           and (proj / "src/main/resources/META-INF/third-party/BUNDLE-CONTENTS.txt").is_file())
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
        run("apply_consumer_patch.py", "--patch", out / "consumer-patch-io.json", "--project", cons, "--out", out / "apply-notree.json", "--diff", out / "apply-notree.diff", expect=2)
        ok("a proposal without a tree cannot be applied", not (out / "apply-notree.json").exists())
        (work / "consumer" / "app" / "target").mkdir(parents=True, exist_ok=True)
        rebuilt = fx.build_new_app(app, good, selected_jars, work / "consumer" / "app" / "target" / "app-1.0.0.jar")
        (work / "consumer" / "app" / "target" / "app-1.0.0.jar.original").write_bytes(b"not a jar")
        run("find_app_jar.py", "--project", cons, "--manifest", out / "bundle-manifest.json", "--out", out / "rebuilt-app.json")
        found = read_json(out / "rebuilt-app.json")
        ok("rebuilt boot archive located and the original artifact remembered",
           Path(found["new_jar"]) == rebuilt and found["old_ref"] == str(app) and found["old_type"] == "file")

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

        # ---- summary + settings -----------------------------------------------------------
        print("[summary]")
        run("write_summary.py", "--out-dir", out)
        ok("summary written", (out / "SUMMARY.md").is_file() and "Bundle verification: passed" in (out / "SUMMARY.md").read_text(encoding="utf-8"))
        run("gen_maven_settings.py", "--mirror-url", "https://nexus.example.com/repository/maven-public/", "--out", work / "settings.xml")
        ok("settings with mirror", "<mirrorOf>*</mirrorOf>" in (work / "settings.xml").read_text(encoding="utf-8"))
        run("gen_maven_settings.py", "--out", work / "settings-plain.xml")
        ok("settings without mirror", "<mirrors>" not in (work / "settings-plain.xml").read_text(encoding="utf-8"))
    finally:
        server.shutdown()

    print("\nSELFTEST PASSED: %d assertions (workdir %s)" % (passed, work))


if __name__ == "__main__":
    main()
