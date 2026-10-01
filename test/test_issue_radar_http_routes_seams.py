"""Issue Radar's historic ``backend.routes`` monkeypatch seams intercept every caller.

The route layer's tests, ``crew_routes`` and operators' own debugging all patch
names on ``backend.routes`` -- ``_connected``, ``_repo_can_write``, ``_scope``,
``_st``, ``_audit``, ``is_app_enabled``, the cache-first loaders, the model calls,
the PR-action dispatch and so on. A patch only means something when the code
under test looks the name up on ``backend.routes`` at call time. A handler that
bound its own copy would keep answering correctly while silently ignoring the
patch, so each test here replaces one seam with a recording double and asserts
the double was CONSULTED from a representative caller -- a denial alone is not
proof, because the real gate would often deny too.

Everything is patched at the ``routes`` / ``store`` / ``provider`` boundary: no
``gh`` subprocess, no model call, no network, and nothing written outside the
per-test ``KIROCREW_HOME``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store

BASE = "/api/apps/issue-radar"
SHA = "a" * 40
KEY = provider.key_from_parts("o", "r")
GITLAB = provider.key_from_parts("g", "p", "gitlab", "gitlab.com")


def _get(path: str, query: dict | None = None, app: web.Application | None = None) -> web.Request:
    full = f"{BASE}/{path}"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full, app=app or web.Application())


def _post(path: str, body: object, method: str = "POST", app: web.Application | None = None):
    req = make_mocked_request(method, f"{BASE}/{path}", app=app or web.Application())
    if "state" not in req.app:
        req.app["state"] = NoConfiguredOwner()
    req["user"] = "local-app"
    req["app"] = ""
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


REPO = {"owner": "o", "repo": "r"}
REPO_Q = {"owner": "o", "repo": "r"}

#: (label, handler name, request factory). Every route that runs the connected-repo
#: gate, across every owner module, reached through the facade exactly as the
#: registrar and the tests reach it.
CONNECTED_GATED = [
    ("issues", "_handle_issues", lambda: _get("issues", REPO_Q)),
    ("issue", "_handle_issue_detail", lambda: _get("issue", {**REPO_Q, "number": "5"})),
    ("labels", "_handle_labels", lambda: _get("labels", REPO_Q)),
    ("members", "_handle_members", lambda: _get("members", REPO_Q)),
    ("settings-get", "_handle_get_settings", lambda: _get("settings", REPO_Q)),
    ("pulls", "_handle_pulls", lambda: _get("pulls", REPO_Q)),
    ("pulls-search", "_handle_pulls_search", lambda: _get("pulls/search", REPO_Q)),
    ("pull", "_handle_pull_detail", lambda: _get("pull", {**REPO_Q, "number": "5"})),
    ("ref", "_handle_ref_summary", lambda: _get("ref", {**REPO_Q, "number": "5"})),
    ("deps", "_handle_deps", lambda: _get("deps", REPO_Q)),
    ("issue-ai", "_handle_issue_ai", lambda: _get("issue-ai", {**REPO_Q, "number": "5"})),
    ("pull-ai", "_handle_pull_ai", lambda: _get("pull-ai", {**REPO_Q, "number": "5"})),
    (
        "investigation-get",
        "_handle_get_investigation",
        lambda: _get("investigation", {**REPO_Q, "number": "5"}),
    ),
    ("recommendations-get", "_handle_get_recommendations", lambda: _get("recommendations", REPO_Q)),
    ("tagging-get", "_handle_get_tagging", lambda: _get("tagging", REPO_Q)),
    ("pull-runs", "_handle_pull_runs", lambda: _get("pull/runs", {**REPO_Q, "sha": SHA})),
    (
        "labels-apply",
        "_handle_labels_apply",
        lambda: _post("labels/apply", {**REPO, "number": 5, "add": ["bug"]}),
    ),
    (
        "labels-apply-bulk",
        "_handle_labels_apply_bulk",
        lambda: _post("labels/apply-bulk", {**REPO, "changes": [{"number": 5, "add": ["bug"]}]}),
    ),
    (
        "issue-state",
        "_handle_issue_state",
        lambda: _post("issue/state", {**REPO, "number": 5, "state": "closed"}),
    ),
    (
        "issue-assignees",
        "_handle_issue_assignees",
        lambda: _post("issue/assignees", {**REPO, "number": 5, "assignees": [], "expected": []}),
    ),
    (
        "labels-create",
        "_handle_create_label",
        lambda: _post("labels/create", {**REPO, "name": "x"}),
    ),
    (
        "investigation-put",
        "_handle_put_investigation",
        lambda: _post("investigation", {**REPO, "number": 5}, method="PUT"),
    ),
    (
        "recommendations-post",
        "_handle_generate_recommendations",
        lambda: _post("recommendations", REPO),
    ),
    ("tagging-post", "_handle_generate_tagging", lambda: _post("tagging", REPO)),
    ("pull-state", "_handle_pull_state", lambda: _post("pull/state", {**REPO, "number": 5})),
    ("pull-review", "_handle_pull_review", lambda: _post("pull/review", {**REPO, "number": 5})),
    ("pull-comment", "_handle_pull_comment", lambda: _post("pull/comment", {**REPO, "number": 5})),
    ("pull-merge", "_handle_pull_merge", lambda: _post("pull/merge", {**REPO, "number": 5})),
    (
        "pull-auto-merge",
        "_handle_pull_auto_merge",
        lambda: _post("pull/auto-merge", {**REPO, "number": 5}),
    ),
    ("pull-run", "_handle_pull_run_action", lambda: _post("pull/run", {**REPO, "number": 5})),
    ("pulls-bulk", "_handle_pulls_bulk", lambda: _post("pulls/bulk", {**REPO, "numbers": [5]})),
]

#: The write routes and the op each one audits a permission denial under. Their
#: bodies are valid, so the write-permission gate is the first thing that can stop them.
WRITE_GATED = [
    (
        "apply_labels",
        "_handle_labels_apply",
        lambda: _post("labels/apply", {**REPO, "number": 5, "add": ["bug"]}),
        "o/r#5",
    ),
    (
        "apply_labels_bulk",
        "_handle_labels_apply_bulk",
        lambda: _post("labels/apply-bulk", {**REPO, "changes": [{"number": 5, "add": ["bug"]}]}),
        "o/r",
    ),
    (
        "issue_state",
        "_handle_issue_state",
        lambda: _post("issue/state", {**REPO, "number": 5, "state": "closed"}),
        "o/r#5",
    ),
    (
        "issue_assignees",
        "_handle_issue_assignees",
        lambda: _post("issue/assignees", {**REPO, "number": 5, "assignees": [], "expected": []}),
        "o/r#5",
    ),
    (
        "create_label",
        "_handle_create_label",
        lambda: _post("labels/create", {**REPO, "name": "x"}),
        "o/r:x",
    ),
    ("pull_state", "_handle_pull_state", lambda: _post("pull/state", {**REPO}), "o/r"),
    ("pull_review", "_handle_pull_review", lambda: _post("pull/review", {**REPO}), "o/r"),
    ("pull_comment", "_handle_pull_comment", lambda: _post("pull/comment", {**REPO}), "o/r"),
    ("pull_merge", "_handle_pull_merge", lambda: _post("pull/merge", {**REPO}), "o/r"),
    (
        "pull_auto_merge",
        "_handle_pull_auto_merge",
        lambda: _post("pull/auto-merge", {**REPO}),
        "o/r",
    ),
    ("pull_run", "_handle_pull_run_action", lambda: _post("pull/run", {**REPO}), "o/r"),
    ("pulls_bulk", "_handle_pulls_bulk", lambda: _post("pulls/bulk", {**REPO}), "o/r"),
]


class TestEnabledGateSeam(unittest.IsolatedAsyncioTestCase):
    async def test_the_wrapper_consults_routes_is_app_enabled_off_the_loop(self):
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _enabled(name: str) -> bool:
            seen.append(threading.get_ident())
            return False

        handler = AsyncMock()
        with mock.patch.object(routes, "is_app_enabled", side_effect=_enabled) as enabled:
            resp = await routes._require_enabled(handler)(_get("repos"))
        self.assertEqual(resp.status, 403)
        self.assertEqual(_body(resp), {"error": "issue-radar is disabled"})
        enabled.assert_called_once_with("issue-radar")
        handler.assert_not_awaited()
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)

    async def test_an_enabled_app_reaches_the_wrapped_handler(self):
        sentinel = web.json_response({"ok": True})
        handler = AsyncMock(return_value=sentinel)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            resp = await routes._require_enabled(handler)(_get("repos"))
        self.assertIs(resp, sentinel)
        handler.assert_awaited_once()


class TestConnectedGateSeam(unittest.IsolatedAsyncioTestCase):
    """Every repo-scoped route consults ``routes._connected`` with the request's key."""

    async def test_every_gated_route_consults_the_patched_gate(self):
        for label, name, make in CONNECTED_GATED:
            with self.subTest(route=label):
                gate = MagicMock(return_value=False)
                client = MagicMock()
                with (
                    mock.patch.object(routes, "_connected", gate),
                    mock.patch.object(provider, "client_for", return_value=client),
                ):
                    resp = await getattr(routes, name)(make())
                self.assertEqual(resp.status, 404, _body(resp))
                gate.assert_called_once_with(KEY)
                self.assertEqual(client.method_calls, [])

    async def test_the_gate_receives_the_non_github_identity(self):
        gate = MagicMock(return_value=False)
        query = {"owner": "g", "repo": "p", "provider": "gitlab", "host": "GitLab.com"}
        with mock.patch.object(routes, "_connected", gate):
            resp = await routes._handle_labels(_get("labels", query))
        self.assertEqual(resp.status, 404)
        gate.assert_called_once_with(GITLAB)


class TestWritePermissionSeam(unittest.IsolatedAsyncioTestCase):
    """Every write consults ``routes._repo_can_write`` and treats an UNKNOWN answer
    (``None``, a failed permission read) as a denial, audited as one."""

    async def test_every_write_consults_the_patched_gate_and_fails_closed(self):
        for op, name, make, target in WRITE_GATED:
            for verdict in (None, False):
                with self.subTest(op=op, verdict=verdict):
                    gate = MagicMock(return_value=verdict)
                    audit = MagicMock()
                    client = MagicMock()
                    with (
                        mock.patch.object(routes, "_connected", return_value=True),
                        mock.patch.object(routes, "_repo_can_write", gate),
                        mock.patch.object(routes, "_audit", audit),
                        mock.patch.object(routes, "_load_labels_for_ai", AsyncMock()) as labels,
                        mock.patch.object(provider, "client_for", return_value=client),
                    ):
                        resp = await getattr(routes, name)(make())
                    self.assertEqual(resp.status, 403, _body(resp))
                    gate.assert_called_once_with(KEY)
                    labels.assert_not_awaited()
                    self.assertEqual(client.method_calls, [])
                    audit.assert_called_once_with(
                        op, target, "denied", error="no confirmed write access"
                    )


class TestScopeSeam(unittest.IsolatedAsyncioTestCase):
    """``routes._scope`` decides the data root of every per-repo store call."""

    async def test_st_hands_the_patched_scope_to_the_store(self):
        root = Path("/scoped-root")
        read = MagicMock(return_value=[{"name": "bug"}])
        with (
            mock.patch.object(routes, "_scope", return_value=root) as scope,
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_labels_cache", read),
        ):
            resp = await routes._handle_labels(_get("labels", REPO_Q))
        self.assertEqual(resp.status, 200)
        scope.assert_called_with(KEY)
        self.assertEqual(read.call_args.kwargs["root"], root)

    def test_the_locked_write_helpers_scope_through_routes(self):
        root = Path("/scoped-root")
        client = MagicMock()
        client.add_issue_labels.return_value = [{"name": "bug"}]
        client.get_issue_detail.return_value = {"labels": [{"name": "bug"}], "assignees": []}
        client.set_issue_assignees.return_value = []
        lock = MagicMock()
        with (
            mock.patch.object(routes, "_scope", return_value=root),
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(store, "issue_write_lock", lock),
            mock.patch.object(store, "apply_label_change_to_caches") as patch_labels,
            mock.patch.object(store, "apply_assignees_change_to_caches") as patch_assignees,
        ):
            routes._apply_label_change(KEY, 7, ["bug"], [])
            routes._reread_labels_and_patch(KEY, 7)
            routes._replace_assignees_checked(KEY, 7, [], [])
        self.assertEqual([c.args for c in lock.call_args_list], [("o", "r", 7, root)] * 3)
        self.assertEqual([c.kwargs["root"] for c in patch_labels.call_args_list], [root, root])
        self.assertEqual(patch_assignees.call_args.kwargs["root"], root)

    def test_the_member_loader_scopes_through_routes(self):
        root = Path("/scoped-root")
        client = MagicMock()
        client.list_repo_collaborators.side_effect = gh.GhPermissionError("403")
        client.derive_members.return_value = []
        with (
            mock.patch.object(routes, "_scope", return_value=root),
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(store, "read_issues_cache", return_value=[]) as read,
            mock.patch.object(store, "write_members_cache") as write,
        ):
            self.assertEqual(routes._load_members(KEY), ([], "derived"))
        self.assertEqual(
            [(c.args[2], c.kwargs["state"]) for c in read.call_args_list],
            [(root, "open"), (root, "closed")],
        )
        self.assertEqual(write.call_args.kwargs["root"], root)


class TestStoreFunnelSeam(unittest.IsolatedAsyncioTestCase):
    async def test_handlers_await_the_patched_st(self):
        st = AsyncMock(return_value=[{"name": "bug"}])
        with (
            mock.patch.object(routes, "_st", st),
            mock.patch.object(routes, "_connected", return_value=True),
        ):
            resp = await routes._handle_labels(_get("labels", REPO_Q))
        self.assertEqual(_body(resp)["labels"], [{"name": "bug"}])
        st.assert_awaited_once_with(KEY, store.read_labels_cache, "o", "r")


class TestAuditSeam(unittest.IsolatedAsyncioTestCase):
    async def test_the_pr_preamble_audits_a_denial_under_the_callers_op(self):
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=None),
            mock.patch.object(routes, "_audit") as audit,
        ):
            _raw, key, early = await routes._pr_action_preamble(_post("x", REPO), "issue_comment")
        self.assertIsNotNone(early)
        self.assertEqual(key, KEY)
        audit.assert_called_once_with(
            "issue_comment", "o/r", "denied", error="no confirmed write access"
        )

    def test_the_error_mapper_audits_through_routes(self):
        with mock.patch.object(routes, "_audit") as audit:
            denied = routes._pr_action_error("pull_x", "o/r#1", gh.GhPermissionError("locked"))
            failed = routes._pr_action_error("pull_x", "o/r#1", gh.GhCliError("auto-merge is off"))
        self.assertEqual(
            (denied.status, _body(denied)), (403, {"error": "locked", "code": "provider_forbidden"})
        )
        self.assertEqual(
            (failed.status, _body(failed)),
            (502, {"error": "auto-merge is off", "code": "provider_error"}),
        )
        self.assertEqual(
            audit.call_args_list,
            [
                mock.call("pull_x", "o/r#1", "denied", error="locked"),
                mock.call("pull_x", "o/r#1", "failure", error="auto-merge is off"),
            ],
        )

    def test_audit_payload(self):
        log = MagicMock()
        with mock.patch.dict(routes._audit.__globals__, {"sel": MagicMock(return_value=log)}):
            routes._audit("apply_labels", "o/r#7", "failure", error="x" * 500)
            routes._audit("apply_labels", "o/r#7", "ok")
        self.assertEqual(
            log.log_api_access.call_args_list,
            [
                mock.call(
                    caller="core:issue-radar",
                    operation="issue_radar.apply_labels",
                    outcome="failure",
                    source="builtin-app",
                    resources="o/r#7",
                    error="x" * 200,
                ),
                mock.call(
                    caller="core:issue-radar",
                    operation="issue_radar.apply_labels",
                    outcome="ok",
                    source="builtin-app",
                    resources="o/r#7",
                    error="",
                ),
            ],
        )


class TestLanguageSeam(unittest.IsolatedAsyncioTestCase):
    """``routes._ui_language`` is the configured language every AI surface reads."""

    def test_the_configured_language_outranks_the_browser_hint(self):
        with mock.patch.object(routes, "_ui_language", return_value="ko") as lang:
            self.assertEqual(routes._resolve_ui_language("zh-CN"), "ko")
        lang.assert_called_once_with()
        with mock.patch.object(routes, "_ui_language", return_value=""):
            self.assertEqual(routes._resolve_ui_language("zh-CN"), "zh-CN")

    async def test_issue_ai_reads_its_cache_in_the_patched_language(self):
        read = MagicMock(return_value={"summary": "s", "suggested_labels": []})
        with (
            mock.patch.object(routes, "_ui_language", return_value="ko"),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_issue_ai_cache", read),
        ):
            resp = await routes._handle_issue_ai(
                _get("issue-ai", {**REPO_Q, "number": "5", "lang": "zh-CN"})
            )
        self.assertTrue(_body(resp)["from_cache"])
        self.assertEqual(read.call_args.kwargs["ui_language"], "ko")

    async def test_tagging_generation_verifies_with_the_patched_resolver(self):
        compute = AsyncMock(return_value={"5": [{"name": "bug", "reason": ""}]})
        merge = MagicMock(return_value={"suggestions": {}, "generated_at": "t"})
        with (
            mock.patch.object(routes, "_ui_language", return_value="de-DE") as lang,
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(
                routes, "_load_labels_for_ai", AsyncMock(return_value=[{"name": "bug"}])
            ),
            mock.patch.object(
                routes,
                "_load_open_issues_for_reco",
                AsyncMock(return_value=[{"number": 5, "labels": []}]),
            ),
            mock.patch.object(routes, "_compute_tagging_suggestions", compute),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(store, "merge_tagging_suggestions", merge),
        ):
            resp = await routes._handle_generate_tagging(_post("tagging", {**REPO, "lang": "it"}))
        self.assertEqual(resp.status, 200, _body(resp))
        self.assertEqual(compute.call_args.kwargs["ui_language"], "de-DE")
        self.assertEqual(merge.call_args.kwargs["ui_language"], "de-DE")
        self.assertIs(merge.call_args.kwargs["verify_language"], lang)


class TestLoaderAndComputeSeams(unittest.IsolatedAsyncioTestCase):
    """The cache-first loaders and the model calls are patched per route by the
    suites; each caller must reach the patched object."""

    async def test_issue_ai_reaches_the_patched_loaders_and_compute(self):
        detail = AsyncMock(return_value={"title": "t"})
        labels = AsyncMock(return_value=[{"name": "bug"}])
        compute = AsyncMock(return_value={"summary": "", "suggested_labels": []})
        with (
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_issue_ai_cache", return_value=None),
            mock.patch.object(routes, "_load_detail_for_ai", detail),
            mock.patch.object(routes, "_load_labels_for_ai", labels),
            mock.patch.object(routes, "_compute_issue_ai", compute),
        ):
            resp = await routes._handle_issue_ai(_get("issue-ai", {**REPO_Q, "number": "5"}))
        self.assertEqual(resp.status, 200)
        detail.assert_awaited_once_with(KEY, 5)
        labels.assert_awaited_once_with(KEY)
        self.assertEqual(
            compute.await_args.args[1:], ("o", "r", 5, {"title": "t"}, [{"name": "bug"}])
        )

    async def test_pull_ai_reaches_the_patched_compute(self):
        cached = {"detail": {"number": 5}, "timeline": [], "checks": []}
        compute = AsyncMock(return_value="")
        with (
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_pr_detail_cache", return_value=cached),
            mock.patch.object(store, "read_pr_ai_cache", return_value=None),
            mock.patch.object(routes, "_compute_pr_ai", compute),
        ):
            resp = await routes._handle_pull_ai(_get("pull-ai", {**REPO_Q, "number": "5"}))
        self.assertEqual(resp.status, 200)
        compute.assert_awaited_once()

    async def test_recommendations_reach_the_patched_loaders_and_compute(self):
        labels = AsyncMock(return_value=[])
        issues = AsyncMock(return_value=[])
        compute = AsyncMock(return_value={"recommendations": []})
        with (
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai", labels),
            mock.patch.object(routes, "_load_open_issues_for_reco", issues),
            mock.patch.object(routes, "_compute_label_recommendations", compute),
            mock.patch.object(store, "write_recommendations_cache") as write,
        ):
            resp = await routes._handle_generate_recommendations(_post("recommendations", REPO))
        self.assertEqual(resp.status, 200)
        labels.assert_awaited_once_with(KEY)
        issues.assert_awaited_once_with(KEY)
        compute.assert_awaited_once()
        write.assert_called_once()

    async def test_the_tagging_queue_reaches_the_patched_issue_loader(self):
        issues = AsyncMock(return_value=[])
        with (
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_load_open_issues_for_reco", issues),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
        ):
            resp = await routes._handle_get_tagging(_get("tagging", {**REPO_Q, "refresh": "1"}))
        self.assertEqual(resp.status, 200)
        issues.assert_awaited_once_with(KEY, refresh=True)

    async def test_members_reach_the_patched_loader(self):
        load = MagicMock(return_value=([{"login": "a", "role": "admin"}], "collaborators"))
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_members_cache", return_value=None),
            mock.patch.object(routes, "_load_members", load),
        ):
            resp = await routes._handle_members(_get("members", REPO_Q))
        self.assertEqual(_body(resp)["source"], "collaborators")
        load.assert_called_once_with(KEY)

    async def test_the_model_adapter_is_reached_by_every_compute_that_uses_it(self):
        oneshot = AsyncMock(return_value="{}")
        req = _get("issue-ai", REPO_Q)
        with mock.patch.object(routes, "_run_oneshot_model", oneshot):
            await routes._compute_issue_ai(req, "o", "r", 7, {"labels": []}, [])
            await routes._compute_pr_ai(req, "o", "r", 7, {}, [], [])
            await routes._compute_tagging_suggestions(req, "o", "r", [], [])
        prefixes = [c.args[1].split(":", 1)[0] for c in oneshot.await_args_list]
        self.assertEqual(prefixes, ["issue-radar-ai", "issue-radar-pr-ai", "issue-radar-tagging"])


class TestWriteHelperSeams(unittest.IsolatedAsyncioTestCase):
    async def test_labels_apply_reaches_the_patched_writers(self):
        apply = MagicMock(return_value=None)
        reread = MagicMock(return_value=[{"name": "bug"}])
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit"),
            mock.patch.object(
                routes, "_load_labels_for_ai", AsyncMock(return_value=[{"name": "bug"}])
            ),
            mock.patch.object(routes, "_apply_label_change", apply),
            mock.patch.object(routes, "_reread_labels_and_patch", reread),
            mock.patch.object(store, "drop_tagging_suggestions"),
        ):
            resp = await routes._handle_labels_apply(
                _post("labels/apply", {**REPO, "number": 5, "remove": ["bug"]})
            )
        self.assertEqual(_body(resp)["labels"], [{"name": "bug"}])
        apply.assert_called_once_with(KEY, 5, [], ["bug"])
        reread.assert_called_once_with(KEY, 5)

    async def test_bulk_apply_reaches_the_patched_label_writer_add_only_in_order(self):
        apply = MagicMock(side_effect=lambda key, number, add, remove: [{"name": n} for n in add])
        body = {
            **REPO,
            "changes": [
                {"number": 9, "add": ["bug"]},
                {"number": 7, "add": ["docs"]},
                {"number": 9, "add": ["docs", "bug"]},
            ],
        }
        known = [{"name": "bug"}, {"name": "docs"}]
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit") as audit,
            mock.patch.object(routes, "_load_labels_for_ai", AsyncMock(return_value=known)),
            mock.patch.object(routes, "_apply_label_change", apply),
            mock.patch.object(store, "drop_tagging_suggestions") as drop,
        ):
            resp = await routes._handle_labels_apply_bulk(_post("labels/apply-bulk", body))
        self.assertEqual(
            [c.args for c in apply.call_args_list],
            [(KEY, 9, ["bug", "docs"], []), (KEY, 7, ["docs"], [])],
        )
        self.assertEqual([r["number"] for r in _body(resp)["applied"]], [9, 7])
        self.assertEqual(drop.call_args.args[2], [9, 7])
        self.assertEqual(
            audit.call_args_list,
            [
                mock.call("apply_labels_bulk", "o/r#9", "ok"),
                mock.call("apply_labels_bulk", "o/r#7", "ok"),
            ],
        )

    async def test_pr_routes_reach_the_patched_dispatch_and_head_check(self):
        run = AsyncMock(return_value={"state": "closed"})
        head = AsyncMock(return_value=None)
        order: list[tuple[str, int]] = []
        run.side_effect = lambda key, action, number, **kw: order.append(("act", number)) or {}
        head.side_effect = lambda key, number, sha, op: order.append(("check", number))
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_audit"),
            mock.patch.object(routes, "_run_pr_action", run),
            mock.patch.object(routes, "_refuse_if_head_moved", head),
        ):
            review = await routes._handle_pull_review(
                _post("pull/review", {**REPO, "number": 5, "event": "approve", "head_sha": SHA})
            )
            bulk = await routes._handle_pulls_bulk(
                _post(
                    "pulls/bulk",
                    {
                        **REPO,
                        "action": "approve",
                        "numbers": [1, 2],
                        "head_shas": {"1": SHA, "2": SHA},
                    },
                )
            )
        self.assertEqual((review.status, bulk.status), (200, 200))
        self.assertEqual(head.await_args_list[0], mock.call(KEY, 5, SHA, "pull_review"))
        self.assertEqual(
            order, [("check", 5), ("act", 5), ("check", 1), ("act", 1), ("check", 2), ("act", 2)]
        )


class TestListSeams(unittest.IsolatedAsyncioTestCase):
    async def test_issue_and_pull_polls_reach_the_patched_poll_decision_with_their_kind(self):
        snapshot = {"rows": [{"number": 1}], "probe": None, "age_sec": 0}
        decide = AsyncMock(return_value=(True, None))
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_poll_can_serve_cache", decide),
            mock.patch.object(store, "read_issues_snapshot", return_value=snapshot),
            mock.patch.object(store, "read_pulls_snapshot", return_value=snapshot),
        ):
            issues = await routes._handle_issues(_get("issues", {**REPO_Q, "poll": "1"}))
            pulls = await routes._handle_pulls(_get("pulls", {**REPO_Q, "poll": "1"}))
        self.assertTrue(_body(issues)["from_cache"])
        self.assertTrue(_body(pulls)["from_cache"])
        self.assertEqual(
            [c.args for c in decide.await_args_list],
            [(KEY, "issue", "open", snapshot), (KEY, "pr", "open", snapshot)],
        )

    async def test_first_page_requests_reach_the_patched_fast_paths(self):
        issues_fp = AsyncMock(return_value=web.json_response({"issues": []}))
        pulls_fp = AsyncMock(return_value=web.json_response({"pulls": []}))
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_handle_issues_first_page", issues_fp),
            mock.patch.object(routes, "_handle_pulls_first_page", pulls_fp),
        ):
            await routes._handle_issues(
                _get("issues", {**REPO_Q, "first_page": "1", "refresh": "1"})
            )
            await routes._handle_pulls(_get("pulls", {**REPO_Q, "first_page": "1", "poll": "1"}))
        self.assertEqual(issues_fp.await_args.args[0], KEY)
        self.assertEqual(pulls_fp.await_args.args[0], KEY)


class TestDepsSeams(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_reaches_the_patched_rebuild_on_the_requests_app(self):
        app = web.Application()
        rebuild = AsyncMock(return_value={"edges": [], "nodes": {}, "fetched_at": 1.0})
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_rebuild_deps", rebuild),
        ):
            resp = await routes._handle_deps(_get("deps", {**REPO_Q, "refresh": "1"}, app=app))
        self.assertEqual(
            _body(resp),
            {**routes._identity(KEY), "edges": [], "nodes": {}, "from_cache": False},
        )
        rebuild.assert_awaited_once_with(app, KEY)

    async def test_a_stale_graph_reaches_the_patched_scheduler(self):
        app = web.Application()
        stale = {"edges": [], "nodes": {}, "fetched_at": 0.0}
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_deps_cache", return_value=stale),
            mock.patch.object(routes, "_schedule_deps_refresh") as schedule,
        ):
            resp = await routes._handle_deps(_get("deps", REPO_Q, app=app))
        self.assertTrue(_body(resp)["from_cache"])
        schedule.assert_called_once_with(app, KEY)

    async def test_the_background_refresh_reaches_the_patched_rebuild(self):
        app = web.Application()
        rebuild = AsyncMock(return_value={})
        with mock.patch.object(routes, "_rebuild_deps", rebuild):
            routes._schedule_deps_refresh(app, KEY)
            task = app[routes._DEPS_REFRESH_TASKS_APP_KEY]["github:github.com:o/r"]
            await asyncio.wait_for(task, 5)
        rebuild.assert_awaited_once_with(app, KEY)

    async def test_the_rebuild_reaches_the_patched_issue_loader(self):
        app = web.Application()
        issues = AsyncMock(side_effect=gh.GhCliError("scope boom"))
        with mock.patch.object(routes, "_load_open_issues_for_reco", issues):
            with self.assertRaises(routes._DepsScopeUnavailable) as caught:
                await routes._rebuild_deps(app, KEY)
        self.assertEqual(str(caught.exception), "scope boom")
        issues.assert_awaited_once_with(KEY)
        # The mutex is released on failure, so the next rebuild is not wedged.
        self.assertFalse(routes._deps_rebuild_lock(app, KEY).locked())


class TestTagBatchSeam(unittest.IsolatedAsyncioTestCase):
    async def test_the_batch_cap_is_read_from_routes(self):
        issues = [{"number": n, "labels": []} for n in range(1, 6)]
        compute = AsyncMock(return_value={})
        with (
            mock.patch.object(routes, "_TAG_BATCH_MAX", 2),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(
                routes, "_load_labels_for_ai", AsyncMock(return_value=[{"name": "bug"}])
            ),
            mock.patch.object(routes, "_load_open_issues_for_reco", AsyncMock(return_value=issues)),
            mock.patch.object(routes, "_compute_tagging_suggestions", compute),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(
                store,
                "merge_tagging_suggestions",
                return_value={"suggestions": {}, "generated_at": "t"},
            ),
        ):
            post = await routes._handle_generate_tagging(_post("tagging", REPO))
            get = await routes._handle_get_tagging(_get("tagging", REPO_Q))
        self.assertEqual(len(compute.await_args.args[4]), 2)
        self.assertEqual(_body(post)["remaining"], 3)
        self.assertEqual(_body(get)["batch_size"], 2)


class TestProviderContextPerRequest(unittest.IsolatedAsyncioTestCase):
    """Two concurrent requests for the same slug on different providers never share
    a client, a host or a data root."""

    async def test_concurrent_requests_keep_their_own_provider(self):
        started = threading.Barrier(2, timeout=5)
        calls: list[tuple[str, dict]] = []

        def _fetch(name):
            def _inner(owner, repo, **kwargs):
                started.wait()
                calls.append((name, kwargs))
                return [{"name": name}]

            return _inner

        roots: list[object] = []

        def _refresh(owner, repo, fetch, root=None):
            roots.append(root)
            return fetch()

        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(store, "read_labels_cache", return_value=None),
            mock.patch.object(store, "refresh_labels_cache", side_effect=_refresh),
            mock.patch.object(gh, "list_repo_labels", side_effect=_fetch("github")),
            mock.patch(
                "kiro_crew.apps.builtins.issue_radar.backend.gitlab_client.list_repo_labels",
                side_effect=_fetch("gitlab"),
            ),
        ):
            github, gitlab = await asyncio.gather(
                routes._handle_labels(_get("labels", REPO_Q)),
                routes._handle_labels(
                    _get(
                        "labels",
                        {"owner": "o", "repo": "r", "provider": "gitlab", "host": "gitlab.com"},
                    )
                ),
            )
        self.assertEqual(_body(github)["labels"], [{"name": "github"}])
        self.assertEqual(_body(gitlab)["labels"], [{"name": "gitlab"}])
        self.assertEqual(dict(calls)["gitlab"], {"host": "gitlab.com"})
        self.assertEqual(dict(calls)["github"], {})
        self.assertEqual(len(set(map(str, roots))), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
