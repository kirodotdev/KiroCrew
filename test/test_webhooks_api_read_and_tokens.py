"""The Webhooks page's read and source-token routes, and the body gates in front of them.

``GET /api/webhooks`` (limits, redacted and fresh context summaries, runs newest
first), source-token create / patch / delete, the JSON shape, media-type and size
gates on the dashboard webhook routes, context-id redaction, and a context delete
that cannot destroy the shared script-hooks key. The store-failure guards, the
external agent-run route, its run history and the owner gate are in
``test_webhooks_api.py``; the shared fixture and request builders are in
``webhooks_api_helpers.py``.
"""

from __future__ import annotations

import contextlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from webhooks_api_helpers import wired_fixture  # noqa: F401
from webhooks_api_helpers import _bearer, _payload, _req

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H


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
        # flake-ok: pre-existing real-clock anchor for the registration offsets the handler ages on its own clock, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing 55-120 s real-clock age window around the 60 s registration offset, moved verbatim; tracked for a de-flake pass
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
    async def test_create_returns_secret_once_with_routing(self, wired):
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {"label": "Review Bot", "agent": "code-reviewer"},
            )
        )
        assert resp.status == 201
        data = await _payload(resp)
        assert data["ok"] is True
        raw = data["token"]
        assert raw.startswith(webhooks.TOKEN_PREFIX)
        assert "token_hash" not in data["entry"]
        assert data["entry"]["agent"] == "code-reviewer"
        assert data["entry"]["enabled"] is True

        listed = await _payload(await H.api_webhooks(_req("GET", "/api/webhooks")))
        assert listed["enabled"] is True
        assert listed["tokens"][0]["agent"] == "code-reviewer"
        assert raw not in json.dumps(listed)

    @pytest.mark.asyncio
    async def test_create_requires_an_installed_destination(self, wired):
        missing = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", {"label": "Review Bot"})
        )
        assert missing.status == 400
        assert (await _payload(missing))["code"] == "agent_required"

        unknown = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {"label": "Review Bot", "agent": "deleted-agent"},
            )
        )
        assert unknown.status == 400
        assert (await _payload(unknown))["code"] == "destination_agent_unavailable"

    @pytest.mark.asyncio
    async def test_pin_commits_under_the_agents_spec_lock_and_reverifies(self, wired, monkeypatch):
        """The template delete unlinks a spec while holding ``agents_spec_lock``
        and counts webhook pins first; a mint or re-pin must therefore commit
        under that lock and re-check the agent THERE, or a token can validate
        against a file the delete is about to remove. Here the agent vanishes
        between the loop-side pre-check and the locked commit: nothing is
        minted, and a re-pin leaves the stored pin alone."""
        names = [{"kirocrew", "code-reviewer", "oncall"}]
        monkeypatch.setattr(H, "_installed_agent_names", lambda: names[-1])
        held: list[bool] = []
        vanish: list[str] = ["code-reviewer"]  # what a concurrent delete removes, once
        real_lock = H.agents_spec_lock

        @contextlib.contextmanager
        def _spy_lock(agents_dir):
            with real_lock(agents_dir):
                held.append(True)
                if vanish:  # the delete landed while we waited for the lock
                    names.append(names[-1] - {vanish.pop()})
                yield

        monkeypatch.setattr(H, "agents_spec_lock", _spy_lock)
        gone = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", {"label": "Review Bot", "agent": "code-reviewer"})
        )
        assert gone.status == 400
        assert (await _payload(gone))["code"] == "destination_agent_unavailable"
        assert held == [True]
        assert webhooks.token_store().count() == 0

        # A re-pin walks the same path; a change that leaves the pin alone does not.
        minted = await _payload(
            await H.api_webhook_token_create(
                _req("POST", "/api/webhooks/tokens", {"label": "Pager", "agent": "oncall"})
            )
        )
        token_id = minted["entry"]["id"]
        held.clear()
        relabel = await H.api_webhook_token_update(
            _req(
                "PATCH",
                f"/api/webhooks/tokens/{token_id}",
                {"label": "Pager 2"},
                match_info={"token_id": token_id},
            )
        )
        assert relabel.status == 200 and held == []
        vanish.append("oncall")
        repin = await H.api_webhook_token_update(
            _req(
                "PATCH",
                f"/api/webhooks/tokens/{token_id}",
                {"agent": "oncall"},
                match_info={"token_id": token_id},
            )
        )
        assert repin.status == 400 and held == [True]
        assert (await _payload(repin))["code"] == "destination_agent_unavailable"
        assert webhooks.token_store().entry_for(token_id)["agent"] == "oncall"

    @pytest.mark.asyncio
    async def test_create_rejects_bad_label(self, wired):
        resp = await H.api_webhook_token_create(
            _req("POST", "/api/webhooks/tokens", {"label": "  ", "agent": "kirocrew"})
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
                _req(
                    "POST",
                    "/api/webhooks/tokens",
                    {"label": f"Bot {i}", "agent": "kirocrew"},
                )
            )
            assert r.status == 201
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {"label": "Too many", "agent": "kirocrew"},
            )
        )
        assert resp.status == 400
        assert "token limit reached" in (await _payload(resp))["error"]

    @pytest.mark.asyncio
    async def test_patch_updates_only_source_settings(self, wired):
        created = await _payload(
            await H.api_webhook_token_create(
                _req(
                    "POST",
                    "/api/webhooks/tokens",
                    {"label": "Review Bot", "agent": "code-reviewer"},
                )
            )
        )
        token_id = created["entry"]["id"]
        resp = await H.api_webhook_token_update(
            _req(
                "PATCH",
                f"/api/webhooks/tokens/{token_id}",
                {"label": "CI callback", "agent": "oncall", "enabled": False},
                match_info={"token_id": token_id},
            )
        )
        assert resp.status == 200
        entry = (await _payload(resp))["entry"]
        assert entry["label"] == "CI callback"
        assert entry["agent"] == "oncall"
        assert entry["enabled"] is False
        assert "token_hash" not in entry
        assert "signing_secret" not in entry

    @pytest.mark.asyncio
    async def test_patch_reports_an_unavailable_store(self, wired):
        store = webhooks.token_store()
        _raw, _secret, entry = store.create("Review Bot", agent="kirocrew")
        store.path.write_text("{truncated", encoding="utf-8")

        resp = await H.api_webhook_token_update(
            _req(
                "PATCH",
                f"/api/webhooks/tokens/{entry['id']}",
                {"label": "CI callback"},
                match_info={"token_id": entry["id"]},
            )
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"
        assert store.path.read_text(encoding="utf-8") == "{truncated"

    @pytest.mark.asyncio
    async def test_patch_rejects_unknown_fields_and_legacy(self, wired, monkeypatch):
        audit = MagicMock()
        monkeypatch.setattr(H, "_sel", lambda: audit)
        invalid = await H.api_webhook_token_update(
            _req(
                "PATCH",
                "/api/webhooks/tokens/nope",
                {"require_signature": False},
                match_info={"token_id": "nope"},
            )
        )
        assert invalid.status == 400
        assert (await _payload(invalid))["code"] == "invalid_source_patch"

        legacy = await H.api_webhook_token_update(
            _req(
                "PATCH",
                "/api/webhooks/tokens/legacy",
                {"enabled": False},
                match_info={"token_id": "legacy"},
            )
        )
        assert legacy.status == 400
        assert (await _payload(legacy))["code"] == "legacy_credential_in_config"
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert audit.log_api_access.call_args.kwargs["error"] == (
            "legacy credential is config-managed"
        )

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
                _req(
                    "POST",
                    "/api/webhooks/tokens",
                    {"label": "A", "agent": "kirocrew"},
                )
            )
        )
        b = await _payload(
            await H.api_webhook_token_create(
                _req(
                    "POST",
                    "/api/webhooks/tokens",
                    {"label": "B", "agent": "kirocrew"},
                )
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


class TestNonObjectJsonBodies:
    """A valid-but-non-object body must be 400, never 500.

    ``await request.json()`` returns a list for ``[]``, and every handler then
    called ``.get()`` on it — so a client sending the wrong JSON shape got a
    server error instead of a validation error.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], [1, 2], "text", 7, True])
    async def test_switch_rejects_non_object(self, wired, payload):
        resp = await H.api_webhooks_switch(_req("POST", "/api/webhooks/switch", payload))
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
    async def test_absent_body_still_uses_defaults(self, wired, monkeypatch):
        """default_empty must keep the no-body case working, unlike a bad shape.

        The probe's loopback client is stubbed, as in ``TestWebhookTest``, so
        the test never sends a real request to whatever listens on 127.0.0.1.
        """
        posted: list[str] = []

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
                posted.append(url)
                return _Resp()

        monkeypatch.setattr("aiohttp.ClientSession", _Session)
        req = _req("POST", "/api/webhooks/test", None)
        req.json = AsyncMock(side_effect=ValueError("no body"))
        with patch.object(H, "_json_object", wraps=H._json_object):
            resp = await H.api_webhook_test(req)
        # Reaches the probe path rather than being rejected as malformed.
        assert resp.status != 400
        # The probe went to the stub, not the network.
        assert posted == ["http://127.0.0.1:6776/api/hooks/agent"]


class TestJsonContentTypeGate:
    """The hook/webhook dashboard routes read through ``read_bounded_json``.

    So a body that is present but not DECLARED JSON is refused 415 before it is
    read -- including on the ``default_empty`` routes, which default an absent
    or unparseable body but must not default a refused one.
    """

    _ROUTES = [
        ("api_webhooks_switch", "POST", "/api/webhooks/switch", {"enabled": False}),
        ("api_webhook_token_create", "POST", "/api/webhooks/tokens", {"label": "x"}),
        ("api_webhook_test", "POST", "/api/webhooks/test", {"message": "hi"}),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler,method,path,body", _ROUTES)
    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
    async def test_a_non_json_media_type_is_refused_415(
        self, wired, handler, method, path, body, content_type
    ):
        req = _req(method, path, body, headers={"Content-Type": content_type})
        resp = await getattr(H, handler)(req)
        assert resp.status == 415
        assert (await _payload(resp))["code"] == "unsupported_media_type"
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_token_update_is_gated_too(self, wired):
        req = _req(
            "PATCH",
            "/api/webhooks/tokens/t1",
            {"enabled": False},
            match_info={"token_id": "t1"},
            headers={"Content-Type": "text/plain"},
        )
        resp = await H.api_webhook_token_update(req)
        assert resp.status == 415

    @pytest.mark.asyncio
    async def test_a_declared_oversize_is_refused_413(self, wired):
        req = _req(
            "POST",
            "/api/webhooks/test",
            {"message": "hi"},
            headers={"Content-Length": str(H._HOOK_BODY_MAX_BYTES + 1)},
        )
        resp = await H.api_webhook_test(req)
        assert resp.status == 413
        assert (await _payload(resp))["code"] == "payload_too_large"

    @pytest.mark.asyncio
    async def test_a_json_body_is_still_accepted(self, wired):
        resp = await H.api_webhooks_switch(_req("POST", "/api/webhooks/switch", {"enabled": False}))
        assert resp.status == 200


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
