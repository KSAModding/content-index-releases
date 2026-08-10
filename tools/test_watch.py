#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the tick's own logic: no token, no network, no git.

The API is a stub that records what the watcher would send, which is what makes
"one open issue per listing" and the sweep's decisions testable at all.
"""

import json
import tempfile
import unittest
from pathlib import Path

from watch import Cache, Issues, Sweep, Watcher, parse_arguments

GAME_VERSIONS = {"spec_version": 1, "versions": ["2026.8.3.5117"]}


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


class TheRepositoryIsTheState(unittest.TestCase):
    def watcher(self, folder, argv=()):
        (folder / "game-versions.json").write_text(json.dumps(GAME_VERSIONS))
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
        watcher = Watcher(options)
        watcher.log = lambda message: None
        return watcher

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


if __name__ == "__main__":
    unittest.main()
