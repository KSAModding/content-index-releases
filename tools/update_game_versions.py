#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Append the production build the master server reports to game-versions.json.

Takes the response of `VersionInfo.GetServerVersionAsync` as argv[1] and prints
the version it added, or nothing, so the workflow knows whether to commit.
"""

import json
import re
import sys
from pathlib import Path

VERSIONS_FILE = Path("game-versions.json")

# Production builds only. A suffix such as -LOCAL is a stream this list is not about.
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")


def revision(version):
    """The fourth component, the only one that orders (RFC 0017)."""
    return int(VERSION.match(version).group(4))


def main():
    if len(sys.argv) < 2:
        print("no master server response given", file=sys.stderr)
        return 0

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        print("could not parse the master server response as JSON", file=sys.stderr)
        return 0

    version = (payload.get("Version") or "").strip()
    if not VERSION.match(version):
        print(f"ignoring '{version}', which is not a plain production version", file=sys.stderr)
        return 0

    document = json.loads(VERSIONS_FILE.read_text(encoding="utf-8"))
    versions = document["versions"]

    if version in versions:
        print(f"'{version}' is already listed", file=sys.stderr)
        return 0

    clash = next((other for other in versions if revision(other) == revision(version)), None)
    if clash is not None:
        # Revisions are unique across the shipped history, so this needs a human.
        print(f"revision {revision(version)} is already listed as '{clash}', refusing to add '{version}'", file=sys.stderr)
        return 1

    versions.append(version)
    versions.sort(key=revision)

    with VERSIONS_FILE.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")

    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
