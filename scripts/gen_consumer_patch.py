#!/usr/bin/env python3
"""Stage 7: proposal for the consumer project's POM change (never applied automatically).

With one or more `mvn dependency:tree -Dverbose -DoutputType=text` outputs of the consumer
project the proposal is exact: which direct dependencies to delete and which direct
dependencies need an <exclusion> because they transitively bring an embedded coordinate.
Without a tree it explains how to produce one and gives the generic snippets.

The Enforcer rule in the proposal is the real gate: the consumer build fails whenever an
embedded coordinate sneaks back through any path, so incomplete exclusions cannot go
unnoticed.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, ga_str, gav_str, log, parse_dependency_trees, parse_selection_text, read_json, write_json  # noqa: E402

ENFORCER = """<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-enforcer-plugin</artifactId>
  <version>3.5.0</version>
  <executions>
    <execution>
      <id>ban-bundled-coordinates</id>
      <goals>
        <goal>enforce</goal>
      </goals>
      <configuration>
        <rules>
          <bannedDependencies>
            <excludes>
%s
            </excludes>
            <message>These coordinates are embedded in %s. Add an exclusion on the dependency that reintroduces them (mvn dependency:tree -Dverbose -Dincludes=%s).</message>
          </bannedDependencies>
        </rules>
      </configuration>
    </execution>
  </executions>
</plugin>"""


def local_tag(el) -> str:
    return el.tag.split("}", 1)[-1]


def reduced_pom_dependencies(path: Path):
    deps = []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return deps
    for el in root:
        if local_tag(el) == "dependencies":
            for dep in el:
                if local_tag(dep) == "dependency":
                    deps.append({local_tag(c): (c.text or "").strip() for c in dep})
    return deps


def analyse_trees(texts, embedded_ga):
    modules = []
    for text in texts:
        for tree in parse_dependency_trees(text):
            root = tree["root"]
            module = {"module": ga_str(root), "remove_direct": OrderedDict(), "exclusions": OrderedDict(), "paths": []}
            for node in tree["nodes"]:
                ga = ga_str(node)
                if ga not in embedded_ga:
                    continue
                path = node["path"]
                module["paths"].append(" -> ".join(path[1:]) + (" (%s)" % node["note"] if node.get("note") else ""))
                if node["depth"] == 1:
                    module["remove_direct"].setdefault(ga, {"scope": node.get("scope"), "version": node.get("version")})
                else:
                    direct = path[1]
                    entry = module["exclusions"].setdefault(direct, {"version": None, "targets": []})
                    if ga not in entry["targets"]:
                        entry["targets"].append(ga)
            # version of the direct dependencies that need exclusions
            for node in tree["nodes"]:
                if node["depth"] == 1 and ga_str(node) in module["exclusions"]:
                    module["exclusions"][ga_str(node)]["version"] = node.get("version")
            modules.append(module)  # untouched modules are kept so the report shows them
    return modules


def dependency_snippet(bundle: dict) -> str:
    return "\n".join([
        "<dependency>",
        "  <groupId>%s</groupId>" % bundle["groupId"],
        "  <artifactId>%s</artifactId>" % bundle["artifactId"],
        "  <version>%s</version>" % bundle["version"],
        "</dependency>",
    ])


def exclusion_snippet(direct_ga: str, targets, version) -> str:
    g, a = direct_ga.split(":", 1)
    lines = ["<dependency>", "  <groupId>%s</groupId>" % g, "  <artifactId>%s</artifactId>" % a]
    if version:
        lines.append("  <!-- keep your existing version / managed version -->")
    lines.append("  <exclusions>")
    for t in targets:
        tg, ta = t.split(":", 1)
        lines += ["    <exclusion>", "      <groupId>%s</groupId>" % tg, "      <artifactId>%s</artifactId>" % ta, "    </exclusion>"]
    lines += ["  </exclusions>", "</dependency>"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json", help="bundle manifest written by gen_bundle_project.py")
    ap.add_argument("--bundle", default="", help="instead of --manifest: groupId:artifactId:version of an existing bundle")
    ap.add_argument("--embedded", default="", help="with --bundle: embedded coordinates g:a:v, comma or newline separated")
    ap.add_argument("--embedded-file", default="", help="with --bundle: file holding the embedded coordinates")
    ap.add_argument("--tree", action="append", default=[], help="dependency:tree text output (repeatable)")
    ap.add_argument("--reduced-pom", help="dependency-reduced POM of the bundle (default from manifest)")
    ap.add_argument("--out", default="out/consumer-patch.md")
    ap.add_argument("--out-json", default="out/consumer-patch.json")
    args = ap.parse_args()

    if args.bundle:
        parts = args.bundle.split(":")
        if len(parts) != 3:
            fail("--bundle must be groupId:artifactId:version")
        text = args.embedded or ""
        if args.embedded_file:
            text += "\n" + Path(args.embedded_file).read_text(encoding="utf-8")
        components = []
        for tok in parse_selection_text(text):
            p = tok.split(":")
            if len(p) < 3:
                fail("embedded coordinate '%s' must be groupId:artifactId:version" % tok)
            gav = {"groupId": p[0], "artifactId": p[1], "version": p[2]}
            if len(p) > 3 and p[3]:
                gav["classifier"] = p[3]
            components.append({"gav": gav})
        if not components:
            fail("--embedded (or --embedded-file) is required with --bundle")
        man = {"bundle": {"groupId": parts[0], "artifactId": parts[1], "version": parts[2]},
               "coordinate": args.bundle, "components": components, "reduced_pom": ""}
    else:
        man = read_json(args.manifest)
    bundle = man["bundle"]
    coord = man["coordinate"]
    embedded = [c["gav"] for c in man["components"]]
    embedded_ga = {ga_str(g) for g in embedded}
    reduced_pom = Path(args.reduced_pom or man.get("reduced_pom", ""))
    promoted = [d for d in reduced_pom_dependencies(reduced_pom) if "%s:%s" % (d.get("groupId"), d.get("artifactId")) not in embedded_ga] if reduced_pom.is_file() else []

    texts = []
    for t in args.tree:
        p = Path(t)
        if p.is_file():
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
        else:
            log("WARN: tree file not found: %s" % t)
    modules = analyse_trees(texts, embedded_ga) if texts else []

    includes_arg = ",".join(sorted(embedded_ga))
    md = []
    md.append("# Consumer POM change proposal: %s" % coord)
    md.append("")
    md.append("Coordinates embedded in the bundle: %s" % ", ".join(gav_str(g) for g in embedded))
    md.append("")
    md.append("No Java code changes; package names stay the same. Three things to do: delete the replaced direct dependencies, add the bundle dependency, and add an exclusion on every path that still brings an original coordinate transitively. The Enforcer rule below fails the build whenever a path is missed.")
    md.append("")
    md.append("## 1. Add the bundle dependency")
    md.append("")
    md.append("```xml\n%s\n```" % dependency_snippet(bundle))
    md.append("")

    if modules:
        md.append("## 2. Changes derived from the dependency tree")
        md.append("")
        for m in modules:
            md.append("### Module %s" % m["module"])
            md.append("")
            if not m["paths"]:
                md.append("No embedded coordinate appears in this module's dependency tree; nothing to change.")
                md.append("")
                continue
            if m["remove_direct"]:
                md.append("Delete these direct dependencies:")
                md.append("")
                for ga, info in m["remove_direct"].items():
                    md.append("- `%s` (version %s, scope %s)" % (ga, info.get("version"), info.get("scope") or "compile"))
                md.append("")
            if m["exclusions"]:
                md.append("Add exclusions on these direct dependencies:")
                md.append("")
                for direct, info in m["exclusions"].items():
                    md.append("```xml\n%s\n```" % exclusion_snippet(direct, info["targets"], info.get("version")))
                    md.append("")
            md.append("<details><summary>Paths found in the dependency tree (%d)</summary>\n" % len(m["paths"]))
            for p in m["paths"]:
                md.append("- %s" % p)
            md.append("\n</details>")
            md.append("")
    else:
        md.append("## 2. Producing the exact change list")
        md.append("")
        md.append("No dependency tree of the consumer project was provided, so exclusions cannot be listed per path. Run this in the root of the consumer project (a multi-module build appends every module to the same file):")
        md.append("")
        md.append("```bash\nmvn -B org.apache.maven.plugins:maven-dependency-plugin:3.7.1:tree -Dverbose -DoutputType=text -DoutputFile=\"$PWD/dependency-tree.txt\" -DappendOutput=true\n```")
        md.append("")
        md.append("Then regenerate this proposal:")
        md.append("")
        md.append("```bash\npython3 scripts/gen_consumer_patch.py --manifest out/bundle-manifest.json --tree dependency-tree.txt\n```")
        md.append("")
        md.append("Or inspect every path that brings an original coordinate by hand:")
        md.append("")
        md.append("```bash\nmvn dependency:tree -Dverbose -Dincludes=%s\n```" % includes_arg)
        md.append("")

    md.append("## 3. Enforcer gate (put it in the parent POM's build/plugins so every module inherits it)")
    md.append("")
    md.append("```xml\n%s\n```" % (ENFORCER % ("\n".join("              <exclude>%s</exclude>" % ga for ga in sorted(embedded_ga)), coord, includes_arg)))
    md.append("")
    md.append("## 4. Transitive dependencies the bundle POM still declares")
    md.append("")
    if promoted:
        md.append("These are not embedded; consumers keep getting them transitively through the bundle. Their versions follow the consumer's dependencyManagement (for example the Spring Boot BOM):")
        md.append("")
        for d in promoted:
            md.append("- `%s:%s:%s`%s" % (d.get("groupId"), d.get("artifactId"), d.get("version"), (" (scope %s)" % d["scope"]) if d.get("scope") else ""))
    else:
        md.append("None (or the bundle was not built yet and the dependency-reduced POM does not exist).")
    md.append("")
    md.append("## 5. Verification")
    md.append("")
    md.append("1. Consumer `mvn -B clean verify`: a passing Enforcer check means no path brings the original coordinates back.")
    md.append("2. Run Jenkinsfile.verify on the app.jar or image built before and after the change: every class and resource on the dependency classpath must be byte-identical, with only the replaced jars gone and the bundle added.")
    md.append("3. Run the existing tests, rebuild the image, smoke-test it.")
    md.append("")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(md), encoding="utf-8")
    write_json(args.out_json, {
        "bundle": bundle,
        "coordinate": coord,
        "embedded": embedded,
        "modules": [{"module": m["module"], "remove_direct": m["remove_direct"], "exclusions": m["exclusions"], "paths": m["paths"]} for m in modules],
        "promoted_dependencies": promoted,
        "tree_provided": bool(texts),
    })
    log("Consumer patch proposal written to %s" % args.out)
    for m in modules:
        log("  %s: remove %d direct, exclusions on %d direct dependencies, %d paths" % (
            m["module"], len(m["remove_direct"]), len(m["exclusions"]), len(m["paths"])))


if __name__ == "__main__":
    main()
