#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Where the authored repository keeps its documents, and which types each folder holds.

A listing of a mod or a mod-loader is `listings/<id>.toml`, a vehicle is `vehicles/<id>.toml` and a save is `saves/<id>.toml` (RFC 0085).
A pack is the folder `packs/<id>/` (RFC 0080).
All of them share one namespace of ids, compared without case (RFC 0031).
"""

from pathlib import Path

# The folders of authored listing documents and the types each holds, in the
# order they are read. `listings/` stays the sign of a content-index checkout.
FOLDERS = {
    "listings": ("mod", "mod-loader"),
    "vehicles": ("vehicle",),
    "saves": ("save",),
}

PACKS = "packs"


def listing_files(root):
    """Every listing document, folder by folder in FOLDERS order. A missing folder is empty."""
    files = []
    for name in FOLDERS:
        folder = Path(root) / name
        if folder.is_dir():
            files.extend(sorted(folder.glob("*.toml")))
    return files


def find(root, listing_id):
    """The document of the listing `listing_id`, or None."""
    for name in FOLDERS:
        path = Path(root) / name / f"{listing_id}.toml"
        if path.is_file():
            return path
    return None


def holder(content_type):
    """The folder that holds listings of `content_type`, or None."""
    return next((name for name, types in FOLDERS.items() if content_type in types), None)


def type_problem(folder, content_type):
    """Why a document of `content_type` cannot be in `folder`, or None.

    Under `listings/` only a type another folder holds is refused here, because
    the schema of the authored repository already refuses an unknown type.
    """
    types = FOLDERS.get(folder, ())
    if content_type in types:
        return None
    belongs = holder(content_type)
    if folder == "listings" and belongs is None:
        return None
    held = " and ".join(types)
    if content_type is None:
        return f"the document names no type, and {folder}/ holds {held}"
    message = f"type '{content_type}' does not belong under {folder}/, which holds {held}"
    return f"{message}; it belongs under {belongs}/" if belongs else message


def id_holders(root):
    """The documents that hold each id, lowercased, as paths relative to `root`.

    A pack holds the name of its folder.
    """
    holders = {}
    for path in listing_files(root):
        holders.setdefault(path.stem.lower(), []).append(f"{path.parent.name}/{path.name}")
    packs = Path(root) / PACKS
    for path in sorted(packs.iterdir()) if packs.is_dir() else []:
        if path.is_dir():
            holders.setdefault(path.name.lower(), []).append(f"{PACKS}/{path.name}/")
    return holders
