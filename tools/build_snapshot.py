#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Both halves of the index plus the game release list, as the snapshot documents.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from authored import FOLDERS, PACKS, host_key, id_holders, type_problem
from stamp_release import SEMVER, StampError, normalize_version, valid_id

SNAPSHOT_VERSION = 1

# The published files: the main document, and one file for each type that is not
# in it, keyed by its folder, which also names the array of the file. RFC 0094
# names the two files, and the names live here and nowhere else.
MAIN_FILE = "index.json"
TYPE_FILES = {"vehicles": "snapshot-vehicles.json", "saves": "snapshot-saves.json"}

STATES = ("delisted", "disputed", "retracted")

STATUS_FIELDS = ("state", "since", "reason")

DOWNLOAD_COUNTS_VERSION = 1

DOWNLOAD_HOSTS = ("github", "spacedock")

REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


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
    except (OSError, UnicodeDecodeError) as error:
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


def read_listings(authored, folder="listings"):
    """The authored listings of one folder, keyed by lowercased id, since ids compare that way."""
    directory = authored / folder
    listings = {}
    for path in sorted(directory.glob("*.toml")) if directory.is_dir() else []:
        document = load_toml(path)
        identifier = declared_id(document, path)
        if identifier != path.stem:
            raise SnapshotError(
                f"{path}: declares id '{identifier}', the file name says '{path.stem}'"
            )
        problem = type_problem(folder, document.get("type"))
        if problem:
            raise SnapshotError(f"{path}: {problem}")
        if identifier.lower() in listings:
            raise SnapshotError(f"{path}: the id '{identifier}' is listed twice")
        listings[identifier.lower()] = document
    return listings


def read_all_listings(authored):
    """The authored listings of every folder of FOLDERS, each keyed as read_listings keys it."""
    return {folder: read_listings(authored, folder) for folder in FOLDERS}


def check_ids_unique(authored):
    """Mods and loaders, vehicles, saves and packs share one namespace of ids (RFC 0031)."""
    for key, holders in sorted(id_holders(authored).items()):
        if len(holders) > 1:
            raise SnapshotError(
                f"the id '{key}' is held by {' and '.join(holders)}, and an id is unique "
                "across content types, compared without case"
            )


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
                f"index-status.toml: '{names[key]}' is no listing, vehicle, save or pack, "
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


def check_count(value, where):
    # JSON `true` is an int to Python, so a bool is refused explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SnapshotError(f"{where}: {value!r} is not a non-negative integer")


def check_counted(item, where, keys):
    """One object of a `total` and the `hosts` it is the sum of, with exactly `keys`."""
    if not isinstance(item, dict):
        raise SnapshotError(f"{where}: is not an object")
    unknown = sorted(set(item) - set(keys))
    if unknown:
        raise SnapshotError(f"{where}: carries the unknown key(s) {', '.join(unknown)}")
    missing = [key for key in keys if key not in item]
    if missing:
        raise SnapshotError(f"{where}: lacks {', '.join(missing)}")

    counts = item["hosts"]
    if not isinstance(counts, dict) or not counts:
        raise SnapshotError(f"{where}: hosts has to be an object with at least one host")
    for host, value in counts.items():
        # A share host counts under its key in hosts.toml. Its form is checked and
        # not the file, because a host that left hosts.toml keeps its last count.
        if host not in DOWNLOAD_HOSTS and not host_key(host):
            raise SnapshotError(
                f"{where}: '{host}' is neither a host RFC 0052 defines nor the key of a share host (RFC 0094)"
            )
        check_count(value, f"{where} hosts.{host}")
    check_count(item["total"], f"{where} total")
    if item["total"] != sum(counts.values()):
        raise SnapshotError(
            f"{where}: total {item['total']} is not the sum of its hosts"
        )


def check_download_counts(document, where):
    """download-counts.json checked against RFC 0052, its entries keyed by lowercased id."""
    if sorted(document) != ["listings", "spec_version"]:
        raise SnapshotError(f"{where}: the document carries exactly spec_version and listings")
    version = document["spec_version"]
    if isinstance(version, bool) or version != DOWNLOAD_COUNTS_VERSION:
        raise SnapshotError(
            f"{where}: spec_version {version!r} is not {DOWNLOAD_COUNTS_VERSION}"
        )
    if not isinstance(document["listings"], list):
        raise SnapshotError(f"{where}: listings has to be an array")

    entries = {}
    previous = None
    for position, entry in enumerate(document["listings"], start=1):
        at = f"{where} listing {position}"
        check_counted(entry, at, ("id", "total", "hosts", "releases"))
        identifier = entry["id"]
        if not isinstance(identifier, str) or not valid_id(identifier):
            raise SnapshotError(f"{at}: names no id that satisfies the id rules of RFC 0031")
        key = identifier.lower()
        if previous is not None and key <= previous:
            raise SnapshotError(
                f"{at}: '{identifier}' is listed twice or out of order, and listings "
                "are ascending by lowercased id"
            )
        previous = key

        releases = entry["releases"]
        if not isinstance(releases, list):
            raise SnapshotError(f"{at}: releases has to be an array")
        for index, release in enumerate(releases, start=1):
            place = f"{at} release {index}"
            check_counted(release, place, ("version", "total", "hosts"))
            text = release["version"]
            try:
                normalized = normalize_version(text) if isinstance(text, str) else None
            except StampError:
                normalized = None
            if normalized != text:
                raise SnapshotError(
                    f"{place}: version {text!r} is not a normalized SemVer 2.0.0 version"
                )
            stray = sorted(set(release["hosts"]) - set(entry["hosts"]))
            if stray:
                raise SnapshotError(
                    f"{place}: counts {', '.join(stray)}, which the listing has no total for"
                )
        versions = [release["version"] for release in releases]
        if len(set(versions)) != len(versions):
            raise SnapshotError(f"{at}: a version is listed twice")
        if releases != newest_first(releases, at):
            raise SnapshotError(f"{at}: releases are not descending by SemVer precedence")
        entries[key] = entry
    return entries


def read_download_counts(path):
    """The download counts keyed by lowercased id, or nothing when the file is absent."""
    if path is None or not Path(path).is_file():
        return {}
    return check_download_counts(load_json(Path(path)), str(path))


def instant(value, where):
    """A timestamp with an offset as an aware datetime."""
    try:
        moment = datetime.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise SnapshotError(f"{where}: {value!r} is not an ISO 8601 timestamp with an offset")
    return moment


def entry_dates(timestamps):
    """`published_at` and `updated_at` from (text, where, withdrawn). Equal instants tie on the text."""
    parsed = [(instant(text, where), text, withdrawn) for text, where, withdrawn in timestamps]
    dates = {}
    if parsed:
        dates["published_at"] = min(parsed, key=lambda item: item[:2])[1]
    live = [item for item in parsed if not item[2]]
    if live:
        dates["updated_at"] = max(live, key=lambda item: item[:2])[1]
    return dates


def git(authored, *arguments):
    """The output of one git command in the authored checkout, or None when it fails.

    Variables such as GIT_DIR, which a git hook sets, would point git at another
    repository, so the ones that choose the repository are left out.
    """
    environment = {
        key: value for key, value in os.environ.items() if key not in REPOSITORY_VARIABLES
    }
    try:
        result = subprocess.run(
            ["git", "-C", str(authored), *arguments],
            capture_output=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
    except (OSError, ValueError):
        return None
    return result.stdout if result.returncode == 0 else None


def history_key(path):
    """The folder and lowercased id a path of the authored tree belongs to, or None.

    The folder is part of the key, so an id that moves between two folders, such as
    from a pack to a listing, does not take the time of the other one.
    """
    parts = path.split("/")
    if len(parts) == 2 and parts[0] in FOLDERS and parts[1].endswith(".toml"):
        return (parts[0], parts[1][: -len(".toml")].lower())
    if len(parts) == 3 and parts[0] == PACKS and parts[2].endswith(".toml"):
        return (PACKS, parts[1].lower())
    return None


def listed_times(authored, log):
    """When each entry came into the index, keyed as history_key does, or None without the history.

    RFC 0092 takes the committer time of the oldest commit in the first-parent history
    of HEAD whose tree holds the listing's document or a version of the pack, which is
    the first commit that adds such a file against its first parent. A shallow checkout
    cuts the history, so its oldest commit would claim every file and is not used.
    """
    top = git(authored, "rev-parse", "--show-toplevel")
    try:
        own = top is not None and os.path.samefile(top.strip(), authored)
    except OSError:
        own = False
    if not own:
        log(
            f"{authored} is not a git checkout of its own that git can read, "
            "so no entry carries listed_at"
        )
        return None
    if (git(authored, "rev-parse", "--is-shallow-repository") or "").strip() != "false":
        log(
            f"{authored} is a shallow checkout without its full history, "
            "so no entry carries listed_at"
        )
        return None

    output = git(
        authored,
        "-c", "core.quotePath=false",
        "log", "HEAD",
        "--first-parent", "--diff-merges=first-parent", "--root", "--reverse",
        "--no-renames", "--diff-filter=A", "--name-only", "--format=commit %ct",
        "--", *FOLDERS, PACKS,
    )
    if output is None:
        log(f"the history of {authored} could not be read, so no entry carries listed_at")
        return None

    times = {}
    moment = None
    for line in output.splitlines():
        if line.startswith("commit "):
            seconds = int(line[len("commit "):])
            moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
            continue
        key = history_key(line.strip())
        if key is not None and moment is not None:
            times.setdefault(key, moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return times


def listing_entry(document, status, releases, downloads=None, listed_at=None):
    """One entry of `listings`, `vehicles` or `saves`. Delisted becomes a tombstone: id and status only."""
    if status is not None and status["state"] == "delisted":
        return {"id": document["id"], "index_status": status}

    dates = entry_dates(
        (
            release.get("release_date"),
            f"{document['id']} {release['version']} release_date",
            release.get("yanked") is True,
        )
        for release in releases
    )
    if listed_at is not None:
        dates["listed_at"] = listed_at
    entry = {"id": document["id"], **dates, "authored": document, "releases": releases}
    if status is not None:
        entry["index_status"] = status  # disputed ships whole, the client warns
    if downloads is not None:
        entry["downloads"] = {key: value for key, value in downloads.items() if key != "id"}
    return entry


def pack_entry(pack, status, versioned, listed_at=None):
    """One entry of `packs`. A pack has no generated half, so there is no `releases`."""
    identifier = pack["id"]
    if status is not None and status["state"] == "delisted":
        return {"id": identifier, "index_status": status}

    rendered = []
    timestamps = []
    for document in newest_first(list(pack["versions"].values()), f"packs/{identifier}"):
        item = {"authored": document}
        retracted = versioned.get((identifier.lower(), document["version"]))
        if retracted is not None:
            item["index_status"] = retracted
        rendered.append(item)
        timestamps.append(
            (
                document.get("released_at"),
                f"packs/{identifier}/{document['version']}.toml released_at",
                retracted is not None,
            )
        )

    dates = entry_dates(timestamps)
    if listed_at is not None:
        dates["listed_at"] = listed_at
    entry = {"id": identifier, **dates, "versions": rendered}
    if status is not None:
        entry["index_status"] = status
    return entry


def build(authored, releases, game_versions, sources=None, log=None, download_counts=None):
    """The main snapshot document for the state of the two repositories on disk."""
    return build_files(
        authored, releases, game_versions, sources, log, download_counts
    )[MAIN_FILE]


def build_files(
    authored, releases, game_versions, sources=None, log=None, download_counts=None, published=()
):
    """Every snapshot file for the state of the two repositories on disk, by name, the main document first.

    A type of TYPE_FILES gets its file once its folder holds a document, a delisted one
    included, and keeps it while the name is in `published` or the first-parent history
    ever added a document to the folder, because a client reads a published file that
    goes away as a fault and keeps its copy.
    """
    log = log or warn

    require_directory(authored, "the authored checkout")
    require_directory(authored / "listings", "the authored listings folder")
    require_directory(authored / "packs", "the authored packs folder")
    require_directory(releases, "the releases folder")

    groups = read_all_listings(authored)
    packs = read_packs(authored)
    check_ids_unique(authored)
    # The ids are unique, so the folders merge without loss.
    everything = {key: document for group in groups.values() for key, document in group.items()}

    whole, versioned, names = read_index_status(authored)
    check_states_resolve(whole, versioned, names, everything, packs, log)

    counts = read_download_counts(download_counts)
    unjoinable = sorted(set(counts) - set(everything))
    if unjoinable:
        raise SnapshotError(
            f"download-counts.json counts {', '.join(counts[key]['id'] for key in unjoinable)}, "
            "which is no listing, vehicle or save"
        )

    folders = release_folders(releases)

    listed = listed_times(authored, log)
    if listed is not None:
        entries = [(folder, key, group[key]["id"]) for folder, group in groups.items() for key in group]
        entries += [(PACKS, key, packs[key]["id"]) for key in packs]
        for folder, key, identifier in sorted(entries):
            if (folder, key) not in listed and whole.get(key, {}).get("state") != "delisted":
                log(f"'{identifier}' is not in the committed history, so it carries no listed_at")
    listed = listed or {}

    def rendered(group):
        entries = []
        for key in sorted(groups[group]):
            document = groups[group][key]
            status = whole.get(key)
            delisted = status is not None and status["state"] == "delisted"
            folder = folders.get(key)
            files = [] if delisted or folder is None else read_releases(folder, document["id"])
            entries.append(
                listing_entry(document, status, files, counts.get(key), listed.get((group, key)))
            )
        return entries

    rendered_packs = [
        pack_entry(packs[key], whole.get(key), versioned, listed.get(("packs", key)))
        for key in sorted(packs)
    ]

    for key in sorted(set(folders) - set(everything)):
        # The snapshot carries the release files of listed entries only.
        log(
            f"{folders[key]} belongs to no listing, vehicle or save, so its releases are not "
            f"in the snapshot"
        )

    def envelope():
        document = {"snapshot_version": SNAPSHOT_VERSION}
        if sources is not None:
            document["sources"] = sources
        return document

    main = envelope()
    main["listings"] = rendered("listings")
    main["packs"] = rendered_packs
    main["game_versions"] = load_json(game_versions)
    tags = authored / "tags.toml"
    if tags.is_file():
        main["tags"] = load_toml(tags)

    # The fetched copies alone can miss a published file, for example on a stale 404
    # from the CDN, so the history counts too.
    held = {folder for folder, _ in listed}
    files = {MAIN_FILE: main}
    for group, name in TYPE_FILES.items():
        if groups[group] or group in held or name in published:
            files[name] = {**envelope(), group: rendered(group)}
    return files


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
        description="Merge both halves of the index into the snapshot documents."
    )
    parser.add_argument(
        "--authored", default=".authored", type=Path,
        help="a checkout of the authored repository, which holds listings/, packs/, "
        "optional vehicles/ and saves/, index-status.toml and optional tags.toml",
    )
    parser.add_argument("--releases", default="releases", type=Path)
    parser.add_argument("--game-versions", default="game-versions.json", type=Path)
    parser.add_argument(
        "--download-counts", default="download-counts.json", type=Path,
        help="the download counts of RFC 0052, optional",
    )
    parser.add_argument(
        "--previous", type=Path,
        help="the main document published today, with the published copy of each other "
        "file next to it under its own name. Unchanged content keeps its provenance, "
        "so the bytes stay identical and the deploy can be skipped",
    )
    parser.add_argument(
        "--out", type=Path,
        help="write the main document here instead of to stdout, and each other file "
        "next to it. Never inside this repository: the snapshot is published as a "
        "Pages artifact and stays out of git history",
    )
    parser.add_argument(
        "--published-files", action="store_true",
        help="print the name of each file the snapshot publishes, one per line, and exit",
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


def published_files():
    """The name of every file the snapshot publishes, the main document first."""
    return [MAIN_FILE, *TYPE_FILES.values()]


def sibling(path, name):
    """Where the file `name` sits next to `path`, the place of the main document."""
    return path if name == MAIN_FILE else path.parent / name


def main(argv=None):
    arguments = parse_arguments(argv)
    if arguments.published_files:
        print("\n".join(published_files()))
        return 0

    previous = {}
    if arguments.previous is not None:
        previous = {name: sibling(arguments.previous, name) for name in published_files()}
    try:
        documents = build_files(
            arguments.authored,
            arguments.releases,
            arguments.game_versions,
            sources=sources_from(arguments),
            log=warn,
            download_counts=arguments.download_counts,
            # A copy that does not parse is still published, so existence decides.
            published=[name for name, path in previous.items() if path.is_file()],
        )
        rendered = {}
        for name, document in documents.items():
            documents[name] = carry_forward(
                document,
                read_previous(previous.get(name)),
                lambda message, name=name: info(f"{name}: {message}"),
            )
            rendered[name] = serialize(documents[name])
    except SnapshotError as error:
        print(f"cannot build the snapshot: {error}", file=sys.stderr)
        return 1

    if not arguments.out:
        sys.stdout.write(rendered[MAIN_FILE])
        if len(rendered) > 1:
            info(f"stdout holds the main document only, and --out also writes {', '.join(list(rendered)[1:])}")
        return 0

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        path = sibling(arguments.out, name)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        counted = [
            f"{len(value)} in {key}" for key, value in documents[name].items() if isinstance(value, list)
        ]
        info(f"wrote {path}: {', '.join(counted)}, {len(text.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
