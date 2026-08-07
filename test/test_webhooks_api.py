"""Tests for the dashboard-authed webhook management endpoints.

Also covers the run-history side effects of the ``POST /api/hooks/agent``
rejection paths (401 unauthorized, 429 capacity), which are the only record
of a call that never became a run.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Point both webhook stores and hooks.json at a temp dir."""
    monkeypatch.setattr(webhooks, "config_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(H, "_HOOK_STORE_PATH", Path(tmp_path) / "hooks.json")
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    monkeypatch.setattr(H, "_legacy_hook_token", lambda: "")
    # The failed-auth throttle, the replay seen-set and the in-flight session
    # registry are all process-global, so leaking them between tests would make a
    # later 401 come back as a 429, or a fresh call come back as a 409.
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    H._reset_hook_inflight()
    yield Path(tmp_path)
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    H._reset_hook_inflight()


def _req(
    method: str,
    path: str,
    body=None,
    match_info=None,
    headers=None,
    sign_with: str | None = None,
    timestamp: int | None = None,
):
    """Build a mocked request whose raw body matches its parsed body.

    ``sign_with`` adds the timestamp + signature headers for that signing
    secret, computed over the same bytes ``request.read()`` returns — the
    handler verifies raw bytes, so the test double has to be byte-accurate.
    """
    hdrs = dict(headers or {})
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    if sign_with is not None:
        stamp = int(time.time()) if timestamp is None else timestamp
        hdrs[webhooks.TIMESTAMP_HEADER] = str(stamp)
        hdrs[webhooks.SIGNATURE_HEADER] = webhooks.sign_payload(sign_with, stamp, raw)
    req = make_mocked_request(method, path, match_info=match_info or {}, headers=hdrs)
    req.app["state"] = MagicMock()
    # make_mocked_request's app is a Mock, so .get() would hand back a Mock
    # rather than the port the real Application holds.
    req.app.get = lambda key, default=None: {"port": 6776}.get(key, default)
    req.read = AsyncMock(return_value=raw)
    if body is not None:
        req.json = AsyncMock(return_value=body)
    else:
        req.json = AsyncMock(side_effect=ValueError("no body"))
    return req


async def _payload(resp):
    return json.loads(resp.body.decode("utf-8"))


class TestWebhooksRead:
    @pytest.mark.asyncio
    async def test_disabled_when_no_tokens(self, wired):
        resp = await H.api_webhooks(_req("GET", "/api/webhooks"))
        data = await _payload(resp)
        assert resp.status == 200
        assert data["enabled"] is False
        assert data["tokens"] == []
        assert data["contexts"] == []
        assert data["runs"] == []
        assert data["url"].endswith("/api/hooks/agent")
        assert data["slots"] == {"in_use": 0, "max": H._HOOK_MAX_CONCURRENT}
        assert data["limits"] == {
            "session_key_prefix": "hook:",
            "message_max": H._HOOK_MESSAGE_MAX_LEN,
            "body_max_bytes": H._HOOK_BODY_MAX_BYTES,
            "timeout_default": H._HOOK_TIMEOUT_DEFAULT,
            "timeout_max": H._HOOK_TIMEOUT_MAX,
            "max_concurrent": H._HOOK_MAX_CONCURRENT,
            "signature_window_seconds": webhooks.SIGNATURE_WINDOW_SECONDS,
        }

    @pytest.mark.asyncio
    async def test_enabled_by_legacy_token_alone(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_legacy_hook_token", lambda: "legacy-secret")
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        assert data["enabled"] is True
        assert [t["id"] for t in data["tokens"]] == ["legacy"]

    @pytest.mark.asyncio
    async def test_context_summary_is_redacted_before_transport(self, wired):
        """A credential in an agent-written summary must not reach the client.

        ``register_hook`` stores free text the agent composed, so it can contain
        whatever the session had in view. This read surface is an egress point
        like the delivered result and the kiro-hooks command list, both of which
        already scrub, so it must scrub too.
        """
        secret = "ghp_" + "a" * 36
        (wired / "hooks.json").write_text(
            json.dumps(
                {
                    "leaky:job": {
                        "context_summary": f"Resuming deploy with token {secret} for acct",
                        "registered_at": time.time(),
                    }
                }
            ),
            encoding="utf-8",
        )
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        summary = data["contexts"][0]["context_summary"]
        assert secret not in summary
        # Redaction replaces the secret rather than dropping the whole summary,
        # so the pane still explains what the hook was doing.
        assert "Resuming deploy" in summary

    @pytest.mark.asyncio
    async def test_redaction_runs_before_the_transport_slice(self, wired):
        """Slicing first would let a secret survive by straddling the cut."""
        secret = "ghp_" + "b" * 36
        padding = "p" * (webhooks.CONTEXT_SUMMARY_TRANSPORT_MAX - 10)
        (wired / "hooks.json").write_text(
            json.dumps(
                {
                    "straddle:job": {
                        "context_summary": padding + secret,
                        "registered_at": time.time(),
                    }
                }
            ),
            encoding="utf-8",
        )
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        ctx = data["contexts"][0]
        assert secret[:20] not in ctx["context_summary"]
        # context_chars still reports the stored length, not the redacted one.
        assert ctx["context_chars"] == len(padding + secret)

    @pytest.mark.asyncio
    async def test_contexts_carry_shared_freshness(self, wired):
        now = time.time()
        (wired / "hooks.json").write_text(
            json.dumps(
                {
                    "review:pr-1": {
                        "session_key": "hook:review:pr-1",
                        "context_summary": "x" * 2500,
                        "registered_at": now - 60,
                    },
                    "old:job": {"context_summary": "y", "registered_at": now - 90000},
                    # ScriptHookStore's own shape must not be mistaken for a context.
                    "hooks": [{"id": "script-hook"}],
                }
            ),
            encoding="utf-8",
        )
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        by_id = {c["hook_id"]: c for c in data["contexts"]}
        assert set(by_id) == {"review:pr-1", "old:job"}
        fresh = by_id["review:pr-1"]
        assert fresh["freshness"] == "fresh"
        assert fresh["context_chars"] == 2500
        assert len(fresh["context_summary"]) == webhooks.CONTEXT_SUMMARY_TRANSPORT_MAX
        assert fresh["session_key"] == "hook:review:pr-1"
        assert 55 <= fresh["age_seconds"] <= 120
        assert by_id["old:job"]["freshness"] == "expired"
        # Newest registration first.
        assert data["contexts"][0]["hook_id"] == "review:pr-1"

    @pytest.mark.asyncio
    async def test_runs_included_newest_first(self, wired):
        webhooks.run_store().record(outcome="completed", hook_id="a")
        webhooks.run_store().record(outcome="error", hook_id="b")
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        assert [r["hook_id"] for r in data["runs"]] == ["b", "a"]


class TestTokenEndpoints:
    @pytest.mark.asyncio
    async def test_create_returns_secret_once(self, wired):
        resp = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", {"label": "Review Bot"})
        )
        assert resp.status == 201
        data = await _payload(resp)
        assert data["ok"] is True
        raw = data["token"]
        assert raw.startswith(webhooks.TOKEN_PREFIX)
        assert "token_hash" not in data["entry"]

        listed = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        assert listed["enabled"] is True
        assert raw not in json.dumps(listed)

    @pytest.mark.asyncio
    async def test_create_rejects_bad_label(self, wired):
        resp = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", {"label": "  "})
        )
        assert resp.status == 400
        assert "required" in (await _payload(resp))["error"]

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_json(self, wired):
        resp = await H.api_webhook_token_create(_req("POST", "/api/webhooks/tokens"))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_cap_returns_400_on_twenty_first(self, wired):
        for i in range(webhooks.MAX_TOKENS):
            r = await H.api_webhook_token_create(
                _req("POST", "/api/webhooks/tokens", {"label": f"Bot {i}"})
            )
            assert r.status == 201
        resp = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", {"label": "Too many"})
        )
        assert resp.status == 400
        assert "token limit reached" in (await _payload(resp))["error"]

    @pytest.mark.asyncio
    async def test_delete_unknown_is_404(self, wired):
        resp = await H.api_webhook_token_delete(
            _req("DELETE", "/api/webhooks/tokens/nope", match_info={"token_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_legacy_refused_with_config_pointer(self, wired):
        resp = await H.api_webhook_token_delete(
            _req("DELETE", "/api/webhooks/tokens/legacy", match_info={"token_id": "legacy"})
        )
        assert resp.status == 400
        assert "hooks.webhook_token" in (await _payload(resp))["error"]

    @pytest.mark.asyncio
    async def test_revoke_one_leaves_others_authenticating(self, wired):
        a = await _payload(
            await H.api_webhook_token_create(
                _req("POST", "/api/webhooks/tokens", {"label": "A"})
            )
        )
        b = await _payload(
            await H.api_webhook_token_create(
                _req("POST", "/api/webhooks/tokens", {"label": "B"})
            )
        )
        resp = await H.api_webhook_token_delete(
            _req(
                "DELETE",
                f"/api/webhooks/tokens/{a['entry']['id']}",
                match_info={"token_id": a["entry"]["id"]},
            )
        )
        assert resp.status == 200
        assert H._verify_hook_token(_bearer(a["token"])) is None
        assert H._verify_hook_token(_bearer(b["token"])) == b["entry"]["id"]


def _bearer(raw: str):
    return make_mocked_request(
        "POST", "/api/hooks/agent", headers={"Authorization": f"Bearer {raw}"}
    )


class TestNonObjectJsonBodies:
    """A valid-but-non-object body must be 400, never 500.

    ``await request.json()`` returns a list for ``[]``, and every handler then
    called ``.get()`` on it — so a client sending the wrong JSON shape got a
    server error instead of a validation error.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], [1, 2], "text", 7, True])
    async def test_switch_rejects_non_object(self, wired, payload):
        resp = await H.api_webhooks_switch(
            _req("POST", "/api/webhooks/switch", payload)
        )
        assert resp.status == 400
        assert (await _payload(resp))["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_token_create_rejects_non_object(self, wired):
        resp = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", ["Review Bot"])
        )
        assert resp.status == 400
        # And nothing was minted.
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_webhook_test_rejects_non_object(self, wired):
        resp = await H.api_webhook_test(_req("POST", "/api/webhooks/test", []))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_absent_body_still_uses_defaults(self, wired):
        """default_empty must keep the no-body case working, unlike a bad shape."""
        req = _req("POST", "/api/webhooks/test", None)
        req.json = AsyncMock(side_effect=ValueError("no body"))
        with patch.object(H, "_json_object", wraps=H._json_object):
            resp = await H.api_webhook_test(req)
        # Reaches the probe path rather than being rejected as malformed.
        assert resp.status != 400


class TestIdentifierRedaction:
    """Hook ids are agent-supplied free text, so they are egress too."""

    @pytest.mark.asyncio
    async def test_hook_id_and_session_key_are_redacted(self, wired):
        secret = "ghp_" + "c" * 36
        (wired / "hooks.json").write_text(
            json.dumps(
                {
                    f"job-{secret}": {
                        "session_key": f"hook:job-{secret}",
                        "context_summary": "benign",
                        "registered_at": time.time(),
                    }
                }
            ),
            encoding="utf-8",
        )
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        ctx = data["contexts"][0]
        assert secret not in ctx["hook_id"]
        assert secret not in ctx["session_key"]
        assert secret not in json.dumps(data)

    @pytest.mark.asyncio
    async def test_a_redacted_context_can_still_be_deleted(self, wired):
        """Redacting the id must not strand the context: delete still resolves it."""
        secret = "ghp_" + "d" * 36
        raw_id = f"job-{secret}"
        (wired / "hooks.json").write_text(
            json.dumps({raw_id: {"context_summary": "x", "registered_at": time.time()}}),
            encoding="utf-8",
        )
        data = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        shown = data["contexts"][0]["hook_id"]
        assert shown != raw_id

        resp = await H.api_webhook_context_delete(
            _req(
                "DELETE",
                f"/api/webhooks/contexts/{shown}",
                match_info={"hook_id": shown},
            )
        )
        assert resp.status == 200
        assert json.loads((wired / "hooks.json").read_text(encoding="utf-8")) == {}


class TestContextDeleteCannotDestroyScriptHooks:
    """hooks.json is shared, so an id-addressed delete must check the shape.

    ``ScriptHookStore`` owns the top-level ``hooks`` key — a LIST of script
    hooks. Deleting by raw key name meant an authenticated
    ``DELETE /api/webhooks/contexts/hooks`` removed every script hook the user
    had, with a 200 as if it were a normal context removal.
    """

    @pytest.mark.asyncio
    async def test_deleting_the_shared_hooks_key_is_refused(self, wired):
        (wired / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [{"id": "sh_1", "name": "fmt"}, {"id": "sh_2", "name": "lint"}],
                    "review:pr-1": {
                        "context_summary": "ctx",
                        "registered_at": time.time(),
                    },
                }
            ),
            encoding="utf-8",
        )
        resp = await H.api_webhook_context_delete(
            _req(
                "DELETE",
                "/api/webhooks/contexts/hooks",
                match_info={"hook_id": "hooks"},
            )
        )
        assert resp.status == 404
        after = json.loads((wired / "hooks.json").read_text(encoding="utf-8"))
        assert len(after["hooks"]) == 2
        assert "review:pr-1" in after

    @pytest.mark.asyncio
    async def test_a_real_context_still_deletes(self, wired):
        (wired / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [{"id": "sh_1", "name": "fmt"}],
                    "review:pr-1": {
                        "context_summary": "ctx",
                        "registered_at": time.time(),
                    },
                }
            ),
            encoding="utf-8",
        )
        resp = await H.api_webhook_context_delete(
            _req(
                "DELETE",
                "/api/webhooks/contexts/review:pr-1",
                match_info={"hook_id": "review:pr-1"},
            )
        )
        assert resp.status == 200
        after = json.loads((wired / "hooks.json").read_text(encoding="utf-8"))
        assert "review:pr-1" not in after
        assert len(after["hooks"]) == 1


class TestEveryStoreTouchingHandlerIsGuarded:
    """A store failure must never reach the client as a 500.

    Reads refuse rather than reporting an empty file, and the shared hooks.json
    write refuses rather than erasing the webhook contexts beside it. Those
    refusals were being guarded one handler per review round, so this pins the
    CLASS instead of the instances: every handler that touches a store is wrapped.
    """

    @pytest.mark.asyncio
    async def test_revocation_reports_an_unavailable_store(self, wired):
        """The specific gap that was reported: revoking against a corrupt store."""
        store = webhooks.token_store()
        _raw, _secret, entry = store.create("Review Bot")
        store.path.write_text("{truncated", encoding="utf-8")

        resp = await H.api_webhook_token_delete(
            _req("DELETE", f"/api/webhooks/tokens/{entry['id']}",
                 match_info={"token_id": entry["id"]})
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"
        # The refusal must not have altered the file.
        assert store.path.read_text(encoding="utf-8") == "{truncated"

    def test_all_store_touching_handlers_carry_the_guard(self):
        """Structural: a new store-touching handler cannot ship unguarded.

        Checked by reading the source rather than by exercising each route, so the
        assertion covers handlers this suite has no fixture for. Without it the
        next handler added would repeat the same review round.
        """
        import inspect
        import re

        source = inspect.getsource(H)
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
            body = source[match.end():body_end]
            name = match.group(2)
            if store_call.search(body) and not match.group(1) and name not in exempt:
                unguarded.append(name)

        assert not unguarded, (
            "these handlers touch a store but lack @_store_failure_guard: "
            + ", ".join(unguarded)
        )


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
            "POST", "/api/hooks/agent",
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
    async def test_the_key_is_released_when_the_turn_finishes(self, wired, monkeypatch):
        """A completed turn must not leave its key claimed forever."""
        monkeypatch.setattr(
            H, "_run_hook_inner", AsyncMock(return_value="done")
        )
        state = MagicMock()
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
            state, session_key, "hi", "Bot", None, True, 30,
        )

        assert session_key not in H._hook_inflight_sessions, "key stayed claimed"
        assert H._hook_semaphore._value == before, "capacity semaphore leaked a permit"

    @pytest.mark.asyncio
    async def test_a_crashed_turn_still_releases_the_key(self, wired, monkeypatch):
        """A failing turn must not wedge the session key permanently."""
        monkeypatch.setattr(
            H, "_run_hook_inner", AsyncMock(side_effect=RuntimeError("boom"))
        )
        state = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.reset = AsyncMock()
        state.owner_id = None
        state.slack_client = None
        state.notify = MagicMock()

        session_key = f"{H._HOOK_SESSION_PREFIX}crash"
        H._hook_inflight_sessions.add(session_key)
        await H._hook_semaphore.acquire()
        await H._run_hook_agent(
            state, session_key, "hi", "Bot", None, True, 30,
        )

        assert session_key not in H._hook_inflight_sessions, "crashed turn wedged the key"


class TestDeliveryIsRecordedAfterItHappens:
    """Run history must name only destinations that actually received the result.

    `delivered` used to be derived from intent (`deliver and result_text`) and the
    record was written before delivery was attempted. Slack failures are caught
    and logged, so a run whose DM failed was still stored as delivered to Slack —
    and the run history is the only place an operator can check.
    """

    @staticmethod
    def _state():
        state = MagicMock()
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
            state, f"{H._HOOK_SESSION_PREFIX}t", "hi", "Bot", None, True, 30,
        )
        assert H._hook_semaphore._value == before, "capacity semaphore leaked a permit"

    @pytest.mark.asyncio
    async def test_a_failed_slack_dm_is_not_recorded_as_delivered(self, wired, monkeypatch):
        monkeypatch.setattr(
            H, "_run_hook_inner", AsyncMock(return_value="the agent answer")
        )
        state = self._state()
        state.owner_id = "U123"
        state.notify = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="D1")
        state.slack_client.post_message = AsyncMock(
            side_effect=RuntimeError("slack down")
        )

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
    async def test_every_destination_failing_is_not_recorded_as_delivered(
        self, wired, monkeypatch
    ):
        monkeypatch.setattr(
            H, "_run_hook_inner", AsyncMock(return_value="the agent answer")
        )
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
        monkeypatch.setattr(
            H, "_run_hook_inner", AsyncMock(return_value="the agent answer")
        )
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

        resp = await H.api_webhooks_switch(
            _req("POST", "/api/webhooks/switch", {"enabled": False})
        )

        assert resp.status == 503
        body = await _payload(resp)
        assert body["code"] == "store_unavailable"
        # The refusal must not have changed anything on disk.
        assert store.path.read_text(encoding="utf-8") == "{truncated"

    @pytest.mark.asyncio
    async def test_a_script_hook_create_reports_an_unavailable_store(
        self, wired, monkeypatch
    ):
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
            _req("POST", "/api/hooks",
                 {"name": "fmt", "event": "Stop", "command": "true"})
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

    Store reads refuse rather than reporting an empty file (which previously let
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
        resp = await H.api_webhook_test(_req("POST", "/api/webhooks/test", {}))
        data = await _payload(resp)
        assert data["ok"] is True
        assert data["session_key"].startswith("hook:test:")
        assert seen["url"] == "http://127.0.0.1:6776/api/hooks/agent"
        assert seen["json"]["deliver"] is False
        assert seen["json"]["message"]
        assert seen["verified"] is not None
        # The probe must exercise the signing path, not bypass it: its token
        # requires a signature and the headers it sent must verify against the
        # exact bytes it posted.
        assert seen["entry"]["require_signature"] is True
        assert webhooks.verify_signature(
            secret=seen["entry"]["signing_secret"],
            timestamp=seen["headers"][webhooks.TIMESTAMP_HEADER],
            signature=seen["headers"][webhooks.SIGNATURE_HEADER],
            body=seen["data"],
        ) is None
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

        ``{"message": 1}`` used to reach ``.strip()`` on an int, and a non-string
        ``sessionKey`` reached ``.startswith()`` — both raise inside the handler
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
        held = [asyncio.ensure_future(H._hook_semaphore.acquire()) for _ in range(
            H._HOOK_MAX_CONCURRENT
        )]
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
        state = MagicMock()
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
                state, "hook:review:pr-3", "go", "Review Bot", None, True, 60,
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
        state = MagicMock()
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
        state = MagicMock()
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
