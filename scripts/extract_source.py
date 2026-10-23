#!/usr/bin/env python3
"""Stage 1: fetch the application artifact to analyse.

  --type image   copy --path out of a container image (docker or crane backend)
  --type jar     a local path or an http(s) URL of a Spring Boot jar / war
  --type dir     a local directory holding an exploded / Jib style application

Writes a metadata JSON (--meta) consumed by the following stages.
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, safe_join, sha256_file, utc_now_iso, which, write_json  # noqa: E402


def run(cmd, check=True):
    log("  $ " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        fail("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), (p.stderr or "")[-2000:]))
    return p


def docker_available() -> bool:
    if not which("docker"):
        return False
    p = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0


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


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "dep-bundle-pipeline"})
    user, pw = os.environ.get("SOURCE_USERNAME"), os.environ.get("SOURCE_PASSWORD")
    if user and pw:
        token = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    log("  downloading %s" % url)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--type", choices=["image", "jar", "dir"], required=True)
    ap.add_argument("--ref", required=True, help="image reference, jar path/URL or directory")
    ap.add_argument("--path", default="/app/app.jar", help="file or directory inside the image (type=image)")
    ap.add_argument("--out", default="work/app", help="directory receiving the extracted artifact")
    ap.add_argument("--meta", default="out/source.json")
    ap.add_argument("--image-tool", choices=["auto", "docker", "crane"], default="auto")
    ap.add_argument("--platform", default="", help="e.g. linux/amd64 (optional)")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    meta = {
        "type": args.type,
        "ref": args.ref,
        "app_path": None,
        "local_path": None,
        "digest": None,
        "sha256": None,
        "image_tool": None,
        "platform": args.platform or None,
        "fetched_at": utc_now_iso(),
    }

    if args.type == "image":
        tool = args.image_tool
        if tool == "auto":
            if docker_available():
                tool = "docker"
            elif which("crane"):
                tool = "crane"
            else:
                fail("neither docker (with a reachable daemon) nor crane is available on PATH")
        log("Extracting %s from image %s using %s" % (args.path, args.ref, tool))
        info = extract_with_docker(args.ref, args.path, out_dir, args.platform) if tool == "docker" \
            else extract_with_crane(args.ref, args.path, out_dir, args.platform)
        local = out_dir / Path(args.path.rstrip("/")).name
        if not local.exists():
            fail("extraction produced nothing at %s" % local)
        meta.update(app_path=args.path, local_path=str(local), image_tool=tool, digest=info.get("digest"))
    elif args.type == "jar":
        if args.ref.startswith(("http://", "https://")):
            name = args.ref.rsplit("/", 1)[-1].split("?", 1)[0] or "app.jar"
            local = out_dir / name
            download(args.ref, local)
        else:
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


if __name__ == "__main__":
    main()
