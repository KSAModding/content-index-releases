#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the snapshot builder.
"""

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from build_snapshot import (
    SnapshotError,
    build,
    precedence,
    serialize,
    sources_from,
)

GAME_VERSIONS = {
    "spec_version": 1,
    "source": "http://ksa-master1.rocketwerkz.com:8082/version",
    "versions": ["2026.7.5.4892", "2026.8.3.5117", "2026.8.5.5168"],
}

LISTING = """\
spec_version = 1
id = "{identifier}"
type = "{content_type}"
name = "{identifier} display name"
authors = ["Maxi"]
abstract = "One sentence for a list view."
license = "MIT"
tags = ["control"]
{extra}
[links]
forums = "https://forums.ahwoo.com/threads/example.1/"

[compatibility]
game_min = "2026.8.3.5117"
"""

PACK = """\
spec_version = 1
id = "{identifier}"
type = "modpack"
name = "{identifier} display name"
authors = ["Maxi"]
abstract = "One sentence for a list view."
license = "CC0-1.0"
version = "{version}"
released_at = "2026-08-05T12:00:00Z"

[links]
forums = "https://forums.ahwoo.com/threads/example.2/"

[compatibility]
game_min = "2026.8.3.5117"

[[mods]]
id = "AdvancedFlightComputer"
version = "0.7.0"
"""


class Index:
    """The two repositories on disk, so a test states only what it is about."""

    def __init__(self, root):
        self.root = root
        self.authored = root / "authored"
        self.releases = root / "releases"
        (self.authored / "listings").mkdir(parents=True)
        (self.authored / "packs").mkdir(parents=True)
        self.releases.mkdir(parents=True)
        self.game_versions = root / "game-versions.json"
        self.write(self.game_versions, json.dumps(GAME_VERSIONS, indent=2) + "\n")
        self.notes = []

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def listing(self, identifier, content_type="mod", extra="", body=None):
        text = body if body is not None else LISTING.format(
            identifier=identifier, content_type=content_type, extra=extra
        )
        self.write(self.authored / "listings" / f"{identifier}.toml", text)

    def pack(self, identifier, version, body=None):
        text = body if body is not None else PACK.format(
            identifier=identifier, version=version
        )
        self.write(self.authored / "packs" / identifier / f"{version}.toml", text)

    def release(self, identifier, version, folder=None, **overrides):
        document = {
            "spec_version": 1,
            "id": identifier,
            "type": "mod",
            "version": version,
            "version_scheme": "semver",
            "release_status": "stable",
            "release_date": "2026-08-05T17:48:55Z",
            "game_min": "2026.8.3.5117",
            "game_min_revision": 5117,
        }
        document.update(overrides)
        name = overrides.get("file_name", version)
        document.pop("file_name", None)
        self.write(
            self.releases / (folder or identifier) / f"{name}.json",
            json.dumps(document, indent=2) + "\n",
        )

    def status(self, *entries):
        lines = []
        for entry in entries:
            lines.append("[[entries]]")
            for key, value in entry.items():
                lines.append(f'{key} = "{value}"')
            lines.append("")
        self.write(self.authored / "index-status.toml", "\n".join(lines) or "entries = []\n")

    def build(self, **kwargs):
        return build(
            self.authored,
            self.releases,
            self.game_versions,
            log=self.notes.append,
            **kwargs,
        )


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.index = Index(Path(self.directory.name))

    def entry(self, document, identifier):
        for item in document["listings"] + document["packs"]:
            if item["id"] == identifier:
                return item
        self.fail(f"no entry for '{identifier}'")


class Precedence(unittest.TestCase):
    def test_the_semver_precedence_table(self):
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
            "1.1.0",
            "2.0.0",
        ]
        keys = [precedence(version, "test") for version in ordered]
        self.assertEqual(keys, sorted(keys))

    def test_build_metadata_takes_no_part_in_precedence(self):
        self.assertEqual(
            precedence("1.0.0+one", "test"), precedence("1.0.0+two", "test")
        )

    def test_a_version_that_does_not_parse_is_an_error(self):
        with self.assertRaises(SnapshotError):
            precedence("0.4", "test")


class Document(Fixture):
    def test_an_empty_index_has_empty_arrays_and_not_absent_fields(self):
        document = self.index.build()
        self.assertEqual(document["listings"], [])
        self.assertEqual(document["packs"], [])

    def test_the_snapshot_carries_its_own_format_version(self):
        self.assertEqual(self.index.build()["snapshot_version"], 1)

    def test_sources_is_absent_without_a_provenance(self):
        self.assertNotIn("sources", self.index.build())

    def test_sources_names_both_halves_when_given(self):
        sources = {
            "authored": {"repository": "KSAModding/content-index", "commit": "9fe1c0f"},
            "generated": {
                "repository": "KSAModding/content-index-releases",
                "commit": "3a77b21",
            },
        }
        self.assertEqual(self.index.build(sources=sources)["sources"], sources)

    def test_the_game_release_list_is_embedded_verbatim(self):
        self.assertEqual(self.index.build()["game_versions"], GAME_VERSIONS)

    def test_a_game_release_list_that_is_missing_is_an_error(self):
        self.index.game_versions.unlink()
        with self.assertRaises(SnapshotError):
            self.index.build()


class Listings(Fixture):
    def test_the_authored_document_appears_verbatim(self):
        self.index.listing("AutoStage")
        entry = self.entry(self.index.build(), "AutoStage")
        self.assertEqual(entry["authored"]["id"], "AutoStage")
        self.assertEqual(entry["authored"]["type"], "mod")
        self.assertEqual(entry["authored"]["license"], "MIT")
        self.assertEqual(
            entry["authored"]["links"]["forums"],
            "https://forums.ahwoo.com/threads/example.1/",
        )
        self.assertEqual(entry["authored"]["compatibility"]["game_min"], "2026.8.3.5117")

    def test_releases_are_newest_first(self):
        self.index.listing("AutoStage")
        for version in ("0.4.3", "0.4.10", "0.4.4-beta.2", "0.4.4"):
            self.index.release("AutoStage", version)
        entry = self.entry(self.index.build(), "AutoStage")
        self.assertEqual(
            [release["version"] for release in entry["releases"]],
            ["0.4.10", "0.4.4", "0.4.4-beta.2", "0.4.3"],
        )

    def test_a_listing_with_no_release_yet_carries_an_empty_array(self):
        self.index.listing("AutoStage")
        self.assertEqual(self.entry(self.index.build(), "AutoStage")["releases"], [])

    def test_listings_are_ascending_by_lowercased_id(self):
        for identifier in ("zeta", "Alpha", "middle"):
            self.index.listing(identifier)
        document = self.index.build()
        self.assertEqual(
            [entry["id"] for entry in document["listings"]], ["Alpha", "middle", "zeta"]
        )

    def test_a_release_folder_whose_casing_differs_is_still_found(self):
        self.index.listing("AutoStage")
        self.index.release("AutoStage", "0.4.3", folder="autostage")
        entry = self.entry(self.index.build(), "AutoStage")
        self.assertEqual([release["version"] for release in entry["releases"]], ["0.4.3"])

    def test_a_release_whose_version_disagrees_with_its_file_name_is_an_error(self):
        self.index.listing("AutoStage")
        self.index.release("AutoStage", "0.4.3", file_name="0.4.4")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_release_whose_id_disagrees_with_the_listing_is_an_error(self):
        self.index.listing("AutoStage")
        self.index.release("SomethingElse", "0.4.3", folder="AutoStage")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_release_files_of_no_listing_are_not_in_the_snapshot(self):
        self.index.release("Unlisted", "1.0.0")
        document = self.index.build()
        self.assertEqual(document["listings"], [])
        self.assertTrue(any("belongs to no listing" in note for note in self.index.notes))

    def test_a_listing_whose_id_disagrees_with_its_file_name_is_an_error(self):
        self.index.write(
            self.index.authored / "listings" / "Wrong.toml",
            LISTING.format(identifier="AutoStage", content_type="mod", extra=""),
        )
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_mod_loader_is_a_listing_like_any_other(self):
        self.index.listing("StarMap", content_type="mod-loader")
        self.assertEqual(
            self.entry(self.index.build(), "StarMap")["authored"]["type"], "mod-loader"
        )


class Packs(Fixture):
    def test_a_pack_version_is_wrapped_in_authored(self):
        self.index.pack("NavigationStarterPack", "1.0.0")
        entry = self.entry(self.index.build(), "NavigationStarterPack")
        self.assertEqual(list(entry["versions"][0]), ["authored"])
        self.assertEqual(entry["versions"][0]["authored"]["version"], "1.0.0")

    def test_pack_versions_are_newest_first(self):
        for version in ("1.0.0", "1.2.0", "1.1.0-rc.1"):
            self.index.pack("NavigationStarterPack", version)
        entry = self.entry(self.index.build(), "NavigationStarterPack")
        self.assertEqual(
            [item["authored"]["version"] for item in entry["versions"]],
            ["1.2.0", "1.1.0-rc.1", "1.0.0"],
        )

    def test_a_pack_has_no_releases_key(self):
        self.index.pack("NavigationStarterPack", "1.0.0")
        self.assertNotIn("releases", self.entry(self.index.build(), "NavigationStarterPack"))

    def test_a_pack_version_that_disagrees_with_its_file_name_is_an_error(self):
        self.index.write(
            self.index.authored / "packs" / "NavigationStarterPack" / "9.9.9.toml",
            PACK.format(identifier="NavigationStarterPack", version="1.0.0"),
        )
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_an_id_held_by_a_listing_and_a_pack_is_an_error(self):
        self.index.listing("Shared")
        self.index.pack("Shared", "1.0.0")
        with self.assertRaises(SnapshotError):
            self.index.build()


class IndexStatus(Fixture):
    def test_a_delisted_listing_is_a_tombstone(self):
        self.index.listing("Gone")
        self.index.release("Gone", "1.0.0")
        self.index.status(
            {"id": "Gone", "state": "delisted", "since": "2026-08-10T00:00:00Z"}
        )
        entry = self.entry(self.index.build(), "Gone")
        self.assertEqual(sorted(entry), ["id", "index_status"])
        self.assertEqual(entry["index_status"]["state"], "delisted")
        self.assertEqual(entry["index_status"]["since"], "2026-08-10T00:00:00Z")

    def test_a_tombstone_stays_in_the_array_its_listing_was_in(self):
        self.index.listing("GoneListing")
        self.index.pack("GonePack", "1.0.0")
        self.index.status(
            {"id": "GoneListing", "state": "delisted"},
            {"id": "GonePack", "state": "delisted"},
        )
        document = self.index.build()
        self.assertEqual([entry["id"] for entry in document["listings"]], ["GoneListing"])
        self.assertEqual([entry["id"] for entry in document["packs"]], ["GonePack"])

    def test_a_disputed_listing_ships_whole(self):
        self.index.listing("Contested")
        self.index.release("Contested", "1.0.0")
        self.index.status({"id": "Contested", "state": "disputed", "reason": "Two claims."})
        entry = self.entry(self.index.build(), "Contested")
        self.assertEqual(entry["authored"]["id"], "Contested")
        self.assertEqual([release["version"] for release in entry["releases"]], ["1.0.0"])
        self.assertEqual(entry["index_status"], {"state": "disputed", "reason": "Two claims."})

    def test_a_retracted_state_lands_on_one_pack_version(self):
        self.index.pack("Pack", "1.0.0")
        self.index.pack("Pack", "1.1.0")
        self.index.status(
            {"id": "Pack", "state": "retracted", "version": "1.0.0", "reason": "Broken pin."}
        )
        entry = self.entry(self.index.build(), "Pack")
        newest, oldest = entry["versions"]
        self.assertEqual(newest["authored"]["version"], "1.1.0")
        self.assertNotIn("index_status", newest)
        self.assertEqual(oldest["authored"]["version"], "1.0.0")
        self.assertEqual(oldest["index_status"]["state"], "retracted")

    def test_the_version_key_of_an_entry_does_not_reach_the_snapshot(self):
        self.index.pack("Pack", "1.0.0")
        self.index.status({"id": "Pack", "state": "retracted", "version": "1.0.0"})
        status = self.entry(self.index.build(), "Pack")["versions"][0]["index_status"]
        self.assertEqual(sorted(status), ["state"])

    def test_a_state_for_an_unknown_id_is_an_error(self):
        self.index.listing("Known")
        self.index.status({"id": "Typo", "state": "delisted"})
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_an_unknown_state_is_an_error(self):
        self.index.listing("Known")
        self.index.status({"id": "Known", "state": "hidden"})
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_version_on_a_whole_entry_state_is_an_error(self):
        self.index.listing("Known")
        self.index.status({"id": "Known", "state": "disputed", "version": "1.0.0"})
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_retracted_state_without_a_version_is_an_error(self):
        self.index.pack("Pack", "1.0.0")
        self.index.status({"id": "Pack", "state": "retracted"})
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_retracted_version_that_does_not_exist_is_an_error(self):
        self.index.pack("Pack", "1.0.0")
        self.index.status({"id": "Pack", "state": "retracted", "version": "2.0.0"})
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_retracted_state_on_a_listing_is_an_error(self):
        self.index.listing("NotAPack")
        self.index.status({"id": "NotAPack", "state": "retracted", "version": "1.0.0"})
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_two_states_for_one_entry_are_an_error(self):
        self.index.listing("Known")
        self.index.status(
            {"id": "Known", "state": "disputed"}, {"id": "Known", "state": "delisted"}
        )
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_state_matches_its_id_case_insensitively(self):
        self.index.listing("MixedCase")
        self.index.status({"id": "mixedcase", "state": "disputed"})
        self.assertIn("index_status", self.entry(self.index.build(), "MixedCase"))

    def test_retracting_a_version_of_a_delisted_pack_is_only_a_note(self):
        self.index.pack("Pack", "1.0.0")
        self.index.status(
            {"id": "Pack", "state": "delisted"},
            {"id": "Pack", "state": "retracted", "version": "1.0.0"},
        )
        entry = self.entry(self.index.build(), "Pack")
        self.assertEqual(sorted(entry), ["id", "index_status"])
        self.assertTrue(any("changes nothing" in note for note in self.index.notes))


class Determinism(Fixture):
    def build_twice(self):
        first = serialize(self.index.build())
        second = serialize(self.index.build())
        return first, second

    def test_the_same_input_produces_the_same_bytes(self):
        self.index.listing("AutoStage")
        self.index.release("AutoStage", "0.4.3")
        self.index.pack("Pack", "1.0.0")
        first, second = self.build_twice()
        self.assertEqual(first, second)

    def test_the_bytes_end_in_exactly_one_newline(self):
        rendered = serialize(self.index.build())
        self.assertTrue(rendered.endswith("}\n"))

    def test_the_document_indents_by_two_spaces(self):
        rendered = serialize(self.index.build())
        self.assertIn('\n  "snapshot_version": 1', rendered)

    def test_two_releases_differing_only_in_build_metadata_stay_ordered(self):
        self.index.listing("AutoStage")
        self.index.release("AutoStage", "1.0.0+one")
        self.index.release("AutoStage", "1.0.0+two")
        entry = self.entry(self.index.build(), "AutoStage")
        self.assertEqual(
            [release["version"] for release in entry["releases"]],
            ["1.0.0+two", "1.0.0+one"],
        )


class Encoding(Fixture):
    def test_a_bare_toml_datetime_becomes_an_iso_string(self):
        self.index.pack(
            "Pack",
            "1.0.0",
            body=PACK.format(identifier="Pack", version="1.0.0").replace(
                'released_at = "2026-08-05T12:00:00Z"',
                "released_at = 2026-08-05T12:00:00Z",
            ),
        )
        released = self.entry(self.index.build(), "Pack")["versions"][0]["authored"][
            "released_at"
        ]
        self.assertEqual(released, "2026-08-05T12:00:00Z")
        json.dumps(released)  # has to stay JSON, so not a datetime

    def test_a_value_json_cannot_hold_is_an_error(self):
        self.index.listing(
            "Odd",
            body=LISTING.format(identifier="Odd", content_type="mod", extra="")
            + "\nweight = nan\n",
        )
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_listing_that_is_not_valid_toml_is_an_error(self):
        self.index.write(self.index.authored / "listings" / "Broken.toml", "id = \n")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_release_that_is_not_valid_json_is_an_error(self):
        self.index.listing("AutoStage")
        self.index.write(self.index.releases / "AutoStage" / "0.4.3.json", "{oops")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_non_ascii_text_is_not_escaped(self):
        # chr() keeps this file ASCII while the data it feeds in is not.
        abstract = "F" + chr(0xFC) + "r die " + chr(0xDC) + "bersicht."
        self.index.listing(
            "Umlaut",
            body=LISTING.format(identifier="Umlaut", content_type="mod", extra="").replace(
                "One sentence for a list view.", abstract
            ),
        )
        rendered = serialize(self.index.build())
        self.assertIn(abstract, rendered)
        self.assertNotIn("\\u00fc", rendered)


class Inputs(Fixture):
    """A half that does not match the layout must not publish an empty index."""

    def test_a_missing_authored_checkout_is_an_error(self):
        for child in sorted(self.index.authored.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        self.index.authored.rmdir()
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_missing_listings_folder_is_an_error(self):
        (self.index.authored / "listings").rmdir()
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_missing_packs_folder_is_an_error(self):
        (self.index.authored / "packs").rmdir()
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_missing_releases_folder_is_an_error(self):
        self.index.releases.rmdir()
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_an_empty_but_present_layout_stays_legal(self):
        document = self.index.build()
        self.assertEqual(document["listings"], [])
        self.assertEqual(document["packs"], [])

    def test_two_release_folders_differing_only_in_case_are_an_error(self):
        probe = self.index.releases / "CaseProbe"
        probe.mkdir()
        insensitive = (self.index.releases / "caseprobe").exists()
        probe.rmdir()
        if insensitive:
            self.skipTest("this filesystem cannot hold two folders differing only in case")

        self.index.listing("AutoStage")
        self.index.release("AutoStage", "0.4.3", folder="AutoStage")
        self.index.release("AutoStage", "0.4.4", folder="autostage")
        with self.assertRaises(SnapshotError):
            self.index.build()


class Provenance(unittest.TestCase):
    @staticmethod
    def arguments(**values):
        defaults = {
            "authored_repo": None,
            "authored_commit": None,
            "generated_repo": None,
            "generated_commit": None,
        }
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def test_naming_none_of_the_four_omits_the_block(self):
        self.assertIsNone(sources_from(self.arguments()))

    def test_naming_all_four_builds_the_block(self):
        block = sources_from(
            self.arguments(
                authored_repo="KSAModding/content-index",
                authored_commit="9fe1c0f",
                generated_repo="KSAModding/content-index-releases",
                generated_commit="3a77b21",
            )
        )
        self.assertEqual(block["authored"]["commit"], "9fe1c0f")
        self.assertEqual(block["generated"]["repository"], "KSAModding/content-index-releases")

    def test_a_commit_that_came_back_empty_is_an_error(self):
        # A failed command substitution: no shell fails on one in argument position.
        with self.assertRaises(SnapshotError):
            sources_from(
                self.arguments(
                    authored_repo="KSAModding/content-index",
                    authored_commit="",
                    generated_repo="KSAModding/content-index-releases",
                    generated_commit="3a77b21",
                )
            )


class Malformed(Fixture):
    def test_a_json_nan_literal_is_an_error(self):
        self.index.listing("AutoStage")
        self.index.write(
            self.index.releases / "AutoStage" / "0.4.3.json",
            '{"id": "AutoStage", "version": "0.4.3", "install_size": NaN}\n',
        )
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_release_that_is_not_an_object_is_an_error(self):
        self.index.listing("AutoStage")
        self.index.write(self.index.releases / "AutoStage" / "0.4.3.json", "[]\n")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_a_game_release_list_that_is_not_an_object_is_an_error(self):
        self.index.write(self.index.game_versions, "[]\n")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_entries_that_are_not_an_array_is_an_error(self):
        self.index.write(self.index.authored / "index-status.toml", "entries = 0\n")
        with self.assertRaises(SnapshotError):
            self.index.build()

    def test_index_status_that_is_absent_means_no_states(self):
        self.index.listing("AutoStage")
        self.assertNotIn("index_status", self.entry(self.index.build(), "AutoStage"))


if __name__ == "__main__":
    unittest.main()
