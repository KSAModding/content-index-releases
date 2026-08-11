#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the stamper. No token, no network: `python3 -m unittest discover tools`.

Every case here is one sentence from RFC 0031, RFC 0035 or RFC 0017 that the
stamper has to keep. The archives are built in memory, so the fixtures are the
tests.

The other half of the stamper's test is the design repository's examples/, where
every value was produced by this procedure by hand: `tools/verify_examples.py`
re-derives all of them from their release hosts and diffs.
"""

import io
import json
import unittest
import zipfile
from datetime import datetime, timezone

from stamp_release import (
    StampError,
    derived_dependencies,
    merge_dependencies,
    normalize_version,
    relative_path,
    release_status,
    resolve_bound,
    serialize,
    stamp,
    valid_id,
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

    def test_a_version_that_does_not_parse_is_rejected(self):
        for tag in ("rc", "1.2", "2026.8.3.5117", "", None, "v1.02.3"):
            with self.assertRaises(StampError):
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

    def test_standalone_requires_a_launch(self):
        with self.assertRaises(StampError):
            self.stamp_loader(install={"target": "standalone"})

    def test_an_unknown_content_dir_is_an_error(self):
        with self.assertRaises(StampError):
            self.stamp_loader(provides={"launch": "StarMap.exe", "content-dir": "somewhere"})


if __name__ == "__main__":
    unittest.main()
