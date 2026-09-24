#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The amendment class of RFC 0031, and what RFC 0079 adds for the verified owner of the listing.

A steward may narrow a release: a yank, adding or lowering `game_max`, raising `game_min`, tightening a dependency or loader bound, and adding a dependency entry that was missing.
The owner may also lower `game_min`, raise or remove `game_max`, change `os`, loosen or remove a loader or dependency bound, change the kind of an entry, remove an authored entry, and take back a yank.
A dependency the archive's mod.toml declares stays in the file, and anything else is not an amendment at all.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stamp_release import (
    DEPENDENCY_KINDS,
    SEMVER,
    StampError,
    game_revision,
    normalize_version,
    valid_id,
)

RELEASE_PATH = re.compile(r"^releases/([^/]+)/(.+)\.[Jj][Ss][Oo][Nn](?![\s\S])")

# Everything the stamper writes, plus the two keys only an amendment adds.
TOP_LEVEL = frozenset(
    {
        "spec_version",
        "id",
        "type",
        "version",
        "version_scheme",
        "release_status",
        "release_date",
        "game_min",
        "game_min_revision",
        "game_max",
        "game_max_revision",
        "os",
        "download",
        "install_size",
        "install",
        "loader",
        "dependencies",
        "changelog",
        "changelog_text",
        "listing",
        "yanked",
        "yanked_reason",
    }
)

IMMUTABLE = (
    "spec_version",
    "id",
    "type",
    "version",
    "version_scheme",
    "release_status",
    "release_date",
    "download",
    "install_size",
    "install",
    "changelog",
    "listing",
)

OS_VALUES = ("windows", "linux", "macos")

LOADER_KEYS = frozenset({"id", "min", "max", "source"})
DEPENDENCY_KEYS = frozenset({"id", "any_of", "kind", "min", "max", "source"})
MEMBER_KEYS = frozenset({"id", "min", "max"})

SOURCES = ("authored", "derived")

# The kinds an `any_of` entry may have (RFC 0031), as the stamp also enforces.
ANY_OF_KINDS = ("required", "recommends")


class Outcome:
    """One file's verdict, in the shape `validate.py` reports.

    `owner_only` says that only the verified owner of the listing may make this amendment.
    """

    def __init__(self, outcome, messages=(), owner_only=False):
        self.outcome = outcome
        self.messages = list(messages)
        self.owner_only = owner_only


def precedence(version):
    """A sort key ordering versions by SemVer 2.0.0 precedence, item 11 included."""
    match = SEMVER.match(version or "")
    if match is None:
        raise ValueError(f"'{version}' does not parse as SemVer 2.0.0")

    core = (int(match.group("major")), int(match.group("minor")), int(match.group("patch")))
    prerelease = match.group("prerelease")
    if prerelease is None:
        return core, (1,)

    identifiers = []
    for part in prerelease.split("."):
        # A numeric identifier compares as a number, and always sorts below a text one.
        if part.isdigit():
            identifiers.append((0, int(part), ""))
        else:
            identifiers.append((1, 0, part))
    return core, (0,) + tuple(identifiers)


def _bound(value, what, errors, side="proposed"):
    """A stamped bound as a sort key, or None with the problem reported.

    A stamped bound is normalized, so a leading `v` says it was written by hand.
    `side` keeps a value the author never touched from being reported as theirs.
    """
    where = what if side == "proposed" else f"the published {what}"
    if not isinstance(value, str):
        errors.append(f"{where} is not a version string")
        return None
    try:
        normalized = normalize_version(value)
    except StampError as error:
        errors.append(f"{where}: {error}")
        return None
    if normalized != value:
        errors.append(f"{where} '{value}' is not normalized, and a stamped bound is")
        return None
    return precedence(normalized)


def _compare_min(base, head, what, errors, owner_only):
    """A `min` that is lowered or removed widens the release."""
    if head is None:
        if base is not None:
            owner_only.append(f"{what} removes its min '{base}', which widens the release")
        return
    high = _bound(head, f"{what} min", errors)
    if high is None or base is None:
        return
    low = _bound(base, f"{what} min", errors, side="published")
    if low is not None and high < low:
        owner_only.append(
            f"{what} lowers its min from '{base}' to '{head}', which widens the release"
        )


def _compare_max(base, head, what, errors, owner_only):
    """A `max` that is raised or removed widens the release."""
    if head is None:
        if base is not None:
            owner_only.append(f"{what} removes its max '{base}', which widens the release")
        return
    low = _bound(head, f"{what} max", errors)
    if low is None or base is None:
        return
    high = _bound(base, f"{what} max", errors, side="published")
    if high is not None and low > high:
        owner_only.append(
            f"{what} raises its max from '{base}' to '{head}', which widens the release"
        )


def _bounds_agree(entry, what, errors):
    """A `max` below its own `min` is a range nothing satisfies."""
    if entry.get("min") is None or entry.get("max") is None:
        return
    low = _bound(entry["min"], f"{what} min", errors)
    high = _bound(entry["max"], f"{what} max", errors)
    if low is not None and high is not None and high < low:
        errors.append(f"{what} ends up with max '{entry['max']}' below min '{entry['min']}'")


def _unknown(mapping, allowed, what, errors):
    for key in sorted(set(mapping) - set(allowed)):
        errors.append(f"{what} carries '{key}', which a release file does not have")


def check_path(path, head, errors):
    """The file sits where its own id and version say it does."""
    match = RELEASE_PATH.match(path)
    if match is None:
        errors.append(f"{path} is not a release file")
        return
    folder, stem = match.group(1), match.group(2)
    if not path.endswith(".json"):
        errors.append(f"{path} does not end in a lowercase .json")
    if "/" in stem:
        errors.append(f"{path} sits below releases/<id>/, where a release file does not")
        return
    if not valid_id(folder):
        errors.append(f"{path} names '{folder}', which is not a valid content id")
    if head.get("id") != folder:
        errors.append(f"{path} carries the id '{head.get('id')}', and the folder says '{folder}'")
    if head.get("version") != stem:
        errors.append(
            f"{path} carries the version '{head.get('version')}', and the file name says '{stem}'"
        )
        return
    try:
        stored = normalize_version(stem)
    except StampError:
        return  # A new file fails its stamp for it, with the reason.
    if stored != stem:
        errors.append(
            f"{path} names the version '{stem}', which the index stores as '{stored}', "
            f"so the file is releases/{folder}/{stored}.json with the version '{stored}'"
        )


def check_immutable(base, head, errors):
    for key in IMMUTABLE:
        if base.get(key) == head.get(key):
            continue
        errors.append(
            f"'{key}' changed, and identity, the version, the download and the "
            "install data never change after publish"
        )
        if key == "download" and _only_mirrors_differ(base.get(key), head.get(key)):
            # The author probably branched before the watcher added a mirror, so it
            # now looks like their own edit. A rebase clears it up.
            errors.append(
                "only 'download.mirrors' differs, which the watcher appends on the "
                "default branch: rebase and run tools/amend.py again"
            )


def _only_mirrors_differ(base, head):
    if not isinstance(base, dict) or not isinstance(head, dict):
        return False
    return {key: value for key, value in base.items() if key != "mirrors"} == {
        key: value for key, value in head.items() if key != "mirrors"
    }


def check_changelog_text(base, head, errors):
    """Only the watcher adds `changelog_text`, and nobody changes or removes it (RFC 0064)."""
    absent = object()
    before = base.get("changelog_text", absent)
    after = head.get("changelog_text", absent)
    if before == after:
        return
    if before is absent:
        errors.append("'changelog_text' is added, and only the watcher adds it, from the release host")
    elif after is absent:
        errors.append(
            "'changelog_text' is removed, and nobody removes it. The watcher adds it on "
            "the default branch: rebase and run tools/amend.py again"
        )
    else:
        errors.append("'changelog_text' changed, and it never changes after it was added")


def check_os(base, head, errors, owner_only):
    """Only the owner changes `os`, and absent means no known restriction (RFC 0031)."""
    if base.get("os") == head.get("os"):
        return
    platforms = head.get("os")
    if platforms is not None and (
        not isinstance(platforms, list)
        or not platforms
        or any(platform not in OS_VALUES for platform in platforms)
        or len(set(platforms)) != len(platforms)
    ):
        errors.append(f"os is absent or a list of distinct platforms from {', '.join(OS_VALUES)}")
        return
    owner_only.append(f"os changes from {_platforms(base)} to {_platforms(head)}")


def _platforms(document):
    return ", ".join(document.get("os") or []) or "no restriction"


def check_game_bounds(base, head, errors, owner_only):
    """Lowering `game_min`, or raising or removing `game_max`, widens the release."""
    base_min, head_min = base.get("game_min_revision"), head.get("game_min_revision")
    if not isinstance(base_min, int):
        # The stamper always writes one, so a file without it was never stamped.
        errors.append(
            "the published version carries no game_min_revision, so the lower bound "
            "cannot be compared"
        )
    if not isinstance(head_min, int):
        errors.append("game_min_revision is missing or is not a number")
    elif isinstance(base_min, int) and head_min < base_min:
        owner_only.append(
            f"game_min_revision falls from {base_min} to {head_min}, which widens the release"
        )

    base_max, head_max = base.get("game_max_revision"), head.get("game_max_revision")
    if head_max is None:
        if base_max is not None:
            owner_only.append(
                f"game_max_revision {base_max} is removed, which widens the release"
            )
    elif not isinstance(head_max, int):
        errors.append("game_max_revision is not a number")
    elif isinstance(base_max, int) and head_max > base_max:
        owner_only.append(
            f"game_max_revision rises from {base_max} to {head_max}, which widens the release"
        )

    for which in ("game_min", "game_max"):
        display, revision = head.get(which), head.get(f"{which}_revision")
        if display is None and revision is None:
            continue
        if display is None or revision is None:
            errors.append(f"{which} and {which}_revision have to appear together")
            continue
        stated = game_revision(display) if isinstance(display, str) else None
        if stated is None:
            errors.append(f"{which} '{display}' is not a game version string")
        elif isinstance(display, str) and display.startswith("v"):
            errors.append(f"{which} '{display}' carries a leading v, and the game shows none")
        elif stated != revision:
            errors.append(
                f"{which} '{display}' is revision {stated}, and {which}_revision says {revision}"
            )

    if isinstance(head_min, int) and isinstance(head_max, int) and head_max < head_min:
        errors.append(
            f"game_max_revision {head_max} ends up below game_min_revision {head_min}, "
            "so the compatibility range is empty"
        )


def check_yank(base, head, errors, owner_only):
    """A yank is the author retracting one build, and only the owner takes it back."""
    base_yanked, head_yanked = base.get("yanked"), head.get("yanked")

    if head_yanked is not None and head_yanked is not True:
        errors.append("yanked is true on a retracted release and absent otherwise")
    elif base_yanked is True and head_yanked is not True:
        owner_only.append("the release is un-yanked, which widens the release")

    reason = head.get("yanked_reason")
    if reason is None:
        return
    if head_yanked is not True:
        errors.append("yanked_reason says nothing without yanked")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("yanked_reason is empty")


def check_loader(base, head, errors, owner_only):
    """Loader bounds move on an existing entry, and the entry itself is fixed."""
    base_loader, head_loader = base.get("loader"), head.get("loader")

    if head_loader is None:
        if base_loader is not None:
            errors.append("the loader requirement is removed, which widens the release")
        return
    if not isinstance(head_loader, dict):
        errors.append("loader is not an object")
        return

    _unknown(head_loader, LOADER_KEYS, "loader", errors)

    if base_loader is None:
        errors.append(
            "a loader requirement is added where the release had none, and the "
            "amendment class covers a missing dependency entry, not a loader"
        )
        return

    if head_loader.get("id") != base_loader.get("id"):
        errors.append(
            f"the loader changes from '{base_loader.get('id')}' to '{head_loader.get('id')}', "
            "which is a different requirement rather than a tighter one"
        )
    if head_loader.get("source") != base_loader.get("source"):
        errors.append("the loader's source changed, and it records where the bounds came from")

    _compare_min(base_loader.get("min"), head_loader.get("min"), "the loader", errors, owner_only)
    _compare_max(base_loader.get("max"), head_loader.get("max"), "the loader", errors, owner_only)
    _bounds_agree(head_loader, "the loader", errors)


def entry_key(entry):
    """What identifies an entry across the two versions of the file.

    Ids compare case insensitively (RFC 0031).
    An `any_of` entry has no single id, so its members name it, sorted so the error order does not move.
    """
    if "any_of" in entry:
        members = entry.get("any_of") or []
        return "any_of", tuple(
            sorted(
                (member.get("id") or "").lower()
                for member in members
                if isinstance(member, dict)
            )
        )
    return "id", (entry.get("id") or "").lower()


def _describe(key):
    kind, value = key
    if kind == "id":
        return f"the dependency '{value}'"
    return "the any_of dependency on " + ", ".join(value or ["nothing"])


def _check_entry_shape(entry, what, errors):
    """What every entry has to look like, whether it is new or was there before."""
    _unknown(entry, DEPENDENCY_KEYS, what, errors)

    if entry.get("kind") not in DEPENDENCY_KINDS:
        errors.append(f"{what} has kind '{entry.get('kind')}', which is not a dependency kind")
    if entry.get("source") not in SOURCES:
        errors.append(f"{what} has source '{entry.get('source')}', which is not a source")
    elif entry.get("source") == "derived" and (
        entry.get("min") is not None or entry.get("max") is not None
    ):
        # The loader's `[[StarMap.ModDependencies]]` has no versions, so a derived
        # entry can never have carried a bound.
        errors.append(f"{what} is derived and carries a bound, which no derivation produces")

    if "any_of" in entry:
        if entry.get("id"):
            errors.append(f"{what} carries both id and any_of")
        if entry.get("kind") in DEPENDENCY_KINDS and entry.get("kind") not in ANY_OF_KINDS:
            errors.append(
                f"{what} carries any_of with kind '{entry.get('kind')}', and any_of is valid "
                "with kind required or recommends"
            )
        members = entry.get("any_of")
        if not isinstance(members, list) or not members:
            errors.append(f"{what} names no members")
            return
        for member in members:
            if not isinstance(member, dict) or not member.get("id"):
                errors.append(f"{what} has a member with no id")
                continue
            _unknown(member, MEMBER_KEYS, f"{what} member '{member['id']}'", errors)
            _bounds_agree(member, f"{what} member '{member['id']}'", errors)
    elif not entry.get("id"):
        errors.append(f"{what} carries no id")

    _bounds_agree(entry, what, errors)


def _check_members(base_entry, head_entry, what, errors, owner_only):
    """An `any_of` set is fixed, and each alternative's own bounds may tighten."""
    base_members = {
        (member.get("id") or "").lower(): member
        for member in base_entry.get("any_of") or []
        if isinstance(member, dict)
    }
    head_members = {
        (member.get("id") or "").lower(): member
        for member in head_entry.get("any_of") or []
        if isinstance(member, dict)
    }

    for name in sorted(set(base_members) - set(head_members)):
        errors.append(f"{what} drops the alternative '{name}', which widens the release")
    for name in sorted(set(head_members) - set(base_members)):
        errors.append(f"{what} adds the alternative '{name}', which widens the release")

    for name in sorted(set(base_members) & set(head_members)):
        where = f"{what} member '{name}'"
        if head_members[name].get("id") != base_members[name].get("id"):
            errors.append(f"{where} is renamed, and an id is not rewritten after publish")
        before, after = base_members[name], head_members[name]
        _compare_min(before.get("min"), after.get("min"), where, errors, owner_only)
        _compare_max(before.get("max"), after.get("max"), where, errors, owner_only)


def check_dependencies(base, head, errors, owner_only, derived=None):
    """Entries tighten or arrive, and only the owner loosens, re-kinds or removes one.

    A derived entry is never removed, because the loader acts on it, unless an `any_of` entry names it, as the stamp's merge does with an optional one.
    `derived` is what the archive's mod.toml declares, which shows whether removing an authored entry drops a dependency the loader acts on.
    """
    head_list = head.get("dependencies")
    base_list = base.get("dependencies")
    if not isinstance(head_list, list):
        errors.append("dependencies is missing or is not a list")
        return
    if not isinstance(base_list, list):
        base_list = []

    head_entries = {}
    for entry in head_list:
        if not isinstance(entry, dict):
            errors.append("a dependency entry is not an object")
            continue
        key = entry_key(entry)
        if key in head_entries:
            errors.append(f"{_describe(key)} appears more than once")
            continue
        head_entries[key] = entry

    base_entries = {
        entry_key(entry): entry for entry in base_list if isinstance(entry, dict)
    }

    alternatives = {name for key in head_entries if key[0] == "any_of" for name in key[1]}
    ids = {key[1] for key in head_entries if key[0] == "id"}
    removed = set(base_entries) - set(head_entries)
    # The names of a removed authored `any_of`, whose declared entries come back as derived.
    freed = {
        name
        for key in removed
        if key[0] == "any_of" and base_entries[key].get("source") == "authored"
        for name in key[1]
    }
    for key in sorted(removed, key=str):
        before = base_entries[key]
        if before.get("source") != "derived":
            owner_only.append(f"{_describe(key)} is removed, which widens the release")
            _check_declared(key, ids, alternatives, derived, errors)
        elif before.get("kind") != "optional" or key[1] not in alternatives:
            errors.append(
                f"{_describe(key)} is removed, and a derived entry stays, because the "
                "loader acts on it"
            )

    for key, entry in sorted(head_entries.items(), key=lambda item: str(item[0])):
        what = _describe(key)
        _check_entry_shape(entry, what, errors)

        if key not in base_entries:
            restored = (
                key[0] == "id" and key[1] in freed and derived is not None and entry in derived
            )
            if entry.get("source") != "authored" and not restored:
                errors.append(f"{what} is added with source '{entry.get('source')}', and an "
                              "added entry is authored")
            _check_added_bounds(entry, what, errors)
            continue

        before = base_entries[key]
        if entry.get("kind") != before.get("kind"):
            owner_only.append(
                f"{what} changes kind from '{before.get('kind')}' to '{entry.get('kind')}'"
            )
        if "any_of" not in entry and entry.get("id") != before.get("id"):
            errors.append(f"{what} is renamed, and an id is not rewritten after publish")

        _check_source(before, entry, what, errors, owner_only, derived)
        _compare_min(before.get("min"), entry.get("min"), what, errors, owner_only)
        _compare_max(before.get("max"), entry.get("max"), what, errors, owner_only)
        if "any_of" in entry and "any_of" in before:
            _check_members(before, entry, what, errors, owner_only)


def _check_added_bounds(entry, what, errors):
    """The bounds of an added entry, which no comparison ever parses."""
    _compare_min(None, entry.get("min"), what, errors, errors)
    _compare_max(None, entry.get("max"), what, errors, errors)
    for member in entry.get("any_of") or []:
        if isinstance(member, dict) and member.get("id"):
            where = f"{what} member '{member['id']}'"
            _compare_min(None, member.get("min"), where, errors, errors)
            _compare_max(None, member.get("max"), where, errors, errors)


def _check_declared(key, ids, alternatives, derived, errors):
    """A removed authored entry may have stood in for one the archive's mod.toml declares, which stays.

    An entry of its id keeps it, and an `any_of` that names it keeps it only when it is optional, as in the stamp's merge, because the loader refuses to start without a required one.
    """
    what = _describe(key)
    if derived is None:
        errors.append(
            f"{what} is removed, and the archive was not read, so nothing shows that its "
            "mod.toml does not declare it"
        )
        return
    declared = {entry["id"].lower(): entry.get("kind") for entry in derived}
    names = key[1] if key[0] == "any_of" else (key[1],)
    for name in names:
        if name not in declared or name in ids:
            continue
        if name in alternatives:
            if declared[name] != "optional":
                errors.append(
                    f"{what} is removed, and the archive's mod.toml declares '{name}' as "
                    "required, so an any_of entry cannot take its place"
                )
        else:
            errors.append(
                f"{what} is removed, and the archive's mod.toml declares '{name}', so the "
                "release keeps it as a derived entry"
            )


def _check_source(before, entry, what, errors, owner_only, derived=None):
    """`derived` to `authored` records a bound or a kind authored onto the entry.

    The way back is the owner turning it derived again, exactly as the archive's mod.toml declares it.
    """
    if entry.get("source") == before.get("source"):
        if entry.get("source") == "derived" and entry.get("kind") != before.get("kind"):
            errors.append(
                f"{what} changes kind and stays derived, and a kind the archive's mod.toml "
                "does not declare is authored"
            )
        return
    changed = any(entry.get(key) != before.get(key) for key in ("kind", "min", "max"))
    if before.get("source") == "derived" and entry.get("source") == "authored" and changed:
        return
    if before.get("source") == "authored" and entry.get("source") == "derived":
        owner_only.append(f"{what} changes back to what the archive's mod.toml declares")
        if derived is None:
            errors.append(f"{what} turns derived, and the archive was not read to confirm it")
        elif entry not in derived:
            errors.append(f"{what} turns derived, and the archive's mod.toml does not declare it so")
        return
    errors.append(
        f"{what} changes source from '{before.get('source')}' to '{entry.get('source')}', "
        "and only a derived entry gaining a bound or a kind does that"
    )


def check_document(path, base, head, errors, owner_only=None, derived=None):
    """One release file. `base` is the default branch's version, and is required.

    What only the verified owner of the listing may change goes to `owner_only`, or to `errors` without it, which is the class of a steward acting alone.
    `derived` is what the archive's mod.toml declares, needed when `reads_archive` says so.
    """
    if owner_only is None:
        owner_only = errors
    if not isinstance(head, dict):
        errors.append(f"{path} is not a JSON object")
        return
    if not isinstance(base, dict):
        errors.append(f"{path} has no readable published version to be measured against")
        return

    _unknown(head, TOP_LEVEL, path, errors)
    check_path(path, head, errors)
    check_immutable(base, head, errors)
    check_changelog_text(base, head, errors)
    check_os(base, head, errors, owner_only)
    check_game_bounds(base, head, errors, owner_only)
    check_yank(base, head, errors, owner_only)
    check_loader(base, head, errors, owner_only)
    check_dependencies(base, head, errors, owner_only, derived)


def reads_archive(base, head):
    """Whether an authored dependency entry is removed or turns derived, which only the archive's mod.toml can vouch for."""
    base_list, head_list = base.get("dependencies"), head.get("dependencies")
    if not isinstance(base_list, list) or not isinstance(head_list, list):
        return False
    sources = {entry_key(entry): entry.get("source") for entry in head_list if isinstance(entry, dict)}
    return any(
        entry.get("source") == "authored" and sources.get(entry_key(entry)) != "authored"
        for entry in base_list
        if isinstance(entry, dict)
    )


def check(changes, derived=None):
    """Every changed release file, as `{path: Outcome}`.

    `changes` is an iterable of `(path, base, head)`, where a document is None when the file does not exist on that side.
    `derived` maps a path to what its archive's mod.toml declares.
    """
    results = {}
    for path, base, head in changes:
        errors, notes, owner_only = [], [], []
        if head is None:
            errors.append(
                f"{path} is deleted, and a published release stays. "
                "A release that turns out to be broken is yanked"
            )
        elif base is None:
            # A note, not a rejection: there is no published release to measure
            # against, and the scope rule already hands it to a steward.
            notes.append(f"{path} is a new release file rather than an amendment")
        else:
            check_document(path, base, head, errors, owner_only, (derived or {}).get(path))
        results[path] = Outcome(
            "reject" if errors else "pass", errors + notes + owner_only, owner_only=bool(owner_only)
        )
    return results
