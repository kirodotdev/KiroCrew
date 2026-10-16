"""Tests for a write the app's stores refuse.

A refused keystone write answers with the app's coded error instead of a bare 500, and a
ledger-sync remote that carries a credential is refused before it reaches the config file the
app serves without session auth.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes


class TestACredentialBearingRemoteIsRefused(unittest.IsolatedAsyncioTestCase):
    """`data/config.json` is served over `/api/apps/<name>/config` WITHOUT session auth.

    `redact_tokens` has no pattern for a PAT embedded in a URL, so
    `https://user:ghp_xxx@github.com/org/repo.git` pasted into the ledger-sync remote was
    persisted verbatim into a world-readable file and echoed into SEL output. The frontend's
    `displayRemote()` strips userinfo for DISPLAY only — and its own docstring said so
    outright, calling the stored value out as still exposed. A documented hole rather than a
    fixed one; review blocked on it, correctly.

    Refused rather than silently stripped: the pasted token is compromised either way, and a
    remote quietly rewritten to an unauthenticated URL would fail to push later with no hint
    why.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_a_token_remote_is_rejected_and_never_stored(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        token = "ghp_ThisIsTheActualTokenValue"
        app = TestAStoreThatRefusesToWriteIsReportedNotCrashed._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/apps/ops-mission-control/settings",
                    json={"ledger_sync_remote": f"https://u:{token}@github.com/org/repo.git"},
                )
                self.assertEqual(resp.status, 400)
                body = await resp.json()

        self.assertEqual(body["code"], "remote_has_credentials")
        # The refusal must not echo the token back either.
        self.assertNotIn(token, json.dumps(body))
        # ...and nothing was persisted.
        self.assertNotIn(token, json.dumps(read_config()))

    async def test_an_ordinary_remote_still_saves(self):
        """The guard must not break the feature it protects."""
        from aiohttp.test_utils import TestClient, TestServer

        app = TestAStoreThatRefusesToWriteIsReportedNotCrashed._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                for remote in (
                    "https://github.com/org/repo.git",
                    "git@github.com:org/repo.git",  # scp-style, the commonest form
                    "ssh://git@github.com/org/repo.git",
                ):
                    resp = await client.put(
                        "/api/apps/ops-mission-control/settings",
                        json={"ledger_sync_remote": remote},
                    )
                    self.assertEqual(resp.status, 200, f"{remote} must be accepted")

    def test_userinfo_detection_covers_the_shapes_that_matter(self):
        """ANY userinfo on http(s) is refused; scp-style and `ssh://git@` still work.

        The row that matters most here is `https://ghp_xxx@github.com/...` — a token in the
        USERNAME position with no password. An earlier version of this test asserted that as
        ACCEPTABLE, on the reasoning that a bare `user@` is a username rather than a secret.
        That is GitHub's own documented token-remote form, so the likeliest way an operator
        pastes a credential was the one shape the guard let through, blessed by a test. Review
        caught it.

        On http(s) there is no userinfo worth storing in a world-readable file: a real
        username comes from a credential helper, and anything else is a secret.
        """
        for remote, refuse in (
            ("https://user:ghp_secret@github.com/org/repo.git", True),
            ("https://u:p@host:8443/r.git", True),
            # GitHub's documented PAT form: token as username, NO password.
            ("https://ghp_secret@github.com/org/repo.git", True),
            ("https://github_pat_11ABC@github.com/org/repo.git", True),
            ("git@github.com:org/repo.git", False),  # no `://`, so no userinfo component
            ("ssh://git@github.com/org/repo.git", False),  # key auth; username is not a secret
            ("https://github.com/org/repo.git", False),
            ("https://github.com/org/re@po.git", False),  # `@` in the PATH, not the authority
        ):
            self.assertEqual(
                routes._url_has_userinfo(remote), refuse, f"wrong verdict for {remote!r}"
            )


class TestAStoreThatRefusesToWriteIsReportedNotCrashed(unittest.IsolatedAsyncioTestCase):
    """A refused keystone write must answer with the app's coded error, not a bare 500.

    The two stores behind these routes are read-modify-writes that now REFUSE rather than
    publishing over a read they could not make, so each write can raise ``OSError``. Nothing
    converts that into a body: ``sel_audit_middleware`` logs and re-raises, and
    ``deny_audit_middleware`` only handles ``web.HTTPException`` — so an escaping error becomes
    aiohttp's default ``500`` with a plain-text body and no ``code`` to branch on.

    On these particular routes the ambiguity is the security-relevant part. ``mode`` and
    ``autonomy_rules`` ARE the authorization ceiling, and the secret routes decide whether a
    live credential is stored or revoked — "did my change land?" is exactly the question the
    operator must not be left guessing about. ``_handle_rotation_arm`` already set the shape
    for this in the same file (``503 cron_store_unreadable``, with its progress list attached).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("KIROCREW_HOME")
        self.addCleanup(self._restore_home)
        os.environ["KIROCREW_HOME"] = self.tmp

    def _restore_home(self):
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev

    @staticmethod
    def _refuse(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    @staticmethod
    def _owner_app() -> web.Application:
        """An app whose requests read as the dashboard owner (the secret routes are
        owner-gated): no configured owner, caller ``local-app``."""

        @web.middleware
        async def _identity(request, handler):
            request["user"] = "local-app"
            request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[_identity])
        app["state"] = mock.MagicMock(owner_id="")
        return app

    async def _client(self, app):
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_a_refused_secret_save_is_a_coded_503_not_a_500(self):
        token = "u+ThisIsTheActualTokenValue"
        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes, "put_secret", self._refuse):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/providers/pagerduty/secret",
                    json={"field": "api_token", "value": token},
                )
                self.assertEqual(resp.status, 503)
                body = await resp.json()

        self.assertEqual(body["code"], "secret_store_unwritable")
        self.assertIs(body["ok"], False)
        # A refusal must not echo the credential it failed to store.
        self.assertNotIn(token, json.dumps(body))

    async def test_a_refused_revocation_never_reports_success(self):
        """The load-bearing one. The failure this replaces did not 500 — it returned
        ``{"ok": true, "removed": false}``, i.e. "there was nothing to revoke", while the
        live token was still on disk. Anything 2xx here is the bug."""
        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes, "delete_secret", self._refuse):
                client = await self._client(app)
                resp = await client.delete(
                    "/api/apps/ops-mission-control/providers/pagerduty/secret"
                )
                self.assertEqual(resp.status, 503, "a refused revocation answered as success")
                body = await resp.json()

        self.assertEqual(body["code"], "secret_store_unwritable")
        self.assertNotIn("removed", body, "the refusal must not claim a removal verdict")

    async def test_a_corrupt_secret_store_is_a_coded_500_on_save(self):
        """Corruption is not retryable, so it must not be advertised as a 503.

        The store's update reader refuses a corrupt document rather than
        replacing it, so a handler catching only ``OSError`` would surface the
        refusal protecting the operator's only copy of every provider token
        as aiohttp's bare uncoded 500.
        """
        token = "u+ThisIsTheActualTokenValue"
        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)

        def _corrupt(*_a, **_kw):
            raise corrupt

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes, "put_secret", _corrupt):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/providers/pagerduty/secret",
                    json={"field": "api_token", "value": token},
                )
                self.assertEqual(resp.status, 500)
                body = await resp.json()

        self.assertEqual(body["code"], "secret_store_corrupt")
        self.assertIs(body["ok"], False)
        # A refusal must not echo the credential it failed to store, nor the
        # document bytes the decode error may carry.
        self.assertNotIn(token, json.dumps(body))
        self.assertNotIn("{ not json", json.dumps(body))

    async def test_a_corrupt_secret_store_is_a_coded_500_on_revocation(self):
        """The revocation half: the old lenient read answered "nothing to
        revoke" over a store whose token was still on disk; the corrupt refusal
        must leave the operator correctly believing the token is still live."""
        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)

        def _corrupt(*_a, **_kw):
            raise corrupt

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes, "delete_secret", _corrupt):
                client = await self._client(app)
                resp = await client.delete(
                    "/api/apps/ops-mission-control/providers/pagerduty/secret"
                )
                self.assertEqual(resp.status, 500)
                body = await resp.json()

        self.assertEqual(body["code"], "secret_store_corrupt")
        self.assertNotIn("removed", body, "the refusal must not claim a removal verdict")

    async def test_a_refused_ceiling_write_is_a_coded_503(self):
        """The ceiling is the one value where a silent partial apply is a security state."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(policy_store, "set_ceiling", self._refuse):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/settings", json={"mode": "observe"}
                )
                self.assertEqual(resp.status, 503)
                body = await resp.json()

        self.assertEqual(body["code"], "policy_store_unwritable")
        self.assertIs(body["ok"], False)

    async def test_a_refused_provider_config_write_is_a_coded_503(self):
        """The companion strict read landed, so this write can now refuse too.

        This helper's docstring said `set_top_level` had no such guarantee "yet" and
        pointed at the companion PR for `providers/__init__.py`. That PR is this change,
        so `merge_provider_config` now propagates a failed read and this route needed the
        same coded refusal. Found in review (Opus 4.8, Design Review).
        """
        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes, "merge_provider_config", self._refuse):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/providers/datadog/config",
                    json={"site": "datadoghq.eu"},
                )
                self.assertEqual(resp.status, 503)
                body = await resp.json()

        self.assertEqual(body["code"], "app_config_unreadable")

    async def test_a_corrupt_provider_config_is_a_coded_500_not_a_bare_one(self):
        """Corruption is not retryable, so it must not be advertised as a 503.

        Found in review (GPT 5.6): the coded refusal covered only `OSError`, so a malformed
        `config.json` still answered the bare uncoded 500 this PR set out to remove.
        """
        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)

        def _corrupt(*_a, **_kw):
            raise corrupt

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes, "merge_provider_config", _corrupt):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/providers/datadog/config",
                    json={"site": "datadoghq.eu"},
                )
                self.assertEqual(resp.status, 500)
                body = await resp.json()

        self.assertEqual(body["code"], "app_config_corrupt")

    async def test_a_refused_ledger_post_is_a_coded_503_not_a_500(self):
        """POST /ledger appends-or-rewrites under the ledger lock, so it can refuse.

        The ledger trio (POST, DELETE, hygiene) was the last set of mutating routes in
        this file still answering aiohttp's bare plain-text 500 on a refused write —
        every sibling store write already reports ``{ok, error, code}``.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(ledger, "upsert", self._refuse):
                client = await self._client(app)
                resp = await client.post(
                    "/api/apps/ops-mission-control/ledger",
                    json={"pattern": "disk full on host", "fix": "clear the spool"},
                )
                self.assertEqual(resp.status, 503)
                body = await resp.json()

        self.assertEqual(body["code"], "ledger_store_unwritable")
        self.assertIs(body["ok"], False)

    async def test_a_refused_ledger_delete_never_reports_success(self):
        """Same property as the secret-revocation route: anything 2xx here is the bug.

        A refused rewrite means the entry the operator just deleted is still on disk,
        and the previous escape (bare 500) left them unable to tell that from a crash
        after the write. The coded 503 says plainly: still there, retry.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(ledger, "remove", self._refuse):
                client = await self._client(app)
                resp = await client.delete(
                    "/api/apps/ops-mission-control/ledger?id=abc123",
                )
                self.assertEqual(resp.status, 503, "a refused removal answered as success")
                body = await resp.json()

        self.assertEqual(body["code"], "ledger_store_unwritable")
        self.assertNotIn("removed", body, "the refusal must not claim a removal verdict")

    async def test_a_failed_underlying_read_on_delete_is_a_503_not_a_404(self):
        """The read half of the same fault, driven from below the handler.

        ``remove`` starts from a read of the file it rewrites. When that READ was the
        thing that failed, the lenient read collapsed it to ``[]`` and the route
        answered a coded 404 — "no such entry" — about an entry still on disk, with no
        failure audit. Found in review (GPT 5.6). With the mutation path on the strict
        read, the same fault now surfaces as the identical retryable 503 the write
        half answers, and the entry is not mis-reported as absent.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(ledger, "read_entries_for_update", self._refuse):
                client = await self._client(app)
                resp = await client.delete(
                    "/api/apps/ops-mission-control/ledger?id=abc123",
                )
                self.assertNotEqual(resp.status, 404, "a failed read was reported as absence")
                self.assertEqual(resp.status, 503)
                body = await resp.json()

        self.assertEqual(body["code"], "ledger_store_unwritable")

    async def test_a_refused_hygiene_rewrite_is_a_coded_503_and_audited(self):
        """``hygiene`` rewrites the whole ledger; a refused rewrite must not push.

        Also pins that the refusal is AUDITED as a failure: the success path writes a
        ``ledger_hygiene`` audit line, and a refusal that skipped the audit would make
        the maintenance pass look like it never ran rather than like it failed.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_sync

        directions: list[str] = []

        async def _sync(*, direction="pull"):
            directions.append(direction)
            return ""

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "is_primary", return_value=True):
                with mock.patch.object(ledger_sync, "sync_safely", _sync):
                    with mock.patch.object(ledger, "hygiene", self._refuse):
                        with mock.patch.object(routes, "_audit") as audited:
                            client = await self._client(app)
                            resp = await client.post("/api/apps/ops-mission-control/ledger/hygiene")
                            self.assertEqual(resp.status, 503)
                            body = await resp.json()

        self.assertEqual(body["code"], "ledger_store_unwritable")
        self.assertIs(body["ok"], False)
        self.assertNotIn(
            "push", directions, "a ledger the dedupe never committed to must not be pushed"
        )
        failures = [
            c
            for c in audited.call_args_list
            if c.args[0] == "ledger_hygiene" and c.args[2] == "failure"
        ]
        self.assertEqual(len(failures), 1, "the refusal must be audited as a failure")

    async def test_a_claim_survives_a_failed_ledger_annotation(self):
        """The claim is committed before the annotation runs, so it must not be un-reported.

        `attach_ledger_matches` writes through `store.update_fields` -> `transition` ->
        `_read_index_for_update()`, which this change gave two new ways to raise. Unguarded it
        answered a bare uncoded 500 for a claim that had ALREADY been written durably and whose
        webhook had already been acked. Found in review (Opus 4.8).

        Its suggested fix was the coded 503 the sibling routes use, and that is wrong here for
        the same reason it was wrong at `_schedule_verification`: a 503 says "the claim failed,
        retry" about a claim that succeeded, the retry answers `409 signal_already_claimed`, and
        the audit entry for a real claim is never written. The annotation is the deferrable half
        -- the dispatch cycle re-derives matches next pass -- so the degraded answer is the
        claim without its matches, still 200, still audited, with the degradation recorded.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, models

        firing = models.Signal.create(
            source="cloudwatch", native_id="alarm/x", title="broke", labels={"alarm_name": "x"}
        )

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.get_registry(), "poll_all", mock.AsyncMock(return_value=([firing], {}))
            ):
                with mock.patch.object(dispatch, "attach_ledger_matches", self._refuse):
                    with mock.patch.object(routes, "_audit") as audited:
                        client = await self._client(app)
                        resp = await client.post(
                            "/api/apps/ops-mission-control/incident/claim",
                            json={"signal": {"id": firing.id, "source": "cloudwatch"}},
                        )
                        body = await resp.json()

        self.assertEqual(resp.status, 200, "a committed claim was reported as failed")
        self.assertTrue(body["incident"]["incident_id"])
        self.assertEqual(body["matches"], [], "matches degrade to empty, not to an error")
        claims = [c for c in audited.call_args_list if c.args[0] == "incident_claim"]
        self.assertEqual(len(claims), 1, "the claim must be audited exactly once")
        self.assertEqual(claims[0].args[2], "success")
        self.assertIn("without ledger matches", claims[0].kwargs.get("error", ""))

    async def test_a_corrupt_index_is_not_reported_as_an_illegal_transition(self):
        """The misclassification, pinned. This is the worst of the uncoded cases.

        `_handle_transition` caught `except ValueError`, and `JSONDecodeError` subclasses
        `ValueError`, so a corrupt index answered `409 illegal_transition` carrying the JSON
        parser's message -- telling the operator their own state change was invalid when the
        real fault was a broken file. A 409 sends them to fix their request; nothing sends
        them to the file. Found in review (Opus 4.8); it is the same accident this change
        closed at three non-route callers.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)

        def _corrupt(*_a, **_kw):
            raise corrupt

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(store, "get_incident", lambda *_a, **_kw: None):
                with mock.patch.object(store, "transition", _corrupt):
                    client = await self._client(app)
                    resp = await client.post(
                        "/api/apps/ops-mission-control/incident/transition",
                        json={"id": "INV-1", "status": "resolved"},
                    )
                    body = await resp.json()

        self.assertNotEqual(resp.status, 409, "corruption was blamed on the operator's request")
        self.assertNotEqual(body.get("code"), "illegal_transition")
        self.assertEqual(resp.status, 500)
        self.assertEqual(body["code"], "dispatch_index_corrupt")

    async def test_an_unreadable_index_on_transition_is_a_retryable_coded_503(self):
        """The other half: transient gets 503, so "retry" is only said when it can work."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(store, "get_incident", lambda *_a, **_kw: None):
                with mock.patch.object(store, "transition", self._refuse):
                    client = await self._client(app)
                    resp = await client.post(
                        "/api/apps/ops-mission-control/incident/transition",
                        json={"id": "INV-1", "status": "resolved"},
                    )
                    body = await resp.json()

        self.assertEqual(resp.status, 503)
        self.assertEqual(body["code"], "dispatch_index_unreadable")

    async def test_a_refused_manual_claim_is_coded_rather_than_a_bare_500(self):
        """`claim` raises on both failures by design, so the board's own route must translate."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        firing = models.Signal.create(
            source="cloudwatch", native_id="alarm/x", title="broke", labels={"alarm_name": "x"}
        )

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(
                routes.get_registry(), "poll_all", mock.AsyncMock(return_value=([firing], {}))
            ):
                with mock.patch.object(store, "claim", self._refuse):
                    client = await self._client(app)
                    resp = await client.post(
                        "/api/apps/ops-mission-control/incident/claim",
                        json={"signal": {"id": firing.id, "source": "cloudwatch"}},
                    )

        self.assertEqual(resp.status, 503)
        self.assertEqual((await resp.json())["code"], "dispatch_index_unreadable")

    async def test_a_corrupt_policy_store_on_settings_is_a_coded_500(self):
        """The regression this change introduced into a MERGED helper.

        `_settings_write_or_refuse` caught only `OSError`. Making the config reader refuse on
        a malformed document gave `set_top_level` a second way to raise, and the bare 500 came
        back for it -- in the one helper whose docstring had promised this PR would close that
        gap. Found in review (First Principles).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)

        def _corrupt(*_a, **_kw):
            raise corrupt

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(policy_store, "set_ceiling", _corrupt):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/settings", json={"mode": "observe"}
                )
                body = await resp.json()

        self.assertEqual(resp.status, 500)
        self.assertEqual(body["code"], "policy_store_corrupt")
        self.assertIs(body["ok"], False)

    async def test_a_corrupt_index_is_not_reported_as_an_invalid_proposal(self):
        """The last reachable `ValueError` arm, found by auditing all ten in this file.

        `propose_action` reaches the strict reader through `update_fields` -> `transition`,
        so a corrupt index raises `CorruptDocumentError` -- a `ValueError` -- and the
        `except ValueError` arm reported it as `400 invalid_proposal`. That is worse than the
        bare 500 it replaces: a 400 blames the operator's proposal, so they re-type the form
        while the real fault sits on disk. Nine of the ten arms needed nothing -- three
        already carried this clause and six cannot reach a strict reader at all.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        corrupt = json.JSONDecodeError("Expecting value", "{ not json", 2)

        def _corrupt(*_a, **_kw):
            raise corrupt

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(store, "propose_action", _corrupt):
                client = await self._client(app)
                resp = await client.post(
                    "/api/apps/ops-mission-control/incident/propose",
                    json={"id": "INV-1", "action": "silence", "sink": "pagerduty"},
                )
                body = await resp.json()

        self.assertNotEqual(resp.status, 400, "corruption was blamed on the operator's proposal")
        self.assertNotEqual(body.get("code"), "invalid_proposal")
        self.assertEqual(resp.status, 500)
        self.assertEqual(body["code"], "dispatch_index_corrupt")

    async def test_a_refused_proposal_decision_is_a_coded_503(self):
        """A human's approval of a production action must not fail ambiguously.

        `decide_proposal` is a locked read-modify-write that now refuses rather than
        publishing over a failed read. Before this arm the handler could only answer a
        coded 404, so a transient read failure became a bare 500 with no `code`.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(store, "decide_proposal", self._refuse):
                client = await self._client(app)
                resp = await client.post(
                    "/api/apps/ops-mission-control/incident/proposal/decide",
                    json={"id": "INV-1", "approve": True},
                )
                self.assertEqual(resp.status, 503)
                body = await resp.json()

        self.assertEqual(body["code"], "dispatch_index_unreadable")

    async def test_a_refused_destination_write_is_a_coded_503_too(self):
        """The keys reached THROUGH an intermediary, which the first pass missed.

        `slack_out.set_settings` and `ledger_sync.set_settings` call `policy_store.put`
        internally -- their keys are operator-only, so they must -- so guarding only the
        call sites that literally spell `policy_store` left the destination writes
        answering a refused keystone write with a plain 500. These are the keys an agent
        must not be able to redirect, so they are the last place to leave the operator
        guessing. Found in review (GPT 5.6).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(slack_out, "set_settings", self._refuse):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/settings", json={"slack_channel": "C123456"}
                )
                self.assertEqual(resp.status, 503, "a refused destination write answered 200")
                body = await resp.json()

        self.assertEqual(body["code"], "policy_store_unwritable")

    async def test_a_refusal_body_carries_no_field_the_dashboard_discards(self):
        """`req` in the app's own api.ts reads only `error` from a non-2xx body.

        An earlier revision shipped an `applied` key here on the reasoning that naming
        what landed is actionable. It is -- but not there, because nothing reads it. The
        partial set goes to the audit log instead, which has a reader and mirrors the
        `settings_put` line the success path already writes.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(policy_store, "set_ceiling", self._refuse):
                client = await self._client(app)
                resp = await client.put(
                    "/api/apps/ops-mission-control/settings", json={"mode": "observe"}
                )
                body = await resp.json()

        self.assertNotIn("applied", body)
        self.assertEqual(sorted(body), ["code", "error", "ok"])

    async def test_an_ordinary_settings_write_still_succeeds(self):
        """Negative control: the guard must not break the route it protects."""
        app = self._owner_app()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            client = await self._client(app)
            resp = await client.put(
                "/api/apps/ops-mission-control/settings", json={"mode": "observe"}
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()

        self.assertIs(body["ok"], True)
        self.assertEqual(body["applied"]["mode"], "observe")
