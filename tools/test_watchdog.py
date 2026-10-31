#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the watchdog: no token, no network.

The API is a stub that answers the run list and records what the watchdog would
send, which is what makes "one issue, kept current" testable at all.
"""

import contextlib
import io
import re
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import watchdog
from watchdog import MARKER, PollWatchdog, Watchdog, parse_arguments

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

    def test_the_log_compares_a_gap_only_when_a_run_exists(self):
        options = parse_arguments([])
        lines = []
        with patch.object(watchdog, "now", lambda: NOW):
            api = StubApi({"/actions/workflows": {"workflow_runs": []}, "/issues": []})
            Watchdog(api, options, log=lines.append).run()
            self.assertNotIn("past the", lines[0])
            lines.clear()
            api = StubApi({"/actions/workflows": run_list(240), "/issues": []})
            Watchdog(api, options, log=lines.append).run()
            self.assertIn("past the 45", lines[0])

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
        self.assertEqual(runs[0].get("status"), "success")

    def test_a_hand_dispatch_between_two_dropped_ticks_is_not_a_tick(self):
        api = EventAware(scheduled=run_list(240), dispatched=run_list(5))
        self.assertTrue(check(api))
        opened = api.writes("POST", "/issues")
        self.assertEqual(len(opened), 1)
        self.assertIn("240 minute(s) ago", opened[0][2]["body"])

    def test_an_empty_event_counts_every_run(self):
        api = EventAware(scheduled=run_list(240), dispatched=run_list(5))
        self.assertFalse(check(api, ["--event", ""]))


STEP = "Master server did not answer"

POLL_BODY = MARKER.format(workflow="game-versions.yml")


def poll_runs(*minutes_ago):
    """Runs of the poll workflow, newest first, with the ids 1, 2, 3 and so on."""
    runs = []
    for number, minutes in enumerate(minutes_ago, start=1):
        started = watchdog.iso(NOW - timedelta(minutes=minutes))
        runs.append(
            {
                "id": number,
                "run_started_at": started,
                "created_at": started,
                "html_url": f"https://github.com/KSAModding/content-index-releases/actions/runs/{number}",
            }
        )
    return {"workflow_runs": runs}


def job_with(conclusion):
    """The jobs of a run whose marker step ended as `conclusion`."""
    steps = [{"name": "Poll the master server", "conclusion": "success"}]
    if conclusion is not None:
        steps.append({"name": STEP, "conclusion": conclusion})
    return {"jobs": [{"steps": steps}]}


class PollApi(StubApi):
    """Answers the poll runs and, per run id, the jobs of that run."""

    def __init__(self, runs, jobs, issues=None):
        super().__init__({"/actions/workflows": runs, "/issues": issues or []})
        self.jobs = jobs

    def get(self, path, **query):
        if path.startswith("/actions/runs/"):
            self.read.append((path, query))
            return self.jobs[int(path.split("/")[3])]
        return super().get(path, **query)


def check_poll(api, argv=None, log=None):
    options = parse_arguments(
        ["--workflow", "game-versions.yml", "--unanswered-step", STEP] + (argv or [])
    )
    with patch.object(watchdog, "now", lambda: NOW):
        return PollWatchdog(api, options, log=log or (lambda _: None)).run()


def jobs_read(api):
    return [path for path, _ in api.read if path.startswith("/actions/runs/")]


class AnUnansweredServerIsReported(unittest.TestCase):
    def test_a_poll_the_server_answered_reports_nothing(self):
        api = PollApi(poll_runs(30), {1: job_with("skipped")})
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_one_unanswered_poll_after_an_older_answer_reports_nothing(self):
        # The answer is far older than the failed poll, which is how the real
        # schedule spaces its runs.
        api = PollApi(poll_runs(30, 430), {1: job_with("success"), 2: job_with("skipped")})
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_two_unanswered_polls_after_an_answer_report_nothing(self):
        api = PollApi(
            poll_runs(30, 330, 630),
            {1: job_with("success"), 2: job_with("success"), 3: job_with("skipped")},
        )
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_three_unanswered_polls_in_a_row_open_one_issue(self):
        runs = poll_runs(30, 330, 630)
        api = PollApi(runs, {number: job_with("success") for number in range(1, 4)})
        self.assertTrue(check_poll(api))
        opened = api.writes("POST", "/issues")
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][2]["title"], watchdog.POLL_TITLE)
        self.assertIn(POLL_BODY, opened[0][2]["body"])
        self.assertIn("newest 3 scheduled poll(s)", opened[0][2]["body"])

    def test_the_count_is_an_option(self):
        api = PollApi(poll_runs(30, 330), {1: job_with("success"), 2: job_with("success")})
        self.assertTrue(check_poll(api, ["--unanswered-polls", "2"]))
        self.assertEqual(len(api.writes("POST", "/issues")), 1)

    def test_an_old_answer_with_no_poll_since_is_not_an_outage(self):
        # A dropped schedule is not a silent server, so the age of the newest
        # poll decides nothing here.
        api = PollApi(poll_runs(6000), {1: job_with("skipped")})
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_a_long_gap_between_unanswered_polls_still_counts_them(self):
        api = PollApi(
            poll_runs(30, 600, 6000),
            {number: job_with("success") for number in range(1, 4)},
        )
        self.assertTrue(check_poll(api))

    def test_too_few_listed_polls_neither_open_nor_close(self):
        api = PollApi(
            poll_runs(30, 330),
            {1: job_with("success"), 2: job_with("success")},
            issues=[{"number": 7, "body": POLL_BODY}],
        )
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_an_empty_run_list_neither_opens_nor_closes(self):
        api = PollApi({"workflow_runs": []}, {}, issues=[{"number": 7, "body": POLL_BODY}])
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_the_newest_answer_ends_the_walk(self):
        api = PollApi(
            poll_runs(30, 90, 150),
            {1: job_with("skipped"), 2: job_with("success"), 3: job_with("success")},
        )
        check_poll(api)
        self.assertEqual(jobs_read(api), ["/actions/runs/1/jobs"])

    def test_runs_from_before_the_marker_count_as_answered(self):
        api = PollApi(poll_runs(30, 330, 630), {number: job_with(None) for number in range(1, 4)})
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_the_first_unanswered_poll_after_the_change_reports_nothing(self):
        api = PollApi(
            poll_runs(30, 330, 630),
            {1: job_with("success"), 2: job_with(None), 3: job_with(None)},
        )
        self.assertFalse(check_poll(api))
        self.assertEqual(api.sent, [])

    def test_an_outage_that_is_already_reported_is_edited(self):
        api = PollApi(
            poll_runs(30, 330, 630),
            {number: job_with("success") for number in range(1, 4)},
            issues=[{"number": 7, "body": POLL_BODY}],
        )
        self.assertTrue(check_poll(api))
        self.assertEqual(api.writes("POST", "/issues"), [])
        self.assertEqual(len(api.writes("PATCH", "/issues/7")), 1)

    def test_the_watcher_issue_is_not_the_poll_issue(self):
        api = PollApi(
            poll_runs(30, 330, 630),
            {number: job_with("success") for number in range(1, 4)},
            issues=[{"number": 7, "body": BODY}],
        )
        check_poll(api)
        self.assertEqual(len(api.writes("POST", "/issues")), 1)
        self.assertEqual(api.writes("PATCH", "/issues/7"), [])

    def test_the_run_list_is_asked_for_successful_scheduled_runs(self):
        # A red run proves nothing about the server, and a hand dispatch does
        # not close the issue, which is what the advice in its body promises.
        api = PollApi(poll_runs(30), {1: job_with("skipped")})
        check_poll(api)
        query = [query for path, query in api.read if path.startswith("/actions/workflows")][0]
        self.assertEqual(query.get("status"), "success")
        self.assertEqual(query.get("event"), "schedule")
        self.assertEqual(query.get("per_page"), watchdog.LISTED_POLLS)


class TheIssueSaysWhenTheServerLastAnswered(unittest.TestCase):
    def body_of(self, runs, jobs):
        api = PollApi(runs, jobs)
        self.assertTrue(check_poll(api))
        return api, api.writes("POST", "/issues")[0][2]["body"]

    def test_it_names_the_newest_answer_past_the_count(self):
        runs = poll_runs(30, 330, 630, 930, 1230)
        jobs = {number: job_with("success") for number in range(1, 4)}
        jobs[4] = job_with("skipped")
        jobs[5] = job_with("skipped")
        api, body = self.body_of(runs, jobs)
        self.assertIn(f"It last answered a poll {watchdog.iso(NOW - timedelta(minutes=930))}", body)
        self.assertIn("930 minute(s) ago", body)
        self.assertIn("Last poll the server answered: https://github.com/", body)
        self.assertIn("/actions/runs/4", body)
        self.assertNotIn("/actions/runs/5/jobs", jobs_read(api))

    def test_it_says_so_when_no_listed_run_shows_an_answer(self):
        runs = poll_runs(30, 330, 630)
        _, body = self.body_of(runs, {number: job_with("success") for number in range(1, 4)})
        self.assertIn("do not show when it last answered", body)
        self.assertNotIn("Last poll the server answered", body)

    def test_a_run_from_before_the_marker_is_not_named_as_the_answer(self):
        # Nothing shows whether the server answered that run, so the issue must
        # not claim it did, and older runs cannot say anything either.
        runs = poll_runs(30, 330, 630, 930, 1230)
        jobs = {number: job_with("success") for number in range(1, 4)}
        jobs[4] = job_with(None)
        jobs[5] = job_with("skipped")
        api, body = self.body_of(runs, jobs)
        self.assertIn("do not show when it last answered", body)
        self.assertNotIn("Last poll the server answered", body)
        self.assertNotIn("/actions/runs/5/jobs", jobs_read(api))

    def test_the_log_says_the_same(self):
        lines = []
        runs = poll_runs(30, 330, 630)
        api = PollApi(runs, {number: job_with("success") for number in range(1, 4)})
        check_poll(api, log=lines.append)
        self.assertIn("got no answer from the master server", lines[0])
        self.assertNotIn("past the", lines[0])


class TheIssueClosesWhenTheServerAnswers(unittest.TestCase):
    def test_an_answered_poll_closes_the_open_issue(self):
        api = PollApi(
            poll_runs(5),
            {1: job_with("skipped")},
            issues=[{"number": 7, "body": POLL_BODY}],
        )
        self.assertFalse(check_poll(api))
        self.assertEqual(
            api.writes("PATCH", "/issues/7"), [("PATCH", "/issues/7", {"state": "closed"})]
        )
        comments = api.writes("POST", "/issues/7/comments")
        self.assertEqual(len(comments), 1)
        self.assertIn("master server answers again", comments[0][2]["body"])

    def test_the_watcher_issue_is_left_open(self):
        api = PollApi(
            poll_runs(5),
            {1: job_with("skipped")},
            issues=[{"number": 7, "body": BODY}],
        )
        check_poll(api)
        self.assertEqual(api.sent, [])


class ThePollWorkflowKeepsTheMarker(unittest.TestCase):
    """The step the watchdog reads has to exist under the name it is given."""

    def test_the_step_the_watchdog_reads_is_in_the_poll_workflow(self):
        text = (WORKFLOWS / "watchdog.yml").read_text(encoding="utf-8")
        found = re.search(r'--unanswered-step "([^"]+)"', text)
        assert found, "watchdog.yml no longer passes the marker step"
        poll = (WORKFLOWS / "game-versions.yml").read_text(encoding="utf-8")
        self.assertIn(f"- name: {found.group(1)}\n        if: steps.poll.outputs.answered != 'true'", poll)

    def test_an_answered_poll_is_what_the_poll_step_reports(self):
        poll = (WORKFLOWS / "game-versions.yml").read_text(encoding="utf-8")
        script = poll.split('if [ -z "$response" ]; then', 1)[1]
        unreachable, answered = script.split("\n          fi\n", 1)
        self.assertNotIn("answered=true", unreachable)
        self.assertIn('echo "answered=true" >> "$GITHUB_OUTPUT"', answered)

    def test_a_build_by_hand_reaches_the_script_only_through_the_environment(self):
        poll = (WORKFLOWS / "game-versions.yml").read_text(encoding="utf-8")
        self.assertIn("BUILD: ${{ inputs.build }}", poll)
        script = poll.split("Poll the master server", 1)[1].split("run: |", 1)[1]
        self.assertNotIn("inputs.build", script)
        by_hand = script.split('if [ -n "$BUILD" ]; then', 1)[1].split("else", 1)[0]
        self.assertNotIn("curl", by_hand)

    def test_an_unanswered_poll_still_ends_green(self):
        poll = (WORKFLOWS / "game-versions.yml").read_text(encoding="utf-8")
        branch = poll.split('if [ -z "$response" ]; then', 1)[1].split("\n          fi\n", 1)[0]
        self.assertIn("exit 0", branch)
        self.assertNotIn("exit 1", branch)


def run_main(api, argv):
    """Runs main() against the stub and hides what it prints."""
    with (
        patch.object(watchdog, "Api", lambda *_: api),
        patch.object(watchdog, "now", lambda: NOW),
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        return watchdog.main(argv)


class TheCommandLineChoosesTheCheck(unittest.TestCase):
    POLL = ["--workflow", "game-versions.yml", "--unanswered-step", STEP]

    def test_the_unanswered_step_reads_the_marker_of_the_poll(self):
        api = PollApi(poll_runs(30), {1: job_with("skipped")})
        self.assertEqual(run_main(api, self.POLL), 0)
        self.assertEqual(jobs_read(api), ["/actions/runs/1/jobs"])

    def test_an_unanswered_outage_is_reported_and_still_exits_zero(self):
        runs = poll_runs(30, 330, 630)
        api = PollApi(runs, {number: job_with("success") for number in range(1, 4)})
        self.assertEqual(run_main(api, self.POLL), 0)
        self.assertEqual(api.writes("POST", "/issues")[0][2]["title"], watchdog.POLL_TITLE)

    def test_without_the_step_no_job_is_read(self):
        api = StubApi({"/actions/workflows": run_list(12), "/issues": []})
        self.assertEqual(run_main(api, []), 0)
        self.assertEqual(jobs_read(api), [])

    def test_an_unreadable_run_list_fails_the_poll_check(self):
        api = PollApi(poll_runs(30), {})
        api.failing = ["/actions/workflows"]
        self.assertEqual(run_main(api, self.POLL), 1)


def poll_check_step():
    """The text of the step of watchdog.yml that runs the poll check."""
    text = (WORKFLOWS / "watchdog.yml").read_text(encoding="utf-8")
    return text.split("- name: Check when the master server last answered a poll\n", 1)[1]


def reported_polls():
    """The count of unanswered polls the watchdog workflow reports at."""
    found = re.search(
        r"UNANSWERED_POLLS: \$\{\{ inputs\.unanswered_polls \|\| '(\d+)' \}\}",
        poll_check_step(),
    )
    assert found, "watchdog.yml no longer passes a default count of polls"
    return int(found.group(1))


class TheWorkflowRunsThePollCheck(unittest.TestCase):
    """These lines only run on GitHub, so the text is what a local test can hold."""

    def test_the_check_watches_the_poll_workflow(self):
        self.assertIn("--workflow game-versions.yml\n", poll_check_step())

    def test_the_first_check_does_not_stop_the_second(self):
        self.assertTrue(poll_check_step().startswith("        if: ${{ !cancelled() }}\n"))

    def test_the_count_reaches_the_tool(self):
        self.assertIn('--unanswered-polls "$UNANSWERED_POLLS"', poll_check_step())

    def test_the_workflow_reports_at_the_count_the_tool_defaults_to(self):
        self.assertEqual(reported_polls(), parse_arguments([]).unanswered_polls)

    def test_a_hand_dispatch_offers_the_same_count(self):
        text = (WORKFLOWS / "watchdog.yml").read_text(encoding="utf-8")
        inputs = text.split("  unanswered_polls:\n", 1)[1].split("dry_run:", 1)[0]
        found = re.search(r'default: "(\d+)"', inputs)
        assert found, "watchdog.yml no longer offers a count of polls"
        self.assertEqual(int(found.group(1)), reported_polls())

    def test_the_default_needs_more_than_one_failed_poll(self):
        # A poll fails now and then, and one failed poll must report nothing.
        self.assertGreater(reported_polls(), 1)


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

    def test_a_count_no_run_list_can_reach_is_refused(self):
        # A count past the listed page would never report.
        for count in ("0", str(watchdog.LISTED_POLLS + 1)):
            with self.subTest(count=count), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    parse_arguments(["--unanswered-polls", count])
        self.assertEqual(
            parse_arguments(["--unanswered-polls", str(watchdog.LISTED_POLLS)]).unanswered_polls,
            watchdog.LISTED_POLLS,
        )


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
