#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the folders of the authored repository (RFC 0031, RFC 0080, RFC 0085)."""

import tempfile
import unittest
from pathlib import Path

import authored


class Folders(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def document(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'id = "{path.stem}"\n', encoding="utf-8")
        return path

    def test_a_vehicle_is_found_under_vehicles(self):
        mod = self.document("listings/Mod.toml")
        rover = self.document("vehicles/ExampleRover.toml")
        base = self.document("saves/MoonBase.toml")
        self.assertEqual(authored.find(self.root, "ExampleRover"), rover)
        self.assertEqual(authored.find(self.root, "Missing"), None)
        self.assertEqual(authored.listing_files(self.root), [mod, rover, base])

    def test_a_tree_without_vehicles_and_saves_gives_its_listings(self):
        files = [self.document("listings/B.toml"), self.document("listings/A.toml")]
        self.assertEqual(authored.listing_files(self.root), sorted(files))

    def test_a_type_outside_its_folder_names_the_folder(self):
        problem = authored.type_problem("vehicles", "mod")
        self.assertIn("vehicles/", problem)
        self.assertIn("belongs under listings/", problem)
        problem = authored.type_problem("listings", "vehicle")
        self.assertIn("listings/", problem)
        self.assertIn("belongs under vehicles/", problem)
        self.assertIn("names no type", authored.type_problem("saves", None))

    def test_the_schema_keeps_the_types_of_listings(self):
        for content_type in ("mod", "mod-loader", None, "modpack"):
            self.assertIsNone(authored.type_problem("listings", content_type), content_type)
        self.assertIsNone(authored.type_problem("vehicles", "vehicle"))
        self.assertIsNone(authored.type_problem("saves", "save"))

    def test_an_id_is_held_across_every_folder_without_case(self):
        self.document("listings/Rover.toml")
        self.document("vehicles/rover.toml")
        (self.root / "packs" / "ROVER").mkdir(parents=True)
        self.document("saves/Other.toml")
        holders = authored.id_holders(self.root)
        self.assertEqual(holders["rover"], ["listings/Rover.toml", "vehicles/rover.toml", "packs/ROVER/"])
        self.assertEqual(holders["other"], ["saves/Other.toml"])


if __name__ == "__main__":
    unittest.main()
