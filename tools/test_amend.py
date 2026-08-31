#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the amendment tool.

The tool checks its own output, so a case that passes here is one the check in front of the pull request also accepts.
"""

import argparse
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import amend
from amend import AmendError

GAME_VERSIONS = [
    "2026.7.9.5018",
    "2026.7.10.5056",
    "2026.8.3.5117",
    "2026.8.5.5168",
    "2026.8.19.5261",
    "2026.8.22.5348",
]

RELEASE = {
    "spec_version": 1,
    "id": "AdvancedFlightComputer",
    "type": "mod",
    "version": "0.7.2",
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
    "install": {"root": "AdvancedFlightComputer", "derived": True},
    "loader": {"id": "StarMap", "min": "0.4.5", "source": "authored"},
    "dependencies": [{"id": "KittenExtensions", "kind": "optional", "source": "derived"}],
    "changelog": "https://example.invalid/tag",
    "listing": {"name": "Advanced Flight Computer", "authors": ["Maxi"]},
}


def release(version, listing="AdvancedFlightComputer", **changes):
    document = copy.deepcopy(RELEASE)
    document["id"] = listing
    document["version"] = version
    document.update(changes)
    return document


class Tree(unittest.TestCase):
    """A releases tree on disk, which is what the tool reads and writes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "releases"

    def write(self, listing, *versions):
        folder = self.root / listing
        folder.mkdir(parents=True, exist_ok=True)
        for version in versions:
            (folder / f"{version}.json").write_text(
                json.dumps(release(version, listing), indent=2) + "\n", encoding="utf-8"
            )

    def read(self, listing, version):
        return json.loads((self.root / listing / f"{version}.json").read_text(encoding="utf-8"))

    def run_tool(self, *argv):
        return amend.main([*argv, "--releases", str(self.root),
                           "--game-versions", str(self.game_versions())])

    def game_versions(self):
        path = Path(self.directory.name) / "game-versions.json"
        path.write_text(
            json.dumps({"spec_version": 1, "versions": GAME_VERSIONS}), encoding="utf-8"
        )
        return path


class Selection(Tree):
    def test_up_to_takes_that_release_and_everything_older(self):
        self.write("Mod", "0.7.0", "0.7.2", "0.7.3")
        paths = amend.select(amend.stamped(self.root, "Mod"), up_to="0.7.2")
        self.assertEqual(sorted(path.stem for path in paths), ["0.7.0", "0.7.2"])

    def test_up_to_orders_by_precedence_and_not_by_string(self):
        self.write("Mod", "0.9.0", "0.10.0")
        paths = amend.select(amend.stamped(self.root, "Mod"), up_to="0.9.0")
        self.assertEqual([path.stem for path in paths], ["0.9.0"])

    def test_a_pre_release_sorts_below_its_own_release(self):
        self.write("Mod", "1.0.0-rc.1", "1.0.0")
        paths = amend.select(amend.stamped(self.root, "Mod"), up_to="1.0.0-rc.1")
        self.assertEqual([path.stem for path in paths], ["1.0.0-rc.1"])

    def test_named_versions_are_taken_as_given(self):
        self.write("Mod", "0.7.0", "0.7.2", "0.7.3")
        paths = amend.select(amend.stamped(self.root, "Mod"), versions=["0.7.3"])
        self.assertEqual([path.stem for path in paths], ["0.7.3"])

    def test_a_version_that_is_not_stamped_is_named(self):
        self.write("Mod", "0.7.0")
        with self.assertRaises(AmendError) as raised:
            amend.select(amend.stamped(self.root, "Mod"), versions=["9.9.9"])
        self.assertIn("9.9.9", str(raised.exception))

    def test_all_takes_everything(self):
        self.write("Mod", "0.7.0", "0.7.2", "0.7.3")
        paths = amend.select(amend.stamped(self.root, "Mod"), everything=True)
        self.assertEqual(len(paths), 3)

    def test_a_listing_with_no_folder_is_named(self):
        with self.assertRaises(AmendError):
            amend.stamped(self.root, "Missing")

    def test_up_to_below_everything_stamped_is_named(self):
        self.write("Mod", "1.0.0")
        with self.assertRaises(AmendError):
            amend.select(amend.stamped(self.root, "Mod"), up_to="0.1.0")


class GameMax(Tree):
    def test_it_bounds_every_selected_release(self):
        self.write("Mod", "0.7.0", "0.7.2", "0.7.3")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--up-to", "0.7.2",
                          "--game-max", "2026.8.19.5261"),
            0,
        )
        for version in ("0.7.0", "0.7.2"):
            self.assertEqual(self.read("Mod", version)["game_max_revision"], 5261)
        self.assertNotIn("game_max", self.read("Mod", "0.7.3"))

    def test_the_bound_lands_where_a_fresh_stamp_would_put_it(self):
        self.write("Mod", "0.7.2")
        self.run_tool("--listing", "Mod", "--up-to", "0.7.2", "--game-max", "2026.8.19.5261")
        keys = list(self.read("Mod", "0.7.2"))
        self.assertEqual(
            keys[keys.index("game_min_revision") + 1: keys.index("game_min_revision") + 3],
            ["game_max", "game_max_revision"],
        )

    def test_a_month_resolves_to_its_last_revision(self):
        # July 2026 is over whenever this runs, so its last revision is fixed.
        self.write("Mod", "0.7.2")
        older = self.root / "Mod" / "0.7.2.json"
        document = json.loads(older.read_text(encoding="utf-8"))
        document["game_min"], document["game_min_revision"] = "2026.7.9.5018", 5018
        older.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        self.assertEqual(self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.7"), 0)
        self.assertEqual(self.read("Mod", "0.7.2")["game_max_revision"], 5056)

    def test_running_it_twice_writes_nothing_the_second_time(self):
        self.write("Mod", "0.7.2")
        self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.8.19.5261")
        before = (self.root / "Mod" / "0.7.2.json").read_text(encoding="utf-8")
        self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.8.19.5261")
        self.assertEqual((self.root / "Mod" / "0.7.2.json").read_text(encoding="utf-8"), before)

    def test_a_bound_below_the_stamped_game_min_is_refused(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.8.3.5117"), 1
        )
        self.assertNotIn("game_max", self.read("Mod", "0.7.2"))

    def test_a_build_the_game_release_list_does_not_know_still_resolves(self):
        # A full version carries its own revision, so it needs no list (RFC 0017).
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.9.1.5400"), 0
        )
        self.assertEqual(self.read("Mod", "0.7.2")["game_max_revision"], 5400)

    def test_a_leading_v_is_stripped_before_it_is_published(self):
        # A tag carries one and a game version does not (RFC 0017).
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--game-max", "v2026.8.22.5348"), 0
        )
        self.assertEqual(self.read("Mod", "0.7.2")["game_max"], "2026.8.22.5348")

    def test_an_empty_bound_is_refused(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(self.run_tool("--listing", "Mod", "--all", "--game-max", "  "), 1)

    def test_dry_run_writes_nothing(self):
        self.write("Mod", "0.7.2")
        self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.8.19.5261", "--dry-run")
        self.assertNotIn("game_max", self.read("Mod", "0.7.2"))


class GameMin(Tree):
    def test_it_can_be_raised(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--game-min", "2026.8.22.5348"), 0
        )
        self.assertEqual(self.read("Mod", "0.7.2")["game_min_revision"], 5348)

    def test_it_cannot_be_lowered(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--game-min", "2026.8.3.5117"), 1
        )
        self.assertEqual(self.read("Mod", "0.7.2")["game_min_revision"], 5261)


class Yank(Tree):
    def test_a_release_can_be_yanked_with_a_reason(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--yank", "--reason", "It corrupts saves."),
            0,
        )
        document = self.read("Mod", "0.7.2")
        self.assertIs(document["yanked"], True)
        self.assertEqual(document["yanked_reason"], "It corrupts saves.")

    def test_the_yank_goes_last_where_RFC_0031_lists_it(self):
        self.write("Mod", "0.7.2")
        self.run_tool("--listing", "Mod", "--all", "--yank")
        self.assertEqual(list(self.read("Mod", "0.7.2"))[-1], "yanked")

    def test_a_reason_says_nothing_without_the_yank(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(self.run_tool("--listing", "Mod", "--all", "--reason", "why"), 1)


class Bounds(Tree):
    def test_a_loader_min_can_be_raised(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(self.run_tool("--listing", "Mod", "--all", "--loader-min", "0.4.6"), 0)
        self.assertEqual(self.read("Mod", "0.7.2")["loader"]["min"], "0.4.6")

    def test_a_loader_min_cannot_be_lowered(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(self.run_tool("--listing", "Mod", "--all", "--loader-min", "0.4.0"), 1)

    def test_a_loader_bound_keeps_the_stamper_key_order(self):
        self.write("Mod", "0.7.2")
        self.run_tool("--listing", "Mod", "--all", "--loader-max", "0.4.9")
        self.assertEqual(
            list(self.read("Mod", "0.7.2")["loader"]), ["id", "min", "max", "source"]
        )

    def test_a_dependency_bound_makes_a_derived_entry_authored(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all",
                          "--dependency-min", "KittenExtensions=0.4.0"),
            0,
        )
        entry = self.read("Mod", "0.7.2")["dependencies"][0]
        self.assertEqual(entry["min"], "0.4.0")
        self.assertEqual(entry["source"], "authored")
        self.assertEqual(list(entry), ["id", "kind", "min", "max", "source"][:3] + ["source"])

    def test_a_dependency_the_release_does_not_have_is_named(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--dependency-min", "Nothing=1.0.0"), 1
        )

    def test_an_entry_that_was_missing_can_be_added(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all",
                          "--add-dependency", "BadMod:conflict",
                          "--dependency-max", "BadMod=1.2.0"),
            0,
        )
        added = self.read("Mod", "0.7.2")["dependencies"][-1]
        self.assertEqual(added, {"id": "BadMod", "kind": "conflict", "max": "1.2.0",
                                 "source": "authored"})

    def test_a_kind_the_format_does_not_have_is_refused(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--add-dependency", "BadMod:wants"), 1
        )

    def test_a_bound_that_is_not_ID_EQUALS_VERSION_is_named(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--dependency-min", "KittenExtensions"), 1
        )

    def test_a_leading_v_on_a_bound_is_normalized(self):
        self.write("Mod", "0.7.2")
        self.run_tool("--listing", "Mod", "--all", "--dependency-min", "KittenExtensions=v0.4.0")
        self.assertEqual(self.read("Mod", "0.7.2")["dependencies"][0]["min"], "0.4.0")

    def test_a_loader_bound_on_a_release_with_no_loader_is_refused(self):
        folder = self.root / "Asset"
        folder.mkdir(parents=True)
        document = release("1.0.0", "Asset")
        del document["loader"]
        (folder / "1.0.0.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.run_tool("--listing", "Asset", "--all", "--loader-min", "0.4.6"), 1)


class Refusals(Tree):
    def test_naming_no_change_is_refused(self):
        self.write("Mod", "0.7.2")
        self.assertEqual(self.run_tool("--listing", "Mod", "--all"), 1)

    def test_a_month_that_is_not_over_has_no_last_revision(self):
        # The clock is injected rather than read, so this case does not change its meaning as time passes.
        arguments = argparse.Namespace(
            game_min=None, game_max="2026.8", yank=False, reason=None,
            loader_min=None, loader_max=None, dependency_min=[], dependency_max=[],
            add_dependency=[],
        )
        with self.assertRaises(AmendError) as raised:
            amend.build_amendment(
                arguments, GAME_VERSIONS, datetime(2026, 8, 29, tzinfo=timezone.utc)
            )
        self.assertIn("not over", str(raised.exception))

    def test_the_same_month_resolves_once_it_is_over(self):
        arguments = argparse.Namespace(
            game_min=None, game_max="2026.8", yank=False, reason=None,
            loader_min=None, loader_max=None, dependency_min=[], dependency_max=[],
            add_dependency=[],
        )
        amendment = amend.build_amendment(
            arguments, GAME_VERSIONS, datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(amendment["game_max"], ("2026.8.22.5348", 5348))

    def test_a_release_file_that_does_not_parse_is_named(self):
        folder = self.root / "Mod"
        folder.mkdir(parents=True)
        (folder / "0.7.2.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(
            self.run_tool("--listing", "Mod", "--all", "--game-max", "2026.8.19.5261"), 1
        )

    def test_a_file_name_that_is_not_a_version_is_named(self):
        folder = self.root / "Mod"
        folder.mkdir(parents=True)
        (folder / "latest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(AmendError):
            amend.stamped(self.root, "Mod")


class Reminders(unittest.TestCase):
    def test_a_dependency_bound_points_at_the_authored_document(self):
        lines = amend.reminders({"dependency_bounds": {"X": ("1.0.0", None)}})
        self.assertTrue(any("content-index" in line for line in lines))

    def test_a_game_max_warns_against_capping_the_listing(self):
        lines = amend.reminders({"game_max": ("2026.8.19.5261", 5261)})
        self.assertTrue(any("authored game_max" in line for line in lines))

    def test_a_yank_needs_no_reminder(self):
        self.assertEqual(amend.reminders({"yank": True}), [])


if __name__ == "__main__":
    unittest.main()
