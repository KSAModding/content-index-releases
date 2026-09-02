#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the auto-merge scope rule."""

import unittest

import check_scope


class IsRelease(unittest.TestCase):
    def test_a_stamped_release_is_one(self):
        self.assertTrue(check_scope.is_release("releases/AdvancedFlightComputer/0.7.2.json"))

    def test_a_pre_release_version_is_one(self):
        self.assertTrue(check_scope.is_release("releases/Mod/1.0.0-beta.1.json"))

    def test_the_suffix_is_matched_case_insensitively(self):
        self.assertTrue(check_scope.is_release("releases/Mod/1.0.0.JSON"))

    def test_the_folder_name_is_matched_case_sensitively(self):
        self.assertFalse(check_scope.is_release("Releases/Mod/1.0.0.json"))

    def test_a_trailing_newline_does_not_smuggle_a_path_through(self):
        self.assertFalse(check_scope.is_release("releases/Mod/1.0.0.json\nx"))

    def test_a_release_at_the_wrong_depth_is_not_one(self):
        self.assertFalse(check_scope.is_release("releases/1.0.0.json"))
        self.assertFalse(check_scope.is_release("releases/Mod/nested/1.0.0.json"))

    def test_a_traversing_folder_is_not_one(self):
        # It would resolve outside releases/, so it never reaches a reader.
        self.assertFalse(check_scope.is_release("releases/../evil.json"))
        self.assertFalse(check_scope.is_release("releases/./evil.json"))

    def test_the_game_release_list_is_not_one(self):
        self.assertFalse(check_scope.is_release("game-versions.json"))

    def test_the_tooling_is_not_one(self):
        self.assertFalse(check_scope.is_release("tools/amend.py"))

    def test_a_path_that_only_starts_like_one_is_not_one(self):
        self.assertFalse(check_scope.is_release("releases-archive/Mod/1.0.0.json"))


class ListingOf(unittest.TestCase):
    def test_the_folder_names_the_listing(self):
        self.assertEqual(
            check_scope.listing_of("releases/AdvancedFlightComputer/0.7.2.json"),
            "AdvancedFlightComputer",
        )

    def test_anything_else_names_nothing(self):
        self.assertIsNone(check_scope.listing_of("tools/amend.py"))


class Evaluate(unittest.TestCase):
    def evaluate(self, paths, status="modified"):
        return check_scope.evaluate(check_scope.changes(paths, status))

    def test_one_amended_release_merges_itself(self):
        candidate, paths, reason = self.evaluate(["releases/Mod/1.0.0.json"])
        self.assertTrue(candidate)
        self.assertEqual(paths, ["releases/Mod/1.0.0.json"])
        self.assertEqual(reason, "")

    def test_several_releases_of_one_listing_merge_themselves(self):
        # RFC 0033: "this mod breaks above game build X" is one pull request.
        candidate, paths, _ = self.evaluate(
            ["releases/Mod/1.0.0.json", "releases/Mod/1.1.0.json", "releases/Mod/1.2.0.json"]
        )
        self.assertTrue(candidate)
        self.assertEqual(len(paths), 3)

    def test_two_listings_wait_for_a_steward(self):
        candidate, _, reason = self.evaluate(
            ["releases/A/1.0.0.json", "releases/B/1.0.0.json"]
        )
        self.assertFalse(candidate)
        self.assertIn("2 listings", reason)

    def test_a_release_next_to_anything_else_waits(self):
        candidate, paths, reason = self.evaluate(
            ["releases/Mod/1.0.0.json", "tools/check_amendment.py"]
        )
        self.assertFalse(candidate)
        self.assertEqual(paths, ["releases/Mod/1.0.0.json"])
        self.assertIn("tools/check_amendment.py", reason)

    def test_a_change_to_the_tooling_alone_waits(self):
        candidate, paths, reason = self.evaluate(["tools/amend.py"])
        self.assertFalse(candidate)
        self.assertEqual(paths, [])
        self.assertIn("no release file", reason)

    def test_an_empty_change_waits(self):
        candidate, paths, reason = check_scope.evaluate([])
        self.assertFalse(candidate)
        self.assertEqual(paths, [])
        self.assertIn("no file at all", reason)

    def test_the_reason_does_not_list_every_path_of_a_wide_change(self):
        paths = ["releases/Mod/1.0.0.json"] + [f"tools/f{index}.py" for index in range(9)]
        _, _, reason = self.evaluate(paths)
        self.assertIn("and 4 more", reason)

    def test_the_game_release_list_is_not_an_amendment(self):
        candidate, _, _ = self.evaluate(["game-versions.json"])
        self.assertFalse(candidate)


class Statuses(unittest.TestCase):
    def evaluate(self, path, status):
        return check_scope.evaluate([check_scope.Change(path, status)])

    def test_an_added_release_merges_itself(self):
        # RFC 0033: a release pull request adds exactly one new file.
        candidate, paths, reason = self.evaluate("releases/Mod/1.0.0.json", "added")
        self.assertTrue(candidate)
        self.assertEqual(paths, ["releases/Mod/1.0.0.json"])
        self.assertEqual(reason, "")

    def test_two_added_releases_wait_for_a_steward(self):
        candidate, _, reason = check_scope.evaluate(
            check_scope.changes(["releases/Mod/1.0.0.json", "releases/Mod/1.1.0.json"], "added")
        )
        self.assertFalse(candidate)
        self.assertIn("exactly one", reason)

    def test_an_added_release_next_to_anything_else_waits(self):
        candidate, paths, reason = check_scope.evaluate(
            [
                check_scope.Change("releases/Mod/1.0.0.json", "added"),
                check_scope.Change("tools/amend.py", "modified"),
            ]
        )
        self.assertFalse(candidate)
        self.assertEqual(paths, ["releases/Mod/1.0.0.json"])
        self.assertIn("tools/amend.py", reason)

    def test_a_deleted_release_does_not_merge_itself(self):
        candidate, _, reason = self.evaluate("releases/Mod/1.0.0.json", "removed")
        self.assertFalse(candidate)
        self.assertIn("removed", reason)

    def test_a_renamed_release_does_not_merge_itself(self):
        candidate, _, reason = self.evaluate("releases/Mod/1.0.0.json", "renamed")
        self.assertFalse(candidate)
        self.assertIn("renamed", reason)

    def test_a_status_the_api_may_grow_does_not_merge_itself(self):
        candidate, _, _ = self.evaluate("releases/Mod/1.0.0.json", "copied")
        self.assertFalse(candidate)

    def test_one_added_file_among_amendments_stops_the_whole_set(self):
        candidate, _, reason = check_scope.evaluate(
            [
                check_scope.Change("releases/Mod/1.0.0.json", "modified"),
                check_scope.Change("releases/Mod/1.1.0.json", "added"),
            ]
        )
        self.assertFalse(candidate)
        self.assertIn("1.1.0", reason)
        self.assertIn("next to an amendment", reason)


class Added(unittest.TestCase):
    def test_the_added_release_files_are_read_off_the_statuses(self):
        changes = [
            check_scope.Change("releases/Mod/1.0.0.json", "added"),
            check_scope.Change("releases/Mod/0.9.0.json", "modified"),
            check_scope.Change("tools/amend.py", "added"),
        ]
        self.assertEqual(check_scope.added(changes), ["releases/Mod/1.0.0.json"])

    def test_nothing_added_is_an_empty_list(self):
        self.assertEqual(check_scope.added(check_scope.changes(["releases/Mod/1.0.0.json"])), [])


if __name__ == "__main__":
    unittest.main()
