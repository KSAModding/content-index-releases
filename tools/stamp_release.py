#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Authored document plus release archive in, release file out, per RFC 0031.

The one place a release file is derived. The watcher stamps with it, and the
release pull request checks re-derive with it and compare against what was
submitted, so the two paths cannot disagree.

It needs no token and touches no network: everything it says comes from the
authored document, the archive's own bytes, the release facts its caller read
off the host, and the game release list. That is what makes it testable against
examples/ in the design repository, where every value was produced by this
procedure by hand.

A version that does not parse, or an install root that is neither derivable nor
authored, raises StampError for the caller to report.

RFC 0031 defines the format, RFC 0035 the install descriptor, RFC 0017 the
version ordering the month bounds resolve against.
"""

import argparse
import hashlib
import io
import json
import re
import sys
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SPEC_VERSION = 1

# SemVer 2.0.0, from semver.org, with the leading `v` a tag is allowed to carry.
SEMVER = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

# A KSA production version as the game displays it (RFC 0017). Only the fourth
# component orders, and it is readable straight off the string, so a full bound
# needs no list lookup at all.
GAME_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\.(\d+)(?:-[^+]+)?(?:\+.*)?$")

# A month bound, "the whole of that month" (RFC 0017).
GAME_MONTH = re.compile(r"^(\d{4})\.(\d{1,2})$")

# Pre-release identifiers that mean "not a release at all", so a nightly does
# not look like one. Anything else pre-release is a testing build.
DEV_IDENTIFIERS = frozenset({"dev", "nightly", "snapshot", "canary", "ci", "git", "pre"})

MOD_TOML = "mod.toml"

# The listing facts a release file freezes, in the order RFC 0031 lists them.
# `status` and `superseded_by` are deliberately not here: deprecation has to
# reach every release the moment it is declared, so a client reads it live.
LISTING_FIELDS = ("name", "authors", "abstract", "description", "license", "tags")

DEPENDENCY_KINDS = ("required", "optional", "recommends", "suggests", "conflict")

INSTALL_ANCHORS = ("mods", "user-data", "game-root", "standalone")


class StampError(Exception):
    """The release cannot be stamped, and the caller reports it.

    Not a transient failure: a host that is down or an archive that did not
    download is the caller's business, and never reaches this module.
    """


def normalize_version(tag):
    """The tag as SemVer 2.0.0, with a leading `v` stripped.

    Raises StampError when it does not parse, which is what rejects the release
    at publish time with the error in front of the author.
    """
    match = SEMVER.match((tag or "").strip())
    if match is None:
        raise StampError(f"version '{tag}' does not parse as SemVer 2.0.0")

    version = "{major}.{minor}.{patch}".format(**match.groupdict())
    if match.group("prerelease"):
        version += "-" + match.group("prerelease")
    if match.group("build"):
        version += "+" + match.group("build")
    return version


def prerelease_identifiers(version):
    """The dot-separated pre-release identifiers of a version, lowercased."""
    match = SEMVER.match(version)
    part = match.group("prerelease") if match else None
    return [identifier.lower() for identifier in part.split(".")] if part else []


def release_status(version, host_prerelease):
    """`stable`, `testing`, or `dev`, from the host's flag and the version.

    A pre-release identifier that names a development stream wins over the
    host's flag, because a nightly marked as a normal release is still a
    nightly.
    """
    identifiers = prerelease_identifiers(version)
    if DEV_IDENTIFIERS.intersection(identifiers):
        return "dev"
    if identifiers or host_prerelease:
        return "testing"
    return "stable"


def game_revision(version):
    """The revision of a full game version string, the only component that orders."""
    match = GAME_VERSION.match(version.strip())
    return int(match.group(4)) if match else None


def month_of(version):
    """The (year, month) a full game version string displays, or None."""
    match = GAME_VERSION.match(version.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def month_is_over(year, month, now):
    """Whether that calendar month has completed, as of `now` (UTC)."""
    return (now.year, now.month) > (year, month)


def resolve_bound(bound, which, game_versions, now):
    """Resolve an authored game bound to (display, revision).

    A full version string carries its own revision (RFC 0017), so it never
    needs the list. A month resolves to the first revision within it as a lower
    bound and to the last as an upper bound, against the game release list.

    An upper bound naming a month that is not over yet cannot resolve to its
    last revision: it returns (None, None), which stamps the release with no
    upper bound at all, the only honest reading of "open". The watcher
    re-resolves it once the month completes, which is a stamp correction and
    not an amendment.
    """
    bound = (bound or "").strip()
    if not bound:
        return None, None

    revision = game_revision(bound)
    if revision is not None:
        return bound, revision

    match = GAME_MONTH.match(bound)
    if match is None:
        raise StampError(f"{which} '{bound}' is neither a game version nor a month")

    year, month = int(match.group(1)), int(match.group(2))
    revisions = sorted(
        game_revision(version)
        for version in game_versions
        if month_of(version) == (year, month)
    )

    if which == "game_max" and not month_is_over(year, month, now):
        # Stamped open, and re-resolved once the month completes.
        return None, None

    if not revisions:
        raise StampError(
            f"{which} '{bound}' names a month with no build in the game release list"
        )

    revision = revisions[0] if which == "game_min" else revisions[-1]
    display = next(
        version
        for version in game_versions
        if month_of(version) == (year, month) and game_revision(version) == revision
    )
    return display, revision


def open_archive(archive):
    """A ZipFile over the archive bytes. Anything unreadable is a StampError."""
    try:
        return zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as error:
        raise StampError(f"the archive is not a readable zip, {error}") from error


def top_level_directories(handle):
    """The names of the directories at the root of the archive, in archive order."""
    seen = []
    for entry in handle.namelist():
        name = entry.replace("\\", "/")
        head, _, rest = name.partition("/")
        if not head or (not rest and not name.endswith("/")):
            continue  # A file sitting at the archive root.
        if head not in seen:
            seen.append(head)
    return seen


def entries_under(handle, root):
    """The file entries whose path lies under `root`, which may be the archive root."""
    prefix = f"{root}/" if root else ""
    return [
        info
        for info in handle.infolist()
        if not info.is_dir() and info.filename.replace("\\", "/").startswith(prefix)
    ]


def read_mod_toml(handle, root):
    """The archive's own mod.toml, parsed, or None when it carries none.

    An archive without one, which is what a mod-loader archive looks like, is
    not an error and contributes no code dependencies.
    """
    name = f"{root}/{MOD_TOML}" if root else MOD_TOML
    try:
        raw = handle.read(name)
    except KeyError:
        return None
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise StampError(f"the archive's {name} is not valid TOML, {error}") from error


def derive_root(handle, listing_id, content_type):
    """The install root derived from the archive's layout.

    For a mod, RFC 0031's rule: one top-level directory containing mod.toml,
    its name matching the id, because that name is the identity the game will
    see. For every other type the derived root is the archive root itself
    (RFC 0035, rule 9), which this returns as "".

    Returns None when nothing is derivable, which needs an authored `root`.
    """
    if content_type != "mod":
        return ""

    directories = top_level_directories(handle)
    with_manifest = [
        name for name in directories if f"{name}/{MOD_TOML}" in handle.namelist()
    ]

    candidates = with_manifest or directories
    if len(candidates) != 1:
        return None

    name = candidates[0]
    if name == listing_id:
        return name
    if name.lower() == listing_id.lower():
        raise StampError(
            f"the archive's top-level directory is '{name}' and the id is "
            f"'{listing_id}': the folder name is the identity the game sees, "
            "so the casing has to match"
        )
    raise StampError(
        f"the archive's top-level directory is '{name}', which does not match "
        f"the id '{listing_id}'"
    )


def install_object(handle, listing_id, content_type, authored_install):
    """The release file's `install` object, or None when it says nothing.

    `root` is omitted when it is the archive root, the way RFC 0035 omits
    `path` for the anchor itself. `target` and `path` appear as resolved at
    stamp time and are absent where the type default applies, so a mod stamps
    neither. `[provides]` is never stamped: it says what a loader offers right
    now, and a stale copy would point a manager at the wrong directory.
    """
    authored_install = authored_install or {}
    authored_root = authored_install.get("root")

    if authored_root is not None:
        root, derived = str(authored_root).strip("/"), False
        if root and not entries_under(handle, root):
            raise StampError(f"the authored install root '{root}' is not in the archive")
    else:
        root, derived = derive_root(handle, listing_id, content_type), True
        if root is None:
            raise StampError(
                "the install root is neither derivable from the archive nor authored: "
                "the standard layout is one top-level directory containing mod.toml, "
                f"named '{listing_id}'"
            )

    install = {}
    if root:
        install["root"] = root
    install["derived"] = derived

    target = authored_install.get("target")
    if target is not None:
        if target not in INSTALL_ANCHORS:
            raise StampError(f"the authored install target '{target}' is not an anchor")
        install["target"] = target
    path = authored_install.get("path")
    if path is not None:
        install["path"] = str(path)

    if set(install) == {"derived"}:
        # Nothing but the fact that nobody authored anything, which the absence
        # of the object already says.
        return None
    return install


def install_size(handle, root):
    """The unpacked size of what gets installed, in bytes."""
    return sum(info.file_size for info in entries_under(handle, root))


def derived_dependencies(mod_toml):
    """The code dependencies the archive's own mod.toml declares.

    `[[StarMap.ModDependencies]]` is the only dependency data that exists in
    the ecosystem, it is name-only, and `Optional` defaults to false, which is
    the loader refusing to start the mod without it.
    """
    blocks = ((mod_toml or {}).get("StarMap") or {}).get("ModDependencies") or []
    dependencies = []
    for block in blocks:
        identifier = (block.get("ModId") or "").strip()
        if not identifier:
            raise StampError("a [[StarMap.ModDependencies]] block carries no ModId")
        optional = bool(block.get("Optional", False))
        dependencies.append(
            {
                "id": identifier,
                "kind": "optional" if optional else "required",
                "source": "derived",
            }
        )
    return dependencies


def _authored_entry(entry):
    """One authored dependency entry, validated, in release file shape."""
    kind = entry.get("kind")
    if kind not in DEPENDENCY_KINDS:
        raise StampError(f"dependency kind '{kind}' is not one of {', '.join(DEPENDENCY_KINDS)}")

    stamped = {}
    if "any_of" in entry:
        if entry.get("id"):
            raise StampError("a dependency entry carries both id and any_of")
        if kind not in ("required", "recommends"):
            raise StampError(f"any_of is not valid with kind '{kind}'")
        members = entry.get("any_of") or []
        if not members:
            raise StampError("an any_of dependency entry names no members")
        stamped["any_of"] = [
            {
                key: member[key]
                for key in ("id", "min", "max")
                if member.get(key) is not None
            }
            for member in members
        ]
        for member in stamped["any_of"]:
            if not member.get("id"):
                raise StampError("an any_of member carries no id")
    else:
        identifier = (entry.get("id") or "").strip()
        if not identifier:
            raise StampError("a dependency entry carries no id")
        stamped["id"] = identifier

    stamped["kind"] = kind
    for key in ("min", "max"):
        if entry.get(key) is not None:
            stamped[key] = normalize_version(entry[key])
    stamped["source"] = "authored"
    return stamped


def merge_dependencies(derived, authored):
    """The merged dependency list of RFC 0031.

    Derived entries are ground truth for code dependencies, because the loader
    acts on that data at runtime, so nothing can suppress one. An authored
    entry replaces the derived entry of the same id, which is how bounds get
    added, and an authored `any_of` replaces the derived entry of every member
    it names.

    A member of an `any_of` whose derived entry was not `Optional = true` is a
    validation error: the loader refuses to start the mod without that specific
    dependency, so an alternative set would claim a choice runtime does not
    offer.
    """
    merged = [dict(entry) for entry in derived]
    by_id = {entry["id"].lower(): index for index, entry in enumerate(merged)}
    replaced = set()

    for entry in authored or []:
        stamped = _authored_entry(entry)
        names = (
            [member["id"] for member in stamped["any_of"]]
            if "any_of" in stamped
            else [stamped["id"]]
        )

        for name in names:
            index = by_id.get(name.lower())
            if index is None:
                continue
            if "any_of" in stamped and merged[index]["kind"] != "optional":
                raise StampError(
                    f"the any_of entry names '{name}', whose derived entry is required: "
                    "the loader refuses to start the mod without that dependency, so an "
                    "alternative set claims a choice that does not exist at runtime"
                )
            replaced.add(index)

        merged.append(stamped)

    return [entry for index, entry in enumerate(merged) if index not in replaced]


def listing_snapshot(authored):
    """The shared authored core as it stands now, frozen into this release."""
    snapshot = {
        field: authored[field]
        for field in LISTING_FIELDS
        if authored.get(field) is not None
    }
    links = authored.get("links")
    if links:
        snapshot["links"] = dict(links)
    return snapshot


def stamp(authored, release, archive, game_versions, mirrors=(), now=None):
    """The release file for one release.

    `authored` is the parsed authored document, `archive` the release archive's
    bytes, `game_versions` the `versions` list of the game release list, and
    `release` the facts the caller read off the release host:

        tag             the tag or version string the host names, required
        release_date    ISO 8601 UTC timestamp of the release, required
        url             direct download URL of the archive, required
        content_type    the archive format, defaults to application/zip
        prerelease      the host's pre-release flag, defaults to false
        changelog       URL of the release's changelog, optional

    `now` is when the stamp happens, which only the open month bound depends
    on. The release checks pass the submission's own time so a re-derivation
    reaches the same answer as the stamp it is checking.
    """
    now = now or datetime.now(timezone.utc)

    listing_id = (authored.get("id") or "").strip()
    content_type = authored.get("type")
    if not listing_id:
        raise StampError("the authored document carries no id")
    if content_type not in ("mod", "mod-loader"):
        raise StampError(f"type '{content_type}' has no generated release file")

    version = normalize_version(release.get("tag"))
    handle = open_archive(archive)

    install = install_object(
        handle, listing_id, content_type, authored.get("install")
    )
    root = (install or {}).get("root", "")

    mod_toml = read_mod_toml(handle, root) if content_type == "mod" else None
    dependencies = merge_dependencies(
        derived_dependencies(mod_toml), authored.get("dependencies")
    )

    compatibility = authored.get("compatibility") or {}
    if not compatibility.get("game_min"):
        raise StampError("the authored document states no game_min")
    game_min, game_min_revision = resolve_bound(
        compatibility.get("game_min"), "game_min", game_versions, now
    )
    game_max, game_max_revision = resolve_bound(
        compatibility.get("game_max"), "game_max", game_versions, now
    )

    if not release.get("release_date"):
        raise StampError("the host reports no release date")
    if not release.get("url"):
        raise StampError("the host reports no download URL for the archive")

    document = {
        "spec_version": SPEC_VERSION,
        "id": listing_id,
        "type": content_type,
        "version": version,
        "version_scheme": "semver",
        "release_status": release_status(version, release.get("prerelease", False)),
        "release_date": release["release_date"],
        "game_min": game_min,
        "game_min_revision": game_min_revision,
    }
    if game_max is not None:
        document["game_max"] = game_max
        document["game_max_revision"] = game_max_revision
    if compatibility.get("os"):
        document["os"] = list(compatibility["os"])

    # The archive parsed as a zip above, so that is what it is, whatever the
    # host declared an asset uploaded as octet-stream to be.
    content = release.get("content_type")
    if content in (None, "", "application/octet-stream", "binary/octet-stream"):
        content = "application/zip"

    download = {
        "url": release["url"],
        "sha256": hashlib.sha256(archive).hexdigest().upper(),
        "size": len(archive),
        "content_type": content,
    }
    if mirrors:
        download["mirrors"] = list(mirrors)
    document["download"] = download

    document["install_size"] = install_size(handle, root)
    if install is not None:
        document["install"] = install

    loader = authored.get("loader")
    if loader and content_type == "mod":
        stamped_loader = {"id": loader.get("id")}
        if not stamped_loader["id"]:
            raise StampError("the authored [loader] section names no id")
        if not loader.get("min"):
            raise StampError("the authored [loader] section states no min")
        stamped_loader["min"] = normalize_version(loader["min"])
        if loader.get("max"):
            stamped_loader["max"] = normalize_version(loader["max"])
        stamped_loader["source"] = "authored"
        document["loader"] = stamped_loader

    document["dependencies"] = dependencies
    if release.get("changelog"):
        document["changelog"] = release["changelog"]
    document["listing"] = listing_snapshot(authored)

    return document


def serialize(document):
    """The release file's bytes, as every stamped file in the repository is written."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stamp one release file from an authored document and a release archive."
    )
    parser.add_argument("--listing", required=True, type=Path, help="the authored TOML document")
    parser.add_argument("--archive", required=True, type=Path, help="the release archive")
    parser.add_argument(
        "--release",
        required=True,
        type=Path,
        help="JSON of the release facts read off the host: tag, release_date, url, "
        "content_type, prerelease, changelog",
    )
    parser.add_argument(
        "--game-versions", type=Path, default=Path("game-versions.json"),
        help="the game release list month bounds resolve against",
    )
    parser.add_argument(
        "--mirror", action="append", default=[],
        help="a further URL serving byte-identical archive, verified by the caller",
    )
    parser.add_argument("--out", type=Path, help="write here instead of to stdout")
    arguments = parser.parse_args(argv)

    with arguments.listing.open("rb") as handle:
        authored = tomllib.load(handle)
    release = json.loads(arguments.release.read_text(encoding="utf-8"))
    game_versions = json.loads(
        arguments.game_versions.read_text(encoding="utf-8")
    )["versions"]

    try:
        document = stamp(
            authored,
            release,
            arguments.archive.read_bytes(),
            game_versions,
            mirrors=arguments.mirror,
        )
    except StampError as error:
        print(f"cannot stamp {arguments.listing}: {error}", file=sys.stderr)
        return 1

    rendered = serialize(document)
    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        with arguments.out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
