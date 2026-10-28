#!/usr/bin/env python3
"""Shared helpers for the mvn-rebundle pipeline.

Standard library only, Python 3.8+. Every script in this directory adds its own
directory to sys.path before importing this module, so the scripts also work when
run with ``python -I``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- constants

SIGNATURE_PATTERNS = [
    "META-INF/*.SF",
    "META-INF/*.RSA",
    "META-INF/*.DSA",
    "META-INF/*.EC",
    "META-INF/SIG-*",
]
MODULE_INFO_PATTERNS = ["module-info.class", "META-INF/versions/*/module-info.class"]
LEGAL_PATTERNS = [
    "META-INF/LICENSE",
    "META-INF/LICENSE.*",
    "META-INF/LICENSE-*",
    "META-INF/NOTICE",
    "META-INF/NOTICE.*",
    "META-INF/NOTICE-*",
    "META-INF/DEPENDENCIES",
    "META-INF/DEPENDENCIES.*",
    # lowercase variants some jars carry (matching is case-sensitive on Linux)
    "META-INF/license",
    "META-INF/license.*",
    "META-INF/license-*",
    "META-INF/notice",
    "META-INF/notice.*",
    "META-INF/notice-*",
]
SERVICES_PREFIX = "META-INF/services/"
# Resources Shade merges by appending the files (AppendingTransformer). spring-autoconfigure-metadata.properties
# has one key per auto-configuration class, so appending the files is a correct merge.
APPEND_RESOURCES = [
    "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports",
    "META-INF/spring-autoconfigure-metadata.properties",
    "META-INF/spring.handlers",
    "META-INF/spring.schemas",
    "META-INF/spring.tooling",
]
# Log4j 2 plugin cache: Log4j reads every copy at start-up, so copies from several jars are merged by Log4j's
# own Shade transformer (log4j-transform-maven-shade-plugin-extensions) instead of collapsing to the first.
LOG4J_PLUGIN_CACHE = "META-INF/org/apache/logging/log4j/core/config/plugins/Log4j2Plugins.dat"
# Duplicated resources nothing reads at run time (IDE and documentation metadata): collapsing them is harmless.
COLLAPSE_HARMLESS = [
    "META-INF/spring-configuration-metadata.json",
    "META-INF/additional-spring-configuration-metadata.json",
    "META-INF/DISCLAIMER",
    "META-INF/DISCLAIMER.*",
    "META-INF/eclipse.inf",
    "about.html",
]
# Properties files whose values for the same key must be merged
# (Spring Boot's PropertiesMergingResourceTransformer).
PROPERTIES_MERGE_RESOURCES = [
    "META-INF/spring.factories",
    "META-INF/spring/aot.factories",
    "META-INF/spring.components",
    "META-INF/spring-devtools.properties",
]
NATIVE_SUFFIXES = (".so", ".dll", ".dylib", ".jnilib")
THIRD_PARTY_DIR = "META-INF/third-party/"
BUNDLE_CONTENTS_ATTR = "X-Dependency-Bundle-Contents"
BUNDLE_SOURCE_ATTR = "X-Dependency-Bundle-Source"
BUNDLE_BOOT_ATTR = "X-Dependency-Bundle-Spring-Boot"
# written by the rework job: the original build a bundle descends from, and the bundle it was derived from
BUNDLE_BASE_ATTR = "X-Dependency-Bundle-Base"
BUNDLE_REWORKED_FROM_ATTR = "X-Dependency-Bundle-Reworked-From"
BUNDLE_CONTENTS_FILE = THIRD_PARTY_DIR + "BUNDLE-CONTENTS.txt"

# --------------------------------------------------------------------------- small utils

for _stream in (sys.stdout, sys.stderr):  # policy messages may be non-ASCII; never crash on a cp1252 console
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    print("ERROR: " + msg, file=sys.stderr, flush=True)
    sys.exit(code)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def gav_str(g: dict) -> str:
    s = "%s:%s:%s" % (g.get("groupId"), g.get("artifactId"), g.get("version"))
    if g.get("classifier"):
        s += ":" + g["classifier"]
    return s


def ga_str(g: dict) -> str:
    return "%s:%s" % (g.get("groupId"), g.get("artifactId"))


# --------------------------------------------------------------------------- ant-style globs

_glob_cache: Dict[str, "re.Pattern[str]"] = {}


def ant_match(pattern: str, name: str) -> bool:
    """Ant/Shade style matching: '*' does not cross '/', '**' does."""
    rx = _glob_cache.get(pattern)
    if rx is None:
        out = []
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "*":
                if pattern[i : i + 2] == "**":
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            else:
                out.append(re.escape(c))
            i += 1
        rx = re.compile("^" + "".join(out) + "$")
        _glob_cache[pattern] = rx
    return rx.match(name) is not None


def match_any(name: str, patterns) -> bool:
    return any(ant_match(p, name) for p in patterns)


def classify_entry(name: str) -> str:
    """Category of a jar entry.

    dir | manifest | signature | index | module-info | legal | services | append |
    props-merge | log4j-plugins | class | resource
    """
    if name.endswith("/"):
        return "dir"
    if name == "META-INF/MANIFEST.MF":
        return "manifest"
    if match_any(name, SIGNATURE_PATTERNS):
        return "signature"
    if name == "META-INF/INDEX.LIST":
        return "index"
    if match_any(name, MODULE_INFO_PATTERNS):
        return "module-info"
    if match_any(name, LEGAL_PATTERNS):
        return "legal"
    if name.startswith(SERVICES_PREFIX) and name.count("/") == 2:
        return "services"
    if name in APPEND_RESOURCES:
        return "append"
    if name in PROPERTIES_MERGE_RESOURCES:
        return "props-merge"
    if name == LOG4J_PLUGIN_CACHE:
        return "log4j-plugins"
    if name.endswith(".class"):
        return "class"
    return "resource"


# --------------------------------------------------------------------------- java properties


def _split_kv(line: str) -> Tuple[str, str]:
    i, n, key = 0, len(line), []
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            key.append(c)
            key.append(line[i + 1])
            i += 2
            continue
        if c in "=: \t\f":
            break
        key.append(c)
        i += 1
    while i < n and line[i] in " \t\f":
        i += 1
    if i < n and line[i] in "=:":
        i += 1
    while i < n and line[i] in " \t\f":
        i += 1
    return "".join(key), line[i:]


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "f": "\f"}


def _unescape(s: str) -> str:
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            d = s[i + 1]
            if d == "u" and i + 5 < n + 0:
                try:
                    out.append(chr(int(s[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(_ESCAPES.get(d, d))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_properties(text: str) -> Dict[str, str]:
    """Minimal java.util.Properties parser: comments, continuations, =/:/space separators."""
    result: Dict[str, str] = {}
    logical: List[str] = []
    buf: Optional[str] = None
    for raw in text.splitlines():
        line = raw.lstrip()
        if buf is None:
            if not line or line[0] in "#!":
                continue
            buf = line
        else:
            buf += line
        trailing = len(buf) - len(buf.rstrip("\\"))
        if trailing % 2 == 1:
            buf = buf[:-1]
            continue
        logical.append(buf)
        buf = None
    if buf is not None:
        logical.append(buf)
    for l in logical:
        k, v = _split_kv(l)
        result[_unescape(k)] = _unescape(v)
    return result


def properties_values(props: Dict[str, str]) -> Dict[str, List[str]]:
    """spring.factories style: comma separated values per key."""
    return {k: [x.strip() for x in v.split(",") if x.strip()] for k, v in props.items()}


def significant_lines(text: str) -> List[str]:
    """Non-empty, non-comment lines of a services / imports style file."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


# --------------------------------------------------------------------------- manifests


def parse_manifest(data: bytes) -> Dict[str, str]:
    """Main section attributes of a MANIFEST.MF (continuation lines unwrapped)."""
    text = data.decode("utf-8", "replace")
    lines: List[str] = []
    for raw in text.splitlines():
        if raw.startswith(" ") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    attrs: Dict[str, str] = {}
    for line in lines:
        if not line.strip():
            break  # end of main section
        if ":" in line:
            k, v = line.split(":", 1)
            attrs[k.strip()] = v.strip()
    return attrs


def read_manifest(zf: zipfile.ZipFile) -> Dict[str, str]:
    try:
        return parse_manifest(zf.read("META-INF/MANIFEST.MF"))
    except KeyError:
        return {}


def parse_contents_attr(text: str) -> List[dict]:
    """gav dicts from the X-Dependency-Bundle-Contents manifest attribute (comma separated g:a:v[:classifier])."""
    out: List[dict] = []
    for tok in (text or "").replace(",", "\n").split():
        p = tok.strip().split(":")
        if len(p) < 3 or not all(p[:3]):
            continue
        g = {"groupId": p[0], "artifactId": p[1], "version": p[2]}
        if len(p) > 3 and p[3]:
            g["classifier"] = p[3]
        out.append(g)
    return out


def parse_contents_file(text: str) -> Dict[str, dict]:
    """{coordinate: {"sha256", "file"}} from META-INF/third-party/BUNDLE-CONTENTS.txt (coordinate, sha256, file name)."""
    out: Dict[str, dict] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = {"sha256": parts[1], "file": parts[2] if len(parts) > 2 else None}
    return out


# --------------------------------------------------------------------------- GAV identification

_QUALIFIER = re.compile(
    r"^(snapshot|final|ga|release|rc\d*|m\d+|alpha\d*|beta\d*|cr\d*|sp\d*|incubating|jre\d*|android|\d.*)$",
    re.I,
)


def split_filename(base: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """'commons-io-2.16.1' -> ('commons-io', '2.16.1', None)
    'netty-tcnative-2.0.65.Final-linux-x86_64' -> (..., '2.0.65.Final', 'linux-x86_64')"""
    segs = base.split("-")
    for i in range(1, len(segs)):
        if segs[i][:1].isdigit():
            artifact = "-".join(segs[:i])
            rest = segs[i:]
            ver = [rest[0]]
            j = 1
            while j < len(rest) and _QUALIFIER.match(rest[j]):
                ver.append(rest[j])
                j += 1
            classifier = "-".join(rest[j:]) or None
            return artifact, "-".join(ver), classifier
    return None


def _gav(c: dict, classifier: Optional[str]) -> dict:
    g = {"groupId": c["groupId"], "artifactId": c["artifactId"], "version": c["version"]}
    if classifier:
        g["classifier"] = classifier
    return g


def identify_gav(zf: zipfile.ZipFile, filename: str) -> Tuple[Optional[dict], str, List[dict]]:
    """Identify the Maven coordinates of a jar.

    Returns (gav or None, source, candidates). Primary evidence is the
    META-INF/maven/<g>/<a>/pom.properties whose artifactId-version matches the file
    name (shaded jars carry several). Fallbacks only produce candidates.
    """
    base = filename[:-4] if filename.lower().endswith(".jar") else filename
    candidates: List[dict] = []
    for n in zf.namelist():
        if n.startswith("META-INF/maven/") and n.endswith("/pom.properties"):
            try:
                props = parse_properties(zf.read(n).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
            g, a, v = props.get("groupId"), props.get("artifactId"), props.get("version")
            if g and a and v:
                candidates.append(
                    {"groupId": g, "artifactId": a, "version": v, "source": "pom.properties", "path": n}
                )
    matches: List[Tuple[dict, Optional[str]]] = []
    for c in candidates:
        prefix = "%s-%s" % (c["artifactId"], c["version"])
        if base == prefix:
            matches.append((c, None))
        elif base.startswith(prefix + "-"):
            matches.append((c, base[len(prefix) + 1 :]))
    if matches:
        c, classifier = matches[0]
        return _gav(c, classifier), "pom.properties", candidates
    if len(candidates) == 1 and base.startswith(candidates[0]["artifactId"]):
        fn = split_filename(base)
        return _gav(candidates[0], fn[2] if fn else None), "pom.properties", candidates
    fn = split_filename(base)
    if fn:
        a, v, classifier = fn
        candidates.append(
            {"groupId": None, "artifactId": a, "version": v, "classifier": classifier, "source": "filename"}
        )
    man = read_manifest(zf)
    title = man.get("Implementation-Title") or man.get("Bundle-SymbolicName")
    if title:
        candidates.append(
            {
                "groupId": man.get("Implementation-Vendor-Id"),
                "artifactId": title,
                "version": man.get("Implementation-Version") or man.get("Bundle-Version"),
                "source": "manifest",
            }
        )
    return None, "unknown", candidates


def declared_dependencies(zf: zipfile.ZipFile, gav: dict) -> List[dict]:
    """Runtime dependencies declared in the jar's own embedded pom.xml (compile/runtime, not optional)."""
    path = "META-INF/maven/%s/%s/pom.xml" % (gav.get("groupId"), gav.get("artifactId"))
    try:
        data = zf.read(path)
    except KeyError:
        return []
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(data)
    except Exception:  # noqa: BLE001
        return []

    def local(tag: str) -> str:
        return tag.split("}", 1)[-1]

    def child(el, name):
        for c in el:
            if local(c.tag) == name:
                return c
        return None

    subst = {"${project.groupId}": gav.get("groupId"), "${pom.groupId}": gav.get("groupId"),
             "${project.artifactId}": gav.get("artifactId"), "${pom.artifactId}": gav.get("artifactId")}
    deps = child(root, "dependencies")
    out: List[dict] = []
    if deps is None:
        return out
    for dep in deps:
        f = {local(c.tag): (c.text or "").strip() for c in dep}
        g, a = f.get("groupId", ""), f.get("artifactId", "")
        g, a = subst.get(g, g), subst.get(a, a)
        if not g or not a or "${" in g or "${" in a:
            continue
        if f.get("scope", "compile") in ("test", "provided", "system") or f.get("optional", "").lower() == "true":
            continue
        out.append({"groupId": g, "artifactId": a, "scope": f.get("scope", "compile")})
    return out


def scan_jar(zf: zipfile.ZipFile) -> dict:
    """Facts about a jar that matter for merging."""
    man = read_manifest(zf)
    flags = {
        "signed": False,
        "module_info": False,
        "multi_release": man.get("Multi-Release", "").strip().lower() == "true",
        "automatic_module_name": man.get("Automatic-Module-Name"),
        "native_libs": [],
        "services": [],
        "append_resources": [],
        "props_merge_resources": [],
        "log4j_plugin_cache": False,
        "nested_jars": [],
        "class_count": 0,
        "entry_count": 0,
    }
    for n in zf.namelist():
        cat = classify_entry(n)
        if cat == "dir":
            continue
        flags["entry_count"] += 1
        if cat == "signature":
            flags["signed"] = True
        elif cat == "module-info":
            flags["module_info"] = True
        elif cat == "services":
            flags["services"].append(n[len(SERVICES_PREFIX) :])
        elif cat == "append":
            flags["append_resources"].append(n)
        elif cat == "props-merge":
            flags["props_merge_resources"].append(n)
        elif cat == "log4j-plugins":
            flags["log4j_plugin_cache"] = True
        elif cat == "class":
            flags["class_count"] += 1
        if n.lower().endswith(".jar"):
            flags["nested_jars"].append(n)
        if n.lower().endswith(NATIVE_SUFFIXES):
            flags["native_libs"].append(n)
    return flags


# --------------------------------------------------------------------------- application artifacts


class DepJar:
    """A dependency jar inside an application (fat jar entry or a file on disk)."""

    def __init__(self, name: str, path_in_app: str, loader: Callable[[], bytes]):
        self.name = name
        self.path_in_app = path_in_app
        self._loader = loader
        self._data: Optional[bytes] = None
        self._sha: Optional[str] = None

    @property
    def data(self) -> bytes:
        if self._data is None:
            self._data = self._loader()
        return self._data

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        if self._sha is None:
            self._sha = sha256_bytes(self.data)
        return self._sha

    def open(self) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(self.data))


class AppArtifact:
    """Spring Boot application in one of the supported layouts.

    boot-jar      executable jar (BOOT-INF/lib, BOOT-INF/classes)
    boot-war      executable war (WEB-INF/lib, WEB-INF/lib-provided, WEB-INF/classes)
    boot-exploded directory with BOOT-INF/lib (buildpacks /workspace, `jar -xf`)
    war-exploded  directory with WEB-INF/lib
    lib-dir       directory whose jars are the dependencies (Jib: /app/libs + /app/classes)
    """

    def __init__(self, path):
        self.path = Path(path)
        self._zip: Optional[zipfile.ZipFile] = None
        self._order: Optional[Tuple[List[str], str]] = None
        self.lib_prefixes: List[str] = []
        self.classes_prefix: Optional[str] = None
        if self.path.is_file():
            self._zip = zipfile.ZipFile(self.path)
            names = self._zip.namelist()
            if any(n.startswith("BOOT-INF/lib/") and n.lower().endswith(".jar") for n in names):
                self.layout = "boot-jar"
                self.lib_prefixes = ["BOOT-INF/lib/"]
                self.classes_prefix = "BOOT-INF/classes/"
            elif any(n.startswith("WEB-INF/lib") and n.lower().endswith(".jar") for n in names):
                self.layout = "boot-war"
                self.lib_prefixes = ["WEB-INF/lib/", "WEB-INF/lib-provided/"]
                self.classes_prefix = "WEB-INF/classes/"
            else:
                raise ValueError(
                    "%s is not a Spring Boot archive: no BOOT-INF/lib or WEB-INF/lib entries" % self.path
                )
        elif self.path.is_dir():
            if (self.path / "BOOT-INF" / "lib").is_dir():
                self.layout = "boot-exploded"
                self.lib_prefixes = ["BOOT-INF/lib/"]
                self.classes_prefix = "BOOT-INF/classes/"
            elif (self.path / "WEB-INF" / "lib").is_dir():
                self.layout = "war-exploded"
                self.lib_prefixes = ["WEB-INF/lib/", "WEB-INF/lib-provided/"]
                self.classes_prefix = "WEB-INF/classes/"
            else:
                self.layout = "lib-dir"
                self.lib_prefixes = self._find_lib_dirs()
                if not self.lib_prefixes:
                    raise ValueError("%s contains no jar files" % self.path)
                for cand in ("classes/", "BOOT-INF/classes/"):
                    if (self.path / cand).is_dir():
                        self.classes_prefix = cand
                        break
        else:
            raise FileNotFoundError(str(self.path))

    # -- helpers ---------------------------------------------------------------------------
    def _find_lib_dirs(self) -> List[str]:
        found = set()
        root = self.path
        for p in root.rglob("*.jar"):
            rel = p.relative_to(root).as_posix()
            if rel.count("/") > 3:
                continue
            found.add(rel.rsplit("/", 1)[0] + "/" if "/" in rel else "")
        return sorted(found)

    @property
    def is_archive(self) -> bool:
        return self._zip is not None

    def manifest(self) -> Dict[str, str]:
        """Main attributes of the application's own META-INF/MANIFEST.MF; empty when there is none."""
        try:
            if self._zip is not None:
                return parse_manifest(self._zip.read("META-INF/MANIFEST.MF")) if "META-INF/MANIFEST.MF" in self._zip.namelist() else {}
            p = self.path / "META-INF" / "MANIFEST.MF"
            return parse_manifest(p.read_bytes()) if p.is_file() else {}
        except Exception:  # noqa: BLE001
            return {}

    def file_sha256(self) -> Optional[str]:
        return sha256_file(self.path) if self.path.is_file() else None

    def _under_lib(self, rel: str) -> bool:
        return any(rel.startswith(p) for p in self.lib_prefixes)

    # -- dependency jars -------------------------------------------------------------------
    def _raw_dependency_jars(self) -> List[DepJar]:
        jars: List[DepJar] = []
        if self._zip is not None:
            zf = self._zip
            for n in zf.namelist():
                if self._under_lib(n) and n.lower().endswith(".jar"):
                    jars.append(DepJar(n.rsplit("/", 1)[-1], n, lambda n=n: zf.read(n)))
        else:
            for prefix in self.lib_prefixes:
                d = self.path / prefix if prefix else self.path
                if not d.is_dir():
                    continue
                for p in sorted(d.iterdir()):
                    if p.is_file() and p.suffix.lower() == ".jar":
                        rel = p.relative_to(self.path).as_posix()
                        jars.append(DepJar(p.name, rel, lambda p=p: p.read_bytes()))
        return jars

    def classpath_order(self) -> Tuple[List[str], str]:
        """Dependency jar names in class-loading order, and where that order comes from.

        Spring Boot writes BOOT-INF/classpath.idx and its launcher follows it; Jib writes
        jib-classpath-file. Without either, the archive / directory order is used."""
        if self._order is not None:
            return self._order

        def parse_idx(data: bytes) -> List[str]:
            out = []
            for raw in data.decode("utf-8", "replace").splitlines():
                line = raw.strip()
                if line.startswith("- "):
                    line = line[2:].strip()
                line = line.strip('"')
                if line.lower().endswith(".jar"):
                    out.append(line.rsplit("/", 1)[-1])
            return out

        names, source = None, ""
        if self._zip is not None:
            for idx in ("BOOT-INF/classpath.idx", "WEB-INF/classpath.idx"):
                if idx in self._zip.namelist():
                    names, source = parse_idx(self._zip.read(idx)), idx
                    break
        else:
            for idx in ("BOOT-INF/classpath.idx", "WEB-INF/classpath.idx"):
                p = self.path / idx
                if p.is_file():
                    names, source = parse_idx(p.read_bytes()), idx
                    break
            if names is None and (self.path / "jib-classpath-file").is_file():
                text = (self.path / "jib-classpath-file").read_text(encoding="utf-8", errors="replace")
                names = [e.strip().rsplit("/", 1)[-1] for e in text.replace("\n", ":").split(":") if e.strip().lower().endswith(".jar")]
                source = "jib-classpath-file"
        if names is None:
            names = [j.name for j in self._raw_dependency_jars()]
            source = "archive-order" if self._zip is not None else "directory-order"
        self._order = (names, source)
        return self._order

    def dependency_jars(self) -> List[DepJar]:
        """Dependency jars in class-loading order (see classpath_order)."""
        jars = self._raw_dependency_jars()
        order, _ = self.classpath_order()
        position = {n: i for i, n in enumerate(order)}
        jars.sort(key=lambda j: (position.get(j.name, len(order)), j.name))
        return jars

    # -- everything else -------------------------------------------------------------------
    def app_entries(self) -> Iterator[Tuple[str, Callable[[], bytes]]]:
        """All non-dependency file entries (loader classes, BOOT-INF/classes, manifest...)."""
        if self._zip is not None:
            zf = self._zip
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                if self._under_lib(n) and n.lower().endswith(".jar"):
                    continue
                yield n, (lambda n=n: zf.read(n))
        else:
            for p in sorted(self.path.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(self.path).as_posix()
                if self._under_lib(rel) and p.suffix.lower() == ".jar":
                    continue
                yield rel, (lambda p=p: p.read_bytes())

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def spring_boot_version(dependencies: List[dict]) -> Optional[str]:
    """Version of the dependency identified as org.springframework.boot:spring-boot, if any."""
    for d in dependencies:
        g = d.get("gav") or {}
        if g.get("groupId") == "org.springframework.boot" and g.get("artifactId") == "spring-boot":
            return g.get("version")
    return None


def spring_boot_version_from_app(app: "AppArtifact", jar_names) -> Tuple[Optional[str], Optional[str]]:
    """(version, where it comes from) before any jar is identified: the application's own manifest carries
    Spring-Boot-Version in every archive the Boot Maven and Gradle plugins write; a layout without that
    manifest (Jib) still names the spring-boot jar. Spring's own jars are built with Gradle and carry no
    pom.properties, so the version must not depend on identifying them."""
    version = app.manifest().get("Spring-Boot-Version")
    if version:
        return version, "application manifest Spring-Boot-Version"
    for name in jar_names:
        fn = split_filename(name[:-4] if name.lower().endswith(".jar") else name)
        if fn and fn[0] == "spring-boot":
            return fn[1], "spring-boot jar name"
    return None, None


def parse_log4j_plugin_cache(data: bytes) -> "Dict[str, Dict[str, tuple]]":
    """{category: {key: (className, name, printable, defer)}} of a Log4j2Plugins.dat, the DataOutputStream
    format PluginCache.writeCache produces (int count, then per category: UTF name, int count, per plugin:
    UTF key, UTF class, UTF name, boolean printable, boolean defer)."""
    import struct

    pos = 0

    def read_int():
        nonlocal pos
        v = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
        return v

    def read_utf():
        nonlocal pos
        n = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        s = data[pos:pos + n].decode("utf-8", "replace")
        pos += n
        return s

    def read_bool():
        nonlocal pos
        v = data[pos] != 0
        pos += 1
        return v

    categories: Dict[str, Dict[str, tuple]] = {}
    for _ in range(read_int()):
        category = read_utf()
        entries = categories.setdefault(category, {})
        for _ in range(read_int()):
            key, cls, pname = read_utf(), read_utf(), read_utf()
            entries[key] = (cls, pname, read_bool(), read_bool())
    return categories


def write_log4j_plugin_cache(categories: "Dict[str, Dict[str, tuple]]") -> bytes:
    """The inverse of parse_log4j_plugin_cache, categories and keys sorted as Log4j's TreeMaps are."""
    import struct

    def utf(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack(">H", len(b)) + b

    out = [struct.pack(">i", len(categories))]
    for category in sorted(categories):
        entries = categories[category]
        out.append(utf(category) + struct.pack(">i", len(entries)))
        for key in sorted(entries):
            cls, pname, printable, defer = entries[key]
            out.append(utf(key) + utf(cls) + utf(pname) + bytes([1 if printable else 0, 1 if defer else 0]))
    return b"".join(out)


# --------------------------------------------------------------------------- selection tokens


def parse_selection_text(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        for tok in re.split(r"[,;\s]+", line):
            tok = tok.strip()
            if tok:
                tokens.append(tok)
    return tokens


def match_token(token: str, dependencies: List[dict]) -> Tuple[List[dict], Optional[dict]]:
    """Match a selection token (a | g:a | g:a:v | g:a:v:c) against inventory entries.

    Returns (matches, explicit) where explicit is the parsed g:a:v[:c] when given.
    """
    parts = token.split(":")
    explicit = None
    matches = []
    if len(parts) == 1:
        a = parts[0]
        for d in dependencies:
            gav = d.get("gav")
            if gav and gav["artifactId"] == a:
                matches.append(d)
            elif not gav and any(c.get("artifactId") == a for c in d.get("candidates", [])):
                matches.append(d)
    elif len(parts) == 2:
        g, a = parts
        for d in dependencies:
            gav = d.get("gav")
            if gav and gav["groupId"] == g and gav["artifactId"] == a:
                matches.append(d)
            elif not gav and any(c.get("artifactId") == a and c.get("groupId") in (g, None) for c in d.get("candidates", [])):
                matches.append(d)
    elif len(parts) in (3, 4):
        g, a, v = parts[0], parts[1], parts[2]
        c = parts[3] if len(parts) == 4 else None
        explicit = {"groupId": g, "artifactId": a, "version": v}
        if c:
            explicit["classifier"] = c
        for d in dependencies:
            gav = d.get("gav")
            if gav and gav["groupId"] == g and gav["artifactId"] == a and gav["version"] == v and gav.get("classifier") == c:
                matches.append(d)
            elif not gav and any(
                cand.get("artifactId") == a and cand.get("version") == v and cand.get("groupId") in (g, None)
                for cand in d.get("candidates", [])
            ):
                matches.append(d)
    return matches, explicit


# --------------------------------------------------------------------------- maven dependency tree

_TREE_PREFIXES = ("+- ", "\\- ", "|  ", "   ")


def parse_coordinate(coord: str) -> Optional[dict]:
    parts = coord.strip().split(":")
    if len(parts) < 4:
        return None
    node = {"groupId": parts[0], "artifactId": parts[1], "type": parts[2], "classifier": None, "scope": None}
    if len(parts) == 4:
        node["version"] = parts[3]
    elif len(parts) == 5:
        node["version"], node["scope"] = parts[3], parts[4]
    else:
        node["classifier"], node["version"], node["scope"] = parts[3], parts[4], parts[5]
    return node


def parse_dependency_trees(text: str) -> List[dict]:
    """Parse `mvn dependency:tree -DoutputType=text [-Dverbose]` output (one or more modules).

    Returns a list of {"root": node, "nodes": [node...]} where every node carries
    depth, omitted (verbose duplicates/conflicts), note and path (root..self).
    """
    trees: List[dict] = []
    stack: List[dict] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line.startswith("[INFO] "):
            line = line[7:]
        elif line.startswith("[INFO]"):
            continue
        if not line.strip():
            continue
        depth, pos = 0, 0
        while line[pos : pos + 3] in _TREE_PREFIXES and pos + 3 <= len(line):
            depth += 1
            pos += 3
        body = line[pos:].strip()
        omitted, note = False, None
        if body.startswith("(") and body.endswith(")"):
            inner = body[1:-1]
            omitted = True
            if " - " in inner:
                body, note = inner.split(" - ", 1)
            else:
                body = inner
        node = parse_coordinate(body)
        if node is None:
            continue
        node["depth"], node["omitted"], node["note"] = depth, omitted, note
        if depth == 0:
            stack = [node]
            node["path"] = [ga_str(node)]
            trees.append({"root": node, "nodes": []})
            continue
        if not trees:
            continue
        stack = stack[:depth]
        if len(stack) < depth:
            # malformed indentation; attach to the deepest known ancestor
            pass
        node["path"] = [ga_str(n) for n in stack] + [ga_str(node)]
        stack.append(node)
        trees[-1]["nodes"].append(node)
    return trees


# --------------------------------------------------------------------------- misc


def rel_to_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def which(cmd: str) -> Optional[str]:
    from shutil import which as _which

    return _which(cmd)


def safe_join(root: Path, rel: str) -> Path:
    """Join a relative archive path to root, refusing traversal."""
    rel_norm = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel_norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("refusing path traversal: %s" % rel)
    return root.joinpath(*parts) if parts else root


if __name__ == "__main__":  # tiny self-check
    assert ant_match("META-INF/*.SF", "META-INF/A.SF")
    assert not ant_match("META-INF/*.SF", "META-INF/x/A.SF")
    assert ant_match("META-INF/versions/*/module-info.class", "META-INF/versions/9/module-info.class")
    assert split_filename("commons-io-2.16.1") == ("commons-io", "2.16.1", None)
    assert split_filename("netty-tcnative-2.0.65.Final-linux-x86_64") == ("netty-tcnative", "2.0.65.Final", "linux-x86_64")
    assert split_filename("spring-core-6.1.14") == ("spring-core", "6.1.14", None)
    assert split_filename("jakarta.annotation-api-2.1.1") == ("jakarta.annotation-api", "2.1.1", None)
    assert split_filename("foo-1.0-SNAPSHOT") == ("foo", "1.0-SNAPSHOT", None)
    assert parse_properties("a=1\nb : 2\n# c\nd=x,\\\n  y\n")["d"] == "x,y"
    print("bundlelib self-check OK")
