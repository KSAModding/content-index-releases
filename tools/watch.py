#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One tick of the watcher (RFC 0033).

Scan every authored listing's authority host, stamp every release that appeared
after the newest one already stamped, commit it, keep the release notes of every
stamped release the host lists equal to the notes on the host, fetch the
listing's images again, keep one error issue per listing current on the authored
repository, which mentions the owner of the listing, and sweep that repository's
open pull requests.

Older releases are left alone, and a listing's first tick takes its newest
release only, because RFC 0031 freezes the authored facts "current at release
time". A listing that names `since` under `[releases]` opts in (RFC 0079), and
every release at or above that version is stamped too, with today's facts, which
the owner corrects with an amendment. `--backfill` stamps the whole history.

A stamped release that the host no longer lists for a day, and whose every URL
then answers that the archive is gone, gets `download.unavailable_since`, and
the field goes again once the host serves the stamped bytes (RFC 0078).

What is stamped in the repository is the whole state, so a tick GitHub delays,
drops or cancels costs latency and not data, and a re-run stamps nothing twice.

Per-release derivation is tools/stamp_release.py, the hosts are tools/hosts.py.
The image fetch rules are tools/images.py of the authored checkout, the code the
checks of RFC 0058 run, so the two cannot disagree. The owner an issue mentions
is who tools/ownership.py of the authored checkout names, through tools/decide.py.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import decide
import hosts
from check_amendment import precedence
from hosts import HostError
from stamp_release import (
    GAME_MONTH,
    VERSION_FORMS,
    StampError,
    as_archive,
    changelog_text,
    month_is_over,
    normalize_version,
    resolve_bound,
    serialize,
    stamp,
    valid_id,
)

GITHUB_API = "https://api.github.com"

# The cache is derived, so a version bump just costs one expensive tick.
CACHE_VERSION = 3

# The marker that makes a listing's issue findable without a search, and the
# signature that decides whether a genuinely new error deserves a comment.
LISTING_MARKER = "<!-- watcher:listing={id} -->"
SIGNATURE_MARKER = "<!-- watcher:signature={signature} -->"
WAITING_MARKER = "<!-- watcher:waiting={sha} -->"
NO_RUN_MARKER = "<!-- watcher:no-run={sha} -->"

# The same marker read back, to find which listing an open issue belongs to.
MARKED_LISTING = re.compile(r"<!-- watcher:listing=(\S+) -->")

# Ends the line of an issue body that names the owner, so a tick that changes
# nothing keeps that line without asking the host again. At the start of the
# line, it would make GitHub render the whole line as HTML, mention included.
OWNER_MARKER = "<!-- watcher:owner -->"
OWNER_LINE = re.compile(rf"^(.+) {re.escape(OWNER_MARKER)}\r?$", re.MULTILINE)

BACKTICKED = re.compile(r"`([^`]+)`")

# A check run conclusion that is neither a pass nor a reject: the check could
# not run to a verdict, which never auto-merges and never auto-rejects, and is
# what the sweep re-runs.
COULD_NOT_EVALUATE = frozenset(
    {"cancelled", "timed_out", "stale", "neutral", "skipped", "action_required"}
)
PENDING = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

# Grace for a finished run to get its verdict posted.
SETTLING_MINUTES = 5

# How long a release that `since` reaches and the checks rejected is reported
# again without a download, while nothing it was stamped from changed.
REJECTION_HOURS = 24

# How long every observation has to find a stamped release gone from its host
# before the watcher asks its URLs, and how long it waits to ask again (RFC 0078).
GONE_HOURS = 24

IMAGES_VERIFIED = (
    "The images of this listing verify again and the watcher found no other error, "
    "so this is done."
)


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        moment = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def load_images(authored):
    """`images.py` from the authored checkout: one implementation of the fetch rules, not two."""
    path = Path(authored) / "tools" / "images.py"
    if not path.is_file():
        raise ImportError(
            f"images.py is not at {path.parent}: point --authored at a checkout of "
            "KSAModding/content-index, which holds the image fetch rules"
        )
    spec = importlib.util.spec_from_file_location("images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def oldest_first(releases):
    return sorted(releases, key=lambda release: (release.release_date or "", release.tag))


def is_history(date, frontier, newest):
    """Whether a release is older than this listing's stamping starts.
    """
    if date is None:
        return False
    if frontier is None:
        return newest is not None and date < newest
    return date < frontier


def filled(version):
    """`version` with a missing minor or patch component filled with 0 (RFC 0072)."""
    core, suffix = re.match(r"([^+-]*)(.*)", version.strip(), re.DOTALL).groups()
    parts = core.split(".")
    return ".".join(parts + ["0"] * (3 - len(parts))) + suffix


def reaches(version, floor):
    """Whether `version` is at or above `floor`, the precedence of `since`."""
    return floor is not None and version is not None and precedence(version) >= floor


def stamped_urls(download):
    """`download.url` and every mirror, the URLs a client may fetch a release from."""
    return [
        url for url in [download.get("url"), *(download.get("mirrors") or [])]
        if isinstance(url, str) and url
    ]


def served(answer):
    """Whether a status says the URL serves something."""
    return answer is not None and 200 <= answer < 300


class Cache:
    """Derived cache, never state.

    It holds the per-listing ETags, the consecutive-failure counts, the issue
    numbers, the last image check, what the mirror and sweep passes already
    tried, and how long a stamped release has been gone from its host. Every entry is rebuildable from the repository and the hosts, so
    losing the whole file costs one expensive tick and nothing else.
    """

    def __init__(self, path, log=None):
        self._log = log or (lambda message: None)
        self.path = Path(path) if path else None
        self.data = {"version": CACHE_VERSION}
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                # Starting cold is fine, but a restore that fails every tick
                # must not look exactly like one, so say it happened.
                self._log(f"the derived cache could not be read and starts cold: {error}")
                loaded = {}
            if isinstance(loaded, dict) and loaded.get("version") == CACHE_VERSION:
                self.data = loaded
        for section in (
            "differs", "gone", "hosts", "images", "listings", "mirrors", "rejected", "swaps",
            "sweep",
        ):
            self.data.setdefault(section, {})

    def section(self, name, key):
        return self.data[name].setdefault(key, {})

    def save(self):
        """Best-effort, like every other cache operation: a full disk after the
        stamping is done must not fail the tick and keep the push from running."""
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.data, handle, indent=1, sort_keys=True)
                handle.write("\n")
        except OSError as error:
            self._log(f"the derived cache could not be written: {error}")


class Api:
    """The GitHub API calls that write, so a dry run can be one flag."""

    def __init__(self, http, repository, dry_run=False, log=print):
        self.http = http
        self.repository = repository
        self.dry_run = dry_run
        self.log = log

    def get(self, path, **query):
        url = f"{GITHUB_API}/repos/{self.repository}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        answer = self.http.get(url, accept="application/vnd.github+json", api=True)
        return json.loads(answer.body) if answer.body else None

    def get_paged(self, path, key=None, max_pages=10, **query):
        """Every item across pages, because a one-page read that looks complete
        is how a duplicate issue gets opened past 100 open ones."""
        items = []
        for page in range(1, max_pages + 1):
            answer = self.get(path, per_page=100, page=page, **query)
            batch = (answer or {}).get(key) if key else (answer or [])
            batch = batch or []
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def send(self, method, path, payload):
        url = f"{GITHUB_API}/repos/{self.repository}{path}"
        if self.dry_run or not self.http.token:
            self.log(f"    would {method} {path} {json.dumps(payload)[:200]}")
            return None
        body = json.dumps(payload).encode("utf-8")
        last = None
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                data=body,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.http.token}",
                    "Content-Type": "application/json",
                    "User-Agent": hosts.USER_AGENT,
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.http.timeout) as answer:
                    raw = answer.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                # A 4xx is an answer. A 5xx, or a secondary rate limit on a
                # write (a 403 with Retry-After), is the API having a bad
                # moment, and one of those must not mark a listing failed for
                # a reason that has nothing to do with the listing.
                transient = error.code in (403, 429) and hosts._rate_limited(error.headers)
                if error.code < 500 and not transient:
                    raise
                last = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = error
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise HostError(f"{method} {url}: {last}")


class Issues:
    """One open issue per listing on the authored repository, kept current.

    A new tick never opens a second issue for a listing that already has one:
    the body is rewritten to the current failure, and a comment is added only
    when the failure itself changed, so a host that stays down is one issue and
    no notifications.

    `owner` gives the line that mentions the owner of a listing, or says why
    nobody is told. It is asked only for what notifies: a new issue, a comment
    on a changed failure, and a note.
    """

    def __init__(self, api, label, log=print, owner=None):
        self.api = api
        self.label = label
        self.log = log
        self.owner = owner or (lambda listing_id: "")
        self._open = {}
        self._degraded = False

    @staticmethod
    def signature_of(errors):
        """The stable fingerprint of a failure, for edit-versus-comment decisions."""
        return hashlib.sha256("\n".join(sorted(errors)).encode()).hexdigest()[:16]

    def _all(self, labelled, state="open"):
        """The issues of the authored repository in `state`, listed once per tick.

        The label narrows the list to the watcher's own issues, and the
        unlabelled list is the fallback for the tick that opened an issue before
        the label existed. A listing that fails marks the lookup degraded, so
        `report` skips creating rather than duplicating an issue it could not
        see.
        """
        if (labelled, state) not in self._open:
            query = {"labels": self.label} if labelled else {}
            try:
                issues = self.api.get_paged("/issues", state=state, **query)
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"  could not list issues: {error}")
                self._degraded = True
                issues = []
            self._open[(labelled, state)] = [
                issue for issue in issues if "pull_request" not in issue
            ]
        return self._open[(labelled, state)]

    def _remembered(self, listing_id, cache, key, marker):
        """The issue the cache remembers under `key`, when it still carries the listing's marker."""
        number = cache.section("listings", listing_id).get(key)
        if not number:
            return None
        try:
            issue = self.api.get(f"/issues/{number}")
        except (urllib.error.HTTPError, HostError):
            return None
        return issue if issue and marker in (issue.get("body") or "") else None

    def find(self, listing_id, cache):
        marker = LISTING_MARKER.format(id=listing_id)
        issue = self._remembered(listing_id, cache, "issue", marker)
        if issue and issue.get("state") == "open":
            return issue
        for labelled in (True, False):
            for issue in self._all(labelled):
                if marker in (issue.get("body") or ""):
                    cache.section("listings", listing_id)["issue"] = issue["number"]
                    return issue
        return None

    def last(self, listing_id, cache):
        """The newest watcher issue of the listing, closed ones included, or None.

        `find` sees only open issues, and a note goes to a closed one as well.
        The issues the cache remembers count together with the listed ones,
        because a human can close a newer issue than the one the watcher
        closed last.
        """
        marker = LISTING_MARKER.format(id=listing_id)
        found = [
            issue
            for issue in (
                self._remembered(listing_id, cache, "last_issue", marker),
                self._remembered(listing_id, cache, "issue", marker),
            )
            if issue
        ]
        for labelled in (True, False):
            listed = [
                issue for issue in self._all(labelled, "all")
                if marker in (issue.get("body") or "")
            ]
            if listed:
                found.extend(listed)
                break
        return max(found, key=lambda issue: issue["number"], default=None)

    def _open_issue(self, title, body):
        """Open an issue with the watcher's label, or without it when the label is refused."""
        try:
            return self.api.send(
                "POST", "/issues", {"title": title, "body": body, "labels": [self.label]}
            )
        except urllib.error.HTTPError as error:
            if error.code != 422:
                self.log(f"  could not open an issue (HTTP {error.code})")
                return None
        # A label the repository does not define is not worth losing the
        # report over; the marker in the body is what the watcher finds
        # the issue by anyway.
        self.log(f"  the '{self.label}' label was refused (HTTP 422)")
        try:
            return self.api.send("POST", "/issues", {"title": title, "body": body})
        except urllib.error.HTTPError as error:
            self.log(f"  could not open an issue (HTTP {error.code})")
            return None

    def note(self, listing_id, text, cache):
        """Comment `text` on the listing's issue, and return whether it was sent.

        A note is a fact and not an error, so it never opens an issue for the
        listing and never keeps one open. With no open issue, it goes to the
        listing's last watcher issue, which stays closed, and a listing that
        never had one gets a new issue that is closed at once.
        """
        try:
            issue = self.find(listing_id, cache) or self.last(listing_id, cache)
            if issue is None and self._degraded:
                self.log("  the note waits: the issue list could not be read this tick")
                return False
            owner = self.owner(listing_id)
            if owner:
                text = f"{text}\n\n{owner}"
            if issue is None:
                body = f"{LISTING_MARKER.format(id=listing_id)}\n{text}"
                created = self._open_issue(f"{listing_id}: releases gone from their host", body)
                if not created:
                    return False
                number = created["number"]
                cache.section("listings", listing_id)["last_issue"] = number
                try:
                    self.api.send("PATCH", f"/issues/{number}", {"state": "closed"})
                except (urllib.error.HTTPError, HostError) as error:
                    # The note is sent, so it is not sent again. The next clean
                    # tick closes the issue.
                    self.log(f"  noted on {self.api.repository}#{number}, not closed: {error}")
                    return True
                self.log(f"  noted on {self.api.repository}#{number}, closed")
                return True
            number = issue["number"]
            self.api.send("POST", f"/issues/{number}/comments", {"body": text})
            cache.section("listings", listing_id)["last_issue"] = number
            self.log(f"  noted on {self.api.repository}#{number}")
            return True
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"  could not note on the issue for {listing_id}: {error}")
            return False

    def report(self, listing_id, errors, cache):
        """Keep the listing's issue current with `errors`.

        Never raises for an API-shaped failure: reporting is best-effort, and
        several callers sit inside except clauses, where a raise would leave
        the per-listing guard and take the rest of the tick with it.
        """
        try:
            self._report(listing_id, list(dict.fromkeys(errors)), cache)
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"  could not keep the issue for {listing_id} current: {error}")

    def _report(self, listing_id, errors, cache):
        signature = self.signature_of(errors)
        title = f"{listing_id}: the watcher found a problem"
        issue = self.find(listing_id, cache)

        if issue is None:
            if self._degraded:
                self.log(
                    "  not opening an issue: the issue list could not be read "
                    "this tick, and a blind create duplicates"
                )
                return
            body = self._body(listing_id, errors, signature, self.owner(listing_id))
            created = self._open_issue(title, body)
            if created:
                cache.section("listings", listing_id)["issue"] = created["number"]
                self.log(f"  opened {self.api.repository}#{created['number']}")
            return

        number = issue["number"]
        known = SIGNATURE_MARKER.format(signature=signature) in (issue.get("body") or "")
        if known:
            kept = OWNER_LINE.search(issue.get("body") or "")
            owner = kept.group(1) if kept else ""
        else:
            owner = self.owner(listing_id)
        body = self._body(listing_id, errors, signature, owner)
        self.api.send("PATCH", f"/issues/{number}", {"title": title, "body": body})
        if not known:
            comment = "The watcher is now failing on something else:\n\n" + "\n".join(
                f"- {error}" for error in errors
            )
            if owner:
                comment += f"\n\n{owner}"
            self.api.send("POST", f"/issues/{number}/comments", {"body": comment})
        self.log(f"  kept {self.api.repository}#{number} current")

    def open_listings(self):
        """The listing id of every open issue the watcher owns, to its issue."""
        found = {}
        for labelled in (True, False):
            for issue in self._all(labelled):
                match = MARKED_LISTING.search(issue.get("body") or "")
                if match:
                    found.setdefault(match.group(1), issue)
        return found

    @property
    def degraded(self):
        """Whether a read failed this tick, so the issue list is incomplete."""
        return self._degraded

    def resolve(self, listing_id, cache, reason=None):
        """Close the listing's issue, because the tick evaluated it cleanly.

        Best-effort like `report`: a failure here leaves an issue open one tick
        longer, which is not worth the rest of the tick.
        """
        try:
            self._resolve(listing_id, cache, reason)
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"  could not close the issue for {listing_id}: {error}")

    def _resolve(self, listing_id, cache, reason=None):
        issue = self.find(listing_id, cache)
        if issue is None:
            return
        number = issue["number"]
        self.api.send(
            "POST",
            f"/issues/{number}/comments",
            {"body": reason
             or "The watcher stamped this listing without an error, so this is done."},
        )
        self.api.send("PATCH", f"/issues/{number}", {"state": "closed"})
        state = cache.section("listings", listing_id)
        state.pop("issue", None)
        state["last_issue"] = number
        self.log(f"  closed {self.api.repository}#{number}")

    def attempted(self, listing_id, cache):
        """The versions the listing's open issue already names.
        """
        body = (self.find(listing_id, cache) or {}).get("body") or ""
        found = set()
        for token in BACKTICKED.findall(body):
            try:
                found.add(normalize_version(token))
            except StampError:
                continue  # A digest, a bound, a tag that never parsed.
        return found

    def resolve_if(self, listing_id, signature, cache, reason=None):
        """Close the listing's issue only when it reports exactly `signature`.

        The recovery from an unreachable host must not close an issue that
        meanwhile reports something else, and the signature marker is what
        tells the two apart.
        """
        issue = self.find(listing_id, cache)
        if issue is None:
            return
        if SIGNATURE_MARKER.format(signature=signature) in (issue.get("body") or ""):
            self.resolve(listing_id, cache, reason)

    def _body(self, listing_id, errors, signature, owner=""):
        return "\n".join(
            [
                LISTING_MARKER.format(id=listing_id),
                SIGNATURE_MARKER.format(signature=signature),
                f"The watcher found a problem with `{listing_id}`.",
                "",
                *[f"- {error}" for error in errors],
                "",
                *([f"{owner} {OWNER_MARKER}", ""] if owner else []),
                "The watcher retries every tick and keeps this issue current rather than",
                "opening a new one. It closes by itself once a tick evaluates the listing",
                "without an error.",
                "",
                f"Last checked {iso(now())}.",
            ]
        )


class Sweep:
    """The event-driven half, swept once per tick.

    GitHub drops triggers and retries nothing, so a pull request with no verdict
    has its validation re-run. A re-run replays the event; a dispatch would carry
    the branch it ran on and name no pull request.
    """

    def __init__(self, api, cache, options, log=print):
        self.api = api
        self.cache = cache
        self.options = options
        self.log = log

    def run(self):
        try:
            pulls = self.api.get_paged("/pulls", state="open")
        except urllib.error.HTTPError as error:
            self.log(f"  could not list pull requests: HTTP {error.code}")
            return
        except HostError as error:
            self.log(f"  could not list pull requests: {error}")
            return

        for pull in pulls[: self.options.sweep_limit]:
            try:
                self._one(pull)
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"  #{pull['number']}: {error}")

        # Every open pull request, or the ones past the limit lose their count.
        open_shas = {pull["head"]["sha"] for pull in pulls}
        section = self.cache.data["sweep"]
        for sha in [key for key in section if key not in open_shas]:
            del section[sha]

    def _one(self, pull):
        number, sha = pull["number"], pull["head"]["sha"]
        state = self.cache.section("sweep", sha)

        runs = (self.api.get("/actions/runs", head_sha=sha, per_page=50) or {}).get(
            "workflow_runs", []
        )
        waiting = [
            run
            for run in runs
            if run.get("status") in ("waiting", "action_required")
            or run.get("conclusion") == "action_required"
        ]
        if waiting:
            self._ping(number, sha, state)
            return

        if any(run.get("status") in PENDING for run in runs):
            return  # Something is still running; a verdict is on its way.

        verdict = self._verdict(sha)
        if verdict in ("pass", "reject"):
            return  # A verdict either way. It waits for the author, not for us.

        validation = self._validation_run(runs)
        if validation is None:
            self._ask_for_a_commit(number, sha, state)
            return

        # The ownership workflow runs elsewhere, so nothing here shows it working.
        finished = parse_iso(validation.get("updated_at"))
        if finished and now() - finished < timedelta(minutes=SETTLING_MINUTES):
            return

        self._rerun(number, sha, state, validation, f"verdict {verdict}")

    def _validation_run(self, runs):
        """The validation run a pull request started, the only one worth replaying."""
        for run in runs:
            if run.get("event") != "pull_request":
                continue
            if (run.get("path") or "").endswith("/" + self.options.sweep_workflow):
                return run
        return None

    def _verdict(self, sha):
        """`pass`, `reject`, `could-not-evaluate`, or `missing` for the required check."""
        wanted = self.options.verdict_check
        status = self.api.get(f"/commits/{sha}/status") or {}
        for entry in status.get("statuses") or []:
            if entry.get("context") == wanted:
                return {
                    "success": "pass",
                    "failure": "reject",
                    "error": "could-not-evaluate",
                    "pending": "missing",
                }.get(entry.get("state"), "could-not-evaluate")

        checks = self.api.get(f"/commits/{sha}/check-runs") or {}
        for entry in checks.get("check_runs") or []:
            if entry.get("name") != wanted:
                continue
            conclusion = entry.get("conclusion")
            if conclusion == "success":
                return "pass"
            if conclusion == "failure":
                return "reject"
            if conclusion in COULD_NOT_EVALUATE:
                return "could-not-evaluate"
            return "missing"
        return "missing"

    def _within_the_clock(self, state):
        """Whether a temporary wait is over. Attempts are terminal, so separate."""
        last = parse_iso(state.get("last"))
        if last and now() - last < timedelta(minutes=self.options.sweep_cooldown):
            return False
        refused = parse_iso(state.get("refused"))
        return not (
            refused and now() - refused < timedelta(hours=self.options.sweep_refusal_hours)
        )

    def _rerun(self, number, sha, state, run, reason):
        if state.get("attempts", 0) >= self.options.sweep_attempts:
            self._ask_for_a_commit(number, sha, state)
            return
        if not self._within_the_clock(state):
            return

        self.log(f"  #{number}: re-running validation ({reason})")
        try:
            self.api.send("POST", f"/actions/runs/{run['id']}/rerun", {})
        except urllib.error.HTTPError as error:
            self.log(f"  #{number}: run {run['id']} would not re-run (HTTP {error.code})")
            state["refused"] = iso(now())
            self._ask_for_a_commit(number, sha, state)
            return
        state.pop("refused", None)
        state["last"] = iso(now())
        state["attempts"] = state.get("attempts", 0) + 1

    def _ask_for_a_commit(self, number, sha, state):
        """Every dead end ends here, because only the author can start a run."""
        if state.get("asked"):
            return
        if not state.get("seen"):
            state["seen"] = True
            return

        marker = NO_RUN_MARKER.format(sha=sha)
        comments = self.api.get_paged(f"/issues/{number}/comments")
        if any(marker in (comment.get("body") or "") for comment in comments):
            state["asked"] = True
            return

        self.log(f"  #{number}: {sha[:7]} reached no verdict, asking for a commit")
        try:
            self.api.send(
                "POST",
                f"/issues/{number}/comments",
                {
                    "body": f"{marker}\nNo validation verdict reached this commit, and "
                    "the watcher could not start one. Any new commit on this pull "
                    "request runs the checks again."
                },
            )
        except urllib.error.HTTPError as error:
            self.log(f"  #{number}: the comment was refused (HTTP {error.code})")
        state["asked"] = True

    def _ping(self, number, sha, state):
        if state.get("pinged"):
            return
        marker = WAITING_MARKER.format(sha=sha)
        comments = self.api.get_paged(f"/issues/{number}/comments")
        if any(marker in (comment.get("body") or "") for comment in comments):
            state["pinged"] = True
            return
        self.log(f"  #{number}: waiting for approval, pinging a steward")
        self.api.send(
            "POST",
            f"/issues/{number}/comments",
            {
                "body": f"{marker}\n{self.options.steward_team} this run is sitting in "
                "GitHub's waiting-for-approval state, which only a steward can release. "
                "It needs a steward to approve the workflow run."
            },
        )
        state["pinged"] = True


class Watcher:
    def __init__(self, options):
        self.options = options
        self.releases_root = Path(options.releases)
        self.authored_root = Path(options.authored)
        self.cache = Cache(options.cache, log=self.log)
        self.http = hosts.Http(token=options.token, log=self.log)
        self.api = Api(self.http, options.authored_repo, options.dry_run, self.log)
        self.issues = Issues(self.api, options.issue_label, self.log, owner=self.owner_line)
        self.game_versions = json.loads(
            Path(options.game_versions).read_text(encoding="utf-8")
        )["versions"]
        self.stamp_budget = options.stamp_budget
        self.since_budget = options.since_budget
        self.mirror_budget = options.mirror_budget
        self.image_budget = options.image_budget
        self.gone_budget = options.gone_budget
        self.images = None
        self.proofs = None
        # The authored document of every listing this tick read, and the owner
        # line of every listing it told something. Neither outlives the tick.
        self.documents = {}
        self.owners = {}
        self.stamped = []
        self.mirrored = []
        self.noted = []
        self.marked = []
        self.unmarked = []
        self.notes = {}
        self.failed = []
        self.lines = []
        self._mirror_lists = {}

    def log(self, message):
        print(message, flush=True)
        self.lines.append(str(message))

    def folder(self, listing_id):
        return self.releases_root / listing_id

    def stamped_versions(self, listing_id):
        """The versions of a listing that have a file, which is the whole state."""
        folder = self.folder(listing_id)
        if not folder.is_dir():
            return {}
        return {path.stem: path for path in sorted(folder.glob("*.json"))}

    def poll_etag(self, host_state, authored_digest):
        """The ETag says the host's answer is unchanged, not that a release the
        tick rejected would be rejected again, so an edited authored document
        drops it. A backfill drops it too, because history counts as settled."""
        if self.options.backfill or host_state.get("authored") != authored_digest:
            return None
        return host_state.get("etag")

    @staticmethod
    def authored_digest(authored):
        return hashlib.sha256(
            json.dumps(authored, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def stamped_frontier(self, stamped, errors):
        newest = None
        complete = True
        for path in stamped.values():
            document = self.read_release(path, errors)
            if document is None:
                complete = False
                continue
            date = parse_iso(document.get("release_date"))
            if date is None:
                errors.append(
                    f"the stamped file releases/{path.parent.name}/{path.name} "
                    "carries no usable release_date"
                )
                complete = False
                continue
            if newest is None or date > newest:
                newest = date
        return newest, complete

    def read_release(self, path, errors):
        """A stamped release file, or None with the corruption reported.

        The repository is the state, so a stamped file that does not parse is
        exactly the corruption that has to reach a human, not the catch-all.
        """
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(
                f"the stamped file releases/{path.parent.name}/{path.name} "
                f"is not readable JSON: {error}"
            )
            return None

    def write(self, path, text, message):
        """Write one release file and commit it. Refuses anything else.

        The branch protection bypass is scoped to an identity, not to a path, so
        this is where the limit is enforced. Both sides are resolved: a `..`
        keeps `releases` in `Path.parents` while the file lands outside it.
        """
        path = Path(path)
        try:
            contained = path.resolve().is_relative_to(self.releases_root.resolve())
        except OSError:
            contained = False
        if not contained or path.resolve() == self.releases_root.resolve():
            raise RuntimeError(f"the watcher does not write {path}")
        if self.options.dry_run:
            self.log(f"    would write {path} and commit '{message}'")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self.commit(path, message)

    def commit(self, path, message):
        """One commit per release file, scoped to that path and nothing else."""
        if self.options.no_commit:
            return
        subprocess.run(["git", "add", "--", str(path)], check=True)
        unchanged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", str(path)], check=False
        ).returncode
        if unchanged == 0:
            return  # The file already says this, so there is nothing to record.
        subprocess.run(
            ["git", "commit", "--quiet", "-m", message, "--", str(path)], check=True
        )

    def listings(self):
        """The authored listing documents this tick looks at."""
        folder = self.authored_root / "listings"
        if not folder.is_dir():
            self.log(f"{folder} does not exist, so there is nothing to watch")
            return []

        wanted = {name.lower() for name in self.options.listing or []}
        delisted = self.delisted()
        if delisted is None:
            # Failing open would stamp releases a steward delisted, so an
            # unreadable status file skips the whole tick's listings instead.
            self.log("index-status.toml is unreadable, so no listing is scanned this tick")
            return []
        chosen = []
        for path in sorted(folder.glob("*.toml")):
            if wanted and path.stem.lower() not in wanted:
                continue
            if path.stem.lower() in delisted:
                self.log(f"{path.stem}: delisted, so the watcher leaves it alone")
                continue
            chosen.append(path)
        return chosen

    def delisted(self):
        """The ids the index has delisted, or None when the file is unreadable.

        A delisted listing is out of the snapshot, so stamping further releases
        for it would be the watcher arguing with a steward.
        """
        path = self.authored_root / "index-status.toml"
        if not path.is_file():
            return set()
        try:
            with path.open("rb") as handle:
                document = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            self.log(f"could not read {path.name}: {error}")
            return None
        return {
            (entry.get("id") or "").lower()
            for entry in document.get("entries") or []
            if entry.get("state") == "delisted"
        }

    def listing_problem(self, path, listing_id):
        """Why this listing cannot be processed at all, or None.

        The id becomes a path segment under releases/, so the id rules are the
        gate in front of every path this tick builds from it, and the file stem
        is what the delisting and `--listing` filters match, so it has to name
        the same listing.
        """
        if not valid_id(listing_id):
            return (
                f"the id '{listing_id}' does not satisfy the id rules of "
                "RFC 0031, so the watcher does not use it"
            )
        if path.stem.lower() != listing_id.lower():
            return (
                f"the file is named '{path.stem}.toml' but the document says "
                f"id = '{listing_id}'; the file name and the id must match"
            )
        return None

    def tick(self):
        try:
            paths = self.listings()
            for path in paths:
                listing_id = path.stem
                try:
                    with path.open("rb") as handle:
                        authored = tomllib.load(handle)
                    listing_id = (authored.get("id") or path.stem).strip()
                    self.documents[listing_id] = authored
                    problem = self.listing_problem(path, listing_id)
                    if problem:
                        self.log(f"{listing_id}: {problem}")
                        self.failed.append(listing_id)
                        self.issues.report(listing_id, [problem], self.cache)
                        continue
                    self.log(f"{listing_id}:")
                    try:
                        self.one_listing(listing_id, authored)
                    finally:
                        # A mark written before an error still reaches the author.
                        self.tell(listing_id)
                except tomllib.TOMLDecodeError as error:
                    # `report` never raises for API failures, which matters
                    # here: an exception inside an except clause would leave
                    # the loop past the sibling guard below.
                    message = f"{path.name} is not valid TOML: {error}"
                    self.log(f"{listing_id}: {message}")
                    self.failed.append(listing_id)
                    self.issues.report(listing_id, [message], self.cache)
                except Exception as error:  # noqa: BLE001 - one listing never fails the tick
                    self.log(f"  unexpected: {error!r}")
                    self.log(traceback.format_exc())
                    self.failed.append(listing_id)
                    self.issues.report(
                        listing_id,
                        [f"the watcher hit an internal error on this listing: {error!r}"],
                        self.cache,
                    )

            self.close_orphans(paths)

            if not self.options.no_sweep:
                self.log(f"sweeping {self.options.authored_repo}:")
                Sweep(self.api, self.cache, self.options, self.log).run()
        finally:
            # A dry run must leave no trace: an ETag it stored would make the
            # next real tick take the 304 path over releases it never stamped.
            if not self.options.dry_run:
                self.cache.save()
            self.summarize()
        return 0

    def close_orphans(self, paths):
        """Close the issue of a listing this tick no longer scans.

        A delisted or deleted listing never reaches `one_listing`, so nothing
        else closes its issue. Closing is the destructive direction, so three
        cases are skipped: a narrow dispatch, an empty listing set, which is
        what an unreadable status file looks like, and a failed issue read.
        """
        if self.options.listing or not paths or self.issues.degraded:
            return
        watched = {path.stem.lower() for path in paths}
        for listing_id, issue in self.issues.open_listings().items():
            if listing_id.lower() in watched:
                continue
            self.log(f"{listing_id}: no longer scanned, closing #{issue['number']}")
            self.issues.resolve(
                listing_id,
                self.cache,
                reason="The watcher no longer scans this listing, so it has nothing "
                "left to report here. A delisting, a deleted document, or a renamed "
                "id gets here.",
            )

    def one_listing(self, listing_id, authored):
        state = self.cache.section("listings", listing_id)
        errors = []

        self.month_pass(listing_id, authored, errors)
        images = self.image_pass(listing_id, authored)
        errors.extend(images or [])

        try:
            authority, mirrors = hosts.build(
                authored.get("releases"), self.http, listing_id
            )
        except StampError as error:
            self.failed.append(listing_id)
            self.report(listing_id, errors + [str(error)], state, images)
            return
        if authority is None:
            self.log("  no [releases] section, so releases enter by pull request")
            self.gone_by_request(listing_id, errors)
            if errors:
                self.failed.append(listing_id)
                self.report(listing_id, errors, state, images)
            elif images is not None:
                # The listing left the watcher's half. Anything it reports here
                # is about a host it no longer names.
                self.issues.resolve(
                    listing_id,
                    self.cache,
                    reason=IMAGES_VERIFIED if state.pop("images_signature", None)
                    else "This listing has no [releases] section any more, so the "
                    "watcher has nothing left to report here.",
                )
            return

        # Keyed per listing, not per host: two listings naming the same
        # repository must not blind each other through a shared ETag.
        host_state = self.cache.section("hosts", f"{listing_id}/{authority.key}")
        digest = self.authored_digest(authored)
        try:
            releases, etag = authority.releases(self.poll_etag(host_state, digest))
        except HostError as error:
            self.unreachable(listing_id, state, str(error))
            return
        except StampError as error:
            state["unreachable"] = 0
            state.pop("unreachable_signature", None)
            self.failed.append(listing_id)
            self.report(listing_id, errors + [str(error)], state, images)
            return

        self.recover(listing_id, state)
        settled = True

        if releases is None:
            self.log("  unchanged since the last tick")
        else:
            self.log(f"  {len(releases)} release(s) on {authority.key}")
            if getattr(authority, "truncated", False):
                errors.append(
                    "the host lists more releases than one scan covers, so the "
                    "oldest are not watched; raising the watcher's max_pages "
                    "needs a human"
                )
            settled = self.stamp_pass(
                listing_id, authored, authority, mirrors, releases, errors,
                self.issues.attempted(listing_id, self.cache),
            )
            self.changelog_pass(listing_id, releases, errors)

        self.mirror_pass(listing_id, mirrors, errors)
        settled = self.gone_pass(listing_id, authority, releases, errors) and settled

        if settled:
            # The ETag stands for "every release behind this answer is
            # settled", so a tick that ran out of budget or could not reach the
            # host always refetches. A payload that changes changes the ETag, so
            # a fixed tag is picked up at once.
            if etag:
                host_state["etag"] = etag
            host_state["authored"] = digest
            host_state["checked"] = iso(now())
        else:
            host_state.pop("etag", None)

        if errors:
            self.failed.append(listing_id)
            for error in errors:
                self.log(f"    reporting: {error}")
            self.report(listing_id, errors, state, images)
        elif releases is not None and settled and images is not None:
            reason = IMAGES_VERIFIED if state.pop("images_signature", None) else None
            self.issues.resolve(listing_id, self.cache, reason)
        elif images == [] and "images_signature" in state:
            # An unchanged host answer says nothing about any other error, so
            # only an issue that reports exactly the image failure closes.
            self.issues.resolve_if(
                listing_id, state.pop("images_signature"), self.cache, IMAGES_VERIFIED
            )

    def report(self, listing_id, errors, state, images):
        """Report `errors`, and keep their signature when they are the image failures alone."""
        errors = list(dict.fromkeys(errors))
        if images and errors == images:
            state["images_signature"] = Issues.signature_of(errors)
        else:
            state.pop("images_signature", None)
        self.issues.report(listing_id, errors, self.cache)

    def image_rules(self):
        """The images module of the authored checkout, or None when it does not load."""
        if self.images is None:
            try:
                self.images = load_images(self.authored_root)
            except Exception as error:  # noqa: BLE001 - no image check is not a failed tick
                self.log(f"the images are not checked this tick: {error!r}")
                self.images = False
        return self.images or None

    def owner_proofs(self):
        """`ownership.py` of the authored checkout, or None when it does not load."""
        if self.proofs is None:
            try:
                self.proofs = decide.load_ownership(self.authored_root)
            except Exception as error:  # noqa: BLE001 - no owner is not a failed tick
                self.log(f"the owners are not looked up this tick: {error!r}")
                self.proofs = False
        return self.proofs or None

    def owner_line(self, listing_id):
        """The line that mentions the owner of the listing, or says why nobody is told.

        The owner is who the proofs on the listing's host name today. The memo
        in `self.owners` does not outlive the tick and nothing goes into the
        cache, but an issue body keeps the line it was written with, and a tick
        with the same failure takes it back from there.
        """
        if listing_id not in self.owners:
            document = self.documents.get(listing_id)
            proofs = self.owner_proofs() if document is not None else None
            if document is None:
                logins, reason = (), "the watcher could not read the listing"
            elif proofs is None:
                logins, reason = (), "the ownership proofs of content-index did not load"
            else:
                api = decide.Api(
                    None, None, unavailable=proofs.Unavailable, public_token=self.http.token
                )
                logins, reason = decide.owner_logins(proofs, document, api)
            if logins:
                self.owners[listing_id] = (
                    f"{decide.mentions(logins)}, the watcher tells you because you own this "
                    "listing."
                )
            else:
                self.owners[listing_id] = (
                    "The watcher tells nobody, because no owner of this listing could be "
                    f"named: {reason}."
                )
        return self.owners[listing_id]

    def image_pass(self, listing_id, authored):
        """The image problems of a listing, fetched again once they are due.

        None when they are not known yet, so a tick without a result closes no
        issue. A result is kept for `--image-hours`, and a changed record is
        fetched at once. The pass never writes a listing or `index_status`.
        """
        rules = self.image_rules()
        if rules is None:
            reported = "images_signature" in self.cache.section("listings", listing_id)
            return None if reported else []
        found = rules.records(authored)
        section = self.cache.data["images"]
        if not found:
            section.pop(listing_id, None)
            return []

        entry = section.setdefault(listing_id, {})
        records = json.dumps([record for _, _, record in found], sort_keys=True, default=str)
        digest = hashlib.sha256(records.encode("utf-8")).hexdigest()
        if entry.get("records") != digest:
            entry.clear()
            entry["records"] = digest

        checked = parse_iso(entry.get("checked"))
        if checked is None or now() - checked >= timedelta(hours=self.options.image_hours):
            pending = found
        else:
            pending = [item for item in found if item[0] in entry.get("waiting", [])]
        if pending and self.image_budget > 0:
            self.image_budget -= len(pending)
            self.check_images(rules, pending, entry)
            if pending is found:
                entry["checked"] = iso(now())
        elif pending:
            self.log("  the image budget is spent, the next tick carries on")

        problems = entry.get("problems", {})
        if "checked" not in entry or (entry.get("waiting") and not problems):
            return None
        return [problems[place] for place, _, _ in found if place in problems]

    def check_images(self, rules, pending, entry):
        """Fetch the `pending` images of one listing and keep what fails in `entry`.

        An image that could not be fetched keeps its last result and is fetched
        again on the next tick. It becomes a problem only after
        `--unreachable-ticks` checks in a row, the patience a release host gets.
        """
        problems = entry.setdefault("problems", {})
        unavailable = []
        for place, role, record in pending:
            try:
                rules.verify(record, role)
                problems.pop(place, None)
            except rules.Invalid as error:
                problems[place] = self.image_problem(place, record, str(error))
            except rules.Unavailable as error:
                unavailable.append((place, record, str(error)))
            except Exception as error:  # noqa: BLE001 - one image never fails the listing
                reason = f"the image check behaved unexpectedly: {error!r}"
                unavailable.append((place, record, reason))

        entry.pop("waiting", None)
        if unavailable:
            count = entry.get("unavailable", 0) + 1
            entry["unavailable"] = count
            self.log(
                f"  {len(unavailable)} image(s) could not be fetched "
                f"({count} check(s) in a row)"
            )
            for place, record, reason in unavailable:
                if count >= self.options.unreachable_ticks:
                    problems[place] = self.image_problem(
                        place, record, f"it could not be fetched in consecutive checks: {reason}"
                    )
                else:
                    entry.setdefault("waiting", []).append(place)
        else:
            entry.pop("unavailable", None)
        self.log(f"  checked {len(pending)} image(s), {len(problems)} with a problem")

    @staticmethod
    def image_problem(place, record, reason):
        return (
            f"the image {place} at `{record.get('url')}`, expected sha256 "
            f"`{record.get('sha256')}`: {reason}"
        )

    def stamp_pass(
        self, listing_id, authored, authority, mirror_hosts, releases, errors, attempted=()
    ):
        """Stamp every release that appeared after the stamped frontier, and every older one `since` reaches.

        Returns whether every release behind this answer is settled, which means
        stamped, reported, or left as history. A host that could not be reached
        and a budget that ran out are not.
        """
        stamped = self.stamped_versions(listing_id)
        backfill = self.options.backfill
        frontier, complete = (None, True)
        floor = None
        if not backfill:
            frontier, complete = self.stamped_frontier(stamped, errors)
            if not complete:
                self.log("    the stamped history could not be read in full, stamping nothing")
                return False
            floor = self.since(authored, errors)
        newest = max(
            (date for date in (parse_iso(item.release_date) for item in releases) if date),
            default=None,
        )
        cutoff = (
            now() - timedelta(days=self.options.lookback_days)
            if self.options.lookback_days
            else None
        )
        settled = True
        outside_lookback = 0
        history = 0
        waiting = 0

        # Oldest first, so a budget that runs out leaves a monotone history and
        # the next tick simply carries on.
        ordered = oldest_first(releases)
        owners = self.version_owners(ordered, stamped)
        for release in ordered:
            date = parse_iso(release.release_date)
            owner = owners.get(release.version, release)

            if release.version is not None and release.version in stamped:
                if owner is not release:
                    errors.append(self.collision_error(owner, release))
                elif not self.check_for_a_swap(
                    listing_id, authority, release, stamped[release.version], errors
                ):
                    settled = False
                continue

            if cutoff and date and date < cutoff:
                # Skipped, not settled: a later tick with a wider window has
                # to see these again.
                outside_lookback += 1
                settled = False
                continue

            older = not backfill and is_history(date, frontier, newest)
            from_since = older and reaches(release.version, floor)
            # The open issue still names a release that `since` reached and
            # the checks rejected after the author raised `since` above it or
            # removed it. Such a release is history again, because retrying it
            # would download it and report it on every tick. The cache entry
            # stays, so an issue that could not be rewritten this tick does
            # not bring the release back.
            dropped = f"{listing_id}/{release.version}" in self.cache.data["rejected"]
            if (
                older
                and not from_since
                and (release.version not in attempted or dropped)
            ):
                history += 1
                continue

            if release.version is None:
                errors.append(
                    f"the tag `{release.tag}` does not parse as a version, so the release "
                    f"cannot be stamped; {VERSION_FORMS}"
                )
                continue

            if owner is not release:
                errors.append(self.collision_error(owner, release))
                continue

            if from_since:
                known = self.known_rejection(listing_id, authored, release)
                if known:
                    errors.append(known)
                    continue
                if self.since_budget <= 0:
                    # A rejection older than REJECTION_HOURS stays in the
                    # report until the release is downloaded again, so the
                    # issue does not change twice for a failure that did not.
                    stale = self.known_rejection(listing_id, authored, release, any_age=True)
                    if stale:
                        errors.append(stale)
                    waiting += 1
                    settled = False
                    continue
                self.since_budget -= 1
            elif self.stamp_budget <= 0:
                carries_on = (
                    "a further --backfill dispatch" if backfill else "the next tick"
                )
                self.log(f"    the stamp budget is spent, {carries_on} carries on")
                return False

            try:
                self.stamp_one(listing_id, authored, authority, mirror_hosts, release)
            except HostError as error:
                self.log(f"    {release.version}: {error}")
                settled = False
            except StampError as error:
                message = f"`{release.version}`: {error}"
                errors.append(message)
                if from_since:
                    self.remember_rejection(listing_id, authored, release, message)
            else:
                if from_since:
                    self.cache.data["rejected"].pop(f"{listing_id}/{release.version}", None)
                else:
                    self.stamp_budget -= 1

        if outside_lookback:
            self.log(
                f"    {outside_lookback} release(s) outside the lookback window, "
                "left for a wider tick"
            )
        if history:
            self.log(
                f"    {history} release(s) older than the stamped history, left "
                "unstamped (since under [releases] or --backfill stamps them)"
            )
        if waiting:
            self.log(
                f"    {waiting} older release(s) that since reaches wait for the "
                "since budget of the next tick"
            )
        return settled

    def version_owners(self, ordered, stamped):
        """The one release each version is stamped from (RFC 0072).

        Two tags that fill to the same version are one version. A stamped
        version belongs to the tag whose URL its file names, and any other to
        its oldest tag, so a tag that never parsed before cannot take a version
        over from the tag it was stamped from.
        """
        groups = {}
        for release in ordered:
            if release.version is not None:
                groups.setdefault(release.version, []).append(release)

        owners = {}
        for version, group in groups.items():
            owner = group[0]
            if len(group) > 1 and version in stamped:
                # A corrupt file is reported by the swap check of the owner.
                download = (self.read_release(stamped[version], []) or {}).get("download") or {}
                urls = {download.get("url"), *(download.get("mirrors") or [])} - {None}
                owner = next((release for release in group if release.url in urls), owner)
            owners[version] = owner
        return owners

    @staticmethod
    def collision_error(owner, release):
        return (
            f"the tag `{release.tag}` fills to `{release.version}`, the same version as "
            f"the tag `{owner.tag}`, so it is refused. A version is stamped exactly once, "
            "so the way forward is a new version."
        )

    def since(self, authored, errors):
        """The precedence of `since` under `[releases]`, or None (RFC 0079).

        It is read with the filling rule of RFC 0072, so `1.2` is `1.2.0`. A
        value that is not a version is reported, and the tick stamps as if the
        key were not there.
        """
        section = authored.get("releases")
        value = section.get("since") if isinstance(section, dict) else None
        if value is None:
            return None
        try:
            if not isinstance(value, str):
                raise StampError(f"{value!r} is not a version string")
            return precedence(normalize_version(filled(value)))
        except StampError as error:
            errors.append(
                f"`since` under [releases]: {error}, so no older release is stamped from it"
            )
            return None

    def stamp_inputs(self, authored):
        """What a stamp reads besides the archive, so a rejection is retried once it changes."""
        return self.authored_digest([authored, self.game_versions])

    def known_rejection(self, listing_id, authored, release, any_age=False):
        """The error an older release got from the same inputs within REJECTION_HOURS, or None.

        A back catalogue can hold many releases the checks reject, and
        downloading each of them again every tick costs time and traffic for
        an answer that is already known. The cache is derived, so losing it
        costs one more download. With `any_age`, a rejection older than
        REJECTION_HOURS counts too.
        """
        seen = self.cache.data["rejected"].get(f"{listing_id}/{release.version}") or {}
        checked = parse_iso(seen.get("checked"))
        if (
            checked is None
            or (not any_age and now() - checked >= timedelta(hours=REJECTION_HOURS))
            or seen.get("url") != release.url
            or seen.get("size") != release.size
            or seen.get("inputs") != self.stamp_inputs(authored)
        ):
            return None
        return seen.get("error")

    def remember_rejection(self, listing_id, authored, release, message):
        self.cache.data["rejected"][f"{listing_id}/{release.version}"] = {
            "url": release.url,
            "size": release.size,
            "inputs": self.stamp_inputs(authored),
            "checked": iso(now()),
            "error": message,
        }

    def stamp_one(self, listing_id, authored, authority, mirror_hosts, release):
        """Stamp one release, then look for its mirrors.

        The archive is closed before a mirror downloads, so at most one archive
        is on disk and the mirror is checked against the stamped digest.
        """
        archive, content_type = authority.download(release)
        with as_archive(archive) as archive:
            facts = release.facts()
            facts["content_type"] = content_type
            document = stamp(authored, facts, archive, self.game_versions, now=now())

        download = document["download"]
        mirrors = self.mirrors_for(mirror_hosts, release, download["sha256"])
        if mirrors:
            download["mirrors"] = mirrors
        path = self.folder(listing_id) / f"{document['version']}.json"
        self.write(path, serialize(document), f"Stamp {listing_id} {document['version']}")
        self.log(f"    stamped {document['version']} ({download['size']} bytes)")
        self.stamped.append(f"{listing_id} {document['version']}")

    def check_for_a_swap(self, listing_id, authority, release, path, errors):
        """A stamped version is never overwritten, and a swap gets reported.

        The signal is the size and URL the release list already carries; a swap
        keeping the byte count needs every archive re-downloaded per tick for an
        answer `download.sha256` already gives the client. SpaceDock reports no
        size, so only the URL comparison remains there. A deleted asset is not
        in the list at all, which `gone_pass` handles.
        """
        document = self.read_release(path, errors)
        if document is None:
            return True  # Reported; nothing changes here until a human acts.
        download = document.get("download") or {}
        stamped_digest = (download.get("sha256") or "").upper()
        same_size = release.size is None or release.size == download.get("size")
        same_url = (release.url or download.get("url")) == download.get("url")
        if same_size and same_url:
            return True

        # A rejection is permanent, so re-downloading the swapped archive every
        # tick would spend the tick on an answer that is already known. The
        # cache is derived: losing it costs one more download.
        seen = self.cache.section("swaps", f"{listing_id}/{release.version}")
        if seen.get("size") == release.size and seen.get("url") == release.url:
            if seen.get("digest"):
                errors.append(
                    self.swap_error(release.version, stamped_digest, seen["digest"])
                )
            return True

        try:
            archive, _ = authority.download(release)
        except HostError as error:
            self.log(f"    {release.version}: {error}")
            return False
        except StampError as error:
            errors.append(f"`{release.version}`: {error}")
            return True

        with as_archive(archive) as archive:
            digest = archive.sha256
        seen.update({"size": release.size, "url": release.url, "checked": iso(now())})
        if digest == stamped_digest:
            seen["digest"] = None
            self.log(f"    {release.version}: the same bytes at a new URL")
            self.append_mirror(path, document, release.url)
            if "unavailable_since" in download:
                self.unmark(listing_id, path, document)
            return True

        seen["digest"] = digest
        errors.append(self.swap_error(release.version, stamped_digest, digest))
        return True

    @staticmethod
    def swap_error(version, stamped_digest, served_digest):
        return (
            f"`{version}` is already stamped from `{stamped_digest}`, and the host now "
            f"serves `{served_digest}` for the same tag. A version is stamped exactly "
            "once and the file is never overwritten, so this release is rejected. The "
            "way forward is a new version, or a yank of this one."
        )

    def month_pass(self, listing_id, authored, errors):
        """Resolve an authored `game_max` month once that month is over.

        Adding the bound is the stamp correction RFC 0033 describes, not an
        amendment: the file only becomes less permissive and nothing already
        present is touched. It reads the authored document, so it needs no host
        and runs for every listing.
        """
        bound = ((authored.get("compatibility") or {}).get("game_max") or "").strip()
        match = GAME_MONTH.match(bound)
        if match is None:
            return
        if not month_is_over(int(match.group(1)), int(match.group(2)), now()):
            return

        try:
            display, revision = resolve_bound(bound, "game_max", self.game_versions, now())
        except StampError as error:
            errors.append(f"game_max: {error}")
            return
        if display is None:
            return

        for version, path in self.stamped_versions(listing_id).items():
            document = self.read_release(path, errors)
            if document is None:
                continue
            if "game_max" in document or "game_min_revision" not in document:
                continue
            if revision < document["game_min_revision"]:
                errors.append(
                    f"game_max `{bound}` resolves to revision {revision}, below "
                    f"`{version}`'s stamped game_min_revision "
                    f"{document['game_min_revision']}, so it is not applied"
                )
                continue
            updated = {}
            for key, value in document.items():
                updated[key] = value
                if key == "game_min_revision":
                    # Inserted where a fresh stamp would put it, so a corrected
                    # file and a fresh one have the same shape.
                    updated["game_max"] = display
                    updated["game_max_revision"] = revision
            self.write(
                path,
                serialize(updated),
                f"Resolve the game_max month for {listing_id} {version}",
            )
            self.log(f"    resolved game_max {display} onto {version}")

    def changelog_pass(self, listing_id, releases, errors):
        """Keep `changelog_text` equal to the host's notes for every release the list carries (RFC 0064, RFC 0079).

        The notes come with the release list the tick already holds, so the pass
        costs no request. They are the notes of the tag the version is stamped
        from. Notes that are empty or over the limit leave the field out, as a
        fresh stamp does.
        """
        stamped = self.stamped_versions(listing_id)
        owners = self.version_owners(oldest_first(releases), stamped)
        for version, path in stamped.items():
            owner = owners.get(version)
            # None is a host answer that does not say, which changes nothing.
            if owner is None or owner.changelog_text is None:
                continue
            text = changelog_text(owner.changelog_text) or ""
            document = self.read_release(path, errors)
            if document is None:
                continue
            if document.get("changelog_text", "") == text:
                continue
            updated = {}
            for key, value in document.items():
                if key == "listing" and text:
                    updated["changelog_text"] = text
                if key != "changelog_text":
                    updated[key] = value
            if text:
                updated.setdefault("changelog_text", text)

            if not text:
                action = "Remove the changelog text from"
            elif "changelog_text" in document:
                action = "Replace the changelog text of"
            else:
                action = "Add the changelog text to"
            self.write(path, serialize(updated), f"{action} {listing_id} {version}")
            self.log(f"    {action.lower()} {version}")
            self.noted.append(f"{listing_id} {version}")

    def append_mirror(self, path, document, url):
        """Record a further URL proven byte-identical, as a mirror.

        `download.url` is immutable, so a release whose authority now serves
        the same bytes from a new address keeps its stamped URL and gains the
        new one as a mirror, which RFC 0031 admits for any source whose bytes
        match the sha256.
        """
        download = document.get("download") or {}
        if not url or url == download.get("url"):
            return
        mirrors = list(download.get("mirrors") or [])
        if url in mirrors:
            return
        download["mirrors"] = mirrors + [url]
        self.write(
            path,
            serialize(document),
            f"Add a mirror for {document['id']} {document['version']}",
        )
        self.mirrored.append(f"{document['id']} {document['version']}")

    def mirrors_for(self, mirror_hosts, release, digest):
        """The non-authority hosts serving byte-identical bytes for this release.

        `digest` is the stamped SHA-256. Shares the mirror budget with
        `mirror_pass`: verifying costs a full download, and a fresh-stamp burst
        must not multiply that unbounded. A mirror that did not fit the budget
        is appended by a later tick's pass.
        """
        found = []
        for host in mirror_hosts:
            candidate = self.mirror_release(host, release.version)
            if candidate is None or self.known_to_differ(host, candidate):
                continue
            if self.mirror_budget <= 0:
                break
            self.mirror_budget -= 1
            url = self.verify_mirror(host, candidate, digest)
            if url:
                found.append(url)
        return found

    def mirror_release(self, host, version):
        """The mirror host's release for `version`, from one list per tick."""
        if host.key not in self._mirror_lists:
            try:
                listed, _ = host.releases()
            except (HostError, StampError) as error:
                self.log(f"    {host.key}: {error}")
                listed = []
            self._mirror_lists[host.key] = listed or []
        return next(
            (
                release
                for release in self._mirror_lists[host.key]
                if release.version == version
            ),
            None,
        )

    def verify_mirror(self, host, release, digest):
        """The mirror's URL when its bytes are identical, else None.

        Different bytes are remembered by URL and size, so the mirror is not
        downloaded again until one of them changes.
        """
        try:
            archive, _ = host.download(release)
        except (HostError, StampError) as error:
            self.log(f"    {host.key}: {error}")
            return None
        with as_archive(archive) as archive:
            served = archive.sha256
        if served != digest:
            self.log(f"    {host.key} serves different bytes for {release.version}")
            self.cache.section("differs", f"{host.key} {release.version}").update(
                {"url": release.url, "size": release.size, "checked": iso(now())}
            )
            return None
        return release.url

    def known_to_differ(self, host, release):
        """Whether the mirror served different bytes at this URL and size before.

        The cache is derived: losing it costs one more download.
        """
        seen = self.cache.data["differs"].get(f"{host.key} {release.version}")
        return bool(seen) and seen.get("url") == release.url and seen.get("size") == release.size

    def mirror_pass(self, listing_id, mirror_hosts, errors):
        """Append a mirror that appeared after a release was stamped.

        Watcher-only and append-only. Verifying one costs a full download, so a
        tick works through the least recently checked candidates within a budget,
        and the rest wait for the next tick.
        """
        if not mirror_hosts or self.mirror_budget <= 0:
            return

        candidates = []
        for version, path in self.stamped_versions(listing_id).items():
            document = self.read_release(path, errors)
            if document is None:
                continue
            known = set((document.get("download") or {}).get("mirrors") or [])
            if len(known) >= len(mirror_hosts):
                continue
            key = f"{listing_id}/{version}"
            checked = parse_iso(self.cache.section("mirrors", key).get("checked"))
            candidates.append((checked or datetime.min.replace(tzinfo=timezone.utc), version, path))

        for _, version, path in sorted(candidates, key=lambda entry: entry[0]):
            if self.mirror_budget <= 0:
                return
            document = self.read_release(path, errors)
            if document is None:
                continue
            download = document.get("download")
            if not download:
                continue
            known = list(download.get("mirrors") or [])
            found = []
            for host in mirror_hosts:
                candidate = self.mirror_release(host, version)
                if candidate is None or candidate.url in known or self.known_to_differ(host, candidate):
                    continue
                self.mirror_budget -= 1
                url = self.verify_mirror(host, candidate, (download.get("sha256") or "").upper())
                if url:
                    found.append(url)
            self.cache.section("mirrors", f"{listing_id}/{version}")["checked"] = iso(now())
            if not found:
                continue
            download["mirrors"] = known + found
            self.write(
                path,
                serialize(document),
                f"Add a mirror for {listing_id} {version}",
            )
            self.log(f"    appended {len(found)} mirror(s) to {version}")
            self.mirrored.append(f"{listing_id} {version}")

    def gone_pass(self, listing_id, authority, releases, errors):
        """Mark a stamped release the authority host no longer lists, and remove the mark once it lists it again (RFC 0078).

        `releases` is None when the host answered unchanged, which repeats the
        last answer, so a wait goes on and none starts or ends. In a truncated
        answer, a release that is not in it is no observation.

        Returns False when a marked release is listed again but its bytes could
        not be checked, so that the next tick fetches the full list and checks
        them, instead of an unchanged answer that keeps the mark.
        """
        settled = True
        listed = None
        if releases is not None:
            listed = {
                url for release in releases for url in (release.url, *release.archives) if url
            }
        truncated = getattr(authority, "truncated", False)
        section = self.cache.data["gone"]
        for version, path in self.stamped_versions(listing_id).items():
            document = self.read_release(path, errors)
            if document is None:
                continue
            key = f"{listing_id}/{version}"
            download = document.get("download") or {}
            marked = "unavailable_since" in download
            found = [url for url in stamped_urls(download) if listed and url in listed]
            if found:
                if marked:
                    size = next((item.size for item in releases if item.url == found[0]), None)
                    if not self.restore(
                        listing_id, authority.download, path, document, found[0], size, errors
                    ):
                        settled = False
                else:
                    section.pop(key, None)
            elif marked:
                if listed is None:
                    self.report_swap(listing_id, document, errors)
                else:
                    section.pop(key, None)
            elif listed is not None and not truncated:
                wait = section.setdefault(key, {})
                wait.setdefault("since", iso(now()))
                self.confirm(listing_id, path, document, wait)
            elif listed is None and "since" in section.get(key, {}):
                self.confirm(listing_id, path, document, section[key])
        return settled

    def confirm(self, listing_id, path, document, wait):
        """Ask every stamped URL of a release the host stopped listing, once the wait is over.

        Every URL has to answer that the archive is gone. Any other answer
        marks nothing, and the watcher asks again a day later.
        """
        version = document.get("version")
        day = timedelta(hours=GONE_HOURS)
        started = parse_iso(wait.get("since"))
        asked = parse_iso(wait.get("asked"))
        if started is None or now() - started < day or (asked and now() - asked < day):
            return
        urls = stamped_urls(document.get("download") or {})
        answers = self.ask(urls)
        if not answers:
            return
        wait["asked"] = iso(now())
        if all(answer in hosts.GONE for answer in answers):
            self.mark(listing_id, path, document)
            return
        self.log(f"    {version}: its host does not list it, and not every URL says it is gone")
        if answers[0] in hosts.GONE and any(served(answer) for answer in answers[1:]):
            if not wait.get("mirror"):
                wait["mirror"] = True
                self.tell_later(
                    listing_id,
                    f"`{version}` is gone from its host, and a mirror in `download.mirrors` "
                    "still serves it, so it is not marked.",
                )

    def gone_by_request(self, listing_id, errors):
        """Ask the stamped URLs of a listing without [releases] once a day, because no host lists its releases (RFC 0078).

        A release is marked when every URL said that the archive is gone in
        every answer over at least a day, and its mark goes once a URL serves
        the stamped bytes again. A host that could not be evaluated is no
        observation.
        """
        section = self.cache.data["gone"]
        day = timedelta(hours=GONE_HOURS)
        for version, path in self.stamped_versions(listing_id).items():
            document = self.read_release(path, errors)
            if document is None:
                continue
            entry = section.setdefault(f"{listing_id}/{version}", {})
            download = document.get("download") or {}
            marked = "unavailable_since" in download
            urls = stamped_urls(download)
            asked = parse_iso(entry.get("asked"))
            answers = self.ask(urls) if asked is None or now() - asked >= day else None
            if not answers:
                self.report_swap(listing_id, document, errors)
                continue
            entry["asked"] = iso(now())
            serving = [url for url, answer in zip(urls, answers) if served(answer)]
            if serving:
                # Asked once a day, so the bytes are downloaded again each time.
                entry.pop("since", None)
                entry.pop("swap", None)
                if marked and not self.restore(
                    listing_id, lambda release: hosts.download(self.http, release),
                    path, document, serving[0], None, errors,
                ):
                    # The bytes are not checked yet, so the next tick asks again.
                    entry.pop("asked", None)
            elif all(answer in hosts.GONE for answer in answers):
                entry.pop("swap", None)
                since = parse_iso(entry.setdefault("since", iso(now())))
                if not marked and now() - since >= day:
                    self.mark(listing_id, path, document)
            elif None not in answers:
                entry.pop("since", None)

    def ask(self, urls):
        """The status each URL answers, None for one that could not be evaluated.

        Nothing is asked, and None comes back, when the gone budget does not
        cover every URL, because a mark needs the answer of each one.
        """
        if not urls or self.gone_budget < len(urls):
            if urls:
                self.log("    the gone budget is spent, the next tick carries on")
            return None
        self.gone_budget -= len(urls)
        answers = []
        for url in urls:
            try:
                answers.append(self.http.status(url))
            except HostError as error:
                self.log(f"    {url}: {error}")
                answers.append(None)
        return answers

    def restore(self, listing_id, fetch, path, document, url, size, errors):
        """Remove the gone mark once `url` serves the stamped bytes again.

        Other bytes are a swap, reported like any other, and the mark stays.
        The swap is remembered by URL and size, so it is not downloaded again
        until one of them changes. Returns whether the bytes were checked, which
        is False when the gone budget is spent or the download failed.
        """
        version = document.get("version")
        entry = self.cache.section("gone", f"{listing_id}/{version}")
        swap = entry.get("swap")
        if swap and swap.get("url") == url and swap.get("size") == size:
            self.report_swap(listing_id, document, errors)
            return True
        if self.gone_budget <= 0:
            self.log(f"    {version}: the gone budget is spent, the next tick checks its bytes")
            return False
        self.gone_budget -= 1
        try:
            archive, _ = fetch(hosts.stamped_release(document, url))
        except (HostError, StampError) as error:
            self.log(f"    {version}: {error}, the next tick checks its bytes")
            return False
        with as_archive(archive) as archive:
            digest = archive.sha256
        if digest == (document["download"].get("sha256") or "").upper():
            self.unmark(listing_id, path, document)
            return True
        entry["swap"] = {"url": url, "size": size, "digest": digest}
        self.report_swap(listing_id, document, errors)
        return True

    def report_swap(self, listing_id, document, errors):
        """Report the other bytes a marked release was last found with, which keeps its mark."""
        version = document.get("version")
        swap = self.cache.data["gone"].get(f"{listing_id}/{version}", {}).get("swap")
        download = document.get("download") or {}
        if swap and "unavailable_since" in download:
            errors.append(
                self.swap_error(version, (download.get("sha256") or "").upper(), swap["digest"])
            )

    def mark(self, listing_id, path, document):
        """Write `download.unavailable_since`, which keeps its value while the mark stands."""
        version = document["version"]
        document["download"]["unavailable_since"] = iso(now())
        self.write(path, serialize(document), f"Mark {listing_id} {version} as gone from its host")
        self.log(f"    marked {version} as gone from its host")
        self.marked.append(f"{listing_id} {version}")
        self.tell_later(listing_id, f"`{version}` is gone from its host, so the watcher marked it.")

    def unmark(self, listing_id, path, document):
        """Remove `download.unavailable_since`, after the stamped bytes were found again."""
        version = document["version"]
        document["download"].pop("unavailable_since", None)
        self.write(path, serialize(document), f"Remove the gone mark from {listing_id} {version}")
        entry = self.cache.data["gone"].get(f"{listing_id}/{version}") or {}
        for name in ("since", "mirror", "swap"):
            entry.pop(name, None)
        self.log(f"    removed the gone mark from {version}")
        self.unmarked.append(f"{listing_id} {version}")
        self.tell_later(
            listing_id,
            f"`{version}` is on its host again with the stamped bytes, so the watcher "
            "removed its mark.",
        )

    def tell_later(self, listing_id, line):
        self.notes.setdefault(listing_id, []).append(line)

    def tell(self, listing_id):
        """Post the notes of this listing as one comment, and keep them for the next tick when that fails."""
        state = self.cache.section("listings", listing_id)
        lines = state.pop("notes", []) + self.notes.pop(listing_id, [])
        if not lines:
            return
        text = "\n".join(
            [
                "The watcher found a change in which releases can be downloaded:",
                "",
                *[f"- {line}" for line in lines],
                "",
                "A client does not offer a release marked as gone for a new install, and "
                "it never changes an installed copy. Uploading the same archive again "
                "removes the mark.",
            ]
        )
        if not self.issues.note(listing_id, text, self.cache):
            state["notes"] = lines

    def unreachable(self, listing_id, state, message):
        """A host that could not be evaluated. Latency, not data.

        It reaches the author only after consecutive failed ticks, so a short
        outage stays out of everyone's notifications. `>=` rather than `==`
        keeps a lost cache from restarting the countdown silently, and the count
        stays out of the text so an unchanged outage is not a new failure.
        """
        count = state.get("unreachable", 0) + 1
        state["unreachable"] = count
        self.log(f"  could not be evaluated ({count} tick(s) in a row): {message}")
        if count >= self.options.unreachable_ticks:
            errors = [
                "the authority host has stayed unreachable across consecutive "
                f"ticks: {message}"
            ]
            state["unreachable_signature"] = Issues.signature_of(errors)
            self.failed.append(listing_id)
            self.issues.report(listing_id, errors, self.cache)

    def recover(self, listing_id, state):
        """The host answered again. Close the outage issue, and only that one.

        The signature guard keeps a recovery from closing an issue that
        meanwhile reports something else about the listing.
        """
        signature = state.pop("unreachable_signature", None)
        state["unreachable"] = 0
        if signature:
            self.issues.resolve_if(listing_id, signature, self.cache)

    def summarize(self):
        counts = [
            f"- stamped: {len(self.stamped)}",
            f"- mirrors appended: {len(self.mirrored)}",
            f"- changelog texts added, replaced or removed: {len(self.noted)}",
            f"- marked as gone from their host: {len(self.marked)}",
            f"- gone marks removed: {len(self.unmarked)}",
            f"- listings with an error: {len(set(self.failed))}",
            f"- host requests: {self.http.requests}",
        ]
        summary = ["## Watcher", "", *counts]
        for title, names in (
            ("Stamped", self.stamped),
            ("Mirrors", self.mirrored),
            ("Changelog texts", self.noted),
            ("Marked as gone", self.marked),
            ("Gone marks removed", self.unmarked),
            ("Reported", sorted(set(self.failed))),
        ):
            if names:
                summary += ["", f"### {title}", ""] + [f"- `{name}`" for name in names]

        print("\n".join(counts))
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(summary) + "\n")


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="One tick of the watcher.")
    parser.add_argument(
        "--authored", default=".authored", type=Path,
        help="a checkout of the authored repository, which holds listings/",
    )
    parser.add_argument(
        "--authored-repo", default="KSAModding/content-index",
        help="the authored repository, for issues and the sweep",
    )
    parser.add_argument("--releases", default="releases", type=Path)
    parser.add_argument("--game-versions", default="game-versions.json", type=Path)
    parser.add_argument(
        "--cache", default=".watcher/cache.json", type=Path,
        help="the derived cache: ETags, failure counts, issue numbers. Never state",
    )
    parser.add_argument(
        "--listing", action="append",
        help="only this listing, repeatable. For a manual dispatch",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=0,
        help="ignore releases older than this. 0 scans the host's whole list",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="also stamp releases older than what is stamped, with today's "
        "authored facts rather than the ones they shipped under",
    )
    parser.add_argument(
        "--stamp-budget", type=int, default=20,
        help="how many releases one tick stamps at most; the next tick carries on",
    )
    parser.add_argument(
        "--since-budget", type=int, default=10,
        help="how many older releases that since reaches one tick tries at most, "
        "apart from --stamp-budget so a back catalogue never holds up a new release",
    )
    parser.add_argument(
        "--mirror-budget", type=int, default=4,
        help="how many mirror candidates one tick verifies at most",
    )
    parser.add_argument(
        "--gone-budget", type=int, default=20,
        help="how many requests one tick spends at most on whether stamped archives "
        "are gone from their host; the next tick carries on",
    )
    parser.add_argument(
        "--image-hours", type=int, default=24,
        help="hours before the images of a listing are fetched again",
    )
    parser.add_argument(
        "--image-budget", type=int, default=20,
        help="how many images one tick fetches at most; the next tick carries on",
    )
    parser.add_argument(
        "--unreachable-ticks", type=int, default=6,
        help="consecutive failed ticks before a host being down reaches the author",
    )
    parser.add_argument("--issue-label", default="watcher")
    parser.add_argument("--steward-team", default="@KSAModding/content-manager-stewards")
    parser.add_argument("--sweep-workflow", default="checks.yml")
    parser.add_argument("--verdict-check", default="validate")
    parser.add_argument("--sweep-limit", type=int, default=30)
    parser.add_argument("--sweep-attempts", type=int, default=3)
    parser.add_argument("--sweep-cooldown", type=int, default=30, help="minutes")
    parser.add_argument(
        "--sweep-refusal-hours", type=int, default=24,
        help="hours before a re-run GitHub refused is tried again",
    )
    parser.add_argument("--no-sweep", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="write nothing, commit nothing, and open nothing. Downloads still happen",
    )
    parser.add_argument(
        "--fail-on-error", action="store_true",
        help="exit non-zero when a listing was reported. Off by default, because a "
        "broken listing is an issue on the authored repository and not a red tick",
    )
    options = parser.parse_args(argv)
    if options.backfill and not options.listing:
        parser.error("--backfill needs --listing: it is not an every-listing operation")
    options.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("INDEX_TOKEN")
    return options


def main(argv=None):
    options = parse_arguments(argv)
    if not options.token:
        print(
            "no token in GITHUB_TOKEN: the tick can read public hosts but cannot keep "
            "issues current or sweep",
            file=sys.stderr,
        )
    watcher = Watcher(options)
    watcher.tick()
    return 1 if options.fail_on_error and watcher.failed else 0


if __name__ == "__main__":
    sys.exit(main())
