#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the watchdog: no token, no network.

The API is a stub that answers the run list and records what the watchdog would
send, which is what makes "one issue, kept current" testable at all.
"""

import re
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import watchdog
from watchdog import MARKER, Watchdog, parse_arguments

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)

BODY = MARKER.format(workflow="watcher.yml")


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "boom", {}, None)


class StubApi:
    """Answers reads from a routing table and records every read and write."""

    repository = "KSAModding/content-index-releases"

    def __init__(self, routes=None, failing=None):
        self.routes = routes or {}
        self.failing = failing or ()
        self.sent = []
        self.read = []

    def get(self, path, **query):
        self.read.append((path, query))
        if any(path.startswith(prefix) for prefix in self.failing):
            raise http_error(500)
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                return answer
        return None

    def get_paged(self, path, key=None, max_pages=10, **query):
        answer = self.get(path, **query)
        if key:
            return list((answer or {}).get(key) or [])
        return list(answer or [])

    def send(self, method, path, payload):
        self.sent.append((method, path, payload))
        if method == "POST" and path == "/issues":
            return {"number": 7}
        return {}

    def writes(self, method=None, path=None):
        return [
            entry
            for entry in self.sent
            if (method is None or entry[0] == method) and (path is None or entry[1] == path)
        ]


def run_list(minutes_ago):
    started = watchdog.iso(NOW - timedelta(minutes=minutes_ago))
    return {
        "workflow_runs": [
            {
                "id": 11,
                "run_started_at": started,
                "created_at": started,
                "html_url": "https://github.com/KSAModding/content-index-releases/actions/runs/11",
            }
        ]
    }


class EventAware(StubApi):
    """Answers the run list per event, the way the Actions API does."""

    def __init__(self, scheduled, dispatched):
        super().__init__({"/issues": []})
        self.scheduled = scheduled
        self.dispatched = dispatched

    def get(self, path, **query):
        if path.startswith("/actions/workflows"):
            self.read.append((path, query))
            return self.scheduled if query.get("event") == "schedule" else self.dispatched
        return super().get(path, **query)


def check(api, argv=None):
    options = parse_arguments(argv or [])
    with patch.object(watchdog, "now", lambda: NOW):
        stale = Watchdog(api, options, log=lambda _: None).run()
    return stale


class AGapIsReported(unittest.TestCase):
    def test_a_recent_tick_reports_nothing(self):
        api = StubApi({"/actions/workflows": run_list(12), "/issues": []})
        self.assertFalse(check(api))
        self.assertEqual(api.sent, [])

    def test_a_long_gap_opens_one_issue(self):
        api = StubApi({"/actions/workflows": run_list(240), "/issues": []})
        self.assertTrue(check(api))
        opened = api.writes("POST", "/issues")
        self.assertEqual(len(opened), 1)
        self.assertIn(BODY, opened[0][2]["body"])
        self.assertIn("240 minute(s) ago", opened[0][2]["body"])

    def test_the_gap_is_measured_against_the_stated_age(self):
        api = StubApi({"/actions/workflows": run_list(100), "/issues": []})
        self.assertFalse(check(api, ["--stale-minutes", "120"]))
        self.assertTrue(check(api, ["--stale-minutes", "60"]))

    def test_a_workflow_that_never_succeeded_is_reported(self):
        api = StubApi({"/actions/workflows": {"workflow_runs": []}, "/issues": []})
        self.assertTrue(check(api))
        opened = api.writes("POST", "/issues")
        self.assertIn("no successful scheduled run", opened[0][2]["body"])

    def test_a_gap_that_is_already_reported_is_edited(self):
        api = StubApi(
            {
                "/actions/workflows": run_list(240),
                "/issues": [{"number": 7, "body": BODY}],
            }
        )
        check(api)
        self.assertEqual(api.writes("POST", "/issues"), [])
        self.assertEqual(len(api.writes("PATCH", "/issues/7")), 1)
        self.assertEqual(api.writes("POST", "/issues/7/comments"), [])

    def test_an_issue_opened_without_the_label_is_still_found(self):
        class Unlabelled(StubApi):
            def get(self, path, **query):
                if path.startswith("/issues") and query.get("labels"):
                    return []
                return super().get(path, **query)

        api = Unlabelled(
            {
                "/actions/workflows": run_list(240),
                "/issues": [{"number": 7, "body": BODY}],
            }
        )
        check(api)
        self.assertEqual(api.writes("POST", "/issues"), [])
        self.assertEqual(len(api.writes("PATCH", "/issues/7")), 1)

    def test_an_unreadable_issue_list_opens_nothing(self):
        api = StubApi({"/actions/workflows": run_list(240)}, failing=["/issues"])
        check(api)
        self.assertEqual(api.sent, [])

    def test_a_pull_request_carrying_the_marker_is_not_the_issue(self):
        api = StubApi(
            {
                "/actions/workflows": run_list(240),
                "/issues": [{"number": 7, "body": BODY, "pull_request": {}}],
            }
        )
        check(api)
        self.assertEqual(len(api.writes("POST", "/issues")), 1)


class TheIssueClosesItself(unittest.TestCase):
    def test_a_tick_again_closes_the_open_issue(self):
        api = StubApi(
            {
                "/actions/workflows": run_list(3),
                "/issues": [{"number": 7, "body": BODY}],
            }
        )
        self.assertFalse(check(api))
        self.assertEqual(
            api.writes("PATCH", "/issues/7"), [("PATCH", "/issues/7", {"state": "closed"})]
        )
        self.assertEqual(len(api.writes("POST", "/issues/7/comments")), 1)

    def test_another_issue_is_left_alone(self):
        api = StubApi(
            {
                "/actions/workflows": run_list(3),
                "/issues": [{"number": 7, "body": "an unrelated issue"}],
            }
        )
        check(api)
        self.assertEqual(api.sent, [])


class OnlyTheScheduleCounts(unittest.TestCase):
    """A hand dispatch answers a gap. It does not end it, so it must not hide it."""

    def test_the_run_list_is_asked_for_scheduled_runs_only(self):
        api = StubApi({"/actions/workflows": run_list(12), "/issues": []})
        check(api)
        runs = [query for path, query in api.read if path.startswith("/actions/workflows")]
        self.assertEqual(runs[0].get("event"), "schedule")

    def test_a_hand_dispatch_between_two_dropped_ticks_is_not_a_tick(self):
        api = EventAware(scheduled=run_list(240), dispatched=run_list(5))
        self.assertTrue(check(api))
        opened = api.writes("POST", "/issues")
        self.assertEqual(len(opened), 1)
        self.assertIn("240 minute(s) ago", opened[0][2]["body"])

    def test_an_empty_event_counts_every_run(self):
        api = EventAware(scheduled=run_list(240), dispatched=run_list(5))
        self.assertFalse(check(api, ["--event", ""]))


class TheArguments(unittest.TestCase):
    def test_the_defaults_watch_the_watcher(self):
        options = parse_arguments([])
        self.assertEqual(options.workflow, "watcher.yml")
        self.assertEqual(options.event, "schedule")
        self.assertEqual(options.stale_minutes, 45)

    def test_the_default_label_is_one_this_repository_defines(self):
        # An undefined label is created on the spot by an actor with push
        # access, which is how an off-convention label appears.
        self.assertEqual(parse_arguments([]).label, "area:infra")

    def test_an_age_of_zero_is_refused(self):
        with self.assertRaises(SystemExit):
            parse_arguments(["--stale-minutes", "0"])


WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def fire_minutes(workflow):
    """Every minute of the hour a workflow's schedule entries fire at."""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    schedule = text.split("on:", 1)[1].split("jobs:", 1)[0]
    minutes = []
    for entry in re.findall(r'- cron: "([^"]+)"', schedule):
        field = entry.split()[0]
        minutes += [int(part) for part in field.split(",")]
    return sorted(minutes)


def longest_gap(minutes):
    """The longest wait between two fires, across the turn of the hour."""
    wrapped = minutes + [minutes[0] + 60]
    return max(second - first for first, second in zip(wrapped, wrapped[1:]))


def reported_age():
    """The age the watchdog workflow reports a gap at."""
    text = (WORKFLOWS / "watchdog.yml").read_text(encoding="utf-8")
    found = re.search(r"STALE_MINUTES: \$\{\{ inputs\.stale_minutes \|\| '(\d+)' \}\}", text)
    assert found, "watchdog.yml no longer passes a default stale age"
    return int(found.group(1))


class TheSchedules(unittest.TestCase):
    """The tuning the two workflows carry, which no other test reaches."""

    def test_the_watcher_ticks_every_ten_minutes_off_the_hour(self):
        minutes = fire_minutes("watcher.yml")
        self.assertEqual(minutes, [4, 14, 24, 34, 44, 54])
        self.assertEqual(longest_gap(minutes), 10)

    def test_a_gap_is_reported_before_it_reaches_an_hour(self):
        # A gap is only seen at a sample, so what stays invisible is the
        # reported age plus the sample interval, and the issue asks for an
        # outage of an hour to be visible.
        sample = longest_gap(fire_minutes("watchdog.yml"))
        self.assertLessEqual(sample + reported_age(), 60)

    def test_the_workflow_reports_at_the_age_the_tool_defaults_to(self):
        # The workflow always passes --stale-minutes, so its number is the one
        # that runs and the default below it is only for a hand run.
        self.assertEqual(reported_age(), parse_arguments([]).stale_minutes)

    def test_the_watchdog_samples_off_the_hour_too(self):
        self.assertNotIn(0, fire_minutes("watchdog.yml"))


if __name__ == "__main__":
    unittest.main()
