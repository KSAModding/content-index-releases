#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Decide whether a change is narrow enough to merge itself.

RFC 0033 names two shapes. An amendment touches release files of exactly one listing, one or several, so "this mod breaks above game build X" is one pull request rather than one per past release.
A release pull request adds exactly one new file whose path and version do not exist yet.
Anything wider, and any mix of the two, waits for a steward.
"""

import re
from collections import namedtuple

RELEASE = re.compile(r"^releases/([A-Za-z0-9][^/]*)/[^/]+\.[Jj][Ss][Oo][Nn](?![\s\S])")

ADDED = "added"
MODIFIED = "modified"

Change = namedtuple("Change", "path status")


def changes(paths, status=MODIFIED):
    return [Change(path, status) for path in paths]


def is_release(path):
    return RELEASE.match(path) is not None


def listing_of(path):
    match = RELEASE.match(path)
    return match.group(1) if match else None


def releases(changes):
    """The release file paths among `changes`, in the order given."""
    return [change.path for change in changes if is_release(change.path)]


def added(changes):
    """The release files the change adds, in the order given."""
    return [
        change.path for change in changes if is_release(change.path) and change.status == ADDED
    ]


def evaluate(changes):
    """Whether this set of changes is an auto-merge candidate.

    Returns (candidate, paths, reason).
    The reason is written for the author when the answer is no, and is empty when it is yes.
    """
    changes = list(changes)
    found = releases(changes)
    other = [change.path for change in changes if not is_release(change.path)]

    if not changes:
        return False, [], "the change touches no file at all"
    if not found:
        return False, [], "the change touches no release file"
    if other:
        listed = ", ".join(sorted(other)[:5])
        if len(other) > 5:
            listed += f", and {len(other) - 5} more"
        return False, found, f"the change also touches {listed}"

    for change in changes:
        if change.status not in (ADDED, MODIFIED):
            return (
                False,
                found,
                f"the change {change.status} {change.path}, and a release file is added "
                "once and only ever amended after that",
            )

    new = added(changes)
    if new and len(new) < len(changes):
        return (
            False,
            found,
            f"the change adds {new[0]} next to an amendment, and a release pull request "
            "adds exactly one new file and nothing else",
        )
    if len(new) > 1:
        return (
            False,
            found,
            f"the change adds {len(new)} release files, and a release pull request adds "
            "exactly one",
        )
    if new:
        return True, found, ""

    listings = {listing_of(path) for path in found}
    if len(listings) > 1:
        named = ", ".join(sorted(listings))
        return False, found, f"the change touches {len(listings)} listings ({named}), and an amendment touches one"

    return True, found, ""
