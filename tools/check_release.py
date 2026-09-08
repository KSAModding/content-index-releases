#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A submitted release file, measured against its own archive.

RFC 0033: a release pull request adds exactly one new file under `releases/<id>/<version>.json`, for a listing without a `[releases]` section.
The checks do not trust the submitted document.
They download the archive from its `download.url`, stamp the release again with `stamp_release.py`, which is the code the watcher stamps with, and reject the submission when any field disagrees.

Three facts only the host knows and no archive carries: the release date, the pre-release flag, and the changelog link.
Those are taken from the submission as the author's word. Everything else is derived.
"""

import json
import os
import sys
import tomllib
import urllib.parse
from datetime import datetime, timezone
from http.client import InvalidURL
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hosts
from check_amendment import Outcome, check_path
from stamp_release import StampError, prerelease_identifiers, stamp

PASS = "pass"
REJECT = "reject"
COULD_NOT_EVALUATE = "could-not-evaluate"

# The sibling checkout the workflow makes. A local run either has one next to
# this repository or points at one.
DEFAULT_AUTHORED = Path(__file__).resolve().parent.parent.parent / "content-index"

# What the stamper writes for an archive it opened as a zip.
ZIP_CONTENT_TYPES = ("application/zip", "application/x-zip-compressed")

SCHEMES = ("http", "https")

# The form every stamped release date has.
RELEASE_DATE = "%Y-%m-%dT%H:%M:%SZ"

# Set by an amendment after publish, so a submission never carries them.
AMENDMENT_ONLY = ("yanked", "yanked_reason")


def authored_root(authored=None):
    """The authored checkout: the argument, the environment, then the sibling."""
    return Path(authored or os.environ.get("CONTENT_INDEX") or DEFAULT_AUTHORED)


def listing_path(root, listing_id):
    return Path(root) / "listings" / f"{listing_id}.toml"


def authored_document(root, listing_id):
    """The listing from the content-index checkout, as (document, problem)."""
    path = listing_path(root, listing_id)
    if not path.is_file():
        return None, f"the listing '{listing_id}' is not in content-index"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return None, f"listings/{listing_id}.toml does not parse: {error}"


def facts(head):
    """The release facts the stamper takes, read off the submission.

    The pre-release flag is read back out of `release_status`: `testing` on a version with no pre-release identifiers can only have come from the host's flag.
    Every other value the stamper derives from the version alone, so a status the version contradicts fails the comparison.
    """
    download = head.get("download") or {}
    version = head.get("version") or ""
    return {
        "tag": version,
        "release_date": head.get("release_date"),
        "url": download.get("url"),
        "content_type": download.get("content_type"),
        "prerelease": head.get("release_status") == "testing"
        and not prerelease_identifiers(version),
        "changelog": head.get("changelog"),
    }


def _address(url):
    """Whether `url` is an http or https address with a host."""
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in SCHEMES and bool(parts.netloc)


def check_shape(path, head, errors):
    """What has to hold before the archive is fetched.

    The URL is the author's, so it is checked before anything is requested from it.
    """
    check_path(path, head, errors)

    for key in AMENDMENT_ONLY:
        if key in head:
            errors.append(
                f"'{key}' is set, and a release is yanked after publish through an "
                "amendment, never submitted yanked"
            )

    download = head.get("download")
    if not isinstance(download, dict):
        errors.append("download is missing or is not an object")
    else:
        if "mirrors" in download:
            errors.append(
                "download.mirrors is the watcher's to append, once a further host from "
                "[releases] serves the same bytes, and a submitted release names one host"
            )
        if not _address(download.get("url")):
            errors.append(f"download.url {download.get('url')!r} is not an http or https address")
        if download.get("content_type") not in ZIP_CONTENT_TYPES:
            errors.append(
                f"download.content_type {download.get('content_type')!r} is not what the "
                f"stamper writes for a zip ({' or '.join(ZIP_CONTENT_TYPES)})"
            )
        size = download.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append("download.size is not a byte count")

    date = head.get("release_date")
    try:
        datetime.strptime(date, RELEASE_DATE)
    except (TypeError, ValueError):
        errors.append(
            f"release_date {date!r} is not a UTC timestamp of the form 2026-08-05T17:48:57Z"
        )

    changelog = head.get("changelog")
    if changelog is not None and (not isinstance(changelog, str) or not changelog.strip()):
        errors.append("changelog is present and empty, and the stamper leaves an empty one out")


def check_listing(folder, document, errors):
    """The listing the release belongs to, against the folder it lands in."""
    if document.get("id") != folder:
        errors.append(
            f"the folder releases/{folder}/ does not match the listing's id "
            f"'{document.get('id')}' letter for letter, and the folder name is the "
            "identity the game sees"
        )
    if document.get("releases"):
        errors.append(
            "the listing names a release host under [releases], so the watcher stamps "
            "its releases from there; an older release is stamped by dispatching the "
            "watcher with backfill"
        )


def _show(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 120 else text[:117] + "..."


def _differ(left, right):
    """The keys on which two objects disagree, in order."""
    return [key for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)]


def compare(submitted, derived):
    """Every field where the submission and the derivation disagree, as messages."""
    messages = []
    for key in sorted(set(submitted) | set(derived)):
        if key not in derived:
            messages.append(f"'{key}' is not something the stamper writes for this release")
        elif key not in submitted:
            messages.append(f"'{key}' is missing, and stamping the archive gives {_show(derived[key])}")
        elif submitted[key] == derived[key]:
            continue
        elif key == "download" and isinstance(submitted[key], dict):
            for name in _differ(submitted[key], derived[key]):
                messages.append(
                    f"download.{name}: the submission says {_show(submitted[key].get(name))}, "
                    f"and the archive gives {_show(derived[key].get(name))}"
                )
        elif key == "listing" and isinstance(submitted[key], dict):
            names = ", ".join(_differ(submitted[key], derived[key]))
            messages.append(
                f"listing: the frozen facts differ from the authored document as "
                f"content-index has it now ({names}); stamp the release again against "
                "that document"
            )
        else:
            messages.append(
                f"{key}: the submission says {_show(submitted[key])}, and stamping the "
                f"archive gives {_show(derived[key])}"
            )
    return messages


def check(path, head, root, game_versions, http, now=None):
    """One submitted release file, as an Outcome.

    `head` is the submitted document and `root` the content-index checkout.
    The caller has already established that nothing is published at `path`.
    """
    now = now or datetime.now(timezone.utc)

    if not isinstance(head, dict):
        return Outcome(REJECT, [f"{path} is not a JSON object"])

    errors = []
    check_shape(path, head, errors)
    if errors:
        return Outcome(REJECT, errors)

    # `check_path` passed, so the path is releases/<id>/<version>.json.
    folder = path.split("/")[1]
    if not listing_path(root, folder).is_file():
        return Outcome(
            REJECT,
            [
                f"the listing '{folder}' is not in content-index: a release is "
                "submitted for a listed id, so the listing comes first"
            ],
        )
    document, problem = authored_document(root, folder)
    if document is None:
        return Outcome(COULD_NOT_EVALUATE, [problem])
    check_listing(folder, document, errors)
    if errors:
        return Outcome(REJECT, errors)

    download = head["download"]
    release = hosts.HostRelease(
        host="submitted",
        tag=head["version"],
        version=head["version"],
        release_date=head["release_date"],
        url=download["url"],
        content_type=download["content_type"],
        size=download["size"],
    )
    try:
        archive, content_type = hosts.download(http, release)
    except StampError as error:
        return Outcome(REJECT, [str(error)])
    except hosts.HostError as error:
        return Outcome(
            COULD_NOT_EVALUATE, [f"the archive could not be downloaded this run: {error}"]
        )
    except (ValueError, InvalidURL) as error:
        return Outcome(REJECT, [f"download.url could not be requested: {error}"])

    submitted = facts(head)
    submitted["content_type"] = content_type
    try:
        derived = stamp(document, submitted, archive, game_versions, now=now)
    except StampError as error:
        return Outcome(REJECT, [f"the release does not stamp: {error}"])

    differences = compare(head, derived)
    if differences:
        return Outcome(REJECT, differences)
    return Outcome(
        PASS,
        [
            f"every stamped field agrees with the archive at {download['url']} "
            f"({len(archive)} bytes, sha256 {derived['download']['sha256']})"
        ],
    )
