#!/usr/bin/env python3
"""Minimal position-preserving editor for Maven POM files.

Every edit is a textual splice at the offsets of the elements involved, so comments, element
order, attribute spelling and the formatting of everything else survive byte for byte. The
parser understands the XML subset POMs use: elements, attributes, comments, CDATA, processing
instructions and a DOCTYPE; it does not expand entities.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

_TOKEN = re.compile(
    r"<!--.*?-->"
    r"|<!\[CDATA\[.*?\]\]>"
    r"|<\?.*?\?>"
    r"|<!DOCTYPE[^>]*>"
    r"|</([A-Za-z_][\w.:-]*)\s*>"
    r"|<([A-Za-z_][\w.:-]*)((?:\s+[^<>]*?)?)(/?)>",
    re.S,
)


def _local(tag: str) -> str:
    return tag.split(":", 1)[-1]


class Element:
    __slots__ = ("tag", "start", "end", "inner_start", "inner_end", "children", "parent")

    def __init__(self, tag: str, start: int, inner_start: int, parent: Optional["Element"]):
        self.tag = tag
        self.start = start
        self.inner_start = inner_start
        self.inner_end = inner_start
        self.end = inner_start
        self.children: List[Element] = []
        self.parent = parent

    def child(self, tag: str) -> Optional["Element"]:
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def children_named(self, tag: str) -> List["Element"]:
        return [c for c in self.children if c.tag == tag]

    def text(self, src: str) -> str:
        return src[self.inner_start:self.inner_end].strip()

    def child_text(self, src: str, tag: str) -> Optional[str]:
        c = self.child(tag)
        return c.text(src) if c is not None else None


def parse(src: str) -> Element:
    """Parse the document and return its root element."""
    stack: List[Element] = []
    for m in _TOKEN.finditer(src):
        if m.group(1):
            if not stack:
                raise ValueError("closing tag </%s> without an open element at offset %d" % (m.group(1), m.start()))
            el = stack.pop()
            if el.tag != _local(m.group(1)):
                raise ValueError("mismatched closing tag </%s> for <%s> at offset %d" % (m.group(1), el.tag, m.start()))
            el.inner_end = m.start()
            el.end = m.end()
            if not stack:
                return el
        elif m.group(2):
            el = Element(_local(m.group(2)), m.start(), m.end(), stack[-1] if stack else None)
            if stack:
                stack[-1].children.append(el)
            if m.group(4) == "/":
                el.inner_start = el.inner_end = m.end()
                el.end = m.end()
                if not stack:
                    return el
            else:
                stack.append(el)
    raise ValueError("no complete root element found")


# --------------------------------------------------------------------------- formatting helpers


def line_start(src: str, pos: int) -> int:
    return src.rfind("\n", 0, pos) + 1


def line_indent(src: str, pos: int) -> str:
    ls = line_start(src, pos)
    i = ls
    while i < len(src) and src[i] in " \t":
        i += 1
    return src[ls:i]


def indent_unit(src: str, root: Element) -> str:
    """Indentation step used by the file, read from the root's first child."""
    for c in root.children:
        ind = line_indent(src, c.start)
        if ind and src[line_start(src, c.start):c.start].strip() == "":
            return ind
    return "  "


def child_indent(src: str, el: Element, root: Element) -> str:
    for c in el.children:
        if src[line_start(src, c.start):c.start].strip() == "":
            return line_indent(src, c.start)
    return line_indent(src, el.start) + indent_unit(src, root)


def element_span_with_line(src: str, el: Element) -> Tuple[int, int]:
    """Offsets of the element extended to whole lines when it sits alone on them."""
    a, b = el.start, el.end
    ls = line_start(src, a)
    if src[ls:a].strip() == "":
        a = ls
    nl = src.find("\n", b)
    if nl >= 0 and src[b:nl].strip() == "":
        b = nl + 1
    return a, b


def remove_element(src: str, el: Element) -> str:
    a, b = element_span_with_line(src, el)
    return src[:a] + src[b:]


def _indent_block(block: str, indent: str, unit: str = "  ") -> str:
    """Re-indent a block written with two spaces per level to the file's own indentation unit."""
    out = []
    for line in block.strip("\n").splitlines():
        if not line.strip():
            out.append("\n")
            continue
        stripped = line.lstrip(" ")
        level = (len(line) - len(stripped)) // 2
        out.append(indent + unit * level + stripped + "\n")
    return "".join(out)


def insert_child(src: str, parent: Element, block: str, root: Element, first: bool = False) -> str:
    """Insert a block of XML (two spaces per level) as a child of parent, keeping the file's indentation."""
    ind = child_indent(src, parent, root)
    text = _indent_block(block, ind, indent_unit(src, root))
    if parent.inner_start == parent.inner_end and src[parent.inner_end - 2:parent.inner_end] == "/>":
        # self-closing element: expand it
        name = src[parent.start + 1:].split()[0].rstrip("/>")
        own = line_indent(src, parent.start)
        return src[:parent.start] + "<%s>\n%s%s</%s>" % (name, text, own, name) + src[parent.end:]
    if "\n" not in src[parent.inner_start:parent.inner_end]:
        own = line_indent(src, parent.start)
        return src[:parent.inner_start] + "\n" + text + own + src[parent.inner_end:]
    if first and parent.children:
        pos = line_start(src, parent.children[0].start)
        return src[:pos] + text + src[pos:]
    pos = line_start(src, parent.inner_end)
    return src[:pos] + text + src[pos:]


def replace_element(src: str, el: Element, block: str, root: Element) -> str:
    ind = line_indent(src, el.start)
    text = _indent_block(block, ind, indent_unit(src, root))
    a, b = element_span_with_line(src, el)
    return src[:a] + text + src[b:]


def set_text(src: str, el: Element, text: str) -> str:
    """Replace the text of a leaf element, leaving its tags, its position and the rest of its line untouched."""
    return src[:el.inner_start] + text + src[el.inner_end:]


def insert_after(src: str, sibling: Element, block: str, root: Element) -> str:
    """Insert a block of XML (two spaces per level) right after a sibling element.

    When the sibling ends its line (a trailing comment stays on that line) the block goes on its own
    line(s) below it, with the sibling's indentation. When other content follows on the same line the
    element is written inline, in the same compact style."""
    nl = src.find("\n", sibling.end)
    rest = (src[sibling.end:nl] if nl >= 0 else src[sibling.end:]).strip()
    if nl >= 0 and (rest == "" or (rest.startswith("<!--") and rest.endswith("-->") and rest.count("<!--") == 1)):
        if src[line_start(src, sibling.start):sibling.start].strip() == "" or sibling.parent is None:
            ind = line_indent(src, sibling.start)
        else:
            ind = child_indent(src, sibling.parent, root)
        return src[:nl + 1] + _indent_block(block, ind, indent_unit(src, root)) + src[nl + 1:]
    return src[:sibling.end] + "".join(l.strip() for l in block.strip().splitlines()) + src[sibling.end:]


def ensure_child(src: str, parent_path: List[str], root_tag_block: Dict[str, str]) -> Tuple[str, Element]:
    """Make sure the element path (below the root) exists, creating empty containers as needed.

    Returns the new source and the (re-parsed) target element."""
    root = parse(src)
    el = root
    for tag in parent_path:
        nxt = el.child(tag)
        if nxt is None:
            src = insert_child(src, el, root_tag_block.get(tag, "<%s>\n</%s>" % (tag, tag)), root)
            root = parse(src)
            el = root
            for t in parent_path[:parent_path.index(tag) + 1]:
                el = el.child(t)
        else:
            el = nxt
    return src, el


# --------------------------------------------------------------------------- POM level helpers


def dependency_ga(dep: Element, src: str, project_group: Optional[str]) -> Tuple[str, str]:
    g = dep.child_text(src, "groupId") or ""
    a = dep.child_text(src, "artifactId") or ""
    if g in ("${project.groupId}", "${pom.groupId}", "${groupId}") and project_group:
        g = project_group
    return g, a


def project_group_id(root: Element, src: str) -> Optional[str]:
    g = root.child_text(src, "groupId")
    if g:
        return g
    parent = root.child("parent")
    return parent.child_text(src, "groupId") if parent is not None else None


def find_dependency(root: Element, src: str, g: str, a: str) -> Optional[Element]:
    deps = root.child("dependencies")
    if deps is None:
        return None
    pg = project_group_id(root, src)
    for d in deps.children_named("dependency"):
        if dependency_ga(d, src, pg) == (g, a):
            return d
    return None


def profile_dependencies(root: Element, src: str) -> List[Tuple[str, Element]]:
    """("profile <id>", element) for every dependency declared inside a profile of the POM."""
    out: List[Tuple[str, Element]] = []
    profiles = root.child("profiles")
    for profile in (profiles.children_named("profile") if profiles is not None else []):
        deps = profile.child("dependencies")
        if deps is None:
            continue
        place = "profile %s" % (profile.child_text(src, "id") or "?")
        out += [(place, d) for d in deps.children_named("dependency")]
    return out


def dependency_declarations(root: Element, src: str, g: str, a: str) -> List[Tuple[str, Element]]:
    """Every declaration of g:a in the POM: ("", element) for the project's own <dependencies>, then
    ("profile <id>", element) for each profile that declares it. While a profile is active its
    declaration replaces the main one wholesale, exclusions included, so each of them counts."""
    pg = project_group_id(root, src)
    found: List[Tuple[str, Element]] = []
    deps = root.child("dependencies")
    if deps is not None:
        found += [("", d) for d in deps.children_named("dependency") if dependency_ga(d, src, pg) == (g, a)]
    found += [(place, d) for place, d in profile_dependencies(root, src) if dependency_ga(d, src, pg) == (g, a)]
    return found


def dependency_xml(g: str, a: str, version: Optional[str] = None, scope: Optional[str] = None, type_: Optional[str] = None,
                   classifier: Optional[str] = None, exclusions=(), comment: Optional[str] = None) -> str:
    """A <dependency> block, two spaces per level, for insert_child / insert_after. A test-jar needs no
    classifier element (it is the tests classifier); compile scope and the jar type are left implicit."""
    lines = ["<!-- %s -->" % comment.replace("--", "-")] if comment else []
    lines += ["<dependency>", "  <groupId>%s</groupId>" % g, "  <artifactId>%s</artifactId>" % a]
    if version:
        lines.append("  <version>%s</version>" % version)
    if type_ and type_ != "jar":
        lines.append("  <type>%s</type>" % type_)
    if classifier and not (type_ == "test-jar" and classifier == "tests"):
        lines.append("  <classifier>%s</classifier>" % classifier)
    if scope and scope != "compile":
        lines.append("  <scope>%s</scope>" % scope)
    if exclusions:
        lines.append("  <exclusions>")
        for t in exclusions:
            tg, ta = t.split(":", 1)
            lines += ["    <exclusion>", "      <groupId>%s</groupId>" % tg, "      <artifactId>%s</artifactId>" % ta, "    </exclusion>"]
        lines.append("  </exclusions>")
    lines.append("</dependency>")
    return "\n".join(lines)


def existing_exclusions(dep: Element, src: str) -> List[Tuple[str, str]]:
    exc = dep.child("exclusions")
    if exc is None:
        return []
    return [(e.child_text(src, "groupId") or "", e.child_text(src, "artifactId") or "") for e in exc.children_named("exclusion")]
