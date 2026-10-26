#!/usr/bin/env python3
"""Coordinate identification for jars that carry no Maven descriptor (Tomcat, BouncyCastle, ...).

Candidates come from three places:
  * the Spring Boot BOM of the application's Boot version (artifactId + version -> groupId),
    including the BOMs it imports (Jackson, Netty, ...);
  * the manifest's Implementation-Vendor-Id combined with the file name;
  * the Nexus search API, which maps a SHA-1 to coordinates.
A candidate is confirmed by comparing the repository's `.sha1` of that artifact with the jar's
SHA-1, so a wrong guess can never be accepted silently.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import log, split_filename  # noqa: E402


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


class Repository:
    """Read access to a Maven 2 layout repository (and the Nexus search API next to it)."""

    def __init__(self, repo_url: str, timeout: int = 20, central: bool = False):
        self.repo_url = (repo_url or "").rstrip("/")
        self.timeout = timeout
        self.central = central
        self._cache: Dict[str, Optional[bytes]] = {}
        self._failures = 0
        self.nexus_base = self.repo_url.split("/repository/")[0] if "/repository/" in self.repo_url else ""

    @property
    def enabled(self) -> bool:
        return bool(self.repo_url)

    def _note(self, msg: str) -> None:
        self._failures += 1
        if self._failures <= 5:
            log("  lookup: " + msg)

    def _get(self, url: str, auth: bool = True) -> Optional[bytes]:
        if url in self._cache:
            return self._cache[url]
        req = urllib.request.Request(url, headers={"User-Agent": "mvn-rebundle"})
        user, pw = os.environ.get("SOURCE_USERNAME"), os.environ.get("SOURCE_PASSWORD")
        if auth and user and pw:
            req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode())
        data = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                self._note("HTTP %s for %s" % (e.code, url))
        except (urllib.error.URLError, OSError) as e:
            self._note("%s: %s" % (url, getattr(e, "reason", e)))
        self._cache[url] = data
        return data

    def artifact_url(self, g: str, a: str, v: str, ext: str = "jar", classifier: Optional[str] = None) -> str:
        return "%s/%s/%s/%s/%s-%s%s.%s" % (self.repo_url, g.replace(".", "/"), a, v, a, v, ("-" + classifier) if classifier else "", ext)

    def sha1(self, g: str, a: str, v: str, ext: str = "jar", classifier: Optional[str] = None) -> Optional[str]:
        data = self._get(self.artifact_url(g, a, v, ext, classifier) + ".sha1")
        if not data:
            return None
        text = data.decode("utf-8", "replace").strip()
        return text.split()[0].lower() if text else None

    def pom(self, g: str, a: str, v: str) -> Optional[str]:
        data = self._get(self.artifact_url(g, a, v, "pom"))
        return data.decode("utf-8", "replace") if data else None

    def search_sha1(self, sha1: str) -> List[dict]:
        hits: List[dict] = []
        if self.nexus_base:
            data = self._get("%s/service/rest/v1/search/assets?sha1=%s" % (self.nexus_base, sha1))
            if data:
                try:
                    items = json.loads(data.decode("utf-8", "replace")).get("items", [])
                except ValueError:
                    items = []
                for it in items:
                    m = it.get("maven2") or {}
                    if m.get("groupId") and m.get("artifactId") and m.get("version") and m.get("extension", "jar") == "jar":
                        hits.append({"groupId": m["groupId"], "artifactId": m["artifactId"], "version": m["version"],
                                     "classifier": m.get("classifier") or None})
        if self.central:
            data = self._get('https://search.maven.org/solrsearch/select?q=1:%%22%s%%22&rows=5&wt=json' % sha1, auth=False)
            if data:
                try:
                    docs = json.loads(data.decode("utf-8", "replace"))["response"]["docs"]
                except (ValueError, KeyError, TypeError):
                    docs = []
                for d in docs:
                    if d.get("g") and d.get("a") and d.get("v"):
                        hits.append({"groupId": d["g"], "artifactId": d["a"], "version": d["v"], "classifier": None})
        return hits


class SpringBootBom:
    """artifactId -> {(groupId, version)} from spring-boot-dependencies and the BOMs it imports."""

    def __init__(self, repo: Repository, boot_version: str):
        self.repo = repo
        self.boot_version = boot_version
        self.index: Dict[str, Set[Tuple[str, str]]] = {}
        self.boms: List[str] = []
        self.loaded = False

    def load(self) -> bool:
        text = self.repo.pom("org.springframework.boot", "spring-boot-dependencies", self.boot_version)
        if not text:
            return False
        self.boms.append("org.springframework.boot:spring-boot-dependencies:%s" % self.boot_version)
        self._ingest(text, 0)
        self.loaded = True
        return True

    @staticmethod
    def _child(el, name):
        for c in el:
            if _local(c.tag) == name:
                return c
        return None

    @staticmethod
    def _resolve(value: str, props: Dict[str, str]) -> Optional[str]:
        for _ in range(6):
            if "${" not in value:
                return value
            start = value.find("${")
            end = value.find("}", start)
            if end < 0:
                return None
            rep = props.get(value[start + 2:end])
            if rep is None:
                return None
            value = value[:start] + rep + value[end + 1:]
        return None

    def _ingest(self, text: str, depth: int) -> None:
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except (ET.ParseError, UnicodeError):
            return
        props: Dict[str, str] = {}
        parent = self._child(root, "parent")
        for name in ("groupId", "version"):
            el = self._child(root, name)
            if el is None and parent is not None:
                el = self._child(parent, name)
            if el is not None and el.text:
                props["project." + name] = el.text.strip()
                props["pom." + name] = el.text.strip()
        p = self._child(root, "properties")
        if p is not None:
            for c in p:
                props[_local(c.tag)] = (c.text or "").strip()
        dm = self._child(root, "dependencyManagement")
        deps = self._child(dm, "dependencies") if dm is not None else None
        if deps is None:
            return
        for dep in deps:
            fields = {_local(c.tag): (c.text or "").strip() for c in dep}
            g = self._resolve(fields.get("groupId", ""), props)
            a = fields.get("artifactId")
            v = self._resolve(fields.get("version", ""), props)
            if not (g and a and v):
                continue
            if fields.get("scope") == "import" and fields.get("type") == "pom":
                if depth < 2:
                    sub = self.repo.pom(g, a, v)
                    if sub:
                        self.boms.append("%s:%s:%s" % (g, a, v))
                        self._ingest(sub, depth + 1)
                continue
            self.index.setdefault(a, set()).add((g, v))

    def lookup(self, artifact_id: str, version: Optional[str] = None) -> List[dict]:
        return [{"groupId": g, "artifactId": artifact_id, "version": v}
                for (g, v) in sorted(self.index.get(artifact_id, ())) if version is None or v == version]


def identify_jar(name: str, sha1: str, manifest: Dict[str, str], repo: Optional[Repository],
                 bom: Optional[SpringBootBom]) -> Tuple[Optional[dict], str, str, List[dict]]:
    """(gav or None, source, note, candidates) for a jar without a usable pom.properties."""
    base = name[:-4] if name.lower().endswith(".jar") else name
    fn = split_filename(base)
    candidates: List[Tuple[dict, str]] = []
    if fn:
        a, v, c = fn
        if bom is not None and bom.loaded:
            for hit in bom.lookup(a, v):
                gav = dict(hit)
                if c:
                    gav["classifier"] = c
                candidates.append((gav, "spring-boot-bom"))
        vendor = (manifest.get("Implementation-Vendor-Id") or "").strip()
        if vendor and "." in vendor and " " not in vendor:
            gav = {"groupId": vendor, "artifactId": a, "version": v}
            if c:
                gav["classifier"] = c
            candidates.append((gav, "manifest-vendor-id"))
    if repo is not None and repo.enabled and sha1:
        for hit in repo.search_sha1(sha1):
            candidates.append((hit, "nexus-search"))

    seen = set()
    unique: List[Tuple[dict, str]] = []
    for gav, src in candidates:
        key = (gav["groupId"], gav["artifactId"], gav["version"], gav.get("classifier"))
        if key in seen:
            continue
        seen.add(key)
        unique.append((gav, src))

    if repo is not None and repo.enabled:
        for gav, src in unique:
            s = repo.sha1(gav["groupId"], gav["artifactId"], gav["version"], "jar", gav.get("classifier"))
            if s and s == sha1:
                return gav, src + "+sha1", "confirmed by the repository checksum", [g for g, _ in unique]
    for gav, src in unique:
        if src == "nexus-search":
            return gav, src, "found by checksum in the repository search", [g for g, _ in unique]
    return None, "unknown", "no candidate confirmed", [dict(g, source=s) for g, s in unique]
