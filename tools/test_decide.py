#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the privileged half.
"""

import importlib.util
import json
import os
import tempfile
import textwrap
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

import decide

# Enough of content-index's ownership.py to exercise the decision without it.
STUB = '''
VERIFIED = "verified"
UNVERIFIED = "unverified"
COULD_NOT_EVALUATE = "could-not-evaluate"
MARKER_PATH = ".github/ksa-content-index.toml"
TOPIC = "ksa-index-{login}"


class Unavailable(Exception):
    pass


class Result:
    def __init__(self, state, reason, proof=None):
        self.state = state
        self.reason = reason
        self.proof = proof


def verify(document, login, author_id, api):
    return Result(VERIFIED, "", "stub")
'''


def stub_ownership(root):
    tools = Path(root) / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "ownership.py").write_text(textwrap.dedent(STUB), encoding="utf-8")
    return Path(root)


class LoadOwnership(unittest.TestCase):
    def test_a_missing_checkout_is_named(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(decide.Unavailable) as raised:
                decide.load_ownership(folder)
        self.assertIn("content-index", str(raised.exception))

    def test_a_stub_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            ownership = decide.load_ownership(stub_ownership(folder))
        self.assertEqual(ownership.VERIFIED, "verified")

    def test_the_real_ownership_module_carries_what_this_repository_uses(self):
        # The same two places load_ownership looks, so this runs wherever it can.
        # CI checks the authored half out and sets CONTENT_INDEX.
        root = Path(os.environ.get("CONTENT_INDEX") or decide.DEFAULT_AUTHORED)
        path = root / "tools" / "ownership.py"
        if not path.is_file():
            self.skipTest("content-index is not checked out next to this repository")
        # Loaded under its own name: another test has put the stub into
        # sys.modules as `ownership`, and load_ownership would hand that back.
        spec = importlib.util.spec_from_file_location("real_ownership_under_test", path)
        ownership = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ownership)
        for name in ("VERIFIED", "UNVERIFIED", "COULD_NOT_EVALUATE", "TOPIC", "MARKER_PATH"):
            self.assertTrue(hasattr(ownership, name), name)
        self.assertTrue(callable(ownership.verify))
        self.assertTrue(issubclass(ownership.Unavailable, Exception))
        self.assertTrue(callable(ownership.Result))


class Ownership(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = stub_ownership(self.directory.name)
        self.ownership = decide.load_ownership(self.root)


class AuthoredDocument(Ownership):
    def write(self, name, text):
        listings = self.root / "listings"
        listings.mkdir(parents=True, exist_ok=True)
        (listings / name).write_text(text, encoding="utf-8")

    def test_the_listing_comes_from_the_authored_half(self):
        self.write("Mod.toml", 'id = "Mod"\n[releases]\ngithub = "owner/repo"\n')
        document, problem = decide.authored_document(self.root, "Mod")
        self.assertEqual(problem, "")
        self.assertEqual(document["releases"]["github"], "owner/repo")

    def test_a_listing_that_is_not_there_has_no_release_host(self):
        document, problem = decide.authored_document(self.root, "Missing")
        self.assertIsNone(document)
        self.assertIn("not in content-index", problem)

    def test_a_listing_that_does_not_parse_is_named(self):
        self.write("Broken.toml", "id = ")
        document, problem = decide.authored_document(self.root, "Broken")
        self.assertIsNone(document)
        self.assertIn("does not parse", problem)


class Decide(Ownership):
    def verdict(self, outcome, **extra):
        return {"verdict": outcome, "checks": [], **extra}

    def result(self, state, reason=""):
        return self.ownership.Result(state, reason)

    def test_a_rejection_fails_the_status(self):
        decision = decide.decide(
            self.verdict("reject", checks=[{"name": "amendment", "outcome": "reject",
                                            "messages": ["it widens"]}]),
            True,
            self.ownership,
            self.result(self.ownership.VERIFIED),
        )
        self.assertEqual(decision.status, "failure")
        self.assertFalse(decision.auto_merge)
        self.assertIn("it widens", decision.comment)
        self.assertIn("Push a fix", decision.comment)

    def test_a_verdict_that_could_not_be_reached_is_an_error(self):
        decision = decide.decide(
            self.verdict("could-not-evaluate"), True, self.ownership,
            self.result(self.ownership.VERIFIED),
        )
        self.assertEqual(decision.status, "error")
        self.assertFalse(decision.auto_merge)
        self.assertIn("run the checks again", decision.comment)

    def test_a_verified_owner_gets_auto_merge(self):
        decision = decide.decide(
            self.verdict("pass"), True, self.ownership, self.result(self.ownership.VERIFIED)
        )
        self.assertEqual(decision.status, "success")
        self.assertTrue(decision.auto_merge)
        self.assertIn("merge automatically", decision.comment)
        self.assertIn("snapshot rebuild", decision.comment)

    def test_an_unverified_author_waits_for_a_steward(self):
        decision = decide.decide(
            self.verdict("pass"), True, self.ownership,
            self.result(self.ownership.UNVERIFIED, "no proof"),
        )
        self.assertEqual(decision.status, "success")
        self.assertFalse(decision.auto_merge)
        self.assertTrue(decision.needs_steward)
        self.assertIn("no proof", decision.comment)
        self.assertIn("ksa-index-<your-github-username>", decision.comment)

    def test_a_stewards_own_widening_is_refused(self):
        decision = decide.decide(
            self.verdict("pass", owner_only=True), True, self.ownership,
            self.result(self.ownership.UNVERIFIED, "no proof"),
        )
        self.assertEqual(decision.status, "failure")
        self.assertFalse(decision.auto_merge)
        self.assertFalse(decision.needs_steward)
        self.assertIn("no proof", decision.comment)
        self.assertIn("`Requested by the author: <link>`", decision.comment)

    def test_the_same_widening_on_the_owners_request_waits_for_a_steward(self):
        decision = decide.decide(
            self.verdict("pass", owner_only=True), True, self.ownership,
            self.result(self.ownership.UNVERIFIED, "no proof"),
            request="https://github.com/someone/Mod/issues/3",
        )
        self.assertEqual(decision.status, "success")
        self.assertFalse(decision.auto_merge)
        self.assertTrue(decision.needs_steward)
        self.assertIn("names a request of the owner", decision.comment)

    def test_a_widening_outside_the_candidate_shape_needs_a_request_too(self):
        refused = decide.decide(
            self.verdict("pass", owner_only=True, scope_reason="it touches tools/"), False,
            self.ownership, self.result(self.ownership.UNVERIFIED, "not checked"),
        )
        self.assertEqual(refused.status, "failure")
        self.assertNotIn("The ownership check reported", refused.comment)
        self.assertIn("ownership is not checked because it touches tools/", refused.comment)
        self.assertNotIn("neither comes from the verified owner", refused.comment)
        requested = decide.decide(
            self.verdict("pass", owner_only=True, scope_reason="it touches tools/"), False,
            self.ownership, self.result(self.ownership.UNVERIFIED, "not checked"),
            request="https://github.com/someone/Mod/issues/3",
        )
        self.assertTrue(requested.needs_steward)
        self.assertIn("it touches tools/", requested.comment)

    def test_a_widening_whose_ownership_could_not_be_checked_is_not_decided(self):
        decision = decide.decide(
            self.verdict("pass", owner_only=True), True, self.ownership,
            self.result(self.ownership.COULD_NOT_EVALUATE, "the API is down"),
        )
        self.assertEqual(decision.status, "error")
        self.assertFalse(decision.needs_steward)
        self.assertIn("the API is down", decision.comment)

    def test_a_widening_by_the_verified_owner_merges_itself(self):
        decision = decide.decide(
            self.verdict("pass", owner_only=True), True, self.ownership,
            self.result(self.ownership.VERIFIED),
        )
        self.assertTrue(decision.auto_merge)
        self.assertNotIn(decide.OWNER_ONLY, decision.comment)

    def test_ownership_that_could_not_be_checked_waits_for_a_steward(self):
        decision = decide.decide(
            self.verdict("pass"), True, self.ownership,
            self.result(self.ownership.COULD_NOT_EVALUATE, "the host is down"),
        )
        self.assertTrue(decision.needs_steward)
        self.assertIn("the host is down", decision.comment)

    def test_a_change_that_is_not_an_amendment_waits_for_a_steward(self):
        decision = decide.decide(
            self.verdict("pass", scope_reason="the change touches 2 listings"),
            False,
            self.ownership,
            self.result(self.ownership.VERIFIED),
        )
        self.assertTrue(decision.needs_steward)
        self.assertIn("2 listings", decision.comment)

    def test_a_passing_message_appears_in_the_comment_on_every_path(self):
        passing_check = {"name": "scope", "outcome": "pass", "messages": ["one document"]}
        cases = [
            ("reject", True, self.result(self.ownership.VERIFIED)),
            ("could-not-evaluate", True, self.result(self.ownership.VERIFIED)),
            ("pass", False, self.result(self.ownership.VERIFIED)),
            ("pass", True, self.result(self.ownership.VERIFIED)),
            ("pass", True, self.result(self.ownership.COULD_NOT_EVALUATE, "host down")),
            ("pass", True, self.result(self.ownership.UNVERIFIED, "no proof")),
        ]

        for outcome, candidate, result in cases:
            with self.subTest(outcome=outcome, candidate=candidate, ownership=result.state):
                verdict = self.verdict(outcome, checks=[passing_check])
                decision = decide.decide(verdict, candidate, self.ownership, result)
                self.assertIn("Notes:", decision.comment)
                self.assertIn("- `scope`: one document", decision.comment)

    def test_a_comment_without_messages_has_no_notes_section(self):
        decision = decide.decide(
            self.verdict("pass"), True, self.ownership, self.result(self.ownership.VERIFIED)
        )
        self.assertNotIn("Notes:", decision.comment)

    def test_the_status_description_fits_what_github_accepts(self):
        for outcome in ("pass", "reject", "could-not-evaluate"):
            decision = decide.decide(
                self.verdict(outcome), True, self.ownership,
                self.result(self.ownership.UNVERIFIED, "x"),
            )
            self.assertLessEqual(len(decision.description), 140)


class Agrees(unittest.TestCase):
    def test_a_verdict_naming_another_pull_request_is_not_acted_on(self):
        agrees, reason = decide._agrees({"pull_request": 2, "head_sha": "a"}, 1, "a")
        self.assertFalse(agrees)
        self.assertIn("2", reason)

    def test_a_verdict_naming_another_commit_is_not_acted_on(self):
        agrees, _ = decide._agrees({"pull_request": 1, "head_sha": "b"}, 1, "a")
        self.assertFalse(agrees)

    def test_a_passing_verdict_with_no_commit_is_not_acted_on(self):
        agrees, _ = decide._agrees({"pull_request": 1, "head_sha": None, "verdict": "pass"}, 1, "a")
        self.assertFalse(agrees)

    def test_a_failing_verdict_with_no_commit_still_reports(self):
        agrees, _ = decide._agrees(
            {"pull_request": 1, "head_sha": None, "verdict": "reject"}, 1, "a"
        )
        self.assertTrue(agrees)

    def test_a_matching_verdict_agrees(self):
        agrees, reason = decide._agrees({"pull_request": 1, "head_sha": "a"}, 1, "a")
        self.assertTrue(agrees)
        self.assertEqual(reason, "")


class NamedRequest(unittest.TestCase):
    LINK = "https://github.com/someone/Mod/issues/3#issuecomment-1"

    def test_the_line_names_the_link(self):
        for body in (
            f"Raises game_max.\n\nRequested by the author: {self.LINK}\n",
            f"Raises game_max.\r\nrequested by the author:  {self.LINK} \r\n",
            f"Requested by the author: {self.LINK}",
            f"Requested by the author: <{self.LINK}>",
        ):
            with self.subTest(body=body):
                self.assertEqual(decide.named_request(body), self.LINK)

    def test_anything_else_names_no_request(self):
        for body in (
            None,
            "",
            "Requested by the author: <link>",
            "Requested by the author: http://example.invalid/request",
            f"See {self.LINK}",
            f"Nobody was Requested by the author: {self.LINK}",
            f"Requested by the author: {self.LINK} and more",
            f"Requested by the author: <{self.LINK}",
            f"Requested by the author: <{self.LINK}> and more",
        ):
            with self.subTest(body=body):
                self.assertIsNone(decide.named_request(body))


class ReadVerdict(unittest.TestCase):
    def test_a_run_that_left_none_is_named(self):
        verdict, problem = decide.read_verdict(Path("no-such-verdict.json"))
        self.assertIsNone(verdict)
        self.assertIn("left no verdict", problem)

    def test_a_verdict_that_does_not_parse_is_named(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "verdict.json"
            path.write_text("{ not json", encoding="utf-8")
            verdict, problem = decide.read_verdict(path)
        self.assertIsNone(verdict)
        self.assertIn("could not be read", problem)

    def test_a_verdict_comes_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "verdict.json"
            path.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
            verdict, problem = decide.read_verdict(path)
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(problem, "")


class FakeApi:
    """Records what would be sent, and answers what the test set up."""

    def __init__(self, pulls=(), files=(), labels=(), comments=()):
        self.repository = "KSAModding/content-index-releases"
        self.token = "t"
        self.public_token = "t"
        self.dry_run = False
        self.log = lambda message: None
        self.unavailable = RuntimeError
        self.pulls = list(pulls)
        self.files = list(files)
        self.labels = list(labels)
        self.comments = list(comments)
        self.sent = []

    def get(self, path, **query):
        if path == "/pulls":
            head = query.get("head")
            return [pull for pull in self.pulls if
                    f"{pull['head']['repo']['full_name'].split('/')[0]}:{pull['head']['ref']}"
                    == head]
        if path.endswith("/files"):
            return self.files if query.get("page", 1) == 1 else []
        if path.endswith("/labels"):
            return [{"name": name} for name in self.labels]
        if path.endswith("/requested_reviewers"):
            return {"teams": []}
        if path.endswith("/comments"):
            return self.comments
        return None

    def send(self, method, path, payload, token=None):
        self.sent.append((method, path, payload))
        if method == "POST" and path.endswith("/comments"):
            self.comments.append({"id": len(self.comments) + 1, "body": payload["body"]})
        elif method == "PATCH" and path.startswith("/issues/comments/"):
            comment_id = int(path.rsplit("/", 1)[1])
            for comment in self.comments:
                if comment["id"] == comment_id:
                    comment["body"] = payload["body"]
                    break
        return {}

    def graphql(self, query, variables):
        self.sent.append(("graphql", query.strip().splitlines()[1].strip(), variables))
        return {}


class MissingLabelApi(FakeApi):
    """A repository that does not carry `missing` as a label yet."""

    def __init__(self, missing, **keywords):
        super().__init__(**keywords)
        self.missing = missing

    def send(self, method, path, payload, token=None):
        if self.missing and payload and payload.get("labels") == [self.missing]:
            self.missing = None
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        return super().send(method, path, payload, token)


class Label(unittest.TestCase):
    def test_the_steward_label_is_added_once(self):
        api = FakeApi()
        decide.add_steward_label(api, 5)
        self.assertEqual(api.sent, [("POST", "/issues/5/labels",
                                     {"labels": [decide.STEWARD_LABEL]})])

    def test_the_steward_label_is_not_added_twice(self):
        api = FakeApi(labels=[decide.STEWARD_LABEL])
        decide.add_steward_label(api, 5)
        self.assertEqual(api.sent, [])

    def test_the_steward_label_is_removed_on_the_way_to_a_merge(self):
        api = FakeApi(labels=[decide.STEWARD_LABEL])
        decide.remove_steward_label(api, 5)
        self.assertEqual(api.sent, [("DELETE", f"/issues/5/labels/{decide.STEWARD_LABEL}", None)])

    def test_the_shape_is_named(self):
        api = FakeApi()
        decide.sync_document_labels(api, 5, ["amendment"])
        self.assertEqual(api.sent, [("POST", "/issues/5/labels", {"labels": ["amendment"]})])

    def test_the_shape_is_not_named_twice(self):
        api = FakeApi(labels=["amendment"])
        decide.sync_document_labels(api, 5, ["amendment"])
        self.assertEqual(api.sent, [])

    def test_a_shape_the_change_no_longer_has_is_taken_off(self):
        api = FakeApi(labels=["release", "amendment"])
        decide.sync_document_labels(api, 5, ["amendment"])
        self.assertEqual(api.sent, [("DELETE", "/issues/5/labels/release", None)])

    def test_a_label_this_workflow_does_not_own_is_left_alone(self):
        api = FakeApi(labels=[decide.STEWARD_LABEL, "area:publishing"])
        decide.sync_document_labels(api, 5, [])
        self.assertEqual(api.sent, [])

    def test_a_label_the_repository_does_not_have_is_created_first(self):
        api = MissingLabelApi("release")
        decide.sync_document_labels(api, 5, ["release"])
        self.assertEqual(
            api.sent,
            [
                ("POST", "/labels", {
                    "name": "release",
                    "color": "0e8a16",
                    "description": "adds a release document",
                }),
                ("POST", "/issues/5/labels", {"labels": ["release"]}),
            ],
        )


class PullRequestFor(unittest.TestCase):
    def pull(self):
        return {
            "number": 7,
            "node_id": "PR_7",
            "head": {"sha": "abc", "ref": "amend", "repo": {"full_name": "someone/fork"}},
            "user": {"login": "someone", "id": 1},
        }

    def test_a_fork_branch_resolves_to_its_pull_request(self):
        api = FakeApi(pulls=[self.pull()])
        found = decide.pull_request_for(api, "pull_request", "someone/fork", "amend", "abc")
        self.assertEqual(found["number"], 7)

    def test_another_commit_on_the_same_branch_does_not_resolve(self):
        api = FakeApi(pulls=[self.pull()])
        self.assertIsNone(
            decide.pull_request_for(api, "pull_request", "someone/fork", "amend", "def")
        )

    def test_another_repository_with_the_same_branch_does_not_resolve(self):
        api = FakeApi(pulls=[self.pull()])
        self.assertIsNone(
            decide.pull_request_for(api, "pull_request", "other/fork", "amend", "abc")
        )

    def test_a_dispatched_run_belongs_to_no_pull_request(self):
        api = FakeApi(pulls=[self.pull()])
        self.assertIsNone(
            decide.pull_request_for(api, "workflow_dispatch", "someone/fork", "amend", "abc")
        )


class Act(Ownership):
    """The whole privileged step, with the API faked and the proofs stubbed."""

    def setUp(self):
        super().setUp()
        listings = self.root / "listings"
        listings.mkdir(parents=True, exist_ok=True)
        (listings / "Mod.toml").write_text(
            'id = "Mod"\n[releases]\ngithub = "someone/Mod"\n', encoding="utf-8"
        )
        self.verdict = self.root / "verdict.json"

    def arguments(self, **changes):
        import argparse

        fields = {
            "verdict": self.verdict,
            "authored": str(self.root),
            "event": "pull_request",
            "head_repository": "someone/fork",
            "head_branch": "amend",
            "head_sha": "abc",
            "repository": "KSAModding/content-index-releases",
            "run_url": "https://example.invalid/run",
            "dry_run": False,
        }
        fields.update(changes)
        return argparse.Namespace(**fields)

    def write_verdict(self, **changes):
        self.verdict.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verdict": "pass",
                    "auto_merge_candidate": True,
                    "pull_request": 7,
                    "head_sha": "abc",
                    "documents": ["releases/Mod/1.0.0.json"],
                    "scope_reason": "",
                    "checks": [],
                    **changes,
                }
            ),
            encoding="utf-8",
        )

    def api(self, body=None):
        return FakeApi(
            pulls=[
                {
                    "number": 7,
                    "node_id": "PR_7",
                    "head": {"sha": "abc", "ref": "amend",
                             "repo": {"full_name": "someone/fork"}},
                    "user": {"login": "someone", "id": 1},
                    "body": body,
                }
            ],
            files=[{"filename": "releases/Mod/1.0.0.json", "status": "modified"}],
        )

    def test_a_verified_amendment_is_armed_for_merge(self):
        self.write_verdict()
        api = self.api()
        self.assertEqual(decide.act(api, self.ownership, self.arguments()), 0)

        statuses = [payload for method, path, payload in api.sent
                    if method == "POST" and path.startswith("/statuses/")]
        self.assertEqual([status["state"] for status in statuses], ["pending", "success"])
        self.assertEqual({status["context"] for status in statuses}, {decide.STATUS_CONTEXT})
        self.assertTrue(any(method == "graphql" for method, _, _ in api.sent))
        self.assertTrue(any(path == "/issues/7/comments" for _, path, _ in api.sent))

    def test_a_widening_by_someone_else_merges_only_on_a_named_request(self):
        unverified = self.ownership.Result(self.ownership.UNVERIFIED, "no proof")
        for body, state in (
            ("Bounds Mod 1.0.0 at a newer build.", "failure"),
            (
                "Bounds Mod 1.0.0.\r\n"
                "Requested by the author: https://github.com/someone/Mod/issues/3\r\n",
                "success",
            ),
        ):
            with self.subTest(state=state):
                self.write_verdict(owner_only=True)
                api = self.api(body)
                with unittest.mock.patch.object(self.ownership, "verify", return_value=unverified):
                    self.assertEqual(decide.act(api, self.ownership, self.arguments()), 0)
                statuses = [payload["state"] for method, path, payload in api.sent
                            if method == "POST" and path.startswith("/statuses/")]
                self.assertEqual(statuses, [state])
                self.assertFalse(any(method == "graphql" for method, _, _ in api.sent))
                self.assertEqual(
                    decide.STEWARD_LABEL in {label for _, path, payload in api.sent
                                             if path == "/issues/7/labels"
                                             for label in payload["labels"]},
                    state == "success",
                )

    def test_a_verified_release_is_armed_for_merge(self):
        # One added file is the release pull request shape, and ownership binds
        # to the listing the folder names, the same as for an amendment.
        self.write_verdict(documents=["releases/Mod/2.0.0.json"])
        api = self.api()
        api.files = [{"filename": "releases/Mod/2.0.0.json", "status": "added"}]
        self.assertEqual(decide.act(api, self.ownership, self.arguments()), 0)

        statuses = [payload for method, path, payload in api.sent
                    if method == "POST" and path.startswith("/statuses/")]
        self.assertEqual([status["state"] for status in statuses], ["pending", "success"])
        self.assertTrue(any(method == "graphql" for method, _, _ in api.sent))

    def test_the_same_comment_is_updated_after_a_second_run(self):
        self.write_verdict()
        api = self.api()

        decide.act(api, self.ownership, self.arguments())
        self.write_verdict(
            checks=[{"name": "amendment", "outcome": "pass", "messages": ["still valid"]}]
        )
        decide.act(api, self.ownership, self.arguments())

        created = [path for method, path, _ in api.sent
                   if method == "POST" and path == "/issues/7/comments"]
        updated = [path for method, path, _ in api.sent
                   if method == "PATCH" and path.startswith("/issues/comments/")]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(updated), 1)
        self.assertEqual(len(api.comments), 1)
        self.assertIn("still valid", api.comments[0]["body"])

    def test_an_auto_merge_failure_keeps_notes_and_the_run_link(self):
        self.write_verdict(
            checks=[{"name": "amendment", "outcome": "pass", "messages": ["one amendment"]}]
        )
        api = self.api()
        api.graphql = lambda query, variables: {"errors": [{"message": "auto-merge is off"}]}

        decide.act(api, self.ownership, self.arguments())

        self.assertEqual(len(api.comments), 1)
        self.assertIn("auto-merge could not be armed", api.comments[0]["body"])
        self.assertIn("- `amendment`: one amendment", api.comments[0]["body"])
        self.assertIn("https://example.invalid/run", api.comments[0]["body"])

    def test_two_added_releases_wait_for_a_steward(self):
        self.write_verdict(documents=["releases/Mod/2.0.0.json", "releases/Mod/2.1.0.json"])
        api = self.api()
        api.files = [
            {"filename": "releases/Mod/2.0.0.json", "status": "added"},
            {"filename": "releases/Mod/2.1.0.json", "status": "added"},
        ]
        decide.act(api, self.ownership, self.arguments())
        comments = [payload for method, path, payload in api.sent if "comments" in path]
        self.assertTrue(any("exactly one" in payload["body"] for payload in comments))
        self.assertNotIn("graphql", [method for method, _, _ in api.sent])

    def test_a_verdict_naming_another_pull_request_is_not_acted_on(self):
        self.write_verdict(pull_request=99)
        api = self.api()
        api.comments = [
            {"id": 1, "body": f"{decide.COMMENT_MARKER}\nValidation passed."}
        ]
        # A race, but the status has to say so or the pull request sits pending.
        self.assertEqual(decide.act(api, self.ownership, self.arguments()), 0)
        statuses = [payload for method, path, payload in api.sent
                    if method == "POST" and path.startswith("/statuses/")]
        self.assertEqual([status["state"] for status in statuses], ["error"])
        self.assertEqual(len(api.comments), 1)
        self.assertIn("does not match this run", api.comments[0]["body"])
        self.assertNotIn("Validation passed", api.comments[0]["body"])
        self.assertNotIn("graphql", [method for method, _, _ in api.sent])

    def test_an_armed_auto_merge_is_taken_back_off_when_a_steward_is_needed(self):
        self.write_verdict(scope_reason="the change also touches tools/amend.py")
        api = self.api()
        api.pulls[0]["auto_merge"] = {"enabled_by": {"login": "someone"}}
        api.files = [
            {"filename": "releases/Mod/1.0.0.json", "status": "modified"},
            {"filename": "tools/amend.py", "status": "modified"},
        ]
        decide.act(api, self.ownership, self.arguments())
        mutations = [name for method, name, _ in api.sent if method == "graphql"]
        self.assertTrue(any("disablePullRequestAutoMerge" in name for name in mutations))

    def test_nothing_is_disarmed_when_auto_merge_was_never_armed(self):
        self.write_verdict(scope_reason="the change also touches tools/amend.py")
        api = self.api()
        api.files = [
            {"filename": "releases/Mod/1.0.0.json", "status": "modified"},
            {"filename": "tools/amend.py", "status": "modified"},
        ]
        decide.act(api, self.ownership, self.arguments())
        self.assertNotIn("graphql", [method for method, _, _ in api.sent])

    def test_a_run_that_left_no_verdict_reports_that(self):
        api = self.api()
        self.assertEqual(decide.act(api, self.ownership, self.arguments()), 0)
        statuses = [payload for method, path, payload in api.sent
                    if method == "POST" and path.startswith("/statuses/")]
        self.assertEqual([status["state"] for status in statuses], ["error"])

    def test_a_listing_that_is_not_in_the_authored_half_waits_for_a_steward(self):
        self.write_verdict()
        (self.root / "listings" / "Mod.toml").unlink()
        api = self.api()
        decide.act(api, self.ownership, self.arguments())
        comments = [payload for method, path, payload in api.sent if "comments" in path]
        self.assertTrue(any("not in content-index" in payload["body"] for payload in comments))

    def labelled(self, api):
        return [payload["labels"][0] for method, path, payload in api.sent
                if method == "POST" and path == "/issues/7/labels"]

    def test_an_amendment_reads_as_an_amendment(self):
        self.write_verdict()
        api = self.api()
        decide.act(api, self.ownership, self.arguments())
        self.assertEqual(self.labelled(api), ["amendment"])

    def test_an_added_release_reads_as_a_release(self):
        self.write_verdict(documents=["releases/Mod/2.0.0.json"])
        api = self.api()
        api.files = [{"filename": "releases/Mod/2.0.0.json", "status": "added"}]
        decide.act(api, self.ownership, self.arguments())
        self.assertEqual(self.labelled(api), ["release"])

    def test_a_wide_change_carries_the_shape_next_to_the_steward_label(self):
        self.write_verdict(scope_reason="the change also touches tools/amend.py")
        api = self.api()
        api.files = [
            {"filename": "releases/Mod/1.0.0.json", "status": "modified"},
            {"filename": "tools/amend.py", "status": "modified"},
        ]
        decide.act(api, self.ownership, self.arguments())
        self.assertEqual(self.labelled(api), ["amendment", decide.STEWARD_LABEL])

    def test_a_change_that_holds_both_shapes_carries_both(self):
        self.write_verdict(
            scope_reason="the change adds releases/Mod/2.0.0.json next to an amendment"
        )
        api = self.api()
        api.files = [
            {"filename": "releases/Mod/2.0.0.json", "status": "added"},
            {"filename": "releases/Mod/1.0.0.json", "status": "modified"},
        ]
        decide.act(api, self.ownership, self.arguments())
        self.assertEqual(self.labelled(api), ["release", "amendment", decide.STEWARD_LABEL])

    def test_a_change_that_touches_no_release_file_carries_no_shape(self):
        self.write_verdict(scope_reason="the change touches no release file")
        api = self.api()
        api.files = [{"filename": "tools/amend.py", "status": "modified"}]
        decide.act(api, self.ownership, self.arguments())
        self.assertEqual(self.labelled(api), [decide.STEWARD_LABEL])

    def test_a_run_that_belongs_to_no_pull_request_does_nothing(self):
        self.write_verdict()
        api = self.api()
        self.assertEqual(
            decide.act(api, self.ownership, self.arguments(head_sha="other")), 0
        )
        self.assertEqual(api.sent, [])


class ChangedPaths(unittest.TestCase):
    def test_the_status_travels_with_the_path(self):
        api = FakeApi(files=[{"filename": "releases/Mod/1.0.0.json", "status": "modified"}])
        changes = decide.changed_paths(api, 7)
        self.assertEqual(changes[0].path, "releases/Mod/1.0.0.json")
        self.assertEqual(changes[0].status, "modified")

    def test_a_missing_status_is_read_as_modified(self):
        api = FakeApi(files=[{"filename": "releases/Mod/1.0.0.json"}])
        self.assertEqual(decide.changed_paths(api, 7)[0].status, "modified")


if __name__ == "__main__":
    unittest.main()
