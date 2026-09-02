#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the unprivileged validation.

The published version comes from git, so these run against a real repository built in a temporary directory.
"""

import copy
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
import unittest.mock
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import hosts
import validate
from stamp_release import stamp

PATH = "releases/Mod/1.0.0.json"
NEW = "releases/Mod/2.0.0.json"
URL = "https://example.invalid/Mod-2.0.0.zip"

GAME_VERSIONS = {
    "spec_version": 1,
    "source": "test",
    "versions": ["2026.8.3.5117", "2026.8.19.5261"],
}

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)

LISTING_TOML = """\
spec_version = 1
id = "Mod"
type = "mod"
name = "Mod"
authors = ["Maxi"]
abstract = "A mod."
license = "MIT"

[links]
forums = "https://forums.ahwoo.com/threads/mod.1/"
repository = "https://github.com/someone/Mod"

[compatibility]
game_min = "2026.8.3.5117"
"""

FACTS = {
    "tag": "v2.0.0",
    "release_date": "2026-08-19T10:00:00Z",
    "url": URL,
    "content_type": "application/zip",
    "prerelease": False,
    "changelog": "https://example.invalid/changes",
}


def archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr("Mod/Mod.dll", "x" * 10)
        handle.writestr("Mod/mod.toml", 'name = "Mod"\n')
    return buffer.getvalue()


class FakeHttp:
    """Serves bytes by URL and refuses anything else."""

    def __init__(self, routes):
        self.routes = routes

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        if url not in self.routes:
            raise AssertionError(f"unexpected URL {url}")
        return hosts.Response(200, {"Content-Type": "application/octet-stream"}, self.routes[url])


RELEASE = {
    "spec_version": 1,
    "id": "Mod",
    "type": "mod",
    "version": "1.0.0",
    "version_scheme": "semver",
    "release_status": "stable",
    "release_date": "2026-08-11T16:41:04Z",
    "game_min": "2026.8.19.5261",
    "game_min_revision": 5261,
    "download": {
        "url": "https://example.invalid/a.zip",
        "sha256": "AB",
        "size": 1,
        "content_type": "application/zip",
    },
    "install_size": 2,
    "install": {"root": "Mod", "derived": True},
    "dependencies": [],
    "listing": {"name": "Mod", "authors": ["Maxi"]},
}


class Repository(unittest.TestCase):
    """A repository with one published release on `main`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

        self.original = validate.ROOT
        validate.ROOT = self.root
        self.addCleanup(self.restore)

        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.write(PATH, RELEASE)
        self.write("game-versions.json", GAME_VERSIONS)
        self.git("add", "-A")
        self.git("commit", "-m", "Stamp Mod 1.0.0")

        # The authored half beside it, with the one listing.
        self.authored = self.root / "content-index"
        (self.authored / "listings").mkdir(parents=True)
        (self.authored / "listings" / "Mod.toml").write_text(LISTING_TOML, encoding="utf-8")

    def restore(self):
        validate.ROOT = self.original

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments], cwd=self.root, capture_output=True, check=True
        )

    def write(self, path, document):
        where = self.root / path
        where.parent.mkdir(parents=True, exist_ok=True)
        if document is None:
            where.unlink()
            return
        where.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def amended(self, **changes):
        document = copy.deepcopy(RELEASE)
        document.update(changes)
        return document


class BaseRef(Repository):
    def test_a_ref_that_does_not_resolve_is_unavailable(self):
        with self.assertRaises(validate.Unavailable):
            validate.resolve_base("no-such-ref")

    def test_the_message_names_the_fetch_depth_a_run_needs(self):
        with self.assertRaises(validate.Unavailable) as raised:
            validate.resolve_base("HEAD^1")
        self.assertIn("fetch-depth 2", str(raised.exception))

    def test_a_ref_that_resolves_gives_a_commit(self):
        self.assertEqual(len(validate.resolve_base("main")), 40)


class BaseDocument(Repository):
    def test_a_published_file_comes_back(self):
        document, problem = validate.base_document(validate.resolve_base("main"), PATH)
        self.assertIsNone(problem)
        self.assertEqual(document["version"], "1.0.0")

    def test_a_file_that_is_not_there_is_a_new_release(self):
        document, problem = validate.base_document(
            validate.resolve_base("main"), "releases/Mod/2.0.0.json"
        )
        self.assertIsNone(document)
        self.assertIsNone(problem)

    def test_a_published_file_that_does_not_parse_is_a_problem(self):
        self.write(PATH, None)
        (self.root / PATH).write_text("{ not json", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "Break it")
        document, problem = validate.base_document(validate.resolve_base("main"), PATH)
        self.assertIsNone(document)
        self.assertIn("does not parse", problem)


class HeadDocument(Repository):
    def test_the_working_tree_is_the_head(self):
        self.assertEqual(validate.head_document(PATH)["version"], "1.0.0")

    def test_a_file_that_is_gone_is_deleted(self):
        self.write(PATH, None)
        self.assertIsNone(validate.head_document(PATH))

    def test_a_path_outside_releases_is_refused(self):
        with self.assertRaises(ValueError):
            validate.head_document("releases/../game-versions.json")

    def test_a_file_that_does_not_parse_is_named(self):
        (self.root / PATH).write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            validate.head_document(PATH)


class RunAmendment(Repository):
    def test_a_narrowing_change_passes(self):
        self.write(PATH, self.amended(game_max="2026.8.19.5261", game_max_revision=5261))
        check = validate.run_amendment([PATH], base_ref="main")
        self.assertEqual(check.outcome, validate.PASS)

    def test_a_widening_change_is_rejected(self):
        self.write(PATH, self.amended(game_min="2026.8.3.5117", game_min_revision=5117))
        check = validate.run_amendment([PATH], base_ref="main")
        self.assertEqual(check.outcome, validate.REJECT)
        self.assertTrue(any("falls" in message for message in check.messages))

    def test_a_message_names_the_file_it_is_about(self):
        self.write(PATH, self.amended(version="9.9.9"))
        check = validate.run_amendment([PATH], base_ref="main")
        self.assertTrue(all(message.startswith(PATH) for message in check.messages))

    def test_a_base_that_does_not_resolve_reaches_no_verdict(self):
        check = validate.run_amendment([PATH], base_ref="no-such-ref")
        self.assertEqual(check.outcome, validate.COULD_NOT_EVALUATE)

    def test_no_release_file_is_nothing_to_check(self):
        check = validate.run_amendment([], base_ref="main")
        self.assertEqual(check.outcome, validate.PASS)
        self.assertTrue(any("nothing is amended" in message for message in check.messages))

    def test_a_head_that_does_not_parse_is_rejected(self):
        (self.root / PATH).write_text("{ not json", encoding="utf-8")
        check = validate.run_amendment([PATH], base_ref="main")
        self.assertEqual(check.outcome, validate.REJECT)

    def test_a_new_release_file_is_not_an_amendment(self):
        # `main` routes a new file to the release check; a caller that hands one here gets a note and no verdict on it.
        self.write(NEW, self.amended(version="2.0.0"))
        check = validate.run_amendment([NEW], base_ref="main")
        self.assertEqual(check.outcome, validate.PASS)
        self.assertTrue(any("new release file" in message for message in check.messages))

    def test_a_published_file_that_does_not_parse_reaches_no_verdict(self):
        # Repository state, not something the pull request did.
        self.write(PATH, None)
        (self.root / PATH).write_text("{ not json", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "Break it")
        self.write(PATH, self.amended(yanked=True))
        check = validate.run_amendment([PATH], base_ref="main")
        self.assertEqual(check.outcome, validate.COULD_NOT_EVALUATE)

    def test_a_head_that_does_not_parse_is_the_authors_and_is_rejected(self):
        (self.root / PATH).write_text("{ not json", encoding="utf-8")
        check = validate.run_amendment([PATH], base_ref="main")
        self.assertEqual(check.outcome, validate.REJECT)


class RunRelease(Repository):
    """A submitted release, against a fake host serving its archive."""

    def setUp(self):
        super().setUp()
        self.archive = archive()
        self.http = FakeHttp({URL: self.archive})

    def stamped(self, **changes):
        document = stamp(
            tomllib.loads(LISTING_TOML), FACTS, self.archive, GAME_VERSIONS["versions"], now=NOW
        )
        document.update(changes)
        return document

    def measure(self, paths, **changes):
        options = {"base_ref": "main", "authored": self.authored, "http": self.http, "now": NOW}
        options.update(changes)
        return validate.run_release(paths, **options)

    def test_a_file_the_stamper_wrote_passes(self):
        self.write(NEW, self.stamped())
        check = self.measure([NEW])
        self.assertEqual(check.outcome, validate.PASS, check.messages)
        self.assertEqual(check.name, "release")
        self.assertTrue(all(message.startswith(NEW) for message in check.messages))

    def test_a_field_the_archive_contradicts_is_rejected(self):
        self.write(NEW, self.stamped(install_size=1))
        check = self.measure([NEW])
        self.assertEqual(check.outcome, validate.REJECT)
        self.assertTrue(any("install_size" in message for message in check.messages))

    def test_a_version_that_is_already_published_is_rejected_before_any_download(self):
        self.write(PATH, self.stamped(version="1.0.0"))
        check = self.measure([PATH], http=FakeHttp({}))
        self.assertEqual(check.outcome, validate.REJECT)
        self.assertTrue(any("stamped exactly once" in message for message in check.messages))

    def test_a_head_that_does_not_parse_is_rejected(self):
        (self.root / NEW).write_text("{ not json", encoding="utf-8")
        check = self.measure([NEW], http=FakeHttp({}))
        self.assertEqual(check.outcome, validate.REJECT)

    def test_a_missing_authored_checkout_reaches_no_verdict(self):
        self.write(NEW, self.stamped())
        with tempfile.TemporaryDirectory() as folder:
            check = self.measure([NEW], authored=folder)
        self.assertEqual(check.outcome, validate.COULD_NOT_EVALUATE)
        self.assertIn("content-index", check.messages[0])

    def test_a_base_that_does_not_resolve_reaches_no_verdict(self):
        self.write(NEW, self.stamped())
        check = self.measure([NEW], base_ref="no-such-ref")
        self.assertEqual(check.outcome, validate.COULD_NOT_EVALUATE)

    def test_a_missing_game_release_list_reaches_no_verdict(self):
        self.write(NEW, self.stamped())
        (self.root / "game-versions.json").unlink()
        check = self.measure([NEW])
        self.assertEqual(check.outcome, validate.COULD_NOT_EVALUATE)
        self.assertIn("game release list", check.messages[0])

    def test_a_published_file_that_does_not_parse_reaches_no_verdict(self):
        self.write(PATH, None)
        (self.root / PATH).write_text("{ not json", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "Break it")
        check = self.measure([PATH], http=FakeHttp({}))
        self.assertEqual(check.outcome, validate.COULD_NOT_EVALUATE)

    def fake_http(self):
        original = hosts.Http
        self.http_options = []
        hosts.Http = lambda **options: (self.http_options.append(options), self.http)[1]
        self.addCleanup(setattr, hosts, "Http", original)

    def test_the_release_check_carries_no_token(self):
        # The URL is the author's, so the job's token stays off this path.
        self.write(NEW, self.stamped())
        self.fake_http()
        with unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}):
            check = self.measure([NEW], http=None)
        self.assertEqual(check.outcome, validate.PASS, check.messages)
        self.assertEqual(self.http_options, [{}])

    def test_two_added_files_fetch_nothing(self):
        other = "releases/Mod/2.1.0.json"
        self.write(NEW, self.stamped())
        self.write(other, self.stamped(version="2.1.0"))
        self.fake_http()
        self.http.routes = {}
        output = self.root / "verdict.json"
        code = validate.main(
            ["--changed", NEW, other, "--base-ref", "main", "--authored", str(self.authored),
             "--output", str(output)]
        )
        verdict = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(verdict["verdict"], validate.REJECT)
        self.assertFalse(verdict["auto_merge_candidate"])
        release = next(check for check in verdict["checks"] if check["name"] == "release")
        self.assertIn("exactly one", release["messages"][0])

    def local_run(self, path):
        self.fake_http()
        output = self.root / "verdict.json"
        code = validate.main(
            ["--changed", path, "--base-ref", "main", "--authored", str(self.authored),
             "--output", str(output)]
        )
        return code, json.loads(output.read_text(encoding="utf-8"))

    def test_a_local_run_reads_a_file_main_lacks_as_a_submitted_release(self):
        self.write(NEW, self.stamped())
        code, verdict = self.local_run(NEW)
        self.assertEqual(code, 0)
        self.assertEqual(verdict["verdict"], validate.PASS)
        self.assertTrue(verdict["auto_merge_candidate"])
        self.assertEqual([check["name"] for check in verdict["checks"]], ["release"])
        self.assertEqual(verdict["documents"], [NEW])

    def test_a_local_run_still_measures_a_published_file_as_an_amendment(self):
        self.write(PATH, self.amended(yanked=True))
        code, verdict = self.local_run(PATH)
        self.assertEqual(code, 0)
        self.assertEqual([check["name"] for check in verdict["checks"]], ["amendment"])
        self.assertTrue(verdict["auto_merge_candidate"])

    def test_a_rejected_release_leaves_a_non_zero_exit(self):
        self.write(NEW, self.stamped(install_size=1))
        code, verdict = self.local_run(NEW)
        self.assertEqual(code, 1)
        self.assertEqual(verdict["verdict"], validate.REJECT)


class LocalChanges(Repository):
    def test_a_release_file_main_lacks_is_added(self):
        changes = validate.local_changes([NEW, PATH, "tools/amend.py"], "main")
        self.assertEqual(
            [change.status for change in changes],
            [validate.check_scope.ADDED, validate.check_scope.MODIFIED, validate.check_scope.MODIFIED],
        )

    def test_a_base_that_does_not_resolve_reads_everything_as_modified(self):
        changes = validate.local_changes([NEW], "no-such-ref")
        self.assertEqual(changes[0].status, validate.check_scope.MODIFIED)


class Verdict(Repository):
    def verdict(self, output):
        return json.loads(Path(output).read_text(encoding="utf-8"))

    def test_a_local_run_writes_a_verdict(self):
        self.write(PATH, self.amended(yanked=True))
        output = self.root / "verdict.json"
        code = validate.main(
            ["--changed", PATH, "--base-ref", "main", "--output", str(output)]
        )
        verdict = self.verdict(output)
        self.assertEqual(code, 0)
        self.assertEqual(verdict["verdict"], validate.PASS)
        self.assertTrue(verdict["auto_merge_candidate"])
        self.assertEqual(verdict["documents"], [PATH])
        self.assertEqual(verdict["schema_version"], validate.VERDICT_SCHEMA_VERSION)

    def test_a_rejection_leaves_a_non_zero_exit(self):
        self.write(PATH, self.amended(version="9.9.9"))
        output = self.root / "verdict.json"
        code = validate.main(
            ["--changed", PATH, "--base-ref", "main", "--output", str(output)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.verdict(output)["verdict"], validate.REJECT)

    def test_a_change_outside_releases_is_not_a_candidate(self):
        output = self.root / "verdict.json"
        validate.main(
            ["--changed", "tools/amend.py", "--base-ref", "main", "--output", str(output)]
        )
        verdict = self.verdict(output)
        self.assertFalse(verdict["auto_merge_candidate"])
        self.assertIn("no release file", verdict["scope_reason"])


class Helpers(unittest.TestCase):
    def test_the_worst_outcome_wins(self):
        self.assertEqual(validate.worst(["pass", "reject"]), "reject")
        self.assertEqual(validate.worst(["pass", "could-not-evaluate"]), "could-not-evaluate")
        self.assertEqual(validate.worst(["pass"]), "pass")
        self.assertEqual(validate.worst([]), "pass")

    def test_the_summary_names_every_check(self):
        verdict = validate._verdict([validate.Check("amendment", "pass", ["fine"])])
        summary = validate.summarise(verdict)
        self.assertIn("amendment", summary)
        self.assertIn("fine", summary)

    def test_a_check_with_no_messages_still_renders(self):
        verdict = validate._verdict([validate.Check("amendment", "pass")])
        self.assertIn("nothing to report", validate.summarise(verdict))

    def test_the_next_page_comes_from_the_link_header(self):
        link = '<https://api.github.com/x?page=2>; rel="next", <https://x>; rel="last"'
        self.assertEqual(validate._next_page(link), "https://api.github.com/x?page=2")

    def test_no_link_header_is_the_last_page(self):
        self.assertIsNone(validate._next_page(""))


if __name__ == "__main__":
    unittest.main()
