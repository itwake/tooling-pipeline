#!/usr/bin/env python3
"""Write a Maven settings.xml for the Jenkins agent.

Credentials are read by Maven from the environment (NEXUS_USERNAME / NEXUS_PASSWORD,
set by Jenkins withCredentials), so nothing secret is written to disk.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from xml.sax.saxutils import escape

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.2.0 https://maven.apache.org/xsd/settings-1.2.0.xsd">
  <servers>
    <server>
      <id>%(server_id)s</id>
      <username>${env.NEXUS_USERNAME}</username>
      <password>${env.NEXUS_PASSWORD}</password>
    </server>%(mirror_server)s
  </servers>%(mirrors)s
</settings>
"""

MIRROR_SERVER = """
    <server>
      <id>%(mirror_id)s</id>
      <username>${env.NEXUS_USERNAME}</username>
      <password>${env.NEXUS_PASSWORD}</password>
    </server>"""

MIRRORS = """
  <mirrors>
    <mirror>
      <id>%(mirror_id)s</id>
      <mirrorOf>*</mirrorOf>
      <url>%(mirror_url)s</url>
    </mirror>
  </mirrors>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server-id", default="nexus")
    ap.add_argument("--mirror-url", default="", help="company Maven group repository; empty = resolve from Central")
    ap.add_argument("--mirror-id", default="nexus-mirror")
    ap.add_argument("--out", default="work/settings.xml")
    args = ap.parse_args()
    values = {
        "server_id": escape(args.server_id),
        "mirror_id": escape(args.mirror_id),
        "mirror_url": escape(args.mirror_url),
        "mirror_server": (MIRROR_SERVER % {"mirror_id": escape(args.mirror_id)}) if args.mirror_url else "",
        "mirrors": (MIRRORS % {"mirror_id": escape(args.mirror_id), "mirror_url": escape(args.mirror_url)}) if args.mirror_url else "",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(TEMPLATE % values, encoding="utf-8")
    print("Maven settings written to %s%s" % (out, " (mirror %s)" % args.mirror_url if args.mirror_url else ""))


if __name__ == "__main__":
    main()
