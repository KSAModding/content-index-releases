#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the host adapters: no network, a fake Http answers or raises.

The point of these is the HostError/StampError split on the failure paths: an
HTTPError that escapes either becomes an "unexpected" log line with no
author-facing issue, which is exactly what the watcher must not do.
"""

import dataclasses
import hashlib
import http.client
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hosts import (
    MAX_ARCHIVE_BYTES,
    MAX_RESPONSE_BYTES,
    REFUSED_READ_HOURS,
    GitHubHost,
    HostError,
    HostRelease,
    Http,
    OversizeError,
    Response,
    ShareHost,
    SpaceDockHost,
    _utc,
    build,
    download,
    listed_mirrors,
    named,
    registered,
    share_keys,
    stamped_dependencies,
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


class StampedDependencies(unittest.TestCase):
    DOCUMENT = {
        "version": "1.0.0",
        "download": {"url": "https://example.invalid/a.zip", "sha256": "AB"},
        "install": {"root": "Example Rover", "derived": True, "folder": "Example Rover"},
    }

    def test_a_vehicle_or_a_save_makes_no_request(self):
        for content_type in ("vehicle", "save", "mod-loader"):
            with self.subTest(content_type):
                document = dict(self.DOCUMENT, type=content_type)
                self.assertEqual(stamped_dependencies(FakeHttp({}), document), [])

    def test_a_mod_downloads_its_archive(self):
        with self.assertRaisesRegex(AssertionError, "unexpected URL"):
            stamped_dependencies(FakeHttp({}), dict(self.DOCUMENT, type="mod"))


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

    def test_a_status_request_reads_no_body(self):
        answer = Answer(b"x" * 100)
        self.serve(answer)
        self.assertEqual(Http().status(self.URL), 200)
        self.assertEqual(answer.read_bytes, 0)

    def test_a_status_request_returns_a_client_error_and_retries_a_server_error(self):
        self.serve(http_error(404))
        self.assertEqual(Http().status(self.URL), 404)
        self.serve(http_error(503), http_error(503), http_error(503))
        with self.assertRaises(HostError):
            Http().status(self.URL)

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

    def test_every_uploaded_asset_is_a_listed_archive(self):
        # A stamped URL that is not the picked archive is still on the host.
        payload = self.payload(["StarMap-0.4.6.zip", "Source.tar.gz", "Broken.zip"])
        payload["assets"][2]["state"] = "starter"
        chosen = GitHubHost("o/r", FakeHttp({}), listing_id="StarMap")._release(payload)
        self.assertEqual(
            chosen.archives,
            (
                "https://github.com/o/r/releases/download/0.4.6/StarMap-0.4.6.zip",
                "https://github.com/o/r/releases/download/0.4.6/Source.tar.gz",
            ),
        )

    def test_a_spacedock_version_lists_its_download(self):
        body = json.dumps({"versions": [{"friendly_version": "1.0.0", "download_path": "/d/1"}]})
        http = FakeHttp({"https://spacedock.info/api/mod/1": Response(200, {}, body.encode())})
        releases, _ = SpaceDockHost(1, http).releases()
        self.assertEqual(releases[0].archives, ("https://spacedock.info/d/1",))


def gatos_release(tag, *names, draft=False):
    return {
        "tag_name": tag,
        "draft": draft,
        "published_at": "2026-10-01T00:00:00Z",
        "html_url": f"https://github.com/o/gatos/releases/tag/{tag}",
        "assets": [
            {
                "state": "uploaded",
                "name": name,
                "content_type": "application/zip",
                "size": 1,
                "browser_download_url": f"https://github.com/o/gatos/releases/download/{tag}/{name}",
            }
            for name in names
        ],
    }


GATOS = [
    gatos_release("gatos-v1.3.0", "gatOS.zip", draft=True),
    gatos_release("vm-images-2026-10", "gatos-vm.zip"),
    gatos_release("gatos-v1.2.0", "gatOS.zip"),
    gatos_release("gatos-v1.1.0", "gatOS.zip"),
]


class TagPrefix(unittest.TestCase):
    """A repository that publishes VM image releases next to its mod releases."""

    @staticmethod
    def host(prefix, payloads=GATOS):
        body = json.dumps(payloads).encode()
        http = FakeHttp({"https://api.github.com/repos/o/gatos/releases": Response(200, {}, body)})
        return GitHubHost("o/gatos", http, listing_id="gatOS", tag_prefix=prefix)

    def test_only_a_tag_with_the_prefix_is_a_candidate(self):
        for prefix, rest in (("gatos-v", "1.2.0"), ("gatos-", "v1.2.0")):
            host = self.host(prefix)
            releases, _ = host.releases()
            self.assertEqual([entry.tag for entry in releases], ["gatos-v1.2.0", "gatos-v1.1.0"])
            self.assertEqual([entry.version for entry in releases], ["1.2.0", "1.1.0"])
            self.assertEqual(releases[0].facts()["tag"], rest)
            # The links keep the real tag.
            self.assertEqual(
                releases[0].changelog, "https://github.com/o/gatos/releases/tag/gatos-v1.2.0"
            )
            self.assertIn("/gatos-v1.2.0/", releases[0].url)
            # The draft is not counted, the image release is.
            self.assertEqual(host.skipped, 1)

    def test_without_a_prefix_every_release_is_a_candidate(self):
        host = self.host("")
        releases, _ = host.releases()
        self.assertEqual(len(releases), 3)
        self.assertEqual([entry.version for entry in releases], [None, None, None])
        self.assertEqual(releases[0].facts()["tag"], "vm-images-2026-10")
        self.assertEqual(host.skipped, 0)

    def test_a_prefix_that_matches_nothing_offers_nothing(self):
        # The match is case-sensitive, as GitHub compares tags.
        for prefix in ("GATOS-", "mod-v"):
            host = self.host(prefix)
            releases, _ = host.releases()
            self.assertEqual(releases, [])
            self.assertEqual(host.skipped, 3)

    def test_a_candidate_whose_rest_does_not_parse_has_no_version(self):
        releases, _ = self.host("gatos-", [gatos_release("gatos-beta", "gatOS.zip")]).releases()
        self.assertEqual(len(releases), 1)
        self.assertIsNone(releases[0].version)

    def test_the_asset_name_is_matched_against_the_rest_of_the_tag(self):
        payload = gatos_release("gatos-v1.2.0", "gatOS-1.2.0.zip", "Source.zip")
        for prefix in ("gatos-v", "gatos-"):
            chosen = self.host(prefix)._release(payload)
            self.assertEqual(chosen.asset_name, "gatOS-1.2.0.zip", msg=prefix)

    def test_the_prefix_is_part_of_the_key(self):
        self.assertEqual(self.host("").key, "github:o/gatos")
        self.assertEqual(self.host("gatos-v").key, "github:o/gatos#gatos-v")
        self.assertNotEqual(self.host("gatos-").key, self.host("gatos-v").key)

    def test_named_gives_the_prefix_to_github_as_authority_and_as_mirror(self):
        found = named({"github": "o/gatos", "tag_prefix": "gatos-v"}, FakeHttp({}), "gatOS")
        self.assertEqual(found["github"].tag_prefix, "gatos-v")
        both = {
            "github": "o/gatos", "spacedock": 1, "authority": "spacedock", "tag_prefix": "gatos-v",
        }
        found = named(both, FakeHttp({}), "gatOS")
        self.assertEqual(found["github"].tag_prefix, "gatos-v")
        self.assertEqual(named({"github": "o/gatos"}, FakeHttp({}))["github"].tag_prefix, "")

    def test_an_invalid_prefix_is_a_stamp_error(self):
        for value in ("", "gatos v", "gatos-ä", 5, True, ["gatos-"]):
            with self.assertRaises(StampError, msg=repr(value)):
                named({"github": "o/gatos", "tag_prefix": value}, FakeHttp({}))

    def test_a_prefix_without_a_github_key_is_a_stamp_error(self):
        with self.assertRaises(StampError) as caught:
            named({"spacedock": 1, "tag_prefix": "gatos-v"}, FakeHttp({}))
        self.assertIn("github", str(caught.exception))


STORAGE = "https://api.github.com/repos/ksamods-content/share"
ASSETS = "https://github.com/ksamods-content/share/releases/download"

# The sizes are small, so a test of the limit needs no large archive.
SHARE_TABLE = {
    "kind": "share",
    "name": "ksamods.gg",
    "storage": "ksamods-content/share",
    "bot": 98765432,
    "api": "https://ksamods.gg/api/share/v1/",
    "types": ["vehicle", "save"],
    "max_bytes": {"vehicle": 8, "save": 64},
}
REGISTRY = {"ksamods": SHARE_TABLE}


def share_entry(version, status="stable", mirrors=None):
    entry = {"version": version, "status": status}
    if mirrors is not None:
        entry["mirrors"] = mirrors
    return entry


def share_release(tag, *names, size=4, count=3, draft=False, date="2026-10-11T09:14:30Z"):
    """The GitHub release of a share host, with one uploaded asset per name."""
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": False,
        "published_at": date,
        "assets": [
            {
                "state": "uploaded",
                "name": name,
                "content_type": "application/zip",
                "size": size,
                "download_count": count,
                "browser_download_url": f"{ASSETS}/{tag}/{name}",
            }
            for name in names
        ],
    }


class Storage:
    """The storage repository of a share host.

    It answers the release list with an ETag and a 304 on a match, and each
    GitHub release by its tag. Any other API URL fails the test, so the host's
    own service and its counter are never asked.
    """

    token = None

    def __init__(self, entries, releases=(), archives=None, listing_id="ExampleRover", content_type="vehicle"):
        self.document = {"id": listing_id, "type": content_type, "releases": entries}
        self.releases = {payload["tag_name"]: payload for payload in releases}
        self.archives = archives or {}
        self.asked = []
        self.limits = []
        self.lines = []
        self.requests = 0

    def log(self, message):
        self.lines.append(message)

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        self.asked.append((url, etag))
        self.requests += 1
        if url.startswith(f"{STORAGE}/contents/"):
            body = json.dumps(self.document).encode()
            current = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            if etag == current:
                return Response(304, {}, b"")
            return Response(200, {"ETag": current}, body)
        prefix = f"{STORAGE}/releases/tags/"
        if url.startswith(prefix):
            payload = self.releases.get(urllib.parse.unquote(url[len(prefix):]))
            if payload is None:
                raise http_error(404)
            return Response(200, {}, json.dumps(payload).encode())
        raise AssertionError(f"unexpected URL {url}")

    def archive(self, url, api=False, limit=None):
        self.limits.append(limit)
        body = self.archives[url]
        if limit is not None and len(body) > limit:
            raise OversizeError("the archive is larger than the limit")
        return Archive.of_bytes(body), {"Content-Type": "application/zip"}


def rover(version="1.0.0", **extra):
    tag = f"vehicle/ExampleRover/{version}"
    return share_release(tag, f"ExampleRover-{version}.zip", **extra)


def share_host(http, content_type="vehicle", listing_id="ExampleRover"):
    return named({"ksamods": listing_id}, http, listing_id, content_type, REGISTRY)["ksamods"]


class ShareHosts(unittest.TestCase):
    """RFC 0094: a share host is read from its storage repository only."""

    def test_the_release_list_and_each_github_release_make_the_releases(self):
        http = Storage([share_entry("1.0.0"), share_entry("1.1.0-rc.1", "testing")], [rover(), rover("1.1.0-rc.1")])
        releases, etag = share_host(http).releases()
        first, second = releases
        self.assertEqual((first.version, first.status, first.problem), ("1.0.0", "stable", None))
        self.assertEqual(first.tag, "vehicle/ExampleRover/1.0.0")
        self.assertEqual(first.rest, "1.0.0")
        self.assertEqual(first.url, f"{ASSETS}/vehicle/ExampleRover/1.0.0/ExampleRover-1.0.0.zip")
        self.assertEqual((first.release_date, first.size, first.downloads), ("2026-10-11T09:14:30Z", 4, 3))
        self.assertEqual(second.facts()["status"], "testing")
        self.assertEqual(
            [url for url, _ in http.asked],
            [
                f"{STORAGE}/contents/releases/vehicle/ExampleRover.json",
                f"{STORAGE}/releases/tags/vehicle%2FExampleRover%2F1.0.0",
                f"{STORAGE}/releases/tags/vehicle%2FExampleRover%2F1.1.0-rc.1",
            ],
        )
        due, tag = etag.split(" ")
        self.assertTrue(tag.startswith('"'))
        self.assertGreater(_utc(due), _utc((datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()))

    def test_an_unchanged_release_list_costs_one_request(self):
        http = Storage([share_entry("1.0.0")], [rover()])
        host = share_host(http)
        _, etag = host.releases()
        http.asked.clear()
        self.assertEqual(host.releases(etag), (None, etag))
        self.assertEqual(len(http.asked), 1)
        self.assertEqual(http.asked[0][1], etag.split(" ")[1])

    def test_an_etag_whose_full_read_is_due_is_not_sent(self):
        # A deleted GitHub release does not change the list, so the list is read in full once a day.
        http = Storage([share_entry("1.0.0")], [rover()])
        host = share_host(http)
        _, etag = host.releases()
        tag = etag.split(" ")[1]
        for stored in (f"2020-01-01T00:00:00Z {tag}", tag, "garbage"):
            http.asked.clear()
            releases, _ = host.releases(stored)
            self.assertIsNotNone(releases, stored)
            self.assertIsNone(http.asked[0][1], stored)

    def test_an_entry_without_its_github_release_is_refused_and_the_others_are_not(self):
        http = Storage([share_entry("1.0.0"), share_entry("1.1.0")], [rover("1.1.0")])
        (missing, present), etag = share_host(http).releases()
        self.assertIsNone(missing.url)
        self.assertIsNotNone(present.url)
        with self.assertRaises(StampError) as caught:
            share_host(http).download(missing)
        self.assertIn("`vehicle/ExampleRover/1.0.0`", str(caught.exception))
        self.assertEqual(http.limits, [])
        # The list is read again within REFUSED_READ_HOURS, in case the release was on its way.
        due = _utc(etag.split(" ")[0])
        self.assertLess(due, _utc((datetime.now(timezone.utc) + timedelta(hours=REFUSED_READ_HOURS, minutes=1)).isoformat()))

    def test_a_draft_is_no_github_release(self):
        http = Storage([share_entry("1.0.0")], [rover(draft=True)])
        [entry], _ = share_host(http).releases()
        self.assertIn("no published GitHub release", entry.problem)

    def test_a_release_with_another_asset_count_or_name_is_refused(self):
        tag = "vehicle/ExampleRover/1.0.0"
        for names in ((), ("ExampleRover-1.0.0.zip", "extra.zip"), ("examplerover-1.0.0.zip",), ("ExampleRover.zip",)):
            http = Storage([share_entry("1.0.0")], [share_release(tag, *names)])
            [entry], _ = share_host(http).releases()
            self.assertIsNone(entry.url, names)
            with self.assertRaises(StampError, msg=names) as caught:
                share_host(http).download(entry)
            self.assertIn("exactly one, named ExampleRover-1.0.0.zip", str(caught.exception))

    def test_a_status_outside_the_three_is_refused(self):
        for status in (None, "beta", "Stable", 1):
            http = Storage([share_entry("1.0.0", status)], [rover()])
            [entry], _ = share_host(http).releases()
            self.assertIn("is not one of stable, testing, dev", entry.problem, status)
            self.assertIsNone(entry.status)

    def test_a_version_that_is_not_a_string_has_no_version(self):
        http = Storage([{"status": "stable"}, share_entry("1.0.0")], [rover()])
        (entry, _), _ = share_host(http).releases()
        self.assertIsNone(entry.version)
        self.assertEqual(len(http.asked), 2)

    def test_an_asset_above_the_limit_of_its_type_is_refused(self):
        url = f"{ASSETS}/vehicle/ExampleRover/1.0.0/ExampleRover-1.0.0.zip"
        http = Storage([share_entry("1.0.0")], [rover(size=9)], {url: b"123456789"})
        host = share_host(http)
        [entry], _ = host.releases()
        # By the size the host reports, before any request.
        with self.assertRaises(StampError) as caught:
            host.download(entry)
        self.assertIn("above the", str(caught.exception))
        self.assertEqual(http.limits, [])
        # By the streamed size when the host reports none.
        with self.assertRaises(StampError):
            host.download(dataclasses.replace(entry, size=None))
        self.assertEqual(http.limits, [8])
        # A save of the same size fits the larger limit of saves.
        save = Storage(
            [share_entry("1.0.0")],
            [share_release("save/MoonBase/1.0.0", "MoonBase-1.0.0.zip", size=9)],
            {f"{ASSETS}/save/MoonBase/1.0.0/MoonBase-1.0.0.zip": b"123456789"},
            listing_id="MoonBase", content_type="save",
        )
        host = share_host(save, "save", "MoonBase")
        [entry], _ = host.releases()
        archive, _ = host.download(entry)
        archive.close()
        self.assertEqual(save.limits, [64])

    def test_a_limit_above_the_archive_limit_is_the_archive_limit(self):
        table = {**SHARE_TABLE, "max_bytes": {"vehicle": MAX_ARCHIVE_BYTES * 2}}
        host = named({"ksamods": "R"}, Storage([]), "R", "vehicle", {"ksamods": table})["ksamods"]
        self.assertEqual(host.max_bytes, MAX_ARCHIVE_BYTES)
        table = {**SHARE_TABLE, "max_bytes": {}}
        host = named({"ksamods": "R"}, Storage([]), "R", "vehicle", {"ksamods": table})["ksamods"]
        self.assertEqual(host.max_bytes, MAX_ARCHIVE_BYTES)

    def test_the_key_names_the_host_the_type_and_the_id(self):
        self.assertEqual(share_host(Storage([])).key, "share:ksamods/vehicle/examplerover")

    def test_only_an_https_copy_at_the_internet_archive_is_a_listed_mirror(self):
        kept = [
            "https://archive.org/download/ksamods-vehicle-ExampleRover-1.0.0/ExampleRover-1.0.0.zip",
            "https://ia800100.us.archive.org/1/items/x/ExampleRover-1.0.0.zip",
        ]
        dropped = ["http://archive.org/x.zip", "https://archive.org.example/x.zip", "https://notarchive.org/x.zip", 5]
        http = Storage([share_entry("1.0.0", mirrors=kept + dropped)], [rover()])
        [entry], _ = share_host(http).releases()
        self.assertEqual(entry.mirrors, tuple(kept))
        self.assertEqual(len(http.lines), len(dropped))
        mirror, served = listed_mirrors(share_host(http), entry)[0]
        self.assertEqual((served.url, served.size, served.archives), (kept[0], None, (kept[0],)))
        self.assertNotEqual(mirror.key, listed_mirrors(share_host(http), entry)[1][0].key)

    def test_a_missing_list_or_a_list_of_another_listing_is_a_stamp_error(self):
        class NoList(Storage):
            def get(self, url, **kwargs):
                raise http_error(404)

        with self.assertRaises(StampError) as caught:
            share_host(NoList([])).releases()
        self.assertIn("releases/vehicle/ExampleRover.json", str(caught.exception))
        with self.assertRaises(StampError):
            share_host(Storage([], listing_id="OtherRover")).releases()
        with self.assertRaises(StampError):
            share_host(Storage([], content_type="save")).releases()

    def test_a_list_that_is_not_json_or_a_server_error_is_a_host_error(self):
        class Broken(Storage):
            def get(self, url, **kwargs):
                return Response(200, {}, b"<html>")

        with self.assertRaises(HostError):
            share_host(Broken([])).releases()
        with self.assertRaises(HostError):
            share_host(FakeHttp({STORAGE: http_error(502)})).releases()

    def test_a_list_past_the_scan_limit_reads_the_newest_entries_and_says_so(self):
        entries = [share_entry(f"1.0.{patch}") for patch in range(4)]
        http = Storage(entries, [rover(f"1.0.{patch}") for patch in range(4)])
        host = ShareHost("ksamods", SHARE_TABLE, "ExampleRover", "vehicle", http, max_releases=2)
        releases, _ = host.releases()
        self.assertEqual([release.version for release in releases], ["1.0.2", "1.0.3"])
        self.assertTrue(host.truncated)


class NamedShareHosts(unittest.TestCase):
    def test_a_key_hosts_toml_does_not_register_is_a_stamp_error(self):
        for registry in (None, {}, {"other": SHARE_TABLE}):
            with self.assertRaises(StampError, msg=registry) as caught:
                named({"ksamods": "R"}, FakeHttp({}), "R", "vehicle", registry)
            self.assertIn("neither github nor spacedock", str(caught.exception))
        # The call of a caller that passes no registry refuses it too.
        with self.assertRaises(StampError):
            build({"ksamods": "R"}, FakeHttp({}), "R")

    def test_a_type_the_host_does_not_take_is_a_stamp_error(self):
        with self.assertRaises(StampError) as caught:
            named({"ksamods": "R"}, FakeHttp({}), "R", "mod", REGISTRY)
        self.assertIn("takes vehicle, save", str(caught.exception))

    def test_a_value_that_is_not_the_listing_id_is_a_stamp_error(self):
        for value in ("OtherRover", "r", 5):
            with self.assertRaises(StampError, msg=value):
                named({"ksamods": value}, FakeHttp({}), "R", "vehicle", REGISTRY)

    def test_a_table_the_watcher_cannot_read_is_a_host_error(self):
        for table in (
            {**SHARE_TABLE, "kind": "mirror"},
            {key: value for key, value in SHARE_TABLE.items() if key != "storage"},
            {**SHARE_TABLE, "storage": "https://github.com/x/y"},
            {**SHARE_TABLE, "types": "vehicle"},
            {**SHARE_TABLE, "max_bytes": {"vehicle": "10 MiB"}},
        ):
            with self.assertRaises(HostError, msg=table):
                named({"ksamods": "R"}, FakeHttp({}), "R", "vehicle", {"ksamods": table})

    def test_a_table_name_the_snapshot_cannot_count_under_is_a_host_error(self):
        # Counts under such a key would make the whole counts document fail its check.
        for key in ("KSAmods", "k", "ksa_mods", "k" * 33):
            with self.assertRaises(HostError, msg=key) as caught:
                named({key: "R"}, FakeHttp({}), "R", "vehicle", {key: SHARE_TABLE})
            self.assertIn("is not", str(caught.exception))

    def test_a_share_host_can_be_the_authority_next_to_github(self):
        section = {"ksamods": "R", "github": "o/shares", "tag_prefix": "vehicle/R/", "authority": "ksamods"}
        authority, mirrors = build(section, FakeHttp({}), "R", "vehicle", REGISTRY)
        self.assertIsInstance(authority, ShareHost)
        self.assertEqual([mirror.kind for mirror in mirrors], ["github"])
        authority, mirrors = build({**section, "authority": "github"}, FakeHttp({}), "R", "vehicle", REGISTRY)
        self.assertEqual((authority.kind, [mirror.kind for mirror in mirrors]), ("github", ["share"]))
        with self.assertRaises(StampError) as caught:
            build({**section, "authority": None}, FakeHttp({}), "R", "vehicle", REGISTRY)
        self.assertIn("github, ksamods", str(caught.exception))

    def test_the_keys_that_name_no_host_are_no_share_host(self):
        self.assertEqual(share_keys({"github": "o/r", "since": "1.0", "tag_prefix": "v", "authority": "github", "ksamods": "R"}), ["ksamods"])
        self.assertEqual(share_keys(None), [])

    def test_hosts_toml_is_read_from_the_authored_checkout(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.assertEqual(registered(root), {})
            (root / "hosts.toml").write_text('[ksamods]\nkind = "share"\nstorage = "o/r"\n', encoding="utf-8")
            self.assertEqual(registered(root), {"ksamods": {"kind": "share", "storage": "o/r"}})
            for text in ("[ksamods\n", 'ksamods = "share"\n'):
                (root / "hosts.toml").write_text(text, encoding="utf-8")
                with self.assertRaises(HostError, msg=text):
                    registered(root)


if __name__ == "__main__":
    unittest.main()
