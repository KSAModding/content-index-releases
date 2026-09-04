#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the examples check. `python3 -m unittest discover tools`.

Only the half the archive and its host decide belongs in the diff. The rest is
frozen from the listing (RFC 0031) and drifts when it is edited.
"""

import unittest

from verify_examples import ARCHIVE_FACTS, AUTHORED_FACTS, archive_facts


def release(**overrides):
    """A stamped release file, in the shape the index writes one."""
    document = {
        "spec_version": 1,
        "id": "AdvancedFlightComputer",
        "type": "mod",
        "version": "0.7.0",
        "version_scheme": "semver",
        "release_status": "stable",
        "release_date": "2026-08-02T13:18:41Z",
        "game_min": "2026.8.3.5117",
        "game_min_revision": 5117,
        "download": {
            "url": "https://example.invalid/AdvancedFlightComputer.zip",
            "sha256": "A" * 64,
            "size": 131042,
            "content_type": "application/zip",
        },
        "install_size": 329449,
        "install": {"root": "AdvancedFlightComputer", "derived": True},
        "loader": {"id": "StarMap", "min": "0.4.5", "source": "authored"},
        "dependencies": [
            {"id": "KittenExtensions", "kind": "optional", "source": "derived"},
            {"id": "AutoStage", "kind": "recommends", "source": "authored"},
        ],
        "changelog": "https://example.invalid/tag/v0.7.0",
        "listing": {"name": "Advanced Flight Computer", "license": "MIT"},
    }
    document.update(overrides)
    return document


class Split(unittest.TestCase):
    def test_every_field_of_a_full_release_is_on_one_side(self):
        # An unclassified field would be dropped silently.
        unclassified = set(release()) - set(ARCHIVE_FACTS) - set(AUTHORED_FACTS)
        self.assertEqual(unclassified, {"install"})

    def test_neither_half_claims_the_same_field(self):
        self.assertEqual(set(ARCHIVE_FACTS) & set(AUTHORED_FACTS), set())


class Kept(unittest.TestCase):
    def test_the_facts_the_host_and_the_archive_decide(self):
        facts = archive_facts(release())
        for key in (
            "version", "version_scheme", "release_status", "release_date",
            "download", "install_size", "changelog",
        ):
            self.assertIn(key, facts)

    def test_the_identity_that_says_which_release_this_is(self):
        facts = archive_facts(release())
        self.assertEqual(facts["spec_version"], 1)
        self.assertEqual(facts["id"], "AdvancedFlightComputer")
        self.assertEqual(facts["type"], "mod")

    def test_a_derived_install_root(self):
        facts = archive_facts(release())
        self.assertEqual(facts["install"], {"root": "AdvancedFlightComputer", "derived": True})


class Dropped(unittest.TestCase):
    def test_everything_frozen_from_the_authored_listing(self):
        facts = archive_facts(release(
            game_max="2026.8.19.5261", game_max_revision=5261, os=["windows"],
        ))
        for key in AUTHORED_FACTS:
            self.assertNotIn(key, facts)

    def test_the_install_anchor_and_path_below_it(self):
        # The shape the index stamps for StarMap.
        facts = archive_facts(release(
            type="mod-loader",
            install={"derived": True, "target": "standalone", "path": "bin"},
        ))
        self.assertEqual(facts["install"], {"derived": True})

    def test_an_authored_root_and_the_size_measured_under_it(self):
        facts = archive_facts(release(install={"root": "build/AFC", "derived": False}))
        self.assertEqual(facts["install"], {"derived": False})
        self.assertNotIn("install_size", facts)

    def test_mirrors_when_the_caller_did_not_check_them(self):
        stamped = release()
        stamped["download"] = dict(stamped["download"], mirrors=["https://x.invalid/a.zip"])

        self.assertNotIn("mirrors", archive_facts(stamped, with_mirrors=False)["download"])
        self.assertIn("mirrors", archive_facts(stamped, with_mirrors=True)["download"])


class Comparison(unittest.TestCase):
    """What the check does with two documents, which is the point of the split."""

    def test_a_listing_edit_since_the_stamp_is_not_a_difference(self):
        # The drift KSAModding/content-manager-design#47 introduced.
        frozen = release(
            loader={"id": "StarMap", "min": "0.4.5", "max": "0.4.6", "source": "authored"},
        )
        restamped = release(
            game_min="2026.8.19.5261",
            game_min_revision=5261,
            loader={"id": "StarMap", "min": "0.4.5", "source": "authored"},
            listing={"name": "Advanced Flight Computer", "license": "MIT",
                     "description": "A longer description written since."},
        )

        self.assertEqual(archive_facts(frozen), archive_facts(restamped))

    def test_an_authored_entry_swallowing_a_derived_one_is_not_a_difference(self):
        # merge_dependencies drops the derived entry; the archive did not move.
        restamped = release(dependencies=[
            {"id": "KittenExtensions", "kind": "required", "min": "0.4.0",
             "source": "authored"},
        ])

        self.assertEqual(archive_facts(release()), archive_facts(restamped))

    def test_a_changed_digest_still_is_one(self):
        other = release()
        other["download"] = dict(other["download"], sha256="B" * 64)

        self.assertNotEqual(archive_facts(release()), archive_facts(other))

    def test_a_changed_install_size_still_is_one(self):
        self.assertNotEqual(
            archive_facts(release()), archive_facts(release(install_size=1)),
        )

    def test_a_changed_install_root_still_is_one(self):
        self.assertNotEqual(
            archive_facts(release()),
            archive_facts(release(install={"root": "Other", "derived": True})),
        )


class Shape(unittest.TestCase):
    def test_absent_optionals_do_not_appear(self):
        bare = release()
        for key in ("changelog", "install", "install_size"):
            del bare[key]

        facts = archive_facts(bare)

        for key in ("changelog", "install", "install_size"):
            self.assertNotIn(key, facts)

    def test_an_absent_install_object_still_means_a_derived_root(self):
        # install_object returns None for an archive root with nothing authored.
        bare = release()
        del bare["install"]

        self.assertEqual(archive_facts(bare)["install_size"], 329449)


if __name__ == "__main__":
    unittest.main()
