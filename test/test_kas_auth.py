"""Tests for the KAS auth callback helper.

The security-critical property under test is negative: a token must NEVER appear
in an exception message, a log, or the forwarded dict beyond the covenant keys.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.acp import kas_auth
from kiro_crew.acp.kas_auth import (
    KasAuthCallbackError,
    _parse_token_output,
    resolve_kas_access_token,
)

# A realistic-looking token value. Every failure-path test asserts this string
# never escapes into an exception message.
_FAKE_TOKEN = "aoaAbc123." + "X" * 200


def _line(kind: str, data: dict) -> bytes:
    return (json.dumps({"kind": kind, "data": data}) + "\n").encode()


class TestParsingHappyPath:
    def test_returns_all_present_covenant_keys(self):
        out = _line(
            "getKasToken",
            {
                "accessToken": _FAKE_TOKEN,
                "expiresAt": "2026-08-14T00:12:59Z",
                "profileArn": "arn:aws:codewhisperer:us-east-1:123:profile/X",
                "authMethod": "external_idp",
                "provider": "ExternalIdp",
            },
        )
        result = _parse_token_output(out, b"")
        assert result == {
            "accessToken": _FAKE_TOKEN,
            "expiresAt": "2026-08-14T00:12:59Z",
            "profileArn": "arn:aws:codewhisperer:us-east-1:123:profile/X",
            "authMethod": "external_idp",
            "provider": "ExternalIdp",
        }

    def test_omits_absent_optional_keys(self):
        # Mirrors the real observed output: authMethod null → absent on the wire.
        out = _line(
            "getKasToken",
            {
                "accessToken": _FAKE_TOKEN,
                "expiresAt": "2026-08-14T00:12:59Z",
                "profileArn": "arn:aws:codewhisperer:us-east-1:123:profile/X",
                "provider": "Internal",
            },
        )
        result = _parse_token_output(out, b"")
        assert set(result) == {"accessToken", "expiresAt", "profileArn", "provider"}
        assert "authMethod" not in result

    def test_drops_unexpected_keys(self):
        out = _line(
            "getKasToken",
            {
                "accessToken": _FAKE_TOKEN,
                "expiresAt": "2026-08-14T00:12:59Z",
                "refreshToken": "SHOULD-NOT-FORWARD",
                "surprise": "nope",
            },
        )
        result = _parse_token_output(out, b"")
        assert set(result) == {"accessToken", "expiresAt"}

    def test_uses_last_line_only(self):
        out = b"some leading log noise\n" + _line(
            "getKasToken", {"accessToken": _FAKE_TOKEN, "expiresAt": "2026-08-14T00:12:59Z"}
        )
        result = _parse_token_output(out, b"")
        assert result["accessToken"] == _FAKE_TOKEN


class TestFailurePathsDoNotLeak:
    def test_non_json_output_is_not_echoed(self):
        # A malformed line could itself be a live token with trailing junk.
        junk = _FAKE_TOKEN + " <not json>"
        with pytest.raises(KasAuthCallbackError) as ei:
            _parse_token_output(junk.encode(), b"")
        assert _FAKE_TOKEN not in str(ei.value)

    def test_missing_access_token_raises(self):
        out = _line("getKasToken", {"expiresAt": "2026-08-14T00:12:59Z"})
        with pytest.raises(KasAuthCallbackError, match="missing accessToken"):
            _parse_token_output(out, b"")

    def test_unexpected_kind_raises(self):
        out = _line("somethingElse", {"accessToken": _FAKE_TOKEN})
        with pytest.raises(KasAuthCallbackError) as ei:
            _parse_token_output(out, b"")
        assert _FAKE_TOKEN not in str(ei.value)

    def test_error_kind_uses_a_fixed_generic_message(self):
        # The subprocess-provided error text is NOT surfaced (it is untrusted
        # and could carry sensitive content); a fixed, actionable message is
        # raised instead.
        out = _line("error", {"message": f"boom token={_FAKE_TOKEN}"})
        with pytest.raises(KasAuthCallbackError, match="kiro-cli login") as ei:
            _parse_token_output(out, b"")
        assert _FAKE_TOKEN not in str(ei.value)
        assert "boom" not in str(ei.value)

    def test_empty_output_raises(self):
        with pytest.raises(KasAuthCallbackError, match="no token output"):
            _parse_token_output(b"", b"")

    def test_non_object_json_raises(self):
        with pytest.raises(KasAuthCallbackError):
            _parse_token_output(b"[1,2,3]\n", b"")

    def test_stderr_is_not_echoed_when_no_stdout(self):
        # stderr is untrusted; it must not be echoed at all (redaction is
        # best-effort, so suppression is the only guarantee).
        stderr = f"token={_FAKE_TOKEN}".encode()
        with pytest.raises(KasAuthCallbackError) as ei:
            _parse_token_output(b"", stderr)
        assert _FAKE_TOKEN not in str(ei.value)
        assert "token=" not in str(ei.value)


class TestResolveBinaryMissing:
    @pytest.mark.asyncio
    async def test_missing_binary_raises(self, monkeypatch):
        monkeypatch.setattr(kas_auth, "resolve_kiro_cli", lambda: None)
        with pytest.raises(KasAuthCallbackError, match="kiro-cli not found"):
            await resolve_kas_access_token()


class _HangingProc:
    """A fake subprocess whose communicate() never returns on its own."""

    def __init__(self):
        self.returncode = None
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(3600)  # never completes; caller must kill

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class TestSubprocessNeverOrphaned:
    """The get-kas-token subprocess must not outlive the callback."""

    @pytest.mark.asyncio
    async def test_timeout_kills_the_subprocess(self, monkeypatch):
        proc = _HangingProc()
        monkeypatch.setattr(kas_auth, "resolve_kiro_cli", lambda: "kiro-cli")

        async def _fake_exec(*a, **k):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        with pytest.raises(KasAuthCallbackError, match="timed out"):
            await resolve_kas_access_token(timeout=0.05)
        assert proc.killed is True

    @pytest.mark.asyncio
    async def test_cancellation_kills_the_subprocess(self, monkeypatch):
        proc = _HangingProc()
        monkeypatch.setattr(kas_auth, "resolve_kiro_cli", lambda: "kiro-cli")

        async def _fake_exec(*a, **k):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        task = asyncio.ensure_future(resolve_kas_access_token(timeout=100))
        await asyncio.sleep(0.05)  # let it reach communicate()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed is True
