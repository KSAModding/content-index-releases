#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Re-derive the design repository's examples and diff them against the stamper.

examples/ in content-manager-design was stamped by hand from the same archives,
so it is an expectation this repository did not write itself.

Only the half the archive and its host decide is compared. The rest is frozen
from the authored listing (RFC 0031) and drifts when it is edited.

    python3 tools/verify_examples.py
    python3 tools/verify_examples.py --examples ../content-manager-design/examples
    python3 tools/verify_examples.py --check-mirrors

Needs the network, unlike the unit tests: the bytes have to come from the real
hosts, which exercises tools/hosts.py at the same time.
"""

import argparse
import difflib
import json
import sys
import tomllib
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import hosts
from stamp_release import StampError, serialize, stamp

DESIGN = "KSAModding/content-manager-design"
RAW = "https://raw.githubusercontent.com/{repository}/main/{path}"
TREE = "https://api.github.com/repos/{repository}/git/trees/main?recursive=1"

# What the archive and its host decide, plus the identity.
ARCHIVE_FACTS = (
    "spec_version",
    "id",
    "type",
    "version",
    "version_scheme",
    "release_status",
    "release_date",
    "download",
    "install_size",
    "changelog",
)

# The rest, frozen from the listing. Both halves add up to a full document.
AUTHORED_FACTS = (
    "game_min",
    "game_min_revision",
    "game_max",
    "game_max_revision",
    "os",
    "loader",
    "dependencies",
    "listing",
)

# Only when the root was derived. An authored root and the anchor are listing facts.
INSTALL_ARCHIVE_FACTS = ("root", "derived")


def from_disk(folder):
    listings, releases = {}, {}
    for path in sorted((folder / "listings").glob("*.toml")):
        listings[path.stem] = path.read_text(encoding="utf-8")
    for path in sorted((folder / "releases").glob("*/*.json")):
        releases[(path.parent.name, path.stem)] = path.read_text(encoding="utf-8")
    return listings, releases


def from_github(http, repository):
    answer = http.get(TREE.format(repository=repository), accept="application/vnd.github+json", api=True)
    paths = [entry["path"] for entry in json.loads(answer.body)["tree"]]

    def fetch(path):
        return http.get(RAW.format(repository=repository, path=path)).body.decode("utf-8")

    listings = {
        Path(path).stem: fetch(path)
        for path in paths
        if path.startswith("examples/listings/") and path.endswith(".toml")
    }
    releases = {
        (Path(path).parent.name, Path(path).stem): fetch(path)
        for path in paths
        if path.startswith("examples/releases/") and path.endswith(".json")
    }
    return listings, releases


def archive_facts(document, with_mirrors=True):
    """The half of a release file the archive and its host decide.

    An allowlist, so a field the format grows later stays out until it is classified.
    `dependencies` is out: merge_dependencies drops a derived entry once an authored
    one names the same id, so the stamped list is a joint product.
    """
    facts = {key: document[key] for key in ARCHIVE_FACTS if key in document}

    install = document.get("install")
    derived_root = install is None or bool(install.get("derived"))

    if install is not None:
        facts["install"] = (
            {key: install[key] for key in INSTALL_ARCHIVE_FACTS if key in install}
            if derived_root
            else {"derived": False}
        )

    if not derived_root:
        # Measured under a root the listing chose.
        facts.pop("install_size", None)

    if not with_mirrors and "download" in facts:
        # Without the flag these went into the stamp, so it would compare them to itself.
        facts["download"] = {
            key: value for key, value in facts["download"].items() if key != "mirrors"
        }
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--examples", type=Path,
        help="a local examples/ folder; without it the design repository is fetched",
    )
    parser.add_argument("--repository", default=DESIGN)
    parser.add_argument(
        "--game-versions", type=Path, default=Path("game-versions.json"),
        help="the game release list month bounds resolve against",
    )
    parser.add_argument(
        "--check-mirrors", action="store_true",
        help="derive download.mirrors from the non-authority hosts instead of taking "
        "the example's word for it. One more full download per mirror",
    )
    arguments = parser.parse_args(argv)

    http = hosts.Http(token=_token())
    try:
        listings, examples = (
            from_disk(arguments.examples)
            if arguments.examples
            else from_github(http, arguments.repository)
        )
    except (hosts.HostError, urllib.error.HTTPError, OSError) as error:
        print(f"could not read the examples: {error}", file=sys.stderr)
        return 2

    game_versions = json.loads(
        arguments.game_versions.read_text(encoding="utf-8")
    )["versions"]

    failures = 0
    for (listing_id, version), expected in sorted(examples.items()):
        try:
            rendered = _restamp(
                http, listings[listing_id], version, game_versions,
                json.loads(expected), arguments.check_mirrors,
            )
        except (StampError, hosts.HostError, KeyError, urllib.error.HTTPError) as error:
            print(f"{listing_id} {version}: {error}")
            failures += 1
            continue

        want = archive_facts(json.loads(expected), arguments.check_mirrors)
        got = archive_facts(rendered, arguments.check_mirrors)
        if got == want:
            print(f"{listing_id} {version}: the archive facts match")
            continue

        failures += 1
        print(f"{listing_id} {version}: an archive fact differs from the example")
        sys.stdout.writelines(
            difflib.unified_diff(
                serialize(want).splitlines(True), serialize(got).splitlines(True),
                "hand-stamped", "stamper", n=1,
            )
        )

    print(f"\n{len(examples)} example(s), {failures} failure(s)")
    return 1 if failures else 0


def _restamp(http, listing_toml, version, game_versions, expected, check_mirrors):
    authored = tomllib.loads(listing_toml)
    authority, mirror_hosts = hosts.build(
        authored.get("releases"), http, authored.get("id")
    )
    if authority is None:
        raise StampError("the listing names no release host")

    releases, _ = authority.releases()
    release = next((entry for entry in releases if entry.version == version), None)
    if release is None:
        raise StampError(f"{authority.key} does not offer {version}")

    archive, content_type = authority.download(release)
    facts = release.facts()
    facts["content_type"] = content_type

    mirrors = expected.get("download", {}).get("mirrors", [])
    if check_mirrors:
        mirrors = _mirrors(mirror_hosts, version, archive)

    # Wall clock on purpose: the original stamp time is unrecorded, and the
    # watcher's month pass corrects a stamp once its game_max month completes,
    # so re-deriving with the current time matches the corrected state an
    # example is expected to hold.
    return stamp(
        authored, facts, archive, game_versions,
        mirrors=mirrors, now=datetime.now(timezone.utc),
    )


def _mirrors(mirror_hosts, version, archive):
    import hashlib

    digest = hashlib.sha256(archive).hexdigest().upper()
    found = []
    for host in mirror_hosts:
        releases, _ = host.releases()
        release = next((entry for entry in releases if entry.version == version), None)
        if release is None:
            continue
        mirrored, _ = host.download(release)
        if hashlib.sha256(mirrored).hexdigest().upper() == digest:
            found.append(release.url)
    return found


def _token():
    import os

    return os.environ.get("GITHUB_TOKEN")


if __name__ == "__main__":
    sys.exit(main())
