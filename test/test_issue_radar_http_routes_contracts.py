"""Behaviour contracts of Issue Radar's route layer that a code move could bend.

Each test pins something the existing suites leave implicit and that silently
survives a refactor when it breaks: the exact bytes of the four model prompts and
the persisted PR-summary fingerprint, the background dependency refresh's
lifecycle per aiohttp app, the probe memo's edge cases, the lock discipline of the
issue writers, the privileged merge's head pin and SEL taxonomy, the error bodies
each route returns, and the logger every route writes to.

Everything is patched at the ``routes`` / ``store`` / ``provider`` / model
boundary; nothing spawns ``gh``, calls a model, or writes outside the per-test
``KIROCREW_HOME``. Concurrency tests hold the shared operation open on an event
rather than sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import llm_helpers
from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store

BASE = "/api/apps/issue-radar"
SHA = "a" * 40
OTHER_SHA = "b" * 40
KEY = provider.key_from_parts("o", "r")
REG = "github:github.com:o/r"
LOGGER = "kirocrew.app.issue-radar"


def _get(path: str, query: dict | None = None, app: web.Application | None = None) -> web.Request:
    full = f"{BASE}/{path}"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full, app=app or web.Application())


def _post(path: str, body: object, app: web.Application | None = None) -> web.Request:
    req = make_mocked_request("POST", f"{BASE}/{path}", app=app or web.Application())
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── the prompts and the fingerprint are frozen byte-for-byte ────────────────────

LABELS = [{"name": "bug", "description": "a defect"}, {"name": "docs", "description": ""}]
DETAIL = {
    "number": 7,
    "title": "crash on start",
    "body": "boom\nline2 " + "x" * 7000,
    "labels": [{"name": "bug"}],
}
PR = {
    "number": 12,
    "title": "fix it",
    "body": "desc",
    "state": "open",
    "draft": False,
    "head": "feat",
    "base": "main",
    "additions": 3,
    "deletions": 1,
    "changed_files": 2,
    "commits": 1,
    "author": "alice",
    "head_sha": SHA,
    "updated_at": "2026-01-02T00:00:00Z",
    "merged_at": None,
}
TIMELINE = [
    {"kind": "comment", "actor": "bob", "created_at": "2026-01-01T00:00:00Z", "body": "looks good"},
    {
        "kind": "reviewed",
        "actor": "carol",
        "created_at": "2026-01-01T01:00:00Z",
        "review_state": "CHANGES_REQUESTED",
        "body": "",
    },
    {
        "kind": "review_comment",
        "actor": "dan",
        "created_at": "2026-01-01T02:00:00Z",
        "body": "nit",
        "path": "a.py",
        "line": 3,
    },
    {"kind": "labeled", "actor": "x", "created_at": "2026-01-01T03:00:00Z"},
]
CHECKS = [{"name": "ci", "bucket": "failure"}, {"name": "lint", "bucket": "pass"}]
ISSUES = [
    {"number": 3, "title": "t3", "body": "b" * 500, "labels": ["bug"]},
    {"number": 4, "title": "t4", "body": "c\nd", "labels": []},
]


class TestPromptBytesAreFrozen(unittest.TestCase):
    """The prompts are long implicit concatenations; moving them between files is
    exactly how a space, a newline or the fence placement changes unnoticed. The
    digests were recorded from the code before the route layer was split."""

    def test_issue_triage_prompt(self):
        prompt = routes._build_ai_prompt("o", "r", DETAIL, LABELS, ["bug"])
        self.assertEqual(
            _sha256(prompt), "c1ea2248b81904bbdcd6a8c7e16211128793535d72108beecf2b1f9823fcfbe9"
        )
        self.assertEqual(
            _sha256(routes._build_ai_prompt("o", "r", DETAIL, LABELS, ["bug"], ui_language="ko")),
            "531ac4c5a7dc8e7d0676c597e4795ec7f053658f76d0f80f67ba93bbc4c0b912",
        )

    def test_pr_summary_prompt(self):
        self.assertEqual(
            _sha256(routes._build_pr_ai_prompt("o", "r", PR, TIMELINE, CHECKS)),
            "89b9be33659b88cc942adca60795f351bd1bc1025da5d7676540595770eebc60",
        )
        self.assertEqual(
            _sha256(routes._build_pr_ai_prompt("o", "r", PR, TIMELINE, CHECKS, ui_language="ko")),
            "87335d726690c849ef9697d5dd840e34579900bcc4b1430b12e5f19b153580a1",
        )

    def test_recommendation_prompt(self):
        self.assertEqual(
            _sha256(routes._build_reco_prompt("o", "r", LABELS, ISSUES)),
            "09cc5f3b9b0fb87abc202850275382d00c277e7737f15dde66541161fe8671a7",
        )
        self.assertEqual(
            _sha256(routes._build_reco_prompt("o", "r", LABELS, ISSUES, ui_language="ko")),
            "6857509a727204e59a927ba0bf1c20220b9faf90f1399642f997e440fa73fede",
        )

    def test_tagging_prompt(self):
        self.assertEqual(
            _sha256(routes._build_tagging_prompt("o", "r", LABELS, ISSUES)),
            "1bf49ad5f9908e61e818d1209691030818e16f067a4f17829fdefc574974ab30",
        )
        self.assertEqual(
            _sha256(routes._build_tagging_prompt("o", "r", LABELS, ISSUES, ui_language="ko")),
            "89a0dcede09648700c0a7eed43de7f12635942fd1dace68233f5fa5cc6a4c9e9",
        )

    def test_the_persisted_pr_fingerprint(self):
        # Stored beside every cached PR summary: a changed digest would make every
        # cache entry miss on upgrade and pay a fresh model call per open PR.
        self.assertEqual(
            routes._pr_ai_fingerprint(PR, TIMELINE, CHECKS), "8bb1ad877819352242fb23b3ff138380"
        )
        self.assertEqual(
            routes._pr_ai_fingerprint(PR, TIMELINE, CHECKS, ui_language="ko"),
            "f6ef27e0f3aa8742e30ccbc91943d6c3",
        )


# ── the background dependency refresh, per aiohttp app ──────────────────────────


class TestDepsRefreshLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_registries_are_per_app_and_cleanup_is_scoped_to_its_app(self):
        release = asyncio.Event()

        async def _slow(app, key):
            await release.wait()
            return {}

        a1, a2 = web.Application(), web.Application()
        with mock.patch.object(routes, "_rebuild_deps", side_effect=_slow):
            routes._schedule_deps_refresh(a1, KEY)
            routes._schedule_deps_refresh(a1, KEY)  # coalesced
            routes._schedule_deps_refresh(a2, KEY)
            t1 = a1[routes._DEPS_REFRESH_TASKS_APP_KEY][REG]
            t2 = a2[routes._DEPS_REFRESH_TASKS_APP_KEY][REG]
            self.assertIsNot(t1, t2)
            self.assertEqual(len(a1[routes._DEPS_REFRESH_TASKS_APP_KEY]), 1)
            self.assertEqual(t1.get_name(), f"issue-radar-deps-refresh:{REG}")
            await routes._stop_deps_refreshes(a1)
            self.assertTrue(t1.cancelled())
            self.assertFalse(t2.done())
            self.assertEqual(a1[routes._DEPS_REFRESH_TASKS_APP_KEY], {})
            release.set()
            await asyncio.wait_for(t2, 5)
        self.assertEqual(a2[routes._DEPS_REFRESH_TASKS_APP_KEY], {})
        self.assertIsNot(routes._deps_rebuild_lock(a1, KEY), routes._deps_rebuild_lock(a2, KEY))
        self.assertIs(routes._deps_rebuild_lock(a1, KEY), routes._deps_rebuild_lock(a1, KEY))

    async def test_stop_is_a_no_op_on_an_idle_app(self):
        self.assertIsNone(await routes._stop_deps_refreshes(web.Application()))
        app = web.Application()
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        app[routes._DEPS_REFRESH_TASKS_APP_KEY] = {REG: done}
        await asyncio.wait_for(routes._stop_deps_refreshes(app), 5)

    async def test_a_failed_background_refresh_is_logged_and_frees_its_slot(self):
        app = web.Application()
        with (
            mock.patch.object(
                routes, "_rebuild_deps", AsyncMock(side_effect=gh.GhCliError("boom"))
            ),
            self.assertLogs(LOGGER, level="DEBUG") as logs,
        ):
            routes._schedule_deps_refresh(app, KEY)
            task = app[routes._DEPS_REFRESH_TASKS_APP_KEY][REG]
            await asyncio.wait_for(task, 5)
        self.assertIsNone(task.exception())
        self.assertEqual(app[routes._DEPS_REFRESH_TASKS_APP_KEY], {})
        self.assertIn(
            "issue-radar: background deps refresh failed for o/r; keeping cached graph",
            [r.getMessage() for r in logs.records],
        )

    async def test_register_routes_hooks_the_refresh_shutdown_after_the_watcher(self):
        app = web.Application()
        with mock.patch.object(routes, "watch") as watch:
            routes.register_routes(app)
        cleanup = list(app.on_cleanup)
        self.assertEqual(cleanup.count(routes._stop_deps_refreshes), 1)
        self.assertLess(
            cleanup.index(watch.stop_watcher), cleanup.index(routes._stop_deps_refreshes)
        )
        self.assertIn(watch.start_watcher, list(app.on_startup))

    async def test_the_rebuild_returns_the_stored_normalized_graph(self):
        app = web.Application()
        stored = {"edges": [{"blocked": 2, "blocker": 1, "source": "native"}], "nodes": {}}
        fetch = MagicMock(return_value=([{"blocked": 2, "blocker": 1, "source": "inferred"}], {}))
        with (
            mock.patch.object(routes, "_load_open_issues_for_reco", AsyncMock(return_value=[])),
            mock.patch.object(store, "read_pulls_cache", return_value=None),
            mock.patch.object(gh, "fetch_dependency_edges", fetch),
            mock.patch.object(store, "write_deps_cache") as write,
            mock.patch.object(store, "read_deps_cache", return_value=stored),
        ):
            self.assertEqual(await routes._rebuild_deps(app, KEY), stored)
        self.assertIn("fetched_at", write.call_args.kwargs)
        self.assertEqual(write.call_args.kwargs["root"], routes._scope(KEY))

    async def test_an_unconnected_non_github_repo_is_404_not_an_empty_graph(self):
        with mock.patch.object(store, "is_repo_connected", return_value=False) as gate:
            resp = await routes._handle_deps(
                _get(
                    "deps", {"owner": "o", "repo": "r", "provider": "gitlab", "host": "gitlab.com"}
                )
            )
        self.assertEqual((resp.status, _body(resp)["code"]), (404, "repo_not_connected"))
        gate.assert_called_once_with("o", "r", provider="gitlab", host="gitlab.com")


# ── the probe memo's edges ─────────────────────────────────────────────────────


READING = {"total_count": 3, "top_updated_at": "2026-01-01T00:00:00Z"}
SNAPSHOT = {"rows": [], "probe": READING, "age_sec": 0}


class TestProbeCoalescing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        routes._probe_memo.clear()
        routes._probe_inflight.clear()
        self.addCleanup(routes._probe_memo.clear)
        self.addCleanup(routes._probe_inflight.clear)

    async def test_a_failed_probe_is_neither_memoized_nor_left_in_flight(self):
        probe = MagicMock(side_effect=[gh.GhCliError("quota"), dict(READING)])
        with mock.patch.object(gh, "probe_open_list", probe):
            first = await routes._poll_can_serve_cache(KEY, "issue", "open", SNAPSHOT)
            second = await routes._poll_can_serve_cache(KEY, "issue", "open", SNAPSHOT)
        self.assertEqual(first, (True, None))
        self.assertEqual(second, (True, READING))
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(routes._probe_inflight, {})

    async def test_concurrent_polls_share_one_in_flight_probe_under_its_full_key(self):
        started, release = threading.Event(), threading.Event()

        def _probe(owner, repo, kind, **kwargs):
            started.set()
            release.wait(5)
            return dict(READING)

        with mock.patch.object(gh, "probe_open_list", side_effect=_probe) as probe:
            first = asyncio.ensure_future(
                routes._poll_can_serve_cache(KEY, "issue", "open", SNAPSHOT)
            )
            await asyncio.to_thread(started.wait, 5)
            self.assertEqual(
                list(routes._probe_inflight), [("github", "github.com", "o", "r", "issue")]
            )
            second = asyncio.ensure_future(
                routes._poll_can_serve_cache(KEY, "issue", "open", SNAPSHOT)
            )
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertEqual(results, [(True, READING), (True, READING)])
        self.assertEqual(probe.call_count, 1)

    async def test_the_memo_never_crosses_providers_and_folds_case_per_provider(self):
        gitlab_probe = MagicMock(return_value=dict(READING))
        with (
            mock.patch.object(gh, "probe_open_list", return_value=dict(READING)) as github_probe,
            mock.patch(
                "kiro_crew.apps.builtins.issue_radar.backend.gitlab_client.probe_open_list",
                gitlab_probe,
            ),
        ):
            await routes._poll_can_serve_cache(
                provider.key_from_parts("Acme", "W"), "issue", "open", SNAPSHOT
            )
            await routes._poll_can_serve_cache(
                provider.key_from_parts("acme", "w"), "issue", "open", SNAPSHOT
            )
            await routes._poll_can_serve_cache(
                provider.key_from_parts("acme", "w", "gitlab", "gitlab.com"),
                "issue",
                "open",
                SNAPSHOT,
            )
            await routes._poll_can_serve_cache(
                provider.key_from_parts("acme", "W", "gitlab", "gitlab.com"),
                "issue",
                "open",
                SNAPSHOT,
            )
        self.assertEqual(github_probe.call_count, 1)
        self.assertEqual(github_probe.call_args.args[:3], ("Acme", "W", "issue"))
        self.assertEqual(gitlab_probe.call_count, 2)
        self.assertEqual(gitlab_probe.call_args.kwargs, {"host": "gitlab.com"})

    async def test_remember_probe_ignores_cancelled_and_foreign_futures(self):
        loop = asyncio.get_running_loop()
        k = ("github", "github.com", "o", "r", "issue")
        cancelled = loop.create_future()
        cancelled.cancel()
        routes._probe_inflight[k] = cancelled
        routes._remember_probe(k, cancelled)
        self.assertNotIn(k, routes._probe_inflight)
        self.assertNotIn(k, routes._probe_memo)
        foreign = loop.create_future()
        mine = loop.create_future()
        mine.set_result(dict(READING))
        routes._probe_inflight[k] = foreign
        routes._remember_probe(k, mine)
        self.assertIs(routes._probe_inflight[k], foreign)
        self.assertEqual(routes._probe_memo[k][1], READING)
        foreign.cancel()


# ── the issue writers hold one lock across provider call and cache patch ─────────


class TestIssueWriteLocking(unittest.TestCase):
    def _recording_lock(self, events: list):
        @contextlib.contextmanager
        def _lock(*args):
            events.append(("enter", args))
            try:
                yield
            finally:
                events.append(("exit", args))

        return _lock

    def test_label_change_removes_then_adds_and_patches_inside_one_hold(self):
        events: list = []
        client = MagicMock()
        client.remove_issue_label.side_effect = lambda *a, **k: events.append(("remove", a[3])) or [
            {"name": "old"}
        ]
        client.add_issue_labels.side_effect = lambda *a, **k: events.append(
            ("add", tuple(a[3]))
        ) or [{"name": "new"}]
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(store, "issue_write_lock", self._recording_lock(events)),
            mock.patch.object(
                store,
                "apply_label_change_to_caches",
                side_effect=lambda *a, **k: events.append(
                    ("patch", tuple(x["name"] for x in a[3]))
                ),
            ),
        ):
            final = routes._apply_label_change(KEY, 7, ["new"], ["a", "b"])
        self.assertEqual(final, [{"name": "new"}])
        scope = routes._scope(KEY)
        self.assertEqual(
            events,
            [
                ("enter", ("o", "r", 7, scope)),
                ("remove", "a"),
                ("remove", "b"),
                ("add", ("new",)),
                ("patch", ("new",)),
                ("exit", ("o", "r", 7, scope)),
            ],
        )

    def test_a_cache_patch_failure_is_not_a_failed_write(self):
        client = MagicMock()
        client.add_issue_labels.return_value = [{"name": "bug"}]
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(store, "issue_write_lock", self._recording_lock([])),
            mock.patch.object(store, "apply_label_change_to_caches", side_effect=OSError("disk")),
            self.assertLogs(LOGGER, level="WARNING") as logs,
        ):
            self.assertEqual(routes._apply_label_change(KEY, 7, ["bug"], []), [{"name": "bug"}])
        self.assertTrue(any("cache patch failed" in r.getMessage() for r in logs.records))

    def test_assignee_replacement_reads_compares_and_writes_under_one_hold(self):
        events: list = []
        client = MagicMock()
        client.get_issue_detail.side_effect = lambda *a, **k: events.append(("read",)) or {
            "assignees": ["Alice"]
        }
        client.set_issue_assignees.side_effect = lambda *a, **k: events.append(
            ("write", tuple(a[3]))
        ) or ["alice"]
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(store, "issue_write_lock", self._recording_lock(events)),
            mock.patch.object(store, "apply_assignees_change_to_caches") as patch_cache,
        ):
            ok = routes._replace_assignees_checked(KEY, 7, ["alice"], ["alice", "bob"])
            stale = routes._replace_assignees_checked(KEY, 7, ["carol"], ["bob"])
        self.assertEqual(ok, (["alice"], ["Alice"]))
        self.assertEqual(stale, (None, ["Alice"]))
        self.assertEqual(patch_cache.call_args.args[3], ["alice"])
        self.assertEqual(
            [e[0] for e in events], ["enter", "read", "write", "exit", "enter", "read", "exit"]
        )


# ── the privileged merge: head pin, readiness gate and SEL taxonomy ─────────────


class TestMergeContract(unittest.IsolatedAsyncioTestCase):
    def _gates(self):
        return contextlib.ExitStack()

    async def _merge(self, detail, *, merge_result=None, merge_exc=None, body_extra=None):
        audit = MagicMock()
        body = {"owner": "o", "repo": "r", "number": 7, "head_sha": SHA, "method": "merge"}
        body.update(body_extra or {})
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit", audit),
            mock.patch.object(gh, "get_pr_detail", return_value=detail) as read,
            mock.patch.object(
                gh,
                "merge_pull_request",
                side_effect=merge_exc,
                return_value=merge_result or {"merged": True, "sha": "m", "message": ""},
            ) as merge,
            mock.patch.object(store, "apply_pr_state_change_to_caches") as evict,
        ):
            resp = await routes._handle_pull_merge(_post("pull/merge", body))
        return resp, audit, read, merge, evict

    async def test_a_ready_pr_merges_pinned_to_the_reviewed_head(self):
        resp, audit, read, merge, evict = await self._merge(
            {"mergeable_state": "CLEAN", "head_sha": SHA.upper()}
        )
        self.assertEqual(resp.status, 200, _body(resp))
        self.assertEqual(read.call_args.args, ("o", "r", 7))
        self.assertNotIn("resolve_mergeable", read.call_args.kwargs)
        self.assertEqual(merge.call_args.args, ("o", "r", 7, "MERGE", SHA))
        self.assertEqual(evict.call_args.args[:4], ("o", "r", 7, "closed"))
        audit.assert_called_once_with("pull_merge", "o/r#7", "ok")

    async def test_an_unknown_live_head_does_not_refuse(self):
        resp, *_rest = await self._merge({"mergeable_state": "clean", "head_sha": ""})
        self.assertEqual(resp.status, 200)

    async def test_refusals_and_their_audit_outcomes(self):
        cases = [
            (
                {"mergeable_state": "blocked", "head_sha": SHA},
                {},
                409,
                "merge_not_ready",
                "denied",
                "mergeable_state=blocked",
            ),
            (
                {"mergeable_state": "clean", "head_sha": OTHER_SHA},
                {},
                409,
                "merge_conflict",
                "denied",
                f"head moved: reviewed={SHA} live={OTHER_SHA}",
            ),
            (
                {"mergeable_state": "clean", "head_sha": SHA},
                {"merge_exc": gh.GhCliError("HTTP 405: not allowed")},
                409,
                "merge_not_allowed",
                "denied",
                "HTTP 405: not allowed",
            ),
            (
                {"mergeable_state": "clean", "head_sha": SHA},
                {"merge_exc": gh.GhCliError("405 Method Not Allowed")},
                409,
                "merge_not_allowed",
                "denied",
                "405 Method Not Allowed",
            ),
            (
                {"mergeable_state": "clean", "head_sha": SHA},
                {"merge_exc": gh.GhCliError("gh: HTTP 409 head moved")},
                409,
                "merge_conflict",
                "failure",
                "gh: HTTP 409 head moved",
            ),
            (
                {"mergeable_state": "clean", "head_sha": SHA},
                {"merge_exc": gh.GhPermissionError("no push")},
                403,
                "provider_forbidden",
                "denied",
                "no push",
            ),
            (
                {"mergeable_state": "clean", "head_sha": SHA},
                {"merge_result": {"merged": False, "message": "approvals missing"}},
                502,
                "provider_error",
                "failure",
                "approvals missing",
            ),
        ]
        for detail, kwargs, status, code, outcome, error in cases:
            with self.subTest(code=code, error=error):
                resp, audit, _read, _merge, evict = await self._merge(detail, **kwargs)
                self.assertEqual((resp.status, _body(resp)["code"]), (status, code))
                audit.assert_called_once_with("pull_merge", "o/r#7", outcome, error=error)
                evict.assert_not_called()

    async def test_the_review_head_check_skips_the_mergeability_retry_and_audits_a_move(self):
        audit = MagicMock()
        with (
            mock.patch.object(routes, "_audit", audit),
            mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": OTHER_SHA}) as read,
        ):
            resp = await routes._refuse_if_head_moved(KEY, 7, SHA, "pulls_bulk")
        assert resp is not None
        self.assertEqual((resp.status, _body(resp)["code"]), (409, "review_conflict"))
        self.assertIs(read.call_args.kwargs["resolve_mergeable"], False)
        audit.assert_called_once_with(
            "pulls_bulk", "o/r#7", "denied", error=f"head moved: reviewed={SHA} live={OTHER_SHA}"
        )


class TestPrActionDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_each_verb_calls_its_provider_function_off_the_loop_with_the_key_host(self):
        key = provider.key_from_parts("g", "p", "gitlab", "gitlab.example.com")
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def _rec(result):
            def _inner(*args, **kwargs):
                threads.append(threading.get_ident())
                return result

            return _inner

        client = MagicMock()
        client.set_pr_state.side_effect = _rec({"state": "open"})
        client.merge_pull_request.side_effect = _rec({"merged": True})
        for name in (
            "submit_pr_review",
            "add_pr_comment",
            "enable_auto_merge",
            "disable_auto_merge",
            "cancel_workflow_run",
            "rerun_workflow_run",
        ):
            getattr(client, name).side_effect = _rec({})
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(store, "apply_pr_state_change_to_caches") as state_patch,
            mock.patch.object(store, "drop_pr_detail_cache") as drop,
        ):
            await routes._run_pr_action(key, "close", 7)
            await routes._run_pr_action(key, "approve", 7, body="lgtm", head_sha=SHA)
            await routes._run_pr_action(key, "comment", 7, body="hi")
            await routes._run_pr_action(key, "merge", 7, method="SQUASH", head_sha=SHA)
            await routes._run_pr_action(key, "auto_merge", 7, method="REBASE")
            await routes._run_pr_action(key, "cancel_auto_merge", 7)
            await routes._run_pr_action(key, "cancel_run", 7, run_id=11)
            await routes._run_pr_action(key, "rerun_run", 7, run_id=11, failed_only=True)
            with self.assertRaises(ValueError):
                await routes._run_pr_action(key, "explode", 7)
        host = {"host": "gitlab.example.com"}
        self.assertEqual(client.set_pr_state.call_args, mock.call("g", "p", 7, "closed", **host))
        self.assertEqual(
            client.submit_pr_review.call_args,
            mock.call("g", "p", 7, "APPROVE", "lgtm", SHA, **host),
        )
        self.assertEqual(client.add_pr_comment.call_args, mock.call("g", "p", 7, "hi", **host))
        self.assertEqual(
            client.merge_pull_request.call_args, mock.call("g", "p", 7, "SQUASH", SHA, **host)
        )
        self.assertEqual(
            client.enable_auto_merge.call_args, mock.call("g", "p", 7, "REBASE", **host)
        )
        self.assertEqual(
            client.rerun_workflow_run.call_args, mock.call("g", "p", 11, failed_only=True, **host)
        )
        root = routes._scope(key)
        self.assertEqual(
            [c.args[:4] + (c.kwargs["root"],) for c in state_patch.call_args_list],
            [("g", "p", 7, "open", root), ("g", "p", 7, "closed", root)],
        )
        self.assertEqual(len(drop.call_args_list), 6)
        self.assertEqual(len(threads), 8)
        self.assertNotIn(loop_thread, threads)

    async def test_bulk_rows_report_and_audit_their_own_outcome(self):
        run = AsyncMock(side_effect=[{}, gh.GhCliError("net"), gh.GhPermissionError("no")])
        audit = MagicMock()
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit", audit),
            mock.patch.object(routes, "_run_pr_action", run),
        ):
            resp = await routes._handle_pulls_bulk(
                _post(
                    "pulls/bulk",
                    {"owner": "o", "repo": "r", "action": "close", "numbers": [1, 2, 3, 1]},
                )
            )
        body = _body(resp)
        self.assertEqual(body["action"], "close")
        self.assertEqual([r["number"] for r in body["applied"]], [1])
        self.assertEqual(
            body["failed"], [{"number": 2, "error": "net"}, {"number": 3, "error": "no"}]
        )
        self.assertEqual(
            audit.call_args_list,
            [
                mock.call("pulls_bulk", "o/r#1:close", "ok"),
                mock.call("pulls_bulk", "o/r#2:close", "failure", error="net"),
                mock.call("pulls_bulk", "o/r#3:close", "denied", error="no"),
            ],
        )


# ── error bodies each route returns ─────────────────────────────────────────────


class TestErrorBodies(unittest.IsolatedAsyncioTestCase):
    async def test_the_sanitized_read_errors_log_the_raw_text(self):
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(gh, "list_pr_workflow_runs", side_effect=gh.GhCliError("raw diag")),
            self.assertLogs(LOGGER, level="WARNING") as logs,
        ):
            resp = await routes._handle_pull_runs(
                _get("pull/runs", {"owner": "o", "repo": "r", "sha": SHA})
            )
        self.assertEqual(
            (resp.status, _body(resp)),
            (502, {"error": "upstream provider error", "code": "provider_error"}),
        )
        self.assertTrue(any("raw diag" in r.getMessage() for r in logs.records))

    async def test_the_assignee_502_is_sanitized(self):
        body = {"owner": "o", "repo": "r", "number": 7, "assignees": ["a"], "expected": []}
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit") as audit,
            mock.patch.object(
                routes, "_replace_assignees_checked", side_effect=gh.GhCliError("secret stderr")
            ),
        ):
            resp = await routes._handle_issue_assignees(_post("issue/assignees", body))
        self.assertEqual(
            (resp.status, _body(resp)),
            (502, {"error": "upstream provider error", "code": "provider_error"}),
        )
        audit.assert_called_once_with("issue_assignees", "o/r#7", "failure", error="secret stderr")

    async def test_the_assignee_conflict_is_a_409_carrying_the_forge_state(self):
        body = {"owner": "o", "repo": "r", "number": 7, "assignees": ["a"], "expected": []}
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit") as audit,
            mock.patch.object(routes, "_replace_assignees_checked", return_value=(None, ["bob"])),
        ):
            resp = await routes._handle_issue_assignees(_post("issue/assignees", body))
        self.assertEqual(resp.status, 409)
        self.assertEqual(_body(resp)["assignees"], ["bob"])
        self.assertEqual(_body(resp)["code"], "assignees_conflict")
        audit.assert_called_once_with(
            "issue_assignees", "o/r#7", "failure", error="assignees changed elsewhere"
        )

    async def test_first_page_provider_errors_are_generic_and_logged(self):
        client = MagicMock()
        client.list_open_issues_first_page.side_effect = gh.GhCliError("stderr-a")
        client.list_open_pulls_first_page.side_effect = gh.GhCliError("stderr-b")
        with (
            mock.patch.object(store, "read_issues_snapshot", return_value=None),
            mock.patch.object(store, "read_pulls_snapshot", return_value=None),
            self.assertLogs(LOGGER, level="WARNING") as logs,
        ):
            issues = await routes._handle_issues_first_page(KEY, client, {})
            pulls = await routes._handle_pulls_first_page(KEY, client, {})
        generic = {"error": "upstream provider error", "code": "provider_error"}
        self.assertEqual((issues.status, _body(issues)), (502, generic))
        self.assertEqual((pulls.status, _body(pulls)), (502, generic))
        messages = " ".join(r.getMessage() for r in logs.records)
        self.assertIn("list_open_issues provider error: stderr-a", messages)
        self.assertIn("list_open_pulls provider error: stderr-b", messages)

    async def test_every_pull_list_response_publishes_the_bulk_cap(self):
        rows = [{"number": 1}]
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(
                store,
                "read_pulls_snapshot",
                return_value={"rows": rows, "probe": None, "age_sec": 0},
            ),
        ):
            cached = await routes._handle_pulls(_get("pulls", {"owner": "o", "repo": "r"}))
            first = await routes._handle_pulls(
                _get("pulls", {"owner": "o", "repo": "r", "first_page": "1"})
            )
        for resp in (cached, first):
            self.assertEqual(_body(resp)["bulk_max"], routes._BULK_PR_MAX)
            self.assertEqual(_body(resp)["provider"], "github")


# ── the AI routes' session adapter and response shapes ──────────────────────────


def _sessions() -> SimpleNamespace:
    return SimpleNamespace(
        sessions=SimpleNamespace(
            get_or_create=AsyncMock(return_value=(MagicMock(name="provider"), True, False)),
            release=MagicMock(),
            destroy=AsyncMock(),
        )
    )


class TestModelAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_the_session_is_torn_down_when_the_model_call_is_cancelled(self):
        app = web.Application()
        state = _sessions()
        app["state"] = state
        with mock.patch.object(
            llm_helpers, "stream_and_collect", AsyncMock(side_effect=asyncio.CancelledError())
        ) as stream:
            with self.assertRaises(asyncio.CancelledError):
                await routes._run_oneshot_model(_get("issue-ai", app=app), "k1", "prompt")
        state.sessions.get_or_create.assert_awaited_once_with("k1", agent="kirocrew-lite")
        self.assertIs(
            stream.await_args.kwargs["approval_policy"], llm_helpers.ToolApprovalPolicy.REJECT_ALL
        )
        state.sessions.release.assert_called_once_with("k1")
        state.sessions.destroy.assert_awaited_once_with("k1")

    async def test_the_recommendation_session_is_isolated_the_same_way(self):
        app = web.Application()
        state = _sessions()
        app["state"] = state
        with mock.patch.object(
            llm_helpers, "stream_and_collect", AsyncMock(side_effect=RuntimeError("down"))
        ) as stream:
            with self.assertRaises(RuntimeError):
                await routes._compute_label_recommendations(
                    _get("recommendations", app=app), "o", "r", [], []
                )
        key = state.sessions.get_or_create.await_args.args[0]
        self.assertRegex(key, r"^issue-radar-reco:o/r:[0-9a-f]{32}$")
        self.assertEqual(state.sessions.get_or_create.await_args.kwargs, {"agent": "kirocrew-lite"})
        self.assertIs(
            stream.await_args.kwargs["approval_policy"], llm_helpers.ToolApprovalPolicy.REJECT_ALL
        )
        state.sessions.release.assert_called_once_with(key)
        state.sessions.destroy.assert_awaited_once_with(key)

    async def test_each_generation_gets_a_fresh_session_key(self):
        oneshot = AsyncMock(return_value="{}")
        with mock.patch.object(routes, "_run_oneshot_model", oneshot):
            for _ in range(2):
                await routes._compute_issue_ai(_get("issue-ai"), "o", "r", 7, {}, [])
        keys = [c.args[1] for c in oneshot.await_args_list]
        self.assertNotEqual(keys[0], keys[1])
        for key in keys:
            self.assertRegex(key, r"^issue-radar-ai:o/r#7:[0-9a-f]{32}$")

    async def test_ai_output_is_redacted_before_it_is_clamped(self):
        token = "ghp" + "_" + "z4Kq" * 9
        reply = json.dumps(
            {
                "summary": f"leak {token}",
                "suggested_labels": [{"name": "bug", "reason": "x" * 190 + token}],
            }
        )
        with mock.patch.object(routes, "_run_oneshot_model", AsyncMock(return_value=reply)):
            ai = await routes._compute_issue_ai(
                _get("issue-ai"), "o", "r", 7, {}, [{"name": "bug"}]
            )
        self.assertNotIn(token, ai["summary"])
        self.assertNotIn(token[:10], ai["suggested_labels"][0]["reason"])


class TestAiResponseShapes(unittest.IsolatedAsyncioTestCase):
    async def test_issue_and_pull_ai_key_sets(self):
        with (
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_issue_ai_cache", return_value={"summary": "s"}),
            mock.patch.object(
                store,
                "read_pr_detail_cache",
                return_value={"detail": {"number": 5}, "timeline": [], "checks": []},
            ),
            mock.patch.object(store, "read_pr_ai_cache", return_value={"summary": "p"}),
        ):
            issue = await routes._handle_issue_ai(
                _get("issue-ai", {"owner": "o", "repo": "r", "number": "5"})
            )
            pull = await routes._handle_pull_ai(
                _get("pull-ai", {"owner": "o", "repo": "r", "number": "5"})
            )
        self.assertEqual(
            set(_body(issue)),
            {
                "owner",
                "repo",
                "number",
                "summary",
                "suggested_labels",
                "generated_at",
                "from_cache",
            },
        )
        self.assertEqual(
            set(_body(pull)), {"owner", "repo", "number", "summary", "generated_at", "from_cache"}
        )

    async def test_a_pull_summary_is_written_under_the_language_folded_fingerprint(self):
        detail = {"number": 12, "state": "open"}
        with (
            mock.patch.object(routes, "_ui_language", return_value="ko"),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(
                store,
                "read_pr_detail_cache",
                return_value={"detail": detail, "timeline": [], "checks": []},
            ),
            mock.patch.object(store, "read_pr_ai_cache", return_value=None) as read,
            mock.patch.object(routes, "_compute_pr_ai", AsyncMock(return_value="s")),
            mock.patch.object(store, "write_pr_ai_cache") as write,
        ):
            resp = await routes._handle_pull_ai(
                _get("pull-ai", {"owner": "o", "repo": "r", "number": "12"})
            )
        self.assertEqual(resp.status, 200)
        fingerprint = routes._pr_ai_fingerprint(detail, [], [], ui_language="ko")
        self.assertEqual(read.call_args.kwargs["fingerprint"], fingerprint)
        self.assertEqual(
            write.call_args.args[:4], ("o", "r", 12, {"summary": "s", "fingerprint": fingerprint})
        )
        self.assertEqual(write.call_args.kwargs["ui_language"], "ko")

    async def test_language_resolution_runs_off_the_loop_in_every_ai_handler(self):
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def _lang():
            threads.append(threading.get_ident())
            return ""

        empty = AsyncMock(return_value=[])
        with (
            mock.patch.object(routes, "_ui_language", side_effect=_lang),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_issue_ai_cache", return_value={"summary": "s"}),
            mock.patch.object(
                store,
                "read_pr_detail_cache",
                return_value={"detail": {}, "timeline": [], "checks": []},
            ),
            mock.patch.object(store, "read_pr_ai_cache", return_value={"summary": "p"}),
            mock.patch.object(store, "read_recommendations_cache", return_value=None),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(routes, "_load_open_issues_for_reco", empty),
            mock.patch.object(
                routes, "_load_labels_for_ai", AsyncMock(return_value=[{"name": "bug"}])
            ),
        ):
            q = {"owner": "o", "repo": "r", "number": "5"}
            await routes._handle_issue_ai(_get("issue-ai", q))
            await routes._handle_pull_ai(_get("pull-ai", q))
            await routes._handle_get_recommendations(_get("recommendations", q))
            await routes._handle_get_tagging(_get("tagging", q))
            await routes._handle_generate_tagging(_post("tagging", {"owner": "o", "repo": "r"}))
        self.assertEqual(len(threads), 5)
        self.assertNotIn(loop_thread, threads)


class TestModelOutputRedaction(unittest.IsolatedAsyncioTestCase):
    """Model text derived from untrusted issue and PR bodies is redacted before it
    is cached, proposed or returned, and before any length clamp."""

    TOKEN = "ghp" + "_" + "z4Kq" * 9

    async def test_the_pr_summary_is_redacted(self):
        reply = json.dumps({"summary": f"see {self.TOKEN}"})
        with mock.patch.object(routes, "_run_oneshot_model", AsyncMock(return_value=reply)):
            summary = await routes._compute_pr_ai(_get("pull-ai"), "o", "r", 7, {}, [], [])
        self.assertNotIn(self.TOKEN, summary)

    async def test_recommendation_text_is_redacted_before_it_is_clamped(self):
        app = web.Application()
        app["state"] = _sessions()
        reply = json.dumps(
            {
                "recommendations": [
                    {
                        "name": f"area {self.TOKEN}",
                        "category": "area",
                        "color": "zzzzzz",
                        "description": "d" * 110 + self.TOKEN,
                        "rationale": "r" * 100 + self.TOKEN,
                        "examples": [3, 99],
                    }
                ]
            }
        )
        with mock.patch.object(llm_helpers, "stream_and_collect", AsyncMock(return_value=reply)):
            result = await routes._compute_label_recommendations(
                _get("recommendations", app=app), "o", "r", [], ISSUES
            )
        (row,) = result["recommendations"]
        for field in ("name", "description", "rationale"):
            self.assertNotIn(self.TOKEN[:10], row[field], field)
        self.assertEqual((row["color"], row["examples"]), ("0e8a16", [3]))

    async def test_tagging_reasons_are_redacted(self):
        reply = json.dumps(
            {
                "assignments": [
                    {"number": 3, "labels": [{"name": "bug", "reason": "x" * 190 + self.TOKEN}]}
                ]
            }
        )
        with mock.patch.object(routes, "_run_oneshot_model", AsyncMock(return_value=reply)):
            out = await routes._compute_tagging_suggestions(
                _get("tagging"), "o", "r", [{"name": "bug"}], ISSUES
            )
        self.assertNotIn(self.TOKEN[:10], out["3"][0]["reason"])


GITLAB_Q = {"owner": "g", "repo": "p", "provider": "gitlab", "host": "gitlab.com"}


class TestConfigRoutesKeepTheirProvider(unittest.IsolatedAsyncioTestCase):
    """Every config-level store function defaults to GitHub, so a route that
    dropped the key's provider or host would read, overwrite or DELETE the
    same-slug GitHub entry instead."""

    async def test_settings_and_disconnect_forward_provider_and_host(self):
        identity = {"provider": "gitlab", "host": "gitlab.com"}
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_repo_settings", return_value={}) as read,
            mock.patch.object(store, "write_repo_settings", return_value={}) as write,
            mock.patch.object(store, "add_setting_label", return_value={}) as append,
            mock.patch.object(store, "remove_connected_repo", return_value=True) as remove,
        ):
            await routes._handle_get_settings(_get("settings", GITLAB_Q))
            await routes._handle_put_settings(
                _post("settings", {**GITLAB_Q, "settings": {"revision": 0}})
            )
            await routes._handle_add_settings_label(
                _post("settings/role", {**GITLAB_Q, "role": "triage", "label": "bug"})
            )
            resp = await routes._handle_disconnect(_get("repos", GITLAB_Q))
        self.assertEqual(_body(resp), {"ok": True, "owner": "g", "repo": "p"})
        self.assertEqual(read.call_args, mock.call("g", "p", **identity))
        self.assertEqual(write.call_args.kwargs, {"expected_revision": 0, **identity})
        self.assertEqual(append.call_args, mock.call("g", "p", "triage", "bug", **identity))
        self.assertEqual(remove.call_args, mock.call("g", "p", **identity))

    async def test_account_routes_dispatch_to_the_requested_provider(self):
        gitlab = "kiro_crew.apps.builtins.issue_radar.backend.gitlab_client"
        with (
            mock.patch(f"{gitlab}.get_current_login", return_value="alice.smith") as login,
            mock.patch(
                f"{gitlab}.list_contributed_repos",
                return_value=([{"owner": "g", "repo": "p"}], False),
            ) as contributed,
            mock.patch.object(gh, "get_current_login", side_effect=AssertionError("github")),
            mock.patch.object(
                store,
                "list_connected_repos",
                return_value=[{"owner": "g", "repo": "p"}],  # a same-slug GITHUB entry
            ),
        ):
            me = await routes._handle_me(_get("me", {"provider": "gitlab", "host": "gitlab.com"}))
            recent = await routes._handle_recent_repos(
                _get("recent-repos", {"provider": "gitlab", "host": "gitlab.com"})
            )
        self.assertEqual(
            _body(me), {"login": "alice.smith", "provider": "gitlab", "host": "gitlab.com"}
        )
        self.assertEqual(login.call_args.kwargs, {"host": "gitlab.com"})
        self.assertEqual(contributed.call_args.kwargs["host"], "gitlab.com")
        (row,) = _body(recent)["repos"]
        self.assertEqual(
            row,
            {
                "owner": "g",
                "repo": "p",
                "connected": False,
                "provider": "gitlab",
                "host": "gitlab.com",
            },
        )

    async def test_the_repo_switcher_heals_each_row_against_its_own_provider(self):
        rows = [
            {"owner": "g", "repo": "p", "provider": "gitlab", "host": "gitlab.com"},
            {"owner": "o", "repo": "bad"},
        ]
        gitlab = "kiro_crew.apps.builtins.issue_radar.backend.gitlab_client"
        with (
            mock.patch.object(store, "list_connected_repos", return_value=rows),
            mock.patch(
                f"{gitlab}.verify_repo_access", return_value={"permissions": {"push": True}}
            ),
            mock.patch.object(gh, "verify_repo_access", side_effect=gh.GhCliError("gone")),
            mock.patch.object(store, "set_repo_permissions") as heal,
        ):
            resp = await routes._handle_repos(_get("repos"))
        repos = _body(resp)["repos"]
        self.assertEqual(repos[0]["permissions"], {"push": True})
        self.assertNotIn("permissions", repos[1])
        heal.assert_called_once_with("g", "p", {"push": True}, provider="gitlab", host="gitlab.com")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
