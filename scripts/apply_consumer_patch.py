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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pomedit  # noqa: E402
from bundlelib import fail, log, read_json, write_json  # noqa: E402

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
        </excludes>
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


def remove_dependency(pom: Pom, poms: Dict[str, Pom], ga: str, removed: set, warnings: List[str]) -> None:
    """Delete every declaration of a replaced coordinate the module sees. The Enforcer bans it whatever
    the active profiles, so one left in an inactive profile would fail the build the day it is used."""
    g, a = ga.split(":", 1)
    found = False
    while True:
        decls = declarations(pom, poms, g, a)
        if not decls:
            break
        owner, place, dep = decls[0]
        owner.src = pomedit.remove_element(owner.src, dep)
        owner.reparse()
        owner.actions.append("removed dependency %s%s" % (ga, (" from %s" % place) if place else ""))
        found = True
        removed.add(ga)
    if not found and ga not in removed:
        warnings.append("%s: direct dependency %s not found in the POM, its profiles or its parents inside the checkout (removed by an earlier run, or declared in a parent POM outside the checkout?)" % (pom.ga, ga))


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


def add_exclusions(pom: Pom, poms: Dict[str, Pom], direct_ga: str, targets: List[str], warnings: List[str]) -> None:
    """Add the exclusions on every declaration of the direct dependency the module sees: a profile that
    re-declares it replaces the main declaration, exclusions included, while it is active."""
    g, a = direct_ga.split(":", 1)
    count = len(declarations(pom, poms, g, a))
    if not count:
        warnings.append("%s: dependency %s (needs exclusions for %s) not found in the POM, its profiles or its parents inside the checkout" % (pom.ga, direct_ga, ", ".join(targets)))
        return
    for i in range(count):
        owner, place, dep = declarations(pom, poms, g, a)[i]
        added = ensure_exclusions(owner, dep, targets)
        if added:
            owner.actions.append("excluded %s from %s%s" % (", ".join(added), direct_ga, (" in %s" % place) if place else ""))


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


def add_bundle_dependency(pom: Pom, bundle: dict, with_version: bool, exclusions: List[str]) -> None:
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


def install_enforcer(root_pom: Pom, banned: List[str], boot_excludes: List[str], coordinate: str, boot_version: Optional[str]) -> None:
    excludes = ["<exclude>%s</exclude>" % ga for ga in banned]
    if boot_excludes:
        excludes.append("<!-- the bundle was built from Spring Boot %s: stay on that minor line or rebuild the bundle -->" % boot_version)
        excludes += ["<exclude>%s</exclude>" % e for e in boot_excludes]
    message = "These coordinates are embedded in %s; add an exclusion on the dependency that reintroduces them (mvn dependency:tree -Dverbose -Dincludes=%s)." % (
        coordinate, ",".join(banned))
    if boot_excludes:
        message += " Spring Boot outside the %s.x line needs a rebuilt bundle." % ".".join((boot_version or "").split(".")[:2])
    execution = ENFORCER_EXECUTION % ("\n".join("          " + e for e in excludes), xml_escape(message))

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
    ap.add_argument("--dry-run", action="store_true", help="compute the diff, write nothing")
    ap.add_argument("--out", default="out/consumer-apply.json")
    ap.add_argument("--diff", default="out/consumer-apply.diff")
    args = ap.parse_args()

    patch = read_json(args.patch)
    if not patch.get("tree_provided"):
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
    for module in patch.get("modules", []):
        if not module.get("remove_direct") and not module.get("exclusions"):
            continue
        pom = poms.get(module["module"])
        if pom is None:
            warnings.append("module %s has changes in the proposal but no pom.xml was found for it" % module["module"])
            continue
        touched.append(pom)
        for ga in module.get("remove_direct", {}):
            remove_dependency(pom, poms, ga, removed, warnings)
        for direct_ga, info in module.get("exclusions", {}).items():
            add_exclusions(pom, poms, direct_ga, info.get("targets", []), warnings)
        add_bundle_dependency(pom, bundle, with_version=not multi_module, exclusions=list(module.get("bundle_exclusions") or {}))
        if module.get("direct") is not None:
            unseen = profile_dependencies_unseen(pom, poms, project, module["direct"], set(banned) | {bundle_ga})
            if unseen:
                warnings.append("%s: not in the dependency tree because the profile was not active when it ran: %s. If they bring an embedded coordinate, rerun the consumer tree with the profile active (CONSUMER_MAVEN_ARGS=-P<id>) or add the exclusions by hand" % (pom.ga, ", ".join(unseen)))
    if multi_module and touched:
        manage_bundle_version(root_pom, bundle)
    # before the Boot plugin alignment: a pluginManagement entry created here for the plugin's dependencies
    # is then seen by the alignment, so a first and a second run make the same decision about its version
    plugins_report = [apply_plugin_entry(e, poms, root_pom, bundle, warnings) for e in (patch.get("plugins") or [])]
    plugin_report = None
    if touched and patch.get("boot_embedded") and patch.get("spring_boot_version"):
        plugin_report = {"version": patch["spring_boot_version"],
                         "notes": align_boot_plugin(touched, poms, project, patch["spring_boot_version"], warnings)}
    install_enforcer(root_pom, banned, patch.get("boot_line_excludes") or [], patch["coordinate"], patch.get("spring_boot_version"))

    changed = [p for p in poms.values() if p.changed()]
    diff_chunks = []
    for p in changed:
        rel = p.path.relative_to(project).as_posix()
        diff_chunks.append("".join(difflib.unified_diff(p.original.splitlines(True), p.src.splitlines(True), "a/" + rel, "b/" + rel)))
        pomedit.parse(p.src)  # the result must still be well-formed
    diff_text = "".join(diff_chunks)
    Path(args.diff).parent.mkdir(parents=True, exist_ok=True)
    Path(args.diff).write_text(diff_text, encoding="utf-8")
    if not args.dry_run:
        for p in changed:
            p.path.write_text(p.src, encoding="utf-8")

    unresolved = [s for s in patch.get("synthetic", []) if not s.get("resolved")]
    for s in unresolved:
        warnings.append("embedded jar %s has no known coordinates in the consumer tree; exclude it by hand" % s["gav"].get("artifactId"))
    branch = "rebundle/%s-%s" % (bundle["artifactId"], bundle["version"])
    result = {
        "project": str(project),
        "root_pom": str(root_pom.path),
        "multi_module": multi_module,
        "branch": branch,
        "changed_files": [{"path": p.path.relative_to(project).as_posix(), "actions": p.actions} for p in changed],
        "warnings": warnings,
        "spring_boot_plugin": plugin_report,
        "plugins": plugins_report,
        "dry_run": args.dry_run,
    }
    write_json(args.out, result)
    log("%s %d POM file(s)%s:" % ("Would change" if args.dry_run else "Changed", len(changed), " (dry run)" if args.dry_run else ""))
    for p in changed:
        log("  %s" % p.path.relative_to(project).as_posix())
        for a in p.actions:
            log("    - %s" % a)
    for w in warnings:
        log("  [WARN] %s" % w)
    for n in (plugin_report or {}).get("notes", []):
        log("  [plugin] %s" % n)
    for pr in plugins_report:
        for n in pr["notes"]:
            log("  [plugin] %s: %s" % (pr["plugin"], n))
    log("Diff written to %s; suggested branch %s" % (args.diff, branch))


if __name__ == "__main__":
    main()
