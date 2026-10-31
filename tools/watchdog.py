#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Report when the watcher has not ticked for a long time.

GitHub drops a scheduled run when it has no room and retries nothing, so the
watcher can stretch from ten minutes to hours with only the run list saying so.
This keeps one issue current while the last successful scheduled run is longer
ago than `--stale-minutes`, and the next one that succeeds closes it. Only a
scheduled run counts, because a hand dispatch answers the gap without ending it.

With `--unanswered-step` it watches the game versions poll instead. That poll
ends green when the master server does not answer, so a run that succeeded says
nothing. The workflow runs a marker step only for an unanswered poll, and the
skipped marker of a run is what shows the server answered. This check reports
after `--unanswered-polls` unanswered polls in a row and has no time window,
because GitHub starts that schedule hours apart and a gap between polls says
nothing about the server.
"""

import argparse
import os
import sys
import urllib.error
from datetime import timedelta

import hosts
from hosts import HostError
from watch import Api, iso, now, parse_iso

# The marker that makes the one watchdog issue findable without a search.
MARKER = "<!-- watchdog:workflow={workflow} -->"

TITLE = "The watcher is not ticking"

POLL_TITLE = "The master server is not answering"

# The one page of poll runs a check reads, the most its walk back can reach.
LISTED_POLLS = 100


def minutes_of(age):
    return int(age.total_seconds() // 60)


def started_at(run):
    return parse_iso(run.get("run_started_at") or run.get("created_at"))


class Watchdog:
    def __init__(self, api, options, log=print):
        self.api = api
        self.options = options
        self.log = log
        self.marker = MARKER.format(workflow=options.workflow)
        # "schedule" is the API name, "scheduled" reads better in a report.
        self.described_event = {"schedule": "scheduled"}.get(options.event, options.event) or "any"

    def successful_runs(self, per_page):
        """The newest successful runs of the watched workflow, newest first.

        Only a run started by `--event` counts, because a hand dispatch succeeds
        while the schedule is still dropped.
        """
        query = {
            "status": "success",
            "per_page": per_page,
            "exclude_pull_requests": "true",
        }
        if self.options.event:
            query["event"] = self.options.event
        answer = self.api.get(
            f"/actions/workflows/{self.options.workflow}/runs",
            **query,
        )
        return (answer or {}).get("workflow_runs") or []

    def last_success(self):
        """When the newest successful run of the watched workflow started, and that run.

        Both are None when the API lists no such run, so the watcher never ran
        or ran outside its retention.
        """
        runs = self.successful_runs(1)
        if not runs:
            return None, None
        return started_at(runs[0]), runs[0]

    def run(self):
        """Report or close, and say whether the watcher is late."""
        started, run = self.last_success()
        age = None if started is None else now() - started
        if age is not None and age < timedelta(minutes=self.options.stale_minutes):
            self.log(self.describe(started, age))
            self.resolve()
            return False
        if started is None:
            self.log(self.describe(started, age))
        else:
            self.log(
                f"{self.describe(started, age)} That is past the "
                f"{self.options.stale_minutes} this reports at."
            )
        self.report(started, run, age)
        return True

    def find(self):
        """The open watchdog issue, and whether the lookup itself failed.

        A failed lookup is not an absent issue, and creating one against it is
        how a second issue for the same gap gets opened. The unlabelled list is
        the fallback for an issue opened when the label was refused.
        """
        for query in ({"labels": self.options.label}, {}):
            try:
                issues = self.api.get_paged("/issues", state="open", **query)
            except (urllib.error.HTTPError, HostError) as error:
                self.log(f"could not list issues: {error}")
                return None, True
            for issue in issues:
                if "pull_request" in issue:
                    continue
                if self.marker in (issue.get("body") or ""):
                    return issue, False
        return None, False

    def report(self, started, run, age):
        """Keep the one open issue current with the gap. Never raises."""
        try:
            self._report(started, run, age)
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"could not report the gap: {error}")

    def _report(self, started, run, age):
        body = self.body(started, run, age)
        issue, degraded = self.find()
        if issue is not None:
            # An edit and no comment, so the same gap notifies nobody twice.
            self.api.send("PATCH", f"/issues/{issue['number']}", {"body": body})
            self.log(f"kept {self.api.repository}#{issue['number']} current")
            return
        if degraded:
            self.log(
                "not opening an issue: the issue list could not be read, and a blind "
                "create duplicates"
            )
            return
        payload = {"title": self.title, "body": body, "labels": [self.options.label]}
        try:
            created = self.api.send("POST", "/issues", payload)
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise
            # The marker in the body finds the issue, so an undefined label is
            # not worth losing the report.
            self.log(f"the '{self.options.label}' label was refused (HTTP 422)")
            payload.pop("labels")
            created = self.api.send("POST", "/issues", payload)
        if created:
            self.log(f"opened {self.api.repository}#{created['number']}")

    def resolve(self):
        """Close the issue, because the watcher ran again. Never raises."""
        try:
            issue, _ = self.find()
            if issue is None:
                return
            number = issue["number"]
            self.api.send(
                "POST",
                f"/issues/{number}/comments",
                {"body": self.resolved()},
            )
            self.api.send("PATCH", f"/issues/{number}", {"state": "closed"})
            self.log(f"closed {self.api.repository}#{number}")
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"could not close the issue: {error}")

    title = TITLE
    run_label = "Last successful run"

    def describe(self, started, age):
        """The gap in one sentence, for the issue and for the log."""
        if started is None:
            return (
                f"The Actions API lists no successful {self.described_event} run of "
                f"`{self.options.workflow}` at all."
            )
        return (
            f"`{self.options.workflow}` last started a successful "
            f"{self.described_event} run {iso(started)}, which is "
            f"{minutes_of(age)} minute(s) ago."
        )

    def explanation(self):
        return [
            "GitHub starts a scheduled workflow only when it has room and retries nothing",
            "it drops, so a release published since then is not stamped yet and a client",
            "still sees the version before it.",
        ]

    def threshold(self):
        return (
            f"Reported once the last success is older than {self.options.stale_minutes} "
            "minute(s)."
        )

    def advice(self):
        return [
            f"A steward can start a tick now by dispatching `{self.options.workflow}`, "
            "which stamps everything that is waiting.",
            "That dispatch does not close this issue, because what is late is the "
            f"schedule. This issue closes by itself once a {self.described_event} run "
            "succeeds again.",
        ]

    def resolved(self):
        return f"`{self.options.workflow}` is running again, so this is done."

    def body(self, started, run, age):
        lines = [self.marker, self.describe(started, age), ""]
        lines += self.explanation()
        lines += ["", self.threshold()]
        if run and run.get("html_url"):
            lines.append(f"{self.run_label}: {run['html_url']}")
        lines += ["", *self.advice(), "", f"Last checked {iso(now())}."]
        return "\n".join(lines)


class PollWatchdog(Watchdog):
    """Reports a master server that recent polls of the game versions workflow did not reach.

    A poll the server did not answer still ends green, so the run list cannot
    tell it from one that did. The workflow runs the `--unanswered-step` step
    only for an unanswered poll, which leaves that step skipped in a run that
    got an answer. A run from before the step existed has no such step and
    counts as answered, so a missing step never opens an issue.

    The decision counts runs, because GitHub starts the schedule hours apart and
    often drops it. Only a poll that ran is evidence, so a stretch with no poll
    neither opens nor closes the issue.
    """

    title = POLL_TITLE
    run_label = "Last poll the server answered"

    def run(self):
        """Report or close, and say whether the server counts as silent."""
        runs = self.successful_runs(LISTED_POLLS)
        newest = runs[: self.options.unanswered_polls]
        for run in newest:
            if self.marker_conclusion(run) in (None, "skipped"):
                self.log(
                    f"The `{self.options.workflow}` poll of {iso(started_at(run))} counts "
                    "as answered."
                )
                self.resolve()
                return False
        if len(newest) < self.options.unanswered_polls:
            self.log(
                f"Only {len(newest)} {self.described_event} poll(s) of "
                f"`{self.options.workflow}` are listed and none got an answer, which is "
                f"too few to report at {self.options.unanswered_polls}."
            )
            return False
        started, answered = self.last_answer(runs[len(newest) :])
        age = None if started is None else now() - started
        self.log(self.describe(started, age))
        self.report(started, answered, age)
        return True

    def marker_conclusion(self, run):
        """How the marker step of a run ended, or None for a run without that step."""
        jobs = self.api.get(f"/actions/runs/{run['id']}/jobs", filter="latest", per_page=100)
        for job in (jobs or {}).get("jobs") or []:
            for step in job.get("steps") or []:
                if step.get("name") == self.options.unanswered_step:
                    return step.get("conclusion")
        return None

    def last_answer(self, runs):
        """When the newest of these runs that got an answer started, and that run.

        Both are None when none did. A run without the marker ends the walk,
        because nothing shows whether the server answered it, and the issue must
        not name it as the last answer.
        """
        for run in runs:
            conclusion = self.marker_conclusion(run)
            if conclusion is None:
                break
            if conclusion == "skipped":
                return started_at(run), run
        return None, None

    def describe(self, started, age):
        silent = (
            f"The newest {self.options.unanswered_polls} {self.described_event} poll(s) of "
            f"`{self.options.workflow}` got no answer from the master server."
        )
        if started is None:
            return f"{silent} The listed runs do not show when it last answered."
        return (
            f"{silent} It last answered a poll {iso(started)}, which is "
            f"{minutes_of(age)} minute(s) ago."
        )

    def explanation(self):
        return [
            "The poll asks the KSA master server for the current game build and ends",
            "green when the server does not answer, so nothing else shows this.",
            "A build released since then is not in `game-versions.json`, and an authored",
            "bound that names it does not resolve.",
        ]

    def threshold(self):
        return (
            f"Reported once {self.options.unanswered_polls} poll(s) in a row got no answer. "
            "GitHub starts the schedule hours apart, so this counts polls and not time."
        )

    def advice(self):
        return ["This issue closes by itself once a scheduled poll gets an answer."]

    def resolved(self):
        return "The master server answers again, so this is done."


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description="Report when the watcher has not run for a long time."
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY") or "KSAModding/content-index-releases",
        help="the repository the watched workflow and the issue live in",
    )
    parser.add_argument("--workflow", default="watcher.yml")
    parser.add_argument(
        "--event", default="schedule",
        help="only a run this event started counts as a tick. Empty means any event",
    )
    parser.add_argument(
        "--stale-minutes", type=int, default=45,
        help="how old the last successful run may be before it is reported. The check "
        "samples on its own schedule, so the longest gap that stays invisible is this "
        "plus that interval. Not used with --unanswered-step",
    )
    parser.add_argument(
        "--unanswered-step", default="",
        help="watch a poll instead of a watcher. This is the name of the step the workflow "
        "runs only when the master server did not answer. Without it, a run that "
        "succeeded counts",
    )
    parser.add_argument(
        "--unanswered-polls", type=int, default=3,
        help="with --unanswered-step, how many polls in a row must get no answer before "
        "it is reported",
    )
    parser.add_argument("--label", default="area:infra")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="read everything, open and close nothing",
    )
    options = parser.parse_args(argv)
    if options.stale_minutes < 1:
        parser.error("--stale-minutes must be at least 1")
    if not 1 <= options.unanswered_polls <= LISTED_POLLS:
        parser.error(f"--unanswered-polls must be from 1 to {LISTED_POLLS}")
    options.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("INDEX_TOKEN")
    return options


def main(argv=None):
    options = parse_arguments(argv)
    if not options.token:
        print(
            "no token in GITHUB_TOKEN: the check can read a public run list but cannot "
            "keep the issue current",
            file=sys.stderr,
        )
    http = hosts.Http(token=options.token)
    api = Api(http, options.repository, options.dry_run)
    try:
        # A late watcher is an issue and not a red run, so a gap still exits
        # zero. Only a run list it could not read fails.
        watchdog = PollWatchdog if options.unanswered_step else Watchdog
        watchdog(api, options).run()
    except (urllib.error.HTTPError, HostError) as error:
        print(f"could not read the run list: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
