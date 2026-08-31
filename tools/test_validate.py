#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the unprivileged validation.

The published version comes from git, so these run against a real repository built in a temporary directory.
"""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import validate

PATH = "releases/Mod/1.0.0.json"

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
        self.git("add", "-A")
        self.git("commit", "-m", "Stamp Mod 1.0.0")

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
        # It waits for a steward through the scope rule rather than failing the check, because nothing here re-derives a submitted release yet.
        new = "releases/Mod/2.0.0.json"
        self.write(new, self.amended(version="2.0.0"))
        check = validate.run_amendment([new], base_ref="main")
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
