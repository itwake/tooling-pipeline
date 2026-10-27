#!/usr/bin/env python3
"""Stage 7b: apply a consumer patch proposal to the consumer project's POM files.

Reads consumer-patch.json (written by gen_consumer_patch.py from the consumer's dependency tree)
and edits the POMs in place, preserving their formatting and comments:

  * deletes the direct dependencies that the bundle replaces,
  * adds <exclusions> on the direct dependencies that transitively bring an embedded coordinate,
  * adds the bundle dependency first in every changed module (version managed in the root POM of
    a multi-module build, explicit otherwise),
  * adds or updates the maven-enforcer-plugin execution that bans the embedded coordinates and,
    when Spring Boot is not inside the bundle, pins the consumer to the Boot minor line.

Dependencies declared in a parent POM inside the checkout are edited there. Nothing is committed;
the caller reviews the diff (--diff) and decides.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Dict, List, Optional

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


def locate_dependency(pom: Pom, poms: Dict[str, Pom], g: str, a: str):
    """The POM (module or an ancestor inside the checkout) that declares g:a, and the element."""
    current: Optional[Pom] = pom
    seen = set()
    while current is not None and current.path not in seen:
        seen.add(current.path)
        dep = pomedit.find_dependency(current.root, current.src, g, a)
        if dep is not None:
            return current, dep
        current = parent_of(current, poms)
    return None, None


def remove_dependency(pom: Pom, poms: Dict[str, Pom], ga: str, warnings: List[str]) -> None:
    g, a = ga.split(":", 1)
    owner, dep = locate_dependency(pom, poms, g, a)
    if dep is None:
        warnings.append("%s: direct dependency %s not found in the POM or its parents (declared through a profile or a BOM import?)" % (pom.ga, ga))
        return
    owner.src = pomedit.remove_element(owner.src, dep)
    owner.reparse()
    owner.actions.append("removed dependency %s" % ga)


def add_exclusions(pom: Pom, poms: Dict[str, Pom], direct_ga: str, targets: List[str], warnings: List[str]) -> None:
    g, a = direct_ga.split(":", 1)
    owner, dep = locate_dependency(pom, poms, g, a)
    if dep is None:
        warnings.append("%s: dependency %s (needs exclusions for %s) not found in the POM or its parents" % (pom.ga, direct_ga, ", ".join(targets)))
        return
    present = set(pomedit.existing_exclusions(dep, owner.src))
    missing = [t for t in targets if tuple(t.split(":", 1)) not in present]
    if not missing:
        return
    blocks = "\n".join("<exclusion>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n</exclusion>" % tuple(t.split(":", 1)) for t in missing)
    exc = dep.child("exclusions")
    if exc is None:
        owner.src = pomedit.insert_child(owner.src, dep, "<exclusions>\n" + "\n".join("  " + l for l in blocks.splitlines()) + "\n</exclusions>", owner.root)
    else:
        owner.src = pomedit.insert_child(owner.src, exc, blocks, owner.root)
    owner.reparse()
    owner.actions.append("excluded %s from %s" % (", ".join(missing), direct_ga))


def add_bundle_dependency(pom: Pom, bundle: dict, with_version: bool) -> None:
    if pomedit.find_dependency(pom.root, pom.src, bundle["groupId"], bundle["artifactId"]) is not None:
        return
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
    warnings: List[str] = []
    touched_modules = 0
    for module in patch.get("modules", []):
        if not module.get("remove_direct") and not module.get("exclusions"):
            continue
        pom = poms.get(module["module"])
        if pom is None:
            warnings.append("module %s has changes in the proposal but no pom.xml was found for it" % module["module"])
            continue
        touched_modules += 1
        for ga in module.get("remove_direct", {}):
            remove_dependency(pom, poms, ga, warnings)
        for direct_ga, info in module.get("exclusions", {}).items():
            add_exclusions(pom, poms, direct_ga, info.get("targets", []), warnings)
        add_bundle_dependency(pom, bundle, with_version=not multi_module)
    if multi_module and touched_modules:
        manage_bundle_version(root_pom, bundle)
    banned = patch.get("banned") or sorted({"%s:%s" % (g["groupId"], g["artifactId"]) for g in patch["embedded"] if g.get("groupId") != "rebundle.unidentified"})
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
    log("Diff written to %s; suggested branch %s" % (args.diff, branch))


if __name__ == "__main__":
    main()
