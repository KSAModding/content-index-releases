#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Start each job of this repository whose interval has passed.

GitHub starts only about one scheduled run an hour in this repository, and it
gives these runs in turn to every schedule entry, whatever the entry's cron
says. Each schedule entry besides the watcher's therefore takes ticks from the
watcher. So the watcher is the one workflow with a schedule, and at the end of
each tick this tool starts every other job that is due.

A job is due when its last run by the clock started at least its interval ago.
The run list is the whole state, so this tool holds none. A run by the clock is
a scheduled run, or a run that the job's own dispatch type started. A cancelled
run does not count, because it did nothing. A job is started with a
`repository_dispatch` whose type names that job only, because GitHub sends a
dispatch type to every workflow that listens for it.
"""

import argparse
import os
import sys
import urllib.error
from dataclasses import dataclass
from datetime import timedelta

import hosts
from hosts import HostError
from watch import Api, iso, now, parse_iso


@dataclass(frozen=True)
class Job:
    workflow: str
    event_type: str
    interval: timedelta
    # True when every run of the workflow does the work of a run by the clock.
    any_event: bool = False


# The interval is a lower bound, because a job starts only at the end of a tick.
JOBS = (
    # The master server knows only the current build, so this poll stays hourly.
    Job("game-versions.yml", "due-game-versions", timedelta(hours=1)),
    # Every push and every merge in the authored half builds the snapshot, so
    # this is only the backstop.
    Job("snapshot.yml", "due-snapshot", timedelta(hours=6), any_event=True),
    # A run asks every host once in 20 hours and only the share hosts otherwise.
    Job("download-counts.yml", "due-download-counts", timedelta(hours=6)),
    # The tool reads the forum at most once in 20 hours, so two runs a day keep
    # the read daily.
    Job("forums-status.yml", "due-forums-status", timedelta(hours=12)),
    # The examples job asks the real release hosts.
    Job("checks.yml", "due-checks", timedelta(days=7)),
)

# Ticks do not come at fixed times, so a job is due a little early rather than a
# whole tick late.
SLACK = timedelta(minutes=10)

# The newest runs one read looks at. Pushes and merges interleave with the runs
# by the clock, and a run older than this page is past every interval anyway.
LISTED_RUNS = 100


def by_clock(job, run):
    """Whether a run did the work the clock asks of the job."""
    if run.get("conclusion") == "cancelled" or run.get("event") == "pull_request":
        return False
    if job.any_event:
        return True
    if run.get("event") == "schedule":
        return True
    # The title of a run that a repository_dispatch started is its type.
    return run.get("event") == "repository_dispatch" and run.get("display_title") == job.event_type


def last_run(api, job):
    """When the newest run of the job by the clock was created, or None."""
    queries = [{}] if job.any_event else [{"event": "repository_dispatch"}, {"event": "schedule"}]
    newest = None
    for query in queries:
        answer = api.get(
            f"/actions/workflows/{job.workflow}/runs",
            per_page=LISTED_RUNS,
            exclude_pull_requests="true",
            **query,
        )
        for run in (answer or {}).get("workflow_runs") or []:
            if not by_clock(job, run):
                continue
            created = parse_iso(run.get("created_at"))
            if created is not None and (newest is None or created > newest):
                newest = created
    return newest


def is_due(job, last, moment):
    return last is None or moment - last >= job.interval - SLACK


def dispatch_due(api, jobs=JOBS, log=print):
    """Start every due job, and return how many reads or starts failed.

    A failure for one job does not stop the others.
    """
    failures = 0
    moment = now()
    for job in jobs:
        try:
            last = last_run(api, job)
        except (urllib.error.HTTPError, HostError) as error:
            log(f"{job.workflow}: could not read the run list: {error}")
            failures += 1
            continue
        since = "never" if last is None else f"{iso(last)}"
        if not is_due(job, last, moment):
            log(f"{job.workflow}: not due, last run by the clock {since}")
            continue
        try:
            api.send("POST", "/dispatches", {"event_type": job.event_type})
        except (urllib.error.HTTPError, HostError) as error:
            log(f"{job.workflow}: could not send {job.event_type}: {error}")
            failures += 1
            continue
        log(f"{job.workflow}: due, last run by the clock {since}, {job.event_type}")
    return failures


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Start each job whose interval has passed.")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY") or "KSAModding/content-index-releases",
        help="the repository the jobs live in",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="read the run lists and say which jobs are due, start nothing",
    )
    options = parser.parse_args(argv)
    options.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("INDEX_TOKEN")
    return options


def main(argv=None):
    options = parse_arguments(argv)
    if not options.token:
        print("no token in GITHUB_TOKEN: the jobs that are due are named, not started", file=sys.stderr)
    api = Api(hosts.Http(token=options.token), options.repository, options.dry_run)
    # A failure exits non-zero so that the watcher run shows it, after every
    # other job had its turn.
    return 1 if dispatch_due(api) else 0


if __name__ == "__main__":
    sys.exit(main())
