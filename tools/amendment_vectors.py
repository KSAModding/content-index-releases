#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the amendment cases of `tools/amendment-vectors.json`.

The file is for every program that writes amendments, so a client such as Borea tests against the same cases as `tools/amend.py` and `tools/check_amendment.py`.
README.md describes its shape.
"""

import contextlib
import io
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import amend
import check_amendment
from amend import AmendError

VECTORS = Path(__file__).resolve().parent / "amendment-vectors.json"

ACTORS = ("owner", "steward")
VERDICTS = ("accepted", "unchanged", "rejected")

# The options of tools/amend.py that describe the change.
# The runner names the listing, the one release and the paths itself.
OPTIONS = frozenset(
    {
        "game-min",
        "game-max",
        "yank",
        "reason",
        "loader-min",
        "loader-max",
        "dependency-min",
        "dependency-max",
        "add-dependency",
    }
)


def load(path=VECTORS):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def shape_errors(vector):
    """What makes one vector unreadable to a client, before anything runs."""
    errors = []
    if not isinstance(vector.get("name"), str) or not vector["name"]:
        errors.append("name is missing")
    if vector.get("actor") not in ACTORS:
        errors.append(f"actor is not one of {', '.join(ACTORS)}")
    if vector.get("verdict") not in VERDICTS:
        errors.append(f"verdict is not one of {', '.join(VERDICTS)}")
    try:
        base = json.loads(vector.get("base"))
    except (TypeError, json.JSONDecodeError):
        base = None
    if not isinstance(base, dict) or not all(
        isinstance(base.get(key), str) for key in ("id", "version")
    ):
        errors.append("base is not the text of a release file with an id and a version")

    amendment = vector.get("amendment")
    if not isinstance(amendment, dict):
        errors.append("amendment is not an object")
    else:
        for option in sorted(set(amendment) - OPTIONS):
            errors.append(
                f"amendment carries '{option}', which is not an option that describes a change"
            )
        for option, value in amendment.items():
            if not (value is True or isinstance(value, str) or _strings(value)):
                errors.append(
                    f"amendment '{option}' is neither true, a string nor a list of strings"
                )

    if (vector.get("verdict") == "rejected") != isinstance(vector.get("reason"), str):
        errors.append("a rejected vector carries a reason, and only a rejected one")
    if (vector.get("verdict") == "accepted") != isinstance(vector.get("written"), str):
        errors.append("an accepted vector carries the written text, and only an accepted one")
    return errors


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def arguments(amendment):
    """The amendment as the command line of tools/amend.py."""
    argv = []
    for option, value in amendment.items():
        flag = f"--{option}"
        if value is True:
            argv.append(flag)
        elif isinstance(value, list):
            for item in value:
                argv.extend((flag, item))
        else:
            argv.extend((flag, value))
    return argv


def actor_arguments(vector):
    """A steward acting alone only narrows, and the owner, or anyone on the owner's request, may also widen."""
    return ["--owner"] if vector["actor"] == "owner" else []


def release_path(base):
    return f"releases/{base['id']}/{base['version']}.json"


def through_amend(vector, game_versions):
    """What tools/amend.py does with the vector, as `(verdict, output, written)`.

    `written` is the file's bytes after the run, and `output` is everything the tool printed.
    """
    base = json.loads(vector["base"])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / release_path(base)
        path.parent.mkdir(parents=True)
        path.write_bytes(vector["base"].encode("utf-8"))
        versions = root / "game-versions.json"
        versions.write_text(json.dumps({"versions": game_versions}), encoding="utf-8")

        argv = [
            "--listing", base["id"],
            "--version", base["version"],
            "--releases", str(root / "releases"),
            "--game-versions", str(versions),
            *actor_arguments(vector),
            *arguments(vector["amendment"]),
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = amend.main(argv)
            except SystemExit as error:
                code = error.code
        written = path.read_bytes()

    if code == 1:
        verdict = "rejected"
    elif code == 0:
        verdict = "unchanged" if written == vector["base"].encode("utf-8") else "accepted"
    else:
        verdict = f"exit code {code}"
    return verdict, output.getvalue(), written


def amend_mismatches(vector, game_versions):
    """How tools/amend.py disagrees with the vector. Empty when it agrees."""
    verdict, output, written = through_amend(vector, game_versions)
    found = []
    if verdict != vector["verdict"]:
        found.append(
            f"tools/amend.py gives {verdict}, and the vector says {vector['verdict']}: "
            + output.strip()
        )
    if "reason" in vector and vector["reason"] not in output:
        found.append(f"tools/amend.py does not say '{vector['reason']}': {output.strip()}")
    if "written" in vector and written != vector["written"].encode("utf-8"):
        found.append(
            "tools/amend.py writes other bytes than the vector. It writes "
            + json.dumps(written.decode("utf-8"))
        )
    return found


def applied(vector, game_versions):
    """The release file tools/amend.py derives before its own check, or AmendError when it refuses first."""
    base = json.loads(vector["base"])
    parsed = amend.parse_arguments(
        ["--listing", base["id"], "--version", base["version"], *arguments(vector["amendment"])]
    )
    amendment = amend.build_amendment(parsed, game_versions, datetime.now(timezone.utc))
    amend.apply(base, amendment)
    return base


def invariant_mismatches(vector, game_versions):
    """How tools/check_amendment.py disagrees with the vector. Empty when it agrees.

    An accepted vector is measured by its written text, so the pinned bytes themselves are in the amendment class.
    """
    base = json.loads(vector["base"])
    if vector["verdict"] == "accepted":
        head = json.loads(vector["written"])
    else:
        try:
            head = applied(vector, game_versions)
        except AmendError as error:
            # The tool refuses before there is a file for the invariant to measure.
            if vector["verdict"] != "rejected":
                return [f"tools/amend.py refuses before the invariant runs: {error}"]
            if vector["reason"] not in str(error):
                return [f"tools/amend.py refuses without saying '{vector['reason']}': {error}"]
            return []

    path = release_path(base)
    outcome = check_amendment.check([(path, json.loads(vector["base"]), head)])[path]
    given = outcome.outcome
    if outcome.owner_only and vector["actor"] != "owner":
        given = "reject"
    expected = "reject" if vector["verdict"] == "rejected" else "pass"
    found = []
    if given != expected:
        found.append(
            f"tools/check_amendment.py gives {given}, and the vector says "
            f"{vector['verdict']}: {outcome.messages}"
        )
    if "reason" in vector and not any(vector["reason"] in line for line in outcome.messages):
        found.append(
            f"tools/check_amendment.py does not say '{vector['reason']}': {outcome.messages}"
        )
    return found
