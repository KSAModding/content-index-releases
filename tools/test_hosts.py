#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the host adapters: no network, a fake Http answers or raises.

The point of these is the HostError/StampError split on the failure paths: an
HTTPError that escapes either becomes an "unexpected" log line with no
author-facing issue, which is exactly what the watcher must not do.
"""

import hashlib
import http.client
import json
import unittest
import urllib.error
from unittest.mock import patch

from hosts import (
    MAX_ARCHIVE_BYTES,
    MAX_RESPONSE_BYTES,
    GitHubHost,
    HostError,
    HostRelease,
    Http,
    OversizeError,
    Response,
    SpaceDockHost,
    _utc,
    download,
    named,
)
from stamp_release import Archive, StampError


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "boom", {}, None)


class FakeHttp:
    """Answers by URL prefix, or raises what the route holds."""

    token = None
    timeout = 1

    def __init__(self, routes):
        self.routes = routes
        self.lines = []

    def log(self, message):
        self.lines.append(message)

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected URL {url}")

    def archive(self, url, api=False, limit=None):
        answer = self.get(url, api=api)
        return Archive.of_bytes(answer.body), answer.headers


def release(url, candidates=(), size=None):
    return HostRelease(
        host="github", tag="v1.0.0", version="1.0.0",
        release_date="2020-01-01T00:00:00Z", url=url,
        candidates=tuple(candidates), size=size,
    )


class Recording(FakeHttp):
    """Answers every URL and keeps the `api` flag each request carried."""

    def __init__(self):
        super().__init__({})
        self.flags = []

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        self.flags.append(api)
        return Response(200, {}, b"zip")


class Downloads(unittest.TestCase):
    def test_the_token_goes_to_the_github_api_host_only(self):
        # The flag is what sends the bearer token, and a URL a submission names
        # is the author's, so a look-alike host or a userinfo part must not
        # collect it.
        http = Recording()
        for url in (
            "https://api.github.com.attacker.example/x.zip",
            "https://api.github.com@attacker.example/x.zip",
            "https://github.com/o/r/releases/download/x.zip",
            "https://api.github.com/repos/o/r/releases/assets/1",
            "https://API.GITHUB.COM:443/repos/o/r/releases/assets/1",
        ):
            download(http, release(url))
        self.assertEqual(http.flags, [False, False, False, True, True])

    def test_a_gone_archive_is_a_stamp_error(self):
        # 404, 410 and 451 are facts about the release, reported to the author.
        for code in (404, 410, 451):
            http = FakeHttp({"https://github.com/": http_error(code)})
            with self.assertRaises(StampError, msg=code):
                download(http, release("https://github.com/o/r/releases/download/x.zip"))

    def test_any_other_http_error_is_a_host_error(self):
        http = FakeHttp({"https://github.com/": http_error(403)})
        with self.assertRaises(HostError):
            download(http, release("https://github.com/o/r/releases/download/x.zip"))

    def test_an_oversized_archive_is_a_stamp_error_not_a_retry(self):
        # Oversize is permanent: as a HostError the watcher would download and
        # discard the archive every tick and no issue would ever open.
        with self.assertRaises(StampError):
            download(FakeHttp({}), release("https://github.com/o/r/x.zip",
                                            size=MAX_ARCHIVE_BYTES + 1))
        http = FakeHttp({"https://github.com/": OversizeError("too large")})
        with self.assertRaises(StampError):
            download(http, release("https://github.com/o/r/x.zip"))


class Answer:
    """What urlopen returns, serving `body` and counting how much of it was read.

    `broken_at` ends the body there, the way a dropped connection does: the
    real HTTPResponse.read returns a short read then, it does not raise.
    """

    status = 200

    def __init__(self, body, headers=None, broken_at=None):
        self.body = body
        self.headers = headers or {}
        self.served = body if broken_at is None else body[:broken_at]
        self.read_bytes = 0

    def read(self, amount=-1):
        end = len(self.served) if amount < 0 else min(len(self.served), self.read_bytes + amount)
        chunk = self.served[self.read_bytes:end]
        self.read_bytes = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *details):
        return False


class Streaming(unittest.TestCase):
    """The real Http against a fake urlopen, with a chunk far smaller than a MiB."""

    URL = "https://github.com/o/r/releases/download/x.zip"

    def setUp(self):
        for patcher in (
            patch("hosts.CHUNK_BYTES", 7),
            patch("hosts.time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def serve(self, *answers):
        opener = patch("hosts.urllib.request.urlopen", side_effect=list(answers))
        opener.start()
        self.addCleanup(opener.stop)

    def test_digest_and_size_are_those_of_the_bytes(self):
        body = bytes(range(256)) * 3
        self.serve(Answer(body, {"Content-Type": "application/zip"}))
        archive, headers = Http().archive(self.URL)
        with archive:
            self.assertEqual(archive.sha256, hashlib.sha256(body).hexdigest().upper())
            self.assertEqual(archive.size, len(body))
            self.assertEqual(archive.file.read(), body)
        self.assertEqual(headers["Content-Type"], "application/zip")

    def test_a_content_length_above_the_limit_rejects_before_the_body_is_read(self):
        answer = Answer(b"x" * 100, {"Content-Length": "100"})
        self.serve(answer)
        with self.assertRaises(OversizeError) as raised:
            Http().archive(self.URL, limit=99)
        self.assertEqual(answer.read_bytes, 0)
        self.assertIn("above the", str(raised.exception))

    def test_a_body_without_content_length_is_cut_off_at_the_limit(self):
        # The streamed count is the backstop for a host that sends no length,
        # and it stops within one chunk of the limit instead of reading on.
        answer = Answer(b"x" * 1000)
        self.serve(answer)
        with self.assertRaises(OversizeError):
            Http().archive(self.URL, limit=20)
        self.assertLessEqual(answer.read_bytes, 20 + 7)

    def test_an_oversized_download_is_a_stamp_error(self):
        self.serve(Answer(b"", {"Content-Length": str(MAX_ARCHIVE_BYTES + 1)}))
        with self.assertRaises(StampError) as raised:
            download(Http(), release(self.URL))
        self.assertIn("4,096 MiB", str(raised.exception))

    def test_a_body_that_ends_before_its_length_is_retried_from_the_start(self):
        # A connection that drops midway gives a short read, not an error. The
        # length is what tells, and the next attempt must not have the first
        # attempt's bytes in front of its own, or the digest would be of neither.
        body = b"0123456789" * 5
        headers = {"Content-Length": str(len(body))}
        self.serve(Answer(body, headers, broken_at=23), Answer(body, headers))
        archive, _ = Http().archive(self.URL)
        with archive:
            self.assertEqual(archive.size, len(body))
            self.assertEqual(archive.sha256, hashlib.sha256(body).hexdigest().upper())

    def test_a_body_that_keeps_ending_early_is_a_host_error(self):
        body = b"0123456789" * 5
        headers = {"Content-Length": str(len(body))}
        self.serve(*(Answer(body, headers, broken_at=23) for _ in range(3)))
        with self.assertRaises(HostError):
            Http().archive(self.URL)

    def test_an_invalid_url_is_not_retried(self):
        # InvalidURL is an HTTPException too, and it is the author's URL, not
        # the host having a bad moment: it reaches the caller after one try.
        opener = patch("hosts.urllib.request.urlopen", side_effect=http.client.InvalidURL("a space"))
        urlopen = opener.start()
        self.addCleanup(opener.stop)
        with self.assertRaises(http.client.InvalidURL):
            Http().archive("https://example.com/a b.zip")
        self.assertEqual(urlopen.call_count, 1)

    def test_an_api_answer_has_its_own_smaller_limit(self):
        # The archive limit is for files on disk. JSON is held in memory.
        self.assertLess(MAX_RESPONSE_BYTES, MAX_ARCHIVE_BYTES)
        with patch("hosts.MAX_RESPONSE_BYTES", 10):
            self.serve(Answer(b"[1,2,3,4,5]"), Answer(b"[1,2,3,4,5]"))
            with self.assertRaises(OversizeError):
                Http().get("https://api.github.com/repos/o/r/releases", api=True)
            archive, _ = Http().archive(self.URL)
            with archive:
                self.assertEqual(archive.size, 11)


class Pagination(unittest.TestCase):
    def test_an_error_on_a_later_page_is_a_host_error(self):
        # A 404 from page two must not unwind into the watcher's broad catch.
        first = Response(
            200,
            {"Link": '<https://api.github.com/page2>; rel="next"'},
            b"[]",
        )
        http = FakeHttp({
            "https://api.github.com/repos/o/r/releases": first,
            "https://api.github.com/page2": http_error(404),
        })
        with self.assertRaises(HostError):
            GitHubHost("o/r", http).releases()

    def test_a_missing_repository_stays_a_stamp_error(self):
        http = FakeHttp({"https://api.github.com/repos/o/r/releases": http_error(404)})
        with self.assertRaises(StampError):
            GitHubHost("o/r", http).releases()

    def test_a_page_that_is_not_json_is_a_host_error(self):
        # A 200 carrying proxy HTML is the host having a bad moment, and it
        # must not land in the watcher's broad catch as "unexpected".
        http = FakeHttp({
            "https://api.github.com/repos/o/r/releases": Response(200, {}, b"<html>"),
        })
        with self.assertRaises(HostError):
            GitHubHost("o/r", http).releases()

    def test_a_scan_past_max_pages_says_so(self):
        first = Response(
            200,
            {"Link": '<https://api.github.com/page2>; rel="next"'},
            b"[]",
        )
        http = FakeHttp({"https://api.github.com/repos/o/r/releases": first})
        host = GitHubHost("o/r", http, max_pages=1)
        host.releases()
        self.assertTrue(host.truncated)


class SpaceDock(unittest.TestCase):
    def test_a_non_numeric_id_is_a_stamp_error(self):
        # A ValueError would escape the HostError/StampError split entirely.
        with self.assertRaises(StampError):
            SpaceDockHost("abc", FakeHttp({}))
        SpaceDockHost("4253", FakeHttp({}))

    def payload(self, download_path, page="/mod/1"):
        body = json.dumps({
            "url": page,
            "versions": [{"friendly_version": "1.0.0", "created": "2020-01-01T00:00:00Z",
                          "download_path": download_path}],
        }).encode()
        return FakeHttp({"https://spacedock.info/api/mod/1": Response(200, {}, body)})

    def test_a_download_path_stays_on_the_host(self):
        releases, _ = SpaceDockHost(1, self.payload("/mod/1/x/download/1.0.0")).releases()
        self.assertEqual(releases[0].url, "https://spacedock.info/mod/1/x/download/1.0.0")

    def test_a_download_path_leaving_the_host_is_rejected(self):
        # urljoin returns an absolute or protocol-relative value unchanged, so
        # an unchecked join publishes a foreign address into a stamped file.
        for escape in ("https://evil.example/x.zip", "//evil.example/x.zip",
                       "http://evil.example/x.zip"):
            with self.assertRaises(StampError, msg=escape):
                SpaceDockHost(1, self.payload(escape)).releases()

    def test_a_mod_page_leaving_the_host_is_rejected(self):
        # changelog is published as a link and no checksum gates it.
        with self.assertRaises(StampError):
            SpaceDockHost(1, self.payload("/ok", page="https://evil.example/")).releases()


class DownloadCounts(unittest.TestCase):
    @staticmethod
    def payload(*assets):
        return {
            "tag_name": "v1.0.0",
            "published_at": "2020-01-01T00:00:00Z",
            "assets": [
                {"state": "uploaded", "name": name, "content_type": "application/zip",
                 "download_count": count, "browser_download_url": f"https://github.com/o/r/{name}"}
                for name, count in assets
            ],
        }

    def test_github_reports_the_count_of_the_selected_archive_only(self):
        host = GitHubHost("o/r", FakeHttp({}), listing_id="Mod")
        self.assertEqual(host._release(self.payload(("Mod.zip", 7))).downloads, 7)
        # The archive named after the listing wins, and only its count is reported.
        self.assertEqual(
            host._release(self.payload(("Other.zip", 9), ("Mod.zip", 7))).downloads, 7
        )
        # No archive is picked from an ambiguous release, so there is no count.
        self.assertIsNone(host._release(self.payload(("A.zip", 8), ("B.zip", 9))).downloads)

    def test_a_count_that_is_not_a_non_negative_integer_is_none(self):
        host = GitHubHost("o/r", FakeHttp({}), listing_id="Mod")
        for value in (True, -1, "7", None, 1.5):
            payload = {"tag_name": "v1.0.0", "assets": [
                {"state": "uploaded", "name": "Mod.zip", "download_count": value},
            ]}
            self.assertIsNone(host._release(payload).downloads, msg=repr(value))

    def test_spacedock_reports_the_mod_total_and_each_version(self):
        body = json.dumps({
            "url": "/mod/1",
            "downloads": 90,
            "versions": [{"friendly_version": "1.0.0", "downloads": 20,
                          "created": "2020-01-01T00:00:00Z", "download_path": "/mod/1/d"}],
        }).encode()
        host = SpaceDockHost(1, FakeHttp({"https://spacedock.info/api/mod/1": Response(200, {}, body)}))
        releases, _ = host.releases()
        self.assertEqual(host.downloads, 90)
        self.assertEqual(releases[0].downloads, 20)

    def test_the_pages_of_the_last_answer_are_known(self):
        first = Response(200, {"Link": '<https://api.github.com/page2>; rel="next"'}, b"[]")
        http = FakeHttp({
            "https://api.github.com/repos/o/r/releases": first,
            "https://api.github.com/page2": Response(200, {}, b"[]"),
        })
        host = GitHubHost("o/r", http)
        host.releases()
        self.assertEqual(host.pages, 2)

    def test_an_answer_in_an_undocumented_shape_is_a_host_error(self):
        # An AttributeError would stop every listing after this one.
        spacedock_bodies = (
            b"[]",
            json.dumps({"downloads": 1, "versions": ["1.0.0"]}).encode(),
            json.dumps({"downloads": 1, "versions": {"1.0.0": 1}}).encode(),
        )
        for body in spacedock_bodies:
            http = FakeHttp({"https://spacedock.info/api/mod/1": Response(200, {}, body)})
            with self.assertRaises(HostError, msg=body):
                SpaceDockHost(1, http).releases()
        github_bodies = (b"{}", b"[1]", json.dumps([{"tag_name": "v1", "assets": ["x.zip"]}]).encode())
        for body in github_bodies:
            http = FakeHttp({"https://api.github.com/repos/o/r/releases": Response(200, {}, body)})
            with self.assertRaises(HostError, msg=body):
                GitHubHost("o/r", http).releases()

    def test_named_returns_every_host_without_an_authority(self):
        found = named({"github": "o/r", "spacedock": 1}, FakeHttp({}), "Mod")
        self.assertEqual(sorted(found), ["github", "spacedock"])
        self.assertEqual(named({}, FakeHttp({})), {})


class ReleaseNotes(unittest.TestCase):
    def test_github_reads_the_release_body(self):
        host = GitHubHost("o/r", FakeHttp({}), listing_id="Mod")
        found = host._release({"tag_name": "v1.0.0", "body": "## Changes\r\n- A", "assets": []})
        self.assertEqual(found.changelog_text, "## Changes\r\n- A")
        self.assertEqual(found.facts()["changelog_text"], "## Changes\r\n- A")
        self.assertEqual(host._release({"tag_name": "v1.0.0", "body": None}).changelog_text, "")
        for body in (7, ["x"]):
            payload = {"tag_name": "v1.0.0", "body": body}
            self.assertIsNone(host._release(payload).changelog_text, msg=repr(body))

    def test_notes_the_answer_does_not_carry_are_not_empty_notes(self):
        # The watcher removes a stamped text only when the host says the notes are empty.
        host = GitHubHost("o/r", FakeHttp({}), listing_id="Mod")
        self.assertIsNone(host._release({"tag_name": "v1.0.0"}).changelog_text)

    def test_spacedock_reads_the_changelog_of_each_version(self):
        body = json.dumps({
            "url": "/mod/1",
            "versions": [
                {"friendly_version": "1.0.1", "changelog": "Fixes.",
                 "created": "2020-02-01T00:00:00Z", "download_path": "/mod/1/d/1.0.1"},
                {"friendly_version": "1.0.0", "changelog": None,
                 "created": "2020-01-01T00:00:00Z", "download_path": "/mod/1/d/1.0.0"},
                {"friendly_version": "0.9.0",
                 "created": "2019-01-01T00:00:00Z", "download_path": "/mod/1/d/0.9.0"},
            ],
        }).encode()
        http = FakeHttp({"https://spacedock.info/api/mod/1": Response(200, {}, body)})
        releases, _ = SpaceDockHost(1, http).releases()
        self.assertEqual([entry.changelog_text for entry in releases], ["Fixes.", "", None])


class Timestamps(unittest.TestCase):
    def test_garbage_yields_none_rather_than_passing_through(self):
        # release_date is stamped exactly once; the stamper rejects a release
        # without one, which scopes the failure to that release.
        self.assertIsNone(_utc("not a timestamp"))
        self.assertIsNone(_utc(None))
        self.assertEqual(_utc("2020-01-01T00:00:00Z"), "2020-01-01T00:00:00Z")
        self.assertEqual(_utc("2020-01-01T01:30:00+01:30"), "2020-01-01T00:00:00Z")


class AssetSelection(unittest.TestCase):
    def payload(self, names, tag="0.4.6"):
        return {
            "tag_name": tag,
            "published_at": "2020-01-01T00:00:00Z",
            "html_url": "https://github.com/o/r/releases/tag/x",
            "assets": [
                {
                    "state": "uploaded",
                    "name": name,
                    "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{name}",
                    "content_type": "application/zip",
                    "size": 1,
                }
                for name in names
            ],
        }

    def test_the_asset_named_after_the_listing_wins(self):
        host = GitHubHost("o/r", FakeHttp({}), listing_id="StarMap")
        chosen = host._release(self.payload(["StarMap-0.4.6.zip", "StarMapSource.zip"]))
        self.assertEqual(chosen.asset_name, "StarMap-0.4.6.zip")

    def test_an_ambiguous_release_is_not_guessed_at(self):
        host = GitHubHost("o/r", FakeHttp({}), listing_id="StarMap")
        chosen = host._release(self.payload(["Launcher.zip", "Standalone.zip"]))
        self.assertIsNone(chosen.url)
        self.assertEqual(len(chosen.candidates), 2)


if __name__ == "__main__":
    unittest.main()
