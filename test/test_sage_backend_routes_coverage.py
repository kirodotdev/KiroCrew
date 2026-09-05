"""Coverage tests for the Code Review Sage backend HTTP handlers.

The app ships its own suite next to the code
(``src/kiro_crew/apps/builtins/code_review_sage/tests/``), but several handlers
were never exercised there: the repo-discovery pair (``repo-prs`` /
``review-repo``), the review-settings read/write path, namespace create/delete,
the learnings view, the comment-posting background task, and the notification
helpers a finished run pushes to the bell feed.

Harness matches the app suite's ``test_run_endpoints.py`` exactly: the routes
module is loaded by file path (the app dir is hyphenated and cannot be imported
as a package), ``KIROCREW_HOME`` is pointed at a per-test tmp dir so nothing
touches the operator's real data root, and the handlers are driven with a
minimal fake request rather than a live aiohttp client -- they only read
``request.method`` / ``request.query`` / ``request.match_info`` /
``request.json()``, so no socket is ever bound.

Everything that would reach ``gh``, the network, or a real worker pool is
stubbed; those paths are covered for their branching, not for their upstream.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from aiohttp import web

_APP_ROOT = (Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "apps"
             / "builtins" / "code_review_sage")
_ROUTES = _APP_ROOT / "backend" / "routes.py"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sage_lib import learning  # noqa: E402  (app root added to sys.path above)
from sage_lib import store  # noqa: E402


def _load_routes_module():
    """Fresh module instance so the in-memory run registry starts empty."""
    spec = importlib.util.spec_from_file_location(
        "sage_backend_routes_cov_under_test", str(_ROUTES))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Req:
    """Minimal stand-in for an aiohttp request (same shape the app suite uses).

    A ``None`` body makes ``json()`` raise, which is how a real request with no
    or invalid JSON body reaches the handlers' ``except`` fallbacks.
    """

    def __init__(self, *, run_id=None, query=None, method="GET", body=None):
        self.match_info = {} if run_id is None else {"run_id": run_id}
        self.query = query or {}
        self.method = method
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class _Notifier:
    """Records bell notifications instead of persisting them."""

    def __init__(self, *, boom=False):
        self.calls: list[tuple] = []
        self._boom = boom

    def notify(self, channel, title, body):
        if self._boom:
            raise RuntimeError("sink unavailable")
        self.calls.append((channel, title, body))


class _FakePool:
    """The two batch hooks ``_post_comments_bg`` awaits on the review pool."""

    def __init__(self):
        self.began = 0
        self.ended = 0
        self.batch_models: list[str | None] = []

    async def begin_batch(self, model=None):
        self.began += 1
        self.batch_models.append(model)

    async def end_batch(self):
        self.ended += 1


def _body(resp) -> dict:
    return json.loads(resp.body)


class _SageRoutesBase(unittest.IsolatedAsyncioTestCase):
    """Per-test tmp data root + a freshly loaded routes module."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        # config_dir() memoizes the resolved data home for the process; reset it
        # so this test's KIROCREW_HOME wins over whatever an earlier test cached.
        import kiro_crew.config.paths as _paths
        self._paths = _paths
        self._old_resolved = getattr(_paths, "_resolved_home", None)
        _paths._resolved_home = None
        self.mod = _load_routes_module()
        self.mod._RUNS = []
        self.mod._CANCELLED.clear()
        self.mod._INFLIGHT.clear()
        self.mod._STAGE_OWNER.clear()
        self.mod._CONSOLIDATING.clear()
        self.mod._CONSOLIDATE_STATE.clear()
        store.ensure_layout()

    def tearDown(self):
        self.mod._CANCELLED.clear()
        self.mod._INFLIGHT.clear()
        self.mod._STAGE_OWNER.clear()
        self.mod._CONSOLIDATING.clear()
        self._paths._resolved_home = self._old_resolved
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg_path(self) -> Path:
        return store.data_dir() / "config.json"


# ── run-level error/headline helpers ──

class TestFirstChangeError(_SageRoutesBase):
    def test_empty_summary_yields_empty_string(self):
        self.assertEqual(self.mod._first_change_error({}), "")
        self.assertEqual(self.mod._first_change_error({"per_change": []}), "")

    def test_blank_fields_are_skipped(self):
        summary = {"per_change": [{"deep_error": "   ", "gate_error": ""},
                                  {"skipped_reason": "draft PR"}]}
        self.assertEqual(self.mod._first_change_error(summary), "draft PR")

    def test_sentinel_reasons_are_humanized(self):
        for sentinel, expected in (
            ("no_review_recorded",
             "the reviewer finished but wrote no findings record"),
            ("review_failed", "the review turn failed"),
        ):
            with self.subTest(sentinel=sentinel):
                summary = {"per_change": [{"deep_error": sentinel}]}
                self.assertEqual(self.mod._first_change_error(summary), expected)

    def test_unknown_reason_passes_through_verbatim(self):
        summary = {"per_change": [{"gate_error": "gh exited 128"}]}
        self.assertEqual(self.mod._first_change_error(summary), "gh exited 128")


class TestFirstChangeFailureToken(_SageRoutesBase):
    """The token half of ``_first_change_failure``.

    ``_first_change_error`` maps the reason token to English, so the token is
    gone by the time the run is serialized and the dashboard has to recognize
    causes by their wording. These pin the token being carried BESIDE the
    sentence.
    """

    def test_token_accompanies_the_humanized_sentence(self):
        summary = {"per_change": [{"skipped_reason": "no_review_recorded"}]}
        sentence, token = self.mod._first_change_failure(summary)
        self.assertEqual(
            sentence, "the reviewer finished but wrote no findings record")
        self.assertEqual(token, "no_review_recorded")

    def test_token_comes_from_the_SAME_record_as_the_sentence(self):
        # The trap a second independent scan for the token would fall into: the
        # sentence is built from record 1's `deep_error`, and record 1 kept no
        # reason. A scan that just looked for the first `skipped_reason` anywhere
        # would return record 2's token and label record 1's failure with it.
        summary = {"per_change": [
            {"deep_error": "gh exited 128"},
            {"skipped_reason": "review_record_incomplete"},
        ]}
        sentence, token = self.mod._first_change_failure(summary)
        self.assertEqual(sentence, "gh exited 128")
        self.assertEqual(token, "")

    def test_prose_record_that_also_carries_a_token_reports_both(self):
        # The common real shape: the driver writes the spawn's prose into
        # `deep_error` AND the token into `skipped_reason` on one record.
        summary = {"per_change": [{
            "deep_error": "the reviewer cannot run: no kiro-cli executable was found",
            "skipped_reason": "runtime_unavailable",
        }]}
        sentence, token = self.mod._first_change_failure(summary)
        self.assertEqual(
            sentence, "the reviewer cannot run: no kiro-cli executable was found")
        self.assertEqual(token, "runtime_unavailable")

    def test_empty_summary_yields_no_token(self):
        self.assertEqual(self.mod._first_change_failure({}), ("", ""))
        self.assertEqual(self.mod._first_change_failure({"per_change": []}), ("", ""))


class TestRunPayloadCarriesReasonToken(_SageRoutesBase):
    """The run RECORD the dashboard reads must name its failure cause as a token.

    ``_first_change_failure`` having the token is not the fix on its own -- the
    token has to survive into ``run``, which is what gets serialized to
    ``/runs``. This drives the real background job with the driver, pool and
    persistence stubbed, so the assertions are about the record the job leaves
    behind rather than about a helper's return value.
    """

    def _drive(self, summary: dict) -> dict:
        """Run ``_run_review_bg`` against a stub driver returning ``summary``."""
        run: dict = {"run_id": "r1", "changes": ["https://x/pull/1"],
                     "change_ids": ["c1"], "status": "running"}

        # begin_batch carries the run's pinned model, as ReviewPool.begin_batch does.
        class _Pool:
            async def begin_batch(self, model=None):
                return None

            async def end_batch(self):
                return None

        patches = {
            "_claim_changes_under_lock": lambda r, c: c,
            "_collect_delivered": lambda r, s: None,
            "_record_reviewed": lambda r: None,
            "_release_claims": lambda r: None,
        }
        with contextlib.ExitStack() as stack:
            for name, fn in patches.items():
                stack.enter_context(
                    unittest.mock.patch.object(self.mod, name, fn))
            stack.enter_context(unittest.mock.patch.object(
                self.mod, "_save_runs", unittest.mock.AsyncMock()))
            stack.enter_context(unittest.mock.patch.object(
                self.mod, "_notify_finished", unittest.mock.AsyncMock()))
            stack.enter_context(unittest.mock.patch.object(
                self.mod.review_pool, "runtime_preflight", lambda: ""))
            stack.enter_context(unittest.mock.patch.object(
                self.mod.review_pool, "get_pool", lambda: _Pool()))
            stack.enter_context(unittest.mock.patch.object(
                self.mod.review_pool, "make_sync_dispatch", lambda loop, pool: None))
            stack.enter_context(unittest.mock.patch.object(
                self.mod.review_driver, "run_review",
                lambda *a, **k: summary))
            asyncio.run(self.mod._run_review_bg(run, list(run["changes"])))
        return run

    def _nothing_recorded(self, reason: str) -> dict:
        """A run whose one change failed with ``reason``: ok, but nothing recorded."""
        return {"ok": True, "changes": 1, "cancelled": 0,
                "result_records": 0, "deep_reviewed": 0,
                "per_change": [{"skipped_reason": reason}]}

    def test_error_run_names_its_cause_as_a_token_beside_the_sentence(self):
        run = self._drive(self._nothing_recorded("review_record_incomplete"))
        self.assertEqual(run["status"], "error")
        # The sentence stays exactly where it was -- the token is carried BESIDE
        # it, so nothing that reads the payload today changes.
        self.assertEqual(
            run["error"],
            "the reviewer wrote a findings record but never completed the review")
        # `.get`, not `[...]`: a missing key must fail this as an ASSERTION
        # comparing None against the token, not as a KeyError -- an exception
        # would also be raised by a harness that never reached this code at all.
        self.assertEqual(run.get("reason"), "review_record_incomplete")

    def test_token_is_set_on_the_summary_level_error_branch_too(self):
        # `ok: False` takes the other error branch, which overwrites `error` from
        # the summary. The token describes the same `per_change` records, so it
        # must be present here as well -- this is the preflight-failure shape.
        run = self._drive({
            "ok": False, "error": "the reviewer cannot run: no kiro-cli executable",
            "changes": 1, "cancelled": 0, "result_records": 0, "deep_reviewed": 0,
            "per_change": [{"deep_error": "the reviewer cannot run: no kiro-cli "
                                          "executable",
                            "skipped_reason": "runtime_unavailable"}],
        })
        self.assertEqual(run["status"], "error")
        self.assertEqual(run.get("reason"), "runtime_unavailable")

    def test_a_successful_run_carries_no_reason(self):
        run = self._drive({"ok": True, "changes": 1, "cancelled": 0,
                           "result_records": 1, "deep_reviewed": 1,
                           "per_change": [{}]})
        self.assertEqual(run["status"], "done")
        self.assertNotIn("reason", run)

    def test_reason_is_absent_not_blank_when_no_record_carried_one(self):
        # A reader must not have to tell an empty-string token apart from a
        # missing one; only the key's absence means "this run has no token".
        run = self._drive({"ok": True, "changes": 1, "cancelled": 0,
                           "result_records": 0, "deep_reviewed": 0,
                           "per_change": [{"deep_error": "gh exited 128"}]})
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["error"], "gh exited 128")
        self.assertNotIn("reason", run)


class TestRunHeadline(_SageRoutesBase):
    def test_repo_run_counts_prs(self):
        self.assertEqual(
            self.mod._run_headline({"repo": "acme/repo", "changes": ["a", "b"]}),
            "acme/repo · 2 PRs")

    def test_repo_run_singular(self):
        self.assertEqual(
            self.mod._run_headline({"repo": "acme/repo", "changes": ["a"]}),
            "acme/repo · 1 PR")

    def test_single_github_dot_com_pr_is_host_less(self):
        with unittest.mock.patch.object(
                self.mod.adapters, "github_pr_ref",
                return_value=("github.com", "acme", "repo", "7")):
            headline = self.mod._run_headline(
                {"changes": ["https://github.com/acme/repo/pull/7"]})
        self.assertEqual(headline, "acme/repo/pull/7")

    def test_enterprise_host_is_kept_so_two_instances_read_apart(self):
        with unittest.mock.patch.object(
                self.mod.adapters, "github_pr_ref",
                return_value=("ghe.example.com", "acme", "repo", "7")):
            headline = self.mod._run_headline(
                {"changes": ["https://ghe.example.com/acme/repo/pull/7"]})
        self.assertEqual(headline, "ghe.example.com/acme/repo/pull/7")

    def test_unparseable_link_falls_back_to_the_tail(self):
        with unittest.mock.patch.object(
                self.mod.adapters, "github_pr_ref",
                side_effect=self.mod.adapters.AdapterError("nope")):
            headline = self.mod._run_headline(
                {"changes": ["https://github.com/acme/repo/pull/oops"]})
        self.assertEqual(headline, "acme/repo/pull/oops")

    def test_multi_change_run_without_repo_is_a_bare_count(self):
        self.assertEqual(
            self.mod._run_headline({"changes": ["a", "b", "c"]}), "3 PRs")
        self.assertEqual(self.mod._run_headline({}), "0 PRs")


class TestNotifyFinished(_SageRoutesBase):
    async def test_non_terminal_status_is_silent(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_finished({"status": "running", "changes": ["a"]})
        await self.mod._notify_finished({"status": "cancelled", "changes": ["a"]})
        self.assertEqual(notifier.calls, [])

    async def test_missing_state_is_a_noop(self):
        self.mod._APP_STATE.pop("state", None)
        await self.mod._notify_finished({"status": "done", "changes": ["a"]})

    async def test_state_without_notify_is_a_noop(self):
        self.mod._APP_STATE["state"] = object()
        await self.mod._notify_finished({"status": "done", "changes": ["a"]})

    async def test_error_run_reports_its_error(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_finished(
            {"status": "error", "repo": "acme/repo", "changes": ["a"],
             "error": "gh exited 128"})
        (_channel, title, body) = notifier.calls[0]
        self.assertEqual(title, "Code review failed")
        self.assertIn("gh exited 128", body)

    async def test_error_run_without_error_text_uses_a_default(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_finished(
            {"status": "error", "repo": "acme/repo", "changes": ["a"]})
        self.assertIn("did not complete", notifier.calls[0][2])

    async def test_done_run_summarizes_bands(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_finished({
            "status": "done", "repo": "acme/repo", "changes": ["a", "b"],
            "summary": {"report": {"bands": {"red": 2, "yellow": 1}}},
        })
        (_channel, title, body) = notifier.calls[0]
        self.assertEqual(title, "Code review ready")
        self.assertIn("2 needs review", body)
        self.assertIn("1 worth a glance", body)

    async def test_done_run_with_clean_bands_says_nothing_flagged(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_finished(
            {"status": "done", "repo": "acme/repo", "changes": ["a"]})
        self.assertIn("nothing flagged", notifier.calls[0][2])

    async def test_notification_failure_never_propagates(self):
        self.mod._APP_STATE["state"] = _Notifier(boom=True)
        await self.mod._notify_finished(
            {"status": "done", "repo": "acme/repo", "changes": ["a"]})


class TestNotifyPosted(_SageRoutesBase):
    async def test_missing_state_is_a_noop(self):
        self.mod._APP_STATE.pop("state", None)
        await self.mod._notify_posted({"changes": ["a"]}, 3, False)

    async def test_state_without_notify_is_a_noop(self):
        self.mod._APP_STATE["state"] = object()
        await self.mod._notify_posted({"changes": ["a"]}, 3, False)

    async def test_success_pluralizes_the_comment_count(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_posted({"repo": "acme/repo", "changes": ["a"]}, 1, False)
        await self.mod._notify_posted({"repo": "acme/repo", "changes": ["a"]}, 2, False)
        self.assertIn("1 comment on", notifier.calls[0][2])
        self.assertIn("2 comments on", notifier.calls[1][2])

    async def test_failure_reports_the_post_error(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_posted(
            {"repo": "acme/repo", "changes": ["a"], "post_error": "gh 403"}, 0, True)
        self.assertEqual(notifier.calls[0][1], "Posting review comments failed")
        self.assertIn("gh 403", notifier.calls[0][2])

    async def test_failure_without_error_text_uses_a_default(self):
        notifier = _Notifier()
        self.mod._APP_STATE["state"] = notifier
        await self.mod._notify_posted({"repo": "acme/repo", "changes": ["a"]}, 0, True)
        self.assertIn("did not complete", notifier.calls[0][2])

    async def test_notification_failure_never_propagates(self):
        self.mod._APP_STATE["state"] = _Notifier(boom=True)
        await self.mod._notify_posted({"repo": "acme/repo", "changes": ["a"]}, 1, False)


# ── repo discovery: GET repo-prs ──

_PR_A = {"url": "https://github.com/acme/repo/pull/1", "title": "one",
         "head_sha": "aaa1"}
_PR_B = {"url": "https://github.com/acme/repo/pull/2", "title": "two",
         "head_sha": "bbb2"}


class _RepoHandlerBase(_SageRoutesBase):
    def _patch_prs(self, prs, *, error=None):
        """Stub the `gh`-backed PR enumeration `_list_repo_prs` calls."""
        def _fake(owner, repo, *, host="github.com", **kw):
            if error is not None:
                raise error
            return list(prs)
        return unittest.mock.patch.object(
            self.mod.pipeline, "list_open_prs", side_effect=_fake)

    def _patch_index(self, index):
        return unittest.mock.patch.object(
            self.mod.results, "read_reviewed", return_value=index)


class TestRepoPrsHandler(_RepoHandlerBase):
    async def test_missing_repo_param_is_400(self):
        resp = await self.mod._handle_repo_prs(_Req(query={"repo": "  "}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_unparseable_repo_url_is_400(self):
        resp = await self.mod._handle_repo_prs(
            _Req(query={"repo": "definitely not a url"}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_repo_url")

    async def test_provider_failure_is_502_without_leaking_the_message(self):
        with self._patch_prs([], error=RuntimeError("gh: token expired for acme")):
            resp = await self.mod._handle_repo_prs(
                _Req(query={"repo": "https://github.com/acme/repo"}))
        self.assertEqual(resp.status, 502)
        payload = _body(resp)
        self.assertEqual(payload["code"], "provider_unavailable")
        self.assertNotIn("token", payload["error"])

    async def test_annotates_reviewed_stale_and_new(self):
        rkey = self.mod.review_driver.reviewed_key_for(_PR_A["url"])
        index = {rkey: {"head_sha": "aaa1", "reviewed_at": "2026-01-01T00:00:00Z"}}
        with self._patch_prs([_PR_A, _PR_B]), self._patch_index(index):
            resp = await self.mod._handle_repo_prs(
                _Req(query={"repo": "https://github.com/acme/repo"}))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertEqual(payload["repo"], "acme/repo")
        self.assertEqual(payload["count"], 2)
        first, second = payload["prs"]
        self.assertTrue(first["reviewed"])
        self.assertFalse(first["reviewed_stale"])
        self.assertEqual(first["reviewed_at"], "2026-01-01T00:00:00Z")
        self.assertTrue(first["change_id"])
        self.assertFalse(second["reviewed"])
        self.assertFalse(second["reviewed_stale"])

    async def test_head_sha_drift_marks_the_pr_stale(self):
        rkey = self.mod.review_driver.reviewed_key_for(_PR_A["url"])
        with self._patch_prs([_PR_A]), self._patch_index({rkey: {"head_sha": "old"}}):
            resp = await self.mod._handle_repo_prs(
                _Req(query={"repo": "https://github.com/acme/repo"}))
        pr = _body(resp)["prs"][0]
        self.assertFalse(pr["reviewed"])
        self.assertTrue(pr["reviewed_stale"])


# ── repo discovery: POST review-repo ──

class TestReviewRepoHandler(_RepoHandlerBase):
    def _patch_driver(self):
        """Swallow the background review so no worker pool is ever started."""
        async def _noop(run, changes):
            return None
        return unittest.mock.patch.object(
            self.mod, "_run_review_bg", side_effect=_noop)

    async def test_missing_repo_is_400(self):
        resp = await self.mod._handle_review_repo(_Req(method="POST", body={}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_non_dict_body_is_treated_as_empty(self):
        resp = await self.mod._handle_review_repo(
            _Req(method="POST", body=["not", "a", "dict"]))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_unreadable_body_is_treated_as_empty(self):
        resp = await self.mod._handle_review_repo(_Req(method="POST", body=None))
        self.assertEqual(resp.status, 400)

    async def test_unparseable_repo_url_is_400(self):
        resp = await self.mod._handle_review_repo(
            _Req(method="POST", body={"repo": "definitely not a url"}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_repo_url")

    async def test_provider_failure_is_502(self):
        with self._patch_prs([], error=RuntimeError("gh: not logged in")):
            resp = await self.mod._handle_review_repo(
                _Req(method="POST", body={"repo": "https://github.com/acme/repo"}))
        self.assertEqual(resp.status, 502)
        self.assertEqual(_body(resp)["code"], "provider_unavailable")

    async def test_all_reviewed_at_current_head_is_a_noop(self):
        index = {
            self.mod.review_driver.reviewed_key_for(_PR_A["url"]): {"head_sha": "aaa1"},
            self.mod.review_driver.reviewed_key_for(_PR_B["url"]): {"head_sha": "bbb2"},
        }
        with self._patch_prs([_PR_A, _PR_B]), self._patch_index(index):
            resp = await self.mod._handle_review_repo(
                _Req(method="POST", body={"repo": "https://github.com/acme/repo"}))
        payload = _body(resp)
        self.assertEqual(payload["status"], "noop")
        self.assertEqual(payload["changes"], [])
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(self.mod._RUNS, [])

    async def test_urlless_prs_are_ignored(self):
        with self._patch_prs([{"title": "no url"}]), self._patch_index({}):
            resp = await self.mod._handle_review_repo(
                _Req(method="POST", body={"repo": "https://github.com/acme/repo"}))
        self.assertEqual(_body(resp)["status"], "noop")

    async def test_queues_only_the_unreviewed_pr(self):
        index = {
            self.mod.review_driver.reviewed_key_for(_PR_A["url"]): {"head_sha": "aaa1"},
        }
        with self._patch_prs([_PR_A, _PR_B]), self._patch_index(index), \
                self._patch_driver():
            resp = await self.mod._handle_review_repo(
                _Req(method="POST", body={"repo": "https://github.com/acme/repo"}))
            await asyncio.sleep(0)
        payload = _body(resp)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["changes"], [_PR_B["url"]])
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["repo"], "acme/repo")
        run = self.mod._find_run(payload["run_id"])
        self.assertIsNotNone(run)
        self.assertFalse(run["force"])
        self.assertEqual(len(run["change_ids"]), 1)

    async def test_force_true_reviews_every_open_pr(self):
        index = {
            self.mod.review_driver.reviewed_key_for(_PR_A["url"]): {"head_sha": "aaa1"},
            self.mod.review_driver.reviewed_key_for(_PR_B["url"]): {"head_sha": "bbb2"},
        }
        with self._patch_prs([_PR_A, _PR_B]), self._patch_index(index), \
                self._patch_driver():
            resp = await self.mod._handle_review_repo(_Req(
                method="POST",
                body={"repo": "https://github.com/acme/repo", "force": True}))
            await asyncio.sleep(0)
        payload = _body(resp)
        self.assertEqual(payload["skipped"], 0)
        self.assertEqual(len(payload["changes"]), 2)
        self.assertTrue(self.mod._find_run(payload["run_id"])["force"])

    async def test_truthy_non_boolean_force_does_not_bypass_dedup(self):
        """A JSON string must not trigger a costly re-review of every open PR."""
        index = {
            self.mod.review_driver.reviewed_key_for(_PR_A["url"]): {"head_sha": "aaa1"},
        }
        with self._patch_prs([_PR_A]), self._patch_index(index):
            resp = await self.mod._handle_review_repo(_Req(
                method="POST",
                body={"repo": "https://github.com/acme/repo", "force": "true"}))
        self.assertEqual(_body(resp)["status"], "noop")


# ── review settings: config read/write ──

class TestReviewSectionIO(_SageRoutesBase):
    def test_load_defaults_when_config_is_unreadable(self):
        with unittest.mock.patch.object(
                self.mod.store, "load_config", side_effect=OSError("boom")):
            section = self.mod._load_review_section()
        self.assertIsNone(section["model"])
        self.assertEqual(section["effort"], "")
        self.assertEqual(section["active_namespaces"], ["default"])

    def test_load_defaults_when_review_section_is_not_a_mapping(self):
        with unittest.mock.patch.object(
                self.mod.store, "load_config", return_value={"review": "nope"}):
            section = self.mod._load_review_section()
        self.assertEqual(section["active_namespaces"], ["default"])

    def test_load_defaults_when_config_is_not_a_mapping(self):
        with unittest.mock.patch.object(
                self.mod.store, "load_config", return_value=["not", "a", "dict"]):
            self.assertIsNone(self.mod._load_review_section()["model"])

    def test_write_seeds_the_layout_when_config_is_missing(self):
        self._cfg_path().unlink()
        with unittest.mock.patch.object(self.mod, "_KNOWN_MODELS", ["opus-4.8"]):
            review = self.mod._write_review_section({"model": "opus-4.8"})
        self.assertEqual(review["model"], "opus-4.8")
        self.assertTrue(self._cfg_path().is_file())

    def test_write_replaces_a_non_mapping_review_section(self):
        cfg = json.loads(self._cfg_path().read_text(encoding="utf-8"))
        cfg["review"] = "clobbered"
        self._cfg_path().write_text(json.dumps(cfg), encoding="utf-8")
        review = self.mod._write_review_section({"effort": "high"})
        self.assertEqual(review["effort"], "high")

    def test_write_preserves_unrelated_config_keys(self):
        cfg = json.loads(self._cfg_path().read_text(encoding="utf-8"))
        cfg["kept_by_test"] = {"nested": 1}
        self._cfg_path().write_text(json.dumps(cfg), encoding="utf-8")
        self.mod._write_review_section({"effort": ""})
        after = json.loads(self._cfg_path().read_text(encoding="utf-8"))
        self.assertEqual(after["kept_by_test"], {"nested": 1})

    def test_empty_model_clears_the_override(self):
        review = self.mod._write_review_section({"model": ""})
        self.assertIsNone(review["model"])
        review = self.mod._write_review_section({"model": None})
        self.assertIsNone(review["model"])

    def test_unknown_model_is_rejected(self):
        with unittest.mock.patch.object(self.mod, "_KNOWN_MODELS", ["opus-4.8"]):
            with self.assertRaises(ValueError):
                self.mod._write_review_section({"model": "sneaky[1m]"})

    def test_unknown_effort_falls_back_to_inherit(self):
        self.assertEqual(
            self.mod._write_review_section({"effort": "turbo"})["effort"], "")
        self.assertEqual(
            self.mod._write_review_section({"effort": "HIGH"})["effort"], "high")

    def test_active_namespaces_are_filtered_against_what_exists(self):
        learning.create_namespace("proj-a")
        review = self.mod._write_review_section(
            {"active_namespaces": ["proj-a", "ghost"]})
        self.assertEqual(review["active_namespaces"], ["proj-a"])

    def test_all_unknown_namespaces_fall_back_to_default(self):
        review = self.mod._write_review_section({"active_namespaces": ["ghost"]})
        self.assertEqual(review["active_namespaces"], ["default"])

    def test_non_list_namespaces_patch_is_ignored(self):
        self.mod._write_review_section({"active_namespaces": ["default"]})
        review = self.mod._write_review_section({"active_namespaces": "default"})
        self.assertEqual(review["active_namespaces"], ["default"])
        review = self.mod._write_review_section({"active_namespaces": []})
        self.assertEqual(review["active_namespaces"], ["default"])

    def test_max_concurrent_is_clamped_to_the_ceiling(self):
        ceil = self.mod.review_pool.MAX_CONCURRENT_CEIL
        self.assertEqual(
            self.mod._write_review_section({"max_concurrent": 10_000})["max_concurrent"],
            ceil)
        self.assertEqual(
            self.mod._write_review_section({"max_concurrent": 0})["max_concurrent"], 1)
        self.assertEqual(
            self.mod._write_review_section({"max_concurrent": "3"})["max_concurrent"], 3)

    def test_non_numeric_max_concurrent_is_rejected(self):
        with self.assertRaises(ValueError):
            self.mod._write_review_section({"max_concurrent": "many"})
        with self.assertRaises(ValueError):
            self.mod._write_review_section({"max_concurrent": None})


class TestValidModel(_SageRoutesBase):
    def test_rejects_empty_overlong_and_unsafe_tokens(self):
        for bad in ("", "x" * 65, "opus[1m]", "opus 4.8", "opus/4.8"):
            with self.subTest(model=bad):
                self.assertFalse(self.mod._valid_model(bad))

    def test_registry_membership_is_required_when_known(self):
        with unittest.mock.patch.object(self.mod, "_KNOWN_MODELS", ["opus-4.8"]):
            self.assertTrue(self.mod._valid_model("opus-4.8"))
            self.assertFalse(self.mod._valid_model("sonnet-9"))
            self.assertEqual(self.mod._known_models(), ["opus-4.8"])

    def test_any_safe_token_passes_when_the_registry_is_unavailable(self):
        with unittest.mock.patch.object(self.mod, "_KNOWN_MODELS", []):
            self.assertTrue(self.mod._valid_model("some.model_id-1"))


# ── review settings: the HTTP handler ──

class TestSettingsHandler(_SageRoutesBase):
    def _patch_audit(self):
        return unittest.mock.patch("kiro_crew.sel.sel")

    async def test_get_enumerates_models_efforts_and_namespaces(self):
        resp = await self.mod._handle_settings(_Req(method="GET"))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertIn("settings", payload)
        self.assertIn("reviewer", payload)
        self.assertEqual(payload["namespaces"], ["default"])
        self.assertEqual(payload["efforts"],
                         list(self.mod.review_pool.VALID_EFFORTS))
        self.assertEqual(payload["max_concurrent_max"],
                         self.mod.review_pool.MAX_CONCURRENT_CEIL)
        self.assertEqual(payload["settings"]["active_namespaces"], ["default"])

    async def test_get_falls_back_to_default_when_namespace_walk_fails(self):
        with unittest.mock.patch.object(
                self.mod.learning, "list_namespaces", side_effect=OSError("boom")):
            resp = await self.mod._handle_settings(_Req(method="GET"))
        self.assertEqual(_body(resp)["namespaces"], ["default"])

    async def test_get_tolerates_a_failing_reviewer_probe(self):
        with unittest.mock.patch.object(
                self.mod.review_pool, "reviewer_info", side_effect=RuntimeError("x")):
            resp = await self.mod._handle_settings(_Req(method="GET"))
        self.assertIsNone(_body(resp)["reviewer"])

    async def test_put_persists_the_patch_and_audits_success(self):
        with self._patch_audit() as sel, \
                unittest.mock.patch.object(self.mod, "_KNOWN_MODELS", ["opus-4.8"]):
            resp = await self.mod._handle_settings(_Req(
                method="PUT",
                body={"model": "opus-4.8", "effort": "high", "max_concurrent": 4}))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["settings"]["model"], "opus-4.8")
        self.assertEqual(payload["settings"]["effort"], "high")
        self.assertEqual(payload["settings"]["max_concurrent"], 4)
        outcome = sel.return_value.log_api_access.call_args.kwargs["outcome"]
        self.assertEqual(outcome, "success")
        on_disk = json.loads(self._cfg_path().read_text(encoding="utf-8"))
        self.assertEqual(on_disk["review"]["model"], "opus-4.8")

    async def test_put_with_unreadable_body_is_an_empty_patch(self):
        with self._patch_audit():
            resp = await self.mod._handle_settings(_Req(method="PUT", body=None))
        self.assertEqual(resp.status, 200)
        self.assertTrue(_body(resp)["ok"])

    async def test_put_with_non_dict_body_is_an_empty_patch(self):
        with self._patch_audit():
            resp = await self.mod._handle_settings(_Req(method="PUT", body=[1, 2]))
        self.assertEqual(resp.status, 200)

    async def test_put_rejects_an_invalid_model_and_audits_the_denial(self):
        with self._patch_audit() as sel, \
                unittest.mock.patch.object(self.mod, "_KNOWN_MODELS", ["opus-4.8"]):
            resp = await self.mod._handle_settings(
                _Req(method="PUT", body={"model": "evil[1m]"}))
        self.assertEqual(resp.status, 400)
        payload = _body(resp)
        self.assertEqual(payload["code"], "invalid_request")
        self.assertFalse(payload["ok"])
        outcome = sel.return_value.log_api_access.call_args.kwargs["outcome"]
        self.assertEqual(outcome, "denied")

    async def test_audit_failure_never_breaks_the_response(self):
        with unittest.mock.patch("kiro_crew.sel.sel", side_effect=RuntimeError("no sel")):
            resp = await self.mod._handle_settings(
                _Req(method="PUT", body={"effort": ""}))
        self.assertEqual(resp.status, 200)

    async def test_unexpected_write_failure_is_a_correlated_500(self):
        with self._patch_audit(), unittest.mock.patch.object(
                self.mod, "_write_review_section", side_effect=RuntimeError("disk")):
            resp = await self.mod._handle_settings(
                _Req(method="PUT", body={"effort": "high"}))
        self.assertEqual(resp.status, 500)
        payload = _body(resp)
        self.assertEqual(payload["code"], "internal_error")
        self.assertEqual(payload["error"], "internal error")
        self.assertTrue(payload["id"])
        self.assertNotIn("disk", json.dumps(payload))

    async def test_put_falls_back_to_the_effective_concurrency(self):
        """A patch that never set max_concurrent still reports a usable value."""
        with self._patch_audit():
            resp = await self.mod._handle_settings(
                _Req(method="PUT", body={"effort": ""}))
        self.assertGreaterEqual(_body(resp)["settings"]["max_concurrent"], 1)


# ── namespaces ──

class TestNamespacesHandler(_SageRoutesBase):
    def _patch_audit(self):
        return unittest.mock.patch("kiro_crew.sel.sel")

    async def test_get_lists_namespaces_with_counts_and_active_flags(self):
        learning.create_namespace("proj-a")
        resp = await self.mod._handle_namespaces(_Req(method="GET"))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        names = [row["name"] for row in payload["namespaces"]]
        self.assertIn("default", names)
        self.assertIn("proj-a", names)
        for row in payload["namespaces"]:
            self.assertEqual(row["patterns"], 0)
            self.assertEqual(row["candidate"], 0)
            self.assertEqual(row["active"], row["name"] in payload["active"])
        self.assertEqual(payload["active"], ["default"])

    async def test_get_degrades_gracefully_when_the_walk_fails(self):
        with unittest.mock.patch.object(
                self.mod.learning, "list_namespaces", side_effect=OSError("boom")):
            resp = await self.mod._handle_namespaces(_Req(method="GET"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(_body(resp), {"namespaces": [], "active": ["default"]})

    async def test_missing_name_is_400(self):
        resp = await self.mod._handle_namespaces(
            _Req(method="POST", body={"name": "   "}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "name_required")

    async def test_unreadable_body_is_400(self):
        resp = await self.mod._handle_namespaces(_Req(method="POST", body=None))
        self.assertEqual(resp.status, 400)

    async def test_post_creates_a_namespace(self):
        with self._patch_audit() as sel:
            resp = await self.mod._handle_namespaces(
                _Req(method="POST", body={"name": "proj-b"}))
        self.assertEqual(resp.status, 200)
        self.assertTrue(_body(resp)["ok"])
        self.assertIn("proj-b", learning.list_namespaces())
        self.assertEqual(
            sel.return_value.log_api_access.call_args.kwargs["operation"],
            "create_namespace")

    async def test_post_refusal_is_400(self):
        with self._patch_audit():
            resp = await self.mod._handle_namespaces(
                _Req(method="POST", body={"name": "../escape"}))
        self.assertEqual(resp.status, 400)
        self.assertFalse(_body(resp)["ok"])

    async def test_delete_refuses_while_a_consolidation_runs(self):
        learning.create_namespace("busy")
        self.mod._CONSOLIDATING.add("busy")
        resp = await self.mod._handle_namespaces(
            _Req(method="DELETE", body={"name": "busy"}))
        self.assertEqual(resp.status, 409)
        self.assertEqual(_body(resp)["code"], "consolidation_in_progress")
        self.assertIn("busy", learning.list_namespaces())

    async def test_delete_removes_it_and_prunes_the_active_list(self):
        learning.create_namespace("doomed")
        self.mod._write_review_section({"active_namespaces": ["doomed"]})
        self.assertEqual(
            self.mod._load_review_section()["active_namespaces"], ["doomed"])
        with self._patch_audit() as sel:
            resp = await self.mod._handle_namespaces(
                _Req(method="DELETE", body={"name": "doomed"}))
        self.assertEqual(resp.status, 200)
        self.assertTrue(_body(resp)["ok"])
        self.assertNotIn("doomed", learning.list_namespaces())
        self.assertEqual(
            self.mod._load_review_section()["active_namespaces"], ["default"])
        self.assertEqual(
            sel.return_value.log_api_access.call_args.kwargs["operation"],
            "delete_namespace")

    async def test_delete_leaves_an_unrelated_active_list_alone(self):
        learning.create_namespace("keep")
        learning.create_namespace("drop")
        self.mod._write_review_section({"active_namespaces": ["keep"]})
        with self._patch_audit():
            resp = await self.mod._handle_namespaces(
                _Req(method="DELETE", body={"name": "drop"}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            self.mod._load_review_section()["active_namespaces"], ["keep"])

    async def test_delete_refusal_does_not_prune(self):
        """`default` is undeletable, and a refused delete must not deactivate it."""
        with self._patch_audit():
            resp = await self.mod._handle_namespaces(
                _Req(method="DELETE", body={"name": "default"}))
        self.assertEqual(resp.status, 400)
        self.assertFalse(_body(resp)["ok"])
        self.assertIn("default", learning.list_namespaces())

    async def test_prune_failure_does_not_fail_the_delete(self):
        learning.create_namespace("doomed2")
        with self._patch_audit(), unittest.mock.patch.object(
                self.mod, "_load_review_section", side_effect=OSError("cfg gone")):
            resp = await self.mod._handle_namespaces(
                _Req(method="DELETE", body={"name": "doomed2"}))
        self.assertEqual(resp.status, 200)
        self.assertNotIn("doomed2", learning.list_namespaces())

    async def test_other_methods_are_405(self):
        resp = await self.mod._handle_namespaces(
            _Req(method="PATCH", body={"name": "proj-c"}))
        self.assertEqual(resp.status, 405)
        self.assertEqual(_body(resp)["code"], "method_not_allowed")


# ── learnings view ──

class TestLearningsHandler(_SageRoutesBase):
    async def test_defaults_to_the_default_namespace(self):
        resp = await self.mod._handle_learnings(_Req(method="GET"))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertEqual(payload["namespace"], learning.DEFAULT_NAMESPACE)
        self.assertEqual(payload["patterns"], [])
        self.assertEqual(payload["candidate"], [])
        self.assertFalse(payload["consolidating"])
        self.assertIsNone(payload["consolidate_error"])

    async def test_reports_in_flight_consolidation_state(self):
        self.mod._CONSOLIDATE_STATE["ns1"] = {"running": True, "error": None}
        resp = await self.mod._handle_learnings(
            _Req(method="GET", query={"namespace": "ns1"}))
        payload = _body(resp)
        self.assertEqual(payload["namespace"], "ns1")
        self.assertTrue(payload["consolidating"])

    async def test_reports_the_last_consolidation_error(self):
        self.mod._CONSOLIDATE_STATE["ns2"] = {"running": False, "error": "merge rejected"}
        resp = await self.mod._handle_learnings(
            _Req(method="GET", query={"namespace": "ns2"}))
        payload = _body(resp)
        self.assertFalse(payload["consolidating"])
        self.assertEqual(payload["consolidate_error"], "merge rejected")

    async def test_unreadable_pattern_and_candidate_files_read_as_empty(self):
        with unittest.mock.patch.object(
                self.mod.learning, "list_patterns", side_effect=OSError("boom")), \
                unittest.mock.patch.object(
                    self.mod.learning, "list_candidate", side_effect=OSError("boom")):
            resp = await self.mod._handle_learnings(_Req(method="GET"))
        payload = _body(resp)
        self.assertEqual(payload["patterns"], [])
        self.assertEqual(payload["candidate"], [])


# ── posting recorded findings to the pull request ──

class TestPostCommentsBg(_SageRoutesBase):
    def setUp(self):
        super().setUp()
        self.pool = _FakePool()

    def _stack(self, post_results, *, pending=0):
        """Stub the worker pool, the poster, and the two on-disk side effects."""
        calls: list[dict] = []

        def _post(cid, link, *, dispatch=None, run_id="", keys=None):
            calls.append({"cid": cid, "link": link, "keys": keys,
                          "dispatch": dispatch, "run_id": run_id})
            return dict(post_results.get(cid) or {"post_ok": True, "posted_keys": []})

        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "get_pool", return_value=self.pool))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "make_sync_dispatch",
            return_value="DISPATCH-SENTINEL"))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_driver, "post_recorded", side_effect=_post))
        stack.enter_context(unittest.mock.patch.object(
            self.mod, "_pending_comment_count", return_value=pending))
        stack.enter_context(unittest.mock.patch.object(
            self.mod, "_record_reviewed"))
        return stack, calls

    def _run(self, **kw):
        run = {
            "run_id": "P1",
            "status": "done",
            "posting": True,
            "changes": ["https://github.com/acme/repo/pull/1",
                        "https://github.com/acme/repo/pull/2"],
            "change_ids": ["GH-acme-repo-1", "GH-acme-repo-2"],
            "summary": {"per_change": [{"change_id": "GH-acme-repo-1"},
                                       {"change_id": "GH-acme-repo-2"}]},
        }
        run.update(kw)
        self.mod._RUNS = [run]
        return run

    async def test_posts_every_change_and_marks_the_run_delivered(self):
        run = self._run()
        results_by_cid = {
            "GH-acme-repo-1": {"post_ok": True, "posted_keys": ["k1", "k2"],
                               "posted_review_id": "rev-1"},
            "GH-acme-repo-2": {"post_ok": True, "posted_keys": ["k3"],
                               "posted_review_id": "rev-2"},
        }
        stack, calls = self._stack(results_by_cid)
        with stack:
            await self.mod._post_comments_bg("P1", run)
        self.assertEqual(len(calls), 2)
        # The poster receives the routes-level dispatch WRAPPER (model-bound),
        # not the raw pool dispatch; the wrapper's behavior is pinned by the
        # round-3 model-binding tests below.
        self.assertTrue(callable(calls[0]["dispatch"]))
        self.assertFalse(run["posting"])
        self.assertEqual(run["posted_comments"], 3)
        self.assertIsNone(run["post_error"])
        self.assertIsNotNone(run["posted_at"])
        self.assertEqual(run["posted_keys"]["GH-acme-repo-1"], ["k1", "k2"])
        self.assertEqual(run["posted_review_ids"]["GH-acme-repo-2"], "rev-2")
        self.assertEqual((self.pool.began, self.pool.ended), (1, 1))

    async def test_a_single_change_selection_leaves_the_others_alone(self):
        run = self._run()
        stack, calls = self._stack(
            {"GH-acme-repo-2": {"post_ok": True, "posted_keys": ["k9"]}})
        with stack:
            await self.mod._post_comments_bg(
                "P1", run, change_id="GH-acme-repo-2", keys=["k9"])
        self.assertEqual([c["cid"] for c in calls], ["GH-acme-repo-2"])
        self.assertEqual(calls[0]["keys"], ["k9"])
        self.assertEqual(run["posted_keys"], {"GH-acme-repo-2": ["k9"]})

    async def test_per_change_groups_carry_their_own_key_lists(self):
        run = self._run()
        stack, calls = self._stack({
            "GH-acme-repo-1": {"post_ok": True, "posted_keys": ["a"]},
        })
        with stack:
            await self.mod._post_comments_bg(
                "P1", run, groups={"GH-acme-repo-1": ["a"]})
        self.assertEqual([c["cid"] for c in calls], ["GH-acme-repo-1"])
        self.assertEqual(calls[0]["keys"], ["a"])

    async def test_change_id_is_derived_when_the_run_recorded_none(self):
        run = self._run(change_ids=[])
        stack, calls = self._stack({})
        with stack:
            await self.mod._post_comments_bg("P1", run)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertTrue(call["cid"])

    async def test_a_partial_post_is_not_marked_fully_posted(self):
        run = self._run()
        stack, _calls = self._stack(
            {"GH-acme-repo-1": {"post_ok": True, "posted_keys": ["k1"]}},
            pending=2)
        with stack:
            await self.mod._post_comments_bg("P1", run)
        self.assertIsNone(run["posted_at"])
        self.assertEqual(run["posted_comments"], 1)

    async def test_a_failed_post_is_reported_and_claims_are_released(self):
        run = self._run()
        self.mod._INFLIGHT["github.com/acme/repo#1"] = "P1"
        self.mod._STAGE_OWNER["github.com/acme/repo#1"] = "github.com/acme/repo#1"
        stack, _calls = self._stack({
            "GH-acme-repo-1": {"post_ok": False, "post_error": "gh 403"},
            "GH-acme-repo-2": {"post_ok": False},
        })
        with stack:
            await self.mod._post_comments_bg("P1", run)
        self.assertIn("gh 403", run["post_error"])
        self.assertIn("post failed", run["post_error"])
        self.assertEqual(self.mod._INFLIGHT, {})
        self.assertEqual(self.mod._STAGE_OWNER, {})

    async def test_pool_failure_is_recorded_on_the_run(self):
        run = self._run()
        self.mod._INFLIGHT["github.com/acme/repo#1"] = "P1"
        with unittest.mock.patch.object(
                self.mod.review_pool, "get_pool",
                side_effect=RuntimeError("no runtime")):
            await self.mod._post_comments_bg("P1", run)
        self.assertFalse(run["posting"])
        self.assertEqual(run["post_error"], "no runtime")
        self.assertEqual(self.mod._INFLIGHT, {})

    async def test_delivery_evidence_is_written_back_onto_the_records(self):
        """A retry must repair per_change, which is what the dedup index reads."""
        run = self._run()
        stack, _calls = self._stack({
            "GH-acme-repo-1": {"post_ok": True, "posted_keys": ["k1"],
                               "posted_comments": 1},
        })
        with stack:
            await self.mod._post_comments_bg("P1", run)
        rec = run["summary"]["per_change"][0]
        self.assertEqual(rec.get("posted_keys"), ["k1"])

    # ── round-3: the poster must carry the run's pinned model ──

    def _model_stack(self, dispatch_models: list):
        """Like _stack, but the poster records the model its dispatch names.

        make_sync_dispatch is stubbed with a factory whose returned dispatch
        appends every ``model`` it is called with, and post_recorded is driven
        for real (only its dispatch call is intercepted) so a
        post_kwargs["model"] regression would resurface as a TypeError.
        """

        def _post(change_id, link, *, dispatch, run_id="", keys=None, confirm=None):
            # Call the wrapper the way the real poster turn does (positional
            # task + timeout, no model of its own).
            dispatch("POST-TASK", 5)
            return {"post_ok": True, "posted_keys": []}

        def _factory(loop, pool, default_timeout=None):
            def dispatch(task, timeout=default_timeout or 30,
                         on_activity=None, keep_session_key=None, model=None):
                dispatch_models.append(model)
                return {"ok": True, "output": "", "error": ""}

            return dispatch

        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "get_pool", return_value=self.pool))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "make_sync_dispatch", side_effect=_factory))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_driver, "post_recorded", side_effect=_post))
        stack.enter_context(unittest.mock.patch.object(
            self.mod, "_pending_comment_count", return_value=0))
        stack.enter_context(unittest.mock.patch.object(self.mod, "_record_reviewed"))
        return stack

    async def test_an_explicit_model_run_posts_and_binds_the_pinned_model(self):
        # Regression: post_recorded has NO model parameter, so the old
        # post_kwargs["model"] raised TypeError on every explicit-model post;
        # and a plain make_sync_dispatch would acquire(None) against a batch
        # pinned to m1 and fail closed. Both are covered by this happy path.
        run = self._run(model="m1")
        dispatch_models: list = []
        with self._model_stack(dispatch_models):
            await self.mod._post_comments_bg("P1", run)
        self.assertFalse(run["posting"])
        self.assertIsNone(run["post_error"])
        self.assertEqual(self.pool.batch_models, ["m1"])
        # The dispatch the poster used bound the run's pinned model.
        self.assertEqual(dispatch_models, ["m1", "m1"])  # one per change

    async def test_a_task_level_model_beats_the_run_pinned_model(self):
        run = self._run(model="m1")
        # The driver hands a task-named model straight through dispatch().

        def _post(change_id, link, *, dispatch, run_id="", keys=None, confirm=None):
            dispatch("POST-TASK", 5, model="task-model")
            return {"post_ok": True, "posted_keys": []}

        with contextlib.ExitStack() as stack:
            stack.enter_context(unittest.mock.patch.object(
                self.mod.review_pool, "get_pool", return_value=self.pool))
            stack.enter_context(unittest.mock.patch.object(
                self.mod.review_driver, "post_recorded", side_effect=_post))
            stack.enter_context(unittest.mock.patch.object(
                self.mod, "_pending_comment_count", return_value=0))
            stack.enter_context(unittest.mock.patch.object(self.mod, "_record_reviewed"))

            real_dispatches: list = []

            def _factory(loop, pool, default_timeout=None):
                def dispatch(task, timeout=default_timeout or 30,
                             on_activity=None, keep_session_key=None, model=None):
                    real_dispatches.append(model)
                    return {"ok": True, "output": "", "error": ""}
                return dispatch

            with unittest.mock.patch.object(
                    self.mod.review_pool, "make_sync_dispatch",
                    side_effect=_factory):
                await self.mod._post_comments_bg("P1", run)

        self.assertEqual(real_dispatches, ["task-model", "task-model"])

    async def test_a_no_model_run_passes_model_none_through(self):
        run = self._run()
        dispatch_models: list = []
        with self._model_stack(dispatch_models):
            await self.mod._post_comments_bg("P1", run)
        self.assertFalse(run["posting"])
        self.assertIsNone(run["post_error"])
        self.assertEqual(self.pool.batch_models, [None])
        self.assertEqual(dispatch_models, [None, None])  # one per change

    async def test_post_recorded_has_no_model_parameter(self):
        # Regression pin on the signature itself: the TypeError the old caller
        # hit must stay loud if the parameter is (re)added accidentally rather
        # than plumbed.
        import inspect

        self.assertNotIn(
            "model", inspect.signature(self.mod.review_driver.post_recorded).parameters)


# ── the review run itself must carry the run's pinned model ──

class TestRunReviewBgModelBinding(_SageRoutesBase):
    """``run_review`` takes no model parameter, so the pin travels through the
    dispatch the driver is handed. Same contract the poster path pins above:
    a bare ``make_sync_dispatch`` acquires None against a batch pinned to m1,
    so an explicit-model review used to die on its first dispatched task."""

    def setUp(self):
        super().setUp()
        self.pool = _FakePool()

    def _model_stack(self, dispatch_models: list):
        """Stub the pool, the preflight, and the driver; record the model each
        dispatched review task carried."""

        def _run_review(changes, *, dispatch=None, progress=None, run_id="",
                        cancelled=None, preflight=None, concurrency=1):
            dispatch("REVIEW-TASK", 5)
            return {"ok": True, "changes": len(changes), "cancelled": 0,
                    "result_records": len(changes), "deep_reviewed": len(changes)}

        def _factory(loop, pool, default_timeout=None):
            def dispatch(task, timeout=default_timeout or 30,
                         on_activity=None, keep_session_key=None, model=None):
                dispatch_models.append(model)
                return {"ok": True, "output": "", "error": ""}

            return dispatch

        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "get_pool", return_value=self.pool))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "runtime_preflight", return_value=""))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_pool, "make_sync_dispatch", side_effect=_factory))
        stack.enter_context(unittest.mock.patch.object(
            self.mod.review_driver, "run_review", side_effect=_run_review))
        stack.enter_context(unittest.mock.patch.object(self.mod, "_record_reviewed"))
        return stack

    def _run(self, **kw):
        run = {
            "run_id": "R1",
            "status": "running",
            "changes": ["https://github.com/acme/repo/pull/1"],
        }
        run.update(kw)
        self.mod._RUNS = [run]
        return run

    async def test_an_explicit_model_run_binds_the_pinned_model(self):
        run = self._run(model="m1")
        dispatch_models: list = []
        with self._model_stack(dispatch_models):
            await self.mod._run_review_bg(run, list(run["changes"]))
        self.assertEqual(run["status"], "done")
        self.assertEqual(self.pool.batch_models, ["m1"])
        # The dispatch the driver used bound the run's pinned model.
        self.assertEqual(dispatch_models, ["m1"])

    async def test_a_no_model_run_passes_model_none_through(self):
        run = self._run()
        dispatch_models: list = []
        with self._model_stack(dispatch_models):
            await self.mod._run_review_bg(run, list(run["changes"]))
        self.assertEqual(run["status"], "done")
        self.assertEqual(self.pool.batch_models, [None])
        self.assertEqual(dispatch_models, [None])


# ── remaining request-guard branches on the neighbouring handlers ──

class TestReviewKickoffGuards(_SageRoutesBase):
    def _patch_driver(self):
        async def _noop(run, changes):
            return None
        return unittest.mock.patch.object(
            self.mod, "_run_review_bg", side_effect=_noop)

    async def test_unreadable_body_is_a_400_with_a_paste_hint(self):
        resp = await self.mod._handle_review(_Req(method="POST", body=None))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "no_reviewable_changes")

    async def test_non_dict_body_is_treated_as_empty(self):
        resp = await self.mod._handle_review(_Req(method="POST", body=["links"]))
        self.assertEqual(resp.status, 400)

    async def test_blank_change_entries_are_dropped(self):
        resp = await self.mod._handle_review(
            _Req(method="POST", body={"changes": ["  ", ""]}))
        self.assertEqual(resp.status, 400)

    async def test_non_list_changes_falls_through_to_the_pasted_links(self):
        with self._patch_driver():
            resp = await self.mod._handle_review(_Req(
                method="POST",
                body={"changes": "not a list",
                      "links": "https://github.com/acme/repo/pull/5"}))
            await asyncio.sleep(0)
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertEqual(payload["changes"], ["https://github.com/acme/repo/pull/5"])
        self.assertEqual(payload["status"], "running")

    async def test_the_input_alias_is_accepted_for_pasted_links(self):
        with self._patch_driver():
            resp = await self.mod._handle_review(_Req(
                method="POST", body={"input": "https://github.com/acme/repo/pull/6"}))
            await asyncio.sleep(0)
        self.assertEqual(_body(resp)["changes"],
                         ["https://github.com/acme/repo/pull/6"])


class TestPinnedReposGuards(_SageRoutesBase):
    async def test_get_returns_the_pinned_list(self):
        with unittest.mock.patch.object(
                self.mod.discovery, "read_repos",
                return_value=[{"owner": "acme", "repo": "repo"}]):
            resp = await self.mod._handle_repos(_Req(method="GET"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(_body(resp)["repos"]), 1)

    async def test_unreadable_body_is_a_400(self):
        resp = await self.mod._handle_repos(_Req(method="POST", body=None))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_non_dict_body_is_a_400(self):
        resp = await self.mod._handle_repos(_Req(method="POST", body=["acme"]))
        self.assertEqual(resp.status, 400)

    async def test_an_owner_repo_pair_must_pass_the_same_allowlist(self):
        resp = await self.mod._handle_repos(
            _Req(method="POST", body={"owner": "acme/../etc", "repo": "repo"}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_repo")


class TestRunArchiveSuccess(_SageRoutesBase):
    async def test_archiving_records_the_slug_on_the_run(self):
        self.mod._RUNS = [{"run_id": "ar1", "status": "done"}]
        with unittest.mock.patch.object(
                self.mod.report, "read_within_reports", return_value="<html>r</html>"), \
                unittest.mock.patch.object(
                    self.mod.review_driver, "archive_report",
                    return_value="sage-report-ar1") as archive, \
                unittest.mock.patch.object(
                    self.mod.report, "set_report_slug") as set_slug:
            resp = await self.mod._handle_run_archive(
                _Req(run_id="ar1", method="POST"))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["report_slug"], "sage-report-ar1")
        self.assertEqual(self.mod._find_run("ar1")["report_slug"], "sage-report-ar1")
        archive.assert_called_once()
        set_slug.assert_called_once()


class TestConsolidateGuards(_SageRoutesBase):
    async def test_non_dict_body_falls_back_to_the_default_namespace(self):
        resp = await self.mod._handle_consolidate(_Req(method="POST", body=["ns"]))
        # No candidates are staged in a fresh data dir, so the guard refuses.
        self.assertEqual(resp.status, 409)
        self.assertEqual(_body(resp)["code"], "nothing_to_consolidate")
        self.assertNotIn(learning.DEFAULT_NAMESPACE, self.mod._CONSOLIDATING)

    async def test_an_invalid_namespace_name_is_a_400(self):
        resp = await self.mod._handle_consolidate(
            _Req(method="POST", body={"namespace": "../escape"}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_namespace")

    async def test_a_second_request_for_one_namespace_is_a_409(self):
        self.mod._CONSOLIDATING.add(learning.DEFAULT_NAMESPACE)
        resp = await self.mod._handle_consolidate(_Req(method="POST", body={}))
        self.assertEqual(resp.status, 409)
        self.assertEqual(_body(resp)["code"], "consolidation_in_progress")

    async def test_a_failing_staged_count_gives_the_claim_back(self):
        with unittest.mock.patch.object(
                self.mod.learning, "candidate_count", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                await self.mod._handle_consolidate(_Req(method="POST", body={}))
        self.assertNotIn(learning.DEFAULT_NAMESPACE, self.mod._CONSOLIDATING)

    async def test_staged_candidates_start_a_merge_task(self):
        async def _noop(ns):
            return None
        with unittest.mock.patch.object(
                self.mod.learning, "candidate_count", return_value=3), \
                unittest.mock.patch.object(
                    self.mod, "_consolidate_bg", side_effect=_noop):
            resp = await self.mod._handle_consolidate(_Req(method="POST", body={}))
            await asyncio.sleep(0)
        payload = _body(resp)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["staged"], 3)
        self.assertTrue(
            self.mod._CONSOLIDATE_STATE[learning.DEFAULT_NAMESPACE]["running"])


# ── route registration ──

class TestRegisterRoutes(_SageRoutesBase):
    def test_registers_every_endpoint_and_caches_the_dashboard_state(self):
        app = web.Application()
        notifier = _Notifier()
        app["state"] = notifier
        self.mod.register_routes(app)
        paths = {
            getattr(res, "canonical", "")
            for res in app.router.resources()
        }
        for expected in (
            "/api/apps/code-review-sage/review",
            "/api/apps/code-review-sage/review-repo",
            "/api/apps/code-review-sage/repo-prs",
            "/api/apps/code-review-sage/runs",
            "/api/apps/code-review-sage/runs/{run_id}",
            "/api/apps/code-review-sage/settings",
            "/api/apps/code-review-sage/namespaces",
            "/api/apps/code-review-sage/learnings",
            "/api/apps/code-review-sage/learnings/consolidate",
        ):
            self.assertIn(expected, paths)
        self.assertIs(self.mod._APP_STATE["state"], notifier)
        self.assertTrue(app.on_startup)
        self.assertTrue(app.on_cleanup)

    def test_a_bare_app_without_state_still_registers(self):
        app = web.Application()
        self.mod.register_routes(app)
        self.assertNotIn("state", self.mod._APP_STATE)

    @staticmethod
    def _startup_hook(app, name):
        """Return the ``on_startup`` hook registered under ``name``.

        Selecting by position is what broke here: ``register_routes`` appends
        ``_start_chat_sweeper`` after ``_reap_on_startup``, so ``[-1]`` awaited
        the sweeper instead. That failed one test outright and made its sibling
        pass vacuously (awaiting a hook that cannot raise proves nothing about
        the reaper's error handling). Naming the hook keeps both honest no
        matter how many hooks are appended later.
        """
        for hook in app.on_startup:
            if getattr(hook, "__name__", "") == name:
                return hook
        raise AssertionError(f"no on_startup hook named {name!r}")

    async def test_startup_hook_reaps_orphan_run_dirs_off_the_loop(self):
        app = web.Application()
        self.mod.register_routes(app)
        with unittest.mock.patch.object(
                self.mod, "_reap_orphan_run_dirs", return_value=2) as reap:
            await self._startup_hook(app, "_reap_on_startup")(app)
        reap.assert_called_once_with()

    async def test_startup_hook_swallows_a_reap_failure(self):
        app = web.Application()
        self.mod.register_routes(app)
        with unittest.mock.patch.object(
                self.mod, "_reap_orphan_run_dirs", side_effect=OSError("boom")):
            await self._startup_hook(app, "_reap_on_startup")(app)

    async def test_cleanup_hook_retires_the_review_pool(self):
        app = web.Application()
        self.mod.register_routes(app)
        shutdown = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(
                self.mod.review_pool, "shutdown_pool", shutdown):
            await app.on_cleanup[-1](app)
        shutdown.assert_awaited_once()

    async def test_cleanup_hook_swallows_a_shutdown_failure(self):
        app = web.Application()
        self.mod.register_routes(app)
        with unittest.mock.patch.object(
                self.mod.review_pool, "shutdown_pool",
                unittest.mock.AsyncMock(side_effect=RuntimeError("stuck"))):
            await app.on_cleanup[-1](app)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
