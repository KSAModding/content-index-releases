#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the amendment invariant.
"""

import copy
import json
import unittest

import check_amendment
from check_amendment import check, check_document, precedence

PATH = "releases/AdvancedFlightComputer/0.7.2.json"

# The shape the stamper writes, trimmed to what the invariant reads.
BASE = {
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
        "url": "https://example.invalid/AdvancedFlightComputer.zip",
        "sha256": "D9106AA572BD2DD6CAF046990B75D3F17F16F4CC178288B7983A4B98AEE924A6",
        "size": 129504,
        "content_type": "application/zip",
    },
    "install_size": 327913,
    "install": {"root": "AdvancedFlightComputer", "derived": True},
    "loader": {"id": "StarMap", "min": "0.4.5", "source": "authored"},
    "dependencies": [
        {"id": "KittenExtensions", "kind": "optional", "source": "derived"},
    ],
    "changelog": "https://example.invalid/tag/v0.7.2",
    "listing": {"name": "Advanced Flight Computer", "authors": ["Maxi"]},
}


def head(**changes):
    document = copy.deepcopy(BASE)
    document.update(changes)
    return document


def errors_for(document, path=PATH, base=None):
    found = []
    check_document(path, BASE if base is None else base, document, found)
    return found


class Precedence(unittest.TestCase):
    def test_a_release_outranks_its_own_pre_release(self):
        self.assertGreater(precedence("1.0.0"), precedence("1.0.0-rc.1"))

    def test_numeric_identifiers_compare_numerically(self):
        self.assertGreater(precedence("1.0.0-beta.10"), precedence("1.0.0-beta.9"))

    def test_an_alphanumeric_identifier_outranks_a_numeric_one(self):
        self.assertGreater(precedence("1.0.0-alpha"), precedence("1.0.0-1"))

    def test_a_longer_pre_release_outranks_its_own_prefix(self):
        self.assertGreater(precedence("1.0.0-beta.1"), precedence("1.0.0-beta"))

    def test_build_metadata_does_not_order(self):
        self.assertEqual(precedence("1.0.0+a"), precedence("1.0.0+b"))

    def test_something_that_is_not_a_version_raises(self):
        with self.assertRaises(ValueError):
            precedence("not a version")


class NothingChanged(unittest.TestCase):
    def test_the_published_file_passes_against_itself(self):
        self.assertEqual(errors_for(head()), [])


class Immutable(unittest.TestCase):
    def test_the_version_never_changes(self):
        self.assertTrue(errors_for(head(version="0.7.4")))

    def test_the_download_never_changes(self):
        document = head()
        document["download"]["url"] = "https://example.invalid/other.zip"
        self.assertTrue(any("'download'" in message for message in errors_for(document)))

    def test_a_mirror_does_not_arrive_by_pull_request(self):
        document = head()
        document["download"]["mirrors"] = ["https://example.invalid/mirror.zip"]
        self.assertTrue(any("'download'" in message for message in errors_for(document)))

    def test_a_mirror_the_watcher_appended_says_to_rebase(self):
        # The watcher writes mirrors straight to the default branch, so a branch cut before that reads as the author changing the download.
        base = copy.deepcopy(BASE)
        base["download"]["mirrors"] = ["https://example.invalid/mirror.zip"]
        errors = errors_for(head(yanked=True), base=base)
        self.assertTrue(any("rebase" in message for message in errors))

    def test_the_install_data_never_changes(self):
        document = head()
        document["install"]["root"] = "Elsewhere"
        self.assertTrue(any("'install'" in message for message in errors_for(document)))

    def test_the_frozen_listing_block_never_changes(self):
        document = head()
        document["listing"]["name"] = "Something Else"
        self.assertTrue(any("'listing'" in message for message in errors_for(document)))

    def test_the_release_status_never_changes(self):
        self.assertTrue(errors_for(head(release_status="testing")))

    def test_os_is_not_in_the_amendment_class(self):
        document = head()
        document["os"] = ["windows"]
        self.assertTrue(any("'os'" in message for message in errors_for(document)))

    def test_a_key_a_release_file_does_not_have_is_rejected(self):
        document = head()
        document["downloads"] = 12
        self.assertTrue(any("downloads" in message for message in errors_for(document)))


class PathRules(unittest.TestCase):
    def test_the_folder_has_to_name_the_id(self):
        errors = errors_for(head(), path="releases/SomethingElse/0.7.2.json")
        self.assertTrue(any("folder says" in message for message in errors))

    def test_the_file_name_has_to_name_the_version(self):
        errors = errors_for(head(), path="releases/AdvancedFlightComputer/9.9.9.json")
        self.assertTrue(any("file name says" in message for message in errors))

    def test_an_upper_case_suffix_is_rejected(self):
        errors = errors_for(head(), path="releases/AdvancedFlightComputer/0.7.2.JSON")
        self.assertTrue(any("lowercase .json" in message for message in errors))

    def test_a_file_outside_releases_is_not_a_release_file(self):
        errors = errors_for(head(), path="tools/evil.json")
        self.assertTrue(any("not a release file" in message for message in errors))

    def test_a_traversing_folder_is_not_a_content_id(self):
        errors = errors_for(head(), path="releases/../0.7.2.json")
        self.assertTrue(errors)


class GameBounds(unittest.TestCase):
    def test_a_game_max_can_be_added(self):
        # The amendment PR #27 made by hand.
        document = head(game_max="2026.8.19.5261", game_max_revision=5261)
        self.assertEqual(errors_for(document), [])

    def test_a_game_max_can_be_lowered(self):
        older = {"game_min": "2026.8.3.5117", "game_min_revision": 5117}
        base = head(game_max="2026.8.19.5261", game_max_revision=5261, **older)
        document = head(game_max="2026.8.5.5168", game_max_revision=5168, **older)
        self.assertEqual(errors_for(document, base=base), [])

    def test_a_game_max_cannot_be_raised(self):
        base = head(game_max="2026.8.5.5168", game_max_revision=5168)
        document = head(game_max="2026.8.19.5261", game_max_revision=5261)
        self.assertTrue(any("rises" in message for message in errors_for(document, base=base)))

    def test_a_game_max_cannot_be_removed(self):
        base = head(game_max="2026.8.19.5261", game_max_revision=5261)
        self.assertTrue(
            any("removed" in message for message in errors_for(head(), base=base))
        )

    def test_a_game_min_can_be_raised(self):
        document = head(game_min="2026.8.22.5348", game_min_revision=5348)
        self.assertEqual(errors_for(document), [])

    def test_a_game_min_cannot_be_lowered(self):
        document = head(game_min="2026.8.5.5168", game_min_revision=5168)
        self.assertTrue(any("falls" in message for message in errors_for(document)))

    def test_the_display_string_has_to_agree_with_the_revision(self):
        document = head(game_max="2026.8.22.5348", game_max_revision=5261)
        self.assertTrue(any("revision 5348" in message for message in errors_for(document)))

    def test_a_bound_needs_both_halves(self):
        document = head(game_max="2026.8.19.5261")
        self.assertTrue(any("together" in message for message in errors_for(document)))

    def test_a_month_is_not_a_stamped_bound(self):
        document = head(game_max="2026.8", game_max_revision=5261)
        self.assertTrue(
            any("not a game version string" in message for message in errors_for(document))
        )

    def test_a_game_max_below_the_game_min_is_an_empty_range(self):
        document = head(game_max="2026.8.5.5168", game_max_revision=5168)
        self.assertTrue(any("empty" in message for message in errors_for(document)))

    def test_a_leading_v_is_not_how_the_game_shows_a_version(self):
        document = head(game_max="v2026.8.22.5348", game_max_revision=5348)
        self.assertTrue(any("leading v" in message for message in errors_for(document)))

    def test_a_published_file_with_no_lower_bound_cannot_be_compared(self):
        base = copy.deepcopy(BASE)
        del base["game_min_revision"]
        errors = errors_for(head(), base=base)
        self.assertTrue(any("cannot be compared" in message for message in errors))


class Yank(unittest.TestCase):
    def test_a_release_can_be_yanked(self):
        self.assertEqual(errors_for(head(yanked=True)), [])

    def test_a_yank_can_carry_a_reason(self):
        document = head(yanked=True, yanked_reason="It deletes the save folder.")
        self.assertEqual(errors_for(document), [])

    def test_a_release_cannot_be_un_yanked(self):
        base = head(yanked=True)
        self.assertTrue(
            any("un-yanked" in message for message in errors_for(head(), base=base))
        )

    def test_yanked_false_is_not_a_stamped_value(self):
        self.assertTrue(errors_for(head(yanked=False)))

    def test_a_reason_says_nothing_without_the_yank(self):
        document = head(yanked_reason="It deletes the save folder.")
        self.assertTrue(any("without yanked" in message for message in errors_for(document)))

    def test_an_empty_reason_is_rejected(self):
        self.assertTrue(errors_for(head(yanked=True, yanked_reason="   ")))


class Loader(unittest.TestCase):
    def loader(self, **changes):
        document = head()
        document["loader"] = {**BASE["loader"], **changes}
        return document

    def test_a_loader_min_can_be_raised(self):
        self.assertEqual(errors_for(self.loader(min="0.4.6")), [])

    def test_a_loader_min_cannot_be_lowered(self):
        self.assertTrue(any("lowers" in message for message in errors_for(self.loader(min="0.4.4"))))

    def test_a_loader_max_can_be_added(self):
        self.assertEqual(errors_for(self.loader(max="0.4.6")), [])

    def test_a_loader_max_cannot_be_raised(self):
        base = self.loader(max="0.4.6")
        document = self.loader(max="0.5.0")
        self.assertTrue(any("raises" in message for message in errors_for(document, base=base)))

    def test_a_loader_max_cannot_be_removed(self):
        base = self.loader(max="0.4.6")
        self.assertTrue(
            any("removes its max" in message for message in errors_for(head(), base=base))
        )

    def test_the_loader_cannot_be_repointed(self):
        self.assertTrue(errors_for(self.loader(id="SomeOtherLoader")))

    def test_the_loader_cannot_be_removed(self):
        document = head()
        del document["loader"]
        self.assertTrue(any("removed" in message for message in errors_for(document)))

    def test_a_loader_cannot_be_added_where_there_was_none(self):
        base = copy.deepcopy(BASE)
        del base["loader"]
        document = head()
        self.assertTrue(
            any("not a loader" in message for message in errors_for(document, base=base))
        )

    def test_a_max_below_the_min_is_rejected(self):
        self.assertTrue(any("below min" in message for message in errors_for(self.loader(max="0.4.4"))))

    def test_a_key_a_loader_does_not_have_is_rejected(self):
        self.assertTrue(errors_for(self.loader(kind="required")))

    def test_a_bound_that_is_not_normalized_is_rejected(self):
        self.assertTrue(
            any("not normalized" in message for message in errors_for(self.loader(min="v0.4.6")))
        )


class Dependencies(unittest.TestCase):
    def with_dependencies(self, entries):
        return head(dependencies=entries)

    def test_a_bound_can_be_added_to_a_derived_entry(self):
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "min": "0.4.0", "source": "authored"}]
        )
        self.assertEqual(errors_for(document), [])

    def test_a_derived_entry_stays_derived_when_it_gains_nothing(self):
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "source": "authored"}]
        )
        self.assertTrue(any("changes source" in message for message in errors_for(document)))

    def test_an_authored_entry_cannot_become_derived(self):
        base = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "min": "0.4.0", "source": "authored"}]
        )
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "min": "0.5.0", "source": "derived"}]
        )
        self.assertTrue(
            any("changes source" in message for message in errors_for(document, base=base))
        )

    def test_an_entry_that_was_missing_can_be_added(self):
        document = self.with_dependencies(
            BASE["dependencies"] + [{"id": "SomeConflict", "kind": "conflict", "source": "authored"}]
        )
        self.assertEqual(errors_for(document), [])

    def test_an_added_entry_is_authored(self):
        document = self.with_dependencies(
            BASE["dependencies"] + [{"id": "Other", "kind": "required", "source": "derived"}]
        )
        self.assertTrue(any("added with source" in message for message in errors_for(document)))

    def test_an_entry_cannot_be_removed(self):
        self.assertTrue(
            any("is removed" in message for message in errors_for(self.with_dependencies([])))
        )

    def test_an_entry_cannot_change_kind(self):
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "required", "source": "derived"}]
        )
        self.assertTrue(any("changes kind" in message for message in errors_for(document)))

    def test_an_entry_cannot_be_renamed(self):
        document = self.with_dependencies(
            [{"id": "KittenExtensionsNG", "kind": "optional", "source": "derived"}]
        )
        # A different id is a different entry, so the old one reads as removed.
        self.assertTrue(errors_for(document))

    def test_an_id_matches_case_insensitively(self):
        document = self.with_dependencies(
            [{"id": "kittenextensions", "kind": "optional", "source": "derived"}]
        )
        self.assertTrue(any("renamed" in message for message in errors_for(document)))

    def test_the_same_entry_twice_is_rejected(self):
        document = self.with_dependencies(BASE["dependencies"] + BASE["dependencies"])
        self.assertTrue(any("more than once" in message for message in errors_for(document)))

    def test_a_max_can_be_lowered(self):
        base = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "max": "0.5.0", "source": "authored"}]
        )
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "max": "0.4.0", "source": "authored"}]
        )
        self.assertEqual(errors_for(document, base=base), [])

    def test_a_min_cannot_be_lowered(self):
        base = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "min": "0.5.0", "source": "authored"}]
        )
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "min": "0.4.0", "source": "authored"}]
        )
        self.assertTrue(any("lowers" in message for message in errors_for(document, base=base)))

    def test_a_kind_the_format_does_not_have_is_rejected(self):
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "wants", "source": "derived"}]
        )
        self.assertTrue(any("not a dependency kind" in message for message in errors_for(document)))

    def test_an_entry_with_no_id_is_rejected(self):
        document = self.with_dependencies([{"kind": "required", "source": "authored"}])
        self.assertTrue(errors_for(document))

    def test_an_added_entry_has_its_bounds_parsed(self):
        # An added entry is never compared against a published one, so without its own check the bound reaches a release file unparsed.
        document = self.with_dependencies(
            BASE["dependencies"]
            + [{"id": "BadMod", "kind": "conflict", "max": "not-a-version",
                "source": "authored"}]
        )
        self.assertTrue(any("SemVer" in message for message in errors_for(document)))

    def test_an_added_entry_rejects_a_bound_that_is_not_normalized(self):
        document = self.with_dependencies(
            BASE["dependencies"]
            + [{"id": "BadMod", "kind": "conflict", "min": "v1.2.0", "source": "authored"}]
        )
        self.assertTrue(any("not normalized" in message for message in errors_for(document)))

    def test_an_added_any_of_entry_has_its_member_bounds_parsed(self):
        document = self.with_dependencies(
            BASE["dependencies"]
            + [{"any_of": [{"id": "OneRouter", "min": "nope"}], "kind": "required",
                "source": "authored"}]
        )
        self.assertTrue(any("SemVer" in message for message in errors_for(document)))

    def test_a_derived_entry_cannot_carry_a_bound(self):
        # [[StarMap.ModDependencies]] carries no versions, so no derivation ever produced one.
        document = self.with_dependencies(
            [{"id": "KittenExtensions", "kind": "optional", "min": "0.4.0", "source": "derived"}]
        )
        self.assertTrue(
            any("no derivation produces" in message for message in errors_for(document))
        )


class AnyOf(unittest.TestCase):
    ENTRY = {
        "any_of": [{"id": "OpenALRouter", "min": "2.0.0"}, {"id": "ClassicALRouter"}],
        "kind": "required",
        "source": "authored",
    }

    def base(self):
        return head(dependencies=[copy.deepcopy(self.ENTRY)])

    def test_an_alternative_can_gain_a_bound(self):
        document = head(dependencies=[copy.deepcopy(self.ENTRY)])
        document["dependencies"][0]["any_of"][1]["min"] = "1.1.0"
        self.assertEqual(errors_for(document, base=self.base()), [])

    def test_an_alternative_cannot_be_dropped(self):
        document = head(dependencies=[copy.deepcopy(self.ENTRY)])
        document["dependencies"][0]["any_of"].pop()
        errors = errors_for(document, base=self.base())
        self.assertTrue(errors)

    def test_an_alternative_cannot_be_added(self):
        document = head(dependencies=[copy.deepcopy(self.ENTRY)])
        document["dependencies"][0]["any_of"].append({"id": "ThirdRouter"})
        self.assertTrue(errors_for(document, base=self.base()))

    def test_an_alternative_min_cannot_be_lowered(self):
        document = head(dependencies=[copy.deepcopy(self.ENTRY)])
        document["dependencies"][0]["any_of"][0]["min"] = "1.0.0"
        self.assertTrue(any("lowers" in message for message in errors_for(document, base=self.base())))

    def test_an_entry_carrying_both_id_and_any_of_is_rejected(self):
        entry = copy.deepcopy(self.ENTRY)
        entry["id"] = "Something"
        document = head(dependencies=[entry])
        self.assertTrue(errors_for(document, base=self.base()))


class NewAndDeleted(unittest.TestCase):
    def test_a_new_release_file_is_not_an_amendment(self):
        # Not a rejection: there is no published release to measure against, and the scope rule already routes it to a steward.
        results = check([(PATH, None, head())])
        self.assertEqual(results[PATH].outcome, "pass")
        self.assertTrue(
            any("new release file" in message for message in results[PATH].messages)
        )

    def test_a_widening_next_to_a_new_file_is_still_rejected(self):
        other = "releases/AdvancedFlightComputer/0.7.1.json"
        results = check(
            [
                (PATH, None, head()),
                (other, BASE, head(version="9.9.9")),
            ]
        )
        self.assertEqual(results[PATH].outcome, "pass")
        self.assertEqual(results[other].outcome, "reject")

    def test_a_base_that_is_not_an_object_is_named(self):
        errors = []
        check_document(PATH, "not a document", head(), errors)
        self.assertTrue(any("published version" in message for message in errors))

    def test_a_deleted_release_file_is_not_an_amendment(self):
        results = check([(PATH, BASE, None)])
        self.assertEqual(results[PATH].outcome, "reject")
        self.assertTrue(any("deleted" in message for message in results[PATH].messages))

    def test_a_head_that_is_not_an_object_is_rejected(self):
        errors = []
        check_document(PATH, BASE, [1, 2, 3], errors)
        self.assertTrue(errors)


class Batch(unittest.TestCase):
    def test_every_file_gets_its_own_outcome(self):
        other = "releases/AdvancedFlightComputer/0.7.1.json"
        results = check(
            [
                (PATH, BASE, head(yanked=True)),
                (other, BASE, head(version="9.9.9")),
            ]
        )
        self.assertEqual(results[PATH].outcome, "pass")
        self.assertEqual(results[other].outcome, "reject")

    def test_the_real_amendment_round_trips_through_json(self):
        # The check reads what a pull request actually carries, which is text.
        document = json.loads(
            json.dumps(head(game_max="2026.8.19.5261", game_max_revision=5261))
        )
        self.assertEqual(errors_for(document), [])


class Outcome(unittest.TestCase):
    def test_the_outcome_carries_its_messages(self):
        outcome = check_amendment.Outcome("reject", ["something"])
        self.assertEqual(outcome.outcome, "reject")
        self.assertEqual(outcome.messages, ["something"])


if __name__ == "__main__":
    unittest.main()
