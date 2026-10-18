"""Tests for the dashboard-authed webhook management endpoints.

Also covers the run-history side effects of the ``POST /api/hooks/agent``
rejection paths (401 unauthorized, 429 capacity), which are the only record
of a call that never became a run.

The read and source-token routes and their body gates are in
``test_webhooks_api_read_and_tokens.py``; the fixture and request builders both
share are in ``webhooks_api_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from webhooks_api_helpers import wired_fixture  # noqa: F401
from webhooks_api_helpers import _bearer, _payload, _req

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H


class TestEveryStoreTouchingHandlerIsGuarded:
    """A store failure must never reach the client as a 500.

    Reads refuse rather than reporting an empty file, and the shared hooks.json
    write refuses rather than erasing the webhook contexts beside it. Guarding
    these refusals one handler at a time misses new handlers, so this pins the
    CLASS instead of the instances: every handler that touches a store is wrapped.
    """

    @pytest.mark.asyncio
    async def test_revocation_reports_an_unavailable_store(self, wired):
        """The specific gap that was reported: revoking against a corrupt store."""
        store = webhooks.token_store()
        _raw, _secret, entry = store.create("Review Bot")
        store.path.write_text("{truncated", encoding="utf-8")

        resp = await H.api_webhook_token_delete(
            _req(
                "DELETE",
                f"/api/webhooks/tokens/{entry['id']}",
                match_info={"token_id": entry["id"]},
            )
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"
        # The refusal must not have altered the file.
        assert store.path.read_text(encoding="utf-8") == "{truncated"

    def test_all_store_touching_handlers_carry_the_guard(self):
        """Structural: a new store-touching handler cannot ship unguarded.

        Checked by reading the source rather than by exercising each route, so the
        assertion covers handlers this suite has no fixture for. Without it the
        next handler added could reintroduce the bug unnoticed.
        """
        import importlib
        import inspect
        import re

        source = inspect.getsource(H)
        # a mirror facade: its code is in the owners it lists
        source += "".join(inspect.getsource(importlib.import_module(o)) for o in H._OWNERS)
        # Handlers, with the decorator line (if any) that precedes them.
        pattern = re.compile(
            r"(@_store_failure_guard\n)?async def (api_[a-z_]+)\(request: web\.Request\)"
        )
        store_call = re.compile(
            r"(token_store\(\)|run_store\(\)|_get_hook_store|_mutate_hook_store|"
            r"_webhooks_snapshot|_delete_hook_context)"
        )
        # One deliberate exemption: the externally reachable route answers store
        # failure with a NEUTRAL `webhooks_unavailable` 503 instead of the
        # dashboard's diagnostic `store_unavailable`, because an outside caller
        # should not learn that the operator's store needs repair. Its store
        # touches are individually guarded, so nothing escapes as a 500.
        exempt = {"api_hooks_agent"}

        handlers = list(pattern.finditer(source))
        unguarded = []
        for idx, match in enumerate(handlers):
            body_end = handlers[idx + 1].start() if idx + 1 < len(handlers) else len(source)
            body = source[match.end() : body_end]
            name = match.group(2)
            if store_call.search(body) and not match.group(1) and name not in exempt:
                unguarded.append(name)

        assert (
            not unguarded
        ), "these handlers touch a store but lack @_store_failure_guard: " + ", ".join(unguarded)


class TestOneTurnPerSessionKey:
    """A second concurrent call on the same sessionKey must be refused.

    `sessionKey` is caller-chosen and `register_hook` hands the same id to
    whatever calls back, so two valid calls can share one. Both resolve to a
    SINGLE session, and the runner's cleanup releases and resets by session key
    rather than by ownership — so the second turn tearing down destroys the
    first's live session, losing a turn whose caller already received a 200.
    """

    @pytest.mark.asyncio
    async def test_an_overlapping_call_is_refused_with_409(self, wired):
        # The guard sits AFTER auth on purpose: an unauthenticated caller must not
        # be able to probe which session keys are busy. So this needs a real
        # credential and signature, like the capacity test.
        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        session_key = f"{H._HOOK_SESSION_PREFIX}shared"
        req = _req(
            "POST",
            "/api/hooks/agent",
            {"message": "go", "sessionKey": session_key},
            headers={"Authorization": f"Bearer {raw}"},
            sign_with=secret,
        )

        # A turn is already in flight for this key.
        H._hook_inflight_sessions.add(session_key)
        try:
            resp = await H.api_hooks_agent(req)
            assert resp.status == 409
            assert (await _payload(resp))["code"] == "session_busy"
        finally:
            H._hook_inflight_sessions.discard(session_key)

        # The rejection is visible in the run history, like the other reject paths.
        runs = webhooks.run_store().list_runs()
        assert runs, "premise: the rejection was recorded"
        assert "still running" in (runs[0]["detail"] or "")

    @pytest.mark.asyncio
    async def test_concurrent_same_key_is_claimed_before_capacity_await(self, wired, monkeypatch):
        """A yielding capacity acquire cannot admit two turns for one key."""

        class YieldingSemaphore:
            def __init__(self):
                self.entered = asyncio.Event()
                self.allow = asyncio.Event()
                self.acquire_calls = 0
                self.release_calls = 0

            def locked(self):
                return False

            async def acquire(self):
                self.acquire_calls += 1
                self.entered.set()
                await self.allow.wait()
                return True

            def release(self):
                self.release_calls += 1

        semaphore = YieldingSemaphore()
        monkeypatch.setattr(H, "_hook_semaphore", semaphore)
        run = AsyncMock(return_value=None)
        monkeypatch.setattr(H, "_run_hook_agent", run)

        raw, secret, _entry = webhooks.token_store().create("Review Bot")
        session_key = f"{H._HOOK_SESSION_PREFIX}simultaneous"

        def request(message):
            return _req(
                "POST",
                "/api/hooks/agent",
                {"message": message, "sessionKey": session_key},
                headers={"Authorization": f"Bearer {raw}"},
                sign_with=secret,
            )

        # The waits below are hang guards, not timing assertions: the test
        # proves ORDER (the claim lands before the capacity await), and a loaded
        # Windows runner has taken longer than a second to schedule the first
        # request's coroutine as far as acquire().
        bound = 10
        first = asyncio.create_task(H.api_hooks_agent(request("first")))
        await asyncio.wait_for(semaphore.entered.wait(), timeout=bound)

        # The first request is paused inside acquire(). The claim must already
        # be visible, so the second request finishes with session_busy instead
        # of joining it at the capacity await.
        second = await asyncio.wait_for(H.api_hooks_agent(request("second")), timeout=bound)
        assert second.status == 409
        assert (await _payload(second))["code"] == "session_busy"
        assert semaphore.acquire_calls == 1

        semaphore.allow.set()
        accepted = await asyncio.wait_for(first, timeout=bound)
        assert accepted.status == 200
        await asyncio.sleep(0)
        run.assert_awaited_once()

        H._hook_inflight_sessions.discard(session_key)
        semaphore.release()

    @pytest.mark.asyncio
    async def test_the_key_is_released_when_the_turn_finishes(self, wired, monkeypatch):
        """A completed turn must not leave its key claimed forever."""
        monkeypatch.setattr(H, "_run_hook_inner", AsyncMock(return_value="done"))
        state = MagicMock(memory_startup_task=None)
        state.sessions.record_failure = AsyncMock()
        state.sessions.reset = AsyncMock()
        state.owner_id = None
        state.slack_client = None
        state.notify = MagicMock()

        session_key = f"{H._HOOK_SESSION_PREFIX}release"
        H._hook_inflight_sessions.add(session_key)
        before = H._hook_semaphore._value
        await H._hook_semaphore.acquire()
        await H._run_hook_agent(
            state,
            session_key,
            "hi",
            "Bot",
            None,
            True,
            30,
        )

        assert session_key not in H._hook_inflight_sessions, "key stayed claimed"
        assert H._hook_semaphore._value == before, "capacity semaphore leaked a permit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("crashed", [False, True])
    async def test_teardown_withdraws_the_vouched_execution_identity(
        self, wired, monkeypatch, crashed
    ):
        """A finished hook turn must not leave its identity in the process.

        Same shape as the two leaks above, on a third resource. Hook keys are
        per-request by default (``hook:default:{ts}``) and per-event keys are the
        ordinary webhook pattern, so an entry that outlives the turn means one
        permanent entry per authenticated request, recoverable only by restarting
        the gateway. Neither ``release`` nor ``reset`` reaches it: ``reset``
        recycles the session without going near the execution maps.

        Parametrized over a crashed turn because the withdrawal lives in the
        ``finally``, so the crash path must clear it too -- that is the path a
        leak would otherwise accumulate on fastest.

        The bind here stands in for ``_run_hook_inner``'s own, which is stubbed
        out: the subject is the teardown, and the inner function's persistent-only
        mode guard is what guarantees the entry is the vouched one.
        """
        from dataclasses import replace as dataclass_replace

        from kiro_crew.execution_context import (
            MemoryStoreRef,
            bind_session_execution,
            execution_for_store,
            read_session_execution,
            read_vouched_session_execution,
        )

        inner = AsyncMock(
            side_effect=RuntimeError("boom") if crashed else None,
            return_value=None if crashed else "done",
        )
        monkeypatch.setattr(H, "_run_hook_inner", inner)
        state = MagicMock(memory_startup_task=None)
        state.sessions.record_failure = AsyncMock()
        state.sessions.reset = AsyncMock()
        state.owner_id = None
        state.slack_client = None
        state.notify = MagicMock()

        session_key = f"{H._HOOK_SESSION_PREFIX}default:1700000000"
        # The default store, because the map write this pins is store-agnostic -- but it
        # is NOT member-agnostic: a member-less execution is refused the vouch outright,
        # so the id below is what makes this a vouched entry at all.
        execution = dataclass_replace(
            execution_for_store("", memory_mode="persistent"),
            member_id="id-hook",
            store=MemoryStoreRef("hook-store", "id-hook"),
            template_id="kirocrew",
        )
        bind_session_execution(session_key, execution, replace_existing=True, vouch=True)
        assert read_vouched_session_execution(session_key) == execution, "bind vouched nothing"

        H._hook_inflight_sessions.add(session_key)
        await H._hook_semaphore.acquire()
        await H._run_hook_agent(state, session_key, "hi", "Bot", None, True, 30)

        assert read_vouched_session_execution(session_key) is None, "vouched identity outlived it"
        # The durable record is deliberately untouched: withdrawing this process's
        # word is not the same as forgetting what the session was.
        assert read_session_execution(session_key) == execution

    @pytest.mark.asyncio
    async def test_a_crashed_turn_still_releases_the_key(self, wired, monkeypatch):
        """A failing turn must not wedge the session key permanently."""
        monkeypatch.setattr(H, "_run_hook_inner", AsyncMock(side_effect=RuntimeError("boom")))
        state = MagicMock(memory_startup_task=None)
        state.sessions.record_failure = AsyncMock()
        state.sessions.reset = AsyncMock()
        state.owner_id = None
        state.slack_client = None
        state.notify = MagicMock()

        session_key = f"{H._HOOK_SESSION_PREFIX}crash"
        H._hook_inflight_sessions.add(session_key)
        await H._hook_semaphore.acquire()
        await H._run_hook_agent(
            state,
            session_key,
            "hi",
            "Bot",
            None,
            True,
            30,
        )

        assert session_key not in H._hook_inflight_sessions, "crashed turn wedged the key"


class TestDeliveryIsRecordedAfterItHappens:
    """Run history must name only destinations that actually received the result.

    Deriving `delivered` from intent (`deliver and result_text`) and writing the
    record before delivery is attempted is wrong: Slack failures are caught
    and logged, so a run whose DM fails would still be stored as delivered to Slack —
    and the run history is the only place an operator can check.
    """

    @staticmethod
    def _state():
        state = MagicMock(memory_startup_task=None)
        state.sessions.record_failure = AsyncMock()
        state.sessions.reset = AsyncMock()
        return state

    @staticmethod
    async def _run(state):
        """Drive the runner the way the endpoint does, and prove no permit leaks.

        ``_run_hook_agent`` releases the capacity semaphore in its finally block —
        the ENDPOINT acquires it before spawning the task. Calling the runner
        directly without acquiring first leaves the process one spare permit per
        call, which inflates capacity for every later test in the session: the 429
        capacity test saw a 200 whenever the random order placed it after these.
        Acquire first, then assert the count came back, so a regression fails here
        rather than in an unrelated test.
        """
        before = H._hook_semaphore._value
        await H._hook_semaphore.acquire()
        await H._run_hook_agent(
            state,
            f"{H._HOOK_SESSION_PREFIX}t",
            "hi",
            "Bot",
            None,
            True,
            30,
        )
        assert H._hook_semaphore._value == before, "capacity semaphore leaked a permit"

    @pytest.mark.asyncio
    async def test_a_failed_slack_dm_is_not_recorded_as_delivered(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_run_hook_inner", AsyncMock(return_value="the agent answer"))
        state = self._state()
        state.owner_id = "U123"
        state.notify = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="D1")
        state.slack_client.post_message = AsyncMock(side_effect=RuntimeError("slack down"))

        await self._run(state)

        runs = webhooks.run_store().list_runs()
        assert runs, "premise: the run was recorded"
        row = runs[0]
        # Notifications succeeded, Slack did not: neither the flag nor the detail
        # may claim the DM landed.
        assert "Slack" not in (row["detail"] or "")
        assert row["detail"] == "Delivered to notifications"
        assert row["delivered"] is True

    @pytest.mark.asyncio
    async def test_every_destination_failing_is_not_recorded_as_delivered(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_run_hook_inner", AsyncMock(return_value="the agent answer"))
        state = self._state()
        state.owner_id = None
        state.slack_client = None
        state.notify = MagicMock(side_effect=RuntimeError("notifier down"))

        await self._run(state)

        row = webhooks.run_store().list_runs()[0]
        assert row["delivered"] is False
        assert row["detail"] == "Delivery failed for every destination"

    @pytest.mark.asyncio
    async def test_both_destinations_succeeding_names_both(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_run_hook_inner", AsyncMock(return_value="the agent answer"))
        state = self._state()
        state.owner_id = "U123"
        state.notify = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="D1")
        state.slack_client.post_message = AsyncMock(return_value=None)

        await self._run(state)

        row = webhooks.run_store().list_runs()[0]
        assert row["delivered"] is True
        assert row["detail"] == "Delivered to notifications + Slack DM"


class TestStoreFailuresDoNotCrashMutations:
    """A store that cannot be read or written must not surface as a 500.

    Reads refuse rather than reporting an empty file, and the shared hooks.json
    write refuses rather than erasing the webhook contexts kept alongside the
    script hooks. Both refusals reach these handlers, so each has to answer with
    something a client can act on.
    """

    @pytest.mark.asyncio
    async def test_the_kill_switch_reports_an_unavailable_store(self, wired):
        store = webhooks.token_store()
        store.create("live")
        store.path.write_text("{truncated", encoding="utf-8")

        resp = await H.api_webhooks_switch(_req("POST", "/api/webhooks/switch", {"enabled": False}))

        assert resp.status == 503
        body = await _payload(resp)
        assert body["code"] == "store_unavailable"
        # The refusal must not have changed anything on disk.
        assert store.path.read_text(encoding="utf-8") == "{truncated"

    @pytest.mark.asyncio
    async def test_a_script_hook_create_reports_an_unavailable_store(self, wired, monkeypatch):
        """The shared-store refusal reaches the script-hook handlers too.

        Driven through a stub store that raises what ``ScriptHookStore._save``
        now raises, because this module's request harness supplies a MagicMock
        ``app["state"]`` and so never reaches a real store. The refusal itself is
        covered end-to-end in test_hooks_json_shared_file.py; what matters here is
        that the handler turns it into a response instead of a 500.
        """

        class _RefusingStore:
            def create(self, _validated):
                raise webhooks.WebhookStoreUnreadable("hooks.json is unreadable")

        monkeypatch.setattr(H, "_get_hook_store", lambda _state: _RefusingStore())

        resp = await H.api_hooks_create(
            _req("POST", "/api/hooks", {"name": "fmt", "event": "Stop", "command": "true"})
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"


class TestRunHistoryRedaction:
    """Run history is caller-controlled text and must not leave unscrubbed.

    Every string on a run comes from outside the gateway: ``hook_id`` and
    ``session_key`` are whatever ``register_hook`` was called with, ``name`` is a
    free-text field on the inbound body, and ``detail`` quotes caller-derived
    reasons. A credential pasted into any of them would otherwise be stored
    verbatim and rendered on the dashboard.
    """

    @pytest.mark.asyncio
    async def test_every_caller_supplied_run_field_is_redacted(self, wired):
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        runs = webhooks.run_store()
        runs.record(
            outcome=webhooks.OUTCOME_COMPLETED,
            hook_id=f"hook-{secret}",
            session_key=f"{H._HOOK_SESSION_PREFIX}{secret}",
            name=f"bot {secret}",
            detail=f"refused because {secret}",
        )

        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))

        assert data["runs"], "premise: the run was recorded"
        blob = json.dumps(data["runs"])
        assert secret not in blob, "a credential reached the dashboard payload"
        for field in ("hook_id", "session_key", "name", "detail"):
            assert secret not in str(data["runs"][0].get(field) or "")

    @pytest.mark.asyncio
    async def test_redaction_does_not_empty_the_run_list(self, wired):
        """Scrubbing must not blank the fields the UI groups runs by."""
        runs = webhooks.run_store()
        runs.record(
            outcome=webhooks.OUTCOME_COMPLETED,
            hook_id="ci-deploy",
            session_key=f"{H._HOOK_SESSION_PREFIX}ci-deploy",
            name="Deploy Bot",
        )

        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))

        row = data["runs"][0]
        assert row["hook_id"] == "ci-deploy"
        assert row["name"] == "Deploy Bot"
        assert row["outcome"] == webhooks.OUTCOME_COMPLETED


class TestUnreadableStoreResponses:
    """A store that cannot be parsed must produce named errors, not 500s.

    Store reads refuse rather than reporting an empty file (which would otherwise let
    a single corrupt byte destroy every credential). That refusal is a raised
    exception, so every request path that touches a store needs to answer with
    something meaningful instead of letting it become an unhandled 500.
    """

    @pytest.mark.asyncio
    async def test_the_external_endpoint_answers_503(self, wired):
        store = webhooks.token_store()
        store.create("live")
        store.path.write_text("{truncated", encoding="utf-8")

        resp = await H.api_hooks_agent(_req("POST", "/api/hooks/agent", {"message": "hi"}))

        assert resp.status == 503
        body = await _payload(resp)
        assert body["error"] == "inbound webhooks are unavailable"

    @pytest.mark.asyncio
    async def test_the_dashboard_read_answers_503_with_the_reason(self, wired):
        store = webhooks.token_store()
        store.create("live")
        store.path.write_text("{truncated", encoding="utf-8")

        resp = await H.api_webhooks(_req("GET", "/api/webhooks"))

        assert resp.status == 503
        assert "unreadable" in (await _payload(resp))["error"]

    @pytest.mark.asyncio
    async def test_an_unreadable_history_still_lets_a_turn_be_rejected(self, wired):
        """The run-history write must not change the response the caller gets."""
        webhooks.run_store().record(outcome=webhooks.OUTCOME_COMPLETED, name="seed")
        webhooks.run_store().path.write_text("{truncated", encoding="utf-8")
        webhooks.token_store().set_switch(False)

        resp = await H.api_hooks_agent(_req("POST", "/api/hooks/agent", {"message": "hi"}))

        # 503 for the disabled switch — NOT a 500 from the history refusal.
        assert resp.status == 503
        assert (await _payload(resp))["error"] == "inbound webhooks are disabled"


class TestVerifyHookToken:
    @pytest.mark.asyncio
    async def test_header_variants_and_legacy(self, wired, monkeypatch):
        raw, _secret, entry = webhooks.token_store().create("Review Bot")
        assert H._verify_hook_token(_bearer(raw)) == entry["id"]

        alt = make_mocked_request("POST", "/api/hooks/agent", headers={"x-kirocrew-token": raw})
        assert H._verify_hook_token(alt) == entry["id"]

        bare = make_mocked_request("POST", "/api/hooks/agent")
        assert H._verify_hook_token(bare) is None

        monkeypatch.setattr(H, "_legacy_hook_token", lambda: "cfg-secret")
        assert H._verify_hook_token(_bearer("cfg-secret")) == "legacy"
        assert H._verify_hook_token(_bearer("wrong")) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["Authorization", "x-kirocrew-token"])
    @pytest.mark.parametrize("candidate", ["é", "\udcff", "\ud800"])
    async def test_non_ascii_request_credential_is_unauthorized(
        self, wired, monkeypatch, header: str, candidate: str
    ) -> None:
        """Every request-fed token shape fails closed at the bearer gate."""
        webhooks.token_store().create("active source")
        monkeypatch.setattr(H, "_legacy_hook_token", lambda: "configured-secret")
        value = f"Bearer {candidate}" if header == "Authorization" else candidate

        request = MagicMock(headers={header: value}, remote="test-client")

        resp = await H.api_hooks_agent(request)

        assert resp.status == 401
        assert await _payload(resp) == {"error": "unauthorized", "code": "unauthorized"}

    def test_legacy_reader_tolerates_non_dict_config(self, monkeypatch):
        cfg = MagicMock()
        cfg.hooks = ["not", "a", "dict"]
        monkeypatch.setattr(H.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        assert H._legacy_hook_token() == ""


class TestContextDelete:
    @pytest.mark.asyncio
    async def test_delete_context(self, wired):
        (wired / "hooks.json").write_text(
            json.dumps({"a": {"context_summary": "x", "registered_at": time.time()}}),
            encoding="utf-8",
        )
        resp = await H.api_webhook_context_delete(
            _req("DELETE", "/api/webhooks/contexts/a", match_info={"hook_id": "a"})
        )
        assert resp.status == 200
        assert json.loads((wired / "hooks.json").read_text(encoding="utf-8")) == {}

    @pytest.mark.asyncio
    async def test_delete_unknown_context_is_404(self, wired):
        (wired / "hooks.json").write_text(json.dumps({}), encoding="utf-8")
        resp = await H.api_webhook_context_delete(
            _req("DELETE", "/api/webhooks/contexts/zzz", match_info={"hook_id": "zzz"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_preserves_sibling_registrations(self, wired):
        (wired / "hooks.json").write_text(
            json.dumps({"a": {"context_summary": "x"}, "b": {"context_summary": "y"}}),
            encoding="utf-8",
        )
        await H.api_webhook_context_delete(
            _req("DELETE", "/api/webhooks/contexts/a", match_info={"hook_id": "a"})
        )
        assert list(json.loads((wired / "hooks.json").read_text(encoding="utf-8"))) == ["b"]


class TestWebhookTest:
    @pytest.mark.asyncio
    async def test_probe_token_is_real_and_cleaned_up(self, wired, monkeypatch):
        seen: dict = {}

        class _Resp:
            status = 200

            async def text(self):
                return "{}"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, data=None, headers=None):
                seen["url"] = url
                seen["data"] = data
                seen["json"] = json.loads(data.decode("utf-8"))
                seen["headers"] = headers
                # The probe token must be live at the moment of the call.
                seen["verified"] = webhooks.token_store().verify(
                    headers["Authorization"].removeprefix("Bearer ")
                )
                seen["entry"] = webhooks.token_store().entry_for(seen["verified"])
                return _Resp()

        monkeypatch.setattr("aiohttp.ClientSession", _Session)
        resp = await H.api_webhook_test(_req("POST", "/api/webhooks/test", {"agent": "oncall"}))
        data = await _payload(resp)
        assert data["ok"] is True
        assert data["session_key"].startswith("hook:test:")
        assert seen["url"] == "http://127.0.0.1:6776/api/hooks/agent"
        assert seen["json"]["deliver"] is False
        assert seen["json"]["message"]
        assert seen["json"]["agent"] == "oncall"
        assert seen["verified"] is not None
        assert seen["entry"]["agent"] == "oncall"
        # The probe must exercise the signing path, not bypass it: its token
        # requires a signature and the headers it sent must verify against the
        # exact bytes it posted.
        assert seen["entry"]["require_signature"] is True
        assert (
            webhooks.verify_signature(
                secret=seen["entry"]["signing_secret"],
                timestamp=seen["headers"][webhooks.TIMESTAMP_HEADER],
                signature=seen["headers"][webhooks.SIGNATURE_HEADER],
                body=seen["data"],
            )
            is None
        )
        # Probe token revoked afterwards — no residue in the store.
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_custom_message_used(self, wired, monkeypatch):
        seen: dict = {}

        class _Resp:
            status = 200

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, data=None, headers=None):
                seen.update(json.loads(data.decode("utf-8")))
                return _Resp()

        monkeypatch.setattr("aiohttp.ClientSession", _Session)
        await H.api_webhook_test(_req("POST", "/api/webhooks/test", {"message": "ping me"}))
        assert seen["message"] == "ping me"

    @pytest.mark.asyncio
    async def test_upstream_failure_reported_not_raised(self, wired, monkeypatch):
        class _Session:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, *a, **kw):
                raise OSError("connection refused")

        monkeypatch.setattr("aiohttp.ClientSession", _Session)
        resp = await H.api_webhook_test(_req("POST", "/api/webhooks/test", {}))
        data = await _payload(resp)
        assert data["ok"] is False
        assert "loopback request failed" in data["error"]
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_409_when_probe_cannot_be_minted(self, wired):
        for i in range(webhooks.MAX_TOKENS):
            webhooks.token_store().create(f"Bot {i}")
        resp = await H.api_webhook_test(_req("POST", "/api/webhooks/test", {}))
        assert resp.status == 409
        assert (await _payload(resp))["ok"] is False


class TestSourceRouting:
    @pytest.mark.asyncio
    async def test_paused_source_is_rejected_before_body_read(self, wired):
        raw, _secret, entry = webhooks.token_store().create(
            "Review Bot", False, agent="code-reviewer"
        )
        webhooks.token_store().update(entry["id"], enabled=False)
        req = _req(
            "POST",
            "/api/hooks/agent",
            {"message": "must not be read"},
            headers={"Authorization": f"Bearer {raw}"},
        )

        resp = await H.api_hooks_agent(req)

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "source_disabled"
        req.read.assert_not_awaited()
        assert webhooks.run_store().list_runs()[0]["outcome"] == webhooks.OUTCOME_DISABLED

    @pytest.mark.asyncio
    async def test_concurrent_source_revocation_is_audited_before_body_read(
        self, wired, monkeypatch
    ):
        audit = MagicMock()
        monkeypatch.setattr(H, "_sel", lambda: audit)
        store = webhooks.token_store()
        raw, _secret, entry = store.create("Review Bot", False, agent="code-reviewer")
        monkeypatch.setattr(webhooks, "token_store", lambda: store)
        monkeypatch.setattr(store, "entry_for", lambda _token_id: None)
        req = _req(
            "POST",
            "/api/hooks/agent",
            {"message": "must not be read"},
            headers={"Authorization": f"Bearer {raw}"},
        )

        resp = await H.api_hooks_agent(req)

        assert resp.status == 401
        assert (await _payload(resp))["code"] == "unauthorized"
        req.read.assert_not_awaited()
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["resources"] == f"token:{entry['id']}"
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert audit.log_api_access.call_args.kwargs["error"] == (
            "webhook source revoked during admission"
        )

    @pytest.mark.asyncio
    async def test_conflicting_request_agent_is_rejected(self, wired, monkeypatch):
        audit = MagicMock()
        monkeypatch.setattr(H, "_sel", lambda: audit)
        raw, _secret, _entry = webhooks.token_store().create(
            "Review Bot", False, agent="code-reviewer"
        )
        resp = await H.api_hooks_agent(
            _req(
                "POST",
                "/api/hooks/agent",
                {"message": "go", "agent": "oncall"},
                headers={"Authorization": f"Bearer {raw}"},
            )
        )
        body = await _payload(resp)
        assert resp.status == 409
        assert body["code"] == "agent_conflict"
        assert body["destination_agent"] == "code-reviewer"
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert audit.log_api_access.call_args.kwargs["error"] == (
            "request agent conflicts with source destination"
        )

    @pytest.mark.asyncio
    async def test_mapped_agent_is_dispatched_when_body_omits_agent(self, wired):
        raw, _secret, _entry = webhooks.token_store().create(
            "Review Bot", False, agent="code-reviewer"
        )
        session_key = "hook:routed"
        run = AsyncMock()
        try:
            with patch.object(H, "_run_hook_agent", new=run):
                resp = await H.api_hooks_agent(
                    _req(
                        "POST",
                        "/api/hooks/agent",
                        {"message": "go", "sessionKey": session_key},
                        headers={"Authorization": f"Bearer {raw}"},
                    )
                )
                await asyncio.sleep(0)
            assert resp.status == 200
            assert run.await_args.args[4] == "code-reviewer"
        finally:
            H._hook_inflight_sessions.discard(session_key)
            if H._hook_semaphore._value < H._HOOK_MAX_CONCURRENT:
                H._hook_semaphore.release()

    @pytest.mark.asyncio
    async def test_deleted_mapped_agent_fails_closed(self, wired, monkeypatch):
        audit = MagicMock()
        monkeypatch.setattr(H, "_sel", lambda: audit)
        raw, _secret, _entry = webhooks.token_store().create(
            "Review Bot", False, agent="code-reviewer"
        )
        monkeypatch.setattr(H, "_installed_agent_names", lambda: {"oncall"})
        resp = await H.api_hooks_agent(
            _req(
                "POST",
                "/api/hooks/agent",
                {"message": "go"},
                headers={"Authorization": f"Bearer {raw}"},
            )
        )
        body = await _payload(resp)
        assert resp.status == 409
        assert body["code"] == "destination_agent_unavailable"
        assert body["destination_agent"] == "code-reviewer"
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert audit.log_api_access.call_args.kwargs["error"] == (
            "source destination agent is not installed"
        )

    @pytest.mark.asyncio
    async def test_agent_discovery_failure_is_audited(self, wired, monkeypatch):
        audit = MagicMock()
        monkeypatch.setattr(H, "_sel", lambda: audit)
        raw, _secret, _entry = webhooks.token_store().create(
            "Review Bot", False, agent="code-reviewer"
        )

        def fail_discovery():
            raise OSError("agent inventory unavailable")

        monkeypatch.setattr(H, "_installed_agent_names", fail_discovery)
        resp = await H.api_hooks_agent(
            _req(
                "POST",
                "/api/hooks/agent",
                {"message": "go"},
                headers={"Authorization": f"Bearer {raw}"},
            )
        )
        assert resp.status == 503
        assert (await _payload(resp))["code"] == "agent_discovery_unavailable"
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert audit.log_api_access.call_args.kwargs["error"] == (
            "destination-agent discovery unavailable"
        )


class TestRejectionPathsAreRecorded:
    @pytest.mark.asyncio
    async def test_unknown_bearer_is_rejected_before_body_read(self, wired):
        req = _req(
            "POST",
            "/api/hooks/agent",
            {"message": "x" * 1000},
            headers={"Authorization": "Bearer kc_whk_unknown"},
        )
        resp = await H.api_hooks_agent(req)
        assert resp.status == 401
        req.read.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"message": 1}, "message must be a string"),
            ({"message": ["a"]}, "message must be a string"),
            ({"message": None}, "message must be a string"),
            ({"message": "   "}, "message required"),
            ({"message": "ok", "sessionKey": 5}, "sessionKey must be a string"),
            ({"message": "ok", "agent": 7}, "agent must be a string"),
            ({"message": "ok", "name": 123}, "name must be a string"),
            ({"message": "ok", "name": {"a": 1}}, "name must be a string"),
        ],
    )
    async def test_wrong_field_types_are_400_not_500(self, wired, payload, expected):
        """An authenticated caller's bad types must not become a server error.

        ``{"message": 1}`` can reach ``.strip()`` on an int, and a non-string
        ``sessionKey`` reaches ``.startswith()`` — both raise inside the handler
        and surface as HTTP 500 on a request that authenticated correctly.
        """
        raw, _secret, _entry = webhooks.token_store().create("CI runner", False)
        resp = await H.api_hooks_agent(
            _req(
                "POST",
                "/api/hooks/agent",
                payload,
                headers={"Authorization": f"Bearer {raw}"},
            )
        )
        assert resp.status == 400
        assert (await _payload(resp))["error"] == expected

    @pytest.mark.asyncio
    async def test_declared_oversize_body_is_rejected_before_read(self, wired):
        raw, _secret, _entry = webhooks.token_store().create("CI runner", False)
        req = _req(
            "POST",
            "/api/hooks/agent",
            {"message": "small"},
            headers={
                "Authorization": f"Bearer {raw}",
                "Content-Length": str(H._HOOK_BODY_MAX_BYTES + 1),
            },
        )
        resp = await H.api_hooks_agent(req)
        assert resp.status == 413
        assert "exceeds" in (await _payload(resp))["error"]
        req.read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chunked_body_reader_never_buffers_beyond_cap(self, wired):
        class _Content:
            def __init__(self):
                self.bytes_returned = 0

            async def read(self, size):
                chunk = b"x" * size
                self.bytes_returned += len(chunk)
                return chunk

        content = _Content()
        request = MagicMock()
        request.content_length = None
        request.can_read_body = True
        request.content = content
        request.read = AsyncMock()

        with pytest.raises(H._HookBodyTooLarge):
            await H._read_hook_body(request)
        assert content.bytes_returned == H._HOOK_BODY_MAX_BYTES + 1
        request.read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_401_recorded_without_caller_identity(self, wired):
        req = _req("POST", "/api/hooks/agent", {"message": "hi"})
        resp = await H.api_hooks_agent(req)
        assert resp.status == 401
        runs = webhooks.run_store().list_runs()
        assert len(runs) == 1
        assert runs[0]["outcome"] == "unauthorized"
        assert runs[0]["hook_id"] is None
        assert runs[0]["token_id"] is None
        assert "invalid bearer token" in runs[0]["detail"]

    @pytest.mark.asyncio
    async def test_429_recorded_with_token_and_hook(self, wired):
        raw, secret, entry = webhooks.token_store().create("Review Bot")
        req = _req(
            "POST",
            "/api/hooks/agent",
            {"message": "hi", "sessionKey": "hook:review:pr-9", "name": "Review Bot"},
            headers={"Authorization": f"Bearer {raw}"},
            sign_with=secret,
        )
        # Saturate the capacity gate.
        held = [
            asyncio.ensure_future(H._hook_semaphore.acquire())
            for _ in range(H._HOOK_MAX_CONCURRENT)
        ]
        await asyncio.gather(*held)
        try:
            resp = await H.api_hooks_agent(req)
        finally:
            for _ in range(H._HOOK_MAX_CONCURRENT):
                H._hook_semaphore.release()
        assert resp.status == 429
        runs = webhooks.run_store().list_runs()
        assert [r["outcome"] for r in runs] == ["rejected_capacity"]
        assert runs[0]["hook_id"] == "review:pr-9"
        assert runs[0]["session_key"] == "hook:review:pr-9"
        assert runs[0]["token_id"] == entry["id"]

    @pytest.mark.asyncio
    async def test_completed_run_recorded_with_token(self, wired, monkeypatch):
        state = MagicMock(memory_startup_task=None)
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.record_failure = AsyncMock()
        state.slack_client = None
        state.owner_id = None
        state.notify = MagicMock()
        (wired / "hooks.json").write_text(json.dumps({}), encoding="utf-8")

        with patch.object(H, "_run_hook_inner", new=AsyncMock(return_value="done!")):
            await H._hook_semaphore.acquire()
            await H._run_hook_agent(
                state,
                "hook:review:pr-3",
                "go",
                "Review Bot",
                None,
                True,
                60,
                token_id="wht_abc123",
            )
        runs = webhooks.run_store().list_runs()
        assert runs[0]["outcome"] == "completed"
        assert runs[0]["token_id"] == "wht_abc123"
        assert runs[0]["result_chars"] == len("done!")
        assert runs[0]["delivered"] is True
        assert runs[0]["detail"] == "Delivered to notifications"
        assert runs[0]["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_timeout_run_recorded(self, wired):
        state = MagicMock(memory_startup_task=None)
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.record_failure = AsyncMock()
        state.slack_client = None
        state.owner_id = None
        state.notify = MagicMock()

        async def _slow(*a, **kw):
            raise asyncio.TimeoutError()

        with patch.object(H, "_run_hook_inner", new=_slow):
            await H._hook_semaphore.acquire()
            await H._run_hook_agent(
                state, "hook:slow", "go", "Slow", None, False, 60, token_id="wht_abc123"
            )
        runs = webhooks.run_store().list_runs()
        assert runs[0]["outcome"] == "timeout"
        assert "timed out" in runs[0]["detail"]
        assert runs[0]["delivered"] is False

    @pytest.mark.asyncio
    async def test_error_run_recorded(self, wired):
        state = MagicMock(memory_startup_task=None)
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.record_failure = AsyncMock()
        state.slack_client = None
        state.owner_id = None
        state.notify = MagicMock()

        async def _boom(*a, **kw):
            raise RuntimeError("nope")

        with patch.object(H, "_run_hook_inner", new=_boom):
            await H._hook_semaphore.acquire()
            await H._run_hook_agent(state, "hook:bad", "go", "Bad", None, False, 60)
        runs = webhooks.run_store().list_runs()
        assert runs[0]["outcome"] == "error"
        assert runs[0]["token_id"] is None


class TestDeliveryPathTypeSafety:
    """A completed turn must never be recorded as delivered and then dropped.

    The run record is written BEFORE the delivery redaction runs, so a body field
    that only fails inside redaction is the worst shape of bug available here: the
    agent turn completes, history says `delivered`, the ephemeral session is
    already reset, and the output is unrecoverable. `name` was the last field
    without a type guard, and it is passed to `redact_exfiltration_urls`, whose
    regex raises on a non-string.
    """

    @pytest.mark.asyncio
    async def test_non_string_name_is_refused_before_the_turn_runs(self, wired):
        raw, _secret, _entry = webhooks.token_store().create("CI runner", False)
        with patch.object(H, "_run_hook_agent") as spawn:
            resp = await H.api_hooks_agent(
                _req(
                    "POST",
                    "/api/hooks/agent",
                    {"message": "go", "name": 123},
                    headers={"Authorization": f"Bearer {raw}"},
                )
            )
        assert resp.status == 400
        # No turn was started, so nothing can be recorded as delivered.
        spawn.assert_not_called()
        assert webhooks.run_store().list_runs() == []

    def test_the_redactor_really_does_raise_on_a_non_string(self):
        """Pins the premise, so this stays a real guard and not a style rule."""
        from kiro_crew.security import redact_exfiltration_urls

        with pytest.raises(TypeError):
            redact_exfiltration_urls(123)  # type: ignore[arg-type]


class TestMalformedStoreDoesNotFiveHundredTheExternalRoute:
    """A malformed store must answer 503, never leak a 500 to an outside caller.

    Token verification reads the store, so a file that decodes but holds the
    wrong shape raises WebhookStoreUnreadable inside the auth step — before the
    kill-switch guard's own try/except can see it. Unhandled, that surfaces to an
    unauthenticated internet caller as a 500, which both signals that the
    operator's store needs repair and violates this route's neutral-error
    contract.
    """

    @pytest.mark.asyncio
    async def test_wrong_shape_store_answers_neutral_503(self, wired):
        store = webhooks.token_store()
        store.path.parent.mkdir(parents=True, exist_ok=True)
        # Decodes cleanly, but a mapping sits where the token list belongs.
        store.path.write_text('{"tokens": {}}', encoding="utf-8")

        resp = await H.api_hooks_agent(
            _req(
                "POST",
                "/api/hooks/agent",
                {"message": "hello"},
                headers={"Authorization": "Bearer kc_whk_anything"},
            )
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "webhooks_unavailable"

    @pytest.mark.asyncio
    async def test_the_bytes_on_disk_are_left_alone(self, wired):
        """Refusing must not repair-by-overwrite: the operator inspects the file."""
        store = webhooks.token_store()
        store.path.parent.mkdir(parents=True, exist_ok=True)
        payload = '{"tokens": {}}'
        store.path.write_text(payload, encoding="utf-8")

        await H.api_hooks_agent(
            _req(
                "POST",
                "/api/hooks/agent",
                {"message": "hello"},
                headers={"Authorization": "Bearer kc_whk_anything"},
            )
        )

        assert store.path.read_text(encoding="utf-8") == payload


class TestTokenMintIsOwnerOnly:
    """``POST /api/webhooks/tokens`` mints a credential for the agent-run route.

    ``_verify_hook_token`` accepts the bearer this route returns on
    ``POST /api/hooks/agent``, which runs a real agent turn with full tool
    access, so the mint is owner-gated exactly like the closest guarded sibling
    (``dashboard/agent_admin/crew_records.py::api_kirocrew_agents_create`` ->
    ``handlers/_shared.require_owner_dashboard_request``). These tests hold both
    directions: the callers that legitimately mint today keep minting, and the
    non-owner dashboard session that ordinary token auth admits does not.

    The non-owner is a real principal: an allow-listed messaging user running
    ``!dashboard`` authenticates with ``app == ""`` and ``sub != owner_id``.
    """

    _MINT = {"label": "Review Bot", "agent": "kirocrew", "require_signature": False}

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_session_is_refused(self, wired):
        """THE DEFECT: an authenticated non-owner must not mint a bearer."""
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                dict(self._MINT),
                user="U0NONOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 403
        assert (await _payload(resp))["code"] == "owner_only"
        assert webhooks.token_store().count() == 0, (
            "the refusal must be taken BEFORE the store write, or the credential "
            "exists whatever the response says"
        )

    @pytest.mark.asyncio
    async def test_app_token_caller_is_refused(self, wired):
        """An app token is not the owner, the same answer the sibling gives it."""
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                dict(self._MINT),
                user="someapp",
                app_token="someapp",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 403
        assert (await _payload(resp))["code"] == "owner_only"
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_configured_owner_still_mints(self, wired):
        """POSITIVE CONTROL: an exact owner match keeps the full 201 body."""
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {"label": "Review Bot", "agent": "kirocrew"},
                user="U0THEOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 201
        data = await _payload(resp)
        assert data["ok"] is True
        assert data["token"].startswith(webhooks.TOKEN_PREFIX)
        assert data["signing_secret"].startswith(webhooks.SIGNING_SECRET_PREFIX)
        assert data["entry"]["label"] == "Review Bot"
        assert data["entry"]["agent"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_standalone_local_install_with_no_owner_still_mints(self, wired):
        """POSITIVE CONTROL: the deployment an over-strict gate would break.

        No owner configured is the standalone-local shape, where the predicate
        accepts the signed local bootstrap subject. That install has no Slack
        member id to match against, so refusing it would make the Webhooks page
        unusable for every single-user gateway.
        """
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {"label": "CI runner", "agent": "kirocrew"},
                user="local-app",
                owner_id="",
            )
        )
        assert resp.status == 201
        assert (await _payload(resp))["entry"]["label"] == "CI runner"

    @pytest.mark.asyncio
    async def test_the_probe_route_still_mints_its_throwaway_credential(self, wired, monkeypatch):
        """POSITIVE CONTROL: the owner's own probe keeps working.

        ``POST /api/webhooks/test`` mints a throwaway credential and drives a real
        agent turn through it, so it carries the owner gate like the rest of this
        management surface. The control that matters is therefore that the OWNER
        still gets the full 200 body: a gate that refused here would take the
        Webhooks page's "Send test" button away from the one caller entitled to it.
        The non-owner direction is asserted in
        ``test_the_sibling_routes_are_gated_too``.
        """

        class _Resp:
            status = 200

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, data=None, headers=None):
                assert headers["Authorization"].startswith("Bearer ")
                return _Resp()

        monkeypatch.setattr("aiohttp.ClientSession", _Session)
        resp = await H.api_webhook_test(
            _req(
                "POST",
                "/api/webhooks/test",
                {"message": "ping"},
                user="U0THEOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 200
        assert (await _payload(resp))["ok"] is True

    @pytest.mark.asyncio
    async def test_the_sibling_routes_are_gated_too(self, wired):
        """The whole credential lifecycle and the kill switch share this gate.

        Editing, revoking and the inbound kill switch are the same authorization
        decision as minting: the caller controls credentials it does not own, or
        one operator-owned boolean that silences every inbound integration. Each
        must refuse the same non-owner with the same 403 body, and the assertion
        reads the body rather than only the status so a refusal for some OTHER
        reason -- a 403 from a store error, say -- cannot pass as the gate.
        """
        non_owner = {"user": "U0NONOWNER", "owner_id": "U0THEOWNER"}
        refused = {"error": "owner authorization required", "code": "owner_only"}

        patched = await H.api_webhook_token_update(
            _req(
                "PATCH",
                "/api/webhooks/tokens/nope",
                {"enabled": False},
                match_info={"token_id": "nope"},
                **non_owner,
            )
        )
        assert patched.status == 403
        assert await _payload(patched) == refused

        deleted = await H.api_webhook_token_delete(
            _req(
                "DELETE",
                "/api/webhooks/tokens/nope",
                match_info={"token_id": "nope"},
                **non_owner,
            )
        )
        assert deleted.status == 403
        assert await _payload(deleted) == refused

        switched = await H.api_webhooks_switch(
            _req("POST", "/api/webhooks/switch", {"enabled": False}, **non_owner)
        )
        assert switched.status == 403
        assert await _payload(switched) == refused

        probed = await H.api_webhook_test(
            _req("POST", "/api/webhooks/test", {"message": "ping"}, **non_owner)
        )
        assert probed.status == 403
        assert await _payload(probed) == refused
