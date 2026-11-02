#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the forums status refresh: no network, a fake forum answers by URL.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

import forums_status
from build_snapshot import build
from forums_status import NOT_FOUND, SITEMAP, USER_AGENT, ForumError, Forum, Teller, main
from test_watch import StubApi

START = datetime.datetime(2026, 11, 2, 4, 9, tzinfo=datetime.timezone.utc)

# The rule of the authored schema for links.forums.
FORUMS_URL = (
    "^https://forums\\.ahwoo\\.com/(?:index\\.php\\?)?(?:threads/|forums/(?:[^\\s/?#\\u0000-\\u001f]+/)+)"
    "(?:[^\\s/?#\\u0000-\\u001f]*\\.)?[0-9]+(?:[/?#][^\\s\\u0000-\\u001f]*)?(?![\\s\\S])"
)

LISTING = """\
spec_version = 1
id = "{identifier}"
type = "{content_type}"
name = "{identifier}"
authors = ["Maxi"]
abstract = "One sentence."
license = "MIT"

[links]
forums = "https://forums.ahwoo.com/threads/{identifier}.{thread}/"

[compatibility]
game_min = "2026.8.3.5117"
"""

PACK = """\
spec_version = 1
id = "{identifier}"
type = "modpack"
name = "{identifier}"
authors = ["Maxi"]
abstract = "One sentence."
license = "CC0-1.0"
version = "{version}"
released_at = "2026-08-05T12:00:00Z"

[links]
forums = "https://forums.ahwoo.com/threads/{identifier}.{thread}/"

[compatibility]
game_min = "2026.8.3.5117"

[[mods]]
id = "Alpha"
version = "1.0.0"
"""

GONE_PAGE = (
    b"<!DOCTYPE html><html><head><title>Oops! We ran into some problems. | Ahwoo Forums</title></head>"
    b"<body><div class=\"blockMessage\">" + NOT_FOUND.encode() + b"</div></body></html>"
)


def thread_url(thread):
    return f"https://forums.ahwoo.com/threads/{thread}/"


def urlset(*threads):
    """A sitemap that lists `threads` and one page that is not a thread."""
    rows = "".join(
        f"<url><loc>https://forums.ahwoo.com/threads/t.{thread}/</loc><lastmod>2026-11-01</lastmod></url>"
        for thread in threads
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://forums.ahwoo.com/forums/kitten-space-agency.7/</loc></url>"
        f"{rows}</urlset>"
    ).encode()


def sitemap_index(*locations):
    rows = "".join(f"<sitemap><loc>{location}</loc></sitemap>" for location in locations)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</sitemapindex>'
    ).encode()


def ok(body):
    return (200, None, body)


class FakeForum:
    """Answers by URL with (status, Location, body), or raises what the route holds, and keeps every request."""

    def __init__(self, routes):
        self.routes = routes
        self.asked = []

    @property
    def requests(self):
        return len(self.asked)

    def get(self, url, limit):
        self.asked.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected URL {url}")
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeTeller:
    def __init__(self, sent=True):
        self.sent = sent
        self.told = []

    def tell(self, subject, entry, kind, detail=None):
        self.told.append((subject["id"], kind))
        return self.sent


class Case(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.authored = self.root / "authored"
        self.status = self.root / "forums-status.json"
        self.write(
            self.authored / "schemas" / "authored.schema.json",
            json.dumps({"$defs": {"forumsUrl": {"pattern": FORUMS_URL}}}),
        )
        (self.authored / "packs").mkdir(parents=True)
        self.listing("Alpha", 1)
        self.listing("Beta", 2)
        self.teller = FakeTeller()

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def listing(self, identifier, thread, content_type="mod", folder="listings"):
        text = LISTING.format(identifier=identifier, thread=thread, content_type=content_type)
        self.write(self.authored / folder / f"{identifier}.toml", text)

    def run_at(self, hours, routes, *arguments, expect=0):
        forum = FakeForum(routes)
        code = main(
            ["--authored", str(self.authored), "--status", str(self.status), *arguments],
            forum=forum,
            teller=self.teller,
            moment=START + datetime.timedelta(hours=hours),
        )
        self.assertEqual(code, expect)
        return forum

    def document(self):
        return json.loads(self.status.read_text(encoding="utf-8"))

    def entry(self, identifier):
        for entry in self.document()["listings"]:
            if entry["id"] == identifier:
                return entry
        self.fail(f"no entry for {identifier}")

    def gone_runs(self, runs, routes=None):
        """`runs` daily runs in which the sitemap lacks Alpha's thread and Ahwoo says it does not exist."""
        routes = routes or {SITEMAP: ok(urlset(2)), thread_url(1): (404, None, GONE_PAGE)}
        for day in range(runs):
            self.run_at(24 * day, routes)


class Sitemap(Case):
    def test_a_thread_in_the_sitemap_keeps_the_listing_and_reads_no_thread(self):
        forum = self.run_at(0, {SITEMAP: ok(urlset(1, 2))})
        self.assertEqual(forum.asked, [SITEMAP])
        self.assertEqual(
            self.document()["listings"],
            [
                {"id": "Alpha", "thread": 1, "in_sitemap": True},
                {"id": "Beta", "thread": 2, "in_sitemap": True},
            ],
        )
        self.assertEqual(self.document()["counted_at"], "2026-11-02T04:09:00Z")

    def test_a_thread_the_sitemap_names_under_its_forum_is_in_the_sitemap(self):
        body = urlset(2).replace(
            b"</urlset>",
            b"<url><loc>https://forums.ahwoo.com/forums/kitten-space-agency/mod-releases/alpha.1/</loc></url></urlset>",
        )
        forum = self.run_at(0, {SITEMAP: ok(body)})
        self.assertEqual(forum.asked, [SITEMAP])
        self.assertTrue(self.entry("Alpha")["in_sitemap"])
        self.assertTrue(self.entry("Beta")["in_sitemap"])

    def test_a_sitemap_index_is_followed(self):
        first, second = "https://forums.ahwoo.com/sitemap-1.xml", "https://forums.ahwoo.com/sitemap-2.xml"
        routes = {SITEMAP: ok(sitemap_index(first, second)), first: ok(urlset(1)), second: ok(urlset(2))}
        forum = self.run_at(0, routes)
        self.assertEqual(forum.asked, [SITEMAP, first, second])
        self.assertTrue(self.entry("Alpha")["in_sitemap"])
        self.assertTrue(self.entry("Beta")["in_sitemap"])

    def test_a_sitemap_index_that_names_another_host_does_not_count(self):
        routes = {SITEMAP: ok(sitemap_index("https://example.com/sitemap-1.xml"))}
        self.run_at(0, routes)
        self.assertNotIn("counted_at", self.document())
        self.assertFalse(self.entry("Alpha")["in_sitemap"])

    def test_a_sitemap_that_answers_otherwise_or_does_not_parse_does_not_count(self):
        answers = ((503, None, b""), (429, None, b""), ok(b"<html>no sitemap</html>"), ok(urlset()), ForumError("reset"))
        for answer in answers:
            self.status.unlink(missing_ok=True)
            forum = self.run_at(0, {SITEMAP: answer})
            self.assertEqual(forum.asked, [SITEMAP], msg=repr(answer))
            self.assertNotIn("counted_at", self.document(), msg=repr(answer))
            self.assertEqual(self.document()["checked_at"], "2026-11-02T04:09:00Z")

    def test_a_sitemap_that_lacks_more_than_ten_percent_does_not_count(self):
        for thread in range(3, 11):
            self.listing(f"Mod{thread}", thread)
        everything = list(range(1, 11))
        self.run_at(0, {SITEMAP: ok(urlset(*everything))})

        # One of ten is ten percent, which still counts.
        self.run_at(24, {SITEMAP: ok(urlset(*everything[1:])), thread_url(1): (404, None, GONE_PAGE)})
        self.assertEqual(self.entry("Alpha")["gone"], ["2026-11-03T04:09:00Z"])
        self.assertEqual(self.document()["counted_at"], "2026-11-03T04:09:00Z")

        # The last counted run saw nine, and two of them missing is more than ten percent.
        forum = self.run_at(48, {SITEMAP: ok(urlset(*everything[3:]))})
        self.assertEqual(forum.asked, [SITEMAP])
        self.assertEqual(self.document()["counted_at"], "2026-11-03T04:09:00Z")
        self.assertEqual(self.document()["checked_at"], "2026-11-04T04:09:00Z")
        self.assertTrue(self.entry("Beta")["in_sitemap"])
        self.assertEqual(self.entry("Alpha")["gone"], ["2026-11-03T04:09:00Z"])

    def test_the_third_read_in_a_row_that_does_not_count_fails_and_a_counted_run_clears_it(self):
        self.run_at(0, {SITEMAP: (503, None, b"")})
        self.run_at(24, {SITEMAP: (503, None, b"")})
        self.assertEqual(self.document()["held"], 2)
        self.run_at(48, {SITEMAP: (503, None, b"")}, expect=forums_status.HELD_EXIT)
        self.assertEqual(self.document()["held"], 3)
        self.run_at(50, {}, "--no-read")
        self.run_at(72, {SITEMAP: ok(urlset(1, 2))})
        self.assertNotIn("held", self.document())

    def test_a_run_less_than_20_hours_after_the_last_read_asks_nothing(self):
        self.run_at(0, {SITEMAP: ok(urlset(1, 2))})
        before = self.status.read_bytes()
        forum = self.run_at(19.9, {})
        self.assertEqual(forum.asked, [])
        self.assertEqual(self.status.read_bytes(), before)
        forum = self.run_at(20, {SITEMAP: ok(urlset(1, 2))})
        self.assertEqual(forum.asked, [SITEMAP])

    def test_no_read_asks_nothing(self):
        forum = self.run_at(0, {}, "--no-read")
        self.assertEqual(forum.asked, [])
        self.assertNotIn("checked_at", self.document())


class Threads(Case):
    def test_three_runs_that_find_ahwoos_not_found_page_mark_the_listing(self):
        self.gone_runs(2)
        self.assertEqual(self.entry("Alpha")["gone"], ["2026-11-02T04:09:00Z", "2026-11-03T04:09:00Z"])
        self.assertNotIn("unavailable_since", self.entry("Alpha"))
        self.assertEqual(self.teller.told, [])

        self.run_at(48, {SITEMAP: ok(urlset(2)), thread_url(1): (404, None, GONE_PAGE)})
        alpha = self.entry("Alpha")
        self.assertEqual(alpha, {"id": "Alpha", "thread": 1, "in_sitemap": False, "unavailable_since": "2026-11-04T04:09:00Z"})
        self.assertEqual(self.teller.told, [("Alpha", "marked")])
        self.assertEqual(self.entry("Beta"), {"id": "Beta", "thread": 2, "in_sitemap": True})

    def test_a_marked_thread_is_not_read_again(self):
        self.gone_runs(3)
        forum = self.run_at(72, {SITEMAP: ok(urlset(2))})
        self.assertEqual(forum.asked, [SITEMAP])
        self.assertEqual(self.entry("Alpha")["unavailable_since"], "2026-11-04T04:09:00Z")
        self.assertEqual(self.teller.told, [("Alpha", "marked")])

    def test_the_first_counted_run_that_finds_the_thread_removes_the_mark(self):
        self.gone_runs(3)
        self.run_at(72, {SITEMAP: ok(urlset(1, 2))})
        self.assertEqual(self.entry("Alpha"), {"id": "Alpha", "thread": 1, "in_sitemap": True})
        self.assertEqual(self.teller.told, [("Alpha", "marked"), ("Alpha", "found")])

    def test_a_new_links_forums_removes_the_mark_without_a_read(self):
        self.gone_runs(3)
        self.listing("Alpha", 5)
        forum = self.run_at(73, {}, "--no-read")
        self.assertEqual(forum.asked, [])
        self.assertEqual(self.entry("Alpha"), {"id": "Alpha", "thread": 5, "in_sitemap": False})
        self.assertEqual(self.teller.told, [("Alpha", "marked")])

    def test_a_301_that_ends_in_200_keeps_the_listing(self):
        self.gone_runs(2)
        moved = "https://forums.ahwoo.com/threads/alpha.1/"
        self.run_at(48, {SITEMAP: ok(urlset(2)), thread_url(1): (301, moved, b""), moved: ok(b"<html>thread</html>")})
        self.assertEqual(self.entry("Alpha"), {"id": "Alpha", "thread": 1, "in_sitemap": False})

    def test_any_other_answer_is_unknown_and_changes_nothing(self):
        moved = "https://forums.ahwoo.com/threads/alpha.1/"
        answers = [
            (503, None, b""),
            (429, None, b""),
            (404, None, b"<html>Not Found</html>"),
            (302, moved, b""),
            ForumError("timed out"),
        ]
        for answer in answers:
            with self.subTest(answer=repr(answer)):
                self.status.unlink(missing_ok=True)
                self.gone_runs(2)
                self.run_at(48, {SITEMAP: ok(urlset(2)), thread_url(1): answer})
                alpha = self.entry("Alpha")
                self.assertEqual(alpha["gone"], ["2026-11-02T04:09:00Z", "2026-11-03T04:09:00Z"])
                self.assertEqual(alpha["unknown"], 1)
                self.assertNotIn("unavailable_since", alpha)

    def test_a_404_page_after_a_301_is_unknown(self):
        moved = "https://forums.ahwoo.com/threads/alpha.1/"
        self.run_at(0, {SITEMAP: ok(urlset(2)), thread_url(1): (301, moved, b""), moved: (404, None, GONE_PAGE)})
        self.assertEqual(self.entry("Alpha")["unknown"], 1)
        self.assertNotIn("gone", self.entry("Alpha"))

    def test_unknown_answers_do_not_break_a_run_of_gone_ones(self):
        self.gone_runs(2)
        self.run_at(48, {SITEMAP: ok(urlset(2)), thread_url(1): (503, None, b"")})
        self.run_at(72, {SITEMAP: ok(urlset(2)), thread_url(1): (404, None, GONE_PAGE)})
        self.assertEqual(self.entry("Alpha")["unavailable_since"], "2026-11-05T04:09:00Z")

    def test_three_unknown_answers_in_a_row_go_to_a_steward_and_mark_nothing(self):
        self.gone_runs(4, {SITEMAP: ok(urlset(2)), thread_url(1): (403, None, b"")})
        alpha = self.entry("Alpha")
        self.assertEqual(alpha["unknown"], 4)
        self.assertNotIn("unavailable_since", alpha)
        self.assertEqual(self.teller.told, [("Alpha", "unknown")])

    def test_a_thread_two_documents_name_is_read_once(self):
        self.write(
            self.authored / "packs" / "Bundle" / "1.0.0.toml",
            PACK.format(identifier="Bundle", version="1.0.0", thread=1),
        )
        forum = self.run_at(0, {SITEMAP: ok(urlset(2)), thread_url(1): (404, None, GONE_PAGE)})
        self.assertEqual(forum.asked, [SITEMAP, thread_url(1)])
        self.assertEqual(self.entry("Bundle")["gone"], ["2026-11-02T04:09:00Z"])
        self.assertEqual(self.entry("Alpha")["gone"], ["2026-11-02T04:09:00Z"])

    def test_vehicles_saves_and_the_newest_pack_version_count(self):
        self.listing("Rover", 3, "vehicle", "vehicles")
        self.listing("Orbit", 4, "save", "saves")
        self.write(self.authored / "packs" / "Bundle" / "1.0.0.toml", PACK.format(identifier="Bundle", version="1.0.0", thread=8))
        self.write(self.authored / "packs" / "Bundle" / "1.1.0.toml", PACK.format(identifier="Bundle", version="1.1.0", thread=9))
        self.run_at(0, {SITEMAP: ok(urlset(1, 2, 3, 4, 9))})
        self.assertEqual(
            [(entry["id"], entry["thread"], entry["in_sitemap"]) for entry in self.document()["listings"]],
            [("Alpha", 1, True), ("Beta", 2, True), ("Bundle", 9, True), ("Orbit", 4, True), ("Rover", 3, True)],
        )

    def test_a_delisted_listing_is_not_read(self):
        self.write(
            self.authored / "index-status.toml",
            '[[entries]]\nid = "Alpha"\nstate = "delisted"\nsince = "2026-10-01T00:00:00Z"\nreason = "Gone."\n',
        )
        forum = self.run_at(0, {SITEMAP: ok(urlset(2))})
        self.assertEqual(forum.asked, [SITEMAP])
        self.assertEqual(self.entry("Alpha"), {"id": "Alpha", "thread": 1, "in_sitemap": False})

    def test_a_note_that_could_not_be_sent_is_sent_by_a_later_run(self):
        self.teller = FakeTeller(sent=False)
        self.gone_runs(3)
        self.assertEqual(self.entry("Alpha")["untold"], "marked")
        self.teller = FakeTeller()
        forum = self.run_at(49, {})
        self.assertEqual(forum.asked, [])
        self.assertEqual(self.teller.told, [("Alpha", "marked")])
        self.assertNotIn("untold", self.entry("Alpha"))

    def test_a_dry_run_writes_and_tells_nothing(self):
        self.gone_runs(2)
        before = self.status.read_bytes()
        routes = {SITEMAP: ok(urlset(2)), thread_url(1): (404, None, GONE_PAGE)}
        forum = self.run_at(48, routes, "--dry-run")
        self.assertEqual(forum.asked, [])
        forum = self.run_at(48, routes, "--dry-run", "--read")
        self.assertEqual(forum.asked, [SITEMAP, thread_url(1)])
        self.assertEqual(self.status.read_bytes(), before)
        self.assertEqual(self.teller.told, [])

    def test_the_snapshot_carries_only_the_mark(self):
        self.gone_runs(3)
        releases = self.root / "releases"
        releases.mkdir()
        versions = self.root / "game-versions.json"
        versions.write_text('{"spec_version": 1, "versions": []}', encoding="utf-8")
        snapshot = build(self.authored, releases, versions, log=lambda _: None, forums_status=self.status)
        entries = {entry["id"]: entry for entry in snapshot["listings"]}
        self.assertEqual(entries["Alpha"]["forums"], {"unavailable_since": "2026-11-04T04:09:00Z"})
        self.assertNotIn("forums", entries["Beta"])


class Telling(Case):
    def teller_with(self, api):
        teller = Teller(self.authored, "KSAModding/content-index", None, log=lambda _: None, api=api)
        teller.proofs = False
        return teller

    def test_a_mark_mentions_the_stewards_and_the_owner(self):
        self.write(self.authored / "packs" / "Bundle" / "owner.json", '{"github_login": "renancamm"}')
        api = StubApi({"/issues": []})
        subject = {"id": "Bundle", "thread": 9, "document": {}, "pack": True, "delisted": False}
        entry = {"id": "Bundle", "thread": 9, "unavailable_since": "2026-11-04T04:09:00Z"}
        self.assertTrue(self.teller_with(api).tell(subject, entry, "marked"))
        created = api.sent[0][2]
        self.assertEqual(created["title"], "Bundle: the forums thread is gone")
        self.assertIn("<!-- watcher:listing=Bundle -->", created["body"])
        self.assertIn("@KSAModding/content-manager-stewards", created["body"])
        self.assertIn("@renancamm, the index tells you", created["body"])
        self.assertIn("since 2026-11-04T04:09:00Z", created["body"])

    def test_unknown_answers_go_to_the_stewards_only(self):
        issue = {"number": 7, "state": "open", "body": "<!-- watcher:listing=Alpha -->"}
        api = StubApi({"/issues": [issue]})
        subject = {"id": "Alpha", "thread": 1, "document": {"id": "Alpha"}, "pack": False, "delisted": False}
        self.assertTrue(self.teller_with(api).tell(subject, {"id": "Alpha", "thread": 1}, "unknown", "HTTP 403"))
        [(method, path, payload)] = api.sent
        self.assertEqual((method, path), ("POST", "/issues/7/comments"))
        self.assertIn("@KSAModding/content-manager-stewards", payload["body"])
        self.assertIn("HTTP 403", payload["body"])
        self.assertNotIn("owner", payload["body"])


class Requests(unittest.TestCase):
    def test_a_request_names_borea_and_follows_no_redirect(self):
        class Opener:
            def open(self, request, timeout):
                self.request = request
                raise forums_status.urllib.error.HTTPError(
                    request.full_url, 301, "Moved", {"Location": "/threads/a.1/"}, None
                )

        forum = Forum()
        forum._opener = Opener()
        self.assertEqual(forum.get(thread_url(1), 100), (301, "/threads/a.1/", b""))
        self.assertEqual(forum._opener.request.get_header("User-agent"), USER_AGENT)
        self.assertIsInstance(forum._opener, Opener)
        self.assertTrue(issubclass(forums_status._Unfollowed, forums_status.urllib.request.HTTPRedirectHandler))
        self.assertIsNone(forums_status._Unfollowed().redirect_request(None, None, 301, "", {}, "https://x/"))


if __name__ == "__main__":
    unittest.main()
