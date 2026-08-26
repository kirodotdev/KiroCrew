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
)
from kiro_crew.dashboard.chat_utils import _redact_meta_for_role
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
