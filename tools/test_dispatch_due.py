#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the start of the jobs that are due: no token, no network."""

import re
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import dispatch_due
from dispatch_due import JOBS, Job, dispatch_due as run_dispatch

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

HOURLY = Job("game-versions.yml", "due-game-versions", timedelta(hours=1))
BACKSTOP = Job("snapshot.yml", "due-snapshot", timedelta(hours=6), any_event=True)


def run(minutes_ago, event="repository_dispatch", title="due-game-versions", conclusion="success"):
    return {
        "created_at": dispatch_due.iso(NOW - timedelta(minutes=minutes_ago)),
        "event": event,
        "display_title": title,
        "conclusion": conclusion,
    }


class StubApi:
    """Answers each run list query from a table and records every write."""

    def __init__(self, runs=None, failing_reads=(), failing_sends=()):
        # {(workflow, event or None): [run, ...]}
        self.runs = runs or {}
        self.failing_reads = failing_reads
        self.failing_sends = failing_sends
        self.read = []
        self.sent = []

    def get(self, path, **query):
        self.read.append((path, query))
        workflow = path.split("/")[3]
        if workflow in self.failing_reads:
            raise urllib.error.HTTPError("https://x", 500, "boom", {}, None)
        return {"workflow_runs": self.runs.get((workflow, query.get("event")), [])}

    def send(self, method, path, payload):
        if payload["event_type"] in self.failing_sends:
            raise urllib.error.HTTPError("https://x", 422, "boom", {}, None)
        self.sent.append((method, path, payload))
        return None


def dispatch(api, jobs):
    logged = []
    with patch.object(dispatch_due, "now", return_value=NOW):
        failures = run_dispatch(api, jobs, log=logged.append)
    return failures, logged


def sent_types(api):
    return [payload["event_type"] for _, _, payload in api.sent]


class TheDueJobs(unittest.TestCase):
    def test_a_job_that_never_ran_is_started(self):
        api = StubApi()
        failures, _ = dispatch(api, [HOURLY])
        self.assertEqual(failures, 0)
        self.assertEqual(api.sent, [("POST", "/dispatches", {"event_type": "due-game-versions"})])

    def test_a_job_that_ran_within_its_interval_is_not_started(self):
        api = StubApi({("game-versions.yml", "repository_dispatch"): [run(20)]})
        dispatch(api, [HOURLY])
        self.assertEqual(api.sent, [])

    def test_a_job_is_due_a_little_before_its_interval(self):
        # Ticks come at uneven times, so 55 minutes counts as an hour.
        api = StubApi({("game-versions.yml", "repository_dispatch"): [run(55)]})
        dispatch(api, [HOURLY])
        self.assertEqual(sent_types(api), ["due-game-versions"])

    def test_a_scheduled_run_counts_as_a_run_by_the_clock(self):
        # The runs from before the change, so the first tick starts no burst.
        api = StubApi({("game-versions.yml", "schedule"): [run(20, event="schedule", title="game versions")]})
        dispatch(api, [HOURLY])
        self.assertEqual(api.sent, [])

    def test_the_newest_of_both_lists_decides(self):
        api = StubApi({
            ("game-versions.yml", "repository_dispatch"): [run(300)],
            ("game-versions.yml", "schedule"): [run(20, event="schedule", title="game versions")],
        })
        dispatch(api, [HOURLY])
        self.assertEqual(api.sent, [])

    def test_a_dispatch_of_another_type_does_not_count(self):
        # The merge dispatch starts a prune or a run without reads, not the job.
        api = StubApi({("game-versions.yml", "repository_dispatch"): [run(5, title="index-changed")]})
        dispatch(api, [HOURLY])
        self.assertEqual(sent_types(api), ["due-game-versions"])

    def test_a_cancelled_run_does_not_count(self):
        api = StubApi({("game-versions.yml", "repository_dispatch"): [run(5, conclusion="cancelled")]})
        dispatch(api, [HOURLY])
        self.assertEqual(sent_types(api), ["due-game-versions"])

    def test_a_run_that_has_not_ended_counts(self):
        # A queued run would do the work, so a second one is not started.
        api = StubApi({("game-versions.yml", "repository_dispatch"): [run(5, conclusion=None)]})
        dispatch(api, [HOURLY])
        self.assertEqual(api.sent, [])

    def test_a_failed_run_counts(self):
        # The next run comes at the next interval, not at every tick.
        api = StubApi({("game-versions.yml", "repository_dispatch"): [run(5, conclusion="failure")]})
        dispatch(api, [HOURLY])
        self.assertEqual(api.sent, [])

    def test_any_run_counts_for_a_backstop(self):
        api = StubApi({("snapshot.yml", None): [run(30, event="push", title="Stamp a release")]})
        dispatch(api, [BACKSTOP])
        self.assertEqual(api.sent, [])
        self.assertEqual(api.read[0][1].get("event"), None)

    def test_a_pull_request_run_does_not_count_for_a_backstop(self):
        api = StubApi({("snapshot.yml", None): [run(30, event="pull_request")]})
        dispatch(api, [BACKSTOP])
        self.assertEqual(sent_types(api), ["due-snapshot"])

    def test_a_failed_read_skips_that_job_only(self):
        api = StubApi(failing_reads=("game-versions.yml",))
        failures, logged = dispatch(api, [HOURLY, BACKSTOP])
        self.assertEqual(failures, 1)
        self.assertEqual(sent_types(api), ["due-snapshot"])
        self.assertTrue(any("could not read" in line for line in logged))

    def test_a_failed_start_does_not_stop_the_next_job(self):
        api = StubApi(failing_sends=("due-game-versions",))
        failures, _ = dispatch(api, [HOURLY, BACKSTOP])
        self.assertEqual(failures, 1)
        self.assertEqual(sent_types(api), ["due-snapshot"])

    def test_main_exits_non_zero_after_a_failure(self):
        with patch.object(dispatch_due, "dispatch_due", return_value=1), \
                patch.object(dispatch_due, "Api"), patch.object(dispatch_due.hosts, "Http"):
            self.assertEqual(dispatch_due.main(["--dry-run"]), 1)


def triggers(workflow):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    return text.split("\non:", 1)[1].split("\npermissions:", 1)[0]


class TheWorkflows(unittest.TestCase):
    """The one schedule, and a listener for each dispatch type."""

    def test_only_the_watcher_has_a_schedule(self):
        # GitHub gives its scheduled runs in turn to every schedule entry, so a
        # second entry takes ticks from the watcher.
        scheduled = sorted(
            path.name for path in WORKFLOWS.glob("*.yml")
            if re.search(r"^\s*schedule:", triggers(path.name), re.MULTILINE)
        )
        self.assertEqual(scheduled, ["watcher.yml"])

    def test_every_job_listens_for_its_own_type_only(self):
        for job in JOBS:
            with self.subTest(workflow=job.workflow):
                found = re.search(r"repository_dispatch:\s*\n(?:\s*#.*\n)*\s*types: \[([^\]]*)\]", triggers(job.workflow))
                assert found, f"{job.workflow} does not listen for a repository_dispatch"
                types = [name.strip() for name in found.group(1).split(",")]
                self.assertIn(job.event_type, types)
                self.assertEqual([name for name in types if name.startswith("due-")], [job.event_type])

    def test_each_type_names_one_job(self):
        types = [job.event_type for job in JOBS]
        self.assertEqual(len(types), len(set(types)))

    def test_the_watcher_starts_the_jobs_after_a_failed_tick_too(self):
        text = (WORKFLOWS / "watcher.yml").read_text(encoding="utf-8")
        step = text.split("- name: Start the jobs that are due", 1)[1]
        self.assertIn("if: ${{ !cancelled() }}", step.split("run: |", 1)[0])
        self.assertIn("tools/dispatch_due.py", step)

    def test_no_job_shares_the_concurrency_group_of_the_watcher(self):
        # A pending run in a shared group cancels the pending run before it.
        for path in WORKFLOWS.glob("*.yml"):
            if path.name == "watcher.yml":
                continue
            with self.subTest(workflow=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"^\s*group: watcher\s*$", text, re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
