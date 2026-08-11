#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One tick of the watcher (RFC 0033).

Scan every authored listing's authority host, stamp every release that has no
file under `releases/<id>/` yet, commit it, keep one error issue per listing
current on the authored repository, and sweep that repository's open pull
requests.

What is stamped in the repository is the whole state, so a tick GitHub delays,
drops or cancels costs latency and not data, and a re-run stamps nothing twice.

Per-release derivation is tools/stamp_release.py, the hosts are tools/hosts.py.
"""

import argparse
import hashlib
import json
import os
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

import hosts
from hosts import HostError
from stamp_release import (
    GAME_MONTH,
    StampError,
    month_is_over,
    resolve_bound,
    serialize,
    stamp,
    valid_id,
)

GITHUB_API = "https://api.github.com"

# 2: the host ETag entries moved to a per-listing key. The cache is derived,
# so a version bump just costs one expensive tick.
CACHE_VERSION = 2

# The marker that makes a listing's issue findable without a search, and the
# signature that decides whether a genuinely new error deserves a comment.
LISTING_MARKER = "<!-- watcher:listing={id} -->"
SIGNATURE_MARKER = "<!-- watcher:signature={signature} -->"
WAITING_MARKER = "<!-- watcher:waiting={sha} -->"

# A check run conclusion that is neither a pass nor a reject: the check could
# not run to a verdict, which never auto-merges and never auto-rejects, and is
# what the sweep re-dispatches.
COULD_NOT_EVALUATE = frozenset(
    {"cancelled", "timed_out", "stale", "neutral", "skipped", "action_required"}
)
PENDING = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class Cache:
    """Derived cache, never state.

    It holds the per-listing ETags, the consecutive-failure counts, the issue
    numbers, and what the mirror and sweep passes already tried. Every entry is
    rebuildable from the repository and the hosts, so losing the whole file
    costs one expensive tick and nothing else.
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
        for section in ("hosts", "listings", "mirrors", "swaps", "sweep"):
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
    """

    def __init__(self, api, label, log=print):
        self.api = api
        self.label = label
        self.log = log
        self._open = {}
        self._degraded = False

    @staticmethod
    def signature_of(errors):
        """The stable fingerprint of a failure, for edit-versus-comment decisions."""
        return hashlib.sha256("\n".join(sorted(errors)).encode()).hexdigest()[:16]

    def _all_open(self, labelled):
        """The open issues of the authored repository, listed once per tick.

        The label narrows the list to the watcher's own issues, and the
        unlabelled list is the fallback for the tick that opened an issue before
        the label existed. A listing that fails marks the lookup degraded, so
        `report` skips creating rather than duplicating an issue it could not
        see.
        """
        if labelled not in self._open:
            query = {"labels": self.label} if labelled else {}
            try:
                issues = self.api.get_paged("/issues", state="open", **query)
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"  could not list issues: {error}")
                self._degraded = True
                issues = []
            self._open[labelled] = [
                issue for issue in issues if "pull_request" not in issue
            ]
        return self._open[labelled]

    def find(self, listing_id, cache):
        marker = LISTING_MARKER.format(id=listing_id)
        remembered = cache.section("listings", listing_id).get("issue")
        if remembered:
            try:
                issue = self.api.get(f"/issues/{remembered}")
            except (urllib.error.HTTPError, HostError):
                issue = None
            if issue and issue.get("state") == "open" and marker in (issue.get("body") or ""):
                return issue
        for labelled in (True, False):
            for issue in self._all_open(labelled):
                if marker in (issue.get("body") or ""):
                    cache.section("listings", listing_id)["issue"] = issue["number"]
                    return issue
        return None

    def report(self, listing_id, errors, cache):
        """Keep the listing's issue current with `errors`.

        Never raises for an API-shaped failure: reporting is best-effort, and
        several callers sit inside except clauses, where a raise would leave
        the per-listing guard and take the rest of the tick with it.
        """
        try:
            self._report(listing_id, errors, cache)
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"  could not keep the issue for {listing_id} current: {error}")

    def _report(self, listing_id, errors, cache):
        signature = self.signature_of(errors)
        body = self._body(listing_id, errors, signature)
        title = f"{listing_id}: the watcher could not stamp a release"
        issue = self.find(listing_id, cache)

        if issue is None:
            if self._degraded:
                self.log(
                    "  not opening an issue: the issue list could not be read "
                    "this tick, and a blind create duplicates"
                )
                return
            try:
                created = self.api.send(
                    "POST", "/issues", {"title": title, "body": body, "labels": [self.label]}
                )
            except urllib.error.HTTPError as error:
                if error.code != 422:
                    self.log(f"  could not open an issue (HTTP {error.code})")
                    return
                # A label the repository does not define is not worth losing the
                # report over; the marker in the body is what the watcher finds
                # the issue by anyway.
                self.log(f"  the '{self.label}' label was refused (HTTP 422)")
                try:
                    created = self.api.send("POST", "/issues", {"title": title, "body": body})
                except urllib.error.HTTPError as retry_error:
                    self.log(f"  could not open an issue (HTTP {retry_error.code})")
                    return
            if created:
                cache.section("listings", listing_id)["issue"] = created["number"]
                self.log(f"  opened {self.api.repository}#{created['number']}")
            return

        number = issue["number"]
        known = SIGNATURE_MARKER.format(signature=signature) in (issue.get("body") or "")
        self.api.send("PATCH", f"/issues/{number}", {"title": title, "body": body})
        if not known:
            self.api.send(
                "POST",
                f"/issues/{number}/comments",
                {"body": "The watcher is now failing on something else:\n\n"
                         + "\n".join(f"- {error}" for error in errors)},
            )
        self.log(f"  kept {self.api.repository}#{number} current")

    def resolve(self, listing_id, cache):
        """Close the listing's issue, because the tick evaluated it cleanly.

        Best-effort like `report`: a failure here leaves an issue open one tick
        longer, which is not worth the rest of the tick.
        """
        try:
            self._resolve(listing_id, cache)
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"  could not close the issue for {listing_id}: {error}")

    def _resolve(self, listing_id, cache):
        issue = self.find(listing_id, cache)
        if issue is None:
            return
        number = issue["number"]
        self.api.send(
            "POST",
            f"/issues/{number}/comments",
            {"body": "The watcher stamped this listing without an error, so this is done."},
        )
        self.api.send("PATCH", f"/issues/{number}", {"state": "closed"})
        cache.section("listings", listing_id).pop("issue", None)
        self.log(f"  closed {self.api.repository}#{number}")

    def resolve_if(self, listing_id, signature, cache):
        """Close the listing's issue only when it reports exactly `signature`.

        The recovery from an unreachable host must not close an issue that
        meanwhile reports something else, and the signature marker is what
        tells the two apart.
        """
        issue = self.find(listing_id, cache)
        if issue is None:
            return
        if SIGNATURE_MARKER.format(signature=signature) in (issue.get("body") or ""):
            self.resolve(listing_id, cache)

    def _body(self, listing_id, errors, signature):
        return "\n".join(
            [
                LISTING_MARKER.format(id=listing_id),
                SIGNATURE_MARKER.format(signature=signature),
                f"The watcher cannot stamp `{listing_id}`.",
                "",
                *[f"- {error}" for error in errors],
                "",
                "The watcher retries every tick and keeps this issue current rather than",
                "opening a new one. It closes by itself once a tick evaluates the listing",
                "without an error.",
                "",
                f"Last checked {iso(now())}.",
            ]
        )


class Sweep:
    """The event-driven half, swept once per tick.

    GitHub drops event-driven triggers with nothing to retry them, so every tick
    re-dispatches the validation of an open pull request whose head commit has
    no check suite or whose latest run ended in could-not-evaluate, and pings a
    steward for a run waiting for approval, which a dispatch cannot release.
    """

    def __init__(self, api, cache, options, log=print):
        self.api = api
        self.cache = cache
        self.options = options
        self.log = log

    def run(self):
        try:
            pulls = self.api.get_paged("/pulls", state="open")[: self.options.sweep_limit]
        except urllib.error.HTTPError as error:
            self.log(f"  could not list pull requests: HTTP {error.code}")
            return
        except HostError as error:
            self.log(f"  could not list pull requests: {error}")
            return

        for pull in pulls:
            try:
                self._one(pull)
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"  #{pull['number']}: {error}")

        # The sweep section would otherwise keep a key per head commit forever,
        # in a file the workflow uploads and downloads every ten minutes.
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

        suites = self.api.get(f"/commits/{sha}/check-suites") or {}
        if any(run.get("status") in PENDING for run in runs):
            return  # Something is still running; a verdict is on its way.

        verdict = self._verdict(sha)
        if suites.get("total_count") and verdict == "pass":
            return
        if verdict == "reject":
            return  # A reject is a verdict. It waits for the author, not for us.

        reason = (
            "no check suite" if not suites.get("total_count") else f"verdict {verdict}"
        )
        self._dispatch(number, sha, state, reason)

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

    def _dispatch(self, number, sha, state, reason):
        attempts = state.get("attempts", 0)
        last = parse_iso(state.get("last"))
        refused = parse_iso(state.get("refused"))
        if attempts >= self.options.sweep_attempts:
            return
        if last and now() - last < timedelta(minutes=self.options.sweep_cooldown):
            return
        if refused and now() - refused < timedelta(hours=self.options.sweep_refusal_hours):
            return

        self.log(f"  #{number}: re-dispatching validation ({reason})")
        try:
            self.api.send(
                "POST",
                f"/actions/workflows/{self.options.sweep_workflow}/dispatches",
                {"ref": self.options.sweep_ref, "inputs": {"pull_request": str(number)}},
            )
        except urllib.error.HTTPError as error:
            # A validation workflow that does not accept a dispatch yet is the
            # authored repository's business, and never fails a tick. The
            # refusal is retried on a clock rather than retired for good, so
            # stuck pull requests recover the moment the workflow gains the
            # input.
            self.log(
                f"  #{number}: {self.options.sweep_workflow} did not accept the dispatch "
                f"(HTTP {error.code}); the sweep needs it to take a pull_request input"
            )
            state["refused"] = iso(now())
            return
        state.pop("refused", None)
        state["last"] = iso(now())
        state["attempts"] = attempts + 1

    def _ping(self, number, sha, state):
        if state.get("pinged"):
            return
        marker = WAITING_MARKER.format(sha=sha)
        comments = self.api.get(f"/issues/{number}/comments", per_page=100) or []
        if any(marker in (comment.get("body") or "") for comment in comments):
            state["pinged"] = True
            return
        self.log(f"  #{number}: waiting for approval, pinging a steward")
        self.api.send(
            "POST",
            f"/issues/{number}/comments",
            {
                "body": f"{marker}\n{self.options.steward_team} this run is sitting in "
                "GitHub's waiting-for-approval state, which the watcher cannot release "
                "with a dispatch. It needs a steward to approve the workflow run."
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
        self.issues = Issues(self.api, options.issue_label, self.log)
        self.game_versions = json.loads(
            Path(options.game_versions).read_text(encoding="utf-8")
        )["versions"]
        self.stamp_budget = options.stamp_budget
        self.mirror_budget = options.mirror_budget
        self.stamped = []
        self.mirrored = []
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
            for path in self.listings():
                listing_id = path.stem
                try:
                    with path.open("rb") as handle:
                        authored = tomllib.load(handle)
                    listing_id = (authored.get("id") or path.stem).strip()
                    problem = self.listing_problem(path, listing_id)
                    if problem:
                        self.log(f"{listing_id}: {problem}")
                        self.failed.append(listing_id)
                        self.issues.report(listing_id, [problem], self.cache)
                        continue
                    self.log(f"{listing_id}:")
                    self.one_listing(listing_id, authored)
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

    def one_listing(self, listing_id, authored):
        state = self.cache.section("listings", listing_id)
        errors = []

        self.month_pass(listing_id, authored, errors)

        try:
            authority, mirrors = hosts.build(
                authored.get("releases"), self.http, listing_id
            )
        except StampError as error:
            self.failed.append(listing_id)
            self.issues.report(listing_id, errors + [str(error)], self.cache)
            return
        if authority is None:
            self.log("  no [releases] section, so releases enter by pull request")
            if errors:
                self.failed.append(listing_id)
                self.issues.report(listing_id, errors, self.cache)
            return

        # Keyed per listing, not per host: two listings naming the same
        # repository must not blind each other through a shared ETag.
        host_state = self.cache.section("hosts", f"{listing_id}/{authority.key}")
        try:
            releases, etag = authority.releases(host_state.get("etag"))
        except HostError as error:
            self.unreachable(listing_id, state, str(error))
            return
        except StampError as error:
            state["unreachable"] = 0
            state.pop("unreachable_signature", None)
            self.failed.append(listing_id)
            self.issues.report(listing_id, errors + [str(error)], self.cache)
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
                listing_id, authored, authority, mirrors, releases, errors
            )

        self.mirror_pass(listing_id, mirrors, errors)

        if settled:
            # The ETag stands for "every release behind this answer is either
            # stamped or reported", so a tick that ran out of budget or could
            # not reach the host always refetches, while a release that will
            # never be stampable costs no request per tick. A payload that
            # changes changes the ETag, so a fixed tag is picked up at once.
            if etag:
                host_state["etag"] = etag
            host_state["checked"] = iso(now())
        else:
            host_state.pop("etag", None)

        if errors:
            self.failed.append(listing_id)
            for error in errors:
                self.log(f"    reporting: {error}")
            self.issues.report(listing_id, errors, self.cache)
        elif releases is not None and settled:
            self.issues.resolve(listing_id, self.cache)

    def stamp_pass(self, listing_id, authored, authority, mirror_hosts, releases, errors):
        """Stamp every release with no file yet.

        Returns whether every release behind this answer is now settled, which
        means stamped or reported. A rejected release is settled: nothing about
        it will change until the host's answer does, and that changes the ETag.
        A host that could not be reached and a budget that ran out are not.
        """
        stamped = self.stamped_versions(listing_id)
        cutoff = (
            now() - timedelta(days=self.options.lookback_days)
            if self.options.lookback_days
            else None
        )
        settled = True
        outside_lookback = 0

        # Oldest first, so a budget that runs out leaves a monotone history and
        # the next tick simply carries on.
        ordered = sorted(releases, key=lambda release: (release.release_date or "", release.tag))
        for release in ordered:
            if release.version is None:
                errors.append(
                    f"the tag `{release.tag}` does not parse as a version, so the release "
                    "cannot be stamped; SemVer 2.0.0, with an optional leading `v`"
                )
                continue

            if release.version in stamped:
                if not self.check_for_a_swap(
                    listing_id, authority, release, stamped[release.version], errors
                ):
                    settled = False
                continue

            date = parse_iso(release.release_date)
            if cutoff and date and date < cutoff:
                # Skipped, not settled: the ETag must not claim these are done,
                # or a later backfill dispatch takes the 304 path over them.
                outside_lookback += 1
                settled = False
                continue

            if self.stamp_budget <= 0:
                self.log("    the stamp budget is spent, the next tick carries on")
                return False

            try:
                self.stamp_one(listing_id, authored, authority, mirror_hosts, release)
            except HostError as error:
                self.log(f"    {release.version}: {error}")
                settled = False
            except StampError as error:
                errors.append(f"`{release.version}`: {error}")
            else:
                self.stamp_budget -= 1

        if outside_lookback:
            self.log(
                f"    {outside_lookback} release(s) outside the lookback window, "
                "left for a wider tick"
            )
        return settled

    def stamp_one(self, listing_id, authored, authority, mirror_hosts, release):
        archive, content_type = authority.download(release)
        facts = release.facts()
        facts["content_type"] = content_type

        mirrors = self.mirrors_for(mirror_hosts, release, archive)
        document = stamp(
            authored, facts, archive, self.game_versions, mirrors=mirrors, now=now()
        )
        path = self.folder(listing_id) / f"{document['version']}.json"
        self.write(path, serialize(document), f"Stamp {listing_id} {document['version']}")
        self.log(f"    stamped {document['version']} ({len(archive)} bytes)")
        self.stamped.append(f"{listing_id} {document['version']}")

    def check_for_a_swap(self, listing_id, authority, release, path, errors):
        """A stamped version is never overwritten, and a swap gets reported.

        The signal is the size and URL the release list already carries; a swap
        keeping the byte count needs every archive re-downloaded per tick for an
        answer `download.sha256` already gives the client. Two limits: SpaceDock
        reports no size, so only the URL comparison remains there, and a deleted
        asset reads as unchanged.
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

        digest = hashlib.sha256(archive).hexdigest().upper()
        seen.update({"size": release.size, "url": release.url, "checked": iso(now())})
        if digest == stamped_digest:
            seen["digest"] = None
            self.log(f"    {release.version}: the same bytes at a new URL")
            self.append_mirror(path, document, release.url)
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

    def mirrors_for(self, mirror_hosts, release, archive):
        """The non-authority hosts serving byte-identical bytes for this release.

        Shares the mirror budget with `mirror_pass`: verifying costs a full
        download, and a fresh-stamp burst must not multiply that unbounded. A
        mirror that did not fit the budget is appended by a later tick's pass.
        """
        digest = hashlib.sha256(archive).hexdigest().upper()
        found = []
        for host in mirror_hosts:
            candidate = self.mirror_release(host, release.version)
            if candidate is None:
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
        """The mirror's URL when its bytes are identical, else None."""
        try:
            archive, _ = host.download(release)
        except (HostError, StampError) as error:
            self.log(f"    {host.key}: {error}")
            return None
        if hashlib.sha256(archive).hexdigest().upper() != digest:
            self.log(f"    {host.key} serves different bytes for {release.version}")
            return None
        return release.url

    def mirror_pass(self, listing_id, mirror_hosts, errors):
        """Append a mirror that appeared after a release was stamped.

        The one field the watcher may append to after publish, watcher-only and
        append-only. Verifying one costs a full download, so a tick works
        through the least recently checked candidates within a budget, and the
        rest wait for the next tick.
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
                if candidate is None or candidate.url in known:
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
        summary = [
            "## Watcher",
            "",
            f"- stamped: {len(self.stamped)}",
            f"- mirrors appended: {len(self.mirrored)}",
            f"- listings with an error: {len(set(self.failed))}",
            f"- host requests: {self.http.requests}",
        ]
        if self.stamped:
            summary += ["", "### Stamped", ""] + [f"- `{name}`" for name in self.stamped]
        if self.mirrored:
            summary += ["", "### Mirrors", ""] + [f"- `{name}`" for name in self.mirrored]
        if self.failed:
            summary += ["", "### Reported", ""] + [
                f"- `{name}`" for name in sorted(set(self.failed))
            ]

        print("\n".join(summary[2:6]))
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
        "--stamp-budget", type=int, default=20,
        help="how many releases one tick stamps at most; the next tick carries on",
    )
    parser.add_argument(
        "--mirror-budget", type=int, default=4,
        help="how many mirror candidates one tick verifies at most",
    )
    parser.add_argument(
        "--unreachable-ticks", type=int, default=6,
        help="consecutive failed ticks before a host being down reaches the author",
    )
    parser.add_argument("--issue-label", default="watcher")
    parser.add_argument("--steward-team", default="@KSAModding/content-manager-stewards")
    parser.add_argument("--sweep-workflow", default="checks.yml")
    parser.add_argument("--sweep-ref", default="main")
    parser.add_argument("--verdict-check", default="validate")
    parser.add_argument("--sweep-limit", type=int, default=30)
    parser.add_argument("--sweep-attempts", type=int, default=3)
    parser.add_argument("--sweep-cooldown", type=int, default=30, help="minutes")
    parser.add_argument(
        "--sweep-refusal-hours", type=int, default=24,
        help="hours before a dispatch the workflow refused is tried again",
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
