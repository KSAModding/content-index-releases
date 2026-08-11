#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the host adapters: no network, a fake Http answers or raises.

The point of these is the HostError/StampError split on the failure paths: an
HTTPError that escapes either becomes an "unexpected" log line with no
author-facing issue, which is exactly what the watcher must not do.
"""

import json
import unittest
import urllib.error

from hosts import (
    MAX_ARCHIVE_BYTES,
    GitHubHost,
    HostError,
    HostRelease,
    OversizeError,
    Response,
    SpaceDockHost,
    _download,
    _utc,
)
from stamp_release import StampError


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


def release(url, candidates=(), size=None):
    return HostRelease(
        host="github", tag="v1.0.0", version="1.0.0",
        release_date="2020-01-01T00:00:00Z", url=url,
        candidates=tuple(candidates), size=size,
    )


class Downloads(unittest.TestCase):
    def test_a_gone_archive_is_a_stamp_error(self):
        # 404, 410 and 451 are facts about the release, reported to the author.
        for code in (404, 410, 451):
            http = FakeHttp({"https://github.com/": http_error(code)})
            with self.assertRaises(StampError, msg=code):
                _download(http, release("https://github.com/o/r/releases/download/x.zip"))

    def test_any_other_http_error_is_a_host_error(self):
        http = FakeHttp({"https://github.com/": http_error(403)})
        with self.assertRaises(HostError):
            _download(http, release("https://github.com/o/r/releases/download/x.zip"))

    def test_an_oversized_archive_is_a_stamp_error_not_a_retry(self):
        # Oversize is permanent: as a HostError the watcher would download and
        # discard the archive every tick and no issue would ever open.
        with self.assertRaises(StampError):
            _download(FakeHttp({}), release("https://github.com/o/r/x.zip",
                                            size=MAX_ARCHIVE_BYTES + 1))
        http = FakeHttp({"https://github.com/": OversizeError("too large")})
        with self.assertRaises(StampError):
            _download(http, release("https://github.com/o/r/x.zip"))


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
