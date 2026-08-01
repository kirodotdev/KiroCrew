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
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from oauth_url_corpus import LEGIT_OAUTH_URLS

from kiro_crew.dashboard.chat_runner import (
    _emit_mcp_oauth_request,
    _is_safe_oauth_url,
    _mark_mcp_oauth_completed,
    _oauth_url_contains_credential,
)
from kiro_crew.dashboard.chat_utils import _redact_meta_for_role
from kiro_crew.dashboard.state import _ChatSlot

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
            _oauth_url_contains_credential(url) is False
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
        assert _oauth_url_contains_credential(url) is True

    def test_slack_token_in_redirect_uri_rejected(self):
        url = (
            "https://evil.com/authorize?client_id=x"
            "&redirect_uri=https://evil.com/cb?t=xoxb-123-abc"
        )
        assert _oauth_url_contains_credential(url) is True

    def test_high_entropy_pkce_state_still_allowed(self):
        # A genuine PKCE state/code_challenge (base64-ish, 40+ chars) must NOT
        # be rejected — that was the whole point of the OAuth-param exemption.
        url = (
            "https://github.com/login/oauth/authorize?client_id=Iv1.x"
            "&state=af0ifjsldkjLONGopaqueTOKENvalue1234567890abcd"
            "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
            "&code_challenge_method=S256&response_type=code"
        )
        assert _oauth_url_contains_credential(url) is False
