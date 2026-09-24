#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for what a listing edit changes in the newest release, and for the history the watcher reads it from.

The history comes from git, so those tests run against real repositories built in a temporary directory.
"""

import copy
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import listing_edit
from check_amendment import check_document
from stamp_release import StampError

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
GAME_VERSIONS = ["2026.8.3.5117", "2026.8.20.5300", "2026.9.10.5438"]
WHERE = "releases/M/1.1.0.json"

DERIVED = [{"id": "Lib", "kind": "optional", "source": "derived"}]
RIVAL = {"id": "Rival", "kind": "conflict", "max": "2.0.0", "source": "authored"}

RELEASE = {
    "spec_version": 1,
    "id": "M",
    "type": "mod",
    "version": "1.1.0",
    "version_scheme": "semver",
    "release_status": "stable",
    "release_date": "2026-09-01T00:00:00Z",
    "game_min": "2026.8.20.5300",
    "game_min_revision": 5300,
    "download": {"url": "https://example.invalid/M.zip", "sha256": "AB", "size": 1, "content_type": "application/zip"},
    "install_size": 1,
    "install": {"root": "M", "derived": True},
    "loader": {"id": "StarMap", "min": "0.4.0", "source": "authored"},
    "dependencies": DERIVED + [RIVAL],
    "listing": {"name": "M"},
}

LISTING = {
    "id": "M",
    "compatibility": {"game_min": "2026.8.20.5300"},
    "loader": {"id": "StarMap", "min": "0.4.0"},
    "dependencies": [{"id": "Rival", "kind": "conflict", "max": "2.0.0"}],
}

MOD_MENU = {"id": "ModMenu", "kind": "recommends"}


def moment(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def listing(**changes):
    document = copy.deepcopy(LISTING)
    document.update(changes)
    return document


def compatibility(**bounds):
    return listing(compatibility={**LISTING["compatibility"], **bounds})


def release(**changes):
    document = copy.deepcopy(RELEASE)
    document.update(changes)
    return document


def unused():
    raise AssertionError("the archive is read although the dependencies did not change")


def edited(after, before=LISTING, target=RELEASE, derived=lambda: DERIVED):
    """The release after the edit, the applied descriptions, and what was refused."""
    found = listing_edit.changes(before, after, target, GAME_VERSIONS, NOW, derived)
    return listing_edit.amended(WHERE, target, found, derived)


class WhatAnEditChanges(unittest.TestCase):
    def assert_owner_amendment(self, head, base=RELEASE):
        errors = []
        check_document(WHERE, base, head, errors, owner_only=[], derived=DERIVED)
        self.assertEqual(errors, [])

    def test_an_added_dependency_is_added_as_authored(self):
        head, applied, refused = edited(
            listing(dependencies=LISTING["dependencies"] + [MOD_MENU])
        )
        self.assertEqual(applied, ["adds the recommends dependency ModMenu"])
        self.assertEqual(refused, [])
        self.assertEqual(head["dependencies"], DERIVED + [RIVAL, {**MOD_MENU, "source": "authored"}])
        self.assert_owner_amendment(head)

    def test_a_removed_dependency_is_removed(self):
        head, applied, _ = edited(listing(dependencies=[]))
        self.assertEqual(applied, ["removes the conflict dependency Rival"])
        self.assertEqual(head["dependencies"], DERIVED)
        self.assert_owner_amendment(head)

    def test_removing_an_entry_that_overrode_a_derived_one_leaves_the_derived_one(self):
        override = {"id": "Lib", "kind": "required", "min": "1.0.0"}
        before = listing(dependencies=[override])
        target = release(dependencies=[{**override, "source": "authored"}])
        head, applied, _ = edited(listing(dependencies=[]), before=before, target=target)
        self.assertEqual(head["dependencies"], DERIVED)
        self.assertEqual(applied, ["changes the required dependency Lib to optional"])
        self.assert_owner_amendment(head, base=target)

    def test_a_new_kind_on_a_derived_entry_is_authored_over_it(self):
        after = listing(dependencies=LISTING["dependencies"] + [{"id": "Lib", "kind": "recommends"}])
        head, applied, _ = edited(after)
        self.assertEqual(applied, ["changes the optional dependency Lib to recommends"])
        self.assertEqual(
            head["dependencies"], [{"id": "Lib", "kind": "recommends", "source": "authored"}, RIVAL]
        )
        self.assert_owner_amendment(head)

    def test_an_entry_that_restates_a_derived_one_changes_nothing(self):
        after = listing(dependencies=LISTING["dependencies"] + [{"id": "Lib", "kind": "optional"}])
        self.assertEqual(listing_edit.changes(LISTING, after, RELEASE, GAME_VERSIONS, NOW, lambda: DERIVED), {})

    def test_an_entry_the_release_lost_to_an_amendment_does_not_come_back(self):
        # Rival stood in the listing at the stamp and was amended away, so only ModMenu is the edit.
        target = release(dependencies=DERIVED)
        head, applied, _ = edited(
            listing(dependencies=LISTING["dependencies"] + [MOD_MENU]), target=target
        )
        self.assertEqual(head["dependencies"], DERIVED + [{**MOD_MENU, "source": "authored"}])

    def test_the_archive_is_read_only_when_the_dependencies_changed(self):
        _, applied, _ = edited(compatibility(game_min="2026.9"), derived=unused)
        self.assertEqual(applied, ["raises game_min to 2026.9.10.5438"])

    def test_an_archive_that_cannot_be_read_refuses_the_dependencies_alone(self):
        def broken():
            raise StampError("the archive is gone")

        after = listing(
            compatibility={"game_min": "2026.9"},
            dependencies=LISTING["dependencies"] + [MOD_MENU],
        )
        head, applied, refused = edited(after, derived=broken)
        self.assertEqual(applied, ["raises game_min to 2026.9.10.5438"])
        self.assertEqual(refused, [(["changes the dependencies"], ["the archive is gone"])])
        self.assertEqual(head["dependencies"], RELEASE["dependencies"])

    def test_game_min_moves_both_ways(self):
        for bound, description in (
            ("2026.9", "raises game_min to 2026.9.10.5438"),
            ("2026.8.3.5117", "lowers game_min to 2026.8.3.5117"),
        ):
            with self.subTest(bound=bound):
                head, applied, _ = edited(compatibility(game_min=bound))
                self.assertEqual(applied, [description])
                self.assert_owner_amendment(head)

    def test_game_max_is_added_raised_and_removed(self):
        capped = release(game_max="2026.8.20.5300", game_max_revision=5300)
        before = compatibility(game_max="2026.8.20.5300")
        cases = (
            (LISTING, RELEASE, compatibility(game_max="2026.8"), "adds game_max 2026.8.20.5300"),
            (before, capped, compatibility(game_max="2026.9.10.5438"), "raises game_max to 2026.9.10.5438"),
            (before, capped, LISTING, "removes game_max"),
        )
        for before, target, after, description in cases:
            with self.subTest(description=description):
                head, applied, _ = edited(after, before=before, target=target)
                self.assertEqual(applied, [description])
                self.assert_owner_amendment(head, base=target)
        self.assertNotIn("game_max_revision", head)

    def test_a_game_max_month_that_is_not_over_waits(self):
        with self.assertRaises(listing_edit.Wait):
            edited(compatibility(game_max="2026.9"))

    def test_os_lands_where_a_stamp_puts_it(self):
        head, applied, _ = edited(compatibility(os=["windows"]))
        self.assertEqual(applied, ["adds os windows"])
        keys = list(head)
        self.assertEqual(keys[keys.index("game_min_revision") + 1], "os")
        self.assert_owner_amendment(head)

        head, applied, _ = edited(LISTING, before=compatibility(os=["windows"]), target=head)
        self.assertEqual(applied, ["removes os"])
        self.assertNotIn("os", head)

    def test_loader_bounds_move_both_ways(self):
        after = listing(loader={"id": "StarMap", "min": "0.3.0", "max": "0.6.0"})
        head, applied, _ = edited(after)
        self.assertEqual(
            applied, ["lowers the min of the loader to 0.3.0", "adds the max 0.6.0 to the loader"]
        )
        self.assertEqual(
            head["loader"], {"id": "StarMap", "min": "0.3.0", "max": "0.6.0", "source": "authored"}
        )
        self.assert_owner_amendment(head)

    def test_a_changed_loader_id_is_not_applied(self):
        after = listing(loader={"id": "OtherLoader", "min": "0.5.0"})
        self.assertEqual(listing_edit.changes(LISTING, after, RELEASE, GAME_VERSIONS, NOW, unused), {})

    def test_a_change_the_check_refuses_is_reported_and_the_others_land(self):
        after = listing(
            compatibility={"game_min": "2026.8.20.5300", "game_max": "2026.8.3.5117"},
            dependencies=LISTING["dependencies"] + [MOD_MENU],
        )
        head, applied, refused = edited(after)
        self.assertEqual(applied, ["adds the recommends dependency ModMenu"])
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0][0], ["adds game_max 2026.8.3.5117"])
        self.assertTrue(any("below game_min_revision" in error for error in refused[0][1]))
        self.assertNotIn("game_max", head)

    def test_an_unchanged_listing_changes_nothing(self):
        self.assertEqual(listing_edit.changes(LISTING, LISTING, RELEASE, GAME_VERSIONS, NOW, unused), {})

    def test_a_second_pass_finds_nothing(self):
        after = listing(
            compatibility={"game_min": "2026.9", "os": ["linux"]},
            loader={"id": "StarMap", "min": "0.5.0"},
            dependencies=[MOD_MENU],
        )
        head, applied, _ = edited(after)
        self.assertEqual(len(applied), 5)
        self.assertEqual(listing_edit.changes(LISTING, after, head, GAME_VERSIONS, NOW, lambda: DERIVED), {})


class TheTarget(unittest.TestCase):
    def test_the_newest_release_neither_yanked_nor_dev(self):
        documents = {
            "1.0.0": {},
            "1.2.0": {},
            "1.10.0": {"yanked": True},
            "1.3.0-dev.1": {"release_status": "dev"},
            "1.2.1-rc.1": {"release_status": "testing"},
        }
        self.assertEqual(listing_edit.target(documents), "1.2.1-rc.1")

    def test_nothing_left_is_no_target(self):
        self.assertIsNone(listing_edit.target({"1.0.0": {"yanked": True}}))


class TheHistory(unittest.TestCase):
    """Both checkouts as real repositories, with commit times set by hand."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.authored = self.root / "authored"
        self.releases = self.root / "generated"
        for repository in (self.authored, self.releases):
            repository.mkdir()
            self.git(repository, "init", "-b", "main")
            self.git(repository, "config", "user.email", "test@example.invalid")
            self.git(repository, "config", "user.name", "Test")
        self.history = listing_edit.History(self.authored, self.releases / "releases")

    def git(self, repository, *arguments, when=None):
        environment = dict(os.environ)
        if when:
            environment.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        return subprocess.run(
            ["git", *arguments], cwd=repository, env=environment, capture_output=True, check=True
        )

    def commit(self, repository, path, text, when, message=None):
        where = repository / path
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text, encoding="utf-8")
        self.git(repository, "add", "-A")
        self.git(repository, "commit", "-m", message or f"Change {path}", when=when)

    def test_the_listing_as_it_stood_at_a_stamp(self):
        path = self.authored / "listings" / "M.toml"
        self.commit(self.authored, "listings/M.toml", 'id = "M"\n', "2026-09-01T00:00:00Z")
        self.commit(self.authored, "listings/M.toml", 'id = "M"\nname = "M"\n', "2026-09-03T00:00:00Z")
        self.commit(self.authored, "other.txt", "x", "2026-09-05T00:00:00Z")

        sha, committed = self.history.listing_change(path)
        self.assertEqual(moment(committed), moment("2026-09-03T00:00:00Z"))
        self.assertGreaterEqual(len(sha), 7)
        self.assertEqual(self.history.listing_at(path, "2026-09-02T00:00:00Z"), 'id = "M"')
        self.assertIsNone(self.history.listing_at(path, "2026-08-01T00:00:00Z"))

    def test_a_merged_release_counts_from_its_merge(self):
        self.commit(self.releases, "releases/M/1.0.0.json", "{}\n", "2026-09-01T00:00:00Z")
        self.git(self.releases, "checkout", "-b", "release")
        self.commit(self.releases, "releases/M/1.1.0.json", "{}\n", "2026-09-02T00:00:00Z")
        self.git(self.releases, "checkout", "main")
        self.git(self.releases, "merge", "--no-ff", "-m", "Merge", "release", when="2026-09-04T00:00:00Z")
        self.commit(self.releases, "releases/M/1.0.0.json", '{"yanked": true}\n', "2026-09-06T00:00:00Z")

        stamped = self.history.last_stamp(self.releases / "releases" / "M")
        self.assertEqual(moment(stamped), moment("2026-09-04T00:00:00Z"))
        stamped = self.history.last_stamp(self.releases / "releases" / "M" / "1.0.0.json")
        self.assertEqual(moment(stamped), moment("2026-09-01T00:00:00Z"))

    def test_an_uncommitted_release_file_has_no_stamp_time(self):
        self.commit(self.releases, "releases/M/1.0.0.json", "{}\n", "2026-09-01T00:00:00Z")
        (self.releases / "releases" / "M" / "1.1.0.json").write_text("{}\n", encoding="utf-8")
        self.assertIsNone(self.history.last_stamp(self.releases / "releases" / "M"))

    def test_the_listing_commit_a_release_file_received_is_found(self):
        listing = self.authored / "listings" / "M.toml"
        self.commit(self.authored, "listings/M.toml", 'id = "M"\n', "2026-09-02T00:00:00Z")
        sha, _ = self.history.listing_change(listing)
        path = self.releases / "releases" / "M" / "1.0.0.json"
        self.commit(self.releases, "releases/M/1.0.0.json", "{}\n", "2026-09-01T00:00:00Z")
        self.assertIsNone(self.history.received(path, "KSAModding/content-index"))
        self.commit(
            self.releases, "releases/M/1.0.0.json", "{ }\n", "2026-09-03T00:00:00Z",
            message=f"Apply the listing of M to 1.0.0\n\nListing: KSAModding/content-index@{sha}\n- removes os",
        )
        self.assertEqual(self.history.received(path, "KSAModding/content-index"), sha)
        self.assertIsNone(self.history.received(path, "Other/content-index"))
        self.assertEqual(moment(self.history.committed(listing, sha)), moment("2026-09-02T00:00:00Z"))
        self.assertEqual(self.history.listing_in(listing, sha), 'id = "M"')
        self.assertIsNone(self.history.committed(listing, "0000000"))

    def test_a_shallow_clone_is_not_a_history(self):
        self.commit(self.authored, "listings/M.toml", 'id = "M"\n', "2026-09-01T00:00:00Z")
        self.commit(self.authored, "listings/M.toml", 'id = "N"\n', "2026-09-02T00:00:00Z")
        self.commit(self.releases, "releases/M/1.0.0.json", "{}\n", "2026-09-01T00:00:00Z")
        shallow = self.root / "shallow"
        self.git(self.root, "clone", "--depth", "1", self.authored.as_uri(), str(shallow))
        self.assertIsNone(self.history.problem())
        self.assertIn(
            "full history", listing_edit.History(shallow, self.releases / "releases").problem()
        )


if __name__ == "__main__":
    unittest.main()
