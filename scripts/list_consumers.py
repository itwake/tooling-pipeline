#!/usr/bin/env python3
"""The consumer projects registered for a bundle (config/consumers.json), for the producer jobs' fan-out.

consumers.json maps groupId:artifactId of a bundle to the projects that use it:

  {"consumers": {"com.company.platform:thirdparty-bundle": [
      {"git": "https://git.example.com/team/orders.git", "ref": "main", "apply": true}]}}

Writes one line per consumer to --out (git URL, ref and apply flag, tab separated; the Jenkinsfile starts the
consumer job with them) and the same list as JSON to --json-out for the summary. Nothing registered is not an
error: the file is empty and the job says so.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bundlelib import fail, log, read_json, write_json  # noqa: E402


def consumers_of(config: dict, bundle: str) -> list:
    parts = bundle.split(":")
    if len(parts) < 2 or not all(parts[:2]):
        fail("--bundle must be groupId:artifactId[:version], got '%s'" % bundle)
    ga = "%s:%s" % (parts[0], parts[1])
    out = []
    for entry in (config.get("consumers") or {}).get(ga) or []:
        if not isinstance(entry, dict) or not entry.get("git"):
            fail("consumers.json: every entry under %s needs a git URL, got %r" % (ga, entry))
        out.append({"git": entry["git"], "ref": entry.get("ref") or "main", "apply": bool(entry.get("apply", True))})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True, help="groupId:artifactId[:version] of the published bundle")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config" / "consumers.json"))
    ap.add_argument("--out", default="out/consumers.txt", help="one consumer per line: git URL, ref, apply (tab separated)")
    ap.add_argument("--json-out", default="out/consumers.json")
    args = ap.parse_args()

    config = read_json(args.config) if Path(args.config).is_file() else {}
    consumers = consumers_of(config, args.bundle)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("".join("%s\t%s\t%s\n" % (c["git"], c["ref"], "true" if c["apply"] else "false") for c in consumers), encoding="utf-8")
    write_json(args.json_out, {"bundle": args.bundle, "consumers": consumers, "config": str(args.config)})
    if consumers:
        log("%d consumer(s) registered for %s: %s" % (len(consumers), args.bundle, ", ".join("%s (%s%s)" % (c["git"], c["ref"], ", apply" if c["apply"] else "") for c in consumers)))
    else:
        log("no consumer registered for %s in %s" % (args.bundle, args.config))


if __name__ == "__main__":
    main()
