"""Tests for the MCP OAuth banner pipeline:

* `_redact_meta_for_role` — preserves http(s) oauth_url for `mcp_oauth`, drops
  unsafe schemes, keeps the default redaction behavior for every other role.
* `_emit_mcp_oauth_request` — appends a banner only when the URL passes scheme
  validation; rejects javascript:/data:/ftp:/etc.
* `_mark_mcp_oauth_completed` — flips the most recent open banner to its
  terminal state (success or failure), removes stale failure metadata on
  recovery, no-ops when no banner exists.
* `_ChatSlot.update_message` — patches a message in place and marks the slot
  dirty.
* `_drain_session_init_oauth_requests` / `_connections_managed_mcp_names` —
  every buffered session-init request is emitted, and the ones a rendered
  Connections card owns carry a `card_owned` annotation for the render layer.
  The lookup behind that annotation reads files, so it runs off the event loop.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from oauth_url_corpus import LEGIT_OAUTH_URLS

from kiro_crew import mcp_discovery
from kiro_crew.connections import get_all_registry_providers, get_visible_providers
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import (
    _connections_managed_mcp_names,
    _drain_session_init_oauth_requests,
    _emit_mcp_oauth_request,
    _is_safe_oauth_url,
    _mark_mcp_oauth_completed,
    _supersede_open_mcp_oauth_banners,
)
from kiro_crew.dashboard.chat_utils import (
    _broadcast_expired_oauth_banners,
    _live_child_instance,
    _prepare_messages,
    _redact_meta_for_role,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import oauth_url_contains_credential

# ── _is_safe_oauth_url ──


class TestIsSafeOAuthUrl:
    def test_https_allowed(self):
        assert _is_safe_oauth_url("https://mcp.linear.app/authorize?x=1")

    def test_http_allowed(self):
        assert _is_safe_oauth_url("http://localhost:5476/callback")

    def test_javascript_rejected(self):
        assert not _is_safe_oauth_url("javascript:alert(1)")

    def test_data_rejected(self):
        assert not _is_safe_oauth_url("data:text/html,<script>1</script>")

    def test_empty_rejected(self):
        assert not _is_safe_oauth_url("")

    def test_case_insensitive(self):
        assert _is_safe_oauth_url("HTTPS://EXAMPLE.COM/x")


# ── _redact_meta_for_role ──


class TestRedactMetaForRole:
    def test_mcp_oauth_preserves_https_url(self):
        url = "https://mcp.linear.app/authorize?client_id=abc"
        out = _redact_meta_for_role("mcp_oauth", {"server_name": "linear", "oauth_url": url})
        assert out["oauth_url"] == url
        assert out["server_name"] == "linear"

    def test_mcp_oauth_drops_unsafe_url(self):
        out = _redact_meta_for_role(
            "mcp_oauth",
            {"server_name": "evil", "oauth_url": "javascript:alert(1)"},
        )
        # Unsafe scheme is replaced with empty string, not preserved as-is.
        assert out["oauth_url"] == ""

    def test_mcp_oauth_url_carrying_credential_is_dropped_on_rehydrate(self):
        """A tampered history line whose oauth_url embeds an AKIA-style
        credential gets emptied out on rehydrate, even if the scheme is https.
        Mirrors the live-emission gate in _emit_mcp_oauth_request."""
        out = _redact_meta_for_role(
            "mcp_oauth",
            {
                "server_name": "linear",
                "oauth_url": "https://evil.com/auth?key=AKIAIOSFODNN7EXAMPLE",
            },
        )
        assert out["oauth_url"] == ""

    def test_mcp_oauth_redacts_other_fields(self):
        # error string with a credential should still be redacted via _redact_value
        url = "https://mcp.example.com/authorize"
        out = _redact_meta_for_role(
            "mcp_oauth",
            {
                "server_name": "ex",
                "oauth_url": url,
                "error": "AKIAIOSFODNN7EXAMPLE leaked",
            },
        )
        assert out["oauth_url"] == url
        # _redact_value is invoked for non-preserved fields, so AKIA pattern is scrubbed.
        assert "AKIAIOSFODNN7EXAMPLE" not in out["error"]

    def test_other_role_uses_default_redaction(self):
        # An assistant-meta URL pointing at an exfil-eligible domain should
        # NOT survive through the default _redact_meta path.
        out = _redact_meta_for_role(
            "assistant",
            {"oauth_url": "https://mcp.linear.app/authorize"},
        )
        # Default redaction does not have the oauth_url carve-out — the URL
        # may be redacted (depending on allowlist) but must not be treated as
        # a special-case preserved field.
        assert "oauth_url" in out  # key still present, value may differ

    def test_non_string_oauth_url_redacted_as_value(self):
        # If a tampered history line stored a non-string for oauth_url, fall
        # through to _redact_value so the carve-out can't be exploited.
        out = _redact_meta_for_role("mcp_oauth", {"oauth_url": 123})
        assert out["oauth_url"] == 123  # _redact_value passes through non-str/non-container


# ── _emit_mcp_oauth_request ──


class TestEmitMcpOAuthRequest:
    def test_appends_banner_for_https(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "linear", "https://mcp.linear.app/authorize")
        assert len(slot.messages) == 1
        m = slot.messages[0]
        assert m["role"] == "mcp_oauth"
        assert m["meta"]["server_name"] == "linear"
        assert m["meta"]["oauth_url"] == "https://mcp.linear.app/authorize"

    def test_rejects_javascript_url(self):
        """Unsafe scheme → surface a failed banner so the user knows the
        server-supplied URL was rejected.  The unsafe URL itself is never
        persisted to meta."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "evil", "javascript:alert(1)")
        assert len(slot.messages) == 1
        m = slot.messages[0]
        assert m["role"] == "mcp_oauth"
        assert m["meta"]["failed"] is True
        assert m["meta"]["rejected_url"] is True
        assert "oauth_url" not in m["meta"]
        assert "javascript" not in m["content"]

    def test_rejects_empty_url(self):
        """Empty URL is treated as unsafe; banner explains rejection."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "x", "")
        assert len(slot.messages) == 1
        m = slot.messages[0]
        assert m["meta"]["failed"] is True
        assert m["meta"]["rejected_url"] is True

    def test_rejects_url_carrying_credential(self):
        """A 'consent URL' embedding a credential pattern is bogus — not
        legitimate OAuth.  Surface a failed banner instead of silently
        dropping so the user knows the server-supplied URL was rejected.
        The unsafe URL itself is never persisted to meta."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(
            state,
            slot,
            "linear",
            "https://evil.com/auth?key=AKIAIOSFODNN7EXAMPLE",
        )
        assert len(slot.messages) == 1
        m = slot.messages[0]
        assert m["role"] == "mcp_oauth"
        assert m["meta"]["failed"] is True
        assert m["meta"]["rejected_url"] is True
        assert "oauth_url" not in m["meta"]
        assert "AKIAIOSFODNN7EXAMPLE" not in m["content"]
        assert "credential" in m["meta"].get("error", "")

    def test_rejection_banner_names_the_operator_remedy(self):
        """A rejected URL must name ``oauth_endpoints.json``.

        The endpoint allowlist means a legitimate consent URL at an unlisted
        self-hosted IdP lands in this same branch, and its remedy — the
        operator keystone extension — is agent-fenced with no dashboard
        writer and is documented only in an internal spec. If the banner does
        not name it, the failure is indistinguishable from unfixable: two
        users independently root-caused this from source (#3310) rather than
        finding the one-line config fix.
        """
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(
            state,
            slot,
            "self-hosted",
            "https://evil.com/auth?key=AKIAIOSFODNN7EXAMPLE",
        )
        m = slot.messages[0]
        assert "oauth_endpoints.json" in m["content"]
        assert m["meta"]["remedy"] == "oauth_endpoints.json"
        # The remedy hint must not soften the rejection itself.
        assert m["meta"]["failed"] is True
        assert m["meta"]["rejected_url"] is True
        assert "oauth_url" not in m["meta"]
        assert "AKIAIOSFODNN7EXAMPLE" not in m["content"]

    def test_rejection_banner_names_the_rejected_endpoint(self):
        """A rejected URL must name the SANITIZED host+path that tripped the
        scanner (#7578): without it the user cannot know which endpoint to
        write into ``oauth_endpoints.json``, so the failure reads as
        unfixable. Query values carry state/PKCE material (and here, the
        smuggled credential) and must NEVER be echoed — not in the content,
        not anywhere in the meta dict.
        """
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(
            state,
            slot,
            "self-hosted",
            "https://Unlisted-IdP.example/realms/dev/authorize"
            "?state=topsecretstatevalue&key=AKIAIOSFODNN7EXAMPLE",
        )
        m = slot.messages[0]
        # Sanitized endpoint in the banner text.
        assert "unlisted-idp.example/realms/dev/authorize" in m["content"]
        # The dashboard's failed banner renders meta["error"], NOT content —
        # the endpoint must ride there to actually reach the user's screen.
        assert "unlisted-idp.example/realms/dev/authorize" in m["meta"]["error"]
        # The banner names the expected file shape so the user knows what to write.
        assert "additional_authorization_endpoints" in m["content"]
        # No extra meta keys: no shipped surface reads any, so none are emitted.
        assert "rejected_host" not in m["meta"]
        assert "rejected_path" not in m["meta"]
        assert "remedy_shape" not in m["meta"]
        # Query values never leak into any surfaced field.
        serialized = json.dumps(m, ensure_ascii=False)
        assert "AKIAIOSFODNN7EXAMPLE" not in serialized
        assert "topsecretstatevalue" not in serialized

    def test_redacted_path_never_echoes_the_credential(self):
        """A credential-bearing path self-redacts to the shared tag before it
        reaches the error field or the banner text."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        token = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
        _emit_mcp_oauth_request(
            state,
            slot,
            "self-hosted",
            f"https://idp.example/{token}/authorize?state=x",
        )
        m = slot.messages[0]
        assert "idp.example[REDACTED: credential]" in m["meta"]["error"]
        assert token not in json.dumps(m, ensure_ascii=False)

    def test_rejection_banner_survives_unparseable_url(self):
        """A URL that cannot be parsed to a hostname still rejects with the
        original unnamed banner — no endpoint fields, no crash."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(
            state,
            slot,
            "self-hosted",
            "https://[bad-ipv6/authorize?key=AKIAIOSFODNN7EXAMPLE",
        )
        m = slot.messages[0]
        assert m["meta"]["failed"] is True
        assert m["meta"]["rejected_url"] is True
        assert "rejected_host" not in m["meta"]
        assert "rejected_path" not in m["meta"]
        assert "remedy_shape" not in m["meta"]
        assert "AKIAIOSFODNN7EXAMPLE" not in m["content"]

    def test_accepts_real_github_oauth_pkce_url(self):
        """Regression: a legitimate GitHub OAuth + PKCE consent URL must be
        rendered, not rejected.  These URLs carry high-entropy params
        (``state``, ``code_challenge``) and routinely exceed 200 chars, which
        previously tripped the generic long-query *exfiltration* heuristic and
        broke every real sign-in flow ("github authentication failed: URL
        contained credential or exfiltration pattern")."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        url = (
            "https://github.com/login/oauth/authorize"
            "?client_id=Iv1.b507a08c87ecfe98"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
            "&scope=repo%20read%3Aorg"
            "&state=af0ifjsldkj"
            "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
            "&code_challenge_method=S256&response_type=code"
        )
        _emit_mcp_oauth_request(state, slot, "github", url)
        m = slot.messages[0]
        assert m["role"] == "mcp_oauth"
        # Accepted → the auth-request banner with the live URL, NOT a rejection.
        assert m["meta"].get("rejected_url") is not True
        assert m["meta"].get("failed") is not True
        assert m["meta"]["oauth_url"] == url

    def test_rejects_secret_in_non_oauth_param(self):
        """A credential-like blob in a param that is NOT a standard OAuth
        parameter is still treated as exfil and rejected."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        _emit_mcp_oauth_request(
            state,
            slot,
            "sneaky",
            "https://evil.com/authorize?client_id=x&exfil=" + ("A" * 60),
        )
        m = slot.messages[0]
        assert m["meta"]["failed"] is True
        assert m["meta"]["rejected_url"] is True
        assert "oauth_url" not in m["meta"]

    def test_redacts_server_name_in_content_and_meta(self):
        """server_name comes from kiro-cli (untrusted): scrub creds before it
        reaches the banner content (which is broadcast live to the dashboard)
        and the meta.server_name (used as the dedupe correlation key)."""
        slot = _ChatSlot("s1")
        state = MagicMock()
        # AKIA pattern is on the credential-redaction list.
        _emit_mcp_oauth_request(
            state,
            slot,
            "evil-AKIAIOSFODNN7EXAMPLE",
            "https://mcp.example.com/authorize",
        )
        m = slot.messages[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in m["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in m["meta"]["server_name"]

    def test_completed_path_redacts_error_string(self):
        """error string is also kiro-cli-controlled and lands in the live WS
        broadcast — must be redacted on entry."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "linear", "https://mcp.linear.app/a")
        state = MagicMock()
        _mark_mcp_oauth_completed(
            state,
            slot,
            "linear",
            success=False,
            error="leaked AKIAIOSFODNN7EXAMPLE in error",
        )
        m = slot.messages[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in (m["meta"].get("error") or "")
        # The broadcast payload also went through redacted meta.
        payload = state.broadcast_ws.call_args[0][1]
        assert "AKIAIOSFODNN7EXAMPLE" not in (payload["meta"].get("error") or "")


# ── _mark_mcp_oauth_completed ──


class TestMarkMcpOAuthCompleted:
    def _emit(self, slot, server="linear"):
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, server, f"https://mcp.{server}.app/authorize")
        return state

    def test_success_flips_banner(self):
        slot = _ChatSlot("s1")
        state = self._emit(slot)
        _mark_mcp_oauth_completed(state, slot, "linear", success=True)
        m = slot.messages[0]
        assert m["meta"]["completed"] is True
        assert "failed" not in m["meta"]
        assert "authenticated" in m["content"]
        # WS broadcast carries the new state to clients on this slot AND others.
        state.broadcast_ws.assert_called_once()
        msg_type, payload = state.broadcast_ws.call_args[0]
        assert msg_type == "chat_message_update"
        assert payload["slot"] == "s1"
        assert payload["meta"]["completed"] is True

    def test_failure_records_error(self):
        slot = _ChatSlot("s1")
        state = self._emit(slot)
        _mark_mcp_oauth_completed(state, slot, "linear", success=False, error="dns failed")
        m = slot.messages[0]
        assert m["meta"]["failed"] is True
        assert m["meta"]["error"] == "dns failed"
        assert "failed" in m["content"]

    def test_recovery_clears_prior_failure(self):
        """If a failure was recorded, a later success should drop the failed/error keys."""
        slot = _ChatSlot("s1")
        state = self._emit(slot)
        _mark_mcp_oauth_completed(state, slot, "linear", success=False, error="boom")
        # Banner is now in the failed terminal state, so it's no longer "open".
        # A subsequent retry would emit a new banner; mark_completed on the
        # closed banner is a no-op (regression guard for #6 in review).
        prior_call_count = state.broadcast_ws.call_count
        _mark_mcp_oauth_completed(state, slot, "linear", success=True)
        assert state.broadcast_ws.call_count == prior_call_count

    def test_no_matching_banner_is_noop(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        # No mcp_oauth message has been appended for "phantom".
        _mark_mcp_oauth_completed(state, slot, "phantom", success=True)
        state.broadcast_ws.assert_not_called()

    def test_completion_resolves_the_row_by_mid_on_a_ts_collision(self):
        """The completion path shares the supersede path's row-identity problem.

        Two replayed banners can carry the same `ts`; a ts lookup resolves the
        FIRST match, so a completion for the second one would flip the first and
        leave the real banner open. Both paths now resolve by `mid`.
        """
        slot = _ChatSlot("s1")
        collided = "2026-09-01T00:00:00.000000+00:00"
        for n in range(2):
            slot.append(
                "mcp_oauth",
                f"🔐 linear requires authentication. ({n})",
                "msg msg-info",
                ts=collided,
                meta={"server_name": "linear", "oauth_url": f"https://mcp.linear.app/{n}"},
            )
        # Retire the FIRST so the only open banner is the second one.
        first, second = slot.messages
        first["meta"] = {**first["meta"], "superseded": True}

        state = MagicMock()
        _mark_mcp_oauth_completed(state, slot, "linear", success=True)

        assert second["meta"].get("completed") is True, (
            "the completion landed on the wrong row -- ts is not a row identity"
        )
        assert first["meta"].get("completed") is None
        _, payload = state.broadcast_ws.call_args[0]
        assert payload["mid"] == second["meta"]["mid"]

    def test_targets_only_open_banner(self):
        """Two emitted banners (e.g., token expired then re-issued): only the
        most recent un-terminalized one should be patched."""
        slot = _ChatSlot("s1")
        # First banner — completed already (simulate previous turn success).
        _emit_mcp_oauth_request(MagicMock(), slot, "linear", "https://mcp.linear.app/a")
        slot.messages[0]["meta"]["completed"] = True
        # Second banner — open.
        _emit_mcp_oauth_request(MagicMock(), slot, "linear", "https://mcp.linear.app/a")
        state = MagicMock()
        _mark_mcp_oauth_completed(state, slot, "linear", success=True)
        # Both are now completed; first was already completed, second got flipped.
        assert all(m["meta"].get("completed") is True for m in slot.messages)
        # Only one broadcast — for the second banner.
        state.broadcast_ws.assert_called_once()

    def test_superseded_banner_is_not_reopened_by_completion(self):
        """A superseded banner is terminal: a later success must patch the LIVE
        banner, never walk back into the retired one.

        Without the `superseded` arm in the matcher's terminal test, the loop
        walking backwards would still skip it (it stops at the first open
        banner) — but a completion arriving when the newest banner is itself
        retired would reopen history instead of no-oping.
        """
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "linear", "https://mcp.linear.app/one")
        _emit_mcp_oauth_request(MagicMock(), slot, "linear", "https://mcp.linear.app/two")
        # Retire the live one too, as a fresh emit for the same server would.
        _supersede_open_mcp_oauth_banners(MagicMock(), slot, "linear", False)
        state = MagicMock()
        _mark_mcp_oauth_completed(state, slot, "linear", success=True)
        state.broadcast_ws.assert_not_called()
        assert all(m["meta"].get("superseded") is True for m in slot.messages)
        assert not any(m["meta"].get("completed") for m in slot.messages)


# ── _supersede_open_mcp_oauth_banners (issue #7580) ──


class TestSupersededOAuthBanners:
    """A newer authorize request kills the older flow's loopback listener.

    kiro-cli mints the callback port and the PKCE verifier inside the flow it
    announces, so a second announcement for the same server leaves the first
    banner pointing at a port that can no longer redeem anything. Left rendered
    as a live "Authorize" button, it walks the user through a full provider
    login and dead-ends on `http://127.0.0.1:<dead-port>/?code=…` — a page that
    looks like success and consumes nothing (issue #7580).
    """

    def test_second_request_retires_the_first_banner(self):
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a?port=55089")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "miro", "https://mcp.miro.com/a?port=55090")

        assert len(slot.messages) == 2
        stale, live = slot.messages
        # The stale banner is terminal and, crucially, carries NO url — a client
        # that does not know the flag still cannot render the dead link.
        assert stale["meta"]["superseded"] is True
        assert "oauth_url" not in stale["meta"]
        assert "55089" not in stale["content"]
        # The newest banner is the only live one.
        assert live["meta"]["oauth_url"] == "https://mcp.miro.com/a?port=55090"
        assert "superseded" not in live["meta"]

    def test_a_different_server_does_not_retire_this_ones_live_banner(self):
        """Two servers whose names both redact to one sentinel are still two servers.

        `server_name` is stored REDACTED, and `_redact_acp_string` maps every
        credential-shaped name onto the same `[REDACTED: credential]` string. So
        the stored name cannot tell these two servers apart, and retiring on it
        would pop an `oauth_url` that is still live and still redeemable.

        The resolution is to retire NOTHING once redaction has fired, rather than
        to persist a digest of the raw name so the two can be told apart -- that
        digest would be an unsalted, offline-testable verifier for a credential,
        published to the same transcript and broadcast the redaction exists to
        keep it out of. Declining to act is the same posture the rejected-URL
        branches take, and it costs only a stale banner, which is what main does
        anyway.
        """
        name_a = "ghp_" + "A" * 36
        name_b = "ghp_" + "B" * 36
        # Precondition: the redaction really does collapse both onto one value,
        # otherwise this test would pass for the wrong reason.
        assert chat_runner._redact_acp_string(name_a) == chat_runner._redact_acp_string(name_b)

        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, name_a, "https://mcp.a.example/authorize")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, name_b, "https://mcp.b.example/authorize")

        assert len(slot.messages) == 2
        first, second = slot.messages
        # Server A's flow is untouched by server B's request: still live, still
        # holding the URL the user needs.
        assert "superseded" not in first["meta"]
        assert first["meta"]["oauth_url"] == "https://mcp.a.example/authorize"
        assert second["meta"]["oauth_url"] == "https://mcp.b.example/authorize"
        # And nothing was broadcast as retired, since nothing was retired.
        for call in state.broadcast_ws.call_args_list:
            assert call[0][0] != "chat_message_update"
        # Nothing derived from the raw name is persisted -- that is the point.
        for message in slot.messages:
            assert "server_key" not in message["meta"]

    def test_a_repeat_request_from_one_credential_named_server_also_retires_nothing(self):
        """The known limitation, pinned so it cannot regress into a guess.

        Same server twice is the case #7580 is about, but when its NAME redacts we
        cannot prove from the stored row that it IS the same server, so the stale
        banner stays. Documented in the PR body and on the issue: narrow, confined
        to credential-shaped names, and no worse than main.
        """
        name = "ghp_" + "C" * 36
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, name, "https://mcp.c.example/a?port=1")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, name, "https://mcp.c.example/a?port=2")

        assert len(slot.messages) == 2
        assert "superseded" not in slot.messages[0]["meta"]
        assert slot.messages[0]["meta"]["oauth_url"] == "https://mcp.c.example/a?port=1"

    def test_a_server_name_with_a_lone_surrogate_does_not_crash_the_banner(self):
        """An ACP server name is untrusted text, and JSON can carry a lone surrogate.

        `json.loads` accepts `"\\ud800"`, so `serverName` can reach us holding a
        character that has no UTF-8 encoding. Nothing on this path may encode it:
        an earlier revision hashed the raw name and a plain `.encode("utf-8")`
        raised `UnicodeEncodeError`, taking the whole OAuth event handler down so
        the user got no banner at all for a server whose only sin is a bad name.
        """
        name = json.loads('"bad\\ud800name"')
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, name, "https://mcp.a.example/authorize")

        assert len(slot.messages) == 1
        assert slot.messages[0]["meta"]["oauth_url"] == "https://mcp.a.example/authorize"

    def test_retirement_is_broadcast_to_clients(self):
        """An open tab must be repainted, or the dead button stays clickable in
        the UI the user is actually looking at."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "miro", "https://mcp.miro.com/b")

        state.broadcast_ws.assert_called_once()
        msg_type, payload = state.broadcast_ws.call_args[0]
        assert msg_type == "chat_message_update"
        assert payload["slot"] == "s1"
        assert payload["meta"]["superseded"] is True

    def test_broadcast_identifies_the_row_by_mid_not_only_ts(self):
        """`ts` is not a row identity on the wire either.

        The client patches by the first row matching a `ts`, so two retirements
        for two rows sharing one would both land on the same row and leave the
        other rendering a dead Authorize link. `mid` is the server-minted per-row
        identity the client prefers.
        """
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        stale_mid = slot.messages[0]["meta"]["mid"]
        assert stale_mid

        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "miro", "https://mcp.miro.com/b")

        _, payload = state.broadcast_ws.call_args[0]
        assert payload["mid"] == stale_mid

    def test_broadcast_blanks_the_url_even_though_persisted_meta_omits_it(self):
        """The client MERGES an incoming meta over the row's existing one.

        Omitting `oauth_url` would leave the live URL on the client row, so a tab
        still running pre-upgrade JS -- which does not know `superseded` -- would
        keep rendering the dead link. An empty string fails that client's own
        safe-URL check, so the banner withdraws. The PERSISTED meta still omits
        the key entirely.
        """
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        state = MagicMock()
        _emit_mcp_oauth_request(state, slot, "miro", "https://mcp.miro.com/b")

        _, payload = state.broadcast_ws.call_args[0]
        assert payload["meta"]["oauth_url"] == ""
        # Persisted meta drops the key rather than blanking it.
        assert "oauth_url" not in slot.messages[0]["meta"]

    def test_every_stale_banner_is_retired_not_just_the_newest(self):
        """Banners accumulate one per re-announce (each session init re-emits
        pending requests), so stopping at the first match would leave the older
        ones live."""
        slot = _ChatSlot("s1")
        for n in range(3):
            _emit_mcp_oauth_request(MagicMock(), slot, "miro", f"https://mcp.miro.com/{n}")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/live")

        *stale, live = slot.messages
        assert len(stale) == 3
        assert all(m["meta"].get("superseded") is True for m in stale)
        assert all("oauth_url" not in m["meta"] for m in stale)
        assert live["meta"]["oauth_url"] == "https://mcp.miro.com/live"

    def test_other_servers_are_untouched(self):
        """Each server owns its own flow; retiring one must not retire another's
        live authorize link."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "linear", "https://mcp.linear.app/a")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/b")

        linear = slot.messages[0]
        assert "superseded" not in linear["meta"]
        assert linear["meta"]["oauth_url"] == "https://mcp.linear.app/a"

    def test_already_completed_banner_is_left_alone(self):
        """A completed banner is history the user should keep seeing as success,
        not be told was superseded."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        _mark_mcp_oauth_completed(MagicMock(), slot, "miro", success=True)
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/b")

        first = slot.messages[0]
        assert first["meta"]["completed"] is True
        assert "superseded" not in first["meta"]

    def test_already_failed_banner_is_left_alone(self):
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        _mark_mcp_oauth_completed(MagicMock(), slot, "miro", success=False, error="dns")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/b")

        first = slot.messages[0]
        assert first["meta"]["failed"] is True
        assert first["meta"]["error"] == "dns"
        assert "superseded" not in first["meta"]

    def test_rejected_url_does_not_retire_a_live_banner(self):
        """The rejected branches append a banner with no `oauth_url`, so
        retiring the older one would take away the user's only authorize
        affordance and hand back nothing usable."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "javascript:alert(1)")

        live, rejected = slot.messages
        assert live["meta"]["oauth_url"] == "https://mcp.miro.com/a"
        assert "superseded" not in live["meta"]
        assert rejected["meta"]["failed"] is True

    def test_no_open_banner_is_a_noop(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        _supersede_open_mcp_oauth_banners(state, slot, "miro", False)
        state.broadcast_ws.assert_not_called()
        assert slot.messages == []

    def test_colliding_timestamps_still_retire_every_banner(self):
        """`ts` is not a row identity, so retirement must not resolve rows by it.

        `_ChatSlot.append` preserves an explicitly supplied `ts` verbatim -- a row
        replayed from a channel transcript keeps its own -- and its docstring notes
        a coarse OS clock stamps two same-tick rows identically, which is why every
        row also carries a random id. A ts-keyed patch resolves the FIRST match, so
        on a collision it would rewrite one row twice and leave the second banner
        open, still offering the dead link this function exists to withdraw.

        Two replayed banners sharing a `ts` is the reachable shape, so they are
        appended directly here: going through `_emit_mcp_oauth_request` twice would
        have the second call retire the first, leaving only one row open and never
        exercising the collision at all.
        """
        slot = _ChatSlot("s1")
        collided = "2026-09-01T00:00:00.000000+00:00"
        for n in range(2):
            slot.append(
                "mcp_oauth",
                f"🔐 miro requires authentication. ({n})",
                "msg msg-info",
                ts=collided,
                meta={"server_name": "miro", "oauth_url": f"https://mcp.miro.com/{n}"},
            )
        assert [m["ts"] for m in slot.messages] == [collided, collided]

        state = MagicMock()
        _supersede_open_mcp_oauth_banners(state, slot, "miro", False)

        assert all(m["meta"].get("superseded") is True for m in slot.messages), (
            "a ts collision left a banner open, still offering a dead loopback link"
        )
        assert all("oauth_url" not in m["meta"] for m in slot.messages)
        # Each row patched exactly once -- not the first one twice.
        assert state.broadcast_ws.call_count == 2

    def test_retirement_marks_the_slot_dirty_so_it_is_persisted(self):
        """The patch has to reach disk, or a reload resurrects the dead link."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        slot._dirty = False
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/b")
        assert slot._dirty is True

    def test_retirement_survives_the_display_time_meta_gate(self):
        """The retired banner must still be link-free after the read path's
        redaction pass — that is the layer the client actually consumes."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/a")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "https://mcp.miro.com/b")

        prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
        stale = prepared[0]
        assert stale["meta"]["superseded"] is True
        assert not stale["meta"].get("oauth_url")


# ── read-time minting-child liveness gate (issues #7654, #8149) ──


class TestExpiredByDeadChild:
    """A banner outlives the process whose listener made its URL redeemable.

    The entity whose death invalidates the Authorize link is the ACP child that
    minted it: that process holds the loopback callback listener and the PKCE
    verifier. Every way it can die is silent to the banner — a gateway restart,
    a session reset, the idle sweep, the RSS recycle, or the child exiting on
    its own — so no write-time site can enumerate them. The read-time gate
    compares the process-instance identity stamped at mint against the child
    CURRENTLY serving the slot, resolved by the caller of `_prepare_messages`.
    """

    def _slot_with_banner(self, minted_by="child-a"):
        """A slot holding one open banner stamped with *minted_by*."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(),
            slot,
            "miro",
            "https://mcp.miro.com/a?port=55089",
            minted_by=minted_by,
        )
        return slot

    def test_banner_is_expired_once_its_child_is_gone(self):
        """live_child='' is what every teardown looks like from here: the idle
        sweep, the RSS recycle, a session reset, a gateway restart, and a child
        that exited on its own all leave no live provider for the slot."""
        slot = self._slot_with_banner()
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
        assert prepared[0]["meta"]["expired"] is True

    def test_the_dead_link_is_withdrawn_not_merely_flagged(self):
        """A client that has never heard of `expired` must still not render it."""
        slot = self._slot_with_banner()
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
        assert not prepared[0]["meta"].get("oauth_url")

    def test_a_banner_from_the_live_child_keeps_its_link(self):
        """The false-positive direction: never take away a working button."""
        slot = self._slot_with_banner(minted_by="child-a")
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="child-a")
        assert prepared[0]["meta"]["oauth_url"] == "https://mcp.miro.com/a?port=55089"
        assert "expired" not in prepared[0]["meta"]

    def test_a_replacement_child_does_not_keep_the_banner_alive(self):
        """The resume trap. A resumed session carries the SAME ACP session id on
        a NEW process (client.py re-sets `_session_id` from `resume_sid`), which
        is why the stamp is a per-spawn process-instance identity and never the
        session id: the new child holds neither the loopback listener nor the
        PKCE verifier of the flow the old one started."""
        slot = self._slot_with_banner(minted_by="child-a")
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="child-b")
        assert prepared[0]["meta"]["expired"] is True
        assert not prepared[0]["meta"].get("oauth_url")

    def test_emit_stamps_the_minting_child(self):
        slot = self._slot_with_banner(minted_by="child-a")
        assert slot.messages[0]["meta"]["child"] == "child-a"

    def test_an_unstamped_legacy_banner_is_expired(self):
        """A row with no `child` stamp was written by an older build.

        Running this build means this process replaced the one that hosted that
        row's child, so the child is provably gone. That deduction is what lets
        the fix also retire banners that went stale before it shipped.
        """
        slot = _ChatSlot("s1")
        slot.append(
            "mcp_oauth",
            "🔐 miro requires authentication.",
            "msg msg-info",
            ts="t1",
            meta={"server_name": "miro", "oauth_url": "https://mcp.miro.com/a"},
        )
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="child-a")
        assert prepared[0]["meta"]["expired"] is True
        assert not prepared[0]["meta"].get("oauth_url")

    def test_a_gen_only_legacy_banner_is_expired(self):
        """A row stamped only with the older gateway-generation key is a
        pre-upgrade row; its child cannot be the live one."""
        slot = _ChatSlot("s1")
        slot.append(
            "mcp_oauth",
            "🔐 miro requires authentication.",
            "msg msg-info",
            ts="t1",
            meta={
                "server_name": "miro",
                "oauth_url": "https://mcp.miro.com/a",
                "gen": "some-generation",
            },
        )
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="child-a")
        assert prepared[0]["meta"]["expired"] is True

    def test_an_empty_stamp_never_matches_an_empty_live_child(self):
        """Both sides empty is two unknowns, not a match: a banner whose minting
        child could not be identified must not be kept alive by a slot that has
        no live child either. Pins the non-empty requirement on BOTH sides of
        the comparison (flipping either `and` to accept empties fails here)."""
        slot = self._slot_with_banner(minted_by="")
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
        assert prepared[0]["meta"]["expired"] is True

    def test_a_recorded_outcome_is_never_reinterpreted_by_a_later_read(self):
        """`completed`/`failed`/`superseded` were written by the process that
        observed them; a dead minting child does not overrule them."""
        for flag in ("completed", "failed", "superseded"):
            slot = _ChatSlot("s1")
            slot.append(
                "mcp_oauth",
                "x",
                "msg msg-info",
                ts="t1",
                meta={
                    "server_name": "miro",
                    "oauth_url": "https://mcp.miro.com/a",
                    "child": "dead-child",
                    flag: True,
                },
            )
            prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
            assert "expired" not in prepared[0]["meta"], flag
            assert prepared[0]["meta"][flag] is True

    def test_a_rejected_url_banner_is_not_relabelled(self):
        """Those banners carry no `oauth_url`, so there is no link to withdraw and
        their own `failed` reason must survive."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "miro", "javascript:alert(1)")
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
        assert "expired" not in prepared[0]["meta"]
        assert prepared[0]["meta"]["failed"] is True

    def test_a_non_oauth_row_is_untouched(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", "hello", "msg msg-a", ts="t1", meta={"oauth_url": "x"})
        prepared = _prepare_messages(list(slot.messages), running=False, live_child="")
        assert "expired" not in (prepared[0].get("meta") or {})

    def test_the_gate_does_not_rewrite_the_stored_row(self):
        """It is a presentation verdict. The transcript keeps saying what happened,
        and re-reading the same slot must not accumulate edits."""
        slot = self._slot_with_banner()
        _prepare_messages(list(slot.messages), running=False, live_child="")
        assert slot.messages[0]["meta"]["oauth_url"] == "https://mcp.miro.com/a?port=55089"
        assert "expired" not in slot.messages[0]["meta"]

    def test_a_completion_event_cannot_target_an_expired_banner(self):
        """Once retired, a late event for that server must not reopen the row."""
        slot = _ChatSlot("s1")
        slot.append(
            "mcp_oauth",
            "x",
            "msg msg-info",
            ts="t1",
            meta={"server_name": "miro", "expired": True},
        )
        _mark_mcp_oauth_completed(MagicMock(), slot, "miro", success=True)
        assert "completed" not in slot.messages[0]["meta"]


class TestLiveChildInstance:
    """`_live_child_instance` is the caller-side half of the read-time gate.

    `_prepare_messages` is synchronous, may run in a worker thread, and has no
    ACP access, so its three async callers resolve the live child's identity on
    the event loop — where the session pool is in scope — and pass the verdict
    in. `""` is the one answer for every no-live-child case, which is exactly
    what makes the idle sweep and the RSS recycle covered without either path
    knowing banners exist: both end in the session being popped from the pool.
    """

    def _state_with_provider(self, provider):
        state = MagicMock()
        state.sessions.get_provider.return_value = provider
        return state

    def test_no_session_in_the_pool_answers_empty(self):
        """The idle-sweep / RSS-recycle / reset / restart shape: the session is
        gone from the pool, so there is nothing whose identity could match."""
        state = self._state_with_provider(None)
        assert _live_child_instance(state, _ChatSlot("s1")) == ""

    def test_a_live_provider_answers_its_process_instance(self):
        provider = MagicMock()
        provider.is_process_alive.return_value = True
        provider.process_instance = "child-a"
        state = self._state_with_provider(provider)
        assert _live_child_instance(state, _ChatSlot("s1")) == "child-a"

    def test_a_dead_process_answers_empty_even_while_registered(self):
        """The self-exit case: the session is still in the pool but its child
        exited on its own. The identity of a dead process must not keep its
        banner's button clickable."""
        provider = MagicMock()
        provider.is_process_alive.return_value = False
        provider.process_instance = "child-a"
        state = self._state_with_provider(provider)
        assert _live_child_instance(state, _ChatSlot("s1")) == ""

    def test_a_probe_failure_answers_empty_not_raises(self):
        """The verdict rides slot-detail reads; a probe failure must degrade to
        withdrawing a possibly-dead button, never fail the read."""
        state = MagicMock()
        state.sessions.get_provider.side_effect = RuntimeError("pool exploded")
        assert _live_child_instance(state, _ChatSlot("s1")) == ""

    def test_a_provider_without_the_property_answers_empty(self):
        """A provider not backed by a child process mints no OAuth flows, so an
        empty identity (expiring any stamped banner) is the correct verdict."""
        provider = MagicMock(spec=["is_process_alive"])
        provider.is_process_alive.return_value = True
        state = self._state_with_provider(provider)
        assert _live_child_instance(state, _ChatSlot("s1")) == ""

    def test_end_to_end_a_swept_sessions_banner_is_withdrawn_on_read(self):
        """The whole #8149 defect in one path: mint under child A, sweep the
        session (pool answers None), and the next read withdraws the link."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "miro", "https://mcp.miro.com/a?port=55089", minted_by="child-a"
        )
        state = self._state_with_provider(None)
        prepared = _prepare_messages(
            list(slot.messages), running=False, live_child=_live_child_instance(state, slot)
        )
        assert prepared[0]["meta"]["expired"] is True
        assert not prepared[0]["meta"].get("oauth_url")


class TestBroadcastExpiredOauthBanners:
    """The freshness push for already-open tabs.

    The read-time gate runs only when a client fetches, so a tab left open
    across a teardown keeps rendering the dead Authorize link it already has.
    `_broadcast_expired_oauth_banners` closes that window at the teardowns the
    dashboard can see. It is verdict-driven — it re-resolves the live child and
    applies the read gate's own predicate — so callers invoke it
    unconditionally: no pre-teardown snapshot, no outcome gating.
    """

    def _state(self, provider):
        state = MagicMock()
        state.sessions.get_provider.return_value = provider
        return state

    def _live_provider(self, token="child-a"):
        provider = MagicMock()
        provider.is_process_alive.return_value = True
        provider.process_instance = token
        return provider

    def test_a_dead_childs_banner_is_broadcast_as_expired(self):
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "miro", "https://mcp.miro.com/a?port=55089", minted_by="child-a"
        )
        state = self._state(None)  # session swept from the pool
        _broadcast_expired_oauth_banners(state, slot)
        state.broadcast_ws.assert_called_once()
        kind, payload = state.broadcast_ws.call_args[0]
        assert kind == "chat_message_update"
        assert payload["meta"]["expired"] is True
        assert payload["mid"] == slot.messages[0]["meta"]["mid"]

    def test_the_frame_blanks_the_url_for_a_pre_upgrade_client(self):
        """The client MERGES incoming meta, so omitting the key would leave the
        dead URL on a tab whose JS does not know `expired`."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "miro", "https://mcp.miro.com/a", minted_by="child-a"
        )
        state = self._state(None)
        _broadcast_expired_oauth_banners(state, slot)
        payload = state.broadcast_ws.call_args[0][1]
        assert payload["meta"]["oauth_url"] == ""

    def test_the_stored_row_is_not_rewritten(self):
        """Presentation only: the read gate stays the one liveness mechanism,
        and the transcript keeps saying what happened."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "miro", "https://mcp.miro.com/a", minted_by="child-a"
        )
        _broadcast_expired_oauth_banners(self._state(None), slot)
        assert slot.messages[0]["meta"]["oauth_url"] == "https://mcp.miro.com/a"
        assert "expired" not in slot.messages[0]["meta"]

    def test_a_live_childs_banner_is_not_broadcast(self):
        """The declined/failed-teardown shape: the child is still alive, so the
        stamps match and the button keeps working. This is what lets callers
        skip the old outcome gate."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "miro", "https://mcp.miro.com/a", minted_by="child-a"
        )
        state = self._state(self._live_provider("child-a"))
        _broadcast_expired_oauth_banners(state, slot)
        state.broadcast_ws.assert_not_called()

    def test_a_successors_banner_is_not_swept_with_the_dead_ones(self):
        """The race the old snapshot protected against, now answered by
        identity: the successor's banner names the live child and survives,
        while the dead child's banner is withdrawn."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "miro", "https://mcp.miro.com/old?port=1", minted_by="child-a"
        )
        _emit_mcp_oauth_request(
            MagicMock(), slot, "linear", "https://mcp.linear.app/new?port=2", minted_by="child-b"
        )
        state = self._state(self._live_provider("child-b"))
        _broadcast_expired_oauth_banners(state, slot)
        state.broadcast_ws.assert_called_once()
        payload = state.broadcast_ws.call_args[0][1]
        assert payload["mid"] == slot.messages[0]["meta"]["mid"]

    def test_a_recorded_outcome_is_not_broadcast(self):
        """`completed`/`failed`/`superseded` rows and the rejected-URL banners
        are the gate's own exclusions, and the helper inherits them."""
        for flag in ("completed", "failed", "superseded"):
            slot = _ChatSlot("s1")
            slot.append(
                "mcp_oauth",
                "x",
                "msg msg-info",
                ts="t1",
                meta={
                    "server_name": "miro",
                    "oauth_url": "https://mcp.miro.com/a",
                    "child": "dead-child",
                    flag: True,
                },
            )
            state = self._state(None)
            _broadcast_expired_oauth_banners(state, slot)
            state.broadcast_ws.assert_not_called()

    def test_no_banner_is_a_noop(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", "hello", "msg msg-a", ts="t1")
        state = self._state(None)
        _broadcast_expired_oauth_banners(state, slot)
        state.broadcast_ws.assert_not_called()


# ── per-spawn process-instance identity (the stamp's provider side) ──


class TestProcessInstanceContract:
    """The identity the banner stamp and the read gate both stand on.

    A token names ONE spawn of a child process: minted with the process,
    masked the moment the process object is gone. The property must never
    answer with a previous spawn's token — that equality is exactly the
    fail-open the design removes (a resumed session id surviving onto a new
    process).
    """

    def _bare_client(self):
        from kiro_crew.acp.client import AcpClient

        client = object.__new__(AcpClient)
        return client

    def _bare_runtime(self):
        from kiro_crew.acp.runtime import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        return runtime

    def test_client_answers_empty_without_a_process(self):
        client = self._bare_client()
        client._process = None
        client._process_instance = "left-over-token"
        assert client.process_instance == ""

    def test_client_answers_the_current_processes_token(self):
        client = self._bare_client()
        client._process = object()
        client._process_instance = "tok-a"
        assert client.process_instance == "tok-a"

    def test_runtime_answers_empty_without_a_process(self):
        runtime = self._bare_runtime()
        runtime._process = None
        runtime._process_instance = "left-over-token"
        assert runtime.process_instance == ""

    def test_runtime_answers_the_current_processes_token(self):
        runtime = self._bare_runtime()
        runtime._process = object()
        runtime._process_instance = "tok-b"
        assert runtime.process_instance == "tok-b"


# ── _ChatSlot.update_message ──


class TestSlotUpdateMessage:
    def test_patches_content_and_meta(self):
        slot = _ChatSlot("s1")
        slot.append("mcp_oauth", "old", "msg msg-info", ts="2024-01-01T00:00:00Z", meta={"a": 1})
        slot._dirty = False
        out = slot.update_message("2024-01-01T00:00:00Z", content="new", meta={"a": 2, "b": 3})
        assert out is not None
        assert slot.messages[0]["content"] == "new"
        assert slot.messages[0]["meta"] == {"a": 2, "b": 3}
        assert slot._dirty is True

    def test_meta_replacement_drops_stale_keys(self):
        """meta is replaced wholesale (not merged), so callers can remove keys."""
        slot = _ChatSlot("s1")
        slot.append("mcp_oauth", "old", "msg", ts="t1", meta={"failed": True, "error": "x"})
        slot.update_message("t1", meta={"completed": True})
        assert slot.messages[0]["meta"] == {"completed": True}

    def test_unknown_ts_returns_none(self):
        slot = _ChatSlot("s1")
        slot.append("mcp_oauth", "x", "msg", ts="t1")
        slot._dirty = False
        out = slot.update_message("t-missing", content="y")
        assert out is None
        assert slot._dirty is False  # untouched

    def test_empty_ts_returns_none(self):
        slot = _ChatSlot("s1")
        slot.append("mcp_oauth", "x", "msg", ts="t1")
        out = slot.update_message("", content="y")
        assert out is None


# ── Legit-URL corpus: these provider OAuth URLs must NEVER be rejected ──


class TestLegitOAuthUrlCorpus:
    """Contract: every real provider authorization URL in oauth_url_corpus
    must pass the banner safety check.  A failure here means we've broken
    sign-in for that provider — the exact class of regression that motivated
    this corpus (GitHub OAuth+PKCE URLs rejected as 'credential or
    exfiltration pattern')."""

    @pytest.mark.parametrize("provider,url", LEGIT_OAUTH_URLS, ids=[p for p, _ in LEGIT_OAUTH_URLS])
    def test_corpus_url_not_flagged_as_credential(self, provider: str, url: str):
        assert (
            oauth_url_contains_credential(url) is False
        ), f"{provider}: legit OAuth URL wrongly flagged as containing a credential"

    @pytest.mark.parametrize("provider,url", LEGIT_OAUTH_URLS, ids=[p for p, _ in LEGIT_OAUTH_URLS])
    def test_corpus_url_renders_banner(self, provider: str, url: str):
        """End-to-end: the URL is rendered as a live auth banner (with the
        clickable oauth_url), not a rejection banner."""
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, provider, url)
        assert len(slot.messages) == 1
        meta = slot.messages[0]["meta"]
        assert meta.get("rejected_url") is not True, f"{provider}: wrongly rejected"
        assert meta.get("failed") is not True, f"{provider}: wrongly marked failed"
        assert meta["oauth_url"] == url


class TestOAuthParamCredentialScan:
    """A hard credential signature inside an OAuth param is still exfil."""

    def test_akia_in_state_param_rejected(self):
        # A real OAuth `state` is opaque/high-entropy, but it never legitimately
        # carries an AWS key — a malicious MCP server smuggling one out must be
        # caught even though `state` is an exempted OAuth param.
        url = (
            "https://github.com/login/oauth/authorize?client_id=Iv1.x"
            "&state=AKIAIOSFODNN7EXAMPLE&response_type=code"
        )
        assert oauth_url_contains_credential(url) is True

    def test_slack_token_in_redirect_uri_rejected(self):
        url = (
            "https://evil.com/authorize?client_id=x"
            "&redirect_uri=https://evil.com/cb?t=xoxb-123-abc"
        )
        assert oauth_url_contains_credential(url) is True

    def test_high_entropy_pkce_state_still_allowed(self):
        # A genuine PKCE state/code_challenge (base64-ish, 40+ chars) must NOT
        # be rejected — that was the whole point of the OAuth-param exemption.
        url = (
            "https://github.com/login/oauth/authorize?client_id=Iv1.x"
            "&state=af0ifjsldkjLONGopaqueTOKENvalue1234567890abcd"
            "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
            "&code_challenge_method=S256&response_type=code"
        )
        assert oauth_url_contains_credential(url) is False

    def test_superhuman_mcp_endpoint_high_entropy_state_allowed(self):
        # Superhuman's MCP authorization endpoint is in the builtin allowlist
        # (derived from the Connections registry issuer), so its opaque
        # high-entropy state must NOT be flagged. This is the exact reconnect
        # that fails with "URL contained credential or exfiltration pattern"
        # when the endpoint is absent.
        url = (
            "https://mcp.auth.mail.superhuman.com/oauth2/authorize"
            "?client_id=sh_mcp_client_0123456789&response_type=code"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
            "&scope=email+offline_access"
            "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
            "&code_challenge_method=S256"
            "&state=" + ("Kp7mQ2xR" * 12)
        )
        assert oauth_url_contains_credential(url) is False

    def test_superhuman_high_entropy_state_still_rejected_at_unlisted_host(self):
        # The endpoint allowlist -- not the params -- grants the exemption: the
        # SAME high-entropy state at a look-alike host that is not on the
        # allowlist must still be rejected (no suffix match, no host spoof).
        url = (
            "https://mcp.auth.mail.superhuman.com.attacker.example/oauth2/authorize"
            "?client_id=sh_mcp_client_0123456789&response_type=code"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
            "&scope=email+offline_access"
            "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
            "&code_challenge_method=S256"
            "&state=" + ("Kp7mQ2xR" * 12)
        )
        assert oauth_url_contains_credential(url) is True

    def test_superhuman_endpoint_does_not_waive_fixed_credentials(self):
        # The allowlist exempts only OAuth entropy -- a hard credential
        # signature (AWS key) smuggled into a param must still be rejected at
        # the approved endpoint.
        url = (
            "https://mcp.auth.mail.superhuman.com/oauth2/authorize"
            "?client_id=x&state=AKIAIOSFODNN7EXAMPLE&response_type=code"
        )
        assert oauth_url_contains_credential(url) is True

    def test_superhuman_endpoint_is_in_builtin_allowlist(self):
        # Pins the entry at the data layer: the pair must be present exactly
        # (host lowercase, path exact), so a rename or move in the set fails
        # loudly rather than silently re-breaking reconnect.
        from kiro_crew.security import _OAUTH_AUTHORIZATION_ENDPOINTS

        assert (
            "mcp.auth.mail.superhuman.com",
            "/oauth2/authorize",
        ) in _OAUTH_AUTHORIZATION_ENDPOINTS


# ── Banner gate consolidation: one security predicate, no local copy (#2403) ──


class TestBannerGateIsCanonicalSecurityPredicate:
    """The banner's credential gate must be security.oauth_url_contains_credential
    itself — never a dashboard-local copy that can drift from the tested one.

    A pure delegating wrapper is behaviourally indistinguishable from a direct
    call, so these tests pin the *structure* instead: the local symbol must not
    exist, the name chat_runner calls must be the canonical function object, and
    the emit path must consult exactly that binding.
    """

    def test_no_local_copy_of_the_gate_exists(self):
        # Fails on any reintroduction of a dashboard-local `_oauth_url_...`
        # helper — the drift vector issue #2403 closed.
        assert not hasattr(chat_runner, "_oauth_url_contains_credential")

    def test_chat_runner_binding_is_the_canonical_function(self):
        from kiro_crew import security

        assert chat_runner.oauth_url_contains_credential is security.oauth_url_contains_credential

    def test_emit_path_consults_the_single_binding(self, monkeypatch):
        # Swap the one binding and the banner verdict must follow it. If a
        # second copy of the predicate logic existed on the emit path, the
        # verdict would not flip and this URL would render as a live banner.
        seen: list[str] = []

        def flagging_gate(url: str) -> bool:
            seen.append(url)
            return True

        monkeypatch.setattr(chat_runner, "oauth_url_contains_credential", flagging_gate)
        slot = _ChatSlot("s1")
        url = "https://github.com/login/oauth/authorize?client_id=Iv1.x&state=ok"
        _emit_mcp_oauth_request(MagicMock(), slot, "srv", url)

        assert seen == [url]
        assert len(slot.messages) == 1
        meta = slot.messages[0]["meta"]
        assert meta.get("rejected_url") is True
        assert meta.get("failed") is True
        assert "oauth_url" not in meta


# ── session-init OAuth requests: always emitted, card ownership annotated ──


class _FakeAcpClient:
    """Stands in for AcpClient's pending-oauth buffer."""

    def __init__(self, pending):
        self._pending = list(pending)

    def pop_pending_oauth_requests(self):
        out, self._pending = self._pending, []
        return out


class _FakeProviderClient:
    """Mirrors the ``client.client`` nesting chat_runner reaches through."""

    def __init__(self, pending):
        self.client = _FakeAcpClient(pending)


# A real registry slug with a rendered card, and a real slug whose launch gate is
# closed. Read from the registry rather than hardcoded so a gate flip fails here
# instead of silently changing which requests get annotated.
CARDED_SLUG = "notion"
GATED_SLUG = "github"


def _own(tmp_path, monkeypatch, servers) -> None:
    """Point discovery's kirocrew scope at a temp store holding ``servers``.

    Patches the real read path rather than stubbing ``kirocrew_managed_names``, so
    the store's own parsing (and its fail-open branches) is what the annotation is
    tested against. An unrecognized path buckets as ``SCOPE_KIROCREW``, which is
    the scope Connect writes to.
    """
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (path,))


def _pending(*names):
    return _FakeProviderClient(
        [{"serverName": n, "oauthUrl": f"https://{n}.example.com/authorize?client_id=x"}
         for n in names]
    )


def _owned_flags(slot) -> list[bool]:
    """``card_owned`` per emitted message, absent read as False."""
    return [bool(m["meta"].get("card_owned")) for m in slot.messages]


class TestRegistrySlugsNeedNoAliasWidening:
    def test_every_slug_is_its_own_alias(self):
        """The annotation matches slugs verbatim; this is why that is sufficient.

        kiro-cli reports ``mcp_server_alias(key)`` as the ``serverName``. Registry
        slugs are validated slash-free, so the alias IS the slug and no widening is
        needed. A slug that ever gained a slash would break the match silently, so
        the property is pinned rather than assumed.
        """
        for provider in get_all_registry_providers():
            slug = provider["slug"]
            assert mcp_server_alias(slug) == slug


class TestEveryPendingRequestIsEmitted:
    """The drain never drops a request — not even one a card owns.

    The ``mcp_oauth`` message is the Connections card's approval-URL feed
    (``latestOAuthByServer`` reads ``meta.oauth_url`` off chat messages), so
    dropping one strips the user's only path to authorize. These tests pin
    emission for every row of the matrix; ownership only changes the annotation.
    """

    @pytest.mark.asyncio
    async def test_carded_provider_is_emitted_and_annotated(self, tmp_path, monkeypatch):
        _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}})
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(CARDED_SLUG))
        assert len(slot.messages) == 1
        meta = slot.messages[0]["meta"]
        assert meta["card_owned"] is True
        # The URL the card reads must survive the annotation.
        assert meta["oauth_url"] == f"https://{CARDED_SLUG}.example.com/authorize?client_id=x"
        assert meta["server_name"] == CARDED_SLUG

    @pytest.mark.asyncio
    async def test_custom_server_in_our_own_store_is_not_annotated(self, tmp_path, monkeypatch):
        """Store ownership alone must NOT annotate.

        The dashboard's add-custom-server API writes to the same store as Connect,
        so a hand-added remote is equally "managed" while having no card anywhere.
        Annotating it would let the render layer hide its only prompt.
        """
        _own(tmp_path, monkeypatch, {"my-custom-remote": {"url": "https://mine.example.com/mcp"}})
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending("my-custom-remote"))
        assert len(slot.messages) == 1
        assert "card_owned" not in slot.messages[0]["meta"]

    @pytest.mark.asyncio
    async def test_launch_gated_provider_is_not_annotated(self, tmp_path, monkeypatch):
        """No card is rendered behind a closed launch gate, so chat stays the prompt."""
        assert GATED_SLUG not in {p["slug"] for p in get_visible_providers()}
        _own(tmp_path, monkeypatch, {GATED_SLUG: {"url": "https://api.githubcopilot.com/mcp/"}})
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(GATED_SLUG))
        assert _owned_flags(slot) == [False]

    @pytest.mark.asyncio
    async def test_server_outside_our_store_is_not_annotated(self, tmp_path, monkeypatch):
        """A card alone must not annotate either — we must have written the entry."""
        _own(tmp_path, monkeypatch, {})
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(CARDED_SLUG))
        assert _owned_flags(slot) == [False]

    @pytest.mark.asyncio
    async def test_mixed_batch_annotates_only_the_carded_one(self, tmp_path, monkeypatch):
        _own(
            tmp_path,
            monkeypatch,
            {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}, "handmade": {"url": "https://h"}},
        )
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(
            MagicMock(), slot, _pending(CARDED_SLUG, "handmade")
        )
        assert [m["meta"]["server_name"] for m in slot.messages] == [CARDED_SLUG, "handmade"]
        assert _owned_flags(slot) == [True, False]

    @pytest.mark.asyncio
    async def test_rejected_url_is_emitted_unannotated_for_a_carded_provider(
        self, tmp_path, monkeypatch
    ):
        """A rejected URL is a security notice, not a consent prompt.

        No card can act on "this server sent an unsafe URL", so the notice must
        never be annotated — it stays visible wherever banners render.
        """
        _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}})
        slot = _ChatSlot("s1")
        client = _FakeProviderClient(
            [{"serverName": CARDED_SLUG, "oauthUrl": "javascript:alert(1)"}]
        )
        await _drain_session_init_oauth_requests(MagicMock(), slot, client)
        assert len(slot.messages) == 1
        assert slot.messages[0]["meta"]["rejected_url"] is True
        assert "card_owned" not in slot.messages[0]["meta"]


class TestAnnotationFailsOpen:
    """Any failure resolving ownership yields un-annotated messages.

    Un-annotated is today's behavior: every surface renders every banner. The
    opposite direction would let a broken store file hide a prompt.
    """

    @pytest.mark.asyncio
    async def test_malformed_store_file_fails_open(self, tmp_path, monkeypatch):
        path = tmp_path / "mcp.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (path,))
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(CARDED_SLUG))
        assert _owned_flags(slot) == [False]

    @pytest.mark.asyncio
    async def test_missing_store_file_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (tmp_path / "absent.json",))
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(CARDED_SLUG))
        assert _owned_flags(slot) == [False]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("broken", ["kirocrew_managed_names", "get_visible_providers"])
    async def test_either_lookup_raising_fails_open(self, tmp_path, monkeypatch, broken):
        """Both halves of the predicate must fail open, not just the store read."""
        _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}})
        slot = _ChatSlot("s1")
        with patch(
            f"kiro_crew.dashboard.chat_runner.{broken}", side_effect=RuntimeError("boom")
        ):
            await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(CARDED_SLUG))
        assert _owned_flags(slot) == [False]


class TestDrainDoesNoBlockingWorkOnTheLoop:
    """The drain runs on the event loop; its ownership lookup reads files.

    Both facts are pinned behaviorally rather than by asserting a call to
    ``asyncio.to_thread``, so the guarantee survives a refactor to any other
    off-loop mechanism.
    """

    @pytest.mark.asyncio
    async def test_ownership_lookup_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}})
        seen: list[str] = []
        real = chat_runner._connections_managed_mcp_names

        def _record():
            seen.append(threading.current_thread().name)
            return real()

        monkeypatch.setattr(chat_runner, "_connections_managed_mcp_names", _record)
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _pending(CARDED_SLUG))
        assert seen, "ownership lookup never ran"
        assert seen[0] != threading.current_thread().name
        # The annotation still lands despite the thread hop.
        assert _owned_flags(slot) == [True]

    @pytest.mark.asyncio
    async def test_lookup_is_skipped_entirely_when_nothing_is_pending(
        self, tmp_path, monkeypatch
    ):
        """Session init is the hot path and the common case is zero requests."""
        _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}})
        calls: list[int] = []
        monkeypatch.setattr(
            chat_runner,
            "_connections_managed_mcp_names",
            lambda: calls.append(1) or frozenset(),
        )
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, _FakeProviderClient([]))
        assert slot.messages == []
        assert calls == []


class TestDrainEdgeCases:
    @pytest.mark.asyncio
    async def test_client_without_pending_buffer_is_a_noop(self):
        """A provider client with no ACP buffer (e.g. a non-ACP backend)."""
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(MagicMock(), slot, object())
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_non_dict_request_entry_is_skipped(self, tmp_path, monkeypatch):
        _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"}})
        slot = _ChatSlot("s1")
        await _drain_session_init_oauth_requests(
            MagicMock(), slot, _FakeProviderClient(["not-a-dict"])
        )
        assert slot.messages == []


class TestMidTurnRequestsAreNeverAnnotated:
    """The mid-turn EVENT_MCP_OAUTH_REQUEST path fires when a live token expires.

    The turn is already blocked on it and no card is watching, so it must reach
    every surface. Pinned at the emitter's default rather than through the event
    handler, because the default is what makes every existing call site safe.
    """

    def test_emit_defaults_to_unannotated(self):
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(MagicMock(), slot, "acme", LEGIT_OAUTH_URLS[0][1])
        assert "card_owned" not in slot.messages[0]["meta"]

    def test_annotation_is_opt_in_per_call(self):
        slot = _ChatSlot("s1")
        _emit_mcp_oauth_request(
            MagicMock(), slot, "acme", LEGIT_OAUTH_URLS[0][1], card_owned=True
        )
        assert slot.messages[0]["meta"]["card_owned"] is True


class TestConnectionsManagedMcpNames:
    """The predicate CONSUMES the two deciding facilities; it re-derives neither."""

    def test_intersects_store_ownership_with_carded_providers(self, tmp_path, monkeypatch):
        _own(
            tmp_path,
            monkeypatch,
            {
                CARDED_SLUG: {"url": "https://mcp.notion.com/mcp"},
                GATED_SLUG: {"url": "https://api.githubcopilot.com/mcp/"},
                "my-custom-remote": {"url": "https://mine.example.com/mcp"},
            },
        )
        assert _connections_managed_mcp_names() == frozenset({CARDED_SLUG})

    def test_ownership_discriminator_is_not_reimplemented(self, tmp_path, monkeypatch):
        """A malformed store value is not ownership — decided by the shared function.

        Pinned so the annotation can never drift into its own, laxer rule.
        """
        _own(tmp_path, monkeypatch, {CARDED_SLUG: "not-a-dict"})
        assert CARDED_SLUG not in mcp_discovery.kirocrew_managed_names()
        assert _connections_managed_mcp_names() == frozenset()

    def test_a_scope_we_do_not_own_confers_nothing(self):
        """A slug present only in a scope we do not own stays un-annotated."""
        with patch(
            "kiro_crew.mcp_discovery._load_mcp_json_by_source",
            return_value={
                mcp_discovery.SCOPE_KIROCREW: {},
                mcp_discovery.SCOPE_KIRO_GLOBAL: {CARDED_SLUG: {"url": "https://n"}},
            },
        ):
            assert _connections_managed_mcp_names() == frozenset()
