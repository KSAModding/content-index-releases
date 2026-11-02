#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Refresh forums-status.json from the Ahwoo forum sitemap (RFC 0110).

A run reads https://forums.ahwoo.com/sitemap.xml, and every sitemap a sitemap index names, at most once in 20 hours.
For a listed thread that the sitemap lacks, it reads that one thread once.
Ahwoo allowed exactly these two reads, with a User-Agent that names Borea and a contact link, and asked that only whether a thread exists is stored.

A listing here is every document with `links.forums`: a mod or mod loader, a vehicle, a save, and the newest version of a pack.
The file is the whole state, and it lives in this repository and not in the Actions cache, because a lost cache must not reset a count of gone runs or a mark.
The snapshot takes only the mark from it, as `forums.unavailable_since`.
"""

import argparse
import datetime
import gzip
import http.client
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import decide
import hosts
import watch
from authored import PACKS
from build_snapshot import (
    FORUMS_ENTRY_KEYS,
    FORUMS_STATUS_VERSION,
    SnapshotError,
    check_forums_status,
    check_ids_unique,
    info,
    instant,
    load_json,
    newest_first,
    read_all_listings,
    read_index_status,
    read_packs,
    serialize,
    warn,
)

USER_AGENT = "Borea-Index/1.0 (+https://github.com/KSAModding/Borea)"

FORUM = "https://forums.ahwoo.com/"
SITEMAP = "https://forums.ahwoo.com/sitemap.xml"
THREAD = "https://forums.ahwoo.com/threads/{thread}/"

# The text of the page Ahwoo answers with a 404 for a thread that does not exist.
NOT_FOUND = "The requested thread could not be found."

# The rule of the authored schema for links.forums, and the thread id in it, as check_index.py of content-index reads it.
SCHEMA = Path("schemas") / "authored.schema.json"
THREAD_ID = "[0-9]+"

# A run reads nothing when the last read is younger than this, so the forum sees one read a day.
DUE_HOURS = 20

# How many counted runs in a row have to find a thread gone before the listing is marked.
GONE_RUNS = 3

# How many counted runs in a row with an unclear answer go to a steward.
UNKNOWN_RUNS = 3

# A sitemap that lacks more than this share of the listed threads the last counted run saw does not count.
MISSING_SHARE = 0.10

# A read that does not count and is at least the third in a row ends with HELD_EXIT after the file is written.
# The workflow then fails after its push, so the stewards hear that the runs count nothing any more.
HELD_RUNS = 3
HELD_EXIT = 3

# A thread that moves answers with a 301, and a chain longer than this is not followed.
MAX_REDIRECTS = 5

# How many sitemaps one sitemap index may name.
MAX_SITEMAPS = 100

# The sitemap protocol limits one file to 50 MB, and a thread page is far smaller.
SITEMAP_LIMIT = 50 * 1024 * 1024
PAGE_LIMIT = 4 * 1024 * 1024

FOUND, GONE, UNKNOWN = "found", "gone", "unknown"

STEWARDS = "@KSAModding/content-manager-stewards"

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


class ForumError(Exception):
    """The forum gave no answer that can be read."""


class _Unfollowed(urllib.request.HTTPRedirectHandler):
    """Follows no redirect, so the caller sees each status the forum answers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read(answer, limit):
    body = answer.read(limit + 1)
    if len(body) > limit:
        raise ForumError(f"the answer is larger than {limit} bytes")
    return body


class Forum:
    """The one way to forums.ahwoo.com: one GET per call, without a retry and without a redirect."""

    def __init__(self, timeout=60):
        self.timeout = timeout
        self.requests = 0
        self._opener = urllib.request.build_opener(_Unfollowed())

    def get(self, url, limit):
        """(status, Location, body) of one GET. Raises ForumError when no answer came."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        self.requests += 1
        try:
            with self._opener.open(request, timeout=self.timeout) as answer:
                return answer.status, answer.headers.get("Location"), _read(answer, limit)
        except urllib.error.HTTPError as error:
            try:
                body = _read(error, limit) if error.fp is not None else b""
            except (OSError, http.client.HTTPException):
                body = b""
            return error.code, (error.headers or {}).get("Location"), body
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            raise ForumError(f"{url}: {error}") from error


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def local(tag):
    """An XML tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(body):
    """("sitemapindex" or "urlset", [loc]) of one sitemap file. Raises ValueError when it does not parse."""
    if body[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
                body = handle.read(SITEMAP_LIMIT + 1)
        except (OSError, EOFError) as error:
            raise ValueError(f"the gzip stream does not read: {error}") from error
        if len(body) > SITEMAP_LIMIT:
            raise ValueError(f"the sitemap is larger than {SITEMAP_LIMIT} bytes")
    # A sitemap has no document type, and refusing one keeps entity expansion out.
    if b"<!DOCTYPE" in body:
        raise ValueError("the sitemap declares a document type")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ValueError(f"the sitemap is not XML: {error}") from error
    kind = local(root.tag)
    items = {"sitemapindex": "sitemap", "urlset": "url"}.get(kind)
    if items is None:
        raise ValueError(f"the root element is {kind}, not sitemapindex or urlset")
    locations = []
    for item in root:
        if local(item.tag) != items:
            continue
        for child in item:
            if local(child.tag) == "loc":
                locations.append((child.text or "").strip())
    return kind, locations


def sitemap_threads(forum, pattern, log=warn):
    """The thread ids the sitemap lists, following a sitemap index, or None when the read does not count.

    `pattern` is the rule for links.forums, because the forum names a thread as /threads/<slug>.<id>/ or under
    the path of its forum as /forums/<path>/<slug>.<id>/, and the sitemap can use either form.
    """

    def fetch(url):
        status, _, body = forum.get(url, SITEMAP_LIMIT)
        if status != 200:
            raise ForumError(f"{url} answered {status}")
        return parse_sitemap(body)

    try:
        kind, locations = fetch(SITEMAP)
        if kind == "sitemapindex":
            if len(locations) > MAX_SITEMAPS:
                raise ForumError(f"the sitemap index names {len(locations)} sitemaps, more than {MAX_SITEMAPS}")
            pages = []
            for location in locations:
                if not location.startswith(FORUM):
                    raise ForumError(f"the sitemap index names {location!r}, which is not on the forum")
                child, found = fetch(location)
                if child != "urlset":
                    raise ForumError(f"{location} is a sitemap index inside a sitemap index")
                pages.extend(found)
            locations = pages
    except (ForumError, ValueError) as error:
        log(f"the sitemap could not be read, so this run does not count: {error}")
        return None
    threads = {int(match.group(1)) for location in locations if (match := pattern.match(location))}
    if not threads:
        log("the sitemap names no thread, so this run does not count")
        return None
    return threads


def thread_answer(forum, thread):
    """(FOUND, GONE or UNKNOWN, what the forum answered) for one read of a thread.

    Only a 301 is followed, and only on the forum, because rule 2 of RFC 0110 keeps a listing on a 200 or on a 301 that ends in 200.
    A 404 is gone only with Ahwoo's own page for a thread that does not exist, and after a redirect it is unknown.
    """
    url = THREAD.format(thread=thread)
    moved = False
    for _ in range(MAX_REDIRECTS + 1):
        try:
            status, location, body = forum.get(url, PAGE_LIMIT)
        except ForumError as error:
            return UNKNOWN, f"no answer: {error}"
        if status == 200:
            return FOUND, "a 301 that ends in 200" if moved else "200"
        if status == 301 and location:
            target = urllib.parse.urljoin(url, location)
            if not target.startswith(FORUM):
                return UNKNOWN, f"a 301 to {target}, which is not on the forum"
            url, moved = target, True
            continue
        if status == 404 and not moved and NOT_FOUND in body.decode("utf-8", "replace"):
            return GONE, "404 with the page for a thread that does not exist"
        return UNKNOWN, f"HTTP {status}" + (" after a 301" if moved else "")
    return UNKNOWN, f"more than {MAX_REDIRECTS} redirects"


def thread_pattern(authored):
    """The rule of the authored schema for links.forums, with the thread id captured."""
    path = Path(authored) / SCHEMA
    try:
        rule = load_json(path)["$defs"]["forumsUrl"]["pattern"]
    except (SnapshotError, KeyError, TypeError) as error:
        raise SnapshotError(f"{path}: the rule for links.forums cannot be read: {error}") from error
    if not isinstance(rule, str) or rule.count(THREAD_ID) != 1:
        raise SnapshotError(f"{path}: forumsUrl has no single thread id to capture")
    return re.compile(rule.replace(THREAD_ID, f"({THREAD_ID})"))


def thread_of(document, pattern):
    """The thread id that links.forums of `document` names, or None."""
    links = document.get("links")
    forums = links.get("forums") if isinstance(links, dict) else None
    match = pattern.match(forums) if isinstance(forums, str) else None
    return int(match.group(1)) if match else None


def subjects_of(authored):
    """Every document with a forums thread, keyed by lowercased id, as {id, thread, document, pack, delisted}."""
    check_ids_unique(authored)
    pattern = thread_pattern(authored)
    whole, _, _ = read_index_status(authored)
    documents = [
        (key, document, False)
        for group in read_all_listings(authored).values()
        for key, document in group.items()
    ]
    for key, pack in read_packs(authored).items():
        newest = newest_first(list(pack["versions"].values()), f"{PACKS}/{pack['id']}")[0]
        documents.append((key, newest, True))
    subjects = {}
    for key, document, pack in documents:
        thread = thread_of(document, pattern)
        if thread is None:
            continue
        status = whole.get(key)
        subjects[key] = {
            "id": document["id"],
            "thread": thread,
            "document": document,
            "pack": pack,
            "delisted": status is not None and status["state"] == "delisted",
        }
    return subjects


def ordered(entry):
    """An entry with its keys in the order of FORUMS_ENTRY_KEYS."""
    return {key: entry[key] for key in FORUMS_ENTRY_KEYS if key in entry}


def apply(entry, answer, moment):
    """Rules 2 to 4 and 7 for one counted run, and the note it calls for, or None."""
    if answer == FOUND:
        entry.pop("gone", None)
        entry.pop("unknown", None)
        if entry.pop("unavailable_since", None) is not None:
            entry["untold"] = "found"
            return "found"
        return None
    if answer == GONE:
        entry.pop("unknown", None)
        gone = entry.pop("gone", []) + [iso(moment)]
        if len(gone) < GONE_RUNS:
            entry["gone"] = gone
            return None
        entry["unavailable_since"] = gone[-1]
        entry["untold"] = "marked"
        return "marked"
    count = entry.get("unknown", 0) + 1
    entry["unknown"] = count
    if count == UNKNOWN_RUNS:
        entry["untold"] = "unknown"
        return "unknown"
    return None


def refresh(subjects, current, forum, pattern, moment, read=True, log=warn, note=info):
    """The refreshed document, and what the forum answered for each entry this run, by lowercased id.

    `subjects` is what subjects_of gives, `current` is the stored document or None, and `pattern` is thread_pattern.
    A read that does not count adds one to `held`, and a counted run removes it.
    With `read` false, or when the last read is younger than DUE_HOURS, the forum is not asked, and only a document that changed its forums thread or left the index changes its entry.
    """
    current = current or {"spec_version": FORUMS_STATUS_VERSION, "listings": []}
    stored = check_forums_status(current, "the stored forums status")

    entries = {}
    for key, subject in sorted(subjects.items()):
        old = stored.get(key)
        if old is not None and old["thread"] == subject["thread"]:
            entry = {**old, "id": subject["id"]}
        else:
            entry = {"id": subject["id"], "thread": subject["thread"], "in_sitemap": False}
            if old is not None and "unavailable_since" in old:
                note(f"{subject['id']}: links.forums names a new thread, so the mark is removed")
        entries[key] = entry
    for key in sorted(set(stored) - set(entries)):
        note(f"{stored[key]['id']}: names no forums thread any more, so its entry is dropped")

    document = {"spec_version": FORUMS_STATUS_VERSION}
    for key in ("checked_at", "counted_at", "held"):
        if key in current:
            document[key] = current[key]
    details = {}

    last = current.get("checked_at")
    due = last is None or moment - instant(last, "checked_at") >= datetime.timedelta(hours=DUE_HOURS)
    if read and not due:
        note(f"the forum was read at {last}, less than {DUE_HOURS} hours ago, so this run reads nothing")
    if read and due:
        document["checked_at"] = iso(moment)
        threads = sitemap_threads(forum, pattern, log)
        live = {key: entry for key, entry in entries.items() if not subjects[key]["delisted"]}
        if threads is not None:
            seen = {entry["thread"] for entry in live.values() if entry["in_sitemap"]}
            missing = seen - threads
            if len(missing) > MISSING_SHARE * len(seen):
                log(
                    f"the sitemap lacks {len(missing)} of the {len(seen)} listed threads the last "
                    "counted run saw, so this run does not count"
                )
                threads = None
        if threads is None:
            document["held"] = current.get("held", 0) + 1
        else:
            document.pop("held", None)
            document["counted_at"] = iso(moment)
            answers = {}
            for key, entry in sorted(live.items()):
                thread = entry["thread"]
                entry["in_sitemap"] = thread in threads
                if entry["in_sitemap"]:
                    answer, detail = FOUND, "in the sitemap"
                elif "unavailable_since" in entry:
                    # A marked thread is not read again, and the sitemap or a new links.forums removes the mark.
                    continue
                else:
                    if thread not in answers:
                        answers[thread] = thread_answer(forum, thread)
                    answer, detail = answers[thread]
                    note(f"{entry['id']}: thread {thread} is not in the sitemap, and reading it gave {detail}")
                apply(entry, answer, moment)
                details[key] = detail

    document["listings"] = [ordered(entries[key]) for key in sorted(entries)]
    return document, details


def mark_text(entry):
    return "\n".join(
        [
            f"The forums thread of `{entry['id']}`, {THREAD.format(thread=entry['thread'])}, was gone in "
            f"{GONE_RUNS} daily checks in a row, so the index marks this listing since "
            f"{entry['unavailable_since']} (RFC 0110).",
            "",
            "A client warns about it and does not offer it for a new install, and it never changes an "
            "installed copy. The mark goes when the thread is in the forum sitemap again, or when "
            "`links.forums` names a thread that exists. Whether the listing is delisted is for a steward to decide.",
        ]
    )


def found_text(entry):
    return (
        f"The forums thread of `{entry['id']}` is in the forum again, so the index removed its mark (RFC 0110)."
    )


def unknown_text(entry, detail):
    answer = f" The last answer was {detail}." if detail else ""
    return (
        f"The forums thread of `{entry['id']}`, {THREAD.format(thread=entry['thread'])}, is not in the "
        f"forum sitemap, and reading it gave no clear answer in {UNKNOWN_RUNS} daily checks in a row, "
        f"so the index marks nothing (RFC 0110).{answer}"
    )


class Teller:
    """Sends the notes of RFC 0110 to the watcher issue of a listing on the authored repository."""

    def __init__(self, authored, repository, token, label="watcher", stewards=STEWARDS, log=info, api=None):
        self.authored = Path(authored)
        self.token = token
        self.stewards = stewards
        self.log = log
        self.subjects = {}
        self.proofs = None
        http = hosts.Http(token=token, log=log)
        self.issues = watch.Issues(api or watch.Api(http, repository, log=log), label, log, owner=self.owner_line)
        self.cache = watch.Cache(None)

    def owner_line(self, listing_id):
        """The line that mentions the owner, or says why nobody is told, as the watcher writes it."""
        subject = self.subjects.get(listing_id)
        logins, reason = (), "the forums status could not read the listing"
        if subject is not None and subject["pack"]:
            logins, reason = self.pack_owner(subject["id"])
        elif subject is not None:
            if self.proofs is None:
                try:
                    self.proofs = decide.load_ownership(self.authored)
                except Exception as error:  # noqa: BLE001 - no owner is not a failed run
                    self.log(f"the owners are not looked up: {error!r}")
                    self.proofs = False
            if self.proofs:
                api = decide.Api(None, None, unavailable=self.proofs.Unavailable, public_token=self.token)
                logins, reason = decide.owner_logins(self.proofs, subject["document"], api)
            else:
                reason = "the ownership proofs of content-index did not load"
        if logins:
            return f"{decide.mentions(logins)}, the index tells you because you own this listing."
        return f"The index tells no owner, because no owner of this listing could be named: {reason}."

    def pack_owner(self, identifier):
        """The login `owner.json` of the pack names, as (logins, reason)."""
        path = self.authored / PACKS / identifier / "owner.json"
        try:
            login = json.loads(path.read_text(encoding="utf-8")).get("github_login")
        except (OSError, ValueError, AttributeError):
            return (), f"{PACKS}/{identifier}/owner.json does not read"
        if isinstance(login, str) and LOGIN.fullmatch(login):
            return (login,), ""
        return (), f"{PACKS}/{identifier}/owner.json names no GitHub login"

    def tell(self, subject, entry, kind, detail=None):
        """Send one note, and return whether it was sent."""
        self.subjects[subject["id"]] = subject
        identifier = subject["id"]
        if kind == "marked":
            text = f"{mark_text(entry)}\n\n{self.stewards}, for your review."
            return self.issues.note(identifier, text, self.cache, f"{identifier}: the forums thread is gone")
        if kind == "found":
            return self.issues.note(identifier, found_text(entry), self.cache, f"{identifier}: the forums thread is back")
        text = f"{unknown_text(entry, detail)}\n\n{self.stewards}, please look at the thread."
        return self.issues.note(
            identifier, text, self.cache, f"{identifier}: the forums thread could not be checked", tell_owner=False
        )


def tell_all(document, subjects, teller, details, log=warn):
    """Send every note the entries still owe, and keep the ones that could not be sent."""
    for entry in document["listings"]:
        kind = entry.get("untold")
        if kind is None:
            continue
        key = entry["id"].lower()
        if teller is None:
            log(f"{entry['id']}: the {kind} note waits for a run that can write issues")
            continue
        if teller.tell(subjects[key], entry, kind, details.get(key)):
            del entry["untold"]


def read_current(path):
    """The stored document, or None when there is no file yet."""
    if not path.is_file():
        return None
    document = load_json(path)
    check_forums_status(document, str(path))
    return document


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Refresh forums-status.json from the forum sitemap.")
    parser.add_argument(
        "--authored", default=".authored", type=Path,
        help="a checkout of the authored repository",
    )
    parser.add_argument(
        "--status", default="forums-status.json", type=Path,
        help="the document to refresh",
    )
    parser.add_argument(
        "--authored-repo", default="KSAModding/content-index",
        help="the authored repository, whose watcher issues get the notes",
    )
    parser.add_argument("--issue-label", default="watcher")
    parser.add_argument("--steward-team", default=STEWARDS)
    parser.add_argument(
        "--no-read", action="store_true",
        help="ask the forum nothing, only follow the documents that changed or left the index",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the document, write and tell nothing, and ask the forum nothing unless --read is given",
    )
    parser.add_argument(
        "--read", action="store_true",
        help="with --dry-run, ask the forum when a read is due, although the read is not saved and the next run reads again",
    )
    return parser.parse_args(argv)


def main(argv=None, forum=None, teller=None, moment=None):
    arguments = parse_arguments(argv)
    forum = forum or Forum()
    token = os.environ.get("GITHUB_TOKEN")
    if teller is None and token and not arguments.dry_run:
        teller = Teller(
            arguments.authored, arguments.authored_repo, token, arguments.issue_label, arguments.steward_team
        )

    # A dry run saves no checked_at, so a read in it would let the next run read the forum again the same day.
    read = not arguments.no_read and (arguments.read or not arguments.dry_run)
    try:
        current = read_current(arguments.status)
        subjects = subjects_of(arguments.authored)
        pattern = thread_pattern(arguments.authored)
        document, details = refresh(subjects, current, forum, pattern, moment or now(), read=read)
        if not arguments.dry_run:
            tell_all(document, subjects, teller, details)
        rendered = serialize(document)
        check_forums_status(json.loads(rendered), "the refreshed forums status")
    except SnapshotError as error:
        print(f"cannot refresh the forums status: {error}", file=sys.stderr)
        return 1

    if arguments.dry_run:
        sys.stdout.write(rendered)
    elif arguments.status.is_file() and arguments.status.read_bytes() == rendered.encode("utf-8"):
        info(f"{arguments.status} is already current, so nothing is written")
    else:
        arguments.status.parent.mkdir(parents=True, exist_ok=True)
        with arguments.status.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        info(f"wrote {arguments.status}")

    marked = sum("unavailable_since" in entry for entry in document["listings"])
    info(f"{len(document['listings'])} listing(s) with a forums thread, {marked} marked, {forum.requests} forum request(s)")
    held = document.get("held", 0)
    if held >= HELD_RUNS and held > (current or {}).get("held", 0):
        print(
            f"the last {held} reads of the forum did not count, so no thread is checked until a steward looks",
            file=sys.stderr,
        )
        return HELD_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
