#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Refresh download-counts.json from the release hosts (RFC 0052)."""

import argparse
import json
import os
import sys
from pathlib import Path

import hosts
from build_snapshot import (
    DOWNLOAD_COUNTS_VERSION,
    DOWNLOAD_HOSTS,
    SnapshotError,
    check_download_counts,
    info,
    load_json,
    newest_first,
    read_index_status,
    read_listings,
    serialize,
    warn,
)
from hosts import HostError
from stamp_release import StampError

COUNTED_TYPES = ("mod", "mod-loader")

# Bump it when the cache layout changes. A cold cache only costs conditional requests.
CACHE_VERSION = 1


def usable(observation):
    """Whether a remembered observation has the shape `observe` produces."""
    if not isinstance(observation, dict) or not isinstance(observation.get("versions"), dict):
        return False
    values = [observation.get("total"), *observation["versions"].values()]
    return all(
        value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
        for value in values
    )


def observe_github(host, remembered):
    """The counts a GitHub repository reports, as {"total": n, "versions": {...}}.

    The request is conditional only for a one-page answer, because an ETag covers the first page only.
    """
    etag = None
    if remembered.get("pages") == 1 and usable(remembered.get("observation")):
        etag = remembered.get("etag")

    releases, answered_etag = host.releases(etag)
    if releases is None:
        return remembered["observation"]
    if host.truncated:
        raise HostError(
            f"{host.key} lists more releases than one scan covers, so its total would be short"
        )

    total = None
    versions = {}
    for release in releases:
        if release.downloads is None:
            continue
        total = (total or 0) + release.downloads
        if release.version is not None:
            versions[release.version] = versions.get(release.version, 0) + release.downloads

    observation = {"total": total, "versions": versions}
    remembered.clear()
    if answered_etag:
        remembered.update({"etag": answered_etag, "pages": host.pages, "observation": observation})
    return observation


def observe_spacedock(host):
    """The counts a SpaceDock mod reports."""
    releases, _ = host.releases()
    if host.downloads is None:
        raise HostError(f"{host.key} answered without a download total")
    versions = {}
    for release in releases:
        if release.version is not None and release.downloads is not None:
            versions[release.version] = versions.get(release.version, 0) + release.downloads
    return {"total": host.downloads, "versions": versions}


def observe(host, remembered):
    """What one host reports now. Raises HostError or StampError when it cannot say."""
    if host.kind == "github":
        return observe_github(host, remembered)
    return observe_spacedock(host)


def stored_values(entry):
    """A stored listing entry as {host: (total, {version: count})}."""
    if entry is None:
        return {}
    values = {host: (total, {}) for host, total in entry["hosts"].items()}
    for release in entry["releases"]:
        for host, count in release["hosts"].items():
            values[host][1][release["version"]] = count
    return values


def merge(kind, stored, observation):
    """One host's (total, versions) after an observation, keeping versions the answer lacks.

    On GitHub a kept version also keeps its share of the total. SpaceDock reports its own total.
    """
    total, versions = stored if stored is not None else (None, {})
    kept = {
        version: count
        for version, count in versions.items()
        if version not in observation["versions"]
    }
    merged = {**kept, **observation["versions"]}
    if observation["total"] is None:
        return total, merged
    if kind == "github":
        return observation["total"] + sum(kept.values()), merged
    return observation["total"], merged


def entry_of(identifier, values):
    """One listing entry from {host: (total, versions)}, or None when nothing is known."""
    known = [
        (host, values[host])
        for host in DOWNLOAD_HOSTS
        if host in values and values[host][0] is not None
    ]
    if not known:
        return None

    per_version = {}
    for host, (_, versions) in known:
        for version, count in versions.items():
            per_version.setdefault(version, {})[host] = count
    releases = [
        {"version": version, "total": sum(counts.values()), "hosts": counts}
        for version, counts in per_version.items()
    ]
    totals = {host: total for host, (total, _) in known}
    return {
        "id": identifier,
        "total": sum(totals.values()),
        "hosts": totals,
        "releases": newest_first(releases, f"the counts of {identifier}"),
    }


def named_hosts(document, http, identifier, log):
    try:
        return list(hosts.named(document.get("releases"), http, identifier).values())
    except StampError as error:
        log(f"{identifier}: [releases] cannot be read, so its last known counts stay: {error}")
        return []


def refresh(authored, current, http, cache, log=warn, note=info, ask=True):
    """The refreshed document. With `ask` false it only drops the entries of deleted listings."""
    listings = read_listings(authored)
    whole, _, _ = read_index_status(authored)
    current = current or {}

    for key in sorted(set(current) - set(listings)):
        note(f"{current[key]['id']}: no longer a listing, so its counts are dropped")

    asked = set()
    entries = []
    for key in sorted(listings):
        document = listings[key]
        identifier = document["id"]
        values = stored_values(current.get(key))
        status = whole.get(key)

        if not ask:
            pass
        elif status is not None and status["state"] == "delisted":
            note(f"{identifier}: delisted, so its hosts are not asked")
        elif document.get("type") not in COUNTED_TYPES:
            note(f"{identifier}: a {document.get('type')!r} listing has no counts")
        else:
            for host in named_hosts(document, http, identifier, log):
                slot = f"{key}/{host.key}"
                asked.add(slot)
                remembered = cache.setdefault(slot, {})
                previous = remembered.get("observation")
                try:
                    observation = observe(host, remembered)
                except (HostError, StampError) as error:
                    log(
                        f"{identifier}: {host.kind} could not be counted, so its last "
                        f"known values stay: {error}"
                    )
                    continue
                except Exception as error:  # noqa: BLE001 - one host never stops the refresh
                    log(
                        f"{identifier}: {host.kind} failed unexpectedly, so its last "
                        f"known values stay: {error!r}"
                    )
                    continue
                if previous is not None and observation is previous:
                    note(f"{identifier}: {host.key} is unchanged since the last run")
                values[host.kind] = merge(host.kind, values.get(host.kind), observation)

        entry = entry_of(identifier, values)
        if entry is not None:
            entries.append(entry)

    if ask:
        for slot in [slot for slot in cache if slot not in asked]:
            del cache[slot]
    return {"spec_version": DOWNLOAD_COUNTS_VERSION, "listings": entries}


def read_current(path):
    """The stored counts keyed by lowercased id, or None when there is no file yet."""
    if not path.is_file():
        return None
    return check_download_counts(load_json(path), str(path))


def read_cache(path, note=info):
    """The remembered GitHub answers. A cache that cannot be read starts cold."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        note(f"the derived cache could not be read and starts cold: {error}")
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    remembered = data.get("hosts")
    if not isinstance(remembered, dict):
        return {}
    return {slot: value for slot, value in remembered.items() if isinstance(value, dict)}


def write_cache(path, cache, note=info):
    """Best effort: a cache that cannot be written costs the next run its 304s."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump({"version": CACHE_VERSION, "hosts": cache}, handle, indent=1, sort_keys=True)
            handle.write("\n")
    except OSError as error:
        note(f"the derived cache could not be written: {error}")


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description="Refresh download-counts.json from the release hosts."
    )
    parser.add_argument(
        "--authored", default=".authored", type=Path,
        help="a checkout of the authored repository",
    )
    parser.add_argument(
        "--counts", default="download-counts.json", type=Path,
        help="the document to refresh",
    )
    parser.add_argument(
        "--cache", type=Path,
        help="the derived cache of GitHub ETags",
    )
    parser.add_argument(
        "--prune-only", action="store_true",
        help="ask no host, only drop the counts of deleted listings",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the document, write nothing",
    )
    return parser.parse_args(argv)


def main(argv=None, http=None):
    arguments = parse_arguments(argv)
    token = os.environ.get("GITHUB_TOKEN")
    http = http or hosts.Http(token=token, log=info)

    try:
        current = read_current(arguments.counts)
        if arguments.prune_only and current is None:
            info(f"{arguments.counts} does not exist, so there is nothing to drop")
            return 0
        cache = {} if arguments.prune_only else read_cache(arguments.cache)
        document = refresh(
            arguments.authored, current, http, cache, ask=not arguments.prune_only
        )
        rendered = serialize(document)
        check_download_counts(json.loads(rendered), "the refreshed counts")
    except SnapshotError as error:
        print(f"cannot refresh the download counts: {error}", file=sys.stderr)
        return 1

    requests = getattr(http, "requests", 0)
    if arguments.dry_run:
        sys.stdout.write(rendered)
    elif arguments.counts.is_file() and arguments.counts.read_bytes() == rendered.encode("utf-8"):
        info(f"{arguments.counts} is already current, so nothing is written")
    else:
        arguments.counts.parent.mkdir(parents=True, exist_ok=True)
        with arguments.counts.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        info(f"wrote {arguments.counts}")

    if not arguments.dry_run and not arguments.prune_only:
        write_cache(arguments.cache, cache)
    info(f"{len(document['listings'])} listing(s) with counts, {requests} host request(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
