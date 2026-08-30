#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Derive an amendment to published release files.

This is one of three ways to make an amendment, next to editing the files by hand and letting a content manager such as Borea produce the pull request.

This tool does the arithmetic, and re-checks its own output before it reaches disk, so it cannot write what the pull request would then be rejected for.

    python3 tools/amend.py --listing AdvancedFlightComputer --up-to 0.7.2 --game-max 2026.8.19.5261
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_amendment
from check_amendment import precedence
from stamp_release import DEPENDENCY_KINDS, StampError, normalize_version, resolve_bound, serialize

AFTER_GAME_MIN = "game_min_revision"

# The order the stamper writes the keys of a dependency entry in.
ENTRY_ORDER = ("id", "any_of", "kind", "min", "max", "source")

LOADER_ORDER = ("id", "min", "max", "source")


class AmendError(Exception):
    """The amendment cannot be derived."""


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AmendError(f"{path} is not readable JSON: {error}") from error


def stamped(releases_root, listing_id):
    """Every stamped release of one listing, newest first."""
    folder = Path(releases_root) / listing_id
    if not folder.is_dir():
        raise AmendError(f"{folder} does not exist, so the listing has no stamped release")

    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            key = precedence(path.stem)
        except ValueError as error:
            raise AmendError(f"{path}: {error}") from error
        found.append((key, path))
    if not found:
        raise AmendError(f"{folder} holds no release file")
    return [path for _, path in sorted(found, reverse=True)]


def select(paths, versions=None, up_to=None, everything=False):
    """The release files an amendment applies to."""
    if everything:
        return list(paths)

    if up_to is not None:
        try:
            ceiling = precedence(up_to)
        except ValueError as error:
            raise AmendError(str(error)) from error
        chosen = [path for path in paths if precedence(path.stem) <= ceiling]
        if not chosen:
            raise AmendError(f"no stamped release is at or below {up_to}")
        return chosen

    wanted = list(versions or [])
    by_version = {path.stem: path for path in paths}
    missing = [version for version in wanted if version not in by_version]
    if missing:
        raise AmendError(f"not stamped: {', '.join(missing)}")
    return [by_version[version] for version in wanted]


def _reorder(entry, order):
    """The keys an entry carries, in the order the stamper writes them."""
    ordered = {key: entry[key] for key in order if key in entry}
    ordered.update({key: value for key, value in entry.items() if key not in ordered})
    return ordered


def set_game_bound(document, which, display, revision):
    """Write a resolved game bound, where a fresh stamp would put it."""
    if document.get(which) == display and document.get(f"{which}_revision") == revision:
        return False

    if which in document:
        document[which] = display
        document[f"{which}_revision"] = revision
        return True

    updated = {}
    for key, value in document.items():
        updated[key] = value
        if key == AFTER_GAME_MIN:
            updated[which] = display
            updated[f"{which}_revision"] = revision
    if which not in updated:
        raise AmendError(
            f"the release file carries no {AFTER_GAME_MIN}, so it was not written by "
            "the stamper and there is nowhere to put a game bound"
        )
    document.clear()
    document.update(updated)
    return True


def set_yank(document, reason):
    """Retract one build. `yanked` goes last, as RFC 0031 lists it."""
    changed = document.get("yanked") is not True
    document["yanked"] = True
    if reason is not None and document.get("yanked_reason") != reason:
        document["yanked_reason"] = reason
        changed = True
    return changed


def set_loader_bounds(document, minimum, maximum):
    loader = document.get("loader")
    if loader is None:
        raise AmendError(
            "the release states no loader, and adding one is not an amendment"
        )

    updated = dict(loader)
    changed = False
    for key, value in (("min", minimum), ("max", maximum)):
        if value is not None and updated.get(key) != value:
            updated[key] = value
            changed = True
    if not changed:
        return False
    document["loader"] = _reorder(updated, LOADER_ORDER)
    return True


def _find(dependencies, identifier):
    for index, entry in enumerate(dependencies):
        if (entry.get("id") or "").lower() == identifier.lower():
            return index
    return None


def add_dependency(document, identifier, kind):
    """An entry that was missing, which RFC 0031 admits including a conflict."""
    if kind not in DEPENDENCY_KINDS:
        raise AmendError(f"'{kind}' is not one of {', '.join(DEPENDENCY_KINDS)}")

    dependencies = list(document.get("dependencies") or [])
    if _find(dependencies, identifier) is not None:
        return False
    dependencies.append({"id": identifier, "kind": kind, "source": "authored"})
    document["dependencies"] = dependencies
    return True


def set_dependency_bounds(document, identifier, minimum, maximum):
    dependencies = list(document.get("dependencies") or [])
    index = _find(dependencies, identifier)
    if index is None:
        raise AmendError(
            f"the release states no dependency on '{identifier}'. An entry that was "
            "missing is added with --add-dependency, and an any_of entry is edited by hand"
        )

    entry = dict(dependencies[index])
    changed = False
    for key, value in (("min", minimum), ("max", maximum)):
        if value is not None and entry.get(key) != value:
            entry[key] = value
            changed = True
    if not changed:
        return False

    # Once the author adds the matching authored entry, the next stamp produces exactly this, so it stops being derived now.
    if entry.get("source") == "derived":
        entry["source"] = "authored"

    dependencies[index] = _reorder(entry, ENTRY_ORDER)
    document["dependencies"] = dependencies
    return True


def apply(document, amendment):
    """Every requested change, onto one release file. Returns whether it moved."""
    changed = False

    for which in ("game_min", "game_max"):
        bound = amendment.get(which)
        if bound is not None:
            changed |= set_game_bound(document, which, bound[0], bound[1])

    if amendment.get("yank"):
        changed |= set_yank(document, amendment.get("reason"))

    for identifier, kind in amendment.get("add_dependencies") or []:
        changed |= add_dependency(document, identifier, kind)

    for identifier, (minimum, maximum) in (amendment.get("dependency_bounds") or {}).items():
        changed |= set_dependency_bounds(document, identifier, minimum, maximum)

    if amendment.get("loader_min") or amendment.get("loader_max"):
        changed |= set_loader_bounds(
            document, amendment.get("loader_min"), amendment.get("loader_max")
        )

    return changed


def amend(paths, amendment):
    """Apply the amendment to each file and check the result. Returns the writes.
    """
    writes = []
    for path in paths:
        path = Path(path)
        base = load(path)
        head = json.loads(json.dumps(base))
        if not apply(head, amendment):
            continue

        errors = []

        where = f"releases/{path.parent.name}/{path.name}"
        check_amendment.check_document(where, base, head, errors)
        if errors:
            raise AmendError(
                f"the amendment to {where} would be rejected:\n  "
                + "\n  ".join(errors)
            )
        writes.append((path, serialize(head)))
    return writes


def _bound_pair(values):
    """`ID=VERSION` pairs, each version normalized."""
    pairs = {}
    for value in values or []:
        identifier, separator, version = value.partition("=")
        if not separator or not identifier.strip() or not version.strip():
            raise AmendError(f"'{value}' is not ID=VERSION")
        try:
            pairs[identifier.strip()] = normalize_version(version.strip())
        except StampError as error:
            raise AmendError(f"'{value}': {error}") from error
    return pairs


def _added(values):
    added = []
    for value in values or []:
        identifier, separator, kind = value.partition(":")
        if not separator or not identifier.strip() or not kind.strip():
            raise AmendError(f"'{value}' is not ID:KIND")
        added.append((identifier.strip(), kind.strip()))
    return added


def build_amendment(arguments, game_versions, now):
    """The requested changes, resolved, or an AmendError naming what is wrong."""
    amendment = {
        "yank": arguments.yank,
        "reason": arguments.reason,
        "add_dependencies": _added(arguments.add_dependency),
    }

    for which, value in (("game_min", arguments.game_min), ("game_max", arguments.game_max)):
        if value is None:
            continue
        value = value.strip()
        if value[:1] in ("v", "V"):
            value = value[1:]
        if not value:
            raise AmendError(f"{which} is empty")
        try:
            display, revision = resolve_bound(value, which, game_versions, now)
        except StampError as error:
            raise AmendError(str(error)) from error
        if display is None:
            raise AmendError(
                f"{which} '{value}' names a month that is not over, so it has no last "
                "revision yet. Name a build instead"
            )
        amendment[which] = (display, revision)

    for key, value in (("loader_min", arguments.loader_min), ("loader_max", arguments.loader_max)):
        if value is None:
            continue
        try:
            amendment[key] = normalize_version(value)
        except StampError as error:
            raise AmendError(f"{key.replace('_', ' ')}: {error}") from error

    minimums = _bound_pair(arguments.dependency_min)
    maximums = _bound_pair(arguments.dependency_max)
    bounds = {}
    for identifier in list(minimums) + [key for key in maximums if key not in minimums]:
        bounds[identifier] = (minimums.get(identifier), maximums.get(identifier))
    amendment["dependency_bounds"] = bounds

    if arguments.reason is not None and not arguments.yank:
        raise AmendError("--reason says nothing without --yank")

    wanted = (
        amendment.get("game_min"),
        amendment.get("game_max"),
        amendment["yank"] or None,
        amendment["add_dependencies"] or None,
        bounds or None,
        amendment.get("loader_min"),
        amendment.get("loader_max"),
    )
    if not any(wanted):
        raise AmendError("nothing to amend: name at least one change")
    return amendment


def reminders(amendment):
    """What the index cannot do for the author, said once, at the end."""
    lines = []
    if amendment.get("loader_min") or amendment.get("loader_max"):
        lines.append(
            "The authored [loader] bounds in content-index are separate. Change them "
            "there too, or the next release stamps the old ones."
        )
    if amendment.get("dependency_bounds") or amendment.get("add_dependencies"):
        lines.append(
            "The authored [[dependencies]] in content-index are separate. Change them "
            "there too, or the next release stamps without these bounds."
        )
    if amendment.get("game_max"):
        lines.append(
            "This bounds releases that are already published. Leave the authored "
            "game_max alone unless the content itself is capped, or every future "
            "release is stamped with the bound as well."
        )
    return lines


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listing", required=True, help="the content id being amended")
    parser.add_argument(
        "--releases", default="releases", help="the releases root, defaults to releases"
    )
    parser.add_argument("--game-versions", default="game-versions.json")

    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--version", action="append", default=[], help="one stamped version, repeatable"
    )
    selection.add_argument(
        "--up-to", help="every stamped release at or below this one, by SemVer precedence"
    )
    selection.add_argument(
        "--all", action="store_true", help="every stamped release of the listing"
    )

    parser.add_argument("--game-min", help="raise the lower game bound, a build or a month")
    parser.add_argument("--game-max", help="add or lower the upper game bound")
    parser.add_argument("--yank", action="store_true", help="retract these releases")
    parser.add_argument("--reason", help="one sentence shown with the yank")
    parser.add_argument("--loader-min", help="raise the loader's min")
    parser.add_argument("--loader-max", help="add or lower the loader's max")
    parser.add_argument(
        "--dependency-min", action="append", default=[], metavar="ID=VERSION", help="repeatable"
    )
    parser.add_argument(
        "--dependency-max", action="append", default=[], metavar="ID=VERSION", help="repeatable"
    )
    parser.add_argument(
        "--add-dependency", action="append", default=[], metavar="ID:KIND",
        help="an entry that was missing, repeatable",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="derive everything and write nothing"
    )
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(argv)
    now = datetime.now(timezone.utc)

    try:
        game_versions = load(Path(arguments.game_versions))["versions"]
    except (AmendError, KeyError, TypeError) as error:
        print(f"cannot read the game release list: {error}", file=sys.stderr)
        return 1

    try:
        amendment = build_amendment(arguments, game_versions, now)
        paths = select(
            stamped(arguments.releases, arguments.listing),
            versions=arguments.version,
            up_to=arguments.up_to,
            everything=arguments.all,
        )
        writes = amend(paths, amendment)
    except AmendError as error:
        print(error, file=sys.stderr)
        return 1

    if not writes:
        print("every selected release already says this, so nothing is written")
        return 0

    for path, text in writes:
        if arguments.dry_run:
            print(f"would write {path}")
        else:
            with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            print(f"wrote {path}")

    for line in reminders(amendment):
        print(f"\n{line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
