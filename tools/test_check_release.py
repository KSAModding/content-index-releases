#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the release pull request check. No network: a fake Http serves the archive.

The submitted files come from the stamper itself, so a passing case is the round trip and every rejection is one field moved away from it.
"""

import io
import json
import os
import tempfile
import tomllib
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import check_release
import hosts
from stamp_release import CHANGELOG_TEXT_LIMIT, stamp

GAME_VERSIONS = ["2026.7.5.4892", "2026.7.9.5018", "2026.8.3.5117", "2026.8.19.5261"]

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

URL = "https://example.invalid/downloads/Mod-1.0.0.zip"
PATH = "releases/Mod/1.0.0.json"

MOD_TOML = """\
name = "Mod"

[StarMap]
EntryAssembly = "Mod"

[[StarMap.ModDependencies]]
ModId = "KittenExtensions"
Optional = true
"""

LISTING = """\
spec_version = 1
id = "Mod"
type = "mod"
name = "Mod"
authors = ["Maxi"]
abstract = "A mod."
license = "MIT"
tags = ["control"]

[links]
forums = "https://forums.ahwoo.com/threads/mod.1/"
repository = "https://github.com/someone/Mod"

[compatibility]
game_min = "2026.8.3.5117"

[loader]
id = "StarMap"
min = "0.4.5"
"""

RELEASE = {
    "tag": "v1.0.0",
    "release_date": "2026-08-19T10:00:00Z",
    "url": URL,
    "content_type": "application/zip",
    "prerelease": False,
    "changelog": "https://example.invalid/mod/changes",
}


def archive(files):
    """A zip archive of {path: text}, as bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, content in files.items():
            handle.writestr(name, content)
    return buffer.getvalue()


def mod_archive(manifest=MOD_TOML):
    return archive({"Mod/Mod.dll": "x" * 100, "Mod/mod.toml": manifest})


def http_error(code):
    return urllib.error.HTTPError(URL, code, "boom", {}, None)


class FakeHttp:
    """Serves bytes by URL, raises what a route holds, and refuses anything else."""

    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    def get(self, url, accept=None, etag=None, api=False, limit=None):
        self.requested.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected URL {url}")
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        return hosts.Response(200, {"Content-Type": "application/octet-stream"}, answer)

    def archive(self, url, api=False, limit=None):
        answer = self.get(url, api=api)
        return hosts.Archive.of_bytes(answer.body), answer.headers


class Fixture(unittest.TestCase):
    """A content-index checkout with one listing, and a host serving its archive."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.listing("Mod.toml", LISTING)
        self.archive = mod_archive()
        self.http = FakeHttp({URL: self.archive})

    def listing(self, name, text):
        listings = self.root / "listings"
        listings.mkdir(parents=True, exist_ok=True)
        (listings / name).write_text(text, encoding="utf-8")

    def stamped(self, release=None, data=None, **changes):
        document = stamp(
            tomllib.loads(LISTING), release or RELEASE, data or self.archive, GAME_VERSIONS, now=NOW
        )
        document.update(changes)
        return document

    def check(self, head, path=PATH, http=None):
        return check_release.check(path, head, self.root, GAME_VERSIONS, http or self.http, now=NOW)

    def rejected(self, head, path=PATH, http=None):
        outcome = self.check(head, path, http)
        self.assertEqual(outcome.outcome, check_release.REJECT, outcome.messages)
        return "\n".join(outcome.messages)


class RoundTrip(Fixture):
    def test_a_file_the_stamper_wrote_passes(self):
        outcome = self.check(self.stamped())
        self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)
        self.assertIn(URL, outcome.messages[0])
        self.assertEqual(self.http.requested, [URL])

    def test_the_hosts_pre_release_flag_is_the_authors_word(self):
        # `testing` on a version with no pre-release identifiers can only have
        # come from the host, and no archive can confirm or deny it.
        head = self.stamped(release=dict(RELEASE, prerelease=True))
        self.assertEqual(head["release_status"], "testing")
        outcome = self.check(head)
        self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)

    def test_a_content_type_a_host_declares_for_a_zip_passes(self):
        head = self.stamped(release=dict(RELEASE, content_type="application/x-zip-compressed"))
        outcome = self.check(head)
        self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)

    def test_a_release_without_a_changelog_passes(self):
        head = self.stamped(release=dict(RELEASE, changelog=None))
        self.assertNotIn("changelog", head)
        outcome = self.check(head)
        self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)

    def test_release_notes_are_the_authors_word(self):
        for notes in ("## Changes\n- Nothing a host says", "x" * CHANGELOG_TEXT_LIMIT):
            head = self.stamped(release=dict(RELEASE, changelog_text=notes))
            self.assertEqual(head["changelog_text"], notes)
            outcome = self.check(head)
            self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)


class Derivation(Fixture):
    """Every field the archive and the authored document decide."""

    def test_a_wrong_checksum_is_rejected(self):
        head = self.stamped()
        head["download"]["sha256"] = "0" * 64
        self.assertIn("download.sha256", self.rejected(head))

    def test_a_wrong_size_is_rejected(self):
        head = self.stamped()
        head["download"]["size"] = 1
        self.assertIn("download.size", self.rejected(head))

    def test_a_different_archive_behind_the_url_is_rejected(self):
        # The dependencies come out of the archive's own mod.toml.
        other = FakeHttp({URL: mod_archive(manifest='name = "Mod"\n')})
        messages = self.rejected(self.stamped(), http=other)
        self.assertIn("dependencies", messages)
        self.assertIn("download.sha256", messages)

    def test_a_leading_v_is_not_a_stamped_version(self):
        head = self.stamped(version="v1.0.0")
        self.assertIn("version", self.rejected(head, path="releases/Mod/v1.0.0.json"))

    def test_a_release_status_the_version_contradicts_is_rejected(self):
        self.assertIn("release_status", self.rejected(self.stamped(release_status="dev")))

    def test_a_pre_release_version_is_never_stable(self):
        url = URL.replace("1.0.0", "1.0.0-rc.1")
        head = self.stamped(release=dict(RELEASE, tag="1.0.0-rc.1", url=url), release_status="stable")
        messages = self.rejected(
            head, path="releases/Mod/1.0.0-rc.1.json", http=FakeHttp({url: self.archive})
        )
        self.assertIn("release_status", messages)

    def test_the_listing_block_is_measured_against_the_current_authored_document(self):
        self.listing("Mod.toml", LISTING.replace('abstract = "A mod."', 'abstract = "A better mod."'))
        messages = self.rejected(self.stamped())
        self.assertIn("listing", messages)
        self.assertIn("abstract", messages)

    def test_a_bound_the_authored_document_no_longer_states_is_rejected(self):
        self.listing("Mod.toml", LISTING.replace('game_min = "2026.8.3.5117"', 'game_min = "2026.8.19.5261"'))
        messages = self.rejected(self.stamped())
        self.assertIn("game_min_revision", messages)

    def test_a_key_the_stamper_does_not_write_is_rejected(self):
        self.assertIn("'extra'", self.rejected(self.stamped(extra=1)))

    def test_a_missing_key_is_rejected(self):
        head = self.stamped()
        del head["install_size"]
        self.assertIn("install_size", self.rejected(head))

    def test_an_archive_the_stamper_refuses_is_rejected(self):
        # The archive's folder is the identity the game sees.
        wrong = FakeHttp({URL: archive({"Other/mod.toml": MOD_TOML})})
        self.assertIn("does not stamp", self.rejected(self.stamped(), http=wrong))

    def test_a_corrupt_entry_is_a_fact_about_the_archive(self):
        # The entries are stored, so the bytes of mod.toml sit in the archive
        # verbatim and one changed byte fails its CRC on read.
        corrupt = self.archive.replace(b'name = "Mod"', b'name = "Mxd"', 1)
        self.assertNotEqual(corrupt, self.archive)
        messages = self.rejected(self.stamped(), http=FakeHttp({URL: corrupt}))
        self.assertIn("cannot be read", messages)

    def test_a_mod_toml_above_the_size_limit_is_rejected(self):
        # The one entry the stamper unpacks is bounded by its declared size.
        big = archive({"Mod/Mod.dll": "x", "Mod/mod.toml": "#" * (1024 * 1024 + 1)})
        messages = self.rejected(self.stamped(), http=FakeHttp({URL: big}))
        self.assertIn("byte limit", messages)


class BeforeAnyRequest(Fixture):
    """Everything that is decided without touching the URL."""

    def refused(self, head, path=PATH):
        http = FakeHttp({})
        outcome = check_release.check(path, head, self.root, GAME_VERSIONS, http, now=NOW)
        self.assertEqual(outcome.outcome, check_release.REJECT, outcome.messages)
        self.assertEqual(http.requested, [])
        return "\n".join(outcome.messages)

    def test_a_url_that_is_not_http_is_refused(self):
        # The URL is the author's, and a runner reads file: and ftp: too.
        for url in ("file:///etc/passwd", "ftp://example.invalid/x.zip", "https://", "x.zip", 7, None):
            head = self.stamped()
            head["download"]["url"] = url
            self.assertIn("download.url", self.refused(head), url)

    def test_mirrors_are_the_watchers(self):
        head = self.stamped()
        head["download"]["mirrors"] = ["https://example.invalid/other.zip"]
        self.assertIn("mirrors", self.refused(head))

    def test_a_gone_mark_is_the_watchers(self):
        head = self.stamped()
        head["download"]["unavailable_since"] = "2026-09-23T10:24:00Z"
        self.assertIn("download.unavailable_since is the watcher's", self.refused(head))

    def test_a_missing_download_object_is_refused(self):
        head = self.stamped()
        del head["download"]
        self.assertIn("download is missing", self.refused(head))
        head["download"] = "https://example.invalid/x.zip"
        self.assertIn("download is missing", self.refused(head))

    def test_a_release_is_never_submitted_yanked(self):
        self.assertIn("yanked", self.refused(self.stamped(yanked=True)))
        self.assertIn("yanked", self.refused(self.stamped(yanked_reason="x")))

    def test_the_release_date_has_the_stamped_form(self):
        for date in ("2026-08-19T10:00:00+00:00", "2026-08-19", "", None):
            self.assertIn("release_date", self.refused(self.stamped(release_date=date)), date)

    def test_an_octet_stream_content_type_is_not_what_the_stamper_writes(self):
        head = self.stamped()
        head["download"]["content_type"] = "application/octet-stream"
        self.assertIn("content_type", self.refused(head))

    def test_a_size_that_is_not_a_byte_count_is_refused(self):
        for size in ("1", -1, True, None):
            head = self.stamped()
            head["download"]["size"] = size
            self.assertIn("download.size", self.refused(head), size)

    def test_an_empty_changelog_is_refused(self):
        self.assertIn("changelog", self.refused(self.stamped(changelog="")))

    def test_changelog_text_that_is_not_a_string_is_refused(self):
        for value in (7, None, ["x"]):
            self.assertIn("changelog_text", self.refused(self.stamped(changelog_text=value)), value)

    def test_empty_changelog_text_is_refused(self):
        self.assertIn("changelog_text", self.refused(self.stamped(changelog_text=" \n")))

    def test_changelog_text_longer_than_the_limit_is_refused(self):
        for value in ("x" * (CHANGELOG_TEXT_LIMIT + 1), "ä" * (CHANGELOG_TEXT_LIMIT // 2 + 1)):
            self.assertIn("at most", self.refused(self.stamped(changelog_text=value)))

    def test_changelog_text_in_a_form_the_stamper_does_not_write_is_refused(self):
        for value in (" x", "x\n", "a\r\nb"):
            self.assertIn("stamper writes neither", self.refused(self.stamped(changelog_text=value)))

    def test_the_path_names_the_id_and_the_version(self):
        self.assertIn("folder", self.refused(self.stamped(), path="releases/Other/1.0.0.json"))
        self.assertIn("file name", self.refused(self.stamped(), path="releases/Mod/2.0.0.json"))

    def test_a_short_version_is_refused_before_the_download_with_its_stored_form(self):
        refused = self.refused(self.stamped(version="1.0"), path="releases/Mod/1.0.json")
        self.assertIn("releases/Mod/1.0.0.json", refused)

    def test_a_listing_that_is_not_there_is_refused(self):
        head = self.stamped(id="Other")
        self.assertIn("not in content-index", self.refused(head, path="releases/Other/1.0.0.json"))

    def test_a_watched_listing_is_the_watchers(self):
        self.listing("Mod.toml", LISTING + '\n[releases]\ngithub = "someone/Mod"\n')
        self.assertIn("watcher", self.refused(self.stamped()))

    def test_the_folder_matches_the_listing_id_letter_for_letter(self):
        # The authored id stays "Mod"; the folder says "mod".
        self.listing("mod.toml", LISTING)
        head = self.stamped(id="mod")
        self.assertIn("letter for letter", self.refused(head, path="releases/mod/1.0.0.json"))

    def test_something_that_is_not_an_object_is_refused(self):
        self.assertIn("not a JSON object", self.refused([1, 2]))

    def test_an_oversized_archive_is_refused(self):
        head = self.stamped()
        head["download"]["size"] = hosts.MAX_ARCHIVE_BYTES + 1
        self.assertIn("limit", self.refused(head))


class TheHost(Fixture):
    def test_a_gone_archive_is_rejected(self):
        # 404, 410 and 451 are facts about the release, reported to the author.
        for code in (404, 410, 451):
            outcome = self.check(self.stamped(), http=FakeHttp({URL: http_error(code)}))
            self.assertEqual(outcome.outcome, check_release.REJECT, code)
            self.assertIn("gone", outcome.messages[0])

    def test_a_host_having_a_bad_moment_reaches_no_verdict(self):
        for failure in (hosts.HostError("boom"), http_error(503)):
            outcome = self.check(self.stamped(), http=FakeHttp({URL: failure}))
            self.assertEqual(outcome.outcome, check_release.COULD_NOT_EVALUATE, failure)

    def test_a_listing_that_does_not_parse_reaches_no_verdict(self):
        # Repository state in the authored half, not something the pull request did.
        self.listing("Mod.toml", "id = ")
        outcome = self.check(self.stamped(), http=FakeHttp({}))
        self.assertEqual(outcome.outcome, check_release.COULD_NOT_EVALUATE)


class TagPrefix(Fixture):
    def test_a_release_stamped_from_a_prefixed_tag_passes(self):
        # The watcher hands the stamper the rest of the tag, and the check reads the version, so both stamp the same file.
        for prefix in ("gatos-v", "gatos-"):
            tagged = hosts.HostRelease(
                host="github", tag="gatos-v1.0.0", tag_prefix=prefix, version="1.0.0",
                release_date=RELEASE["release_date"], url=URL, changelog=RELEASE["changelog"],
            )
            head = self.stamped(release=tagged.facts())
            self.assertEqual(head["version"], "1.0.0")
            outcome = self.check(head)
            self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)


VEHICLE = """\
spec_version = 1
id = "ExampleRover"
type = "vehicle"
name = "Example Rover"
authors = ["Maxi"]
abstract = "A rover."
license = "CC-BY-4.0"

[[dependencies]]
id = "KittenExtensions"
kind = "required"
"""

META = 'name = "Example Rover"\nversion = "v2026.8.19.5261"\n'

ROVER_URL = "https://example.invalid/downloads/ExampleRover-1.0.0.zip"
ROVER_PATH = "releases/ExampleRover/1.0.0.json"


class Vehicles(Fixture):
    """RFC 0085: a vehicle release by pull request, measured as the watcher stamps it."""

    def setUp(self):
        super().setUp()
        (self.root / "vehicles").mkdir()
        (self.root / "vehicles" / "ExampleRover.toml").write_text(VEHICLE, encoding="utf-8")
        self.releases = self.root / "releases"
        self.rover = archive({
            "Example Rover/meta.toml": META,
            "Example Rover/vehicle.xml": "<VehicleSaveData />",
            "Example Rover/thumbnail.png": "x",
        })
        self.http = FakeHttp({ROVER_URL: self.rover})

    def stamped(self, **changes):
        document = stamp(
            tomllib.loads(VEHICLE), dict(RELEASE, url=ROVER_URL), self.rover, GAME_VERSIONS, now=NOW
        )
        document.update(changes)
        return document

    def check(self, head, path=ROVER_PATH, http=None):
        return check_release.check(
            path, head, self.root, GAME_VERSIONS, http or self.http, now=NOW, releases=self.releases
        )

    def held_by(self, listing_id, folder):
        path = self.releases / listing_id / "1.0.0.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"id": listing_id, "type": "vehicle", "install": {"folder": folder}}),
            encoding="utf-8",
        )

    def test_a_vehicle_that_matches_its_archive_passes_with_its_note(self):
        outcome = self.check(self.stamped())
        self.assertEqual(outcome.outcome, check_release.PASS, outcome.messages)
        self.assertEqual(len(outcome.messages), 2)
        self.assertTrue(outcome.messages[1].startswith("note: the vehicle folder holds files"))
        self.assertIn('"thumbnail.png"', outcome.messages[1])

    def test_every_field_that_differs_gets_its_own_message(self):
        head = self.stamped(game_min="2026.8.3.5117", game_min_revision=5117)
        head["install"] = dict(head["install"], folder="Other Rover")
        messages = self.rejected(head, path=ROVER_PATH)
        self.assertIn('install.folder: the submission says "Other Rover"', messages)
        self.assertIn('game_min: the submission says "2026.8.3.5117"', messages)
        self.assertIn("game_min_revision", messages)

    def test_a_folder_another_listing_holds_is_refused(self):
        self.held_by("OtherRover", "example rover")
        self.held_by("ExampleRover", "Example Rover")
        messages = self.rejected(self.stamped(), path=ROVER_PATH)
        self.assertIn("`OtherRover`", messages)
        self.assertNotIn("`ExampleRover` already", messages)

    def test_a_release_file_that_does_not_read_reaches_no_verdict(self):
        (self.releases / "Broken").mkdir(parents=True)
        (self.releases / "Broken" / "1.0.0.json").write_text("{", encoding="utf-8")
        outcome = self.check(self.stamped())
        self.assertEqual(outcome.outcome, check_release.COULD_NOT_EVALUATE, outcome.messages)

    def test_an_archive_with_a_program_in_the_folder_is_rejected(self):
        bad = archive({
            "Example Rover/meta.toml": META,
            "Example Rover/vehicle.xml": "<VehicleSaveData />",
            "Example Rover/Setup.exe": "MZ",
        })
        messages = self.rejected(self.stamped(), path=ROVER_PATH, http=FakeHttp({ROVER_URL: bad}))
        self.assertIn("does not stamp", messages)
        self.assertIn("'Example Rover/Setup.exe' is a .exe file", messages)

    def test_a_watched_vehicle_listing_is_the_watchers(self):
        (self.root / "vehicles" / "ExampleRover.toml").write_text(
            VEHICLE + '\n[releases]\ngithub = "someone/shares"\n', encoding="utf-8"
        )
        http = FakeHttp({})
        outcome = self.check(self.stamped(), http=http)
        self.assertEqual(outcome.outcome, check_release.REJECT)
        self.assertIn("watcher", outcome.messages[0])
        self.assertEqual(http.requested, [])

    def test_a_vehicle_listing_in_the_wrong_folder_is_refused_before_the_download(self):
        (self.root / "vehicles" / "ExampleRover.toml").unlink()
        self.listing("ExampleRover.toml", VEHICLE)
        http = FakeHttp({})
        outcome = self.check(self.stamped(), http=http)
        self.assertEqual(outcome.outcome, check_release.REJECT)
        self.assertIn("belongs under vehicles/", outcome.messages[0])
        self.assertEqual(http.requested, [])

    def test_the_folder_message_names_the_folder_the_game_sees(self):
        errors = []
        check_release.check_listing("examplerover", {"id": "ExampleRover", "type": "vehicle"}, errors)
        self.assertIn("the folder the game sees is install.folder", errors[0])
        errors = []
        check_release.check_listing("mod", {"id": "Mod", "type": "mod"}, errors)
        self.assertIn("the folder name is the identity the game sees", errors[0])


class Helpers(unittest.TestCase):
    def test_the_authored_root_is_the_argument_then_the_environment_then_the_sibling(self):
        self.assertEqual(check_release.authored_root("x"), Path("x"))
        with patch.dict(os.environ, {"CONTENT_INDEX": "y"}):
            self.assertEqual(check_release.authored_root(), Path("y"))
            self.assertEqual(check_release.authored_root("x"), Path("x"))
        without = {key: value for key, value in os.environ.items() if key != "CONTENT_INDEX"}
        with patch.dict(os.environ, without, clear=True):
            self.assertEqual(check_release.authored_root(), check_release.DEFAULT_AUTHORED)

    def test_a_vehicle_listing_is_found_under_vehicles(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "vehicles").mkdir()
            (root / "vehicles" / "ExampleRover.toml").write_text('id = "ExampleRover"\n')
            document, problem = check_release.authored_document(root, "ExampleRover")
            self.assertEqual((document, problem), ({"id": "ExampleRover"}, ""))
            (root / "vehicles" / "ExampleRover.toml").write_text("id = ")
            document, problem = check_release.authored_document(root, "ExampleRover")
            self.assertIsNone(document)
            self.assertIn("vehicles/ExampleRover.toml does not parse", problem)

    def test_the_facts_read_the_pre_release_flag_back_out_of_the_status(self):
        flag = lambda version, status: check_release.facts(
            {"version": version, "release_status": status}
        )["prerelease"]
        self.assertTrue(flag("1.0.0", "testing"))
        self.assertFalse(flag("1.0.0-rc.1", "testing"))
        self.assertFalse(flag("1.0.0", "stable"))
        self.assertFalse(flag("1.0.0", "dev"))

    def test_compare_names_every_disagreement(self):
        messages = check_release.compare(
            {"a": 1, "b": 2, "download": {"url": "x", "size": 1}, "listing": {"name": "A"}},
            {"a": 1, "c": 3, "download": {"url": "x", "size": 2}, "listing": {"name": "B"}},
        )
        self.assertEqual(len(messages), 4)
        self.assertTrue(any("'b'" in message for message in messages))
        self.assertTrue(any("'c'" in message for message in messages))
        self.assertTrue(any(message.startswith("download.size") for message in messages))
        self.assertTrue(any("listing" in message and "name" in message for message in messages))

    def test_compare_has_nothing_to_say_about_equal_documents(self):
        self.assertEqual(check_release.compare({"a": [1]}, {"a": [1]}), [])


if __name__ == "__main__":
    unittest.main()
