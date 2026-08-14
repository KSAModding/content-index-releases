#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the tick's own logic: no token, no network, no git.

The API is a stub that records what the watcher would send, which is what makes
"one open issue per listing" and the sweep's decisions testable at all.
"""

import hashlib
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from hosts import HostRelease
from watch import Cache, Issues, Sweep, Watcher, is_history, parse_arguments

GAME_VERSIONS = {"spec_version": 1, "versions": ["2026.8.3.5117"]}


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "boom", {}, None)


class StubApi:
    """Answers reads from a routing table and records every write."""

    repository = "KSAModding/content-index"

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.sent = []

    def get(self, path, **query):
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                return answer(query) if callable(answer) else answer
        return None

    def get_paged(self, path, key=None, max_pages=10, **query):
        answer = self.get(path, **query)
        if key:
            return list((answer or {}).get(key) or [])
        return list(answer or [])

    def send(self, method, path, payload):
        self.sent.append((method, path, payload))
        if method == "POST" and path == "/issues":
            return {"number": 42}
        return {}

    def writes(self, method=None, path=None, contains=None):
        return [
            entry
            for entry in self.sent
            if (method is None or entry[0] == method)
            and (path is None or entry[1] == path)
            and (contains is None or contains in entry[1])
        ]


class RecorderIssues:
    """Records what the watcher would report, for tick-level tests."""

    def __init__(self, listings=None, degraded=False):
        self.reported = []
        self.resolved = []
        self.listings = listings or {}
        self.degraded = degraded

    def report(self, listing_id, errors, cache):
        self.reported.append((listing_id, list(errors)))

    def resolve(self, listing_id, cache, reason=None):
        self.resolved.append(listing_id)

    def resolve_if(self, listing_id, signature, cache):
        self.resolved.append((listing_id, signature))

    def open_listings(self):
        return self.listings


def cache():
    return Cache(None)


class OneIssuePerListing(unittest.TestCase):
    def test_the_first_failure_opens_one_issue(self):
        api = StubApi({"/issues": []})
        Issues(api, "watcher", log=lambda _: None).report(
            "AutoStage", ["`0.4.3`: the archive is not a readable zip"], cache()
        )
        opened = api.writes("POST", path="/issues")
        self.assertEqual(len(opened), 1)
        self.assertIn("watcher:listing=AutoStage", opened[0][2]["body"])
        self.assertIn("not a readable zip", opened[0][2]["body"])

    def test_the_same_failure_again_edits_and_does_not_comment(self):
        errors = ["`0.4.3`: the archive is not a readable zip"]
        store = cache()
        api = StubApi({"/issues": []})
        issues = Issues(api, "watcher", log=lambda _: None)
        issues.report("AutoStage", errors, store)

        body = api.writes("POST", path="/issues")[0][2]["body"]
        second = StubApi({"/issues/42": {"number": 42, "state": "open", "body": body},
                          "/issues": []})
        Issues(second, "watcher", log=lambda _: None).report("AutoStage", errors, store)

        self.assertEqual(len(second.writes("POST", path="/issues")), 0)
        self.assertEqual(len(second.writes("PATCH", path="/issues/42")), 1)
        self.assertEqual(len(second.writes("POST", path="/issues/42/comments")), 0)

    def test_a_different_failure_comments_on_the_same_issue(self):
        store = cache()
        api = StubApi({"/issues": []})
        issues = Issues(api, "watcher", log=lambda _: None)
        issues.report("AutoStage", ["one thing"], store)
        body = api.writes("POST", path="/issues")[0][2]["body"]

        second = StubApi({"/issues/42": {"number": 42, "state": "open", "body": body},
                          "/issues": []})
        Issues(second, "watcher", log=lambda _: None).report(
            "AutoStage", ["something else entirely"], store
        )
        self.assertEqual(len(second.writes("POST", path="/issues")), 0)
        self.assertEqual(len(second.writes("POST", path="/issues/42/comments")), 1)

    def test_an_issue_is_found_by_its_marker_when_the_cache_is_gone(self):
        body = "<!-- watcher:listing=AutoStage -->\nold"
        api = StubApi({"/issues": [{"number": 7, "state": "open", "body": body}]})
        Issues(api, "watcher", log=lambda _: None).report("AutoStage", ["x"], cache())
        self.assertEqual(len(api.writes("POST", path="/issues")), 0)
        self.assertEqual(len(api.writes("PATCH", path="/issues/7")), 1)

    def test_a_clean_tick_closes_the_issue(self):
        body = "<!-- watcher:listing=AutoStage -->\nold"
        api = StubApi({"/issues": [{"number": 7, "state": "open", "body": body}]})
        Issues(api, "watcher", log=lambda _: None).resolve("AutoStage", cache())
        self.assertEqual(api.writes("PATCH", path="/issues/7")[0][2], {"state": "closed"})

    def test_a_pull_request_is_not_an_issue(self):
        api = StubApi(
            {
                "/issues": [
                    {
                        "number": 7,
                        "state": "open",
                        "body": "<!-- watcher:listing=AutoStage -->",
                        "pull_request": {},
                    }
                ]
            }
        )
        Issues(api, "watcher", log=lambda _: None).report("AutoStage", ["x"], cache())
        self.assertEqual(len(api.writes("POST", path="/issues")), 1)


def sweep_api(runs=(), suites=0, statuses=(), check_runs=(), comments=()):
    return StubApi(
        {
            "/pulls": [{"number": 3, "head": {"sha": "abc"}}],
            "/actions/runs": {"workflow_runs": list(runs)},
            "/commits/abc/check-suites": {"total_count": suites},
            "/commits/abc/status": {"statuses": list(statuses)},
            "/commits/abc/check-runs": {"check_runs": list(check_runs)},
            "/issues/3/comments": list(comments),
        }
    )


class TheSweep(unittest.TestCase):
    def sweep(self, api, store=None):
        options = parse_arguments([])
        Sweep(api, store or cache(), options, log=lambda _: None).run()
        return api

    def test_a_head_commit_with_no_check_suite_is_re_dispatched(self):
        api = self.sweep(sweep_api(suites=0))
        dispatched = api.writes("POST", contains="/dispatches")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][2]["inputs"], {"pull_request": "3"})

    def test_a_run_that_could_not_evaluate_is_re_dispatched(self):
        api = self.sweep(
            sweep_api(
                suites=1,
                runs=[{"status": "completed", "conclusion": "success"}],
                statuses=[{"context": "validate", "state": "error"}],
            )
        )
        self.assertEqual(len(api.writes("POST", contains="/dispatches")), 1)

    def test_a_pass_is_left_alone(self):
        api = self.sweep(
            sweep_api(
                suites=1,
                runs=[{"status": "completed", "conclusion": "success"}],
                statuses=[{"context": "validate", "state": "success"}],
            )
        )
        self.assertEqual(api.sent, [])

    def test_a_reject_is_a_verdict_and_waits_for_the_author(self):
        api = self.sweep(
            sweep_api(suites=1, statuses=[{"context": "validate", "state": "failure"}])
        )
        self.assertEqual(api.sent, [])

    def test_a_check_run_conclusion_counts_as_the_verdict(self):
        api = self.sweep(
            sweep_api(suites=1, check_runs=[{"name": "validate", "conclusion": "neutral"}])
        )
        self.assertEqual(len(api.writes("POST", contains="/dispatches")), 1)

    def test_a_run_still_going_is_left_alone(self):
        api = self.sweep(sweep_api(suites=1, runs=[{"status": "in_progress"}]))
        self.assertEqual(api.sent, [])

    def test_waiting_for_approval_pings_a_steward_instead(self):
        api = self.sweep(sweep_api(runs=[{"status": "waiting"}]))
        self.assertEqual(len(api.writes("POST", contains="/dispatches")), 0)
        comments = api.writes("POST", path="/issues/3/comments")
        self.assertEqual(len(comments), 1)
        self.assertIn("stewards", comments[0][2]["body"])

    def test_a_steward_is_pinged_once_per_head_commit(self):
        store = cache()
        self.sweep(sweep_api(runs=[{"status": "waiting"}]), store)
        again = self.sweep(sweep_api(runs=[{"status": "waiting"}]), store)
        self.assertEqual(again.sent, [])

    def test_an_existing_ping_is_not_repeated_after_the_cache_is_gone(self):
        api = self.sweep(
            sweep_api(
                runs=[{"status": "waiting"}],
                comments=[{"body": "<!-- watcher:waiting=abc -->\nalready said"}],
            )
        )
        self.assertEqual(api.sent, [])

    def test_a_dispatch_is_not_repeated_within_the_cooldown(self):
        store = cache()
        self.sweep(sweep_api(suites=0), store)
        again = self.sweep(sweep_api(suites=0), store)
        self.assertEqual(again.sent, [])

    def test_dispatches_give_up_after_the_attempt_limit(self):
        store = cache()
        store.section("sweep", "abc").update({"attempts": 3})
        api = self.sweep(sweep_api(suites=0), store)
        self.assertEqual(api.sent, [])


class WatcherCase(unittest.TestCase):
    def watcher(self, folder, argv=(), versions=None):
        (folder / "game-versions.json").write_text(
            json.dumps({"spec_version": 1, "versions": versions or GAME_VERSIONS["versions"]})
        )
        (folder / ".authored" / "listings").mkdir(parents=True, exist_ok=True)
        options = parse_arguments(
            [
                "--authored", str(folder / ".authored"),
                "--releases", str(folder / "releases"),
                "--game-versions", str(folder / "game-versions.json"),
                "--cache", str(folder / "cache.json"),
                *argv,
            ]
        )
        # Structural, not conventional: no test reaches the network even when
        # the ambient environment carries a token.
        options.token = None
        watcher = Watcher(options)
        watcher.log = lambda message: None
        return watcher


class TheRepositoryIsTheState(WatcherCase):

    def test_stamped_versions_are_read_from_the_repository(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            releases = folder / "releases" / "AutoStage"
            releases.mkdir(parents=True)
            (releases / "0.4.3.json").write_text("{}")
            (releases / "0.4.4.json").write_text("{}")
            watcher = self.watcher(folder)
            self.assertEqual(
                sorted(watcher.stamped_versions("AutoStage")), ["0.4.3", "0.4.4"]
            )
            self.assertEqual(watcher.stamped_versions("Unknown"), {})

    def test_the_watcher_writes_release_files_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit"])
            with self.assertRaises(RuntimeError):
                watcher.write(folder / "game-versions.json", "{}", "no")
            with self.assertRaises(RuntimeError):
                watcher.write(folder / ".authored" / "listings" / "X.toml", "", "no")
            watcher.write(folder / "releases" / "X" / "1.0.0.json", "{}\n", "yes")
            self.assertTrue((folder / "releases" / "X" / "1.0.0.json").is_file())

    def test_a_delisted_listing_is_left_alone(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder)
            listings = folder / ".authored" / "listings"
            for identifier in ("Kept", "Gone"):
                (listings / f"{identifier}.toml").write_text(f'id = "{identifier}"\n')
            (folder / ".authored" / "index-status.toml").write_text(
                '[[entries]]\nid = "Gone"\nstate = "delisted"\n'
                '[[entries]]\nid = "Kept"\nstate = "disputed"\n'
            )
            self.assertEqual([path.stem for path in watcher.listings()], ["Kept"])

    def test_a_single_listing_can_be_dispatched(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--listing", "Wanted"])
            listings = folder / ".authored" / "listings"
            for identifier in ("Wanted", "Other"):
                (listings / f"{identifier}.toml").write_text(f'id = "{identifier}"\n')
            self.assertEqual([path.stem for path in watcher.listings()], ["Wanted"])

    def test_a_path_that_escapes_releases_is_refused(self):
        # Path.parents is lexical, so the guard has to resolve: a `..` inside
        # the path keeps `releases` in the parents while the file lands outside.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit"])
            with self.assertRaises(RuntimeError):
                watcher.write(folder / "releases" / ".." / "evil.json", "{}", "no")
            with self.assertRaises(RuntimeError):
                watcher.write(folder / "releases" / "X" / ".." / ".." / "evil.json", "{}", "no")

    def test_an_unreadable_index_status_fails_closed(self):
        # Failing open would stamp releases a steward delisted.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder)
            (folder / ".authored" / "listings" / "A.toml").write_text('id = "A"\n')
            (folder / ".authored" / "index-status.toml").write_text("= broken\n")
            self.assertEqual(watcher.listings(), [])


class AnIssueOutlivesNothing(WatcherCase):
    """An issue about a listing the watcher stopped scanning has to close."""

    def tick(self, folder, documents, argv=(), **kwargs):
        watcher = self.watcher(folder, ["--no-sweep", "--no-commit", *argv])
        watcher.issues = RecorderIssues(**kwargs)
        for identifier in documents:
            (folder / ".authored" / "listings" / f"{identifier}.toml").write_text(
                f'id = "{identifier}"\n'
            )
        watcher.tick()
        return watcher

    def test_a_delisted_listing_stops_holding_its_issue_open(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / ".authored").mkdir(parents=True, exist_ok=True)
            (folder / ".authored" / "index-status.toml").write_text(
                '[[entries]]\nid = "Gone"\nstate = "delisted"\n'
            )
            watcher = self.tick(
                folder, ("Kept", "Gone"),
                listings={"Gone": {"number": 7}, "Kept": {"number": 8}},
            )
            # Gone is never scanned, so only this pass can close it. Kept names
            # no host either, which the case below covers.
            self.assertIn("Gone", watcher.issues.resolved)

    def test_a_listing_that_drops_its_releases_section_closes_its_issue(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.tick(folder, ("Kept",), listings={})
            # Kept has no [releases] section, so nothing about a host is left
            # to report and its issue closes.
            self.assertEqual(watcher.issues.resolved, ["Kept"])

    def test_a_narrow_dispatch_closes_nothing_it_did_not_look_at(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.tick(
                folder, ("Wanted", "Other"), argv=("--listing", "Wanted"),
                listings={"Other": {"number": 7}},
            )
            self.assertNotIn("Other", watcher.issues.resolved)

    def test_a_failed_issue_read_closes_nothing(self):
        # Every id looks orphaned when the list could not be read.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.tick(
                folder, ("Kept",), listings={"Gone": {"number": 7}}, degraded=True
            )
            self.assertNotIn("Gone", watcher.issues.resolved)


class TheTickSurvivesOneListing(WatcherCase):
    def test_a_malformed_listing_does_not_abort_the_tick(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-sweep", "--no-commit"])
            watcher.issues = RecorderIssues()
            listings = folder / ".authored" / "listings"
            (listings / "Bad.toml").write_text("= broken\n")
            (listings / "Good.toml").write_text('id = "Good"\n')

            watcher.tick()

            reported = [listing for listing, _ in watcher.issues.reported]
            self.assertIn("Bad", reported)
            # The other listing was still scanned, and the cache still saved.
            self.assertNotIn("Good", watcher.failed)
            self.assertTrue((folder / "cache.json").is_file())

    def test_an_invalid_id_is_reported_and_never_a_path(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-sweep", "--no-commit"])
            watcher.issues = RecorderIssues()
            (folder / ".authored" / "listings" / "Evil.toml").write_text('id = "../Evil"\n')

            watcher.tick()

            self.assertEqual(len(watcher.issues.reported), 1)
            self.assertIn("id rules", watcher.issues.reported[0][1][0])

    def test_a_stem_that_does_not_match_the_id_is_reported(self):
        # The delisting and --listing filters match the stem, so a mismatch
        # would let a delisted id keep being stamped under another file name.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-sweep", "--no-commit"])
            watcher.issues = RecorderIssues()
            (folder / ".authored" / "listings" / "A.toml").write_text('id = "B"\n')

            watcher.tick()

            self.assertEqual(len(watcher.issues.reported), 1)
            self.assertIn("must match", watcher.issues.reported[0][1][0])

    def test_a_dry_run_leaves_no_cache_behind(self):
        # An ETag a dry run stored would make the next real tick take the 304
        # path over releases it never stamped.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-sweep", "--dry-run"])
            watcher.tick()
            self.assertFalse((folder / "cache.json").exists())


class TheMonthPass(WatcherCase):
    def release_file(self, folder, listing_id, version, document):
        path = folder / "releases" / listing_id
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{version}.json").write_text(json.dumps(document, indent=2) + "\n")
        return path / f"{version}.json"

    def test_a_completed_month_is_resolved_onto_open_stamps(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(
                folder, ["--no-commit"],
                versions=["2020.1.1.100", "2020.1.2.150", "2020.2.1.200"],
            )
            path = self.release_file(folder, "M", "1.0.0", {
                "id": "M", "version": "1.0.0",
                "game_min": "2020.1.1.100", "game_min_revision": 100,
                "download": {"url": "u"},
            })
            errors = []
            watcher.month_pass(
                "M", {"id": "M", "compatibility": {"game_min": "2020.1.1.100", "game_max": "2020.1"}}, errors
            )
            document = json.loads(path.read_text())
            self.assertEqual(errors, [])
            self.assertEqual(document["game_max"], "2020.1.2.150")
            self.assertEqual(document["game_max_revision"], 150)
            # Inserted where a fresh stamp puts it, not appended at the end.
            self.assertEqual(
                list(document),
                ["id", "version", "game_min", "game_min_revision",
                 "game_max", "game_max_revision", "download"],
            )

    def test_the_pass_is_add_only(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(
                folder, ["--no-commit"], versions=["2020.1.1.100", "2020.1.2.150"]
            )
            path = self.release_file(folder, "M", "1.0.0", {
                "id": "M", "version": "1.0.0",
                "game_min": "2020.1.1.100", "game_min_revision": 100,
                "game_max": "2020.1.1.100", "game_max_revision": 100,
            })
            before = path.read_text()
            watcher.month_pass(
                "M", {"id": "M", "compatibility": {"game_min": "2020.1.1.100", "game_max": "2020.1"}}, []
            )
            self.assertEqual(path.read_text(), before)

    def test_a_running_month_stays_open(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit"], versions=["2020.1.1.100"])
            path = self.release_file(folder, "M", "1.0.0", {
                "id": "M", "version": "1.0.0",
                "game_min": "2020.1.1.100", "game_min_revision": 100,
            })
            before = path.read_text()
            future = f"{datetime.now(timezone.utc).year + 1}.1"
            errors = []
            watcher.month_pass(
                "M", {"id": "M", "compatibility": {"game_min": "2020.1.1.100", "game_max": future}}, errors
            )
            self.assertEqual(path.read_text(), before)
            self.assertEqual(errors, [])

    def test_a_month_below_the_stamped_min_is_reported_not_applied(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(
                folder, ["--no-commit"], versions=["2020.1.1.100", "2020.1.2.150"]
            )
            path = self.release_file(folder, "M", "2.0.0", {
                "id": "M", "version": "2.0.0",
                "game_min": "2020.3.1.999", "game_min_revision": 999,
            })
            before = path.read_text()
            errors = []
            watcher.month_pass(
                "M", {"id": "M", "compatibility": {"game_min": "2020.1.1.100", "game_max": "2020.1"}}, errors
            )
            self.assertEqual(path.read_text(), before)
            self.assertEqual(len(errors), 1)


class FakeAuthority:
    """Serves fixed bytes, and counts how often a download happened."""

    def __init__(self, payload=b"bytes"):
        self.payload = payload
        self.downloads = 0

    def download(self, release):
        self.downloads += 1
        return self.payload, "application/zip"


def release(version, url, size=None, date="2020-01-01T00:00:00Z"):
    return HostRelease(
        host="github", tag=f"v{version}", version=version,
        release_date=date, url=url, size=size,
    )


class SwapsAndMirrors(WatcherCase):
    def test_the_same_bytes_at_a_new_url_become_a_mirror(self):
        # download.url is immutable, so the proven-identical new address lands
        # in download.mirrors instead of rewriting anything.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit"])
            digest = hashlib.sha256(b"bytes").hexdigest().upper()
            path = folder / "releases" / "S"
            path.mkdir(parents=True)
            file = path / "1.0.0.json"
            file.write_text(json.dumps({
                "id": "S", "version": "1.0.0",
                "download": {"url": "old", "sha256": digest, "size": 5},
            }))
            authority = FakeAuthority()
            errors = []

            settled = watcher.check_for_a_swap(
                "S", authority, release("1.0.0", "new", size=999), file, errors
            )

            self.assertTrue(settled)
            self.assertEqual(errors, [])
            self.assertEqual(authority.downloads, 1)
            document = json.loads(file.read_text())
            self.assertEqual(document["download"]["url"], "old")
            self.assertEqual(document["download"]["mirrors"], ["new"])

            # The verdict is cached: the next tick neither downloads again nor
            # appends a duplicate.
            settled = watcher.check_for_a_swap(
                "S", authority, release("1.0.0", "new", size=999), file, errors
            )
            self.assertTrue(settled)
            self.assertEqual(authority.downloads, 1)
            self.assertEqual(json.loads(file.read_text())["download"]["mirrors"], ["new"])

    def test_different_bytes_are_still_a_reported_swap(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit"])
            path = folder / "releases" / "S"
            path.mkdir(parents=True)
            file = path / "1.0.0.json"
            file.write_text(json.dumps({
                "id": "S", "version": "1.0.0",
                "download": {"url": "old", "sha256": "AA", "size": 5},
            }))
            errors = []
            watcher.check_for_a_swap(
                "S", FakeAuthority(b"other"), release("1.0.0", "old", size=999), file, errors
            )
            self.assertEqual(len(errors), 1)
            self.assertIn("stamped exactly", errors[0])

    def test_a_corrupt_stamped_file_is_reported_not_thrown(self):
        # The repository is the state, so a stamped file that does not parse
        # has to reach a human instead of the broad catch.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit"])
            path = folder / "releases" / "S"
            path.mkdir(parents=True)
            file = path / "1.0.0.json"
            file.write_text("not json at all")
            errors = []
            settled = watcher.check_for_a_swap(
                "S", FakeAuthority(), release("1.0.0", "old"), file, errors
            )
            self.assertTrue(settled)
            self.assertEqual(len(errors), 1)
            self.assertIn("releases/S/1.0.0.json", errors[0])

    def test_mirrors_for_respects_the_budget(self):
        # A fresh-stamp burst must not multiply mirror downloads unbounded.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--no-commit", "--mirror-budget", "0"])

            class CountingHost:
                key = "spacedock:1"

                def __init__(self):
                    self.downloads = 0

                def releases(self, etag=None):
                    return [release("1.0.0", "mirror-url")], None

                def download(self, entry):
                    self.downloads += 1
                    return b"bytes", "application/zip"

            host = CountingHost()
            found = watcher.mirrors_for([host], release("1.0.0", "x"), b"bytes")
            self.assertEqual(found, [])
            self.assertEqual(host.downloads, 0)


def moment(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class WhatCountsAsHistory(unittest.TestCase):
    """The selection rule on its own."""

    OLD = moment("2025-11-01T00:00:00Z")
    MID = moment("2026-03-01T00:00:00Z")
    NEW = moment("2026-08-01T00:00:00Z")

    def test_the_first_tick_keeps_only_the_newest(self):
        self.assertTrue(is_history(self.OLD, None, self.NEW))
        self.assertFalse(is_history(self.NEW, None, self.NEW))

    def test_a_frontier_keeps_everything_after_it(self):
        self.assertTrue(is_history(self.OLD, self.MID, self.NEW))
        self.assertFalse(is_history(self.NEW, self.MID, self.NEW))

    def test_a_shared_timestamp_is_not_history(self):
        self.assertFalse(is_history(self.MID, self.MID, self.NEW))
        self.assertFalse(is_history(self.NEW, None, self.NEW))

    def test_a_release_with_no_date_is_never_history(self):
        self.assertFalse(is_history(None, self.NEW, self.NEW))
        self.assertFalse(is_history(None, None, self.NEW))


class TheStampedFrontier(WatcherCase):
    """A back catalogue is not stamped with facts that were not true then."""

    def setup(self, folder, stamped=(), argv=()):
        """A watcher whose stamp_one only records, so this tests selection."""
        for version, date, url in stamped:
            path = folder / "releases" / "M"
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{version}.json").write_text(
                json.dumps(
                    {
                        "id": "M",
                        "version": version,
                        "release_date": date,
                        "download": {"url": url},
                    }
                )
            )
        watcher = self.watcher(folder, ["--no-commit", *argv])
        picked = []
        watcher.stamp_one = lambda *arguments: picked.append(arguments[-1].version)
        return watcher, picked

    def test_the_first_tick_stamps_the_newest_release_only(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(folder)
            errors = []
            settled = watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("1.0.0", "http://a", date="2025-11-01T00:00:00Z"),
                    release("1.1.0", "http://b", date="2026-03-01T00:00:00Z"),
                    release("1.2.0", "http://c", date="2026-08-01T00:00:00Z"),
                ],
                errors,
            )
            self.assertEqual(picked, ["1.2.0"])
            self.assertEqual(errors, [])
            self.assertTrue(settled)

    def test_only_releases_after_the_frontier_are_stamped(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(
                folder, stamped=[("1.1.0", "2026-03-01T00:00:00Z", "http://b")]
            )
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("1.0.0", "http://a", date="2025-11-01T00:00:00Z"),
                    release("1.1.0", "http://b", date="2026-03-01T00:00:00Z"),
                    release("1.2.0", "http://c", date="2026-08-01T00:00:00Z"),
                ],
                [],
            )
            self.assertEqual(picked, ["1.2.0"])

    def test_every_release_after_the_frontier_is_stamped(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(
                folder, stamped=[("1.1.0", "2026-03-01T00:00:00Z", "http://b")]
            )
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("1.1.0", "http://b", date="2026-03-01T00:00:00Z"),
                    release("1.2.0", "http://c", date="2026-06-01T00:00:00Z"),
                    release("1.3.0", "http://d", date="2026-08-01T00:00:00Z"),
                ],
                [],
            )
            self.assertEqual(picked, ["1.2.0", "1.3.0"])

    def test_a_version_the_open_issue_names_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(folder)
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("1.0.0", "http://a", date="2026-01-01T00:00:00Z"),
                    release("1.1.0", "http://b", date="2026-02-01T00:00:00Z"),
                ],
                [],
                attempted={"1.0.0"},
            )
            self.assertEqual(picked, ["1.0.0", "1.1.0"])

    def test_a_frontier_that_could_not_be_read_stamps_nothing(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            path = folder / "releases" / "M"
            path.mkdir(parents=True)
            (path / "0.1.0.json").write_text(
                json.dumps({"id": "M", "version": "0.1.0", "release_date": "2025-01-01T00:00:00Z"})
            )
            (path / "0.7.2.json").write_text("{trunc")
            watcher, picked = self.setup(folder)
            errors = []
            settled = watcher.stamp_pass(
                "M", {}, None, [],
                [release("0.5.0", "http://e", date="2026-05-01T00:00:00Z")],
                errors,
            )
            self.assertEqual(picked, [])
            self.assertFalse(settled)
            self.assertTrue(errors)

    def test_a_stamp_without_a_release_date_is_reported(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            path = folder / "releases" / "M"
            path.mkdir(parents=True)
            (path / "0.1.0.json").write_text(json.dumps({"id": "M", "version": "0.1.0"}))
            watcher, _ = self.setup(folder)
            errors = []
            _, complete = watcher.stamped_frontier(watcher.stamped_versions("M"), errors)
            self.assertFalse(complete)
            self.assertIn("release_date", errors[0])

    def test_a_backfill_has_to_name_its_listing(self):
        with self.assertRaises(SystemExit):
            parse_arguments(["--backfill"])
        self.assertTrue(parse_arguments(["--backfill", "--listing", "M"]).backfill)

    def test_a_patch_for_an_older_line_is_still_stamped(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(
                folder, stamped=[("2.0.0", "2026-03-01T00:00:00Z", "http://b")]
            )
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("2.0.0", "http://b", date="2026-03-01T00:00:00Z"),
                    release("1.9.1", "http://c", date="2026-08-01T00:00:00Z"),
                ],
                [],
            )
            self.assertEqual(picked, ["1.9.1"])

    def test_an_old_unparseable_tag_is_not_reported(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(folder)
            errors = []
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    HostRelease(
                        host="github", tag="rc", version=None,
                        release_date="2025-12-26T00:00:00Z", url="http://rc",
                    ),
                    release("1.2.0", "http://c", date="2026-08-01T00:00:00Z"),
                ],
                errors,
            )
            self.assertEqual(picked, ["1.2.0"])
            self.assertEqual(errors, [])

    def test_a_new_unparseable_tag_is_still_reported(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, _ = self.setup(
                folder, stamped=[("1.0.0", "2026-01-01T00:00:00Z", "http://a")]
            )
            errors = []
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("1.0.0", "http://a", date="2026-01-01T00:00:00Z"),
                    HostRelease(
                        host="github", tag="latest", version=None,
                        release_date="2026-08-01T00:00:00Z", url="http://l",
                    ),
                ],
                errors,
            )
            self.assertEqual(len(errors), 1)
            self.assertIn("`latest`", errors[0])

    def test_backfill_stamps_the_history_too(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, picked = self.setup(
                folder,
                stamped=[("1.1.0", "2026-03-01T00:00:00Z", "http://b")],
                argv=["--backfill", "--listing", "M"],
            )
            watcher.stamp_pass(
                "M", {}, None, [],
                [
                    release("1.0.0", "http://a", date="2025-11-01T00:00:00Z"),
                    release("1.1.0", "http://b", date="2026-03-01T00:00:00Z"),
                    release("1.2.0", "http://c", date="2026-08-01T00:00:00Z"),
                ],
                [],
            )
            self.assertEqual(picked, ["1.0.0", "1.2.0"])

    def test_a_backfill_does_not_poll_with_the_stored_etag(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            state = {"etag": "W/\"abc\"", "authored": "d"}
            self.assertEqual(self.watcher(folder).poll_etag(state, "d"), "W/\"abc\"")
            self.assertIsNone(
                self.watcher(folder, ["--backfill", "--listing", "M"]).poll_etag(state, "d")
            )

    def test_an_edited_listing_drops_the_stored_etag(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder)
            state = {"etag": "W/\"abc\"", "authored": watcher.authored_digest({"id": "M"})}
            self.assertIsNotNone(
                watcher.poll_etag(state, watcher.authored_digest({"id": "M"}))
            )
            self.assertIsNone(
                watcher.poll_etag(state, watcher.authored_digest({"id": "M", "x": 1}))
            )

    def test_the_authored_digest_ignores_key_order(self):
        with tempfile.TemporaryDirectory() as name:
            watcher = self.watcher(Path(name))
            self.assertEqual(
                watcher.authored_digest({"a": 1, "b": 2}),
                watcher.authored_digest({"b": 2, "a": 1}),
            )

    def test_the_frontier_is_read_off_the_stamped_files(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher, _ = self.setup(
                folder,
                stamped=[
                    ("1.0.0", "2025-11-01T00:00:00Z", "http://a"),
                    ("1.2.0", "2026-08-01T00:00:00Z", "http://c"),
                ],
            )
            errors = []
            frontier, complete = watcher.stamped_frontier(
                watcher.stamped_versions("M"), errors
            )
            self.assertEqual(frontier, moment("2026-08-01T00:00:00Z"))
            self.assertTrue(complete)
            self.assertEqual(errors, [])

    def test_a_corrupt_stamp_is_reported_and_sets_no_frontier(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            path = folder / "releases" / "M"
            path.mkdir(parents=True)
            (path / "1.0.0.json").write_text("{not json")
            watcher = self.watcher(folder, ["--no-commit"])
            errors = []
            frontier, complete = watcher.stamped_frontier(
                watcher.stamped_versions("M"), errors
            )
            self.assertIsNone(frontier)
            self.assertFalse(complete)
            self.assertEqual(len(errors), 1)
            self.assertIn("1.0.0.json", errors[0])


class TheLookback(WatcherCase):
    def test_skipped_releases_leave_the_tick_unsettled(self):
        # A settled tick stores the ETag, and a later backfill dispatch with a
        # wider window would then take the 304 path over the skipped releases.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder, ["--lookback-days", "5", "--no-commit"])
            errors = []
            settled = watcher.stamp_pass(
                "L", {}, None, [], [release("1.0.0", "http://x")], errors
            )
            self.assertFalse(settled)
            self.assertEqual(errors, [])


class Unreachable(WatcherCase):
    def test_the_threshold_reports_and_keeps_reporting(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder)
            watcher.issues = RecorderIssues()
            state = {}
            for _ in range(5):
                watcher.unreachable("U", state, "down")
            self.assertEqual(watcher.issues.reported, [])
            watcher.unreachable("U", state, "down")
            self.assertEqual(len(watcher.issues.reported), 1)
            # Past the threshold the issue is kept current, >= not ==.
            watcher.unreachable("U", state, "down")
            self.assertEqual(len(watcher.issues.reported), 2)
            self.assertIn("unreachable_signature", state)

    def test_a_recovery_resolves_by_signature(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            watcher = self.watcher(folder)
            watcher.issues = RecorderIssues()
            state = {"unreachable": 7, "unreachable_signature": "abc123"}
            watcher.recover("U", state)
            self.assertEqual(watcher.issues.resolved, [("U", "abc123")])
            self.assertEqual(state["unreachable"], 0)
            self.assertNotIn("unreachable_signature", state)


class LabelRefusingApi(StubApi):
    def __init__(self, code):
        super().__init__({"/issues": []})
        self.code = code

    def send(self, method, path, payload):
        if method == "POST" and path == "/issues" and "labels" in payload:
            raise http_error(self.code)
        return super().send(method, path, payload)


class IssueRobustness(unittest.TestCase):
    def test_only_a_422_falls_back_to_a_label_free_create(self):
        api = LabelRefusingApi(422)
        Issues(api, "watcher", log=lambda _: None).report("A", ["x"], Cache(None))
        created = api.writes("POST", path="/issues")
        self.assertEqual(len(created), 1)
        self.assertNotIn("labels", created[0][2])

    def test_other_errors_do_not_retry_blind(self):
        api = LabelRefusingApi(500)
        Issues(api, "watcher", log=lambda _: None).report("A", ["x"], Cache(None))
        self.assertEqual(api.sent, [])

    def test_a_degraded_issue_list_never_creates(self):
        # A blind create past a failed listing is how duplicates are born.
        def failing(query):
            raise http_error(500)

        api = StubApi({"/issues": failing})
        Issues(api, "watcher", log=lambda _: None).report("A", ["x"], Cache(None))
        self.assertEqual(api.sent, [])

    def test_resolve_if_matches_the_signature(self):
        signature = Issues.signature_of(["down"])
        body = (f"<!-- watcher:listing=A -->\n"
                f"<!-- watcher:signature={signature} -->\nold")
        issue = {"number": 7, "state": "open", "body": body}
        api = StubApi({"/issues/7": issue, "/issues": [issue]})
        Issues(api, "watcher", log=lambda _: None).resolve_if("A", signature, Cache(None))
        self.assertEqual(api.writes("PATCH", path="/issues/7")[0][2], {"state": "closed"})

    def test_resolve_if_leaves_a_different_failure_alone(self):
        body = ("<!-- watcher:listing=A -->\n"
                "<!-- watcher:signature=ffff -->\nold")
        api = StubApi({"/issues": [{"number": 7, "state": "open", "body": body}]})
        Issues(api, "watcher", log=lambda _: None).resolve_if("A", "0000", Cache(None))
        self.assertEqual(api.sent, [])


class DispatchRefusingApi(StubApi):
    def send(self, method, path, payload):
        if "/dispatches" in path:
            raise http_error(404)
        return super().send(method, path, payload)


class SweepRefusals(unittest.TestCase):
    def sweep(self, api, store):
        options = parse_arguments([])
        Sweep(api, store, options, log=lambda _: None).run()
        return api

    def routes(self):
        return {
            "/pulls": [{"number": 3, "head": {"sha": "abc"}}],
            "/actions/runs": {"workflow_runs": []},
            "/commits/abc/check-suites": {"total_count": 0},
            "/commits/abc/status": {"statuses": []},
            "/commits/abc/check-runs": {"check_runs": []},
            "/issues/3/comments": [],
        }

    def test_a_refused_dispatch_is_retried_on_a_clock_not_never(self):
        store = Cache(None)
        api = DispatchRefusingApi(self.routes())
        self.sweep(api, store)
        state = store.section("sweep", "abc")
        self.assertIn("refused", state)

        # Within the refusal window nothing is tried again.
        again = DispatchRefusingApi(self.routes())
        self.sweep(again, store)
        self.assertEqual(again.writes("POST", contains="/dispatches"), [])

        # Once the window has passed, the pull request recovers.
        state["refused"] = "2020-01-01T00:00:00Z"
        recovered = StubApi(self.routes())
        self.sweep(recovered, store)
        self.assertEqual(len(recovered.writes("POST", contains="/dispatches")), 1)

    def test_stale_sweep_state_is_pruned(self):
        store = Cache(None)
        store.section("sweep", "zzz")["attempts"] = 1
        self.sweep(StubApi(self.routes()), store)
        self.assertNotIn("zzz", store.data["sweep"])
        self.assertIn("abc", store.data["sweep"])


if __name__ == "__main__":
    unittest.main()
