#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the stamper. No token, no network: `python3 -m unittest discover tools`.

Every case here is one sentence from RFC 0031, RFC 0035 or RFC 0017 that the
stamper has to keep. The archives are built in memory, so the fixtures are the
tests.

This is the authored half of a stamp: the bounds, the loader, the dependency
merge, the listing block. `tools/verify_examples.py` covers the archive half.
"""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from check_release import authored_root
from stamp_release import (
    CHANGELOG_TEXT_LIMIT,
    Archive,
    StampError,
    changelog_text,
    check_folder,
    derived_dependencies,
    folder_holders,
    merge_dependencies,
    normalize_version,
    relative_path,
    release_status,
    resolve_bound,
    save_name_problem,
    serialize,
    stamp,
    valid_id,
    valid_save_name,
)

GAME_VERSIONS = [
    "2026.7.5.4892",
    "2026.7.6.4939",
    "2026.7.9.5018",
    "2026.8.3.5117",
    "2026.8.5.5168",
]

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

MOD_TOML = """\
name = "AutoStage"
patches = [ "Patches/BurnControlPatch.xml" ]

[StarMap]
EntryAssembly = "AutoStage"

[[StarMap.ModDependencies]]
ModId = "KittenExtensions"
Optional = false

[[StarMap.ModDependencies]]
ModId = "MeasureTools"
Optional = true
"""

LISTING = {
    "spec_version": 1,
    "id": "AutoStage",
    "type": "mod",
    "name": "AutoStage",
    "authors": ["Maxi"],
    "abstract": "Automatic staging for Kitten Space Agency.",
    "license": "MIT",
    "tags": ["control"],
    "status": "deprecated",
    "superseded_by": "AutoStageNG",
    "releases": {"github": "Maximilian-Nesslauer/KSA-AutoStage"},
    "links": {"forums": "https://forums.ahwoo.com/threads/autostage.891/"},
    "compatibility": {"game_min": "2026.8.3.5117"},
    "loader": {"id": "StarMap", "min": "0.4.5"},
}

RELEASE = {
    "tag": "v0.4.3",
    "release_date": "2026-08-05T17:48:57Z",
    "url": "https://github.com/Maximilian-Nesslauer/KSA-AutoStage/releases/download/v0.4.3/AutoStage.zip",
    "content_type": "application/zip",
    "prerelease": False,
    "changelog": "https://github.com/Maximilian-Nesslauer/KSA-AutoStage/releases/tag/v0.4.3",
}


def archive(files):
    """A zip archive of {path: text}, as bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for path, content in files.items():
            handle.writestr(path, content)
    return buffer.getvalue()


def mod_archive(identifier="AutoStage", manifest=MOD_TOML):
    return archive(
        {
            f"{identifier}/{identifier}.dll": "x" * 100,
            f"{identifier}/mod.toml": manifest,
        }
    )


class Versions(unittest.TestCase):
    def test_a_leading_v_is_stripped(self):
        self.assertEqual(normalize_version("v1.2.3"), "1.2.3")
        self.assertEqual(normalize_version("1.2.3-rc.1+build.5"), "1.2.3-rc.1+build.5")

    def test_the_table_of_rfc_0072(self):
        for tag, stored in (
            ("2.3.4", "2.3.4"),
            ("v2.3.4", "2.3.4"),
            ("0.5", "0.5.0"),
            ("v1", "1.0.0"),
            ("1.2-rc.1", "1.2.0-rc.1"),
            ("1.2+build.7", "1.2.0+build.7"),
            ("2026.9", "2026.9.0"),
        ):
            with self.subTest(tag=tag):
                self.assertEqual(normalize_version(tag), stored)

        for tag in ("0.5.0.1", "01.2.3", "latest"):
            with self.subTest(tag=tag), self.assertRaisesRegex(
                StampError, "one to three numeric components without leading zeros"
            ):
                normalize_version(tag)

    def test_a_version_that_does_not_parse_is_rejected(self):
        for tag in ("rc", "2026.8.3.5117", "", None, "v1.02.3", "1.", ".5", "V1.2", "1.2-01"):
            with self.subTest(tag=tag), self.assertRaises(StampError):
                normalize_version(tag)

    def test_a_nightly_does_not_look_like_a_release(self):
        self.assertEqual(release_status("1.0.0", False), "stable")
        self.assertEqual(release_status("1.0.0", True), "testing")
        self.assertEqual(release_status("1.0.0-rc.1", False), "testing")
        self.assertEqual(release_status("1.0.0-nightly.20260810", False), "dev")


class Bounds(unittest.TestCase):
    def test_a_full_version_carries_its_own_revision(self):
        self.assertEqual(
            resolve_bound("2026.8.3.5117", "game_min", [], NOW), ("2026.8.3.5117", 5117)
        )

    def test_a_month_resolves_to_its_first_and_last_revision(self):
        self.assertEqual(
            resolve_bound("2026.7", "game_min", GAME_VERSIONS, NOW), ("2026.7.5.4892", 4892)
        )
        self.assertEqual(
            resolve_bound("2026.7", "game_max", GAME_VERSIONS, NOW), ("2026.7.9.5018", 5018)
        )

    def test_a_month_that_is_not_over_stamps_an_open_upper_bound(self):
        self.assertEqual(resolve_bound("2026.8", "game_max", GAME_VERSIONS, NOW), (None, None))

    def test_a_month_with_no_build_cannot_resolve(self):
        with self.assertRaises(StampError):
            resolve_bound("2026.9", "game_min", GAME_VERSIONS, NOW)

    def test_something_that_is_neither_is_an_error(self):
        with self.assertRaises(StampError):
            resolve_bound("recent", "game_min", GAME_VERSIONS, NOW)


class Dependencies(unittest.TestCase):
    def derived(self):
        return derived_dependencies({"StarMap": {"ModDependencies": [
            {"ModId": "KittenExtensions", "Optional": False},
            {"ModId": "MeasureTools", "Optional": True},
        ]}})

    def test_optional_defaults_to_false(self):
        derived = derived_dependencies(
            {"StarMap": {"ModDependencies": [{"ModId": "KittenExtensions"}]}}
        )
        self.assertEqual(
            derived, [{"id": "KittenExtensions", "kind": "required", "source": "derived"}]
        )

    def test_an_archive_without_a_mod_toml_contributes_nothing(self):
        self.assertEqual(derived_dependencies(None), [])

    def test_an_authored_entry_replaces_the_derived_entry_of_the_same_id(self):
        merged = merge_dependencies(
            self.derived(),
            [{"id": "KittenExtensions", "kind": "required", "min": "0.4.0"}],
        )
        self.assertEqual(
            merged,
            [
                {"id": "MeasureTools", "kind": "optional", "source": "derived"},
                {
                    "id": "KittenExtensions",
                    "kind": "required",
                    "min": "0.4.0",
                    "source": "authored",
                },
            ],
        )

    def test_a_derived_entry_cannot_be_suppressed(self):
        merged = merge_dependencies(self.derived(), [{"id": "Something", "kind": "suggests"}])
        self.assertEqual([entry["id"] for entry in merged],
                         ["KittenExtensions", "MeasureTools", "Something"])

    def test_any_of_replaces_the_derived_entry_of_every_member_it_names(self):
        merged = merge_dependencies(
            self.derived(),
            [{"kind": "recommends", "any_of": [{"id": "MeasureTools", "min": "1.0.0"},
                                               {"id": "DeltaVMap"}]}],
        )
        self.assertEqual(
            merged,
            [
                {"id": "KittenExtensions", "kind": "required", "source": "derived"},
                {
                    "any_of": [{"id": "MeasureTools", "min": "1.0.0"}, {"id": "DeltaVMap"}],
                    "kind": "recommends",
                    "source": "authored",
                },
            ],
        )

    def test_any_of_over_a_hard_derived_dependency_is_an_error(self):
        with self.assertRaises(StampError):
            merge_dependencies(
                self.derived(),
                [{"kind": "required", "any_of": [{"id": "KittenExtensions"}]}],
            )

    def test_an_unknown_kind_is_an_error(self):
        with self.assertRaises(StampError):
            merge_dependencies([], [{"id": "X", "kind": "needs"}])


class Stamp(unittest.TestCase):
    def stamp(self, listing=None, release=None, data=None, **kwargs):
        return stamp(
            listing or LISTING,
            release or RELEASE,
            data if data is not None else mod_archive(),
            GAME_VERSIONS,
            now=NOW,
            **kwargs,
        )

    def test_the_worked_example(self):
        document = self.stamp()
        self.assertEqual(document["version"], "0.4.3")
        self.assertEqual(document["version_scheme"], "semver")
        self.assertEqual(document["release_status"], "stable")
        self.assertEqual(document["game_min_revision"], 5117)
        self.assertEqual(document["download"]["size"], len(mod_archive()))
        self.assertEqual(len(document["download"]["sha256"]), 64)
        self.assertEqual(document["download"]["sha256"], document["download"]["sha256"].upper())
        self.assertEqual(document["install"], {"root": "AutoStage", "derived": True})
        self.assertEqual(document["install_size"], 100 + len(MOD_TOML))
        self.assertEqual(
            document["loader"], {"id": "StarMap", "min": "0.4.5", "source": "authored"}
        )
        self.assertEqual(
            [entry["id"] for entry in document["dependencies"]],
            ["KittenExtensions", "MeasureTools"],
        )

    def test_a_short_tag_stamps_its_filled_version(self):
        document = self.stamp(release={**RELEASE, "tag": "v1.2-rc.1"})
        self.assertEqual(document["version"], "1.2.0-rc.1")
        self.assertEqual(document["release_status"], "testing")

    def test_short_authored_bounds_are_filled_by_the_same_rule(self):
        listing = {
            **LISTING,
            "loader": {"id": "StarMap", "min": "0.4", "max": "v1"},
            "dependencies": [{"id": "MeasureTools", "kind": "optional", "min": "1.2"}],
        }
        document = self.stamp(listing=listing)
        self.assertEqual((document["loader"]["min"], document["loader"]["max"]), ("0.4.0", "1.0.0"))
        entry = next(item for item in document["dependencies"] if item["id"] == "MeasureTools")
        self.assertEqual(entry["min"], "1.2.0")

    def test_an_archive_in_a_file_stamps_like_its_bytes(self):
        # A download streams into a file and brings its digest and size along,
        # and a caller with bytes in memory gets the same release file.
        data = mod_archive()
        with tempfile.TemporaryFile() as file:
            file.write(data)
            archive = Archive(file, hashlib.sha256(data).hexdigest(), len(data))
            self.assertEqual(self.stamp(data=archive), self.stamp(data=data))

    def test_the_digest_and_size_of_an_archive_are_taken_as_given(self):
        # The stamper does not read the file again for them: the download
        # measured the bytes as they arrived.
        data = mod_archive()
        archive = Archive(io.BytesIO(data), "ab" * 32, 12345)
        download = self.stamp(data=archive)["download"]
        self.assertEqual(download["sha256"], "AB" * 32)
        self.assertEqual(download["size"], 12345)

    def test_the_listing_block_freezes_the_descriptive_facts_only(self):
        listing = self.stamp()["listing"]
        self.assertEqual(listing["name"], "AutoStage")
        self.assertEqual(listing["links"]["forums"], LISTING["links"]["forums"])
        # Deprecation has to reach every release the moment it is declared, so
        # a client reads it live from the authored file and never from here.
        self.assertNotIn("status", listing)
        self.assertNotIn("superseded_by", listing)

    def test_mirrors_are_only_there_when_the_caller_verified_one(self):
        self.assertNotIn("mirrors", self.stamp()["download"])
        document = self.stamp(mirrors=["https://spacedock.info/x"])
        self.assertEqual(document["download"]["mirrors"], ["https://spacedock.info/x"])

    def test_an_octet_stream_asset_is_still_a_zip(self):
        release = dict(RELEASE, content_type="application/octet-stream")
        self.assertEqual(self.stamp(release=release)["download"]["content_type"], "application/zip")

    def test_an_authored_month_max_that_is_over_lands_in_the_file(self):
        listing = dict(LISTING, compatibility={"game_min": "2026.7", "game_max": "2026.7"})
        document = self.stamp(listing=listing)
        self.assertEqual(document["game_min"], "2026.7.5.4892")
        self.assertEqual(document["game_max_revision"], 5018)

    def test_an_authored_month_max_that_is_not_over_stamps_open(self):
        listing = dict(LISTING, compatibility={"game_min": "2026.8.3.5117", "game_max": "2026.8"})
        self.assertNotIn("game_max", self.stamp(listing=listing))

    def test_an_empty_compatibility_range_is_an_error(self):
        # A max that resolves below the min stamps a range no game version can
        # ever satisfy, so it is the author's mistake to fix, not a file to ship.
        listing = dict(
            LISTING, compatibility={"game_min": "2026.8.3.5117", "game_max": "2026.7"}
        )
        with self.assertRaises(StampError):
            self.stamp(listing=listing)

    def test_the_os_list_is_carried_when_authored(self):
        listing = dict(
            LISTING, compatibility={"game_min": "2026.8.3.5117", "os": ["windows"]}
        )
        self.assertEqual(self.stamp(listing=listing)["os"], ["windows"])

    def test_a_folder_name_that_is_not_the_id_is_an_error(self):
        # The folder name is the identity the game will see (Mod.MakeUsing).
        with self.assertRaises(StampError):
            self.stamp(data=mod_archive("autostage"))
        with self.assertRaises(StampError):
            self.stamp(data=mod_archive("Whatever"))

    def test_an_unusual_layout_needs_an_authored_root(self):
        data = archive({"build/AutoStage/mod.toml": MOD_TOML, "README.md": "x" * 9})
        with self.assertRaises(StampError):
            self.stamp(data=data)

        listing = dict(LISTING, install={"root": "build/AutoStage"})
        document = self.stamp(listing=listing, data=data)
        self.assertEqual(document["install"], {"root": "build/AutoStage", "derived": False})
        # Only what gets installed counts, so the README beside it does not.
        self.assertEqual(document["install_size"], len(MOD_TOML))

    def test_an_authored_root_that_is_not_in_the_archive_is_an_error(self):
        with self.assertRaises(StampError):
            self.stamp(listing=dict(LISTING, install={"root": "nowhere"}))

    def test_an_asset_only_mod_has_no_mod_toml_and_no_dependencies(self):
        data = archive({"AutoStage/Parts/thing.json": "{}"})
        document = self.stamp(data=data)
        self.assertEqual(document["install"], {"root": "AutoStage", "derived": True})
        self.assertEqual(document["dependencies"], [])

    def test_a_mod_toml_with_a_byte_order_mark_is_read(self):
        document = self.stamp(data=mod_archive(manifest="﻿" + MOD_TOML))
        self.assertEqual(
            [entry["id"] for entry in document["dependencies"]],
            ["KittenExtensions", "MeasureTools"],
        )

    def test_a_loader_installs_from_the_archive_root(self):
        listing = {
            "spec_version": 1,
            "id": "StarMap",
            "type": "mod-loader",
            "name": "StarMap",
            "authors": ["KlaasWhite"],
            "abstract": "Mod loader that runs code mods.",
            "license": "MIT",
            "links": {"forums": "https://forums.ahwoo.com/threads/starmap-mod-loader.384/"},
            "compatibility": {"game_min": "2026.8.3.5117"},
        }
        data = archive({"StarMap.exe": "x" * 10, "StarMap.dll": "y" * 20})
        document = stamp(
            listing, dict(RELEASE, tag="0.4.6"), data, GAME_VERSIONS, now=NOW
        )
        # Nothing to say about the install, so the object stays out (RFC 0035).
        self.assertNotIn("install", document)
        self.assertNotIn("loader", document)
        self.assertEqual(document["install_size"], 30)

        # With an authored descriptor, the resolved destination is stamped.
        # `standalone` needs a launch (RFC 0035, rule 4), and the launch has to
        # exist in this release's archive (rule 3).
        listing["install"] = {"target": "standalone"}
        listing["provides"] = {"launch": "StarMap.exe"}
        document = stamp(
            listing, dict(RELEASE, tag="0.4.6"), data, GAME_VERSIONS, now=NOW
        )
        self.assertEqual(document["install"], {"derived": True, "target": "standalone"})

    def test_an_unknown_anchor_is_an_error(self):
        with self.assertRaises(StampError):
            self.stamp(listing=dict(LISTING, install={"target": "somewhere"}))

    def test_a_pack_has_no_generated_half(self):
        with self.assertRaises(StampError):
            self.stamp(listing=dict(LISTING, type="modpack"))

    def test_the_archive_has_to_be_a_zip(self):
        with self.assertRaises(StampError):
            self.stamp(data=b"not a zip at all")

    def test_game_min_is_required(self):
        with self.assertRaises(StampError):
            self.stamp(listing=dict(LISTING, compatibility={}))

    def test_the_file_is_written_the_way_every_stamped_file_is(self):
        rendered = serialize(self.stamp())
        self.assertTrue(rendered.endswith("}\n"))
        self.assertEqual(json.loads(rendered)["id"], "AutoStage")
        self.assertIn('\n  "version": "0.4.3",', rendered)

    def test_stamping_twice_gives_the_same_bytes(self):
        self.assertEqual(serialize(self.stamp()), serialize(self.stamp()))

    def test_a_foreign_spec_version_is_refused(self):
        # A document claiming a format this stamper does not implement must not
        # be silently stamped as spec_version 1.
        with self.assertRaises(StampError):
            self.stamp(listing=dict(LISTING, spec_version=2))

    def test_a_duplicate_authored_dependency_id_is_an_error(self):
        listing = dict(
            LISTING,
            dependencies=[
                {"id": "KittenExtensions", "kind": "required", "min": "0.4.0"},
                {"id": "kittenextensions", "kind": "suggests"},
            ],
        )
        with self.assertRaises(StampError):
            self.stamp(listing=listing)


class ChangelogText(unittest.TestCase):
    def stamp(self, notes):
        return stamp(
            LISTING, dict(RELEASE, changelog_text=notes), mod_archive(), GAME_VERSIONS, now=NOW
        )

    def test_whitespace_is_trimmed_and_line_endings_are_lf(self):
        document = self.stamp("\r\n  ## Changes\r\n- One\r- Two  \n\n")
        self.assertEqual(document["changelog_text"], "## Changes\n- One\n- Two")

    def test_empty_notes_are_left_out(self):
        for notes in (None, "", " \r\n\t", 7):
            self.assertNotIn("changelog_text", self.stamp(notes), repr(notes))

    def test_notes_longer_than_16_kib_of_utf_8_are_left_out_and_not_cut(self):
        self.assertEqual(CHANGELOG_TEXT_LIMIT, 16384)
        self.assertEqual(self.stamp("x" * 16384)["changelog_text"], "x" * 16384)
        self.assertNotIn("changelog_text", self.stamp("x" * 16385))
        # Two bytes each, so the limit counts bytes and not characters.
        self.assertNotIn("changelog_text", self.stamp("ä" * 8193))

    def test_text_that_cannot_be_written_as_utf_8_is_left_out(self):
        self.assertIsNone(changelog_text("\ud800"))

    def test_the_text_sits_between_the_changelog_link_and_the_listing(self):
        keys = list(self.stamp("## Changes"))
        self.assertEqual(keys[keys.index("changelog"):], ["changelog", "changelog_text", "listing"])


class Ids(unittest.TestCase):
    def test_the_id_rules_of_rfc_0031(self):
        for good in ("A", "AutoStage", "My.Mod-2_x", "a" * 64):
            self.assertTrue(valid_id(good), good)
        for bad in (None, "", ".Mod", "Mod.", "-Mod", "My Mod", "My/Mod",
                    "..", "../Evil", "a" * 65, "CON", "con.mod", "Core", "COM1"):
            self.assertFalse(valid_id(bad), repr(bad))

    def test_an_invalid_id_rejects_the_stamp(self):
        listing = dict(LISTING, id="../AutoStage")
        with self.assertRaises(StampError):
            stamp(listing, RELEASE, mod_archive(), GAME_VERSIONS, now=NOW)


class SaveNames(unittest.TestCase):
    """The name rule of RFC 0085, as `SaveName.IsSanitized` decides it."""

    def test_names_the_game_keeps(self):
        for name in ("Example Rover", "Rover_2-b", "CON_", "a" * 64, "Räver", "-"):
            with self.subTest(name=name):
                self.assertTrue(valid_save_name(name))
                self.assertIsNone(save_name_problem(name))

    def test_names_the_game_changes(self):
        for name in (
            "", "a" * 65, " Rover", "Rover ", "Two  spaces", "con", "Lpt9", "Rover.v2",
            "Rover²", "\U0001d400", "Rover\n", None, 7,
        ):
            with self.subTest(name=name):
                self.assertFalse(valid_save_name(name))
                self.assertTrue(save_name_problem(name))

    def test_the_limit_counts_utf_16_code_units(self):
        self.assertIn("65 UTF-16 code units", save_name_problem("a" * 63 + "\U0001d400"))

    def test_a_character_the_game_drops_is_named(self):
        self.assertIn("U+00B2", save_name_problem("Rover²"))

    def test_the_vector_file_of_the_authored_repository(self):
        path = authored_root() / "schemas" / "save-names.json"
        if not path.is_file():
            self.skipTest(f"{path} is not there yet")
        vectors = json.loads(path.read_text(encoding="utf-8"))["vectors"]
        self.assertTrue(vectors)
        for vector in vectors:
            with self.subTest(name=vector["name"]):
                self.assertEqual(valid_save_name(vector["value"]), vector["accepted"], vector.get("why"))


VEHICLE = {
    "spec_version": 1,
    "id": "ExampleRover",
    "type": "vehicle",
    "name": "Example Rover",
    "authors": ["Maxi"],
    "abstract": "A rover for the Moon.",
    "license": "CC-BY-4.0",
    "dependencies": [{"id": "KittenExtensions", "kind": "required", "min": "0.4"}],
}

# As the game writes it, with seven fraction digits in a local date-time.
META = """\
name = "Example Rover"
created = 2026-09-22T14:22:28.6152545
updated = 2026-09-22T14:30:01.1000000
version = "v2026.9.22.5482"
systems = [ "Sol", ]
"""

VEHICLE_XML = '<?xml version="1.0" encoding="utf-8"?>\n<VehicleSaveData><Parts /></VehicleSaveData>\n'

UNIVERSE_XML = '<?xml version="1.0" encoding="utf-8"?>\n<UniverseData />\n'


def game_archive(folder="Example Rover", meta=META, game="vehicle.xml", content=VEHICLE_XML, **more):
    prefix = f"{folder}/" if folder else ""
    files = {f"{prefix}meta.toml": meta, f"{prefix}{game}": content}
    files.update({f"{prefix}{name}": text for name, text in more.items()})
    return archive(files)


class Vehicles(unittest.TestCase):
    """RFC 0085: a vehicle is stamped from the game folder in its archive."""

    def stamp(self, listing=None, data=None, notes=None, **changes):
        return stamp(
            {**(listing or VEHICLE), **changes},
            dict(RELEASE, tag="1.0.0"),
            data if data is not None else game_archive(),
            GAME_VERSIONS,
            now=NOW,
            notes=notes,
        )

    def refused(self, data=None, **changes):
        with self.assertRaises(StampError) as raised:
            self.stamp(data=data, **changes)
        return str(raised.exception)

    def test_one_top_level_folder_is_the_example_of_the_rfc(self):
        notes = []
        document = self.stamp(notes=notes)
        self.assertEqual(
            document["install"], {"root": "Example Rover", "derived": True, "folder": "Example Rover"}
        )
        self.assertEqual((document["game_min"], document["game_min_revision"]), ("2026.9.22.5482", 5482))
        self.assertEqual(
            document["dependencies"],
            [{"id": "KittenExtensions", "kind": "required", "min": "0.4.0", "source": "authored"}],
        )
        self.assertNotIn("loader", document)
        self.assertEqual(document["install_size"], len(META) + len(VEHICLE_XML))
        self.assertEqual(notes, [])

    def test_a_folder_at_the_archive_root(self):
        document = self.stamp(data=game_archive(folder=""))
        self.assertEqual(document["install"], {"derived": True, "folder": "Example Rover"})

    def test_an_authored_root(self):
        data = archive({"share/Example Rover/meta.toml": META, "share/Example Rover/vehicle.xml": VEHICLE_XML})
        document = self.stamp(data=data, install={"root": "share/Example Rover"})
        self.assertEqual(
            document["install"], {"root": "share/Example Rover", "derived": False, "folder": "Example Rover"}
        )

    def test_an_authored_game_min_wins(self):
        document = self.stamp(compatibility={"game_min": "2026.8.3.5117"})
        self.assertEqual((document["game_min"], document["game_min_revision"]), ("2026.8.3.5117", 5117))
        meta = META.replace("v2026.9.22.5482", "v2026.9.22.5482-beta")
        document = self.stamp(data=game_archive(meta=meta), compatibility={"game_min": "2026.8.3.5117"})
        self.assertEqual(document["game_min_revision"], 5117)

    def test_a_version_that_is_no_build_needs_an_authored_game_min(self):
        for version, reason in (
            ("v2026.9.22.5482-beta", "without a suffix"),
            ("0.0.0.0", "unknown build"),
            ("2026.9.22", "without a suffix"),
            ("v2026.9.22.5482\\n", "without a suffix"),
        ):
            meta = META.replace("v2026.9.22.5482", version)
            with self.subTest(version=version):
                self.assertIn(reason, self.refused(data=game_archive(meta=meta)))
        self.assertIn("without a suffix", self.refused(data=game_archive(meta='name = "Example Rover"\n')))

    def test_the_meta_toml_has_to_be_there_and_parse(self):
        self.assertIn("meta.toml", self.refused(data=archive({"Example Rover/vehicle.xml": VEHICLE_XML})))
        data = archive({"Example Rover/vehicle.xml": VEHICLE_XML})
        self.assertIn(
            "has no Example Rover/meta.toml", self.refused(data=data, install={"root": "Example Rover"})
        )
        self.assertIn("not valid TOML", self.refused(data=game_archive(meta="name = ")))

    def test_the_name_has_to_pass_the_name_rule(self):
        self.assertIn("no `name` string", self.refused(data=game_archive(meta='version = "v2026.9.22.5482"\n')))
        self.assertIn("no `name` string", self.refused(data=game_archive(meta=META.replace('"Example Rover"', "7"))))
        meta = META.replace('"Example Rover"', '"Example  Rover"')
        self.assertIn("two spaces", self.refused(data=game_archive(folder="Example  Rover", meta=meta)))

    def test_the_folder_has_to_be_the_name(self):
        self.assertIn("does not match", self.refused(data=game_archive(folder="Rover")))
        self.assertIn("casing", self.refused(data=game_archive(folder="example rover")))

    def test_the_game_file_has_to_be_there_with_its_exact_case(self):
        data = archive({"Example Rover/meta.toml": META})
        self.assertIn("has no Example Rover/vehicle.xml", self.refused(data=data))
        refused = self.refused(data=game_archive(game="Vehicle.xml"))
        self.assertIn("'Example Rover/Vehicle.xml'", refused)
        self.assertIn("exact case", refused)

    def test_the_game_file_has_to_be_well_formed_xml(self):
        self.assertIn("not well-formed", self.refused(data=game_archive(content="<VehicleSaveData>")))
        self.assertIn("not well-formed", self.refused(data=game_archive(content="")))

    def test_a_doctype_is_refused(self):
        content = '<?xml version="1.0"?>\n<!DOCTYPE a [<!ENTITY x "y">]>\n<a>&x;</a>\n'
        self.assertIn("DOCTYPE", self.refused(data=game_archive(content=content)))

    def test_a_game_file_above_the_limit_is_refused_before_it_is_read(self):
        with patch("stamp_release.GAME_FILE_LIMIT", len(VEHICLE_XML) - 1):
            self.assertIn("byte limit", self.refused())
        with patch("stamp_release.GAME_FILE_LIMIT", len(VEHICLE_XML)):
            self.stamp()

    def test_two_game_folders_are_not_derivable(self):
        data = archive({
            "Example Rover/meta.toml": META, "Example Rover/vehicle.xml": VEHICLE_XML,
            "Other Rover/meta.toml": META, "Other Rover/vehicle.xml": VEHICLE_XML,
        })
        self.assertIn("named after `name`", self.refused(data=data))

    def test_what_the_type_cannot_author(self):
        for install in ({"target": "user-data"}, {"path": "Vehicles"}, {"manages": ["x"]}, {"folder": "X"}):
            with self.subTest(install=install):
                self.assertIn("cannot author", self.refused(install=install))
        self.assertIn("no [loader]", self.refused(loader={"id": "StarMap", "min": "0.4.5"}))
        self.assertIn("mod-loader", self.refused(provides={"launch": "vehicle.xml"}))

    def test_other_files_of_a_vehicle_get_one_note(self):
        notes = []
        names = {"thumbnail.png": "x", "parts/a.json": "{}"}
        self.stamp(data=game_archive(**names), notes=notes)
        self.assertEqual(len(notes), 1)
        self.assertIn('"thumbnail.png"', notes[0])
        self.assertIn('"parts/a.json"', notes[0])
        self.assertNotIn('"vehicle.xml"', notes[0])

    def test_a_note_never_breaks_a_line_and_stays_short(self):
        notes = []
        names = {f"extra{index:02}.txt": "x" for index in range(25)}
        names["evil\nListing: x@1"] = "x"
        self.stamp(data=game_archive(**names), notes=notes)
        self.assertNotIn("\n", notes[0])
        self.assertIn("and 6 more", notes[0])

    def test_without_a_notes_list_a_vehicle_still_stamps(self):
        self.assertEqual(self.stamp(data=game_archive(extra="x"))["install"]["folder"], "Example Rover")


class Saves(unittest.TestCase):
    def stamp(self, data, notes=None):
        listing = {**VEHICLE, "id": "MoonBase", "type": "save"}
        return stamp(listing, dict(RELEASE, tag="1.0.0"), data, GAME_VERSIONS, now=NOW, notes=notes)

    def test_a_save_is_stamped_from_universe_xml_and_other_files_get_no_note(self):
        notes = []
        data = game_archive(game="universe.xml", content=UNIVERSE_XML, **{"ModState/a.json": "{}"})
        document = self.stamp(data, notes)
        self.assertEqual(document["type"], "save")
        self.assertEqual(document["install"]["folder"], "Example Rover")
        self.assertEqual(notes, [])

    def test_a_save_needs_universe_xml(self):
        with self.assertRaisesRegex(StampError, "universe.xml"):
            self.stamp(archive({"Example Rover/meta.toml": META, "Example Rover/vehicle.xml": VEHICLE_XML}))


class InstallFolders(unittest.TestCase):
    """RFC 0085: one folder per type, held by the listing that took it first."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.releases = Path(directory.name) / "releases"

    def release(self, listing_id, version, content_type="vehicle", folder="Example Rover", **extra):
        path = self.releases / listing_id / f"{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"id": listing_id, "type": content_type, "version": version, **extra}
        if folder is not None:
            document["install"] = {"root": folder, "derived": True, "folder": folder}
        path.write_text(serialize(document), encoding="utf-8")

    def holders(self, content_type="vehicle"):
        return folder_holders(self.releases, content_type)

    def document(self, listing_id, folder, content_type="vehicle"):
        return {"id": listing_id, "type": content_type, "install": {"derived": True, "folder": folder}}

    def test_another_listing_cannot_take_a_folder_without_case(self):
        self.release("ExampleRover", "1.0.0")
        with self.assertRaises(StampError) as raised:
            check_folder(self.document("OtherRover", "example rover"), self.holders())
        message = str(raised.exception)
        self.assertIn("`OtherRover`", message)
        self.assertIn("`ExampleRover`", message)
        self.assertIn("without case", message)

    def test_a_later_release_of_the_same_listing_keeps_its_folder(self):
        self.release("ExampleRover", "1.0.0")
        check_folder(self.document("examplerover", "Example Rover"), self.holders())

    def test_a_save_and_a_vehicle_do_not_collide(self):
        self.release("ExampleRover", "1.0.0")
        self.release("MoonBase", "1.0.0", content_type="save", folder="Moon Base")
        check_folder(self.document("RoverSave", "Example Rover", "save"), self.holders("save"))
        self.assertEqual(self.holders("save"), {"moon base": ["MoonBase"]})

    def test_a_yanked_or_gone_release_still_holds_its_folder(self):
        self.release("Yanked", "1.0.0", folder="A", yanked=True)
        self.release("Gone", "1.0.0", folder="B", download={"unavailable_since": "2026-09-01T00:00:00Z"})
        self.release("Mod", "1.0.0", content_type="mod", folder=None)
        self.assertEqual(self.holders(), {"a": ["Yanked"], "b": ["Gone"]})

    def test_a_missing_releases_folder_holds_nothing(self):
        self.assertEqual(self.holders(), {})

    def test_an_unreadable_release_file_is_an_error(self):
        self.release("ExampleRover", "1.0.0")
        (self.releases / "Broken").mkdir()
        (self.releases / "Broken" / "1.0.0.json").write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(StampError, "releases/Broken/1.0.0.json"):
            self.holders()

    def test_a_document_without_a_folder_is_never_refused(self):
        check_folder({"id": "M", "type": "mod"}, {"m": ["Other"]})


class Rfc0035(unittest.TestCase):
    """The release-time rules of the install descriptor."""

    def loader_listing(self, install=None, provides=None):
        listing = {
            "spec_version": 1,
            "id": "StarMap",
            "type": "mod-loader",
            "name": "StarMap",
            "authors": ["KlaasWhite"],
            "abstract": "Mod loader that runs code mods.",
            "license": "MIT",
            "links": {"forums": "https://forums.ahwoo.com/threads/starmap-mod-loader.384/"},
            "compatibility": {"game_min": "2026.8.3.5117"},
        }
        if install is not None:
            listing["install"] = install
        if provides is not None:
            listing["provides"] = provides
        return listing

    def loader_archive(self):
        return archive({"StarMap.exe": "x" * 10, "StarMap.dll": "y" * 20})

    def stamp_loader(self, install=None, provides=None):
        return stamp(
            self.loader_listing(install, provides),
            dict(RELEASE, tag="0.4.6"),
            self.loader_archive(),
            GAME_VERSIONS,
            now=NOW,
        )

    def test_paths_must_be_relative_and_contained(self):
        # Rule 1: absolute, home-relative, and escaping paths are invalid.
        for bad in ("/etc/x", "~x/y", "..", "../x", "a/../../x", "C:/x", "a\\b"):
            with self.assertRaises(StampError, msg=bad):
                relative_path(bad, "test")
        # A `..` that stays inside its anchor is contained, and the published
        # value is the normalized form, so no client normalizes its own way.
        self.assertEqual(relative_path("a/../b", "test"), "b")
        self.assertEqual(relative_path("./tools", "test"), "tools")
        self.assertEqual(relative_path("build/AutoStage", "test"), "build/AutoStage")

    def test_an_escaping_install_path_rejects_the_release(self):
        with self.assertRaises(StampError):
            self.stamp_loader(
                install={"target": "standalone", "path": "../outside"},
                provides={"launch": "StarMap.exe"},
            )

    def test_a_mod_cannot_author_a_target_or_path(self):
        # The folder name is the identity the game sees, so a mod's location is
        # a default, not an authorable field.
        for install in ({"target": "game-root"}, {"path": "sub"}):
            listing = dict(LISTING, install=install)
            with self.assertRaises(StampError, msg=install):
                stamp(listing, RELEASE, mod_archive(), GAME_VERSIONS, now=NOW)

    def test_a_loader_install_section_needs_a_target(self):
        # The type has no default to fall back on.
        with self.assertRaises(StampError):
            self.stamp_loader(install={"path": "somewhere"})

    def test_provides_is_loader_only(self):
        listing = dict(LISTING, provides={"launch": "AutoStage.dll"})
        with self.assertRaises(StampError):
            stamp(listing, RELEASE, mod_archive(), GAME_VERSIONS, now=NOW)

    def test_a_launch_absent_from_the_archive_rejects_the_release(self):
        with self.assertRaises(StampError):
            self.stamp_loader(provides={"launch": "Missing.exe"})

    def test_a_launch_present_in_the_archive_passes(self):
        document = self.stamp_loader(provides={"launch": "StarMap.exe"})
        self.assertNotIn("install", document)

    def test_a_platform_launch_absent_from_the_archive_rejects_the_release(self):
        provides = {
            "launch": "StarMap.exe",
            "platform": {"linux": {"runtime": "dotnet", "launch": "Missing.dll"}},
        }
        with self.assertRaises(StampError) as raised:
            self.stamp_loader(provides=provides)
        self.assertIn("the provides platform linux launch path 'Missing.dll'", str(raised.exception))

    def test_a_platform_launch_present_in_the_archive_passes(self):
        provides = {
            "launch": "StarMap.exe",
            "platform": {
                "linux": {"runtime": "dotnet", "launch": "StarMap.dll"},
                "macos": {"runtime": "dotnet", "launch": "StarMap.dll"},
            },
        }
        document = self.stamp_loader(provides=provides)
        self.assertNotIn("install", document)
        self.assertNotIn("provides", document)

    def test_an_escaping_platform_launch_rejects_the_release(self):
        provides = {"launch": "StarMap.exe", "platform": {"linux": {"launch": "../StarMap.dll"}}}
        with self.assertRaises(StampError) as raised:
            self.stamp_loader(provides=provides)
        self.assertIn(
            "the provides platform linux launch path '../StarMap.dll' escapes its anchor",
            str(raised.exception),
        )

    def test_standalone_requires_a_launch(self):
        with self.assertRaises(StampError):
            self.stamp_loader(install={"target": "standalone"})

    def test_an_unknown_content_dir_is_an_error(self):
        with self.assertRaises(StampError):
            self.stamp_loader(provides={"launch": "StarMap.exe", "content-dir": "somewhere"})


if __name__ == "__main__":
    unittest.main()
