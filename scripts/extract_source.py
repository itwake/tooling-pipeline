#!/usr/bin/env python3
"""Stage 1: fetch the application artifact to analyse.

The kind of source and the authentication it needs are detected from --ref alone:

  http(s)://...                 downloaded; basic auth with SOURCE_USERNAME / SOURCE_PASSWORD when
                                the host is one of --nexus-hosts
  an existing local path        a jar / war file, or an exploded / Jib style directory
  groupId:artifactId:version    Maven coordinates, optionally :packaging[:classifier] (the
                                dependency-plugin convention); resolved in --maven-repo-url,
                                SNAPSHOT versions through maven-metadata.xml, basic auth as above
  anything else                 an image reference; --path (or the part after '#' in the
                                reference) is copied out of the image. The registry
                                host decides the login: AWS ECR hosts run `aws ecr get-login-password`
                                (AWS credentials from the environment or the instance role), hosts in
                                --nexus-hosts log in with SOURCE_USERNAME / SOURCE_PASSWORD, other
                                registries are expected to be reachable with the agent's docker config

--detect prints "<kind> <auth> <host>" for --ref and exits, so a pipeline can bind only the
credentials that are needed. --type overrides the detection.
Writes a metadata JSON (--meta) consumed by the following stages.
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Set, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, safe_join, sha256_file, utc_now_iso, which, write_json  # noqa: E402

ECR_HOST = re.compile(r"^\d{12}\.dkr\.ecr(?:-fips)?\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$")
ECR_PUBLIC_HOST = "public.ecr.aws"
EXPLICIT_TYPES = {
    "image": ("image", "none"),
    "ecr-image": ("image", "ecr"),
    "nexus-image": ("image", "nexus"),
    "nexus-maven": ("maven", "nexus"),
    "maven": ("maven", "nexus"),
    "dir": ("dir", "none"),
}


# --------------------------------------------------------------------------- detection


def parse_hosts(spec: str) -> Set[str]:
    """Comma separated host[:port] entries or URLs -> set of lower-case host[:port]."""
    hosts = set()
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "://" in item:
            item = urlparse(item).netloc
        if item:
            hosts.add(item.lower())
    return hosts


def registry_host(ref: str) -> str:
    """Registry host of an image reference; Docker Hub when the reference carries none."""
    first = ref.split("/", 1)[0]
    if "/" not in ref or ("." not in first and ":" not in first and first != "localhost"):
        return "index.docker.io"
    return first


def is_ecr_host(host: str) -> bool:
    return host.lower() == ECR_PUBLIC_HOST or ECR_HOST.match(host.lower()) is not None


def ecr_region(host: str, override: str = "") -> Optional[str]:
    if override:
        return override
    m = ECR_HOST.match(host.lower())
    return m.group("region") if m else None


def detect_source(ref: str, nexus_hosts: Set[str]) -> Tuple[str, str, Optional[str]]:
    """(kind, auth, host) for a reference. kind: url | file | dir | maven | image; auth: none | nexus | ecr."""
    if ref.startswith(("http://", "https://")):
        host = urlparse(ref).netloc.lower()
        return "url", ("nexus" if host in nexus_hosts else "none"), host
    p = Path(ref)
    if p.is_dir():
        return "dir", "none", None
    if p.is_file():
        return "file", "none", None
    if "/" not in ref and "\\" not in ref and ref.count(":") >= 2:
        return "maven", "nexus", None
    host = registry_host(ref)
    if is_ecr_host(host):
        return "image", "ecr", host
    if host.lower() in nexus_hosts:
        return "image", "nexus", host
    return "image", "none", host


def split_ref(ref: str) -> Tuple[str, Optional[str]]:
    """'registry/app:tag#/path/in/image' -> ('registry/app:tag', '/path/in/image').

    URLs and existing local paths are never split."""
    if ref.startswith(("http://", "https://")) or "#" not in ref or Path(ref).exists():
        return ref, None
    base, path = ref.split("#", 1)
    return base, (path or None)


def resolve_type(explicit: str, ref: str, nexus_hosts: Set[str]) -> Tuple[str, str, Optional[str]]:
    if explicit == "auto":
        return detect_source(ref, nexus_hosts)
    if explicit == "jar":
        if ref.startswith(("http://", "https://")):
            host = urlparse(ref).netloc.lower()
            return "url", ("nexus" if host in nexus_hosts else "none"), host
        return "file", "none", None
    kind, auth = EXPLICIT_TYPES[explicit]
    return kind, auth, registry_host(ref) if kind == "image" else None


# --------------------------------------------------------------------------- processes


def run(cmd, check=True, input_text=None):
    log("  $ " + " ".join(cmd) + (" (password on stdin)" if input_text is not None else ""))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, input=input_text)
    if check and p.returncode != 0:
        fail("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), (p.stderr or "")[-2000:]))
    return p


def docker_available() -> bool:
    if not which("docker"):
        return False
    p = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0


def pick_image_tool(requested: str) -> str:
    if requested != "auto":
        return requested
    if docker_available():
        return "docker"
    if which("crane"):
        return "crane"
    fail("neither docker (with a reachable daemon) nor crane is available on PATH")


# --------------------------------------------------------------------------- registry login


def source_credentials() -> Tuple[Optional[str], Optional[str]]:
    return os.environ.get("SOURCE_USERNAME"), os.environ.get("SOURCE_PASSWORD")


def registry_login(host: str, user: str, password: str, tool: str) -> None:
    if tool == "docker":
        cmd = ["docker", "login", "--username", user, "--password-stdin", host]
    else:
        cmd = ["crane", "auth", "login", host, "--username", user, "--password-stdin"]
    run(cmd, input_text=password)


def ecr_login(host: str, region: Optional[str], tool: str) -> None:
    if not which("aws"):
        fail("aws CLI not found on PATH; it is needed to log in to ECR")
    if host.lower() == ECR_PUBLIC_HOST:
        cmd = ["aws", "ecr-public", "get-login-password", "--region", "us-east-1"]
    else:
        cmd = ["aws", "ecr", "get-login-password", "--region", region]
    log("  $ " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        fail("aws ecr get-login-password failed (%d): %s" % (p.returncode, (p.stderr or "").strip()[-1500:]))
    registry_login(host, "AWS", p.stdout.strip(), tool)


def nexus_login(host: str, tool: str) -> None:
    user, password = source_credentials()
    if not (user and password):
        fail("SOURCE_USERNAME / SOURCE_PASSWORD are not set; they are the Nexus credentials used to log in to %s" % host)
    registry_login(host, user, password, tool)


# --------------------------------------------------------------------------- image extraction


def extract_with_docker(ref: str, app_path: str, out_dir: Path, platform: str) -> dict:
    plat = ["--platform", platform] if platform else []
    pull = run(["docker", "pull", *plat, ref], check=False)
    if pull.returncode != 0:
        log("  WARN: docker pull failed, trying a locally available image: " + (pull.stderr or "").strip()[-300:])
    # The trailing argument is never executed: `docker create` only needs *a* command when the
    # image defines neither CMD nor ENTRYPOINT.
    cid = run(["docker", "create", *plat, ref, "__dependency_bundle_extract__"]).stdout.strip()
    try:
        run(["docker", "cp", "%s:%s" % (cid, app_path), str(out_dir)])
    finally:
        run(["docker", "rm", "-f", cid], check=False)
    insp = run(["docker", "image", "inspect", "--format", '{{.Id}}|{{join .RepoDigests ","}}', ref], check=False)
    image_id, digests = "", ""
    if insp.returncode == 0 and "|" in insp.stdout:
        image_id, digests = insp.stdout.strip().split("|", 1)
    return {"digest": digests or image_id or None, "image_id": image_id or None}


def extract_with_crane(ref: str, app_path: str, out_dir: Path, platform: str) -> dict:
    plat = ["--platform", platform] if platform else []
    digest = run(["crane", "digest", *plat, ref], check=False).stdout.strip() or None
    target = app_path.strip("/")
    parent = target.rsplit("/", 1)[0] + "/" if "/" in target else ""
    log("  $ crane export %s %s - | (extract %s)" % (" ".join(plat), ref, target))
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(["crane", "export", *plat, ref, "-"], stdout=subprocess.PIPE, stderr=err)
        found = 0
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
                for m in tf:
                    name = m.name[2:] if m.name.startswith("./") else m.name
                    name = name.lstrip("/")
                    if name != target and not name.startswith(target + "/"):
                        continue
                    dest = safe_join(out_dir, name[len(parent):])
                    if m.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    if m.issym() or m.islnk():
                        fail("%s is a link to %s inside the image; pass the real path with --path" % (name, m.linkname))
                    if not m.isfile():
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(m)
                    with open(dest, "wb") as f:
                        shutil.copyfileobj(src, f)
                    found += 1
        finally:
            if proc.stdout:
                proc.stdout.close()
            proc.wait()
        err.seek(0)
        stderr = err.read().decode("utf-8", "replace")
    if proc.returncode != 0:
        fail("crane export failed: " + stderr[-2000:])
    if found == 0:
        fail("path %s not found in image %s" % (app_path, ref))
    return {"digest": digest, "image_id": None}


# --------------------------------------------------------------------------- http / maven


def http_request(url: str, with_auth: bool) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"User-Agent": "mvn-rebundle"})
    user, pw = source_credentials()
    if with_auth and user and pw:
        token = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    return req


def http_get(url: str, with_auth: bool) -> bytes:
    try:
        with urllib.request.urlopen(http_request(url, with_auth), timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        fail("GET %s failed: HTTP %s %s" % (url, e.code, e.reason))
    except urllib.error.URLError as e:
        fail("GET %s failed: %s" % (url, e.reason))


def download(url: str, dest: Path, with_auth: bool) -> None:
    log("  downloading %s%s" % (url, " (basic auth)" if with_auth and all(source_credentials()) else ""))
    try:
        with urllib.request.urlopen(http_request(url, with_auth), timeout=600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        fail("download of %s failed: HTTP %s %s" % (url, e.code, e.reason))
    except urllib.error.URLError as e:
        fail("download of %s failed: %s" % (url, e.reason))


def parse_maven_ref(ref: str, default_packaging: str = "jar") -> dict:
    """groupId:artifactId:version[:packaging[:classifier]] (maven-dependency-plugin convention)."""
    parts = ref.split(":")
    if len(parts) not in (3, 4, 5) or not all(parts[:3]):
        fail("Maven reference must be groupId:artifactId:version[:packaging[:classifier]], got '%s'" % ref)
    return {
        "groupId": parts[0],
        "artifactId": parts[1],
        "version": parts[2],
        "packaging": parts[3] if len(parts) > 3 and parts[3] else default_packaging,
        "classifier": parts[4] if len(parts) > 4 and parts[4] else None,
    }


def resolve_snapshot_version(metadata_xml: str, version: str, extension: str, classifier: Optional[str]) -> str:
    """Timestamped version of a SNAPSHOT from the repository's maven-metadata.xml."""
    try:
        root = ET.fromstring(metadata_xml)
    except ET.ParseError:
        return version
    for sv in root.iter("snapshotVersion"):
        ext = (sv.findtext("extension") or "").strip()
        cls = (sv.findtext("classifier") or "").strip() or None
        if ext == extension and cls == classifier and sv.findtext("value"):
            return sv.findtext("value").strip()
    snap = root.find("./versioning/snapshot")
    if snap is not None:
        if (snap.findtext("localCopy") or "").strip().lower() == "true":
            return version
        ts, bn = (snap.findtext("timestamp") or "").strip(), (snap.findtext("buildNumber") or "").strip()
        if ts and bn:
            return version[: -len("SNAPSHOT")] + ts + "-" + bn
    return version


def maven_artifact_url(ref: str, repo_url: str, default_packaging: str = "jar",
                       metadata_xml: Optional[str] = None) -> Tuple[str, str, str]:
    """(url, file name, resolved version) of an artifact in a Maven 2 layout repository."""
    g = parse_maven_ref(ref, default_packaging)
    base = "%s/%s/%s/%s/" % (repo_url.rstrip("/"), g["groupId"].replace(".", "/"), g["artifactId"], g["version"])
    resolved = g["version"]
    if g["version"].endswith("-SNAPSHOT") and metadata_xml is not None:
        resolved = resolve_snapshot_version(metadata_xml, g["version"], g["packaging"], g["classifier"])
    name = "%s-%s%s.%s" % (g["artifactId"], resolved, ("-" + g["classifier"]) if g["classifier"] else "", g["packaging"])
    return base + name, name, resolved


def fetch_maven(ref: str, repo_url: str, default_packaging: str, out_dir: Path) -> Tuple[Path, dict]:
    if not repo_url:
        fail("--maven-repo-url is required for Maven coordinates")
    g = parse_maven_ref(ref, default_packaging)
    metadata = None
    if g["version"].endswith("-SNAPSHOT"):
        meta_url = "%s/%s/%s/%s/maven-metadata.xml" % (repo_url.rstrip("/"), g["groupId"].replace(".", "/"), g["artifactId"], g["version"])
        log("  resolving snapshot through %s" % meta_url)
        metadata = http_get(meta_url, True).decode("utf-8", "replace")
    url, name, resolved = maven_artifact_url(ref, repo_url, default_packaging, metadata)
    dest = out_dir / name
    download(url, dest, True)
    info = dict(g, repository=repo_url, url=url, resolved_version=resolved)
    return dest, info


# --------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True, help="image reference, Maven coordinates, URL, or local path")
    ap.add_argument("--type", default="auto", choices=["auto", "image", "ecr-image", "nexus-image", "nexus-maven", "maven", "jar", "dir"],
                    help="override the detection (default: auto)")
    ap.add_argument("--detect", action="store_true", help="print '<kind> <auth> <host>' for --ref and exit")
    ap.add_argument("--nexus-hosts", default="", help="comma separated Nexus hosts or URLs that use SOURCE_USERNAME / SOURCE_PASSWORD")
    ap.add_argument("--path", default="/app/app.jar", help="images: file or directory inside the image")
    ap.add_argument("--out", default="work/app", help="directory receiving the extracted artifact")
    ap.add_argument("--meta", default="out/source.json")
    ap.add_argument("--image-tool", choices=["auto", "docker", "crane"], default="auto")
    ap.add_argument("--platform", default="", help="images: e.g. linux/amd64 (optional)")
    ap.add_argument("--aws-region", default="", help="ECR images: override the region derived from the registry host")
    ap.add_argument("--maven-repo-url", default="", help="Maven coordinates: repository URL, e.g. https://nexus.example.com/repository/maven-public/")
    ap.add_argument("--packaging", default="jar", help="Maven coordinates without a packaging: jar or war")
    args = ap.parse_args()

    args.ref, path_override = split_ref(args.ref)
    if path_override:
        args.path = path_override
    nexus_hosts = parse_hosts(args.nexus_hosts) | parse_hosts(args.maven_repo_url)
    kind, auth, host = resolve_type(args.type, args.ref, nexus_hosts)
    if args.detect:
        print("%s %s %s" % (kind, auth, host or "-"))
        return

    out_dir = Path(args.out).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    meta = {
        "type": kind,
        "auth": auth,
        "ref": args.ref,
        "detected": args.type == "auto",
        "app_path": None,
        "local_path": None,
        "digest": None,
        "sha256": None,
        "image_tool": None,
        "platform": args.platform or None,
        "registry": None,
        "aws_region": None,
        "maven": None,
        "fetched_at": utc_now_iso(),
    }
    log("Source %s: %s (%s%s)" % (kind, args.ref, "auth " + auth, ", host " + host if host else ""))

    if kind == "image":
        region = None
        if auth == "ecr":
            region = ecr_region(host, args.aws_region)
            if host.lower() != ECR_PUBLIC_HOST and not region:
                fail("cannot derive the AWS region from registry host %s; pass --aws-region" % host)
        tool = pick_image_tool(args.image_tool)
        if auth == "ecr":
            log("Logging in to ECR registry %s (region %s)" % (host, region or "us-east-1"))
            ecr_login(host, region, tool)
        elif auth == "nexus":
            log("Logging in to Nexus Docker registry %s" % host)
            nexus_login(host, tool)
        log("Extracting %s from image %s using %s" % (args.path, args.ref, tool))
        info = extract_with_docker(args.ref, args.path, out_dir, args.platform) if tool == "docker" \
            else extract_with_crane(args.ref, args.path, out_dir, args.platform)
        local = out_dir / Path(args.path.rstrip("/")).name
        if not local.exists():
            fail("extraction produced nothing at %s" % local)
        meta.update(app_path=args.path, local_path=str(local), image_tool=tool, digest=info.get("digest"),
                    registry=host, aws_region=region)
    elif kind == "maven":
        if not all(source_credentials()):
            meta["auth"] = "none"
        log("Fetching %s from %s" % (args.ref, args.maven_repo_url))
        local, info = fetch_maven(args.ref, args.maven_repo_url, args.packaging, out_dir)
        meta.update(local_path=str(local), maven=info)
    elif kind == "url":
        name = args.ref.rsplit("/", 1)[-1].split("?", 1)[0] or "app.jar"
        local = out_dir / name
        download(args.ref, local, auth == "nexus")
        meta.update(local_path=str(local), registry=host)
    elif kind == "file":
        local = Path(args.ref).resolve()
        if not local.is_file():
            fail("%s is not a file" % local)
        meta["local_path"] = str(local)
    else:
        local = Path(args.ref).resolve()
        if not local.is_dir():
            fail("%s is not a directory" % local)
        meta["local_path"] = str(local)

    if local.is_file():
        meta["sha256"] = sha256_file(local)
    write_json(args.meta, meta)
    log("Source ready: %s" % meta["local_path"])
    if meta["sha256"]:
        log("  sha256 %s" % meta["sha256"])
    if meta["digest"]:
        log("  image  %s" % meta["digest"])
    if meta["maven"]:
        log("  maven  %s (resolved %s)" % (args.ref, meta["maven"]["resolved_version"]))


if __name__ == "__main__":
    main()
