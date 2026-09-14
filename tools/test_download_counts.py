#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the download counts refresh: no network, a fake Http answers or raises.
"""

import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from build_snapshot import SnapshotError, check_download_counts, serialize
from download_counts import main, refresh
from hosts import HostError, Response

GITHUB = "https://api.github.com/repos/o/r/releases"
SPACEDOCK = "https://spacedock.info/api/mod/7"

BOTH_HOSTS = '[releases]\ngithub = "o/r"\nspacedock = 7\nauthority = "github"\n'
GITHUB_ONLY = '[releases]\ngithub = "o/r"\n'
SPACEDOCK_ONLY = "[releases]\nspacedock = 7\n"


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "boom", {}, None)


class FakeHttp:
    """Answers by URL prefix, or raises what the route holds, and keeps every request."""

    token = None

    def __init__(self, routes):
        self.routes = routes
        self.asked = []
        self.requests = 0

    def log(self, message):
        pass

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        self.asked.append((url, etag))
        self.requests += 1
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if callable(answer) and not isinstance(answer, Exception):
                    answer = answer(etag)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected URL {url}")


def asset(name, count, state="uploaded"):
    return {
        "state": state,
        "name": name,
        "content_type": "application/zip",
        "size": 1,
        "download_count": count,
        "browser_download_url": f"https://github.com/o/r/releases/download/x/{name}",
    }


def github_release(tag, count, draft=False, assets=None):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": False,
        "published_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/o/r/releases/tag/{tag}",
        "assets": assets if assets is not None else [asset("Mod.zip", count)],
    }


def github(*releases, etag='W/"one"', headers=None):
    return Response(200, {"ETag": etag, **(headers or {})}, json.dumps(list(releases)).encode())


def spacedock(total, *versions):
    payload = {
        "url": "/mod/7/Mod",
        "versions": [
            {
                "friendly_version": version,
                "downloads": count,
                "created": "2026-08-01T00:00:00Z",
                "download_path": f"/mod/7/Mod/download/{version}",
            }
            for version, count in versions
        ],
    }
    if total is not None:
        payload["downloads"] = total
    return Response(200, {}, json.dumps(payload).encode())


def counted(identifier, hosts, *releases):
    """One stored listing entry, with totals that are the sums of their hosts."""
    return {
        "id": identifier,
        "total": sum(hosts.values()),
        "hosts": hosts,
        "releases": [
            {"version": version, "total": sum(values.values()), "hosts": values}
            for version, values in releases
        ],
    }


def document(*entries):
    return {"spec_version": 1, "listings": list(entries)}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.authored = self.root / "authored"
        (self.authored / "listings").mkdir(parents=True)
        self.notes = []
        self.cache = {}

    def listing(self, identifier="Mod", releases=BOTH_HOSTS, content_type="mod"):
        text = (
            f'spec_version = 1\nid = "{identifier}"\ntype = "{content_type}"\n'
            f'name = "{identifier}"\n\n{releases}'
        )
        (self.authored / "listings" / f"{identifier}.toml").write_text(text, encoding="utf-8")

    def delist(self, identifier):
        (self.authored / "index-status.toml").write_text(
            f'[[entries]]\nid = "{identifier}"\nstate = "delisted"\n', encoding="utf-8"
        )

    def refresh(self, http, current=None):
        stored = check_download_counts(current, "test") if current is not None else None
        result = refresh(
            self.authored, stored, http, self.cache, log=self.notes.append, note=self.notes.append
        )
        # Everything the refresh produces has to pass the builder's own check.
        check_download_counts(json.loads(serialize(result)), "refreshed")
        return result

    @staticmethod
    def only(result):
        (entry,) = result["listings"]
        return entry


class GitHubCounts(Fixture):
    def test_every_published_release_counts_and_a_draft_does_not(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(
            github_release("v1.1.0", 5),
            github_release("v1.0.0", 7),
            github_release("v2.0.0", 100, draft=True),
        )})
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["hosts"], {"github": 12})
        self.assertEqual(entry["total"], 12)
        self.assertEqual(
            entry["releases"],
            [
                {"version": "1.1.0", "total": 5, "hosts": {"github": 5}},
                {"version": "1.0.0", "total": 7, "hosts": {"github": 7}},
            ],
        )

    def test_a_tag_that_does_not_parse_counts_in_the_total_only(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(github_release("v1.0.0", 4), github_release("latest", 6))})
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["total"], 10)
        self.assertEqual([release["version"] for release in entry["releases"]], ["1.0.0"])

    def test_only_the_archive_the_stamping_rules_select_counts(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(
            github_release("v1.1.0", 0, assets=[asset("Mod.zip", 3), asset("Source.zip", 50)]),
            github_release("v1.0.0", 0, assets=[asset("A.zip", 8), asset("B.zip", 9)]),
        )})
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["hosts"], {"github": 3})
        self.assertEqual([release["version"] for release in entry["releases"]], ["1.1.0"])

    def test_a_count_that_is_not_a_non_negative_integer_is_not_counted(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(
            github_release("v1.2.0", 2),
            github_release("v1.1.0", 0, assets=[asset("Mod.zip", True)]),
            github_release("v1.0.0", 0, assets=[asset("Mod.zip", -1)]),
        )})
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["hosts"], {"github": 2})
        self.assertEqual([release["version"] for release in entry["releases"]], ["1.2.0"])

    def test_a_zero_the_host_reports_is_a_count(self):
        self.listing(releases=GITHUB_ONLY)
        entry = self.only(self.refresh(FakeHttp({GITHUB: github(github_release("v1.0.0", 0))})))
        self.assertEqual(entry["hosts"], {"github": 0})

    def test_a_repository_with_no_release_has_no_counts(self):
        self.listing(releases=GITHUB_ONLY)
        self.assertEqual(self.refresh(FakeHttp({GITHUB: github()}))["listings"], [])


class SpaceDockCounts(Fixture):
    def test_the_mod_total_and_each_version_count(self):
        self.listing(releases=SPACEDOCK_ONLY)
        http = FakeHttp({SPACEDOCK: spacedock(90, ("1.1.0", 30), ("1.0.0", 20), ("beta", 5))})
        entry = self.only(self.refresh(http))
        # SpaceDock's own total, not the sum of the versions it still lists.
        self.assertEqual(entry["hosts"], {"spacedock": 90})
        self.assertEqual(
            [(release["version"], release["total"]) for release in entry["releases"]],
            [("1.1.0", 30), ("1.0.0", 20)],
        )

    def test_an_answer_without_a_total_is_a_failed_request(self):
        self.listing(releases=SPACEDOCK_ONLY)
        http = FakeHttp({SPACEDOCK: spacedock(None, ("1.0.0", 20))})
        self.assertEqual(self.refresh(http)["listings"], [])
        self.assertTrue(any("could not be counted" in note for note in self.notes))


class MirrorHost(Fixture):
    def test_a_mirror_contributes_like_the_authority(self):
        self.listing()
        http = FakeHttp({
            GITHUB: github(github_release("v1.0.0", 17), github_release("v0.9.0", 4)),
            SPACEDOCK: spacedock(721, ("1.0.0", 23)),
        })
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["hosts"], {"github": 21, "spacedock": 721})
        self.assertEqual(entry["total"], 742)
        self.assertEqual(
            entry["releases"],
            [
                {"version": "1.0.0", "total": 40, "hosts": {"github": 17, "spacedock": 23}},
                {"version": "0.9.0", "total": 4, "hosts": {"github": 4}},
            ],
        )


class Retention(Fixture):
    STORED = document(
        counted(
            "Mod",
            {"github": 30, "spacedock": 50},
            ("1.1.0", {"github": 10, "spacedock": 20}),
            ("1.0.0", {"github": 20, "spacedock": 30}),
        )
    )

    def test_a_lower_value_replaces_the_stored_one(self):
        self.listing()
        http = FakeHttp({
            GITHUB: github(github_release("v1.1.0", 1), github_release("v1.0.0", 2)),
            SPACEDOCK: spacedock(5, ("1.1.0", 2), ("1.0.0", 3)),
        })
        entry = self.only(self.refresh(http, self.STORED))
        self.assertEqual(entry["hosts"], {"github": 3, "spacedock": 5})
        self.assertEqual(entry["releases"][0]["hosts"], {"github": 1, "spacedock": 2})

    def test_a_failed_request_keeps_the_last_known_values(self):
        self.listing()
        http = FakeHttp({
            GITHUB: HostError("the host is having a bad moment"),
            SPACEDOCK: spacedock(60, ("1.1.0", 25), ("1.0.0", 35)),
        })
        entry = self.only(self.refresh(http, self.STORED))
        self.assertEqual(entry["hosts"], {"github": 30, "spacedock": 60})
        self.assertEqual(entry["releases"][1]["hosts"], {"github": 20, "spacedock": 35})

    def test_a_repository_that_is_gone_keeps_the_last_known_values(self):
        self.listing()
        http = FakeHttp({GITHUB: http_error(404), SPACEDOCK: http_error(404)})
        self.assertEqual(self.refresh(http, self.STORED), self.STORED)

    def test_a_release_that_disappeared_keeps_its_count_and_its_share_of_the_total(self):
        self.listing()
        http = FakeHttp({
            GITHUB: github(github_release("v1.1.0", 12)),
            SPACEDOCK: spacedock(55, ("1.1.0", 25)),
        })
        entry = self.only(self.refresh(http, self.STORED))
        self.assertEqual(entry["hosts"], {"github": 32, "spacedock": 55})
        self.assertEqual(entry["releases"][1], {
            "version": "1.0.0", "total": 50, "hosts": {"github": 20, "spacedock": 30},
        })

    def test_an_archive_that_disappeared_keeps_its_count(self):
        self.listing()
        http = FakeHttp({
            GITHUB: github(
                github_release("v1.1.0", 12),
                github_release("v1.0.0", 0, assets=[asset("A.zip", 1), asset("B.zip", 2)]),
            ),
            SPACEDOCK: spacedock(50, ("1.1.0", 20), ("1.0.0", 30)),
        })
        entry = self.only(self.refresh(http, self.STORED))
        self.assertEqual(entry["hosts"]["github"], 32)
        self.assertEqual(entry["releases"][1]["hosts"]["github"], 20)

    def test_a_host_the_listing_no_longer_names_keeps_its_values_and_is_not_asked(self):
        self.listing(releases=GITHUB_ONLY)
        # FakeHttp fails on any URL it has no route for, SpaceDock included.
        http = FakeHttp({GITHUB: github(github_release("v1.1.0", 11), github_release("v1.0.0", 21))})
        entry = self.only(self.refresh(http, self.STORED))
        self.assertEqual(entry["hosts"], {"github": 32, "spacedock": 50})
        self.assertEqual(entry["releases"][0]["hosts"], {"github": 11, "spacedock": 20})
        self.assertFalse(any(url.startswith(SPACEDOCK) for url, _ in http.asked))

    def test_a_listing_without_releases_keeps_its_values_and_nothing_is_asked(self):
        self.listing(releases="")
        http = FakeHttp({})
        self.assertEqual(self.refresh(http, self.STORED), self.STORED)
        self.assertEqual(http.asked, [])

    def test_a_value_never_observed_stays_absent_and_is_not_zero(self):
        self.listing()
        http = FakeHttp({
            GITHUB: github(github_release("v1.0.0", 3)),
            SPACEDOCK: HostError("down"),
        })
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["hosts"], {"github": 3})
        self.assertNotIn("spacedock", entry["releases"][0]["hosts"])

    def test_a_listing_that_was_never_counted_has_no_entry(self):
        self.listing()
        http = FakeHttp({GITHUB: HostError("down"), SPACEDOCK: HostError("down")})
        self.assertEqual(self.refresh(http)["listings"], [])


class Listings(Fixture):
    def test_listings_ascend_by_lowercased_id_and_releases_descend_by_precedence(self):
        for identifier in ("zeta", "Alpha", "middle"):
            self.listing(identifier, releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(
            github_release("v0.4.3", 1),
            github_release("v0.4.10", 1),
            github_release("v0.4.4-beta.2", 1),
            github_release("v0.4.4", 1),
        )})
        result = self.refresh(http)
        self.assertEqual([entry["id"] for entry in result["listings"]], ["Alpha", "middle", "zeta"])
        self.assertEqual(
            [release["version"] for release in result["listings"][0]["releases"]],
            ["0.4.10", "0.4.4", "0.4.4-beta.2", "0.4.3"],
        )

    def test_the_authored_casing_of_the_id_is_kept(self):
        self.listing("MixedCase", releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(github_release("v1.0.0", 1))})
        stored = document(counted("mixedcase", {"github": 9}, ("1.0.0", {"github": 9})))
        self.assertEqual(self.only(self.refresh(http, stored))["id"], "MixedCase")

    def test_a_listing_that_is_gone_loses_its_entry(self):
        self.listing("Mod", releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(github_release("v1.0.0", 1))})
        stored = document(
            counted("Gone", {"github": 9}, ("1.0.0", {"github": 9})),
            counted("Mod", {"github": 1}, ("1.0.0", {"github": 1})),
        )
        self.assertEqual([entry["id"] for entry in self.refresh(http, stored)["listings"]], ["Mod"])
        self.assertTrue(any("no longer a listing" in note for note in self.notes))

    def test_a_delisted_listing_keeps_its_counts_and_is_not_asked(self):
        self.listing()
        self.delist("Mod")
        http = FakeHttp({})
        stored = document(counted("Mod", {"github": 9}, ("1.0.0", {"github": 9})))
        self.assertEqual(self.refresh(http, stored), stored)
        self.assertEqual(http.asked, [])

    def test_a_mod_loader_is_counted(self):
        self.listing("StarMap", releases=GITHUB_ONLY, content_type="mod-loader")
        http = FakeHttp({GITHUB: github(github_release("v0.4.5", 8))})
        self.assertEqual(self.only(self.refresh(http))["hosts"], {"github": 8})


class ConditionalRequests(Fixture):
    def test_an_unchanged_answer_reuses_the_remembered_counts(self):
        self.listing(releases=GITHUB_ONLY)
        self.refresh(FakeHttp({GITHUB: github(github_release("v1.0.0", 5), etag='W/"a"')}))

        def unchanged(etag):
            return Response(304, {}, b"") if etag == 'W/"a"' else github(github_release("v1.0.0", 99))

        http = FakeHttp({GITHUB: unchanged})
        entry = self.only(self.refresh(http))
        self.assertEqual(http.asked, [(f"{GITHUB}?per_page=100", 'W/"a"')])
        self.assertEqual(entry["hosts"], {"github": 5})

    def test_an_answer_of_several_pages_is_never_asked_conditionally(self):
        self.listing(releases=GITHUB_ONLY)
        link = {"Link": '<https://api.github.com/page2>; rel="next"'}
        first = github(github_release("v1.1.0", 1), etag='W/"a"', headers=link)
        routes = {"https://api.github.com/page2": github(github_release("v1.0.0", 2)), GITHUB: first}
        self.refresh(FakeHttp(routes))
        http = FakeHttp(routes)
        self.refresh(http)
        self.assertEqual(http.asked[0], (f"{GITHUB}?per_page=100", None))

    def test_a_host_that_is_no_longer_asked_leaves_the_cache(self):
        self.listing(releases=GITHUB_ONLY)
        self.refresh(FakeHttp({GITHUB: github(github_release("v1.0.0", 5))}))
        self.assertTrue(self.cache)
        self.listing(releases="")
        self.refresh(FakeHttp({}))
        self.assertEqual(self.cache, {})


class CommandLine(Fixture):
    def run_main(self, http, *extra):
        counts = self.root / "download-counts.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                ["--authored", str(self.authored), "--counts", str(counts),
                 "--cache", str(self.root / "cache.json"), *extra],
                http=http,
            )
        return code, counts, stdout.getvalue(), stderr.getvalue()

    def test_an_invalid_current_document_fails_and_is_not_replaced(self):
        self.listing(releases=GITHUB_ONLY)
        entry = counted("Mod", {"github": 9}, ("1.0.0", {"github": 9}))
        invalid = {
            "not json": "{oops",
            "a total that is not the sum": json.dumps(document({**entry, "total": 10})),
            "an unknown key": json.dumps(document({**entry, "fetched_at": "today"})),
            "a spec_version it does not know": json.dumps({"spec_version": 2, "listings": []}),
            "a host RFC 0052 does not define": json.dumps(
                document(counted("Mod", {"curseforge": 9}))
            ),
            "a negative count": json.dumps(document(counted("Mod", {"github": -1}))),
            "a boolean count": json.dumps(document(counted("Mod", {"github": True}))),
            "no host at all": json.dumps(document(counted("Mod", {}))),
            "a version that is not normalized": json.dumps(
                document(counted("Mod", {"github": 9}, ("v1.0.0", {"github": 9})))
            ),
            "releases out of order": json.dumps(document(counted(
                "Mod", {"github": 2}, ("1.0.0", {"github": 1}), ("1.1.0", {"github": 1})
            ))),
            "a version listed twice": json.dumps(document(counted(
                "Mod", {"github": 2}, ("1.0.0", {"github": 1}), ("1.0.0", {"github": 1})
            ))),
            "listings out of order": json.dumps(document(
                counted("Zeta", {"github": 1}), counted("alpha", {"github": 1})
            )),
            "an id listed twice": json.dumps(document(
                counted("Mod", {"github": 1}), counted("mod", {"github": 1})
            )),
            "a release host without a listing total": json.dumps(document(counted(
                "Mod", {"github": 9}, ("1.0.0", {"spacedock": 9})
            ))),
        }
        for case, text in invalid.items():
            with self.subTest(case):
                counts = self.root / "download-counts.json"
                counts.write_text(text, encoding="utf-8")
                http = FakeHttp({GITHUB: github(github_release("v1.0.0", 1))})
                code, counts, _, stderr = self.run_main(http)
                self.assertEqual(code, 1)
                self.assertIn("cannot refresh", stderr)
                self.assertEqual(counts.read_text(encoding="utf-8"), text)
                self.assertEqual(http.asked, [])

    def test_the_document_is_written_with_the_snapshot_bytes_rules(self):
        self.listing(releases=GITHUB_ONLY)
        code, counts, _, _ = self.run_main(FakeHttp({GITHUB: github(github_release("v1.0.0", 4))}))
        self.assertEqual(code, 0)
        raw = counts.read_bytes()
        self.assertTrue(raw.endswith(b"}\n"))
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(json.loads(raw), document(counted("Mod", {"github": 4}, ("1.0.0", {"github": 4}))))

    def test_an_unchanged_refresh_keeps_the_bytes_and_says_so(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(github_release("v1.0.0", 4))})
        _, counts, _, _ = self.run_main(http)
        before = counts.read_bytes()
        code, counts, _, stderr = self.run_main(FakeHttp({GITHUB: github(github_release("v1.0.0", 4))}))
        self.assertEqual(code, 0)
        self.assertEqual(counts.read_bytes(), before)
        self.assertIn("already current", stderr)

    def test_a_dry_run_prints_the_document_and_writes_nothing(self):
        self.listing(releases=GITHUB_ONLY)
        code, counts, stdout, _ = self.run_main(
            FakeHttp({GITHUB: github(github_release("v1.0.0", 4))}), "--dry-run"
        )
        self.assertEqual(code, 0)
        self.assertFalse(counts.exists())
        self.assertFalse((self.root / "cache.json").exists())
        self.assertEqual(json.loads(stdout)["listings"][0]["total"], 4)

    def test_an_authored_listing_that_does_not_parse_fails_the_refresh(self):
        (self.authored / "listings" / "Broken.toml").write_text("id = \n", encoding="utf-8")
        code, counts, _, _ = self.run_main(FakeHttp({}))
        self.assertEqual(code, 1)
        self.assertFalse(counts.exists())


class Isolation(Fixture):
    def test_a_malformed_answer_stops_only_its_own_host(self):
        self.listing("Broken", releases=SPACEDOCK_ONLY)
        self.listing("Fine", releases=GITHUB_ONLY)
        http = FakeHttp({
            SPACEDOCK: Response(200, {}, b"[]"),
            GITHUB: github(github_release("v1.0.0", 4)),
        })
        self.assertEqual(self.only(self.refresh(http))["id"], "Fine")
        self.assertTrue(any("could not be counted" in note for note in self.notes))

    def test_an_unexpected_error_stops_only_its_own_host(self):
        self.listing("Broken", releases=GITHUB_ONLY)
        self.listing("Fine", releases=SPACEDOCK_ONLY)
        http = FakeHttp({GITHUB: ValueError("boom"), SPACEDOCK: spacedock(3, ("1.0.0", 3))})
        self.assertEqual(self.only(self.refresh(http))["id"], "Fine")
        self.assertTrue(any("failed unexpectedly" in note for note in self.notes))

    def test_a_scan_longer_than_the_page_limit_is_a_failed_request(self):
        self.listing(releases=GITHUB_ONLY)
        page = '<https://api.github.com/page>; rel="next"'
        http = FakeHttp({
            GITHUB: github(github_release("v2.0.0", 1), headers={"Link": page}),
            "https://api.github.com/page": github(headers={"Link": page}),
        })
        stored = document(counted("Mod", {"github": 9}, ("1.0.0", {"github": 9})))
        self.assertEqual(self.refresh(http, stored), stored)
        self.assertTrue(any("more releases than one scan covers" in note for note in self.notes))


class KnownLimits(Fixture):
    """What the stored document cannot remember, pinned so a change is a decision."""

    def test_two_tags_of_one_version_are_added_together(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(github_release("v1.0.0", 4), github_release("1.0.0", 6))})
        entry = self.only(self.refresh(http))
        self.assertEqual(entry["releases"], [{"version": "1.0.0", "total": 10, "hosts": {"github": 10}}])

    def test_a_deleted_second_tag_of_one_version_lowers_the_counts(self):
        self.listing(releases=GITHUB_ONLY)
        stored = document(counted("Mod", {"github": 10}, ("1.0.0", {"github": 10})))
        entry = self.only(self.refresh(FakeHttp({GITHUB: github(github_release("v1.0.0", 4))}), stored))
        self.assertEqual(entry["hosts"], {"github": 4})
        self.assertEqual(entry["releases"][0]["hosts"], {"github": 4})

    def test_a_deleted_release_whose_tag_does_not_parse_loses_its_share_of_the_total(self):
        self.listing(releases=GITHUB_ONLY)
        http = FakeHttp({GITHUB: github(github_release("v1.0.0", 4), github_release("latest", 6))})
        stored = self.refresh(http)
        self.assertEqual(self.only(stored)["hosts"], {"github": 10})
        entry = self.only(self.refresh(FakeHttp({GITHUB: github(github_release("v1.0.0", 4))}), stored))
        self.assertEqual(entry["hosts"], {"github": 4})

    def test_an_archive_uploaded_again_replaces_the_count_with_its_new_one(self):
        self.listing(releases=GITHUB_ONLY)
        stored = document(counted("Mod", {"github": 400}, ("1.0.0", {"github": 400})))
        entry = self.only(self.refresh(FakeHttp({GITHUB: github(github_release("v1.0.0", 3))}), stored))
        self.assertEqual(entry["hosts"], {"github": 3})


class Cache(Fixture):
    SLOT = "mod/github:o/r"

    def remembered(self, observation):
        return {self.SLOT: {"etag": 'W/"a"', "pages": 1, "observation": observation}}

    @staticmethod
    def unchanged(etag):
        return Response(304, {}, b"") if etag == 'W/"a"' else github(github_release("v1.1.0", 99))

    def test_an_observation_that_is_not_usable_makes_the_request_unconditional(self):
        self.listing(releases=GITHUB_ONLY)
        for observation in ({"total": -1, "versions": {}}, {"total": 1}, "counts", {"total": True, "versions": {}}):
            with self.subTest(observation=observation):
                self.cache = self.remembered(observation)
                http = FakeHttp({GITHUB: self.unchanged})
                self.refresh(http)
                self.assertEqual(http.asked[0][1], None)

    def test_a_304_keeps_a_version_the_remembered_answer_no_longer_had(self):
        self.listing(releases=GITHUB_ONLY)
        self.cache = self.remembered({"total": 10, "versions": {"1.1.0": 10}})
        stored = document(counted(
            "Mod", {"github": 30}, ("1.1.0", {"github": 10}), ("1.0.0", {"github": 20})
        ))
        entry = self.only(self.refresh(FakeHttp({GITHUB: self.unchanged}), stored))
        self.assertEqual(entry["hosts"], {"github": 30})
        self.assertEqual([release["version"] for release in entry["releases"]], ["1.1.0", "1.0.0"])
        self.assertTrue(any("unchanged since the last run" in note for note in self.notes))

    def test_a_cache_that_cannot_be_read_starts_cold(self):
        self.listing(releases=GITHUB_ONLY)
        valid = {"version": 1, "hosts": self.remembered({"total": 5, "versions": {"1.1.0": 5}})}
        cases = {
            "valid": (json.dumps(valid), 'W/"a"'),
            "not json": ("{oops", None),
            "another version": (json.dumps({**valid, "version": 99}), None),
            "hosts not an object": (json.dumps({"version": 1, "hosts": []}), None),
        }
        for case, (text, expected) in cases.items():
            with self.subTest(case):
                (self.root / "cache.json").write_text(text, encoding="utf-8")
                http = FakeHttp({GITHUB: self.unchanged})
                code = CommandLine.run_main(self, http)[0]
                self.assertEqual(code, 0)
                self.assertEqual(http.asked[0][1], expected)


class PruneOnly(Fixture):
    run_main = CommandLine.run_main

    def test_it_drops_a_listing_that_is_gone_and_asks_no_host(self):
        self.listing("Mod", releases=GITHUB_ONLY)
        counts = self.root / "download-counts.json"
        stored = document(
            counted("Gone", {"github": 9}, ("1.0.0", {"github": 9})),
            counted("Mod", {"github": 1}, ("1.0.0", {"github": 1})),
        )
        counts.write_text(serialize(stored), encoding="utf-8")
        http = FakeHttp({})
        code, counts, _, _ = self.run_main(http, "--prune-only")
        self.assertEqual(code, 0)
        self.assertEqual(http.asked, [])
        self.assertEqual(json.loads(counts.read_text(encoding="utf-8")), document(stored["listings"][1]))
        self.assertFalse((self.root / "cache.json").exists())

    def test_nothing_to_drop_keeps_the_bytes(self):
        self.listing("Mod", releases=GITHUB_ONLY)
        counts = self.root / "download-counts.json"
        # Bytes and not text, so Windows does not turn the newlines into CRLF.
        raw = serialize(document(counted("Mod", {"github": 1}, ("1.0.0", {"github": 1})))).encode("utf-8")
        counts.write_bytes(raw)
        code, counts, _, stderr = self.run_main(FakeHttp({}), "--prune-only")
        self.assertEqual(code, 0)
        self.assertEqual(counts.read_bytes(), raw)
        self.assertIn("already current", stderr)

    def test_no_document_yet_writes_nothing(self):
        self.listing("Mod", releases=GITHUB_ONLY)
        code, counts, _, _ = self.run_main(FakeHttp({}), "--prune-only")
        self.assertEqual(code, 0)
        self.assertFalse(counts.exists())


class BuilderCheck(unittest.TestCase):
    def test_a_valid_document_comes_back_keyed_by_lowercased_id(self):
        entries = check_download_counts(document(counted("Mod", {"github": 1})), "test")
        self.assertEqual(list(entries), ["mod"])

    def test_a_document_that_is_not_an_object_is_refused_by_the_loader(self):
        with self.assertRaises(SnapshotError):
            check_download_counts(document("Mod"), "test")


if __name__ == "__main__":
    unittest.main()
