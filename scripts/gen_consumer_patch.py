#!/usr/bin/env python3
"""Stage 7: proposal for the consumer project's POM change (never applied automatically).

The bundle is described by one of
  --manifest    bundle-manifest.json of an mvn-rebundle build (default)
  --bundle-jar  a bundle jar, for example downloaded from Nexus (its manifest lists the contents)
  --bundle g:a:v --embedded "g:a:v,..."   coordinates typed by hand

With one or more `mvn dependency:tree -Dverbose -DoutputType=text` outputs of the consumer
project the proposal is exact: which direct dependencies to delete, which direct
dependencies need an <exclusion> because they transitively bring an embedded coordinate, and
which of the transitive dependencies the bundle POM still declares must be excluded on the
bundle dependency itself (the module never had them, reaches them on its own, or only in test
scope) rather than taken from the bundle. Without a tree it explains how to produce one and
gives the generic snippets.

The Enforcer rule in the proposal is the real gate: the consumer build fails whenever an
embedded coordinate sneaks back through any path, and, when Spring Boot itself is not inside
the bundle, whenever the consumer leaves the Boot minor line the bundle was built from.

With --plugin-tree (the dependency:tree output of the reactor plugin_trees.py writes) and --plugins
(its JSON) the proposal also covers the build plugins: a plugin whose class realm contains an
embedded coordinate gets the bundle as its first dependency, and the coordinate is taken away as far
as Maven allows: removed when the POMs add it to the plugin, excluded on the added dependency that
brings it, cut by re-declaring the plugin's own direct dependency with an exclusion when it is
transitive, and only shadowed (the bundle precedes it on the realm classpath) when it is a direct
dependency of the plugin itself, which no POM configuration can exclude. No Enforcer rule covers
plugin realms; verify_plugin_trees.py checks the result after the build instead.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pomedit  # noqa: E402
from bundlelib import (  # noqa: E402
    BUNDLE_BOOT_ATTR,
    BUNDLE_CONTENTS_ATTR,
    embedded_variants,
    fail,
    ga_str,
    gav_str,
    identify_gav,
    is_test_variant,
    log,
    node_classifier,
    parse_dependency_trees,
    parse_selection_text,
    read_json,
    read_manifest,
    test_variant_include,
    variant_key,
    write_json,
)
from identify import SYNTHETIC_GROUP, synthetic_hint  # noqa: E402
from resolve_selection import family_of  # noqa: E402

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
            </excludes>%s
            <message>%s</message>
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


def parse_coordinates(text: str):
    components = []
    for tok in parse_selection_text(text):
        p = tok.split(":")
        if len(p) < 3:
            fail("coordinate '%s' must be groupId:artifactId:version" % tok)
        gav = {"groupId": p[0], "artifactId": p[1], "version": p[2]}
        if len(p) > 3 and p[3]:
            gav["classifier"] = p[3]
        components.append({"gav": gav})
    return components


def manifest_from_jar(path: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        attrs = read_manifest(zf)
        gav, _, _ = identify_gav(zf, Path(path).name)
    contents = attrs.get(BUNDLE_CONTENTS_ATTR, "")
    if not gav or not contents:
        fail("%s is not an mvn-rebundle bundle (needs its own pom.properties and the %s manifest attribute)" % (path, BUNDLE_CONTENTS_ATTR))
    return {"bundle": gav, "coordinate": gav_str(gav), "components": parse_coordinates(contents.replace(",", "\n")),
            "reduced_pom": "", "pom": "", "spring_boot_version": attrs.get(BUNDLE_BOOT_ATTR)}


def manifest_from_coordinates(bundle: str, embedded_text: str) -> dict:
    parts = bundle.split(":")
    if len(parts) != 3:
        fail("--bundle must be groupId:artifactId:version")
    components = parse_coordinates(embedded_text)
    if not components:
        fail("--embedded (or --embedded-file) is required with --bundle")
    return {"bundle": {"groupId": parts[0], "artifactId": parts[1], "version": parts[2]}, "coordinate": bundle,
            "components": components, "reduced_pom": "", "pom": "", "spring_boot_version": None}


def boot_line_excludes(boot_version: str):
    parts = boot_version.split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return []
    major, minor = int(parts[0]), int(parts[1])
    return ["org.springframework.boot:spring-boot:(,%d.%d)" % (major, minor),
            "org.springframework.boot:spring-boot:[%d.%d,)" % (major, minor + 1)]


# Artifacts the Maven core realm exports to every plugin realm (maven-core's META-INF/maven/extension.xml):
# a plugin never loads them from its own dependencies, so an embedded coordinate among them is not a hit.
MAVEN_CORE_ARTIFACTS = {
    "classworlds:classworlds", "org.codehaus.plexus:plexus-classworlds", "org.codehaus.plexus:plexus-component-api",
    "org.codehaus.plexus:plexus-container-default", "plexus:plexus-container-default", "org.sonatype.spice:spice-inject-plexus",
    "org.sonatype.sisu:sisu-inject-plexus", "org.eclipse.sisu:org.eclipse.sisu.plexus",
    "org.apache.maven:maven-artifact", "org.apache.maven:maven-aether-provider", "org.apache.maven:maven-resolver-provider",
    "org.apache.maven:maven-artifact-manager", "org.apache.maven:maven-compat", "org.apache.maven:maven-core",
    "org.apache.maven:maven-error-diagnostics", "org.apache.maven:maven-lifecycle", "org.apache.maven:maven-model",
    "org.apache.maven:maven-model-builder", "org.apache.maven:maven-monitor", "org.apache.maven:maven-plugin-api",
    "org.apache.maven:maven-plugin-descriptor", "org.apache.maven:maven-plugin-parameter-documenter",
    "org.apache.maven:maven-plugin-registry", "org.apache.maven:maven-profile", "org.apache.maven:maven-project",
    "org.apache.maven:maven-repository-metadata", "org.apache.maven:maven-settings", "org.apache.maven:maven-settings-builder",
    "org.apache.maven:maven-toolchain", "org.apache.maven.wagon:wagon-provider-api",
    "org.apache.maven.resolver:maven-resolver-api", "org.apache.maven.resolver:maven-resolver-spi",
    "org.apache.maven.resolver:maven-resolver-impl", "org.apache.maven.resolver:maven-resolver-util",
    "org.apache.maven.resolver:maven-resolver-connector-basic",
    "javax.inject:javax.inject", "javax.annotation:javax.annotation-api", "org.slf4j:slf4j-api", "org.fusesource.jansi:jansi",
    "org.sonatype.aether:aether-api", "org.sonatype.aether:aether-spi", "org.sonatype.aether:aether-impl",
    "org.eclipse.aether:aether-api", "org.eclipse.aether:aether-spi", "org.eclipse.aether:aether-impl",
    "org.eclipse.aether:aether-util", "org.eclipse.aether:aether-connector-basic",
}

PLUGIN_POLICY_DEFAULT = {"direct_hits": "shadow", "skip": []}


def plugin_policy(path) -> dict:
    """The plugins section of policy.json: direct_hits (shadow or skip) and the plugins never to touch."""
    policy = {"direct_hits": PLUGIN_POLICY_DEFAULT["direct_hits"], "skip": []}
    try:
        section = read_json(Path(path)).get("plugins") or {}
    except (OSError, ValueError):
        return policy
    if section.get("direct_hits") in ("shadow", "skip"):
        policy["direct_hits"] = section["direct_hits"]
    policy["skip"] = [s for s in (section.get("skip") or []) if isinstance(s, str)]
    return policy


def hit_ga(node, embedded_ga, hints, resolved, variants=None):
    """groupId:artifactId when the node is an embedded coordinate, by coordinates or through a synthetic
    hint (artifactId and, when known, version); the hint's real coordinates are recorded in resolved.
    With variants (bundlelib.embedded_variants) a test-scope jar under a classifier the bundle does not
    embed is no hit (bundlelib.is_test_variant), and a synthetic hint never stands for a classified test
    jar: it describes one of the application's own jars."""
    ga = ga_str(node)
    if ga in embedded_ga:
        if variants is not None and is_test_variant(node, variants):
            return None
        return ga
    if variants is not None and node_classifier(node) and (node.get("scope") or "") == "test":
        return None
    hit = next((h for h in (hints or []) if h["artifactId"] == node["artifactId"]
                and (not h.get("version") or h["version"] == node.get("version"))), None)
    if hit is None:
        return None
    if resolved is not None:
        resolved[hit["artifactId"]] = ga
    return ga


def promoted_dispositions(nodes, matched, promoted_ga, what="module"):
    """Per transitive dependency the bundle POM still declares: excluded on the bundle dependency (never
    in this tree, already reached through a path without embedded coordinates, or only in test/provided
    scope) or kept (reachable only through an embedded coordinate)."""
    exclusions, kept = OrderedDict(), OrderedDict()
    occurrences = defaultdict(list)
    for node in nodes:
        occurrences[ga_str(node)].append(node)
    for ga in promoted_ga or []:
        occ = occurrences.get(ga, [])
        if not occ:
            exclusions[ga] = "not in this %s's dependency tree" % what
            continue
        runtime = [n for n in occ if (n.get("scope") or "compile") in ("compile", "runtime")]
        own = [n for n in runtime if not any(p in matched for p in n["path"][1:-1])]
        if own:
            exclusions[ga] = "already reached through " + " -> ".join(own[0]["path"][1:])
        elif runtime:
            kept[ga] = "reached only through " + " -> ".join(runtime[0]["path"][1:])
        else:
            exclusions[ga] = "only in %s scope here" % (occ[0].get("scope") or "unknown")
    return exclusions, kept


def descendants(nodes, i):
    """Indexes of the nodes below nodes[i]; the tree is in pre-order."""
    depth, j, out = nodes[i]["depth"], i + 1, []
    while j < len(nodes) and nodes[j]["depth"] > depth:
        out.append(j)
        j += 1
    return out


def analyse_trees(texts, embedded_ga, hints=None, resolved=None, promoted_ga=None, variants=None, embedded=None, families=None):
    """Modules with the changes they need. Nodes matching a synthetic hint (artifactId and, when
    known, version) count as embedded too; their real coordinates are recorded in `resolved`.

    promoted_ga: transitive dependencies the bundle POM still declares. Per module each one is
    either excluded on the bundle dependency (the module never had it, already reaches it through a
    path without embedded coordinates, or only in test/provided scope) or kept (reachable only
    through an embedded coordinate: once the exclusions are in place the bundle is its only path).

    variants (bundlelib.embedded_variants): the classifiers embedded under each groupId:artifactId. A
    test-scope jar under another classifier (test-jar, tests, test...) is not inside the bundle and is no
    hit: it is kept (`test_variants`), and when every path to it is cut, by a deleted direct dependency or
    by an exclusion (Maven excludes a groupId:artifactId whatever the classifier), it is listed under
    `redeclare`, to be declared directly with test scope. Under another classifier outside test scope a
    jar still counts as the embedded coordinate, since such variants usually hold the same classes, and
    is listed under `other_variants`.

    embedded (the bundle's coordinates) and families (policy.json) add `drift` per module: see
    drift_in_tree."""
    resolved = resolved if resolved is not None else {}
    parsed = []
    for text in texts:
        for tree in parse_dependency_trees(text):
            nodes = tree["nodes"]
            hits = [hit_ga(n, embedded_ga, hints, resolved, variants) for n in nodes]
            tops, top = [], None
            for i, n in enumerate(nodes):
                if n["depth"] == 1 or top is None:
                    top = i
                tops.append(top)
            parsed.append((tree, hits, tops))
    modules = []
    # what the exclusions take away below each direct dependency, over all modules: a declaration in a
    # parent POM carries them into every module that inherits it
    cut = defaultdict(set)
    for tree, hits, tops in parsed:
        nodes = tree["nodes"]
        module = {"module": ga_str(tree["root"]), "remove_direct": OrderedDict(), "exclusions": OrderedDict(), "paths": [],
                  "bundle_exclusions": OrderedDict(), "promoted_kept": OrderedDict(),
                  "test_variants": OrderedDict(), "redeclare": OrderedDict(), "other_variants": OrderedDict(), "drift": [],
                  "direct": list(OrderedDict.fromkeys(ga_str(n) for n in nodes if n["depth"] == 1))}
        matched = set()
        for i, node in enumerate(nodes):
            ga = hits[i]
            if ga is None:
                continue
            matched.add(ga)
            path = node["path"]
            module["paths"].append(" -> ".join(path[1:]) + (" (%s)" % node["note"] if node.get("note") else ""))
            if variants is not None and ga in variants and node_classifier(node) not in variants[ga]:
                module["other_variants"].setdefault(variant_key(node), {
                    "version": node.get("version"), "scope": node.get("scope") or "compile", "path": " -> ".join(path[1:])})
            if node["depth"] == 1:
                module["remove_direct"].setdefault(ga, {"scope": node.get("scope"), "version": node.get("version")})
            elif hits[tops[i]] is None:  # below a deleted direct dependency the path goes away with it
                entry = module["exclusions"].setdefault(path[1], {"version": None, "targets": []})
                if ga not in entry["targets"]:
                    entry["targets"].append(ga)
        for node in nodes:
            if node["depth"] == 1 and ga_str(node) in module["exclusions"]:
                module["exclusions"][ga_str(node)]["version"] = node.get("version")
        for direct, entry in module["exclusions"].items():
            cut[direct].update(entry["targets"])
        module["bundle_exclusions"], module["promoted_kept"] = promoted_dispositions(nodes, matched, promoted_ga, "module")
        modules.append(module)
    if variants is None:
        return modules
    known = dict(variants)
    for real in resolved.values():
        known.setdefault(real, {None})
    for module, (tree, hits, tops) in zip(modules, parsed):
        nodes = tree["nodes"]
        occurrences = OrderedDict()
        for i, n in enumerate(nodes):
            if hits[i] is None and is_test_variant(n, known):
                occurrences.setdefault(variant_key(n), []).append(i)
        for key, idxs in occurrences.items():
            winner = next((i for i in idxs if not nodes[i]["omitted"]), idxs[0])
            w = nodes[winner]
            survivors = [i for i in idxs if nodes[i]["depth"] == 1 or (
                hits[tops[i]] is None and not any(ga in cut[nodes[i]["path"][1]] for ga in nodes[i]["path"][2:]))]
            info = {"groupId": w["groupId"], "artifactId": w["artifactId"], "classifier": node_classifier(w),
                    "type": w.get("type") or "jar", "version": w.get("version"),
                    "declared": any(nodes[i]["depth"] == 1 for i in idxs), "path": " -> ".join(w["path"][1:]),
                    "kept_through": " -> ".join(nodes[survivors[0]]["path"][1:]) if survivors else None}
            module["test_variants"][key] = info
            if survivors:
                continue
            direct, excluded = tops[winner], None
            if hits[direct] is not None:
                reason = "%s, which brought it, is deleted (it is inside the bundle)" % ga_str(nodes[direct])
            else:
                excluded = next(ga for ga in w["path"][2:] if ga in cut[w["path"][1]])
                reason = "the exclusion of %s on %s removes it as well: Maven excludes by groupId:artifactId, whatever the classifier" % (
                    excluded, ga_str(nodes[direct]))
            targets = []
            for j in descendants(nodes, winner):
                if hits[j] and hits[j] not in targets:
                    targets.append(hits[j])
            module["redeclare"][key] = dict(info, scope="test", exclusions=targets, reason=reason, via=ga_str(nodes[direct]),
                                            cut_by=excluded, after=ga_str(nodes[direct]) if hits[direct] is None else None)
    if embedded is not None:
        for module, (tree, hits, tops) in zip(modules, parsed):
            module["drift"] = drift_in_tree(tree["nodes"], hits, embedded, families or {}, known)
    return modules


def drift_in_tree(nodes, hits, embedded, families, known):
    """Versions in one module's tree that will not match the bundle after the switch. After it the
    embedded libraries run at the bundle's versions while everything else keeps the consumer's:

      embedded  an embedded coordinate the module resolves at another version
      train     a jar outside the bundle from a release train the bundle embeds, at another version
                (spring-boot-test or spring-test next to the bundle's Spring Boot and Spring Framework,
                a test jar of an embedded coordinate). A train is a groupId whose embedded jars all share
                one version, that is a policy family or holds at least two of them."""
    versions, trains, size = {}, defaultdict(set), defaultdict(int)
    for g in embedded:
        if g.get("groupId") == SYNTHETIC_GROUP or not g.get("version"):
            continue
        versions[(ga_str(g), g.get("classifier") or None)] = g["version"]
        versions.setdefault((ga_str(g), "*"), g["version"])
        trains[g["groupId"]].add(g["version"])
        size[g["groupId"]] += 1
    out = []
    for i, n in enumerate(nodes):
        v = n.get("version")
        if n.get("omitted") or not v:
            continue
        ga, scope = ga_str(n), n.get("scope") or "compile"
        if hits[i]:
            bv = versions.get((ga, node_classifier(n))) or versions.get((ga, "*"))
            if hits[i] == ga and bv and v != bv:
                out.append({"kind": "embedded", "coordinate": variant_key(n), "version": v, "bundle_version": bv, "scope": scope})
            continue
        if ga in known:
            bv, train = versions.get((ga, "*")), ga
        elif len(trains.get(n["groupId"], ())) == 1 and (family_of(n["groupId"], families) or size[n["groupId"]] >= 2):
            bv, train = next(iter(trains[n["groupId"]])), n["groupId"]
        else:
            continue
        if bv and v != bv:
            out.append({"kind": "train", "coordinate": variant_key(n), "version": v, "bundle_version": bv, "scope": scope, "train": train})
    return out


def classify_plugin_tree(tree, plugin_ga, embedded_ga, hints=None, resolved=None):
    """Embedded coordinates in one plugin's dependency tree (a plugin_trees.py reactor module: the
    dependencies the POMs add to the plugin and the plugin artifact at depth 1, the plugin's own
    dependencies below it), sorted by where Maven lets the consumer act:

      remove_declared      an added dependency that is itself embedded: delete it
      declared_exclusions  an added dependency that brings one transitively: <exclusions> on it
      redeclare            a transitive dependency of the plugin: re-declare the plugin's own direct
                           dependency that brings it, with an exclusion (added dependencies dominate the
                           plugin's own ones, so Maven takes the re-declaration)
      shadowed             a direct dependency of the plugin itself, or reached only through one: no POM
                           configuration can exclude it, the bundle merely precedes it on the realm classpath
    """
    nodes = tree["nodes"]
    result = {"remove_declared": OrderedDict(), "declared_exclusions": OrderedDict(), "redeclare": OrderedDict(),
              "shadowed": OrderedDict(), "paths": [], "hits": set(),
              "direct": list(OrderedDict.fromkeys(ga_str(n) for n in nodes if n["depth"] == 1))}
    hits = [(n, hit_ga(n, embedded_ga, hints, resolved)) for n in nodes]
    hits = [(n, ga) for n, ga in hits if ga is not None and ga not in MAVEN_CORE_ARTIFACTS]
    hit_set = {ga for _, ga in hits}
    own_direct = {ga for n, ga in hits if n["depth"] == 2 and n["path"][1] == plugin_ga}
    versions = {}
    for n in nodes:
        if not n.get("omitted"):
            versions.setdefault(ga_str(n), n.get("version"))
    for n, ga in hits:
        path = n["path"]
        result["hits"].add(ga)
        result["paths"].append(" -> ".join(path[1:]) + (" (%s)" % n["note"] if n.get("note") else ""))
        if path[1] != plugin_ga:  # below a dependency the POMs add to the plugin
            if n["depth"] == 1:
                result["remove_declared"].setdefault(ga, {"version": n.get("version"), "scope": n.get("scope")})
            else:
                entry = result["declared_exclusions"].setdefault(path[1], {"version": versions.get(path[1]), "targets": []})
                if ga not in entry["targets"]:
                    entry["targets"].append(ga)
            continue
        if n["depth"] == 2 or ga in own_direct or any(p in hit_set for p in path[2:-1]):
            result["shadowed"].setdefault(ga, versions.get(ga) or n.get("version"))
            continue
        direct = path[2]
        entry = result["redeclare"].setdefault(direct, {"version": versions.get(direct), "targets": []})
        if ga not in entry["targets"]:
            entry["targets"].append(ga)
    return result


def analyse_plugin_trees(texts, plugins_map, embedded_ga, hints=None, resolved=None, promoted_ga=None, policy=None):
    """Entries for the plugin configurations whose dependency tree contains an embedded coordinate, with
    the action the policy allows, and the number of configurations without any."""
    policy = policy or PLUGIN_POLICY_DEFAULT
    by_id = {p["id"]: p for p in (plugins_map or {}).get("plugins", [])}
    entries, clean = [], 0
    for text in texts:
        for tree in parse_dependency_trees(text):
            mapping = by_id.get(tree["root"]["artifactId"])
            if mapping is None:
                continue
            plugin = mapping["plugin"]
            plugin_ga = "%s:%s" % (plugin["groupId"], plugin["artifactId"])
            c = classify_plugin_tree(tree, plugin_ga, embedded_ga, hints, resolved)
            if not c["hits"]:
                clean += 1
                continue
            excl, kept = promoted_dispositions(tree["nodes"], c["hits"], promoted_ga, "plugin")
            entry = {"id": mapping["id"], "plugin": "%s:%s:%s" % (plugin["groupId"], plugin["artifactId"], plugin["version"]),
                     "plugin_ga": plugin_ga, "modules": list(mapping.get("modules", [])),
                     "remove_declared": c["remove_declared"], "declared_exclusions": c["declared_exclusions"],
                     "redeclare": c["redeclare"], "shadowed": c["shadowed"],
                     "bundle_exclusions": excl, "promoted_kept": kept, "paths": c["paths"], "action": "patch", "reason": None}
            if plugin_ga in policy["skip"]:
                entry["action"], entry["reason"] = "skip", "listed in policy plugins.skip"
            elif c["shadowed"] and policy["direct_hits"] == "skip":
                entry["action"], entry["reason"] = "skip", "its own direct dependencies %s are embedded: they cannot be excluded, and policy plugins.direct_hits is skip" % ", ".join(c["shadowed"])
            entries.append(entry)
    return entries, clean


def plugin_management_snippet(entry: dict, bundle: dict) -> str:
    g, a = entry["plugin_ga"].split(":", 1)
    lines = ["<pluginManagement>", "  <plugins>", "    <plugin>", "      <groupId>%s</groupId>" % g,
             "      <artifactId>%s</artifactId>" % a, "      <dependencies>"]
    lines += ["        " + l for l in dependency_snippet(bundle, list(entry["bundle_exclusions"])).splitlines()]
    for direct, info in entry["redeclare"].items():
        dg, da = direct.split(":", 1)
        lines += ["        <dependency>", "          <groupId>%s</groupId>" % dg, "          <artifactId>%s</artifactId>" % da,
                  "          <version>%s</version>" % (info.get("version") or "the version the plugin resolves"), "          <exclusions>"]
        for t in info["targets"]:
            tg, ta = t.split(":", 1)
            lines += ["            <exclusion>", "              <groupId>%s</groupId>" % tg, "              <artifactId>%s</artifactId>" % ta, "            </exclusion>"]
        lines += ["          </exclusions>", "        </dependency>"]
    lines += ["      </dependencies>", "    </plugin>", "  </plugins>", "</pluginManagement>"]
    return "\n".join(lines)


def dependency_snippet(bundle: dict, exclusions=None) -> str:
    lines = [
        "<dependency>",
        "  <groupId>%s</groupId>" % bundle["groupId"],
        "  <artifactId>%s</artifactId>" % bundle["artifactId"],
        "  <version>%s</version>" % bundle["version"],
    ]
    if exclusions:
        lines.append("  <exclusions>")
        for ga in exclusions:
            g, a = ga.split(":", 1)
            lines += ["    <exclusion>", "      <groupId>%s</groupId>" % g, "      <artifactId>%s</artifactId>" % a, "    </exclusion>"]
        lines.append("  </exclusions>")
    lines.append("</dependency>")
    return "\n".join(lines)


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


def policy_families(path) -> dict:
    """The families section of policy.json: groupId prefixes that share one release train."""
    try:
        return read_json(Path(path)).get("families") or {}
    except (OSError, ValueError):
        return {}


def variant_snippet(v: dict) -> str:
    return pomedit.dependency_xml(v["groupId"], v["artifactId"], v.get("version"), "test", v.get("type"), v.get("classifier"), v.get("exclusions") or [])


def collect_drift(modules) -> list:
    """The modules' drift entries, one per coordinate and version, with the modules that show it."""
    found = OrderedDict()
    for m in modules:
        for d in m.get("drift") or []:
            e = found.setdefault((d["kind"], d["coordinate"], d["version"]), dict(d, modules=[]))
            if m["module"] not in e["modules"]:
                e["modules"].append(m["module"])
    return sorted(found.values(), key=lambda d: (d["kind"] != "embedded", d["coordinate"], d["version"]))


def drift_text(d: dict) -> str:
    where = ", ".join(d.get("modules") or [])
    if d["kind"] == "embedded":
        return "`%s`: the consumer resolves %s, the bundle embeds %s (%s)" % (d["coordinate"], d["version"], d["bundle_version"], where)
    return "`%s` %s (%s scope) belongs to `%s`, which the bundle embeds at %s (%s)" % (
        d["coordinate"], d["version"], d["scope"], d["train"], d["bundle_version"], where)


def drift_advice(drift, boot_version) -> str:
    spring = boot_version and any(d["coordinate"].startswith("org.springframework") for d in drift)
    return ("After the switch the embedded libraries run at the bundle's versions, while everything outside the bundle, test "
            "libraries included, keeps the versions the consumer manages. A library run against other versions of the classes "
            "it was built for typically fails with NoClassDefFoundError or NoSuchMethodError, often in the tests only. Align the "
            "consumer with the bundle%s, or rebuild the bundle from an application built at the consumer's versions." % (
                " (Spring Boot parent or BOM %s)" % boot_version if spring else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="out/bundle-manifest.json", help="bundle manifest written by gen_bundle_project.py")
    ap.add_argument("--bundle-jar", default="", help="instead of --manifest: a bundle jar (its manifest lists the contents)")
    ap.add_argument("--bundle", default="", help="instead of --manifest: groupId:artifactId:version of an existing bundle")
    ap.add_argument("--embedded", default="", help="with --bundle: embedded coordinates g:a:v, comma or newline separated")
    ap.add_argument("--embedded-file", default="", help="with --bundle: file holding the embedded coordinates")
    ap.add_argument("--tree", action="append", default=[], help="dependency:tree text output (repeatable)")
    ap.add_argument("--reduced-pom", help="POM published with the bundle: the one finalize_bundle_pom.py wrote, or Shade's dependency-reduced POM (default from the manifest)")
    ap.add_argument("--plugin-tree", action="append", default=[], help="dependency:tree text output of the reactor plugin_trees.py wrote (repeatable)")
    ap.add_argument("--plugins", default="", help="consumer-plugins.json written by plugin_trees.py: the plugin configurations behind the reactor modules")
    ap.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "config" / "policy.json"),
                    help="policy.json; its plugins section says how to treat plugins whose own direct dependencies are embedded")
    ap.add_argument("--out", default="out/consumer-patch.md")
    ap.add_argument("--out-json", default="out/consumer-patch.json")
    ap.add_argument("--findings-out", default="", help="findings one per line: [drift], [variant], [info] (default: <out-json>-findings.txt)")
    args = ap.parse_args()

    if args.bundle_jar:
        man = manifest_from_jar(args.bundle_jar)
    elif args.bundle:
        text = args.embedded or ""
        if args.embedded_file:
            text += "\n" + Path(args.embedded_file).read_text(encoding="utf-8")
        man = manifest_from_coordinates(args.bundle, text)
    else:
        man = read_json(args.manifest)

    bundle = man["bundle"]
    coord = man["coordinate"]
    embedded = [c["gav"] for c in man["components"]]
    synthetic = [(g, synthetic_hint(g)) for g in embedded if g.get("groupId") == SYNTHETIC_GROUP]
    embedded_ga = {ga_str(g) for g in embedded if g.get("groupId") != SYNTHETIC_GROUP}
    variants = embedded_variants(embedded, SYNTHETIC_GROUP)
    boot_version = man.get("spring_boot_version")
    boot_embedded = "org.springframework.boot:spring-boot" in embedded_ga
    boot_excludes = boot_line_excludes(boot_version) if boot_version and not boot_embedded else []
    pom_candidates = [args.reduced_pom, man.get("pom"), man.get("reduced_pom")]
    reduced_pom = next((Path(c) for c in pom_candidates if c and Path(c).is_file()), Path(""))
    promoted = [d for d in reduced_pom_dependencies(reduced_pom) if "%s:%s" % (d.get("groupId"), d.get("artifactId")) not in embedded_ga] if str(reduced_pom) and reduced_pom.is_file() else []
    promoted_ga = ["%s:%s" % (d.get("groupId"), d.get("artifactId")) for d in promoted]

    texts = []
    for t in args.tree:
        p = Path(t)
        if p.is_file():
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
        else:
            log("WARN: tree file not found: %s" % t)
    resolved_synthetic = {}
    modules = analyse_trees(texts, embedded_ga, [h for _, h in synthetic], resolved_synthetic, promoted_ga,
                            variants, embedded, policy_families(args.policy)) if texts else []
    plugin_texts = []
    for t in args.plugin_tree:
        p = Path(t)
        if p.is_file():
            plugin_texts.append(p.read_text(encoding="utf-8", errors="replace"))
        else:
            log("WARN: plugin tree file not found: %s" % t)
    plugins_map = read_json(args.plugins) if args.plugins and Path(args.plugins).is_file() else None
    if plugin_texts and plugins_map is None:
        log("WARN: --plugin-tree without --plugins (consumer-plugins.json): the plugin trees cannot be mapped to plugins and are ignored")
    policy = plugin_policy(args.policy)
    plugins_checked = bool(plugin_texts and plugins_map)
    plugin_entries, plugins_clean = analyse_plugin_trees(plugin_texts, plugins_map, embedded_ga, [h for _, h in synthetic],
                                                         resolved_synthetic, promoted_ga, policy) if plugins_checked else ([], 0)
    banned_ga = sorted(embedded_ga | set(resolved_synthetic.values()))
    # test jars that share a banned groupId:artifactId under a classifier the bundle does not embed
    includes = sorted({test_variant_include(v["groupId"], v["artifactId"], v["classifier"]) for m in modules
                       for v in m["test_variants"].values() if v.get("classifier") and ga_str(v) in banned_ga})
    drift = collect_drift(modules)

    includes_arg = ",".join(banned_ga)
    md = []
    md.append("# Consumer POM change proposal: %s" % coord)
    md.append("")
    md.append("Coordinates embedded in the bundle: %s" % ", ".join(gav_str(g) for g in embedded))
    md.append("")
    md.append("No Java code changes; package names stay the same. Three things to do: delete the replaced direct dependencies, add the bundle dependency, and add an exclusion on every path that still brings an original coordinate transitively. The Enforcer rule below fails the build whenever a path is missed. Section 2 also says, per module, which transitive dependencies of the bundle POM to exclude on the bundle dependency itself.")
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
            if not m["paths"] and not m["test_variants"]:
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
            if m["redeclare"]:
                md.append("Declare these test jars directly. They share groupId:artifactId with an embedded coordinate under another classifier, so the bundle does not contain them, and the changes above would take them away:")
                md.append("")
                for key, v in m["redeclare"].items():
                    md.append("- `%s` %s: %s" % (key, v["version"], v["reason"]))
                md.append("")
                for key, v in m["redeclare"].items():
                    md.append("```xml\n%s\n```" % variant_snippet(v))
                    md.append("")
            kept = [(k, v) for k, v in m["test_variants"].items() if k not in m["redeclare"]]
            if kept:
                md.append("Test jars that stay as they are (same groupId:artifactId as an embedded coordinate, another classifier, not inside the bundle): " + ", ".join(
                    "`%s` %s (%s)" % (k, v["version"], "declared directly" if v["declared"] else "through " + v["kept_through"]) for k, v in kept))
                md.append("")
            if m["other_variants"]:
                md.append("Taken away with the embedded coordinate although the bundle does not contain them (another classifier outside test scope; Maven exclusions and the Enforcer only see groupId:artifactId). Check that the module does not need them: " + ", ".join(
                    "`%s` %s (%s scope)" % (k, v["version"], v["scope"]) for k, v in m["other_variants"].items()))
                md.append("")
            if m["bundle_exclusions"]:
                md.append("The bundle POM still declares transitive dependencies this module must not take from the bundle; add the bundle dependency with these exclusions:")
                md.append("")
                for ga, why in m["bundle_exclusions"].items():
                    md.append("- `%s`: %s" % (ga, why))
                md.append("")
                md.append("```xml\n%s\n```" % dependency_snippet(bundle, list(m["bundle_exclusions"])))
                md.append("")
            if m["promoted_kept"]:
                md.append("Transitive dependencies this module keeps through the bundle: " + ", ".join("`%s` (%s)" % (ga, why) for ga, why in m["promoted_kept"].items()))
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
        md.append("Then regenerate this proposal with `--tree dependency-tree.txt`, or inspect every path that brings an original coordinate by hand:")
        md.append("")
        md.append("```bash\nmvn dependency:tree -Dverbose -Dincludes=%s\n```" % includes_arg)
        md.append("")
        md.append("Test jars that share groupId:artifactId with an embedded coordinate under another classifier (`<type>test-jar</type>`, classifier `tests` or `test`) are not inside the bundle: keep them. Maven excludes by groupId:artifactId whatever the classifier, so where an exclusion takes one away, declare it directly with `<scope>test</scope>`, and let it through the Enforcer rule with an include such as `groupId:artifactId:*:*:test:tests`.")
        md.append("")

    if drift:
        md.append("## Version drift between the consumer and the bundle")
        md.append("")
        md.append("The dependency tree resolves these at other versions than the bundle:")
        md.append("")
        for d in drift:
            md.append("- " + drift_text(d))
        md.append("")
        md.append(drift_advice(drift, boot_version))
        md.append("")

    if synthetic:
        md.append("## Jars embedded under synthetic coordinates")
        md.append("")
        md.append("These jars carried no Maven descriptor and could not be identified, so the bundle embeds them under `%s` coordinates. The consumer POM still references their real coordinates; find them in the dependency tree and exclude them like the others:" % SYNTHETIC_GROUP)
        md.append("")
        for g, h in synthetic:
            real = resolved_synthetic.get(h["artifactId"]) if h else None
            if real:
                md.append("- `%s` (%s%s) is `%s` in the consumer tree; it is banned below" % (
                    gav_str(g), h["artifactId"], (" " + h["version"]) if h.get("version") else "", real))
            else:
                md.append("- `%s`: look for artifactId `%s`%s in `mvn dependency:tree -Dverbose`, add an exclusion for its groupId:artifactId and append it to the Enforcer list" % (
                    gav_str(g), h["artifactId"] if h else g["artifactId"], (" version " + h["version"]) if h and h.get("version") else ""))
        md.append("")

    excludes = ["              <exclude>%s</exclude>" % ga for ga in banned_ga]
    if boot_excludes:
        excludes.append("              <!-- the bundle was built from Spring Boot %s: stay on that minor line or rebuild the bundle -->" % boot_version)
        excludes += ["              <exclude>%s</exclude>" % e for e in boot_excludes]
    message = "These coordinates are embedded in %s. Add an exclusion on the dependency that reintroduces them (mvn dependency:tree -Dverbose -Dincludes=%s)." % (coord, includes_arg)
    if boot_excludes:
        message += " Spring Boot outside the %s.x line needs a rebuilt bundle." % ".".join(boot_version.split(".")[:2])
    md.append("## 3. Enforcer gate (put it in the parent POM's build/plugins so every module inherits it)")
    md.append("")
    includes_block = ("\n            <includes>\n%s\n            </includes>" % "\n".join(
        "              <include>%s</include>" % i for i in includes)) if includes else ""
    md.append("```xml\n%s\n```" % (ENFORCER % ("\n".join(excludes), includes_block, message)))
    md.append("")
    if includes:
        md.append("The includes let the test jars of section 2 through: each pattern (groupId:artifactId:version:type:scope:classifier) admits one classifier in test scope only, so the embedded jars stay banned in every scope.")
        md.append("")

    md.append("## 4. Spring Boot version")
    md.append("")
    if boot_embedded:
        v = boot_version or "the embedded version"
        md.append("Spring Boot %s itself is inside the bundle. The `<version>` of `spring-boot-maven-plugin` must be `%s` so the launcher it writes matches the embedded runtime. `apply_consumer_patch.py` sets it wherever the changed modules declare or manage the plugin inside the checkout; a declaration that takes its version from `spring-boot-starter-parent` %s is left alone, one that takes it from another parent outside the checkout gets the version written explicitly. Remove the Boot starters from the POM; the Enforcer rule bans their coordinates anyway." % (boot_version or "(version unknown)", v, v))
    elif boot_version:
        md.append("The bundle was built from a Spring Boot %s application. The Enforcer rule above keeps the consumer on the %s.x line; moving to another line means rebuilding the bundle first, because the embedded libraries are frozen at the versions that line manages." % (boot_version, ".".join(boot_version.split(".")[:2])))
    else:
        md.append("The Spring Boot version of the source application is unknown; rebuild the bundle whenever the consumer upgrades Spring Boot.")
    md.append("")

    md.append("## 5. Transitive dependencies the bundle POM still declares")
    md.append("")
    if promoted:
        md.append("These are not embedded. The bundle POM promotes them so that a consumer which only reached them through an embedded coordinate still gets them. Section 2 decides per module: a module keeps one when it is reachable only through an embedded coordinate, and excludes it on the bundle dependency when it never had it, already reaches it on its own (the bundle would only move it ahead on the classpath and let the bundle's version compete) or has it only in test scope. Without a dependency tree, exclude the ones the module never had.")
        md.append("")
        for d in promoted:
            md.append("- `%s:%s:%s`%s" % (d.get("groupId"), d.get("artifactId"), d.get("version"), (" (scope %s)" % d["scope"]) if d.get("scope") else ""))
    else:
        md.append("None (or the bundle POM was not available).")
    md.append("")
    if plugins_checked:
        md.append("## 6. Build plugins whose dependency tree contains an embedded coordinate")
        md.append("")
        md.append("A plugin's class realm is its artifact, its compile and runtime dependencies, and the dependencies the POMs add to it; the added ones come first and dominate the plugin's own. %d plugin configuration(s) checked: %d with an embedded coordinate in the realm, %d without. For a hit the bundle becomes the plugin's first dependency, in the root POM's `pluginManagement` so every module's use of the plugin inherits it, and the coordinate is taken away as far as Maven allows: an added dependency that is embedded is removed, an added dependency that brings one gets an exclusion, a transitive dependency of the plugin itself is cut by re-declaring the plugin's own direct dependency with an exclusion (that version is then pinned in the POM), and a direct dependency of the plugin itself cannot be excluded at all: the bundle precedes it on the realm classpath, so its classes come from the bundle while the jar is still resolved. No Enforcer rule covers plugin realms; the consumer job regenerates the plugin trees after the build and checks them instead." % (
            len(plugin_entries) + plugins_clean, len(plugin_entries), plugins_clean))
        md.append("")
        for e in plugin_entries:
            md.append("### %s" % e["plugin"])
            md.append("")
            md.append("Used by: %s." % ", ".join("`%s`" % m for m in e["modules"]))
            md.append("")
            if e["action"] != "patch":
                md.append("**Left untouched**: %s." % e["reason"])
                md.append("")
            if e["remove_declared"]:
                md.append("Remove from the dependencies the POMs add to the plugin: " + ", ".join("`%s` (%s)" % (ga, info.get("version")) for ga, info in e["remove_declared"].items()))
                md.append("")
            for direct, info in e["declared_exclusions"].items():
                md.append("Add exclusions on the added dependency `%s`: %s" % (direct, ", ".join("`%s`" % t for t in info["targets"])))
                md.append("")
            for direct, info in e["redeclare"].items():
                md.append("Re-declare the plugin's own dependency `%s` (version %s) with exclusions: %s" % (direct, info.get("version"), ", ".join("`%s`" % t for t in info["targets"])))
                md.append("")
            if e["shadowed"]:
                md.append("Shadowed only, no exclusion is possible: " + ", ".join("`%s` (%s)" % (ga, v) for ga, v in e["shadowed"].items()))
                md.append("")
            if e["bundle_exclusions"]:
                md.append("Exclusions on the bundle dependency: " + ", ".join("`%s` (%s)" % (ga, why) for ga, why in e["bundle_exclusions"].items()))
                md.append("")
            if e["promoted_kept"]:
                md.append("Transitive dependencies the plugin keeps through the bundle: " + ", ".join("`%s` (%s)" % (ga, why) for ga, why in e["promoted_kept"].items()))
                md.append("")
            if e["action"] == "patch":
                md.append("```xml\n%s\n```" % plugin_management_snippet(e, bundle))
                md.append("")
            md.append("<details><summary>Paths found in the plugin's dependency tree (%d)</summary>\n" % len(e["paths"]))
            for p in e["paths"]:
                md.append("- %s" % p)
            md.append("\n</details>")
            md.append("")
        verification_section = "## 7. Verification"
    else:
        verification_section = "## 6. Verification"
    md.append(verification_section)
    md.append("")
    md.append("1. Consumer `mvn -B clean verify`: a passing Enforcer check means no path brings the original coordinates back.")
    md.append("2. Run the verify job on the app.jar or image built before and after the change: every class and resource on the dependency classpath must be byte-identical, with only the replaced jars gone and the bundle added.")
    md.append("3. Run the existing tests, rebuild the image, smoke-test it.")
    md.append("")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(md), encoding="utf-8")
    write_json(args.out_json, {
        "bundle": bundle,
        "coordinate": coord,
        "embedded": embedded,
        "spring_boot_version": boot_version,
        "boot_embedded": boot_embedded,
        "boot_line_excludes": boot_excludes,
        "synthetic": [{"gav": g, "hint": h, "resolved": resolved_synthetic.get(h["artifactId"]) if h else None} for g, h in synthetic],
        "banned": banned_ga,
        "enforcer_includes": includes,
        "version_drift": drift,
        "modules": [{"module": m["module"], "remove_direct": m["remove_direct"], "exclusions": m["exclusions"],
                     "bundle_exclusions": m["bundle_exclusions"], "promoted_kept": m["promoted_kept"], "paths": m["paths"],
                     "test_variants": m["test_variants"], "redeclare": m["redeclare"], "other_variants": m["other_variants"],
                     "drift": m["drift"], "direct": m["direct"]} for m in modules],
        "promoted_dependencies": promoted,
        "tree_provided": bool(texts),
        "plugins": plugin_entries,
        "plugins_without_hits": plugins_clean,
        "plugin_tree_provided": plugins_checked,
        "plugin_policy": policy,
    })
    findings = ["[drift] " + drift_text(d).replace("`", "") for d in drift]
    for m in modules:
        for k, v in m["other_variants"].items():
            findings.append("[variant] %s: %s %s (%s scope) goes with the embedded %s although the bundle does not contain it: check that the module does not need it" % (
                m["module"], k, v["version"], v["scope"], ":".join(k.split(":")[:2])))
        for k, v in m["redeclare"].items():
            findings.append("[info] %s: test jar %s %s re-declared with test scope, %s" % (m["module"], k, v["version"], v["reason"]))
        for k, v in m["test_variants"].items():
            if k not in m["redeclare"]:
                findings.append("[info] %s: test jar %s %s kept, the bundle does not contain it" % (m["module"], k, v["version"]))
    findings_path = Path(args.findings_out) if args.findings_out else Path(args.out_json).with_name(Path(args.out_json).stem + "-findings.txt")
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text("".join(f + "\n" for f in findings), encoding="utf-8")

    log("Consumer patch proposal written to %s" % args.out)
    for m in modules:
        log("  %s: remove %d direct, exclusions on %d direct dependencies, %d paths%s%s" % (
            m["module"], len(m["remove_direct"]), len(m["exclusions"]), len(m["paths"]),
            (", exclude on the bundle: %s" % ", ".join(m["bundle_exclusions"])) if m["bundle_exclusions"] else "",
            (", test jars kept %d, re-declared %d" % (len(m["test_variants"]) - len(m["redeclare"]), len(m["redeclare"]))) if m["test_variants"] else ""))
    for f in findings:
        if not f.startswith("[info]"):
            log("  WARN: %s" % f)
    if plugins_checked:
        log("  build plugins: %d with an embedded coordinate in their dependency tree (%d to patch, %d skipped), %d without" % (
            len(plugin_entries), sum(1 for e in plugin_entries if e["action"] == "patch"), sum(1 for e in plugin_entries if e["action"] != "patch"), plugins_clean))
        for e in plugin_entries:
            log("    %s (%s): remove %d, exclude on %d, re-declare %d, shadowed %d%s" % (
                e["plugin"], e["action"], len(e["remove_declared"]), len(e["declared_exclusions"]), len(e["redeclare"]), len(e["shadowed"]),
                (": " + e["reason"]) if e.get("reason") else ""))


if __name__ == "__main__":
    main()
