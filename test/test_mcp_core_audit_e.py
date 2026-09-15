"""Regression tests for audit-E mcp_core bugs.

Bug 2 (spawn_sub_agents poll loop): the loop blocked the tool worker until
every sub-agent settled or max_wait elapsed, ignoring notifications/cancelled.
It must check ``is_tool_cancelled()`` on each iteration — like ``wait`` does —
and raise ``ToolCancelled`` when cancelled.

Bug 3 (file_send session resolution): ``_current_session_thread_ts()`` globbed
every ``session_pid_*.txt`` and used the newest by mtime — i.e. an arbitrary,
frequently DIFFERENT session in a multi-session gateway — which misrouted the
Slack upload to another session's thread. It must resolve the CALLER's own
session key via ``_resolve_session_key`` (env var / hardened
``read_session_pid_txt``).
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

import kiro_crew.mcp_core as mcp_core
import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_core import _call_tool, _current_session_thread_ts
from kiro_crew.mcp_shared import ToolCancelled


class TestSpawnSubAgentsCancellation:
    def test_poll_loop_honors_cancellation(self):
        """Failure scenario: sub-agents never finish, but a cancel arrives —
        the loop must raise ToolCancelled instead of blocking until max_wait."""
        evt = threading.Event()
        evt.set()  # cancel already signalled before the first poll iteration
        mcp_shared._thread_cancel_event = evt
        try:
            with patch("kiro_crew.mcp_core._post") as mock_post, \
                 patch("kiro_crew.mcp_core._get") as mock_get, \
                 patch("kiro_crew.mcp_core.sel"), \
                 patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
                mock_post.return_value = {"id": "a1"}
                mock_get.return_value = {"done": False, "agent": "slow"}

                with pytest.raises(ToolCancelled):
                    _call_tool(
                        "spawn_sub_agents",
                        {"agents": [{"prompt": "never finishes"}]},
                    )
        finally:
            mcp_shared._thread_cancel_event = None

    def test_not_cancelled_completes_normally(self):
        """Control: with no cancel set, a done agent still collects results."""
        mcp_shared._thread_cancel_event = None
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}
            result = _call_tool(
                "spawn_sub_agents", {"agents": [{"prompt": "quick"}]}
            )
            assert '"completed"' in result


class TestFileSendCallerSessionResolution:
    def test_uses_caller_slack_thread_ts(self, monkeypatch):
        # Outward-facing send resolves via the STRICT resolver (env var or
        # HMAC-verified host-pid only) — never the lenient/forgeable path.
        # An already-bare legacy Slack thread_ts passes through unchanged.
        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "1710000000.001"
        )
        assert _current_session_thread_ts() == "1710000000.001"

    def test_canonical_slack_key_converts_to_bare_thread_ts(self, monkeypatch):
        """A canonical ``slack:<ts>`` key must be converted to the BARE
        ``thread_ts`` the ``/api/slack/upload-file`` API expects — otherwise
        the namespaced key flows verbatim as ``thread_ts`` and Slack uploads
        from canonical-key Slack sessions break.
        """
        monkeypatch.setattr(
            mcp_core,
            "_resolve_session_key_strict",
            lambda: "slack:1783733803.877979",
        )
        assert _current_session_thread_ts() == "1783733803.877979"

    def test_dashboard_session_has_no_thread_ts(self, monkeypatch):
        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1"
        )
        assert _current_session_thread_ts() is None

    def test_non_slack_namespaces_return_none(self, monkeypatch):
        """Future-namespace guard: the resolver is an ALLOW-LIST, not an
        exclude-one-prefix filter. Any namespace that is not a Slack thread —
        ``discord:``, app/channel keys, or an unknown future namespace — must
        return ``None`` (threadless upload) instead of being misread as a bare
        Slack ``thread_ts`` on the outward-facing upload path.
        """
        for key in (
            "discord:1234567890",
            "channel:C01234567",
            "telegram:987654321",
            "some-future-namespace:whatever",
        ):
            monkeypatch.setattr(
                mcp_core, "_resolve_session_key_strict", lambda k=key: k
            )
            assert _current_session_thread_ts() is None, key

    def test_unresolvable_session_returns_none(self, monkeypatch):
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        assert _current_session_thread_ts() is None

    def test_fails_closed_when_only_lenient_resolves(self, monkeypatch):
        """A forged/PID-walked identity that the lenient resolver would accept
        must NOT drive the outward Slack upload. When strict resolution is
        unavailable we fail closed to a threadless upload (``None``), even if
        the lenient resolver would have returned a key.
        """
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "forged.9999")
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        assert _current_session_thread_ts() is None

    def test_does_not_consult_session_pid_files(self, monkeypatch):
        """Failure scenario: another session's pid file could be the newest on
        disk. Pre-fix the function globbed ``session_pid_*.txt`` and returned
        that other session's thread_ts. Now it derives strictly from the
        caller's resolved key and must NEVER touch ``Path.home()`` — so a decoy
        pid file can't misroute the upload. Fail loudly if the glob path runs.
        """
        def _boom():
            raise AssertionError("Path.home() must not be consulted anymore")

        monkeypatch.setattr(mcp_core.Path, "home", staticmethod(_boom))
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "1111.2222")
        assert _current_session_thread_ts() == "1111.2222"


def _slack_upload_calls(mock_post):
    """Return the list of (body,) recorded for POSTs to /api/slack/upload-file."""
    return [
        c.args[1] if len(c.args) > 1 else (c.kwargs.get("body") or c.args[0])
        for c in mock_post.call_args_list
        if c.args and c.args[0] == "/api/slack/upload-file"
    ]


class TestFileSendThreeStateIdentity:
    """Bug 3, security dimension (GPT 5.6 HIGH / Arbiter item 1).

    ``_current_session_thread_ts()`` returned ``None`` for BOTH "not a Slack
    session" and "Slack identity UNRESOLVED". Collapsing those two is a
    channel-root disclosure: an unresolved caller (e.g. a warm-pool-claimed
    Slack session) that supplies an explicit tracked channel would upload at the
    CHANNEL ROOT (``thread_ts=None`` + channel), exposing a file meant for one
    thread to the whole channel. ``file_send`` now uses the three-state
    :func:`_classify_slack_identity` and FAILS CLOSED (refuses the Slack upload)
    when identity is unresolved, while resolved non-Slack sessions keep their
    authorized routing.
    """

    def _run_file_send(self, monkeypatch, tmp_path, strict_key, channel=None):
        """Invoke file_send with a controlled strict identity; return the
        recorded ``_post`` mock so callers can inspect the Slack upload path."""
        from unittest.mock import patch

        f = tmp_path / "report.txt"
        f.write_text("hello world", encoding="utf-8")
        args = {"path": str(f), "description": "a report"}
        if channel is not None:
            args["channel"] = channel

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: strict_key)
        with patch.object(mcp_core, "_post") as mock_post, \
             patch.object(mcp_core, "outbox_dir", return_value=tmp_path), \
             patch.object(mcp_core, "sel"):
            mock_post.return_value = {}
            result = mcp_core._call_tool_inner("file_send", args)
        return mock_post, result

    def test_unresolved_with_channel_refuses_upload(self, monkeypatch, tmp_path):
        """(a) identity unresolved + explicit channel -> Slack upload REFUSED
        (no channel-root post). This is the exact GPT HIGH disclosure path."""
        mock_post, result = self._run_file_send(
            monkeypatch, tmp_path, strict_key="", channel="C0TRACKED123"
        )
        assert _slack_upload_calls(mock_post) == [], (
            "unresolved identity + explicit channel must NOT reach "
            "/api/slack/upload-file (would broadcast at channel root)"
        )
        assert "skipped" in result.lower() or "refused" in result.lower()

    def test_unresolved_without_channel_refuses_upload(self, monkeypatch, tmp_path):
        """Unresolved identity with no channel is also refused — we cannot
        attribute the caller, so no outward Slack send is attempted."""
        mock_post, result = self._run_file_send(monkeypatch, tmp_path, strict_key="")
        assert _slack_upload_calls(mock_post) == []

    def test_non_slack_session_does_not_fabricate_thread(self, monkeypatch, tmp_path):
        """(b) A RESOLVED non-Slack session (dashboard:) never fabricates a
        Slack thread_ts and never broadcasts to a channel root. With no explicit
        channel the upload carries ``thread_ts=None`` and no channel, so the
        handler applies its OWN authorized routing (owner DM / session-map
        linked thread) — it can never post at a channel root for this caller."""
        mock_post, _ = self._run_file_send(
            monkeypatch, tmp_path, strict_key="dashboard:chat-1"
        )
        calls = _slack_upload_calls(mock_post)
        assert len(calls) == 1
        assert calls[0]["thread_ts"] is None
        assert calls[0]["channel"] == ""

    def test_resolved_slack_key_uploads_threaded(self, monkeypatch, tmp_path):
        """(c) A resolved canonical ``slack:<ts>`` key uploads to the caller's
        bare thread_ts."""
        mock_post, _ = self._run_file_send(
            monkeypatch, tmp_path, strict_key="slack:1783733803.877979"
        )
        calls = _slack_upload_calls(mock_post)
        assert len(calls) == 1
        assert calls[0]["thread_ts"] == "1783733803.877979"

    def test_non_slack_with_explicit_channel_uploads_threadless(self, monkeypatch, tmp_path):
        """(d) Explicitly-requested threadless delivery still works: a resolved
        non-Slack session that names an explicit channel uploads threadless to
        that channel (the handler authorizes it against the tracked-channel
        list). This is the legitimate counterpart the fix must NOT block."""
        mock_post, _ = self._run_file_send(
            monkeypatch, tmp_path, strict_key="dashboard:chat-1", channel="C0TRACKED123"
        )
        calls = _slack_upload_calls(mock_post)
        assert len(calls) == 1
        assert calls[0]["thread_ts"] is None
        assert calls[0]["channel"] == "C0TRACKED123"
