#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One tick of the watcher (RFC 0033).

Scan every authored listing's authority host, stamp every release that has no
file under `releases/<id>/` yet, commit it, keep one error issue per listing
current on the authored repository, and sweep that repository's open pull
requests.

The properties this implementation keeps, and where:

    state is the repository            `stamped()` reads what is stamped from
                                       disk. There is no queue, so a cancelled
                                       or dropped tick costs latency, not data.

    every tick is idempotent           the scan rule is set membership, so a
                                       re-run stamps nothing twice.

    a version is stamped exactly once  `check_for_a_swap()` never overwrites a
                                       stamped file, and reports a tag that
                                       reappeared with different bytes.

    one open issue per listing         `Issues` edits the listing's issue
                                       instead of opening a new one per tick.

    the ETag store is derived cache    `Cache` is written outside the
                                       repository, and an ETag is only stored
                                       once every release behind it is stamped,
                                       so losing the cache costs one expensive
                                       tick and a stale cache cannot hide a
                                       release.

    mirrors are append-only            `mirror_pass()` appends a verified
                                       byte-identical URL and touches nothing
                                       else.

    release files and nothing else     every write goes through `write()`,
                                       which refuses a path outside releases/.

The per-release derivation is tools/stamp_release.py, the same code the release
pull request checks re-derive with. The hosts are tools/hosts.py.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import hosts
from hosts import HostError
from stamp_release import StampError, serialize, stamp

GITHUB_API = "https://api.github.com"

CACHE_VERSION = 1

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
    except ValueError:
        return None


class Cache:
    """Derived cache, never state.

    It holds the per-listing ETags, the consecutive-failure counts, the issue
    numbers, and what the mirror and sweep passes already tried. Every entry is
    rebuildable from the repository and the hosts, so losing the whole file
    costs one expensive tick and nothing else.
    """

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data = {"version": CACHE_VERSION}
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                loaded = {}
            if loaded.get("version") == CACHE_VERSION:
                self.data = loaded
        for section in ("hosts", "listings", "mirrors", "swaps", "sweep"):
            self.data.setdefault(section, {})

    def section(self, name, key):
        return self.data[name].setdefault(key, {})

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.data, handle, indent=1, sort_keys=True)
            handle.write("\n")


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

    def send(self, method, path, payload):
        url = f"{GITHUB_API}/repos/{self.repository}{path}"
        if self.dry_run or not self.http.token:
            self.log(f"    would {method} {path} {json.dumps(payload)[:200]}")
            return None
        body = json.dumps(payload).encode("utf-8")
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
        with urllib.request.urlopen(request, timeout=self.http.timeout) as answer:
            raw = answer.read()
        return json.loads(raw) if raw else None


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

    def _all_open(self, labelled):
        """The open issues of the authored repository, listed once per tick.

        The label narrows the list to the watcher's own issues, and the
        unlabelled list is the fallback for the tick that opened an issue before
        the label existed.
        """
        if labelled not in self._open:
            query = {"labels": self.label} if labelled else {}
            try:
                issues = self.api.get("/issues", state="open", per_page=100, **query) or []
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"  could not list issues: {error}")
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
        """Keep the listing's issue current with `errors`."""
        signature = hashlib.sha256("\n".join(sorted(errors)).encode()).hexdigest()[:16]
        body = self._body(listing_id, errors, signature)
        title = f"{listing_id}: the watcher could not stamp a release"
        issue = self.find(listing_id, cache)

        if issue is None:
            try:
                created = self.api.send(
                    "POST", "/issues", {"title": title, "body": body, "labels": [self.label]}
                )
            except urllib.error.HTTPError as error:
                # A label the repository does not define is not worth losing the
                # report over; the marker in the body is what the watcher finds
                # the issue by anyway.
                self.log(f"  the '{self.label}' label was refused (HTTP {error.code})")
                created = self.api.send("POST", "/issues", {"title": title, "body": body})
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
        """Close the listing's issue, because the tick evaluated it cleanly."""
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

    GitHub drops event-driven triggers with nothing to retry them, which the
    2026-08-06 incident showed, so every tick re-dispatches the validation of
    an open pull request whose head commit has no check suite or whose latest
    run ended in could-not-evaluate, and pings a steward for a run sitting in
    the waiting-for-approval state, which a dispatch cannot release.
    """

    def __init__(self, api, cache, options, log=print):
        self.api = api
        self.cache = cache
        self.options = options
        self.log = log

    def run(self):
        try:
            pulls = self.api.get("/pulls", state="open", per_page=self.options.sweep_limit)
        except urllib.error.HTTPError as error:
            self.log(f"  could not list pull requests: HTTP {error.code}")
            return
        except HostError as error:
            self.log(f"  could not list pull requests: {error}")
            return

        for pull in pulls or []:
            try:
                self._one(pull)
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"  #{pull['number']}: {error}")

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
        if attempts >= self.options.sweep_attempts:
            return
        if last and now() - last < timedelta(minutes=self.options.sweep_cooldown):
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
            # authored repository's business, and never fails a tick.
            self.log(
                f"  #{number}: {self.options.sweep_workflow} did not accept the dispatch "
                f"(HTTP {error.code}); the sweep needs it to take a pull_request input"
            )
            state["last"] = iso(now())
            state["attempts"] = self.options.sweep_attempts
            return
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
        self.cache = Cache(options.cache)
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

    def write(self, path, text, message):
        """Write one release file and commit it. Refuses anything else.

        The bypass on the default branch is scoped to the App the watcher runs
        as, so the watcher's own code enforces the same limit the ruleset
        cannot: release files and nothing else.
        """
        path = Path(path)
        if self.releases_root not in path.parents:
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
        """The ids the index has delisted.

        A delisted listing is out of the snapshot, so stamping further releases
        for it would be the watcher arguing with a steward.
        """
        path = self.authored_root / "index-status.toml"
        if not path.is_file():
            return set()
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        return {
            (entry.get("id") or "").lower()
            for entry in document.get("entries") or []
            if entry.get("state") == "delisted"
        }

    def tick(self):
        for path in self.listings():
            with path.open("rb") as handle:
                authored = tomllib.load(handle)
            listing_id = (authored.get("id") or path.stem).strip()
            self.log(f"{listing_id}:")
            try:
                self.one_listing(listing_id, authored)
            except Exception as error:  # noqa: BLE001 - one listing never fails the tick
                self.log(f"  unexpected: {error!r}")
                self.failed.append(listing_id)

        if not self.options.no_sweep:
            self.log(f"sweeping {self.options.authored_repo}:")
            Sweep(self.api, self.cache, self.options, self.log).run()

        self.cache.save()
        self.summarize()
        return 0

    def one_listing(self, listing_id, authored):
        state = self.cache.section("listings", listing_id)
        errors = []

        try:
            authority, mirrors = hosts.build(
                authored.get("releases"), self.http, listing_id
            )
        except StampError as error:
            self.issues.report(listing_id, [str(error)], self.cache)
            return
        if authority is None:
            self.log("  no [releases] section, so releases enter by pull request")
            return

        host_state = self.cache.section("hosts", authority.key)
        try:
            releases, etag = authority.releases(host_state.get("etag"))
        except HostError as error:
            self.unreachable(listing_id, state, str(error))
            return
        except StampError as error:
            state["unreachable"] = 0
            self.issues.report(listing_id, [str(error)], self.cache)
            return

        state["unreachable"] = 0
        settled = True

        if releases is None:
            self.log("  unchanged since the last tick")
        else:
            self.log(f"  {len(releases)} release(s) on {authority.key}")
            settled = self.stamp_pass(
                listing_id, authored, authority, mirrors, releases, errors
            )

        self.mirror_pass(listing_id, mirrors)

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

        The cheap signal is the size and the URL the host now serves, which the
        release list already carries. A swap that keeps the byte count is not
        detectable without downloading every stamped archive on every tick, and
        it is what `download.sha256` protects a client from anyway.
        """
        document = json.loads(path.read_text(encoding="utf-8"))
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

        digest = hashlib.sha256(archive).hexdigest().upper()
        seen.update({"size": release.size, "url": release.url, "checked": iso(now())})
        if digest == stamped_digest:
            seen["digest"] = None
            self.log(f"    {release.version}: the same bytes at a new URL, nothing to do")
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

    def mirrors_for(self, mirror_hosts, release, archive):
        """The non-authority hosts serving byte-identical bytes for this release."""
        digest = hashlib.sha256(archive).hexdigest().upper()
        found = []
        for host in mirror_hosts:
            candidate = self.mirror_release(host, release.version)
            if candidate is None:
                continue
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

    def mirror_pass(self, listing_id, mirror_hosts):
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
            document = json.loads(path.read_text(encoding="utf-8"))
            known = set((document.get("download") or {}).get("mirrors") or [])
            if len(known) >= len(mirror_hosts):
                continue
            key = f"{listing_id}/{version}"
            checked = parse_iso(self.cache.section("mirrors", key).get("checked"))
            candidates.append((checked or datetime.min.replace(tzinfo=timezone.utc), version, path))

        for _, version, path in sorted(candidates, key=lambda entry: entry[0]):
            if self.mirror_budget <= 0:
                return
            document = json.loads(path.read_text(encoding="utf-8"))
            download = document["download"]
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

        It only becomes the author's business once it stays that way across
        consecutive ticks, which is what keeps a five minute outage out of
        everybody's notifications.
        """
        count = state.get("unreachable", 0) + 1
        state["unreachable"] = count
        self.log(f"  could not be evaluated ({count} tick(s) in a row): {message}")
        if count == self.options.unreachable_ticks:
            self.issues.report(
                listing_id,
                [
                    f"the authority host has not been reachable for {count} consecutive "
                    f"ticks: {message}"
                ],
                self.cache,
            )

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
