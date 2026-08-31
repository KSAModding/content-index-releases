#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Both halves of the index plus the game release list, as one snapshot document.
"""

import argparse
import datetime
import json
import os
import sys
import tomllib
from pathlib import Path

from stamp_release import SEMVER, valid_id

SNAPSHOT_VERSION = 1

STATES = ("delisted", "disputed", "retracted")

STATUS_FIELDS = ("state", "since", "reason")


class SnapshotError(Exception):
    """The snapshot cannot be built. Failing loud keeps the last good one served."""


def warn(message):
    """A note on stderr, and a warning annotation on Actions.

    Every note here is a quiet failure, so none should need the log opened.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}", file=sys.stderr)
    else:
        print(f"note: {message}", file=sys.stderr)


def info(message):
    """What the run did, which is not a warning about anything."""
    print(message, file=sys.stderr)


def precedence(version, what):
    """A sort key for SemVer 2.0.0 precedence, item 11.
    """
    match = SEMVER.match((version or "").strip())
    if match is None:
        raise SnapshotError(f"{what}: version '{version}' does not parse as SemVer 2.0.0")

    core = (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
    prerelease = match.group("prerelease")
    if prerelease is None:
        return (core, (1,))  # a release outranks every pre-release of it

    identifiers = []
    for identifier in prerelease.split("."):
        if identifier.isdigit():
            identifiers.append((0, int(identifier), ""))  # numeric ranks below alphanumeric
        else:
            identifiers.append((1, 0, identifier))
    # A shorter list ranks lower, which tuple comparison gives for free.
    return (core, (0, tuple(identifiers)))


def newest_first(documents, what):
    """Documents newest first, ties broken by version text so the bytes stay stable."""
    return sorted(
        documents,
        key=lambda document: (
            precedence(document["version"], what),
            document["version"],
        ),
        reverse=True,
    )


def jsonable(value, what):
    """A TOML value as something JSON can hold.
    """
    if isinstance(value, dict):
        return {key: jsonable(item, what) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item, what) for item in value]
    if isinstance(value, datetime.datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise SnapshotError(f"{what}: the value {value} cannot be represented in JSON")
    return value


def load_toml(path):
    """One TOML document, as JSON-safe data."""
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise SnapshotError(f"{path}: not valid TOML, {error}") from error
    except OSError as error:
        raise SnapshotError(f"{path}: cannot be read, {error}") from error
    return jsonable(document, str(path))


def load_json(path):
    """One JSON document, which has to be an object.

    Python accepts `NaN` and `Infinity` as literals and neither is JSON, so both
    are refused here rather than in serialize, which cannot name the file.
    """

    def reject(literal):
        raise SnapshotError(f"{path}: {literal} is not valid JSON")

    try:
        document = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)
    except json.JSONDecodeError as error:
        raise SnapshotError(f"{path}: not valid JSON, {error}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SnapshotError(f"{path}: cannot be read, {error}") from error

    if not isinstance(document, dict):
        raise SnapshotError(
            f"{path}: the document is a {type(document).__name__} and not an object"
        )
    return document


def require_directory(path, what):
    if not path.is_dir():
        raise SnapshotError(f"{path}: {what} is not a directory")


def declared_id(document, path):
    identifier = document.get("id")
    if not isinstance(identifier, str) or not valid_id(identifier):
        raise SnapshotError(f"{path}: declares no id that satisfies the id rules of RFC 0031")
    return identifier


def read_listings(authored):
    """The authored listings, keyed by lowercased id, since ids compare that way."""
    folder = authored / "listings"
    listings = {}
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        document = load_toml(path)
        identifier = declared_id(document, path)
        if identifier != path.stem:
            raise SnapshotError(
                f"{path}: declares id '{identifier}', the file name says '{path.stem}'"
            )
        if identifier.lower() in listings:
            raise SnapshotError(f"{path}: the id '{identifier}' is listed twice")
        listings[identifier.lower()] = document
    return listings


def read_packs(authored):
    """The authored pack versions, keyed by lowercased id, authored casing kept."""
    folder = authored / "packs"
    packs = {}
    for path in sorted(folder.glob("*/*.toml")) if folder.is_dir() else []:
        document = load_toml(path)
        identifier = declared_id(document, path)
        if identifier != path.parent.name:
            raise SnapshotError(
                f"{path}: declares id '{identifier}', the folder says '{path.parent.name}'"
            )
        version = document.get("version")
        if version != path.stem:
            raise SnapshotError(
                f"{path}: declares version '{version}', the file name says '{path.stem}'"
            )
        pack = packs.setdefault(identifier.lower(), {"id": identifier, "versions": {}})
        if version in pack["versions"]:
            raise SnapshotError(f"{path}: version '{version}' is listed twice")
        pack["versions"][version] = document
    return packs


def release_folders(releases):
    """The folders under releases/, keyed by lowercased name.

    Case-insensitive because ids are, so a listing and its folder may differ and
    a literal lookup would publish the listing with no releases at all.
    """
    folders = {}
    for path in sorted(releases.iterdir()) if releases.is_dir() else []:
        if not path.is_dir():
            continue
        key = path.name.lower()
        if key in folders:
            raise SnapshotError(
                f"{path}: collides with {folders[key]} once ids are compared "
                f"case-insensitively, so neither can be published safely"
            )
        folders[key] = path
    return folders


def read_releases(folder, identifier):
    """The stamped release files in one folder, newest first."""
    documents = []
    for path in sorted(folder.glob("*.json")):
        document = load_json(path)
        version = document.get("version")
        if version != path.stem:
            raise SnapshotError(
                f"{path}: carries version '{version}', the file name says '{path.stem}'"
            )
        stamped = document.get("id")
        if not isinstance(stamped, str) or stamped.lower() != identifier.lower():
            raise SnapshotError(
                f"{path}: carries id '{stamped}', the listing says '{identifier}'"
            )
        documents.append(document)
    return newest_first(documents, str(folder))


def status_of(entry, where):
    """One index-status entry as the `index_status` the snapshot carries."""
    state = entry.get("state")
    if state not in STATES:
        raise SnapshotError(f"{where}: state '{state}' is not one of {', '.join(STATES)}")

    version = entry.get("version")
    if state == "retracted" and not version:
        raise SnapshotError(f"{where}: a retracted state names no version")
    if state != "retracted" and version is not None:
        raise SnapshotError(
            f"{where}: state '{state}' covers the whole entry and takes no version"
        )

    return {field: entry[field] for field in STATUS_FIELDS if entry.get(field) is not None}


def read_index_status(authored):
    """Whole-entry states, pack-version states, and the casing each id was written with."""
    path = authored / "index-status.toml"
    if not path.is_file():
        return {}, {}, {}

    document = load_toml(path)
    entries = document.get("entries", [])
    if not isinstance(entries, list):
        raise SnapshotError(f"{path}: entries has to be an array of tables")

    whole = {}
    versioned = {}
    names = {}
    for position, entry in enumerate(entries, start=1):
        where = f"{path} entry {position}"
        if not isinstance(entry, dict):
            raise SnapshotError(f"{where}: is not a table")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not valid_id(identifier):
            raise SnapshotError(f"{where}: names no id that satisfies the id rules of RFC 0031")

        status = status_of(entry, where)
        key = identifier.lower()
        names.setdefault(key, identifier)
        if status["state"] == "retracted":
            scope = (key, entry["version"])
            if scope in versioned:
                raise SnapshotError(
                    f"{where}: '{identifier}' version {entry['version']} already has a state"
                )
            versioned[scope] = status
        else:
            if key in whole:
                raise SnapshotError(f"{where}: '{identifier}' already has a state")
            whole[key] = status

    return whole, versioned, names


def check_states_resolve(whole, versioned, names, listings, packs, log):
    for key in sorted(whole):
        if key not in listings and key not in packs:
            raise SnapshotError(
                f"index-status.toml: '{names[key]}' is neither a listing nor a pack, "
                f"so its state cannot be placed"
            )

    for key, version in sorted(versioned):
        if key not in packs:
            raise SnapshotError(
                f"index-status.toml: '{names[key]}' version {version} is retracted, but "
                f"'{names[key]}' is not a pack, and only a pack version can be retracted"
            )
        if version not in packs[key]["versions"]:
            raise SnapshotError(
                f"index-status.toml: '{names[key]}' has no version {version} to retract"
            )
        if whole.get(key, {}).get("state") == "delisted":
            log(
                f"'{names[key]}' is delisted, so retracting its version "
                f"{version} changes nothing"
            )


def listing_entry(document, status, releases):
    """One entry of `listings`. Delisted becomes a tombstone: id and status only."""
    if status is not None and status["state"] == "delisted":
        return {"id": document["id"], "index_status": status}

    entry = {"id": document["id"], "authored": document, "releases": releases}
    if status is not None:
        entry["index_status"] = status  # disputed ships whole, the client warns
    return entry


def pack_entry(pack, status, versioned):
    """One entry of `packs`. A pack has no generated half, so there is no `releases`."""
    identifier = pack["id"]
    if status is not None and status["state"] == "delisted":
        return {"id": identifier, "index_status": status}

    rendered = []
    for document in newest_first(list(pack["versions"].values()), f"packs/{identifier}"):
        item = {"authored": document}
        retracted = versioned.get((identifier.lower(), document["version"]))
        if retracted is not None:
            item["index_status"] = retracted
        rendered.append(item)

    entry = {"id": identifier, "versions": rendered}
    if status is not None:
        entry["index_status"] = status
    return entry


def build(authored, releases, game_versions, sources=None, log=None):
    """The snapshot document for the state of the two repositories on disk."""
    log = log or warn

    require_directory(authored, "the authored checkout")
    require_directory(authored / "listings", "the authored listings folder")
    require_directory(authored / "packs", "the authored packs folder")
    require_directory(releases, "the releases folder")

    listings = read_listings(authored)
    packs = read_packs(authored)

    shared = sorted(set(listings) & set(packs))
    if shared:
        # The id namespace is global across content types (RFC 0031).
        raise SnapshotError(
            f"the id(s) {', '.join(shared)} are held by both a listing and a pack"
        )

    whole, versioned, names = read_index_status(authored)
    check_states_resolve(whole, versioned, names, listings, packs, log)

    folders = release_folders(releases)

    rendered_listings = []
    for key in sorted(listings):
        document = listings[key]
        status = whole.get(key)
        delisted = status is not None and status["state"] == "delisted"
        folder = folders.get(key)
        files = [] if delisted or folder is None else read_releases(folder, document["id"])
        rendered_listings.append(listing_entry(document, status, files))

    rendered_packs = [pack_entry(packs[key], whole.get(key), versioned) for key in sorted(packs)]

    for key in sorted(set(folders) - set(listings)):
        # The snapshot carries the release files of listed entries only.
        log(
            f"{folders[key]} belongs to no listing, so its releases are not "
            f"in the snapshot"
        )

    document = {"snapshot_version": SNAPSHOT_VERSION}
    if sources is not None:
        document["sources"] = sources
    document["listings"] = rendered_listings
    document["packs"] = rendered_packs
    document["game_versions"] = load_json(game_versions)
    return document


def serialize(document):
    """The snapshot's bytes: UTF-8, LF, two-space indent, trailing newline.

    Keys keep insertion order, so the same input gives the same bytes and an
    unchanged index keeps its ETag.
    """
    return json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def body(document):
    """The document without its provenance, which is what "unchanged" means."""
    return {key: value for key, value in document.items() if key != "sources"}


def with_sources(document, sources):
    """`document` with its provenance replaced, in the field order of the format."""
    rebuilt = {"snapshot_version": document["snapshot_version"]}
    if sources is not None:
        rebuilt["sources"] = sources
    for key, value in document.items():
        if key not in ("snapshot_version", "sources"):
            rebuilt[key] = value
    return rebuilt


def read_previous(path, log=warn):
    """The snapshot published today, or None. Never raises: it comes over the network.

    `NaN` and `Infinity` are refused for the reason load_json refuses them.
    """
    if path is None:
        return None

    def reject(literal):
        raise ValueError(f"{literal} is not valid JSON")

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log(f"{path} could not be read, so the provenance is not carried forward: {error}")
        return None
    if not isinstance(document, dict):
        log(f"{path} is not an object, so the provenance is not carried forward")
        return None
    return document


def carry_forward(document, previous, log=info):
    """Keep the published provenance while the content is unchanged.

    Otherwise an unrelated commit moves the bytes and every client re-downloads.
    The test is the bytes, because that is what the publish step compares.
    """
    if previous is None:
        return document
    candidate = with_sources(document, previous.get("sources"))
    if serialize(candidate) != serialize(previous):
        return document
    log("the content is unchanged, so the published provenance is kept and the bytes stay identical")
    return candidate


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description="Merge both halves of the index into one snapshot document."
    )
    parser.add_argument(
        "--authored", default=".authored", type=Path,
        help="a checkout of the authored repository, which holds listings/, packs/ "
        "and index-status.toml",
    )
    parser.add_argument("--releases", default="releases", type=Path)
    parser.add_argument("--game-versions", default="game-versions.json", type=Path)
    parser.add_argument(
        "--previous", type=Path,
        help="the snapshot published today. Unchanged content keeps its provenance, "
        "so the bytes stay identical and the deploy can be skipped",
    )
    parser.add_argument(
        "--out", type=Path,
        help="write here instead of to stdout. Never inside this repository: the "
        "snapshot is published as a Pages artifact and stays out of git history",
    )
    parser.add_argument("--authored-repo", help="for the sources block, as owner/name")
    parser.add_argument("--authored-commit", help="for the sources block")
    parser.add_argument("--generated-repo", help="for the sources block, as owner/name")
    parser.add_argument("--generated-commit", help="for the sources block")
    return parser.parse_args(argv)


def sources_from(arguments):
    """The `sources` block, or nothing when none of the four values was given.

    A partial set is a caller that meant to state a provenance and failed, most
    likely an empty command substitution, which no shell fails on, so it raises.
    """
    values = (
        arguments.authored_repo,
        arguments.authored_commit,
        arguments.generated_repo,
        arguments.generated_commit,
    )
    if not any(values):
        return None
    if not all(values):
        raise SnapshotError(
            "sources needs a repository and a commit for both halves, and this run "
            "gave only some of the four"
        )
    return {
        "authored": {
            "repository": arguments.authored_repo,
            "commit": arguments.authored_commit,
        },
        "generated": {
            "repository": arguments.generated_repo,
            "commit": arguments.generated_commit,
        },
    }


def main(argv=None):
    arguments = parse_arguments(argv)

    try:
        document = build(
            arguments.authored,
            arguments.releases,
            arguments.game_versions,
            sources=sources_from(arguments),
            log=warn,
        )
        document = carry_forward(document, read_previous(arguments.previous))
        rendered = serialize(document)
    except SnapshotError as error:
        print(f"cannot build the snapshot: {error}", file=sys.stderr)
        return 1

    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        with arguments.out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        info(
            f"wrote {arguments.out}: {len(document['listings'])} listing(s), "
            f"{len(document['packs'])} pack(s), "
            f"{len(rendered.encode('utf-8'))} bytes"
        )
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
