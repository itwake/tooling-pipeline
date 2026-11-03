#!/usr/bin/env python3
"""Stage 7b: apply a consumer patch proposal to the consumer project's POM files.

Reads consumer-patch.json (written by gen_consumer_patch.py from the consumer's dependency tree)
and edits the POMs in place, preserving their formatting and comments:

  * deletes the direct dependencies that the bundle replaces, from the main <dependencies> and from
    every profile that declares them (the Enforcer bans them whatever the active profiles),
  * adds <exclusions> on the direct dependencies that transitively bring an embedded coordinate, on
    each of their declarations (a profile that re-declares one replaces the main declaration,
    exclusions included, while it is active),
  * adds the bundle dependency first in every changed module (version managed in the root POM of
    a multi-module build, explicit otherwise), with exclusions for the transitive dependencies of
    the bundle POM that the module must not take from the bundle,
  * adds or updates the maven-enforcer-plugin execution that bans the embedded coordinates and,
    when Spring Boot is not inside the bundle, pins the consumer to the Boot minor line,
  * when Spring Boot itself is inside the bundle, sets the version of spring-boot-maven-plugin to the
    embedded Boot version wherever the changed modules declare or manage the plugin inside the
    checkout, so the launcher the plugin writes matches the runtime,
  * for every build plugin whose dependency tree contains an embedded coordinate (the proposal's
    `plugins`, from the plugin trees): removes the coordinate from the dependencies the POMs add to
    the plugin, adds exclusions on the added dependencies that bring one, and in the root POM's
    pluginManagement puts the bundle first among the plugin's dependencies and re-declares the
    plugin's own direct dependencies that bring one with exclusions. A direct dependency of the
    plugin itself cannot be excluded; the bundle precedes it on the realm classpath.

Dependencies declared in a parent POM inside the checkout are edited there. Dependencies that
profiles declare and that the dependency tree never showed (the profile was not active when it ran)
are listed as warnings: nothing is known about what they bring. Nothing is committed; the caller
reviews the diff (--diff) and decides.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pomedit  # noqa: E402
from bundlelib import embedded_variants, fail, log, read_json, test_variant_include, write_json  # noqa: E402

ENFORCER_PLUGIN = """<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-enforcer-plugin</artifactId>
  <version>3.5.0</version>
  <executions>
%s
  </executions>
</plugin>"""

ENFORCER_EXECUTION = """<execution>
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
</execution>"""

SKIP_DIRS = {".git", "target", "node_modules", ".idea", "build"}
BOOT_PLUGIN_GROUP, BOOT_PLUGIN = "org.springframework.boot", "spring-boot-maven-plugin"
BOOT_PARENT = "org.springframework.boot:spring-boot-starter-parent"


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Pom:
    def __init__(self, path: Path):
        self.path = path
        self.original = path.read_text(encoding="utf-8")
        self.src = self.original
        self.root = pomedit.parse(self.src)
        self.group = pomedit.project_group_id(self.root, self.src)
        self.artifact = self.root.child_text(self.src, "artifactId") or ""
        self.packaging = self.root.child_text(self.src, "packaging") or "jar"
        parent = self.root.child("parent")
        self.parent_ga = None
        self.parent_relpath = None
        if parent is not None:
            self.parent_ga = "%s:%s" % (parent.child_text(self.src, "groupId"), parent.child_text(self.src, "artifactId"))
            self.parent_relpath = parent.child_text(self.src, "relativePath")
            if self.parent_relpath is None:
                self.parent_relpath = "../pom.xml"
        self.actions: List[str] = []

    @property
    def ga(self) -> str:
        return "%s:%s" % (self.group, self.artifact)

    def reparse(self) -> None:
        self.root = pomedit.parse(self.src)

    def changed(self) -> bool:
        return self.src != self.original


def discover_poms(project: Path) -> Dict[str, Pom]:
    poms: Dict[str, Pom] = {}
    for p in sorted(project.rglob("pom.xml")):
        if any(part in SKIP_DIRS for part in p.relative_to(project).parts):
            continue
        try:
            pom = Pom(p)
        except (ValueError, UnicodeDecodeError) as e:
            log("WARN: cannot parse %s: %s" % (p, e))
            continue
        poms[pom.ga] = pom
    return poms


def parent_of(pom: Pom, poms: Dict[str, Pom]) -> Optional[Pom]:
    if not pom.parent_ga:
        return None
    candidate = poms.get(pom.parent_ga)
    if candidate is not None and candidate is not pom:
        return candidate
    rel = (pom.path.parent / (pom.parent_relpath or "../pom.xml")).resolve()
    if rel.is_dir():
        rel = rel / "pom.xml"
    for p in poms.values():
        if p.path.resolve() == rel:
            return p
    return None


def declarations(pom: Pom, poms: Dict[str, Pom], g: str, a: str):
    """Every declaration of g:a the module sees, as (owner POM, place, element): the module's own POM
    and its parents inside the checkout, main <dependencies> ("" as place) and profiles alike."""
    found = []
    for owner in ancestors(pom, poms):
        found += [(owner, place, dep) for place, dep in pomedit.dependency_declarations(owner.root, owner.src, g, a)]
    return found


def declared_classifier(owner: Pom, poms: Dict[str, Pom], dep) -> Tuple[Optional[str], bool]:
    """Classifier of a declaration (a test-jar is the tests classifier) and whether it is known: a property
    the checkout does not define, such as ${os.detected.classifier}, leaves it unknown."""
    text = dep.child_text(owner.src, "classifier")
    if text:
        value = resolve_property(owner, poms, text) if "${" in text else text
        return value, value is not None
    return ("tests", True) if dep.child_text(owner.src, "type") == "test-jar" else (None, True)


def keeps_test_variant(owner: Pom, poms: Dict[str, Pom], dep, ga: str, variants: Dict[str, set], seen: set) -> bool:
    """A declaration of an embedded groupId:artifactId that is a test jar the bundle does not contain:
    another classifier than the embedded ones (a test-jar, the tests or test classifier...) in test scope,
    declared so or shown so by the dependency tree (seen: the module's test_variants)."""
    embedded = variants.get(ga)
    if embedded is None:
        return False
    classifier, known = declared_classifier(owner, poms, dep)
    if known and classifier in embedded:
        return False
    if known and classifier and "%s:%s" % (ga, classifier) in seen:
        return True
    return (dep.child_text(owner.src, "scope") or "") == "test"


def remove_dependency(pom: Pom, poms: Dict[str, Pom], ga: str, removed: set, warnings: List[str], keep=None, captured=None, project=None) -> List[str]:
    """Delete every declaration of a replaced coordinate the module sees. The Enforcer bans it whatever
    the active profiles, so one left in an inactive profile would fail the build the day it is used.
    keep(owner, dep) spares the test jars of that groupId:artifactId the bundle does not contain; they
    are returned, as groupId:artifactId:classifier and where they are declared."""
    g, a = ga.split(":", 1)
    found = False
    while True:
        decls = [d for d in declarations(pom, poms, g, a) if not (keep and keep(d[0], d[2]))]
        if not decls:
            break
        owner, place, dep = decls[0]
        if captured is not None and project is not None:
            captured.append(declaration_info(owner, poms, dep, project, place))
        owner.src = pomedit.remove_element(owner.src, dep)
        owner.reparse()
        owner.actions.append("removed dependency %s%s" % (ga, (" from %s" % place) if place else ""))
        found = True
        removed.add(ga)
    kept = ["%s:%s in %s%s" % (ga, declared_classifier(o, poms, d)[0] or "?", o.ga, (" %s" % place) if place else "")
            for o, place, d in declarations(pom, poms, g, a) if keep and keep(o, d)]
    if not found and ga not in removed and not kept:
        warnings.append("%s: direct dependency %s not found in the POM, its profiles or its parents inside the checkout (removed by an earlier run, or declared in a parent POM outside the checkout?)" % (pom.ga, ga))
    return kept


def ensure_exclusions(owner: Pom, dep, targets: List[str]) -> List[str]:
    """Add the <exclusion> entries a dependency element lacks; returns the ones added."""
    present = set(pomedit.existing_exclusions(dep, owner.src))
    missing = [t for t in targets if tuple(t.split(":", 1)) not in present]
    if not missing:
        return []
    blocks = "\n".join("<exclusion>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n</exclusion>" % tuple(t.split(":", 1)) for t in missing)
    exc = dep.child("exclusions")
    if exc is None:
        owner.src = pomedit.insert_child(owner.src, dep, "<exclusions>\n" + "\n".join("  " + l for l in blocks.splitlines()) + "\n</exclusions>", owner.root)
    else:
        owner.src = pomedit.insert_child(owner.src, exc, blocks, owner.root)
    owner.reparse()
    return missing


def add_exclusions(pom: Pom, poms: Dict[str, Pom], direct_ga: str, targets: List[str], warnings: List[str]) -> List[str]:
    """Add the exclusions on every declaration of the direct dependency the module sees: a profile that
    re-declares it replaces the main declaration, exclusions included, while it is active. Returns the targets added."""
    g, a = direct_ga.split(":", 1)
    count = len(declarations(pom, poms, g, a))
    if not count:
        warnings.append("%s: dependency %s (needs exclusions for %s) not found in the POM, its profiles or its parents inside the checkout" % (pom.ga, direct_ga, ", ".join(targets)))
        return []
    all_added: List[str] = []
    for i in range(count):
        owner, place, dep = declarations(pom, poms, g, a)[i]
        added = ensure_exclusions(owner, dep, targets)
        if added:
            owner.actions.append("excluded %s from %s%s" % (", ".join(added), direct_ga, (" in %s" % place) if place else ""))
            all_added += [t for t in added if t not in all_added]
    return all_added


def declared_test_variants(poms: Dict[str, Pom], variants: Dict[str, set]) -> List[str]:
    """Enforcer includes for every test jar the POMs declare under a classifier the bundle does not embed,
    profiles included: the dependency tree only showed the profiles that were active."""
    found = set()
    for p in poms.values():
        deps = p.root.child("dependencies")
        decls = (deps.children_named("dependency") if deps is not None else []) + [d for _, d in pomedit.profile_dependencies(p.root, p.src)]
        for d in decls:
            g, a = pomedit.dependency_ga(d, p.src, p.group)
            ga = "%s:%s" % (g, a)
            if ga in variants and (d.child_text(p.src, "scope") or "") == "test":
                c, known = declared_classifier(p, poms, d)
                if known and c and c not in variants[ga]:
                    found.add(test_variant_include(g, a, c))
    return sorted(found)


def redeclare_variant(pom: Pom, poms: Dict[str, Pom], entry: dict) -> bool:
    """Declare a test jar directly, with test scope, when the changes cut every path that brought it: it
    shares groupId:artifactId with an embedded coordinate, and Maven excludes that whatever the
    classifier. It goes right after the dependency that brought it, where that one is declared (its main
    declaration first), else at the end of the module's dependencies. False when it is declared already."""
    g, a, classifier = entry["groupId"], entry["artifactId"], entry.get("classifier")
    for owner, _, dep in declarations(pom, poms, g, a):
        c, known = declared_classifier(owner, poms, dep)
        if known and c == classifier:
            return False
    target = None
    if entry.get("after"):
        decls = declarations(pom, poms, *entry["after"].split(":", 1))
        target = next((d for d in decls if d[1] == ""), decls[0] if decls else None)
    if entry.get("cut_by") and entry.get("after"):
        why = "the exclusion of %s on %s removes this test jar too" % (entry["cut_by"], entry["after"])
    else:
        why = "%s, which brought this test jar, is inside the bundle" % (entry.get("via") or "the dependency")
    block = pomedit.dependency_xml(g, a, entry.get("version"), "test", entry.get("type"), classifier, entry.get("exclusions") or [],
                                   comment="mvn-rebundle: " + why)
    if target is not None:
        owner, place, dep = target
        owner.src = pomedit.insert_after(owner.src, dep, block, owner.root)
    else:
        owner, place = pom, ""
        if pom.root.child("dependencies") is None:
            pom.src, _ = pomedit.ensure_child(pom.src, ["dependencies"], {})
            pom.reparse()
        pom.src = pomedit.insert_child(pom.src, pom.root.child("dependencies"), block, pom.root)
    owner.reparse()
    owner.actions.append("declared test jar %s:%s:%s %s with test scope%s%s (%s)" % (
        g, a, classifier, entry.get("version"), (" after %s" % entry["after"]) if target is not None else "",
        (" in %s" % place) if place else "", why))
    return True


def profile_dependencies_unseen(pom: Pom, poms: Dict[str, Pom], project: Path, direct: List[str], skip: set) -> List[str]:
    """Dependencies declared in profiles of the module or its parents that the dependency tree did not
    show: their profile was not active when it ran, so nothing is known about what they bring."""
    out: List[str] = []
    for owner in ancestors(pom, poms):
        rel = owner.path.relative_to(project).as_posix()
        for place, dep in pomedit.profile_dependencies(owner.root, owner.src):
            ga = "%s:%s" % pomedit.dependency_ga(dep, owner.src, owner.group)
            if ga not in direct and ga not in skip:
                out.append("%s (%s of %s)" % (ga, place, rel))
    return out


def add_bundle_dependency(pom: Pom, bundle: dict, with_version: bool, exclusions: List[str], warnings: Optional[List[str]] = None) -> None:
    dep = pomedit.find_dependency(pom.root, pom.src, bundle["groupId"], bundle["artifactId"])
    if dep is not None:
        set_bundle_version(pom, dep, bundle, warnings if warnings is not None else [])
        dep = pomedit.find_dependency(pom.root, pom.src, bundle["groupId"], bundle["artifactId"])
        prune_bundle_exclusions(pom, dep, exclusions)
        dep = pomedit.find_dependency(pom.root, pom.src, bundle["groupId"], bundle["artifactId"])
    if dep is None:
        block = "<dependency>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n%s</dependency>" % (
            bundle["groupId"], bundle["artifactId"], ("  <version>%s</version>\n" % bundle["version"]) if with_version else "")
        deps = pom.root.child("dependencies")
        if deps is None:
            pom.src, deps = pomedit.ensure_child(pom.src, ["dependencies"], {})
            pom.reparse()
            deps = pom.root.child("dependencies")
        pom.src = pomedit.insert_child(pom.src, deps, block, pom.root, first=True)
        pom.reparse()
        pom.actions.append("added bundle dependency %s:%s%s" % (bundle["groupId"], bundle["artifactId"], "" if with_version else " (version managed in the root POM)"))
        dep = pomedit.find_dependency(pom.root, pom.src, bundle["groupId"], bundle["artifactId"])
    added = ensure_exclusions(pom, dep, exclusions)
    if added:
        pom.actions.append("excluded %s from the bundle dependency" % ", ".join(added))


def manage_bundle_version(root_pom: Pom, bundle: dict) -> None:
    root_pom.src, dm_deps = pomedit.ensure_child(root_pom.src, ["dependencyManagement", "dependencies"], {})
    root_pom.reparse()
    dm = root_pom.root.child("dependencyManagement")
    deps = dm.child("dependencies")
    pg = root_pom.group
    for d in deps.children_named("dependency"):
        if pomedit.dependency_ga(d, root_pom.src, pg) == (bundle["groupId"], bundle["artifactId"]):
            v = d.child("version")
            if v is not None and v.text(root_pom.src) != bundle["version"]:
                root_pom.src = root_pom.src[:v.inner_start] + bundle["version"] + root_pom.src[v.inner_end:]
                root_pom.reparse()
                root_pom.actions.append("managed bundle version set to %s" % bundle["version"])
            return
    block = "<dependency>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n  <version>%s</version>\n</dependency>" % (
        bundle["groupId"], bundle["artifactId"], bundle["version"])
    root_pom.src = pomedit.insert_child(root_pom.src, deps, block, root_pom.root)
    root_pom.reparse()
    root_pom.actions.append("managed bundle version %s in dependencyManagement" % bundle["version"])


def ancestors(pom: Pom, poms: Dict[str, Pom]) -> List[Pom]:
    """The POM and its parents inside the checkout, nearest first."""
    chain: List[Pom] = []
    current: Optional[Pom] = pom
    while current is not None and all(current is not p for p in chain):
        chain.append(current)
        current = parent_of(current, poms)
    return chain


def plugin_declarations(pom: Pom, group: str, artifact: str, default_group: str = "org.apache.maven.plugins") -> List[Tuple[str, pomedit.Element]]:
    """(where, element) for every declaration of a plugin in the POM: build/plugins, build/pluginManagement,
    and the same two places inside each profile. A declaration without groupId means default_group."""
    def plugins_in(container, where):
        found = []
        for p in (container.children_named("plugin") if container is not None else []):
            if p.child_text(pom.src, "artifactId") == artifact and (p.child_text(pom.src, "groupId") or default_group) == group:
                found.append((where, p))
        return found

    def in_build(build, prefix):
        if build is None:
            return []
        management = build.child("pluginManagement")
        found = plugins_in(management.child("plugins"), prefix + "pluginManagement") if management is not None else []
        return found + plugins_in(build.child("plugins"), prefix + "build/plugins")

    found = in_build(pom.root.child("build"), "")
    profiles = pom.root.child("profiles")
    for profile in (profiles.children_named("profile") if profiles is not None else []):
        found += in_build(profile.child("build"), "profile %s " % (profile.child_text(pom.src, "id") or "?"))
    return found


def boot_plugin_declarations(pom: Pom) -> List[Tuple[str, pomedit.Element]]:
    return plugin_declarations(pom, BOOT_PLUGIN_GROUP, BOOT_PLUGIN, default_group=BOOT_PLUGIN_GROUP)


def resolve_property(pom: Pom, poms: Dict[str, Pom], expr: str, depth: int = 0) -> Optional[str]:
    """Value of a ${...} expression from the POM and its parents inside the checkout; None when unknown."""
    m = re.fullmatch(r"\$\{([^}]+)\}", expr.strip())
    if m is None:
        return expr.strip() or None
    if depth > 5:
        return None
    name = m.group(1)
    parent = pom.root.child("parent")
    parent_version = parent.child_text(pom.src, "version") if parent is not None else None
    if name in ("project.version", "pom.version", "version"):
        value = pom.root.child_text(pom.src, "version") or parent_version
    elif name in ("project.parent.version", "parent.version"):
        value = parent_version
    else:
        value = None
        for p in ancestors(pom, poms):
            props = p.root.child("properties")
            value = props.child_text(p.src, name) if props is not None else None
            if value is not None:
                break
    if value is None:
        return None
    return resolve_property(pom, poms, value, depth + 1) if "${" in value else value


def boot_parent_version(chain: List[Pom]) -> Optional[str]:
    """Version of spring-boot-starter-parent when the outermost POM of the chain inherits from it."""
    top = chain[-1]
    if top.parent_ga != BOOT_PARENT:
        return None
    parent = top.root.child("parent")
    return parent.child_text(top.src, "version") if parent is not None else None


def align_boot_plugin(modules: List[Pom], poms: Dict[str, Pom], project: Path, version: str, warnings: List[str]) -> List[str]:
    """Spring Boot itself is inside the bundle, so the launcher written by spring-boot-maven-plugin has
    to match the embedded runtime: every declaration of the plugin that the changed modules see (their
    own POM and their parents inside the checkout) is set to `version`. A declaration without a version
    is left alone when a pluginManagement entry inside the checkout provides it (that entry is aligned
    instead) or when it comes from spring-boot-starter-parent of that same version; otherwise the
    version is written explicitly. Returns notes on the declarations that needed no change."""
    reason = "Spring Boot %s is inside the bundle" % version
    notes: List[str] = []
    todo: List[Pom] = []
    for module in modules:
        for pom in ancestors(module, poms):
            if all(pom is not p for p in todo):
                todo.append(pom)
    declared = False
    for pom in todo:
        rel = pom.path.relative_to(project).as_posix()
        chain = ancestors(pom, poms)
        managed = any(where == "pluginManagement" for p in chain for where, _ in boot_plugin_declarations(p))
        parent_version = boot_parent_version(chain)
        edited = set()
        while True:
            change = None
            for where, plugin in boot_plugin_declarations(pom):
                declared = True
                ver = plugin.child("version")
                if ver is not None:
                    text = ver.text(pom.src)
                    resolved = resolve_property(pom, poms, text) if "${" in text else text
                    if text == version or resolved == version:
                        continue
                    change = ("set", where, plugin, ver, text, resolved)
                    break
                if where.endswith("build/plugins") and (managed or parent_version == version):
                    continue
                if where.endswith("pluginManagement") and parent_version == version:
                    continue  # the entry only carries configuration or dependencies; the matching starter parent provides the version
                change = ("insert", where, plugin, None, None, None)
                break
            if change is None:
                break
            kind, where, plugin, ver, text, resolved = change
            edited.add(where)
            if kind == "set":
                pom.src = pomedit.set_text(pom.src, ver, version)
                was = text if resolved in (None, text) else "%s (%s)" % (text, resolved)
                pom.actions.append("set %s version %s -> %s in %s (%s)" % (BOOT_PLUGIN, was, version, where, reason))
            else:
                pom.src = pomedit.insert_after(pom.src, plugin.child("artifactId"), "<version>%s</version>" % version, pom.root)
                origin = ("spring-boot-starter-parent %s" % parent_version) if parent_version else "a parent outside the checkout"
                pom.actions.append("set %s version %s in %s, it was managed by %s (%s)" % (BOOT_PLUGIN, version, where, origin, reason))
            pom.reparse()
        for where, plugin in boot_plugin_declarations(pom):
            if where in edited:
                continue
            ver = plugin.child("version")
            if ver is None:
                source = ("spring-boot-starter-parent %s" % parent_version) if (where.endswith("pluginManagement") or not managed) else "a pluginManagement entry inside the checkout"
                notes.append("%s: %s declares %s without a version, %s provides %s" % (rel, where, BOOT_PLUGIN, source, version))
            elif ver.text(pom.src) != version:
                notes.append("%s: %s has %s %s, which resolves to %s" % (rel, where, BOOT_PLUGIN, ver.text(pom.src), version))
            else:
                notes.append("%s: %s already has %s %s" % (rel, where, BOOT_PLUGIN, version))
    if not declared:
        warnings.append("%s is not declared in the changed modules or their parents inside the checkout; where the application is repackaged, set the plugin version to %s so the launcher matches the embedded Spring Boot runtime" % (BOOT_PLUGIN, version))
    return notes


def find_plugin_dependency(owner: Pom, plugin, g: str, a: str):
    """The <dependency> g:a among the dependencies a plugin declaration adds to the plugin."""
    deps = plugin.child("dependencies")
    if deps is None:
        return None
    for d in deps.children_named("dependency"):
        if pomedit.dependency_ga(d, owner.src, owner.group) == (g, a):
            return d
    return None


def plugin_declarations_seen(modules: List[Pom], poms: Dict[str, Pom], plugin_ga: str):
    """(owner, where, element) for every declaration of the plugin the modules see, each POM once."""
    g, a = plugin_ga.split(":", 1)
    found, done = [], []
    for module in modules:
        for owner in ancestors(module, poms):
            if any(owner is p for p in done):
                continue
            done.append(owner)
            found += [(owner, where, el) for where, el in plugin_declarations(owner, g, a)]
    return found


def edit_plugin_added_dependencies(entry: dict, modules: List[Pom], poms: Dict[str, Pom], warnings: List[str]) -> None:
    """The dependencies the POMs add to the plugin: delete the embedded ones and add exclusions on those
    that bring one, in every declaration of the plugin the modules see."""
    plugin_ga = entry["plugin_ga"]
    name = plugin_ga.split(":", 1)[1]
    for ga in entry.get("remove_declared") or {}:
        g, a = ga.split(":", 1)
        removed = False
        while True:
            hit = None
            for owner, where, plugin in plugin_declarations_seen(modules, poms, plugin_ga):
                d = find_plugin_dependency(owner, plugin, g, a)
                if d is not None:
                    hit = (owner, where, d)
                    break
            if hit is None:
                break
            owner, where, d = hit
            owner.src = pomedit.remove_element(owner.src, d)
            owner.reparse()
            owner.actions.append("plugin %s: removed its dependency %s (%s)" % (name, ga, where))
            removed = True
        if not removed:
            warnings.append("plugin %s: its dependency %s is in the effective POM but was not found in the checkout (declared in a parent outside it?)" % (entry["plugin"], ga))
    for direct_ga, info in (entry.get("declared_exclusions") or {}).items():
        g, a = direct_ga.split(":", 1)
        targets = info.get("targets", [])

        def matches():
            return [(owner, where, plugin) for owner, where, plugin in plugin_declarations_seen(modules, poms, plugin_ga)
                    if find_plugin_dependency(owner, plugin, g, a) is not None]

        count = len(matches())
        if not count:
            warnings.append("plugin %s: its dependency %s (needs exclusions for %s) was not found in the checkout" % (entry["plugin"], direct_ga, ", ".join(targets)))
            continue
        for i in range(count):
            owner, where, plugin = matches()[i]
            added = ensure_exclusions(owner, find_plugin_dependency(owner, plugin, g, a), targets)
            if added:
                owner.actions.append("plugin %s: excluded %s from its dependency %s (%s)" % (name, ", ".join(added), direct_ga, where))
    while True:  # a <dependencies> element left empty is dropped
        empty = next(((owner, plugin.child("dependencies")) for owner, _, plugin in plugin_declarations_seen(modules, poms, plugin_ga)
                      if plugin.child("dependencies") is not None and not plugin.child("dependencies").children_named("dependency")), None)
        if empty is None:
            break
        owner, deps = empty
        owner.src = pomedit.remove_element(owner.src, deps)
        owner.reparse()


def manage_plugin_dependencies(root_pom: Pom, entry: dict, bundle: dict, warnings: List[str]) -> None:
    """Root POM pluginManagement: the bundle first among the plugin's dependencies, with the exclusions the
    plugin needs, then the plugin's own direct dependencies re-declared with exclusions on the embedded
    coordinates they bring. Every module's use of the plugin inherits it, lifecycle defaults included."""
    g, a = entry["plugin_ga"].split(":", 1)
    root_pom.src, _ = pomedit.ensure_child(root_pom.src, ["build", "pluginManagement", "plugins"], {})
    root_pom.reparse()

    def plugin_el():
        plugins = root_pom.root.child("build").child("pluginManagement").child("plugins")
        for p in plugins.children_named("plugin"):
            if p.child_text(root_pom.src, "artifactId") == a and (p.child_text(root_pom.src, "groupId") or "org.apache.maven.plugins") == g:
                return plugins, p
        return plugins, None

    def dependency(dg, da):
        return find_plugin_dependency(root_pom, plugin_el()[1], dg, da)

    plugins, plugin = plugin_el()
    if plugin is None:
        root_pom.src = pomedit.insert_child(root_pom.src, plugins, "<plugin>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n</plugin>" % (g, a), root_pom.root)
        root_pom.reparse()
        plugins, plugin = plugin_el()
        root_pom.actions.append("pluginManagement: added %s to hold its dependencies" % a)
    if plugin.child("dependencies") is None:
        root_pom.src = pomedit.insert_child(root_pom.src, plugin, "<dependencies>\n</dependencies>", root_pom.root)
        root_pom.reparse()

    dep = dependency(bundle["groupId"], bundle["artifactId"])
    if dep is None:
        block = "<dependency>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n  <version>%s</version>\n</dependency>" % (
            bundle["groupId"], bundle["artifactId"], bundle["version"])
        root_pom.src = pomedit.insert_child(root_pom.src, plugin_el()[1].child("dependencies"), block, root_pom.root, first=True)
        root_pom.reparse()
        root_pom.actions.append("pluginManagement %s: bundle %s:%s:%s added as its first dependency" % (a, bundle["groupId"], bundle["artifactId"], bundle["version"]))
    else:
        v = dep.child("version")
        if v is not None and v.text(root_pom.src) != bundle["version"]:
            root_pom.src = pomedit.set_text(root_pom.src, v, bundle["version"])
            root_pom.reparse()
            root_pom.actions.append("pluginManagement %s: bundle version set to %s" % (a, bundle["version"]))
    added = ensure_exclusions(root_pom, dependency(bundle["groupId"], bundle["artifactId"]), list(entry.get("bundle_exclusions") or {}))
    if added:
        root_pom.actions.append("pluginManagement %s: excluded %s from the bundle dependency" % (a, ", ".join(added)))
    for direct, info in (entry.get("redeclare") or {}).items():
        dg, da = direct.split(":", 1)
        targets = info.get("targets", [])
        inserted = False
        if dependency(dg, da) is None:
            if not info.get("version"):
                warnings.append("plugin %s: the version of its dependency %s is unknown, it was not re-declared; exclude %s from it by hand" % (entry["plugin"], direct, ", ".join(targets)))
                continue
            block = "<dependency>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n  <version>%s</version>\n</dependency>" % (dg, da, info["version"])
            root_pom.src = pomedit.insert_child(root_pom.src, plugin_el()[1].child("dependencies"), block, root_pom.root)
            root_pom.reparse()
            inserted = True
        added = ensure_exclusions(root_pom, dependency(dg, da), targets)
        if inserted:
            root_pom.actions.append("pluginManagement %s: re-declared the plugin's dependency %s %s with exclusions %s" % (a, direct, info["version"], ", ".join(targets)))
        elif added:
            root_pom.actions.append("pluginManagement %s: excluded %s from the plugin's dependency %s" % (a, ", ".join(added), direct))


def apply_plugin_entry(entry: dict, poms: Dict[str, Pom], root_pom: Pom, bundle: dict, warnings: List[str]) -> dict:
    """One build plugin of the proposal: the edits above, or nothing when the proposal says skip."""
    report = {"plugin": entry["plugin"], "modules": list(entry.get("modules") or []), "action": entry.get("action"),
              "reason": entry.get("reason"), "shadowed": dict(entry.get("shadowed") or {}), "notes": []}
    if entry.get("action") != "patch":
        report["notes"].append("left untouched: %s" % entry.get("reason"))
        return report
    modules = [poms[m] for m in report["modules"] if m in poms]
    missing = [m for m in report["modules"] if m not in poms]
    if missing:
        warnings.append("plugin %s: module(s) %s not found in the checkout, their declarations of the plugin were not edited" % (entry["plugin"], ", ".join(missing)))
    if modules and (entry.get("remove_declared") or entry.get("declared_exclusions")):
        edit_plugin_added_dependencies(entry, modules, poms, warnings)
    manage_plugin_dependencies(root_pom, entry, bundle, warnings)
    report["action"] = "patched"
    for ga, v in report["shadowed"].items():
        report["notes"].append("%s %s stays in its realm (direct dependency of the plugin, no exclusion possible); the bundle precedes it" % (ga, v))
    return report


def record_rel(path: Path, project: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return str(path)


def record_path_for(project: Path, given: str) -> Path:
    return Path(given) if given else project / ".mvn" / "rebundle.json"


def load_record(path: Path) -> Optional[dict]:
    """The tool's record of what earlier runs changed in this project, or None."""
    if not path.is_file():
        return None
    try:
        rec = read_json(path)
    except ValueError as e:
        fail("%s cannot be read: %s" % (path, e))
    rec.setdefault("modules", {})
    return rec


def module_record(record: dict, name: str) -> dict:
    m = record["modules"].setdefault(name, {})
    for key, empty in (("removed_direct", {}), ("exclusions_added", {}), ("test_jars_declared", []), ("bundle_exclusions", [])):
        m.setdefault(key, empty)
    return m


def declaration_info(owner: Pom, poms: Dict[str, Pom], dep, project: Path, place: str) -> dict:
    """A dependency declaration as the record keeps it, so that it can be put back as it was."""
    g, a = pomedit.dependency_ga(dep, owner.src, owner.group)
    return {"groupId": g, "artifactId": a, "version": dep.child_text(owner.src, "version"), "scope": dep.child_text(owner.src, "scope"),
            "type": dep.child_text(owner.src, "type"), "classifier": dep.child_text(owner.src, "classifier"),
            "pom": owner.path.relative_to(project).as_posix(), "place": place}


def managed_versions(effective_pom) -> Dict[str, Dict[str, str]]:
    """{module groupId:artifactId: {dependency groupId:artifactId: version}} from help:effective-pom's dependencyManagement."""
    out: Dict[str, Dict[str, str]] = {}
    if not effective_pom or not Path(effective_pom).is_file():
        return out
    try:
        root = ET.parse(str(effective_pom)).getroot()
    except ET.ParseError:
        return out

    def local(el):
        return el.tag.split("}", 1)[-1]

    projects = [root] if local(root) == "project" else [p for p in root if local(p) == "project"]
    for proj in projects:
        fields = {local(c): (c.text or "").strip() for c in proj if local(c) in ("groupId", "artifactId")}
        if "groupId" not in fields:
            parent = next((c for c in proj if local(c) == "parent"), None)
            if parent is not None:
                fields["groupId"] = next(((c.text or "").strip() for c in parent if local(c) == "groupId"), "")
        dm = next((c for c in proj if local(c) == "dependencyManagement"), None)
        deps = next((c for c in dm if local(c) == "dependencies"), None) if dm is not None else None
        managed = {}
        for dep in (deps if deps is not None else []):
            rec = {local(c): (c.text or "").strip() for c in dep}
            if rec.get("groupId") and rec.get("artifactId") and rec.get("version"):
                managed["%s:%s" % (rec["groupId"], rec["artifactId"])] = rec["version"]
        out["%s:%s" % (fields.get("groupId"), fields.get("artifactId"))] = managed
    return out


def restore_dependency(pom: Pom, poms: Dict[str, Pom], project: Path, ga: str, info: dict, managed: Dict[str, str], bundle_ga: str) -> Optional[str]:
    """Declare a coordinate that left the bundle again, as the switch found it (the record) or at the version the
    old bundle embedded; without a recorded version the module's dependencyManagement has to provide one, else the
    old bundle's is written. It goes into the POM and profile the switch deleted it from, right after the bundle
    dependency when that container declares it, else at the end of its dependencies. None when it is declared."""
    g, a = ga.split(":", 1)
    classifier = info.get("classifier") or ("tests" if info.get("type") == "test-jar" else None)
    for owner, _, dep in declarations(pom, poms, g, a):
        c, known = declared_classifier(owner, poms, dep)
        if known and c == classifier:
            return None
    owner = pom
    if info.get("pom"):
        owner = next((p for p in poms.values() if p.path.relative_to(project).as_posix() == info["pom"]), pom)
    version = info.get("version")
    if version is None and ga not in managed:
        version = info.get("fallback_version")
    place = info.get("place") or ""
    container = None
    if place.startswith("profile "):
        profiles = owner.root.child("profiles")
        prof = next((p for p in (profiles.children_named("profile") if profiles is not None else []) if (p.child_text(owner.src, "id") or "?") == place[len("profile "):]), None)
        if prof is not None:
            if prof.child("dependencies") is None:
                owner.src = pomedit.insert_child(owner.src, prof, "<dependencies>\n</dependencies>", owner.root)
                owner.reparse()
                profiles = owner.root.child("profiles")
                prof = next(p for p in profiles.children_named("profile") if (p.child_text(owner.src, "id") or "?") == place[len("profile "):])
            container = prof.child("dependencies")
        else:
            place = ""
    if container is None:
        if owner.root.child("dependencies") is None:
            owner.src, _ = pomedit.ensure_child(owner.src, ["dependencies"], {})
            owner.reparse()
        container = owner.root.child("dependencies")
    block = pomedit.dependency_xml(g, a, version, info.get("scope"), info.get("type"), info.get("classifier"))
    bg, ba = bundle_ga.split(":", 1)
    anchor = next((d for d in container.children_named("dependency") if pomedit.dependency_ga(d, owner.src, owner.group) == (bg, ba)), None)
    if anchor is not None:
        owner.src = pomedit.insert_after(owner.src, anchor, block, owner.root)
    else:
        owner.src = pomedit.insert_child(owner.src, container, block, owner.root)
    owner.reparse()
    note = "restored dependency %s%s%s%s (%s)" % (ga, (" " + version) if version else " (managed version)", (" " + info["type"]) if info.get("type") else "",
                                                 (" in %s" % place) if place else "", info.get("source") or "the record")
    owner.actions.append(note)
    return note


def drop_exclusions(pom: Pom, poms: Dict[str, Pom], direct_ga: str, targets: List[str]) -> List[str]:
    """Remove the exclusions of the given targets from every declaration of a direct dependency the module sees."""
    g, a = direct_ga.split(":", 1)
    dropped: List[str] = []
    for i in range(len(declarations(pom, poms, g, a))):
        owner, place, dep = declarations(pom, poms, g, a)[i]
        owner.src, removed = pomedit.remove_exclusions(owner.src, dep, targets)
        owner.reparse()
        if removed:
            owner.actions.append("dropped the exclusion of %s from %s%s" % (", ".join(removed), direct_ga, (" in %s" % place) if place else ""))
            dropped += removed
    return dropped


def drop_declaration(pom: Pom, poms: Dict[str, Pom], ga: str, classifier: Optional[str]) -> bool:
    """Remove a dependency declaration (with the tool's comment above it) of one classifier of a coordinate."""
    g, a = ga.split(":", 1)
    for owner, place, dep in declarations(pom, poms, g, a):
        c, known = declared_classifier(owner, poms, dep)
        if known and c == classifier:
            owner.src = pomedit.remove_element_and_comment(owner.src, dep)
            owner.reparse()
            owner.actions.append("dropped the test jar declaration %s:%s%s" % (ga, classifier, (" in %s" % place) if place else ""))
            return True
    return False


def prune_bundle_exclusions(pom: Pom, dep, keep: List[str]) -> List[str]:
    """The bundle dependency's exclusions are the tool's: those the proposal no longer lists go."""
    present = ["%s:%s" % e for e in pomedit.existing_exclusions(dep, pom.src)]
    stale = [t for t in present if t not in keep]
    if not stale:
        return []
    pom.src, removed = pomedit.remove_exclusions(pom.src, dep, stale)
    pom.reparse()
    if removed:
        pom.actions.append("dropped %s from the bundle dependency's exclusions" % ", ".join(removed))
    return removed


def remove_managed_bundle(root_pom: Pom, bundle: dict) -> None:
    dm = root_pom.root.child("dependencyManagement")
    deps = dm.child("dependencies") if dm is not None else None
    for d in (deps.children_named("dependency") if deps is not None else []):
        if pomedit.dependency_ga(d, root_pom.src, root_pom.group) == (bundle["groupId"], bundle["artifactId"]):
            root_pom.src = pomedit.remove_element(root_pom.src, d)
            root_pom.reparse()
            root_pom.actions.append("removed the managed bundle version")
            return


def remove_enforcer(root_pom: Pom) -> None:
    """Remove the ban-bundled-coordinates execution; the plugin goes with it when nothing else is left of it."""
    build = root_pom.root.child("build")
    plugins = build.child("plugins") if build is not None else None
    for plugin in (plugins.children_named("plugin") if plugins is not None else []):
        if plugin.child_text(root_pom.src, "artifactId") != "maven-enforcer-plugin":
            continue
        executions = plugin.child("executions")
        ours = next((ex for ex in (executions.children_named("execution") if executions is not None else [])
                     if ex.child_text(root_pom.src, "id") == "ban-bundled-coordinates"), None)
        if ours is None:
            return
        others = [ex for ex in executions.children_named("execution") if ex is not ours]
        if not others and plugin.child("configuration") is None and plugin.child("dependencies") is None:
            root_pom.src = pomedit.remove_element(root_pom.src, plugin)
            root_pom.actions.append("removed maven-enforcer-plugin (it only carried the ban-bundled-coordinates execution)")
        else:
            root_pom.src = pomedit.remove_element(root_pom.src, ours)
            root_pom.actions.append("removed the ban-bundled-coordinates execution")
        root_pom.reparse()
        return


def set_bundle_version(pom: Pom, dep, bundle: dict, warnings: List[str]) -> None:
    """An explicit version on an existing bundle dependency follows the target bundle."""
    ver = dep.child("version")
    if ver is None:
        return
    text = ver.text(pom.src)
    if text == bundle["version"]:
        return
    if "${" in text:
        warnings.append("%s: the bundle dependency's version is %s; set that property to %s" % (pom.ga, text, bundle["version"]))
        return
    pom.src = pomedit.set_text(pom.src, ver, bundle["version"])
    pom.reparse()
    pom.actions.append("bundle version %s -> %s" % (text, bundle["version"]))


def install_enforcer(root_pom: Pom, banned: List[str], boot_excludes: List[str], coordinate: str, boot_version: Optional[str],
                     includes: Optional[List[str]] = None) -> None:
    excludes = ["<exclude>%s</exclude>" % ga for ga in banned]
    if boot_excludes:
        excludes.append("<!-- the bundle was built from Spring Boot %s: stay on that minor line or rebuild the bundle -->" % boot_version)
        excludes += ["<exclude>%s</exclude>" % e for e in boot_excludes]
    message = "These coordinates are embedded in %s; add an exclusion on the dependency that reintroduces them (mvn dependency:tree -Dverbose -Dincludes=%s)." % (
        coordinate, ",".join(banned))
    if boot_excludes:
        message += " Spring Boot outside the %s.x line needs a rebuilt bundle." % ".".join((boot_version or "").split(".")[:2])
    # test jars that share a banned groupId:artifactId under a classifier the bundle does not embed
    includes_block = ("\n        <includes>\n%s\n        </includes>" % "\n".join(
        "          <include>%s</include>" % i for i in includes)) if includes else ""
    execution = ENFORCER_EXECUTION % ("\n".join("          " + e for e in excludes), includes_block, xml_escape(message))

    root_pom.src, plugins = pomedit.ensure_child(root_pom.src, ["build", "plugins"], {})
    root_pom.reparse()
    plugins = root_pom.root.child("build").child("plugins")
    enforcer = None
    for p in plugins.children_named("plugin"):
        if p.child_text(root_pom.src, "artifactId") == "maven-enforcer-plugin":
            enforcer = p
            break
    if enforcer is None:
        root_pom.src = pomedit.insert_child(root_pom.src, plugins, ENFORCER_PLUGIN % "\n".join("    " + l for l in execution.splitlines()), root_pom.root)
        root_pom.reparse()
        root_pom.actions.append("added maven-enforcer-plugin with the ban-bundled-coordinates execution")
        return
    executions = enforcer.child("executions")
    if executions is None:
        root_pom.src = pomedit.insert_child(root_pom.src, enforcer, "<executions>\n" + "\n".join("  " + l for l in execution.splitlines()) + "\n</executions>", root_pom.root)
        root_pom.reparse()
        root_pom.actions.append("added the ban-bundled-coordinates execution to maven-enforcer-plugin")
        return
    for ex in executions.children_named("execution"):
        if ex.child_text(root_pom.src, "id") == "ban-bundled-coordinates":
            root_pom.src = pomedit.replace_element(root_pom.src, ex, execution, root_pom.root)
            root_pom.reparse()
            root_pom.actions.append("updated the ban-bundled-coordinates execution")
            return
    root_pom.src = pomedit.insert_child(root_pom.src, executions, execution, root_pom.root)
    root_pom.reparse()
    root_pom.actions.append("added the ban-bundled-coordinates execution")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", default="out/consumer-patch.json")
    ap.add_argument("--project", required=True, help="checkout of the consumer project")
    ap.add_argument("--root-pom", default="", help="root POM of the build (default: <project>/pom.xml)")
    ap.add_argument("--record", default="", help="the tool's record in the project (default: <project>/.mvn/rebundle.json): read for what to undo, rewritten with this run's changes")
    ap.add_argument("--effective-pom", default="", help="help:effective-pom of the project: a restored dependency whose version the project manages is declared without one")
    ap.add_argument("--dry-run", action="store_true", help="compute the diff, write nothing")
    ap.add_argument("--out", default="out/consumer-apply.json")
    ap.add_argument("--diff", default="out/consumer-apply.diff")
    args = ap.parse_args()

    patch = read_json(args.patch)
    unbundle = bool(patch.get("unbundle"))
    if not patch.get("tree_provided") and not unbundle:
        fail("the patch was generated without the consumer's dependency tree; run gen_consumer_patch.py with --tree first")
    project = Path(args.project).resolve()
    if not project.is_dir():
        fail("%s is not a directory" % project)
    poms = discover_poms(project)
    if not poms:
        fail("no pom.xml found under %s" % project)
    root_path = Path(args.root_pom).resolve() if args.root_pom else project / "pom.xml"
    root_pom = next((p for p in poms.values() if p.path.resolve() == root_path.resolve()), None)
    if root_pom is None:
        fail("root POM %s not found among the parsed POMs" % root_path)
    multi_module = root_pom.packaging == "pom" and root_pom.root.child("modules") is not None

    bundle = patch["bundle"]
    bundle_ga = "%s:%s" % (bundle["groupId"], bundle["artifactId"])
    banned = patch.get("banned") or sorted({"%s:%s" % (g["groupId"], g["artifactId"]) for g in patch["embedded"] if g.get("groupId") != "rebundle.unidentified"})
    warnings: List[str] = []
    touched: List[Pom] = []
    removed: set = set()
    record_path = record_path_for(project, args.record)
    record = load_record(record_path)
    new_record = record or {"version": 1, "tool": "mvn-rebundle", "bundle": None, "modules": {}, "enforcer_includes": []}
    managed_all = managed_versions(args.effective_pom)
    prev_versions = {"%s:%s" % (g.get("groupId"), g.get("artifactId")): g.get("version") for g in (patch.get("previous") or {}).get("components") or []}
    transition = patch.get("transition") or {}
    variants = embedded_variants(patch.get("embedded") or [])
    kept_variants: List[str] = []
    declared_variants: List[str] = []
    restored: List[str] = []
    exclusions_dropped: List[str] = []
    declarations_dropped: List[str] = []
    for module in patch.get("modules", []):
        restore = module.get("restore") or {}
        restoring = any(restore.get(k) for k in ("restore_direct", "drop_exclusions", "drop_test_jars"))
        changes = bool(module.get("remove_direct") or module.get("exclusions"))
        uses_bundle = bool(module.get("uses_bundle"))
        if not changes and not module.get("redeclare") and not restoring and not uses_bundle:
            continue
        pom = poms.get(module["module"])
        if pom is None:
            warnings.append("module %s has changes in the proposal but no pom.xml was found for it" % module["module"])
            continue
        mrec = module_record(new_record, module["module"])
        managed = managed_all.get(module["module"], {})
        # what left the bundle comes back first: the declarations the switch deleted, the exclusions and the test jars it added for them
        for ga, info in (restore.get("restore_direct") or {}).items():
            note = restore_dependency(pom, poms, project, ga, dict(info, fallback_version=prev_versions.get(ga)), managed, bundle_ga)
            if note:
                restored.append("%s: %s" % (pom.ga, note))
            mrec["removed_direct"].pop(ga, None)
        for direct_ga, targets in (restore.get("drop_exclusions") or {}).items():
            exclusions_dropped += ["%s: %s from %s" % (pom.ga, t, direct_ga) for t in drop_exclusions(pom, poms, direct_ga, targets)]
            left = [t for t in mrec["exclusions_added"].get(direct_ga, []) if t not in targets]
            if left:
                mrec["exclusions_added"][direct_ga] = left
            else:
                mrec["exclusions_added"].pop(direct_ga, None)
        for tj in restore.get("drop_test_jars") or []:
            ga = "%s:%s" % (tj.get("groupId"), tj.get("artifactId"))
            if drop_declaration(pom, poms, ga, tj.get("classifier")):
                declarations_dropped.append("%s: %s:%s" % (pom.ga, ga, tj.get("classifier")))
            mrec["test_jars_declared"] = [t for t in mrec["test_jars_declared"]
                                         if (t.get("groupId"), t.get("artifactId"), t.get("classifier")) != (tj.get("groupId"), tj.get("artifactId"), tj.get("classifier"))]
        seen = set(module.get("test_variants") or {})
        captured: List[dict] = []
        for ga in module.get("remove_direct", {}):
            kept_variants += remove_dependency(pom, poms, ga, removed, warnings, keep=lambda o, d, ga=ga: keeps_test_variant(o, poms, d, ga, variants, seen),
                                               captured=captured, project=project)
        for info in captured:
            mrec["removed_direct"].setdefault("%s:%s" % (info["groupId"], info["artifactId"]), info)
        for direct_ga, info in module.get("exclusions", {}).items():
            added = add_exclusions(pom, poms, direct_ga, info.get("targets", []), warnings)
            if added:
                have = mrec["exclusions_added"].setdefault(direct_ga, [])
                have += [t for t in added if t not in have]
        for key, entry in (module.get("redeclare") or {}).items():
            if redeclare_variant(pom, poms, entry):
                declared_variants.append("%s: %s %s" % (pom.ga, key, entry.get("version")))
                mrec["test_jars_declared"].append({"groupId": entry["groupId"], "artifactId": entry["artifactId"], "classifier": entry.get("classifier"), "version": entry.get("version")})
        if unbundle:
            remove_dependency(pom, poms, bundle_ga, set(), [])
            touched.append(pom)
            continue
        if not changes and not uses_bundle:
            continue
        touched.append(pom)
        add_bundle_dependency(pom, bundle, with_version=not multi_module, exclusions=list(module.get("bundle_exclusions") or {}), warnings=warnings)
        mrec["bundle_exclusions"] = list(module.get("bundle_exclusions") or {})
        if module.get("direct") is not None:
            unseen = profile_dependencies_unseen(pom, poms, project, module["direct"], set(banned) | {bundle_ga})
            if unseen:
                warnings.append("%s: not in the dependency tree because the profile was not active when it ran: %s. If they bring an embedded coordinate, rerun the consumer tree with the profile active (CONSUMER_MAVEN_ARGS=-P<id>) or add the exclusions by hand" % (pom.ga, ", ".join(unseen)))
    plugins_report: List[dict] = []
    plugin_report = None
    includes: List[str] = []
    if unbundle:
        remove_managed_bundle(root_pom, bundle)
        remove_enforcer(root_pom)
    else:
        if multi_module and touched:
            manage_bundle_version(root_pom, bundle)
        # before the Boot plugin alignment: a pluginManagement entry created here for the plugin's dependencies
        # is then seen by the alignment, so a first and a second run make the same decision about its version
        plugins_report = [apply_plugin_entry(e, poms, root_pom, bundle, warnings) for e in (patch.get("plugins") or [])]
        if touched and patch.get("boot_embedded") and patch.get("spring_boot_version"):
            plugin_report = {"version": patch["spring_boot_version"],
                             "notes": align_boot_plugin(touched, poms, project, patch["spring_boot_version"], warnings)}
        includes = sorted(set(patch.get("enforcer_includes") or []) | set(declared_test_variants(poms, variants)))
        install_enforcer(root_pom, banned, patch.get("boot_line_excludes") or [], patch["coordinate"], patch.get("spring_boot_version"), includes)
        new_record["bundle"] = patch["coordinate"]
        new_record["enforcer_includes"] = includes

    changed = [p for p in poms.values() if p.changed()]
    diff_chunks = []
    for p in changed:
        rel = p.path.relative_to(project).as_posix()
        diff_chunks.append("".join(difflib.unified_diff(p.original.splitlines(True), p.src.splitlines(True), "a/" + rel, "b/" + rel)))
        pomedit.parse(p.src)  # the result must still be well-formed
    diff_text = "".join(diff_chunks)
    Path(args.diff).parent.mkdir(parents=True, exist_ok=True)
    Path(args.diff).write_text(diff_text, encoding="utf-8")
    record_action = "dry run"
    if not args.dry_run:
        for p in changed:
            p.path.write_text(p.src, encoding="utf-8")
        if unbundle:
            if record_path.is_file():
                record_path.unlink()
                record_action = "deleted"
            else:
                record_action = "absent"
        else:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            before = record_path.read_text(encoding="utf-8") if record_path.is_file() else None
            write_json(record_path, new_record)
            after = record_path.read_text(encoding="utf-8")
            record_action = "unchanged" if before == after else ("written" if before is None else "updated")

    unresolved = [s for s in patch.get("synthetic", []) if not s.get("resolved")]
    for s in unresolved:
        warnings.append("embedded jar %s has no known coordinates in the consumer tree; exclude it by hand" % s["gav"].get("artifactId"))
    branch = "rebundle/%s-%s" % (bundle["artifactId"], "removed" if unbundle else bundle["version"])
    result = {
        "project": str(project),
        "root_pom": str(root_pom.path),
        "multi_module": multi_module,
        "branch": branch,
        "changed_files": [{"path": p.path.relative_to(project).as_posix(), "actions": p.actions} for p in changed],
        "warnings": warnings,
        "spring_boot_plugin": plugin_report,
        "plugins": plugins_report,
        "test_variants": {"kept": kept_variants, "declared": declared_variants},
        "enforcer_includes": includes,
        "transition": transition or None,
        "unbundle": unbundle,
        "restored": restored,
        "exclusions_dropped": exclusions_dropped,
        "declarations_dropped": declarations_dropped,
        "record": {"path": record_rel(record_path, project), "action": record_action, "used": record is not None},
        "dry_run": args.dry_run,
    }
    write_json(args.out, result)
    log("%s %d POM file(s)%s:" % ("Would change" if args.dry_run else "Changed", len(changed), " (dry run)" if args.dry_run else ""))
    for p in changed:
        log("  %s" % p.path.relative_to(project).as_posix())
        for a in p.actions:
            log("    - %s" % a)
    for k in kept_variants:
        log("  [test jar] kept %s: the bundle does not contain it" % k)
    for k in declared_variants:
        log("  [test jar] declared %s with test scope" % k)
    for r in restored:
        log("  [restored] %s" % r)
    for e in exclusions_dropped:
        log("  [exclusion dropped] %s" % e)
    for d in declarations_dropped:
        log("  [declaration dropped] %s" % d)
    for w in warnings:
        log("  [WARN] %s" % w)
    for n in (plugin_report or {}).get("notes", []):
        log("  [plugin] %s" % n)
    for pr in plugins_report:
        for n in pr["notes"]:
            log("  [plugin] %s: %s" % (pr["plugin"], n))
    log("Record %s: %s" % (result["record"]["path"], record_action))
    log("Diff written to %s; suggested branch %s" % (args.diff, branch))


if __name__ == "__main__":
    main()
