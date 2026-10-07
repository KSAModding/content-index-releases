#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A merged listing edit, applied to the newest release as an amendment of the verified owner (RFC 0081).

The edit is the difference between the listing as it stood when the most recent release was stamped and the listing now.
Each field it changes is written the way a stamp writes it, and `check_amendment.py` measures the result as it measures an owner's amendment pull request.
The watcher decides when, this module decides what.
"""

import copy
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from amend import AmendError, set_game_bound
from check_amendment import check_document, entry_key, precedence, reads_archive
from stamp_release import StampError, merge_dependencies, resolve_bound, stamp_loader

# Where a stamp puts `os`: after the game bounds, the upper one when there is one.
BEFORE_OS = ("game_max_revision", "game_min_revision")


class Waiting:
    """A changed `game_max` that names a month that is not over yet. Only this field waits until the month is over."""

    def __init__(self, which, bound):
        self.which = which
        self.bound = bound

    def __str__(self):
        return f"{self.which} {self.bound}"


def target(documents):
    """The version an edit goes to: the newest by SemVer precedence that is neither yanked nor `dev`.

    `documents` maps each stamped version to its release file.
    """
    candidates = [
        version
        for version, document in documents.items()
        if document.get("yanked") is not True and document.get("release_status") != "dev"
    ]
    return max(candidates, key=precedence, default=None)


def most_recent(documents):
    """The most recent release by SemVer precedence, yanked and `dev` included, or None.

    The listing as it stood when this release was stamped is what an edit is measured from, so an edit it was stamped with never moves on to an older release.
    """
    return max(documents, key=precedence, default=None)


def changes(before, after, release, game_versions, now, derived, waiting=()):
    """What the edit from `before` to `after` changes in `release`, as {field: (descriptions, apply)}.

    `derived` returns the dependencies the archive's mod.toml declares, and is only called when the listing's dependencies changed.
    A field that `release` already carries as edited is left out, so a second pass finds nothing.
    A `game_max` month that is not over yet maps to a `Waiting`, and the other fields do not wait for it.
    `waiting` names the fields an earlier pass left waiting, which count as edited even when `before` and `after` agree.
    """
    found = {
        "game_min": _game_bound(before, after, "game_min", release, game_versions, now, waiting),
        "game_max": _game_bound(before, after, "game_max", release, game_versions, now, waiting),
        "os": _os(before, after, release),
        "loader": _loader(before, after, release),
        "dependencies": _dependencies(before, after, release, derived),
    }
    return {field: change for field, change in found.items() if change is not None}


def amended(where, release, found, derived):
    """`release` with every change the check of an owner's amendment accepts, as (document, applied, refused).

    Each change is measured on its own and the combined result once more, so one refused change keeps none of the others from landing.
    `applied` holds descriptions, and `refused` pairs the descriptions of a change with the check's errors.
    """
    head = copy.deepcopy(release)
    applied, refused = [], []
    for descriptions, apply in _ready(found):
        errors = _measured(where, release, apply, derived)
        if errors:
            refused.append((descriptions, errors))
            continue
        apply(head)
        applied.extend(descriptions)

    if applied:
        errors = _checked(where, release, head, derived)
        if errors:
            return release, [], refused + [(applied, errors)]
    return head, applied, refused


def refusals(where, release, found, derived):
    """The changes in `found` the check of an owner's amendment refuses, as (descriptions, errors)."""
    measured = [
        (descriptions, _measured(where, release, apply, derived))
        for descriptions, apply in _ready(found)
    ]
    return [(descriptions, errors) for descriptions, errors in measured if errors]


def pending(found):
    """The changes in `found` that wait for a month to end."""
    return [change for change in found.values() if isinstance(change, Waiting)]


def _ready(found):
    return [change for change in found.values() if not isinstance(change, Waiting)]


def _checked(where, release, document, derived):
    errors = []
    try:
        declared = derived() if reads_archive(release, document) else None
    except StampError as error:
        return [str(error)]
    check_document(where, release, document, errors, owner_only=[], derived=declared)
    return errors


def _measured(where, release, apply, derived):
    document = copy.deepcopy(release)
    try:
        apply(document)
    except (AmendError, StampError) as error:
        return [str(error)]
    return _checked(where, release, document, derived)


def _effective(release, descriptions, apply):
    """The change, unless `release` already carries it. One that cannot apply is kept, so its reason is reported."""
    document = copy.deepcopy(release)
    try:
        apply(document)
    except (AmendError, StampError):
        return descriptions, apply
    return None if document == release else (descriptions, apply)


def _raising(error):
    def apply(document):
        raise error
    return apply


def _authored_bound(document, which):
    bound = (document.get("compatibility") or {}).get(which)
    if not isinstance(bound, str):
        return None
    return bound.strip() or None


def _game_bound(before, after, which, release, game_versions, now, waiting=()):
    old, new = _authored_bound(before, which), _authored_bound(after, which)
    if (old == new and which not in waiting) or (new is None and which == "game_min"):
        return None
    if new is None:
        return _effective(release, ["removes game_max"], _remove_game_max)
    try:
        display, revision = resolve_bound(new, which, game_versions, now)
    except StampError as error:
        return [f"sets {which} to {new}"], _raising(error)
    if display is None:
        return Waiting(which, new)

    current = release.get(f"{which}_revision")
    if which not in release:
        description = f"adds {which} {display}"
    elif isinstance(current, int) and revision != current:
        description = f"{'raises' if revision > current else 'lowers'} {which} to {display}"
    else:
        description = f"sets {which} to {display}"
    return _effective(
        release, [description], lambda document: set_game_bound(document, which, display, revision)
    )


def _remove_game_max(document):
    document.pop("game_max", None)
    document.pop("game_max_revision", None)


def _platforms(document):
    platforms = (document.get("compatibility") or {}).get("os")
    return list(platforms) if platforms else None


def _os(before, after, release):
    old, new = _platforms(before), _platforms(after)
    if old == new:
        return None
    if new is None:
        return _effective(release, ["removes os"], lambda document: document.pop("os", None))
    verb = "changes os to" if "os" in release else "adds os"
    return _effective(
        release, [f"{verb} {', '.join(map(str, new))}"], lambda document: _put_os(document, new)
    )


def _put_os(document, platforms):
    if "os" in document:
        document["os"] = list(platforms)
        return
    anchor = next((key for key in BEFORE_OS if key in document), None)
    if anchor is None:
        raise AmendError(
            "the release file carries no game_min_revision, so it was not written by the "
            "stamper and there is nowhere to put os"
        )
    updated = {}
    for key, value in document.items():
        updated[key] = value
        if key == anchor:
            updated["os"] = list(platforms)
    document.clear()
    document.update(updated)


def _loader(before, after, release):
    """New bounds on the same loader. A changed loader id reaches only a future stamp."""
    old, new, stamped = before.get("loader"), after.get("loader"), release.get("loader")
    if not all(isinstance(loader, dict) for loader in (old, new, stamped)):
        return None
    if not old.get("id") == new.get("id") == stamped.get("id"):
        return None
    if (old.get("min"), old.get("max")) == (new.get("min"), new.get("max")):
        return None
    try:
        loader = stamp_loader(new)
    except StampError as error:
        return ["changes the loader bounds"], _raising(error)
    loader["source"] = stamped.get("source", loader["source"])

    descriptions = [
        _bound_change("the loader", which, stamped.get(which), loader.get(which))
        for which in ("min", "max")
        if stamped.get(which) != loader.get(which)
    ]
    return _effective(
        release, descriptions, lambda document: document.update(loader=dict(loader))
    )


def _bound_change(what, which, old, new):
    if old is None:
        return f"adds the {which} {new} to {what}"
    if new is None:
        return f"removes the {which} of {what}"
    try:
        verb = "raises" if precedence(new) > precedence(old) else "lowers"
    except ValueError:
        verb = "sets"
    return f"{verb} the {which} of {what} to {new}"


def _dependencies(before, after, release, derived):
    """The dependency list a stamp would merge from the edited authored entries and the archive's derived ones.

    Only the entries the listing edit touches change, so an entry the release lost to an amendment does not come back.
    """
    try:
        old = _keyed(merge_dependencies([], before.get("dependencies")))
    except StampError:
        return None
    try:
        new = _keyed(merge_dependencies([], after.get("dependencies")))
    except StampError as error:
        return ["changes the dependencies"], _raising(error)
    edited = {key: entry for key, entry in new.items() if old.get(key) != entry}
    removed = set(old) - set(new)
    if not edited and not removed:
        return None

    stamped = [entry for entry in release.get("dependencies") or [] if isinstance(entry, dict)]
    authored = []
    for entry in stamped:
        key = entry_key(entry)
        if entry.get("source") == "authored" and key not in removed:
            authored.append(edited.pop(key, entry))
    authored.extend(edited.values())
    try:
        merged = merge_dependencies(derived(), authored)
    except StampError as error:
        return ["changes the dependencies"], _raising(error)

    # An authored entry that only restates a derived one changes nothing a stamp records,
    # and an entry the release carries keeps its place, so the diff shows only the edit.
    carried = _keyed(stamped)
    merged = [_restated(entry, carried.get(entry_key(entry))) for entry in merged]
    places = {key: place for place, key in enumerate(carried)}
    merged.sort(key=lambda entry: places.get(entry_key(entry), len(places)))
    return _effective(
        release,
        _dependency_changes(stamped, merged),
        lambda document: document.update(dependencies=copy.deepcopy(merged)),
    )


def _keyed(entries):
    return {entry_key(entry): entry for entry in entries}


def _restated(entry, carried):
    if carried is not None and {**entry, "source": None} == {**carried, "source": None}:
        return carried
    return entry


def _label(entry):
    if "any_of" in entry:
        names = ", ".join(member.get("id", "") for member in entry["any_of"])
        return f"the {entry.get('kind')} dependency on any of {names}"
    return f"the {entry.get('kind')} dependency {entry.get('id')}"


def _terms(entry):
    terms = [entry.get("kind")]
    for member in entry.get("any_of") or [entry]:
        name = f"{member.get('id')} " if "any_of" in entry else ""
        terms += [f"{name}{which} {member[which]}" for which in ("min", "max") if member.get(which)]
    return ", ".join(map(str, terms))


def _dependency_changes(before, after):
    old, new = _keyed(before), _keyed(after)
    descriptions = []
    for key, entry in new.items():
        if key not in old:
            descriptions.append(f"adds {_label(entry)}")
        elif _terms(old[key]) != _terms(entry):
            descriptions.append(f"changes {_label(old[key])} to {_terms(entry)}")
    descriptions += [f"removes {_label(entry)}" for key, entry in old.items() if key not in new]
    return descriptions


class History:
    """What git knows about the two checkouts: when a listing changed, when a release was stamped, and which listing commits it was stamped from and received.

    Every answer comes from the first-parent history of the checked-out branch, so a pull request counts from its merge.
    A question git cannot answer is None, and the caller then leaves the release alone.
    """

    def __init__(self, authored_root, releases_root):
        self.authored_root = Path(authored_root)
        self.releases_root = Path(releases_root)

    @staticmethod
    def _git(where, *arguments):
        try:
            result = subprocess.run(
                ["git", "-C", str(where), *arguments], capture_output=True, check=False
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", "replace").strip()

    def problem(self):
        """Why the history cannot be read, or None.

        A shallow clone makes its oldest commit look like the one that added every file.
        """
        for where in (self.authored_root, self.releases_root):
            if self._git(where, "rev-parse", "--is-shallow-repository") != "false":
                return f"{where} is not a git checkout with its full history"
        top = self._git(self.authored_root, "rev-parse", "--show-toplevel")
        if top is None or Path(top).resolve() != self.authored_root.resolve():
            return f"{self.authored_root} is not the top of its own checkout"
        return None

    def listing_change(self, path):
        """The last commit that changed the listing document, as (abbreviated sha, committed), or None."""
        path = Path(path)
        answer = self._git(
            path.parent, "log", "-1", "--first-parent", "--format=%h %cI", "--", path.name
        )
        if not answer:
            return None
        sha, _, committed = answer.partition(" ")
        return sha, committed

    def listing_at(self, path, moment):
        """The listing document as it stood at `moment`, as text, or None when it did not exist yet."""
        path = Path(path)
        sha = self._git(
            path.parent, "rev-list", "-1", "--first-parent", f"--before={moment}", "HEAD",
            "--", path.name,
        )
        if not sha:
            return None
        return self.listing_in(path, sha)

    def listing_in(self, path, sha):
        """The listing document as commit `sha` of the authored checkout has it, as text, or None."""
        path = Path(path)
        return self._git(path.parent, "show", f"{sha}:./{path.name}")

    def committed(self, path, sha):
        """When commit `sha` of the authored checkout was committed, or None when it is unknown there."""
        return self._git(Path(path).parent, "show", "-s", "--format=%cI", f"{sha}^{{commit}}") or None

    def last_stamp(self, path):
        """When a release file was added, or None.

        None also when the file is not committed yet, because its stamp time is then unknown.
        """
        path = Path(path)
        where, spec = path.parent, path.name
        pending = self._git(where, "status", "--porcelain", "--untracked-files=all", "--", spec)
        if pending is None or any(line.startswith(("??", "A")) for line in pending.splitlines()):
            return None
        return self._git(
            where, "log", "-1", "--first-parent", "--diff-filter=A", "--format=%cI", "--", spec
        ) or None

    def stamped_from(self, path, repository):
        """The listing commit the `Listing:` line of the commit that added a release file names, or None.

        The watcher names the listing commit it stamped from, and a release merged by pull request names none.
        """
        path = Path(path)
        message = self._git(
            path.parent, "log", "-1", "--first-parent", "--diff-filter=A", "--format=%B",
            "--", path.name,
        )
        sha, _ = self._listing_line(message, repository)
        return sha

    def received(self, path, repository):
        """The listing commit the newest `Listing:` line on a release file names, and the fields that commit left waiting, or None."""
        path = Path(path)
        message = self._git(
            path.parent, "log", "-1", "--first-parent", "--fixed-strings",
            f"--grep=Listing: {repository}@", "--format=%B", "--", path.name,
        )
        sha, waiting = self._listing_line(message, repository)
        return None if sha is None else (sha, waiting)

    @staticmethod
    def _listing_line(message, repository):
        marker = f"Listing: {repository}@"
        match = re.search(rf"^{re.escape(marker)}([0-9a-f]+)$", message or "", re.MULTILINE)
        if match is None:
            return None, ()
        return match.group(1), tuple(re.findall(r"^Waiting: (\S+)", message, re.MULTILINE))

    def resolved(self, path):
        """Whether the watcher already resolved a `game_max` month on a release file, or None when git cannot say."""
        path = Path(path)
        answer = self._git(
            path.parent, "log", "-1", "--first-parent", "--fixed-strings",
            "--grep=Resolve the game_max month for", "--format=%h", "--", path.name,
        )
        return None if answer is None else bool(answer)

    def introduced(self):
        """When the generated repository first carried this module on its checked-out branch, or None.

        An edit merged before that time changes nothing.
        """
        answer = self._git(
            self.releases_root, "log", "--first-parent", "--diff-filter=A", "--format=%cI",
            "--", ":/tools/listing_edit.py",
        )
        return answer.splitlines()[-1] if answer else None
