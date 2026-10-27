#!/usr/bin/env python3
"""Stage 7a: the build plugins of the consumer project, and a reactor that exposes their dependency trees.

`mvn help:effective-pom -Doutput=FILE` lists, per module, every plugin the build uses: the declared ones
and the ones the lifecycle binds by default (compiler, surefire, jar, resources...), each with the
dependencies the POMs add to it (<plugin><dependencies>, pluginManagement merged in). This script reads
that file and writes a throwaway Maven reactor with one module per distinct plugin configuration. Each
module depends on the added dependencies first and on the plugin artifact last, which is how Maven builds
the plugin's class realm: the added dependencies dominate the plugin's own, and the plugin's provided and
test dependencies stay out. `mvn dependency:tree -Dverbose` on that reactor therefore shows one realm per
module; the output feeds gen_consumer_patch.py --plugin-tree together with the JSON written here.

    plugin_trees.py --effective-pom out/consumer-effective-pom.xml --reactor work/plugin-trees \\
        --plugins-out out/consumer-plugins.json
    mvn -B -f work/plugin-trees/pom.xml dependency:tree -Dverbose -DoutputType=text \\
        -DoutputFile="$PWD/out/consumer-plugin-trees.txt" -DappendOutput=true

Ties between the added dependencies and the plugin's own ones are resolved by depth in the reactor module
(the added ones win), which matches Maven's realm resolution except for a tie between a transitive
dependency of an added one and a direct dependency of the plugin: rare, and only the version differs.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, write_json  # noqa: E402

REACTOR_GROUP = "rebundle.plugins"
REACTOR_ARTIFACT = "plugin-trees"
DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"


def local_tag(el) -> str:
    return el.tag.split("}", 1)[-1]


def child(el, name: str):
    for c in el:
        if local_tag(c) == name:
            return c
    return None


def children(el, name: str):
    return [c for c in (el if el is not None else []) if local_tag(c) == name]


def text(el, name: str) -> Optional[str]:
    c = child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None and c.text and c.text.strip() else None


def xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def project_elements(root):
    """The <project> elements of an effective POM: one, or several under <projects> for a reactor."""
    if local_tag(root) == "project":
        return [root]
    return children(root, "project")


def read_dependency(dep) -> Optional[dict]:
    d = {"groupId": text(dep, "groupId"), "artifactId": text(dep, "artifactId"), "version": text(dep, "version"),
         "type": text(dep, "type") or "jar", "classifier": text(dep, "classifier"), "scope": text(dep, "scope"),
         "exclusions": []}
    if not d["groupId"] or not d["artifactId"]:
        return None
    for ex in children(child(dep, "exclusions"), "exclusion"):
        d["exclusions"].append({"groupId": text(ex, "groupId") or "", "artifactId": text(ex, "artifactId") or ""})
    return d


def read_plugins(effective_pom: Path):
    """Per module, the plugins of its effective build with the dependencies the POMs add to them."""
    try:
        root = ET.parse(str(effective_pom)).getroot()
    except (ET.ParseError, OSError) as e:
        fail("cannot parse %s: %s" % (effective_pom, e))
    modules = OrderedDict()
    skipped = []
    for project in project_elements(root):
        parent = child(project, "parent")
        group = text(project, "groupId") or (text(parent, "groupId") if parent is not None else None)
        module = "%s:%s" % (group, text(project, "artifactId"))
        plugins = []
        build = child(project, "build")
        for p in children(child(build, "plugins") if build is not None else None, "plugin"):
            plugin = {"groupId": text(p, "groupId") or DEFAULT_PLUGIN_GROUP, "artifactId": text(p, "artifactId"), "version": text(p, "version")}
            if not plugin["artifactId"]:
                continue
            if not plugin["version"]:
                skipped.append({"module": module, "plugin": "%s:%s" % (plugin["groupId"], plugin["artifactId"]),
                                "reason": "no version in the effective POM; declare one to have its dependency tree checked"})
                continue
            deps = []
            for dep in children(child(p, "dependencies"), "dependency"):
                d = read_dependency(dep)
                if d is None:
                    continue
                if not d["version"] or d["scope"] == "system":
                    skipped.append({"module": module, "plugin": gav(plugin), "reason": "its dependency %s:%s has %s; not modelled" % (
                        d["groupId"], d["artifactId"], "no version" if not d["version"] else "system scope")})
                    continue
                deps.append(d)
            plugins.append({"plugin": plugin, "dependencies": deps})
        modules[module] = plugins
    return modules, skipped


def gav(c: dict) -> str:
    return "%s:%s:%s" % (c["groupId"], c["artifactId"], c["version"])


def config_key(entry: dict) -> tuple:
    deps = tuple((d["groupId"], d["artifactId"], d["version"], d["type"], d["classifier"] or "",
                  tuple(sorted("%s:%s" % (e["groupId"], e["artifactId"]) for e in d["exclusions"]))) for d in entry["dependencies"])
    return (gav(entry["plugin"]), deps)


def dependency_xml(d: dict, indent: str) -> List[str]:
    lines = [indent + "<dependency>", indent + "  <groupId>%s</groupId>" % xml_escape(d["groupId"]),
             indent + "  <artifactId>%s</artifactId>" % xml_escape(d["artifactId"]), indent + "  <version>%s</version>" % xml_escape(d["version"])]
    if d.get("type") and d["type"] != "jar":
        lines.append(indent + "  <type>%s</type>" % xml_escape(d["type"]))
    if d.get("classifier"):
        lines.append(indent + "  <classifier>%s</classifier>" % xml_escape(d["classifier"]))
    if d.get("exclusions"):
        lines.append(indent + "  <exclusions>")
        for e in d["exclusions"]:
            lines += [indent + "    <exclusion>", indent + "      <groupId>%s</groupId>" % xml_escape(e["groupId"]),
                      indent + "      <artifactId>%s</artifactId>" % xml_escape(e["artifactId"]), indent + "    </exclusion>"]
        lines.append(indent + "  </exclusions>")
    lines.append(indent + "</dependency>")
    return lines


def module_pom(entry: dict) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<project xmlns="http://maven.apache.org/POM/4.0.0">',
             "  <modelVersion>4.0.0</modelVersion>",
             "  <parent>", "    <groupId>%s</groupId>" % REACTOR_GROUP, "    <artifactId>%s</artifactId>" % REACTOR_ARTIFACT,
             "    <version>1</version>", "  </parent>",
             "  <artifactId>%s</artifactId>" % entry["id"],
             "  <description>%s as used by %s</description>" % (xml_escape(gav(entry["plugin"])), xml_escape(", ".join(entry["modules"]))),
             "  <dependencies>"]
    for d in entry["dependencies"]:
        lines += dependency_xml(d, "    ")
    lines += dependency_xml({"groupId": entry["plugin"]["groupId"], "artifactId": entry["plugin"]["artifactId"], "version": entry["plugin"]["version"]}, "    ")
    lines += ["  </dependencies>", "</project>", ""]
    return "\n".join(lines)


def reactor_pom(entries) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<project xmlns="http://maven.apache.org/POM/4.0.0">',
             "  <modelVersion>4.0.0</modelVersion>",
             "  <groupId>%s</groupId>" % REACTOR_GROUP, "  <artifactId>%s</artifactId>" % REACTOR_ARTIFACT, "  <version>1</version>",
             "  <packaging>pom</packaging>",
             "  <description>Dependency trees of the consumer's build plugins, one module per plugin configuration (mvn-rebundle)</description>",
             "  <modules>"]
    lines += ["    <module>%s</module>" % e["id"] for e in entries]
    lines += ["  </modules>", "</project>", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--effective-pom", required=True, help="output of mvn help:effective-pom -Doutput=FILE on the consumer project")
    ap.add_argument("--reactor", default="work/plugin-trees", help="directory of the generated reactor (recreated)")
    ap.add_argument("--plugins-out", default="out/consumer-plugins.json", help="plugin configurations, reactor module ids and the modules using them")
    args = ap.parse_args()

    modules, skipped = read_plugins(Path(args.effective_pom))
    entries: Dict[tuple, dict] = OrderedDict()
    for module, plugins in modules.items():
        for p in plugins:
            key = config_key(p)
            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = {"id": "plugin-%d" % (len(entries) + 1), "plugin": p["plugin"], "dependencies": p["dependencies"], "modules": []}
            if module not in entry["modules"]:
                entry["modules"].append(module)
    ordered = list(entries.values())

    reactor = Path(args.reactor)
    if reactor.exists():
        shutil.rmtree(reactor)
    reactor.mkdir(parents=True)
    (reactor / "pom.xml").write_text(reactor_pom(ordered), encoding="utf-8")
    for e in ordered:
        (reactor / e["id"]).mkdir()
        (reactor / e["id"] / "pom.xml").write_text(module_pom(e), encoding="utf-8")

    write_json(args.plugins_out, {
        "effective_pom": str(args.effective_pom),
        "reactor": str(reactor),
        "plugins": ordered,
        "modules": {m: [gav(p["plugin"]) for p in plugins] for m, plugins in modules.items()},
        "skipped": skipped,
    })
    log("%d module(s), %d distinct plugin configuration(s); reactor written to %s" % (len(modules), len(ordered), reactor))
    for e in ordered:
        log("  %s: %s%s (%s)" % (e["id"], gav(e["plugin"]), (" + %d added dependenc%s" % (len(e["dependencies"]), "y" if len(e["dependencies"]) == 1 else "ies")) if e["dependencies"] else "", ", ".join(e["modules"])))
    for s in skipped:
        log("  [WARN] %s in %s: %s" % (s["plugin"], s["module"], s["reason"]))
    log("Next: mvn -B -f %s/pom.xml org.apache.maven.plugins:maven-dependency-plugin:3.7.1:tree -Dverbose -DoutputType=text -DoutputFile=<abs path> -DappendOutput=true" % reactor)


if __name__ == "__main__":
    main()
