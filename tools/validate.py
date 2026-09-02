#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate a pull request and leave a verdict for the ownership workflow.

The published version comes from git.
A `pull_request` checkout has a merge commit at its head, so `HEAD^1` is the file an amendment is measured against, and a release file it does not have is a submitted release, measured against its own archive.
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_amendment
import check_release
import check_scope
import hosts

ROOT = Path(__file__).resolve().parent.parent

PASS = "pass"
REJECT = "reject"
COULD_NOT_EVALUATE = "could-not-evaluate"

SEVERITY = (REJECT, COULD_NOT_EVALUATE, PASS)

VERDICT_SCHEMA_VERSION = 1

GITHUB_API = "https://api.github.com"
USER_AGENT = "KSAModding-content-index-releases-validation"

DEFAULT_BASE_REF = "HEAD^1"


class Check:

    def __init__(self, name, outcome, messages=()):
        self.name = name
        self.outcome = outcome
        self.messages = list(messages)

    def as_dict(self):
        return {"name": self.name, "outcome": self.outcome, "messages": self.messages}


class Unavailable(Exception):
    """The base version could not be read, so no check reaches a verdict."""


def worst(outcomes):
    for outcome in SEVERITY:
        if outcome in outcomes:
            return outcome
    return PASS


def _git(arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


def resolve_base(ref):
    """The commit the amendment is measured against, or Unavailable.

    Resolved once, so a later `git show` failure is about the file, not the ref.
    """
    result = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if result.returncode != 0 or not result.stdout.strip():
        raise Unavailable(
            f"the base ref '{ref}' does not resolve, so no published version can be read. "
            "A pull_request checkout needs fetch-depth 2 for its merge commit's parents"
        )
    return result.stdout.decode("utf-8", "replace").strip()


# What git prints when the path is simply not in that commit, as opposed to a
# real failure to read it.
ABSENT = ("does not exist in", "exists on disk, but not in")


def base_document(commit, path):
    """The file as the base branch has it, as (document, problem).

    A missing file is `(None, None)`, a new release rather than an amendment.
    Anything else is a problem: reporting a corrupt object as "this file is new" would turn repository trouble into a confident wrong answer.
    """
    result = _git(["show", f"{commit}:{path}"])
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        if any(phrase in message for phrase in ABSENT):
            return None, None
        return None, f"{path}: the published version could not be read: {message}"
    try:
        return json.loads(result.stdout.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"{path}: the published version does not parse: {error}"


def head_document(path):
    """The file as the pull request proposes it, or None when it is deleted."""
    where = ROOT / path
    # Resolve both paths first, the way the watcher guards its own writes, so a
    # `..` cannot climb out of the releases folder.
    try:
        contained = where.resolve().is_relative_to((ROOT / "releases").resolve())
    except OSError:
        contained = False
    if not contained:
        raise ValueError(f"{path} does not sit under releases/")
    if not where.is_file():
        return None
    try:
        return json.loads(where.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{path} is not readable JSON: {error}") from error


def run_amendment(paths, base_ref=DEFAULT_BASE_REF):
    if not paths:
        return Check(
            "amendment", PASS, ["the change touches no release file, so nothing is amended"]
        )

    try:
        commit = resolve_base(base_ref)
    except Unavailable as error:
        return Check("amendment", COULD_NOT_EVALUATE, [str(error)])

    changes = []
    unreadable_base = []
    unreadable_head = []
    for path in paths:
        base, problem = base_document(commit, path)
        if problem:
            unreadable_base.append(problem)
            continue
        try:
            head = head_document(path)
        except ValueError as error:
            unreadable_head.append(str(error))
            continue
        changes.append((path, base, head))

    results = check_amendment.check(changes)

    messages = unreadable_base + unreadable_head
    outcomes = []
    if unreadable_base:
        outcomes.append(COULD_NOT_EVALUATE)
    if unreadable_head:
        outcomes.append(REJECT)
    for path, outcome in sorted(results.items()):
        outcomes.append(outcome.outcome)
        messages.extend(f"{path}: {message}" for message in outcome.messages)
    if not messages:
        messages.append(f"{len(paths)} release file(s) narrow what they claim, and nothing else")
    return Check("amendment", worst(outcomes), messages)


def load_game_versions():
    """The game release list a month bound resolves against, or Unavailable."""
    path = ROOT / "game-versions.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["versions"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise Unavailable(f"the game release list at {path} is not readable: {error}") from error


def run_release(paths, base_ref=DEFAULT_BASE_REF, authored=None, http=None, now=None):
    """The submitted release files, each measured against its own archive.

    A version is stamped exactly once, so a path the base branch already has is rejected before anything is downloaded.
    """
    try:
        commit = resolve_base(base_ref)
        game_versions = load_game_versions()
    except Unavailable as error:
        return Check("release", COULD_NOT_EVALUATE, [str(error)])

    root = check_release.authored_root(authored)
    if not (root / "listings").is_dir():
        return Check(
            "release",
            COULD_NOT_EVALUATE,
            [
                f"content-index is not checked out at {root}: point --authored or "
                "CONTENT_INDEX at a checkout of KSAModding/content-index, which holds "
                "the listing a release belongs to"
            ],
        )
    # No token on this path: the URL is the author's, and a release archive
    # needs no credential to download.
    http = http or hosts.Http()

    outcomes, messages = [], []
    for path in paths:
        base, problem = base_document(commit, path)
        if problem:
            outcomes.append(COULD_NOT_EVALUATE)
            messages.append(problem)
            continue
        if base is not None:
            outcomes.append(REJECT)
            messages.append(
                f"{path} is already published, and a version is stamped exactly once: "
                "a broken release is yanked, and a corrected one gets a new version"
            )
            continue
        try:
            head = head_document(path)
        except ValueError as error:
            outcomes.append(REJECT)
            messages.append(str(error))
            continue
        if head is None:
            outcomes.append(REJECT)
            messages.append(f"{path} is not in the pull request's tree")
            continue
        outcome = check_release.check(path, head, root, game_versions, http, now=now)
        outcomes.append(outcome.outcome)
        messages.extend(f"{path}: {message}" for message in outcome.messages)
    return Check("release", worst(outcomes), messages)


def local_changes(paths, base_ref):
    """The changes of a local run, with a release file the base lacks read as added."""
    try:
        commit = resolve_base(base_ref)
    except Unavailable:
        return check_scope.changes(paths)
    changes = []
    for path in paths:
        status = check_scope.MODIFIED
        if check_scope.is_release(path) and base_document(commit, path) == (None, None):
            status = check_scope.ADDED
        changes.append(check_scope.Change(path, status))
    return changes


def changed_paths(repository, number, token):
    """What a pull request does to each path it touches."""
    changes = []
    url = f"{GITHUB_API}/repos/{repository}/pulls/{number}/files?per_page=100"
    while url:
        payload, link = _api(url, token)
        if not isinstance(payload, list):
            raise ValueError(f"{url}: the answer is not a list of files")
        for entry in payload:
            if not isinstance(entry, dict) or "filename" not in entry:
                raise ValueError(f"{url}: an entry carries no filename")
            changes.append(
                check_scope.Change(entry["filename"], entry.get("status") or "modified")
            )
        url = _next_page(link)
    return changes


def head_sha(repository, number, token):
    """The head commit of a pull request, which is what a commit status names."""
    payload, _ = _api(f"{GITHUB_API}/repos/{repository}/pulls/{number}", token)
    if not isinstance(payload, dict):
        raise ValueError("the pull request answer is not an object")
    return (payload.get("head") or {}).get("sha")


def _api(url, token):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.loads(answer.read()), answer.headers.get("Link", "")


def _next_page(link):
    for part in (link or "").split(","):
        section = part.split(";")
        if len(section) > 1 and 'rel="next"' in section[1]:
            return section[0].strip().strip("<>")
    return None


def summarise(verdict):
    """The run as a few lines of Markdown, for the job summary."""
    lines = [
        f"## Validation: {verdict['verdict']}",
        "",
        f"Auto-merge candidate: {'yes' if verdict['auto_merge_candidate'] else 'no'}",
    ]
    if verdict.get("scope_reason"):
        lines.append(f"Reason: {verdict['scope_reason']}")
    lines.append("")
    for check in verdict["checks"]:
        lines.append(f"### {check['name']}: {check['outcome']}")
        lines.extend(f"- {message}" for message in check["messages"] or ["nothing to report"])
        lines.append("")
    return "\n".join(lines)


def _verdict(checks, candidate=False, documents=(), reason="", number=None, sha=None):
    return {
        "schema_version": VERDICT_SCHEMA_VERSION,
        "verdict": worst([check.outcome for check in checks]),
        "auto_merge_candidate": candidate,
        "pull_request": number,
        "head_sha": sha,
        "documents": list(documents),
        "scope_reason": reason,
        "checks": [check.as_dict() for check in checks],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pull-request", type=int, help="the pull request to validate")
    parser.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/repo, defaults to GITHUB_REPOSITORY",
    )
    parser.add_argument(
        "--changed", nargs="*", default=None,
        help="the changed paths, instead of asking the API. For a local run",
    )
    parser.add_argument(
        "--base-ref", default=DEFAULT_BASE_REF,
        help="what the change is measured against, defaults to the merge commit's first parent",
    )
    parser.add_argument(
        "--authored",
        help="a checkout of KSAModding/content-index, else CONTENT_INDEX, else the sibling directory",
    )
    parser.add_argument("--output", type=Path, help="where to write the verdict as JSON")
    arguments = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    sha = None
    changes = None
    if arguments.changed is not None:
        changes = local_changes(arguments.changed, arguments.base_ref)

    if changes is None:
        if not (arguments.pull_request and arguments.repository):
            parser.error("either --changed, or --pull-request together with --repository")
        try:
            changes = changed_paths(arguments.repository, arguments.pull_request, token)
            sha = head_sha(arguments.repository, arguments.pull_request, token)
        except (OSError, ValueError) as error:
            # With no list of changed files there is nothing to scope, so give up here.
            verdict = _verdict(
                [Check("changed files", COULD_NOT_EVALUATE, [str(error)])],
                reason=f"the changed files could not be read: {error}",
                number=arguments.pull_request,
            )
            _emit(verdict, arguments.output)
            return 0

    candidate, paths, reason = check_scope.evaluate(changes)
    new = check_scope.added(changes)
    amended = [path for path in paths if path not in new]

    try:
        checks = []
        if amended or not new:
            checks.append(run_amendment(amended, arguments.base_ref))
        if len(new) == 1:
            checks.append(run_release(new, arguments.base_ref, arguments.authored))
        elif new:
            # Every added file names a URL of the author's choosing, so nothing
            # is fetched for a shape that can never merge.
            checks.append(
                Check(
                    "release",
                    REJECT,
                    [
                        f"{len(new)} release files are added and none was measured: a "
                        "release pull request adds exactly one, so open one pull request "
                        "per release"
                    ],
                )
            )
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        checks = [
            Check(
                "checks",
                COULD_NOT_EVALUATE,
                [f"the check itself raised {error!r}, which is a defect in tools/, "
                 "not something this pull request did"],
            )
        ]
        candidate = False

    verdict = _verdict(checks, candidate, paths, reason, arguments.pull_request, sha)

    _emit(verdict, arguments.output)
    return 1 if verdict["verdict"] == REJECT else 0


def _emit(verdict, output):
    print(summarise(verdict))
    if output:
        output.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(summarise(verdict) + "\n")


if __name__ == "__main__":
    sys.exit(main())
