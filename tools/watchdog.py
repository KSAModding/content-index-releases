#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Report when the watcher has not ticked for a long time.

GitHub drops a scheduled run when it has no room and retries nothing, so the
watcher can stretch from ten minutes to hours with only the run list saying so.
This keeps one issue current while the last successful scheduled run is longer
ago than `--stale-minutes`, and the next one that succeeds closes it. Only a
scheduled run counts, because a hand dispatch answers the gap without ending it.
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


def minutes_of(age):
    return int(age.total_seconds() // 60)


class Watchdog:
    def __init__(self, api, options, log=print):
        self.api = api
        self.options = options
        self.log = log
        self.marker = MARKER.format(workflow=options.workflow)
        # "schedule" is the API name, "scheduled" reads better in a report.
        self.described_event = {"schedule": "scheduled"}.get(options.event, options.event) or "any"

    def last_success(self):
        """When the newest successful run of the watched workflow started, and that run.

        Only a run started by `--event` counts, because a hand dispatch succeeds
        while the schedule is still dropped. Both are None when the API lists no
        such run, so the watcher never ran or ran outside its retention.
        """
        query = {
            "status": "success",
            "per_page": 1,
            "exclude_pull_requests": "true",
        }
        if self.options.event:
            query["event"] = self.options.event
        answer = self.api.get(
            f"/actions/workflows/{self.options.workflow}/runs",
            **query,
        )
        runs = (answer or {}).get("workflow_runs") or []
        if not runs:
            return None, None
        run = runs[0]
        return parse_iso(run.get("run_started_at") or run.get("created_at")), run

    def run(self):
        """Report or close, and say whether the watcher is late."""
        started, run = self.last_success()
        age = None if started is None else now() - started
        if age is not None and age < timedelta(minutes=self.options.stale_minutes):
            self.log(
                f"{self.options.workflow} last started a successful {self.described_event} "
                f"run {iso(started)}, {minutes_of(age)} minute(s) ago"
            )
            self.resolve()
            return False
        if age is None:
            self.log(
                f"the Actions API lists no successful {self.described_event} run of "
                f"{self.options.workflow}"
            )
        else:
            self.log(
                f"{self.options.workflow} last started a successful {self.described_event} "
                f"run {iso(started)}, {minutes_of(age)} minute(s) ago, past the "
                f"{self.options.stale_minutes} this reports at"
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
        payload = {"title": TITLE, "body": body, "labels": [self.options.label]}
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
                {"body": f"`{self.options.workflow}` is running again, so this is done."},
            )
            self.api.send("PATCH", f"/issues/{number}", {"state": "closed"})
            self.log(f"closed {self.api.repository}#{number}")
        except (urllib.error.HTTPError, HostError) as error:
            self.log(f"could not close the issue: {error}")

    def body(self, started, run, age):
        if started is None:
            gap = (
                f"The Actions API lists no successful {self.described_event} run of "
                f"`{self.options.workflow}` at all."
            )
        else:
            gap = (
                f"`{self.options.workflow}` last started a successful "
                f"{self.described_event} run {iso(started)}, which is "
                f"{minutes_of(age)} minute(s) ago."
            )
        lines = [
            self.marker,
            gap,
            "",
            "GitHub starts a scheduled workflow only when it has room and retries nothing",
            "it drops, so a release published since then is not stamped yet and a client",
            "still sees the version before it.",
            "",
            f"Reported once the last success is older than {self.options.stale_minutes} "
            "minute(s).",
        ]
        if run and run.get("html_url"):
            lines.append(f"Last successful run: {run['html_url']}")
        lines += [
            "",
            f"A steward can start a tick now by dispatching `{self.options.workflow}`, "
            "which stamps everything that is waiting.",
            "That dispatch does not close this issue, because what is late is the "
            f"schedule. This issue closes by itself once a {self.described_event} run "
            "succeeds again.",
            "",
            f"Last checked {iso(now())}.",
        ]
        return "\n".join(lines)


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
        "plus that interval",
    )
    parser.add_argument("--label", default="area:infra")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="read everything, open and close nothing",
    )
    options = parser.parse_args(argv)
    if options.stale_minutes < 1:
        parser.error("--stale-minutes must be at least 1")
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
        Watchdog(api, options).run()
    except (urllib.error.HTTPError, HostError) as error:
        print(f"could not read the run list: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
