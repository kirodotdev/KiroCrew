"""Tests for the timeline comment surface: replies and pending batches.

Three layers, none of which spawns a CLI:
  * the client primitives (GitHub, GitLab, Azure DevOps) with their transport
    monkeypatched, so the paths and payloads each forge receives are pinned;
  * GitLab's native batch, which publishes a merge-request batch as ONE review and
    must never publish a pending review the user started elsewhere;
  * the ``POST /comments`` route: validation, the stop-at-first-failure contract,
    and cache invalidation.
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import azure_client as az
from kiro_crew.apps.builtins.issue_radar.backend import comment_batch
from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
from kiro_crew.apps.builtins.issue_radar.backend import routes
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderPermissionError,
)

DISCUSSION = "6a9c1750b37d513a43987b574953fceb50b03ce7"
MR_BASE = "projects/g%2Fp/merge_requests/7"


class TestPostSequentially(unittest.TestCase):
    def test_posts_in_order(self):
        seen = []

        def post_one(item):
            seen.append(item["body"])
            return {"id": len(seen), "url": None}

        out = comment_batch.post_sequentially([{"body": "a"}, {"body": "b"}], post_one)
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(
            out["posted"], [{"index": 0, "id": 1, "url": None}, {"index": 1, "id": 2, "url": None}]
        )
        self.assertIsNone(out["failed"])

    def test_a_first_item_failure_raises_because_nothing_posted(self):
        def post_one(item):
            raise ProviderPermissionError("locked")

        with self.assertRaises(ProviderPermissionError):
            comment_batch.post_sequentially([{"body": "a"}, {"body": "b"}], post_one)

    def test_a_later_failure_stops_and_reports_what_posted(self):
        seen = []

        def post_one(item):
            if item["body"] == "b":
                raise ProviderCliError("boom")
            seen.append(item["body"])
            return {"id": 9}

        out = comment_batch.post_sequentially(
            [{"body": "a"}, {"body": "b"}, {"body": "c"}], post_one
        )
        # "c" is not attempted: the items are prose meant to be read in order.
        self.assertEqual(seen, ["a"])
        self.assertEqual([p["index"] for p in out["posted"]], [0])
        self.assertEqual(out["failed"], {"index": 1, "error": "boom"})
        # An error with no status may have reached the forge, so "b" is unknown.
        self.assertEqual(out["unknown"], [1])

    def test_a_first_item_timeout_is_unknown_not_unposted(self):
        """A timeout can follow a write the forge accepted, so raising (which the
        client reads as "nothing posted") would invite a duplicate."""

        def post_one(item):
            raise ProviderCliError("`gh` timed out after 30s")

        out = comment_batch.post_sequentially([{"body": "a"}, {"body": "b"}], post_one)
        self.assertEqual(out["posted"], [])
        self.assertEqual(out["unknown"], [0])
        self.assertEqual(out["failed"]["index"], 0)

    def test_a_client_error_status_is_a_definite_refusal(self):
        def post_one(item):
            raise ProviderCliError("gh api POST x failed (exit 1): HTTP 422: Validation Failed")

        with self.assertRaises(ProviderCliError):
            comment_batch.post_sequentially([{"body": "a"}], post_one)

    def test_a_later_refusal_is_not_unknown(self):
        def post_one(item):
            if item["body"] == "b":
                raise ProviderPermissionError("locked")
            return {"id": 1}

        out = comment_batch.post_sequentially([{"body": "a"}, {"body": "b"}], post_one)
        self.assertEqual(out["unknown"], [])
        self.assertEqual(out["failed"]["index"], 1)

    def test_a_server_error_status_may_have_been_delivered(self):
        self.assertTrue(comment_batch.delivery_unknown(ProviderCliError("failed: HTTP 502")))
        self.assertFalse(comment_batch.delivery_unknown(ProviderCliError("failed: HTTP 404")))


class TestThreadIdNormalization(unittest.TestCase):
    def test_ids_become_strings_and_junk_becomes_none(self):
        self.assertEqual(comment_batch.thread_id_or_none(12), "12")
        self.assertEqual(comment_batch.thread_id_or_none(DISCUSSION), DISCUSSION)
        self.assertIsNone(comment_batch.thread_id_or_none(None))
        self.assertIsNone(comment_batch.thread_id_or_none(""))
        self.assertIsNone(comment_batch.thread_id_or_none("../notes"))


class TestGithubReplies(unittest.TestCase):
    def test_reply_posts_to_the_root_comment_replies_endpoint(self):
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"id": 5, "html_url": "u", "created_at": "t"}
        ) as m:
            out = gh.reply_to_comment_thread("o", "r", 7, "pull", "123", "  agreed  ")
        self.assertEqual(m.call_args[0][0], "POST")
        self.assertEqual(m.call_args[0][1], "repos/o/r/pulls/7/comments/123/replies")
        self.assertEqual(m.call_args[0][2], {"body": "agreed"})
        self.assertEqual(out["id"], 5)

    def test_an_issue_reply_is_refused_not_flattened(self):
        """GitHub issue comments do not thread; quietly posting a top-level comment
        would not be what the caller asked for."""
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.reply_to_comment_thread("o", "r", 7, "issue", "123", "x")
            m.assert_not_called()

    def test_a_non_numeric_thread_is_refused_before_any_call(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.reply_to_comment_thread("o", "r", 7, "pull", DISCUSSION, "x")
            m.assert_not_called()

    def test_batch_routes_each_item_to_its_endpoint(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={"id": 1}) as m:
            out = gh.post_comment_batch(
                "o",
                "r",
                7,
                "pull",
                [{"body": "top", "thread_id": None}, {"body": "reply", "thread_id": "44"}],
            )
        paths = [c[0][1] for c in m.call_args_list]
        self.assertEqual(
            paths, ["repos/o/r/issues/7/comments", "repos/o/r/pulls/7/comments/44/replies"]
        )
        self.assertEqual(len(out["posted"]), 2)

    def test_review_comment_rows_carry_the_thread_root(self):
        """The jq projection names the ROOT id for every comment in a thread."""
        self.assertIn("in_reply_to_id", gh._PR_REVIEW_COMMENT_JQ)
        self.assertIn("thread_id", gh._PR_REVIEW_COMMENT_JQ)


class TestGitlabReplies(unittest.TestCase):
    def test_reply_posts_into_the_discussion(self):
        with mock.patch.object(gl, "_glab_api", return_value={"id": 3}) as m:
            gl.reply_to_comment_thread("g", "p", 7, "issue", DISCUSSION, "hi", host="gitlab.com")
        self.assertEqual(
            m.call_args[0][0], f"projects/g%2Fp/issues/7/discussions/{DISCUSSION}/notes"
        )
        self.assertEqual(m.call_args.kwargs["body"], {"body": "hi"})

    def test_the_collection_follows_the_target(self):
        with mock.patch.object(gl, "_glab_api", return_value={"id": 3}) as m:
            gl.reply_to_comment_thread("g", "p", 7, "pull", DISCUSSION, "hi", host="gitlab.com")
        self.assertTrue(m.call_args[0][0].startswith(f"{MR_BASE}/discussions/"))

    def test_a_non_hex_discussion_is_refused(self):
        with mock.patch.object(gl, "_glab_api") as m:
            with self.assertRaises(ProviderCliError):
                gl.reply_to_comment_thread("g", "p", 7, "pull", "123/../x", "hi", host="gitlab.com")
            m.assert_not_called()

    def test_a_single_mr_item_is_posted_directly(self):
        with mock.patch.object(gl, "_glab_api", return_value={"id": 3}) as m:
            out = gl.post_comment_batch(
                "g", "p", 7, "pull", [{"body": "one", "thread_id": None}], host="gitlab.com"
            )
        self.assertEqual([c[0][0] for c in m.call_args_list], [f"{MR_BASE}/notes"])
        self.assertEqual([p["index"] for p in out["posted"]], [0])

    def test_an_issue_batch_is_sequential(self):
        with mock.patch.object(gl, "_glab_api", return_value={"id": 3}) as m:
            gl.post_comment_batch(
                "g",
                "p",
                7,
                "issue",
                [{"body": "a", "thread_id": None}, {"body": "b", "thread_id": DISCUSSION}],
                host="gitlab.com",
            )
        self.assertEqual(
            [c[0][0] for c in m.call_args_list],
            [
                "projects/g%2Fp/issues/7/notes",
                f"projects/g%2Fp/issues/7/discussions/{DISCUSSION}/notes",
            ],
        )


class _FakeDrafts:
    """A stand-in for GitLab's draft-notes API, recording every call."""

    def __init__(
        self,
        preexisting=(),
        fail_create_at=None,
        fail_publish=False,
        intruder=None,
        fail_publish_after=None,
        fail_list_after_publish=False,
    ):
        self.drafts: dict[int, dict] = {i: {"id": i} for i in preexisting}
        self.next_id = 100
        self.calls: list[tuple[str, str]] = []
        self.created = 0
        self.fail_create_at = fail_create_at
        self.fail_publish = fail_publish
        # A draft that appears mid-batch (another tab or GitLab's web UI).
        self.intruder = intruder
        self.published: list[int] = []
        # Per-id publishes allowed before the next one fails.
        self.fail_publish_after = fail_publish_after
        self.fail_list_after_publish = fail_list_after_publish
        self.publish_attempted = False

    def __call__(self, path, *, host, timeout=0, paginate=False, method="GET", body=None):
        self.calls.append((method, path))
        if path == f"{MR_BASE}/draft_notes" and method == "GET":
            if self.fail_list_after_publish and self.publish_attempted:
                raise ProviderCliError("list failed")
            return [dict(d) for d in self.drafts.values()]
        if path == f"{MR_BASE}/draft_notes" and method == "POST":
            if self.fail_create_at is not None and self.created == self.fail_create_at:
                raise ProviderCliError("draft refused")
            self.created += 1
            draft_id = self.next_id
            self.next_id += 1
            self.drafts[draft_id] = {"id": draft_id, **(body or {})}
            if self.intruder is not None and self.created == 1:
                self.drafts[self.intruder] = {"id": self.intruder}
            return {"id": draft_id}
        if path == f"{MR_BASE}/draft_notes/bulk_publish":
            self.publish_attempted = True
            if self.fail_publish:
                raise ProviderCliError("publish failed")
            self.published.extend(sorted(self.drafts))
            self.drafts.clear()
            return {}
        if path.endswith("/publish") and method == "PUT":
            self.publish_attempted = True
            if (
                self.fail_publish_after is not None
                and len(self.published) >= self.fail_publish_after
            ):
                raise ProviderCliError("publish failed")
            draft_id = int(path.split("/")[-2])
            self.published.append(draft_id)
            self.drafts.pop(draft_id, None)
            return {}
        if method == "DELETE":
            self.drafts.pop(int(path.split("/")[-1]), None)
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


class TestGitlabNativeBatch(unittest.TestCase):
    ITEMS: list[dict] = [
        {"body": "first", "thread_id": None},
        {"body": "reply", "thread_id": DISCUSSION},
    ]

    def _run(self, fake):
        with mock.patch.object(gl, "_glab_api", side_effect=fake):
            return gl.post_comment_batch("g", "p", 7, "pull", self.ITEMS, host="gitlab.com")

    def test_publishes_the_batch_as_one_review(self):
        fake = _FakeDrafts()
        out = self._run(fake)
        self.assertEqual(len(out["posted"]), 2)
        self.assertIn(("POST", f"{MR_BASE}/draft_notes/bulk_publish"), fake.calls)
        self.assertEqual(fake.published, [100, 101])
        # The reply rides as a draft in its discussion.
        self.assertEqual(fake.created, 2)

    def test_reply_drafts_name_their_discussion(self):
        fake = _FakeDrafts()
        bodies = []
        original = fake.__call__

        def spy(path, **kwargs):
            if kwargs.get("method") == "POST" and path == f"{MR_BASE}/draft_notes":
                bodies.append(kwargs.get("body"))
            return original(path, **kwargs)

        with mock.patch.object(gl, "_glab_api", side_effect=spy):
            gl.post_comment_batch("g", "p", 7, "pull", self.ITEMS, host="gitlab.com")
        self.assertEqual(
            bodies,
            [{"note": "first"}, {"note": "reply", "in_reply_to_discussion_id": DISCUSSION}],
        )

    def test_a_pending_review_of_the_users_own_is_left_pending(self):
        """``bulk_publish`` publishes EVERY pending draft, so it must not run when the
        user already has an unfinished review on this merge request."""
        fake = _FakeDrafts(preexisting=[7])
        self._run(fake)
        self.assertNotIn(("POST", f"{MR_BASE}/draft_notes/bulk_publish"), fake.calls)
        self.assertEqual(sorted(fake.published), [100, 101])
        self.assertIn(7, fake.drafts)

    def test_a_draft_appearing_mid_batch_is_left_pending(self):
        fake = _FakeDrafts(intruder=55)
        self._run(fake)
        self.assertNotIn(("POST", f"{MR_BASE}/draft_notes/bulk_publish"), fake.calls)
        self.assertIn(55, fake.drafts)
        self.assertEqual(sorted(fake.published), [100, 101])

    def test_a_failed_draft_discards_the_ones_already_created(self):
        fake = _FakeDrafts(fail_create_at=1)
        with self.assertRaises(ProviderCliError):
            self._run(fake)
        self.assertEqual(fake.drafts, {})
        self.assertEqual(fake.published, [])

    def test_a_failed_publish_discards_what_is_still_pending(self):
        fake = _FakeDrafts(fail_publish=True)
        with self.assertRaises(ProviderCliError):
            self._run(fake)
        self.assertEqual(fake.drafts, {})

    def test_a_partly_published_batch_reports_what_landed(self):
        # A pre-existing draft forces the per-id publish; the second publish fails.
        fake = _FakeDrafts(preexisting=(7,), fail_publish_after=1)
        out = self._run(fake)
        self.assertEqual(fake.published, [100])
        self.assertEqual([p["index"] for p in out["posted"]], [0])
        self.assertEqual(out["failed"]["index"], 1)
        # The unpublished draft is discarded; the user's own draft stays pending.
        self.assertEqual(set(fake.drafts), {7})

    def test_an_unreadable_pending_set_reports_the_batch_as_unknown(self):
        """When the publish fails and the pending drafts cannot be read, the server
        cannot tell whether the batch published. Every item is reported unknown so
        the client never resends it, and the drafts are neither retried nor
        deleted."""
        fake = _FakeDrafts(fail_publish=True, fail_list_after_publish=True)
        out = self._run(fake)
        self.assertEqual(out["posted"], [])
        self.assertEqual(out["unknown"], [0, 1])
        self.assertIn("publish failed", out["failed"]["error"])
        self.assertEqual(set(fake.drafts), {100, 101})
        self.assertNotIn("DELETE", [method for method, _ in fake.calls])

    def test_a_confirmed_publish_survives_an_unreadable_pending_set(self):
        """A per-id publish that succeeded stays reported as posted when the next
        publish fails AND the reconciliation read fails too: otherwise the browser
        keeps that comment pending and a retry posts it a second time."""
        fake = _FakeDrafts(preexisting=(7,), fail_publish_after=1, fail_list_after_publish=True)
        out = self._run(fake)
        self.assertEqual(fake.published, [100])
        self.assertEqual([p["index"] for p in out["posted"]], [0])
        self.assertEqual(out["failed"]["index"], 1)
        # The one whose publish failed may have landed: unknown, left as a draft.
        self.assertEqual(out["unknown"], [1])
        self.assertIn(101, fake.drafts)
        self.assertIn(7, fake.drafts)

    def test_a_bad_reply_handle_creates_nothing(self):
        fake = _FakeDrafts()
        with mock.patch.object(gl, "_glab_api", side_effect=fake):
            with self.assertRaises(ProviderCliError):
                gl.post_comment_batch(
                    "g",
                    "p",
                    7,
                    "pull",
                    [{"body": "a", "thread_id": None}, {"body": "b", "thread_id": "nothex!"}],
                    host="gitlab.com",
                )
        self.assertEqual(fake.drafts, {})


class TestAzureReplies(unittest.TestCase):
    def test_reply_posts_into_the_pull_request_thread(self):
        with mock.patch.object(az, "_az_invoke", return_value={"id": 2}) as m:
            az.reply_to_comment_thread(
                "org/proj", "repo", 7, "pull", "12", "ok", host="dev.azure.com"
            )
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["resource"], "pullRequestThreadComments")
        self.assertEqual(kwargs["route"]["threadId"], 12)
        self.assertEqual(kwargs["body"]["content"], "ok")

    def test_a_work_item_reply_is_refused(self):
        with mock.patch.object(az, "_az_invoke") as m:
            with self.assertRaises(ProviderCliError):
                az.reply_to_comment_thread(
                    "org/proj", "repo", 7, "issue", "12", "ok", host="dev.azure.com"
                )
            m.assert_not_called()


def _req(payload: object) -> web.Request:
    request = make_mocked_request("POST", "/api/apps/issue-radar/comments")
    request.app["state"] = SimpleNamespace(owner_id="")
    request["user"] = "local-app"
    request["app"] = ""

    async def _json(*_args: object, **_kwargs: object) -> object:
        return payload

    request.json = _json  # type: ignore[method-assign]
    return request


def _await(coro):
    return asyncio.run(coro)


class TestCommentsRoute(unittest.TestCase):
    BASE = {"owner": "o", "repo": "r", "number": 7, "target": "pull"}

    def setUp(self):
        patches = [
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = mock.Mock()
        self.client.post_comment_batch.return_value = {
            "posted": [{"index": 0, "id": 1, "url": None}],
            "failed": None,
        }
        p = mock.patch.object(routes.provider, "client_for", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)
        self.drop = mock.patch(
            "kiro_crew.apps.builtins.issue_radar.backend.http_routes.comments._drop_detail_cache"
        )
        self.dropped = self.drop.start()
        self.addCleanup(self.drop.stop)

    def _post(self, payload):
        response = _await(routes._handle_comments(_req(payload)))
        return response.status, json.loads(response.text)

    def test_posts_the_batch_and_drops_the_detail_cache(self):
        status, body = self._post({**self.BASE, "items": [{"body": " hi ", "thread_id": None}]})
        self.assertEqual(status, 200)
        args = self.client.post_comment_batch.call_args[0]
        self.assertEqual(args[:4], ("o", "r", 7, "pull"))
        self.assertEqual(args[4], [{"body": "hi", "thread_id": None}])
        self.dropped.assert_called_once()

    def test_validation(self):
        cases = {
            "items_required": {**self.BASE, "items": []},
            "invalid_item": {**self.BASE, "items": ["x"]},
            "body_required": {**self.BASE, "items": [{"body": "   "}]},
            "invalid_thread_id": {**self.BASE, "items": [{"body": "x", "thread_id": "a/b"}]},
            "invalid_target": {**self.BASE, "target": "commit", "items": [{"body": "x"}]},
            "too_many_items": {
                **self.BASE,
                "items": [{"body": "x"}] * (comment_batch.MAX_BATCH_ITEMS + 1),
            },
        }
        for code, payload in cases.items():
            with self.subTest(code=code):
                status, body = self._post(payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], code)
        self.client.post_comment_batch.assert_not_called()

    def test_a_partial_batch_is_a_200_naming_the_failed_item(self):
        self.client.post_comment_batch.return_value = {
            "posted": [{"index": 0, "id": 1, "url": None}],
            "failed": {"index": 1, "error": "locked"},
        }
        with mock.patch.object(routes, "_audit") as audit:
            status, body = self._post({**self.BASE, "items": [{"body": "a"}, {"body": "b"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["failed"], {"index": 1, "error": "locked"})
        self.assertEqual(audit.call_args[0][2], "failure")
        self.assertEqual(audit.call_args[0][1], "o/r#7:1/2")

    def test_unknown_items_reach_the_client(self):
        self.client.post_comment_batch.return_value = {
            "posted": [],
            "failed": {"index": 0, "error": "timeout"},
            "unknown": [0, 1],
        }
        status, body = self._post({**self.BASE, "items": [{"body": "a"}, {"body": "b"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["unknown"], [0, 1])
        # A provider that never reports any still answers with an empty list.
        self.client.post_comment_batch.return_value = {"posted": [], "failed": None}
        _, body = self._post({**self.BASE, "items": [{"body": "a"}]})
        self.assertEqual(body["unknown"], [])

    def test_a_provider_refusal_is_403(self):
        self.client.post_comment_batch.side_effect = ProviderPermissionError("nope")
        status, body = self._post({**self.BASE, "items": [{"body": "a"}]})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "provider_forbidden")
        self.dropped.assert_not_called()

    def test_a_read_only_repo_is_refused_before_any_call(self):
        with mock.patch.object(routes, "_repo_can_write", return_value=False):
            status, _ = self._post({**self.BASE, "items": [{"body": "a"}]})
        self.assertEqual(status, 403)
        self.client.post_comment_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
