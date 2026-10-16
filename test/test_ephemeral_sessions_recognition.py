"""Tests for the session-recognition gate on the lessons and memory write routes.

The gate decides whether an ``X-Session-Key`` names a real session and whether
that session may change durable memory: live slots, channel and cron keys,
evicted slots recovered from a persisted transcript, archived incognito and
temporary transcripts, durable Slack privacy flags, and forged or
path-traversal keys. Also covered: the persisted-transcript probes behind the
gate and the ``unknown_session`` code that ``learn_remove`` dispatches on. The
state and app builders live in ``ephemeral_sessions_helpers.py``.
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from ephemeral_sessions_helpers import (
    _make_app,
    _make_state,
)

# ── Helpers ──


def _owner_dashboard_identity():
    """The owner's dashboard claims, for the routes the owner gate covers.

    ``app == ""`` with the owner's subject: ``state.owner_id`` when one is
    configured, else the signed local bootstrap subject.
    """
    from aiohttp import web

    @web.middleware
    async def middleware(request, handler):
        state = request.app.get("state")
        request["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        request["app"] = ""
        return await handler(request)

    return middleware


# ── Session slot recovery via persisted JSONL ──


class TestSessionSlotRecovery:
    """learn_add must accept keys whose slot was evicted from memory but whose
    JSONL file still exists in the data home's sessions/ — this covers the
    long-lived Slack thread / reopened dashboard tab cases where the MCP
    subprocess holds a stale KIROCREW_SESSION_KEY env var that maps to a swept
    slot.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _persisted_history_dir_tracks_patched_home(self, monkeypatch):
        """Point ``_shared.config_dir()`` at ``<patched home>/.kirocrew``.

        The data home moved from ``~/.kirocrew`` to ``~/.kiro/crew``
        (``config_dir()``); ``_session_has_persisted_history`` now probes
        ``config_dir()/sessions`` rather than ``Path.home()/".kirocrew"/"sessions"``.
        Each test here patches ``Path.home()`` and seeds JSONL under
        ``<home>/.kirocrew/sessions``, but ``config_dir()`` reads ``KIROCREW_HOME``
        (a different tmp dir pinned by conftest), so redirect ``_shared``'s
        ``config_dir`` to ``Path.home()/".kirocrew"`` (lazy, tracks the per-test
        ``Path.home`` patch) to keep the seeded layout authoritative. Applied
        first so a test's own patches still win.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.config_dir",
            lambda: Path.home() / ".kirocrew",
        )

    def _write_sessions_jsonl(self, tmp_path, stem: str) -> None:
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / f"{stem}.jsonl").write_text(
            '{"_type": "metadata", "created_at": "2026-01-01T00:00:00"}\n',
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_learn_add_allowed_when_slot_evicted_but_jsonl_persists(
        self, tmp_path, monkeypatch
    ):
        """Core fix: evicted slot + existing JSONL → learn_add proceeds."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Slot was evicted — state._slots is empty — but JSONL exists.
        self._write_sessions_jsonl(tmp_path, "1776000000.123456")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:1776000000.123456"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_when_slot_evicted_dashboard_prefix_jsonl(
        self, tmp_path, monkeypatch
    ):
        """dashboard_{stem}.jsonl fallback path from slack/interactions.py."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        self._write_sessions_jsonl(tmp_path, "dashboard_chat-1-1776000000")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:chat-1-1776000000"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_evicted_cron_session_underscore_jsonl(
        self, tmp_path, monkeypatch
    ):
        """A cron session keys off ``cron:{id}`` and persists its transcript as
        ``cron_{id}.jsonl`` (``_safe_key`` maps ``:`` → ``_``). The resolver must
        probe that name so an idle-evicted-but-real cron's learn_add succeeds."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # cron:abc123 → _safe_key → cron_abc123.jsonl on disk; slot evicted.
        self._write_sessions_jsonl(tmp_path, "cron_abc123")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "cron:abc123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_evicted_cron_session_dashboard_jsonl(
        self, tmp_path, monkeypatch
    ):
        """A cron's linked dashboard slot keys off ``dashboard:cron-{id}`` and
        persists as ``dashboard_cron-{id}.jsonl``; probing that name also
        recovers the evicted cron session for learn_add."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # dashboard:cron-abc123 → _safe_key → dashboard_cron-abc123.jsonl
        self._write_sessions_jsonl(tmp_path, "dashboard_cron-abc123")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "cron:abc123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_rejects_forged_cron_key_without_jsonl(self, tmp_path, monkeypatch):
        """Regression guard: a ``cron:`` key with no backing JSONL is still
        rejected. The cron probes only ADD positive matches — they must not
        relax the deny path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Empty sessions dir so the path-exists check is meaningful.
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "cron:forged123"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"

    @pytest.mark.asyncio
    async def test_learn_add_rejects_path_traversal_in_cron_slot_name(self, tmp_path, monkeypatch):
        """AC #4: the path-traversal guard still runs first on the slot_name
        even for a ``cron:``-prefixed key, so a traversal attempt is rejected
        even when a file exists at the resolved target."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        # Seed files at each resolved traversal target so the guard — NOT the
        # missing-file fallback — is what rejects each request.
        # "../escape" → sess_dir/../escape.jsonl → ~/.kirocrew/escape.jsonl
        (tmp_path / ".kirocrew" / "escape.jsonl").write_text("{}\n")
        # "a/b" → sess_dir/a/b.jsonl
        sub = sess_dir / "a"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "b.jsonl").write_text("{}\n")
        # "a\\b" (Windows separator) → literal single-name file on Linux.
        (sess_dir / "a\\b.jsonl").write_text("{}\n")

        async with TestClient(TestServer(_make_app(state))) as client:
            for bad_key in (
                "cron:../escape",
                "cron:a/b",
                "cron:a\\b",
            ):
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": bad_key},
                )
                assert resp.status == 400, f"cron path-traversal passed: {bad_key!r}"

    @pytest.mark.asyncio
    async def test_learn_add_still_rejected_when_no_jsonl_exists(self, tmp_path, monkeypatch):
        """Forged/stale keys with no backing JSONL are still rejected as unknown."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Create an empty sessions dir so the path-exists check is meaningful.
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:forged-key"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"

    @pytest.mark.asyncio
    async def test_learn_add_rejects_path_traversal_in_slot_name(self, tmp_path, monkeypatch):
        """Defence-in-depth: slot names with path separators, null bytes, or
        leading dots are rejected even when a matching file happens to exist
        at the resolved traversal target. This proves the guard itself blocks
        the request — without creating the target files, the test would pass
        even if the guard were removed because ``Path.exists()`` would return
        ``False`` for the missing file."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)

        # Seed files at every resolved traversal target so that the guard —
        # NOT the missing-file fallback — is what rejects each request.
        # "../escape" → sess_dir/../escape.jsonl → ~/.kirocrew/escape.jsonl
        (tmp_path / ".kirocrew" / "escape.jsonl").write_text("{}\n")
        # ".hidden" → sess_dir/.hidden.jsonl
        (sess_dir / ".hidden.jsonl").write_text("{}\n")
        # "a/b" → sess_dir/a/b.jsonl
        sub = sess_dir / "a"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "b.jsonl").write_text("{}\n")
        # "a\\b" (Windows path separator) → nominally sess_dir/a\b.jsonl.
        # Create a literal single-filename entry with an embedded backslash
        # so that, on Linux, a JSONL with that exact name exists — proving
        # the guard rejects backslash independent of platform behaviour.
        (sess_dir / "a\\b.jsonl").write_text("{}\n")

        async with TestClient(TestServer(_make_app(state))) as client:
            # These keys must be *rejected* — either by the server guard
            # (status 400) or by aiohttp's own header validation before the
            # request ever reaches the server (ValueError on newline / CR
            # / null byte). Both outcomes prove the traversal attempt is
            # blocked end-to-end.
            for bad_key in (
                "dashboard:../escape",
                "dashboard:.hidden",
                "dashboard:a/b",
                "dashboard:a\\b",
            ):
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": bad_key},
                )
                assert resp.status == 400, f"path-traversal attempt passed: {bad_key!r}"

            # Null byte: blocked at transport level. Older aiohttp raises
            # ValueError client-side; newer versions reject at the HTTP parser
            # (ServerDisconnectedError or similar). Either way, the request
            # cannot reach the handler — verify it doesn't succeed.
            import aiohttp

            try:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:bad\x00key"},
                )
                assert resp.status == 400, "null byte header reached handler"
            except (ValueError, aiohttp.ServerDisconnectedError, aiohttp.ClientConnectionError):
                pass  # transport-level rejection — acceptable

    def test_session_has_persisted_history_unit(self, tmp_path, monkeypatch):
        """Direct unit test for the helper."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _session_has_persisted_history

        assert _session_has_persisted_history("1776000000.123456") is False
        assert _session_has_persisted_history("") is False
        assert _session_has_persisted_history("../escape") is False
        assert _session_has_persisted_history(".hidden") is False
        assert _session_has_persisted_history("a\\b") is False
        assert _session_has_persisted_history("bad\x00key") is False

        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "1776000000.123456.jsonl").write_text("{}\n")
        assert _session_has_persisted_history("1776000000.123456") is True

        # dashboard_ prefix fallback
        (sess_dir / "dashboard_chat-1.jsonl").write_text("{}\n")
        assert _session_has_persisted_history("chat-1") is True

        # Even if a file exists at a traversal-style path, the guard still
        # rejects it — this is what actually proves defence-in-depth.
        (sess_dir / ".hidden.jsonl").write_text("{}\n")
        assert _session_has_persisted_history(".hidden") is False
        (sess_dir / "a\\b.jsonl").write_text("{}\n")
        assert _session_has_persisted_history("a\\b") is False

    # ── Audit events on positive-match paths (security-controls rule) ──

    @pytest.mark.asyncio
    async def test_learn_add_audits_live_slot_allow_path(self, tmp_path, monkeypatch):
        """Live in-memory slot → audit event with resources='live_slot'."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Seed a live slot so in_slots=True on the guard.
        state.get_or_create_slot("live1")

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "remember this", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:live1"},
                )
                assert resp.status == 200

        mock_sel().log_api_access.assert_any_call(
            caller="dashboard:live1",
            operation="learn_add",
            outcome="allowed",
            source="dashboard",
            resources="live_slot",
        )

    @pytest.mark.asyncio
    async def test_learn_add_audits_restricted_key_allow_path(self, tmp_path, monkeypatch):
        """Key present in _restricted_keys → audit event with resources='restricted_key'.

        Restricted keys are blocked *later* in the handler by the
        ``_is_restricted_session`` guard (403), but the session-scope check
        itself permits them through — that positive-match decision must be
        audited for the security-controls rule even though the downstream
        write is denied.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Populate _restricted_keys without a live slot so in_slots=False
        # and in_restricted=True on the guard.
        state._restricted_keys.add("dashboard:r1")

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:r1"},
                )
                # Downstream _is_restricted_session still blocks the write
                # with 403, but the session-scope allow decision fires first.
                assert resp.status in (200, 403)

        mock_sel().log_api_access.assert_any_call(
            caller="dashboard:r1",
            operation="learn_add",
            outcome="allowed",
            source="dashboard",
            resources="restricted_key",
        )

    @pytest.mark.asyncio
    async def test_learn_add_audits_channel_namespace_allow_path(self, tmp_path, monkeypatch):
        """Key in a channel namespace (here ``slack:``) → audit event with
        resources='channel_namespace' (the tag now covers every channel, not
        just Slack)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "slack:C123:1777000000.000000"},
                )
                assert resp.status == 200

        mock_sel().log_api_access.assert_any_call(
            caller="slack:C123:1777000000.000000",
            operation="learn_add",
            outcome="allowed",
            source="dashboard",
            resources="channel_namespace",
        )

    @pytest.mark.asyncio
    async def test_learn_add_audits_dashboard_ui_allow_path(self, tmp_path, monkeypatch):
        """Browser UI's static ``dashboard:ui`` key → audit event with
        resources='dashboard_ui'. This key bypasses the slot-scope block
        entirely; the allow decision still needs its own SEL event.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:ui"},
                )
                assert resp.status == 200

        mock_sel().log_api_access.assert_any_call(
            caller="dashboard:ui",
            operation="learn_add",
            outcome="allowed",
            source="dashboard",
            resources="dashboard_ui",
        )

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_bare_slack_thread_ts_without_jsonl(
        self, tmp_path, monkeypatch
    ):
        """Regression: a Slack thread keys its session off the
        bare ``thread_ts`` (e.g. ``1781215864.487849``), not a ``slack:``
        prefix. The first ``learn_add`` in a fresh thread arrives *before*
        the session JSONL is flushed (the transcript is written only after
        the LLM turn completes), so without recognising the bare-ts shape it
        would race the flush and 400 with ``unknown session``. The key must be
        allowed via the Slack namespace even with an empty sessions dir.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Empty sessions dir: no JSONL fallback is possible, so the allow can
        # only come from the bare-thread_ts namespace recognition.
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "remember this", "category": "knowledge"},
                    headers={"X-Session-Key": "1781215864.487849"},
                )
                assert resp.status == 200

        # Audited as a channel-namespace allow (bare Slack thread_ts), not a JSONL recovery.
        mock_sel().log_api_access.assert_any_call(
            caller="1781215864.487849",
            operation="learn_add",
            outcome="allowed",
            source="dashboard",
            resources="channel_namespace",
        )

    @pytest.mark.asyncio
    async def test_learn_add_rejects_non_slack_bare_numeric_key(self, tmp_path, monkeypatch):
        """The bare-ts heuristic must not widen authorization: a numeric key
        that does not match the Slack ``thread_ts`` shape (too-short
        sub-second component) with no backing JSONL is still ``unknown
        session``. Guards against the regex matching arbitrary ``N.M`` keys.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "123.45"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"

    @pytest.mark.asyncio
    async def test_learn_add_rejects_unicode_digit_thread_ts_lookalike(self, tmp_path, monkeypatch):
        """Security: the Slack-ts regex gates an authorization decision, so it
        must match ASCII digits only. A key built from non-ASCII Unicode
        decimal digits (Arabic-Indic) that is otherwise thread_ts-shaped must
        NOT be granted channel_namespace access — `[0-9]` (not `\\d`) enforces
        this. With no backing JSONL the call is rejected as unknown session.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        # Arabic-Indic digits forming "١٧٨١٢١٥٨٦٤.٤٨٧٨٤٩" — \d would match this,
        # [0-9] does not.
        unicode_ts = "١٧٨١٢١٥٨٦٤.٤٨٧٨٤٩"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": unicode_ts},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"


class TestArchivedRestrictedSessionRecovery:
    """An archived incognito/temporary tab must stay restricted.

    ``api_chat_slot_close`` drops the slot from ``state._slots`` AND discards
    its ``state._restricted_keys`` entry, while ``_save_slot_to_history``
    writes the transcript — including its ``memory_mode`` marker — to disk. A
    still-live MCP subprocess keeps sending the original session key, so both
    in-memory signals miss and the gate must fall back to the persisted mode.
    Without that fallback the establish-session probe (which only tests file
    EXISTENCE) reads the archived transcript as proof of an ordinary session
    and memory writes are allowed.
    """

    @staticmethod
    def _archive(tmp_path, slot_name, mode):
        """Write the JSONL an archived session in *mode* leaves behind."""
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        path = sess_dir / f"dashboard_{slot_name}.jsonl"
        meta = {"_type": "metadata", "created_at": "2026-01-01T00:00:00", "closed": True}
        if mode != "persistent":
            meta["memory_mode"] = mode
        path.write_text(
            _json.dumps(meta)
            + "\n"
            + _json.dumps({"role": "user", "content": "secret", "ts": "2026-01-01T00:00:01"})
            + "\n",
            encoding="utf-8",
        )
        return path

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    @pytest.mark.asyncio
    async def test_learn_add_denied_for_archived_restricted_session(
        self, tmp_path, monkeypatch, mode
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _session_has_persisted_history

        state = _make_state(tmp_path)
        self._archive(tmp_path, "e1", mode)
        # Preconditions: neither in-memory signal survives the archive, and the
        # establish-session probe DOES accept the key — so the only thing that
        # can deny the write is the persisted-mode fallback. Asserting this
        # keeps the test from passing for the wrong reason (a 400 "unknown
        # session") if the probe's path resolution ever changes.
        assert "e1" not in state._slots
        assert "dashboard:e1" not in state._restricted_keys
        assert _session_has_persisted_history("e1") is True

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked from ephemeral session", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:e1"},
            )
            assert resp.status == 403, await resp.text()
            assert "not allowed" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_learn_add_still_allowed_for_archived_persistent_session(
        self, tmp_path, monkeypatch
    ):
        """The recovery path this rides on must keep working for normal sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        self._archive(tmp_path, "p1", "persistent")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "legitimate lesson", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:p1"},
            )
            assert resp.status == 200, await resp.text()

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("incognito", "incognito"), ("temporary", "temporary"), ("persistent", "persistent")],
    )
    def test_persisted_memory_mode_reader(self, tmp_path, monkeypatch, mode, expected):
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode

        self._archive(tmp_path, "s1", mode)
        assert _persisted_session_memory_mode("s1") == expected

    def test_persisted_memory_mode_unknown_is_none_not_persistent(self, tmp_path, monkeypatch):
        """Unreadable metadata reads as None (unknown) — never as a mode.

        A valid header that merely LACKS ``memory_mode`` is a legacy persistent
        session and must read as ``persistent``; anything unparseable must read
        as ``None`` so the write gate fails closed instead of allowing.
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode

        assert _persisted_session_memory_mode("missing") is None
        assert _persisted_session_memory_mode("../escape") is None
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_corrupt.jsonl").write_text("not json\n{{\n", encoding="utf-8")
        assert _persisted_session_memory_mode("corrupt") is None
        # A non-string mode must not be coerced into a truthy value.
        (sess_dir / "dashboard_weird.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": 42}) + "\n", encoding="utf-8"
        )
        assert _persisted_session_memory_mode("weird") is None
        # Legacy header without the field -> persistent (writes stay allowed).
        (sess_dir / "dashboard_legacy.jsonl").write_text(
            _json.dumps({"_type": "metadata", "created_at": "2026-01-01T00:00:00"}) + "\n",
            encoding="utf-8",
        )
        assert _persisted_session_memory_mode("legacy") == "persistent"
        # A metadata object that is NOT the first line must not define the mode:
        # append() writes the header first, so a later one is message content.
        (sess_dir / "dashboard_late.jsonl").write_text(
            _json.dumps({"role": "user", "content": "hi"})
            + "\n"
            + _json.dumps({"_type": "metadata", "memory_mode": "persistent"})
            + "\n",
            encoding="utf-8",
        )
        assert _persisted_session_memory_mode("late") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "incognito ",
            " incognito",
            "\tincognito\n",
            "INCOGNITO",
            "Incognito",
            "temporary ",
            "TEMPORARY",
        ],
    )
    def test_whitespace_or_case_variant_modes_do_not_fail_open(self, tmp_path, monkeypatch, raw):
        """A restricted mode must not slip through on casing/whitespace.

        The downstream comparison is set membership against
        INCOGNITO_MEMORY_MODES, so `"incognito "` would lower() to itself, miss
        the set, and read as unrestricted. Every variant must normalize to the
        restricted mode (never to None, which would also be wrong here: the
        header IS parseable and DOES name a restricted mode).
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode
        from kiro_crew.history import INCOGNITO_MEMORY_MODES

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_v1.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": raw}) + "\n", encoding="utf-8"
        )
        got = _persisted_session_memory_mode("v1")
        assert got == raw.strip().lower()
        assert got in INCOGNITO_MEMORY_MODES, f"{raw!r} escaped the restricted set as {got!r}"

    @pytest.mark.parametrize("raw", ["", "  ", "bogus", "persistent-ish", "incognito2"])
    def test_unrecognized_mode_reads_as_unknown(self, tmp_path, monkeypatch, raw):
        """An unrecognised value is unknown (None), not silently permissive."""
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_v2.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": raw}) + "\n", encoding="utf-8"
        )
        assert _persisted_session_memory_mode("v2") is None

    @pytest.mark.asyncio
    async def test_learn_add_denied_for_whitespace_bearing_incognito(self, tmp_path, monkeypatch):
        """End-to-end: the padded value must still produce a 403, not a 200."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_pad.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": "incognito "}) + "\n",
            encoding="utf-8",
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked via a padded mode value", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:pad"},
            )
            assert resp.status == 403, await resp.text()

    @pytest.mark.asyncio
    async def test_ambiguous_stem_denies_instead_of_picking_a_winner(self, tmp_path, monkeypatch):
        """Two transcripts can claim one stem — the gate must not guess.

        ``slot_name`` arrives with its transport namespace stripped
        (``sk.split(":", 1)[-1]``), so a legacy Slack transcript at
        ``<ts>.jsonl`` and an archived dashboard slot named after that same ts at
        ``dashboard_<ts>.jsonl`` both match. Taking the first candidate lets the
        PERSISTENT Slack file answer for the INCOGNITO dashboard session and the
        lesson is stored. Existence stays true; the mode must read unknown.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import (
            _persisted_session_memory_mode,
            _probe_persisted_session,
        )

        ts = "1785861252.833429"
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        # Bare stem = legacy Slack transcript, persistent. Probed FIRST.
        (sess_dir / f"{ts}.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": "persistent"}) + "\n",
            encoding="utf-8",
        )
        # Same stem under the dashboard prefix = archived INCOGNITO slot.
        (sess_dir / f"dashboard_{ts}.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": "incognito"}) + "\n",
            encoding="utf-8",
        )
        # First-match would report "persistent" here; ambiguity must win.
        assert _persisted_session_memory_mode(ts) == "persistent"  # first-match, unsafe alone
        exists, mode = _probe_persisted_session(ts)
        assert exists is True, "the session does exist — only the mode is unknown"
        assert mode is None, "ambiguous stem must not resolve to a mode"

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked via an ambiguous stem", "category": "knowledge"},
                headers={"X-Session-Key": f"dashboard:{ts}"},
            )
            assert resp.status == 403, await resp.text()

    def test_colon_slot_name_cannot_escape_sessions_dir(self, tmp_path, monkeypatch):
        """A colon is rejected: on Windows it yields a drive-relative escape.

        ``WindowsPath('.../sessions') / 'D:foo.jsonl'`` evaluates to
        ``D:foo.jsonl``, outside the sessions directory entirely (POSIX joins it
        literally and is unaffected), and it also spells an NTFS alternate data
        stream.

        The rejection is name-based and happens before any filesystem access, so
        the assertion below is meaningful on every platform. The *plant* is
        POSIX-only on purpose: on Windows the very path expression under test
        would write to another drive — i.e. outside ``tmp_path`` — which is
        exactly the escape being guarded against (and a colon is not a legal
        NTFS filename character anyway, so the file could not be created there).
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import (
            _persisted_session_memory_mode,
            _persisted_session_path,
            _session_has_persisted_history,
        )

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            # Plant a real file at the literal POSIX name, so the guard — not a
            # missing file — is provably what rejects it.
            (sess_dir / "D:foo.jsonl").write_text(
                _json.dumps({"_type": "metadata", "memory_mode": "persistent"}) + "\n",
                encoding="utf-8",
            )
            assert (sess_dir / "D:foo.jsonl").exists()
        for hostile in ("D:foo", "C:evil", "file:stream"):
            assert _persisted_session_path(hostile) is None, hostile
            assert _session_has_persisted_history(hostile) is False, hostile
            assert _persisted_session_memory_mode(hostile) is None, hostile


class TestDurableSlackFlagsAtHttpGate:
    """The HTTP gate must honour a Slack thread's DURABLE privacy flag.

    ``_thread_incognito``/``_thread_temporary`` are process-local and are only
    populated by ``_hydrate_conv_flags`` on an INBOUND Slack message. A turn no
    inbound message drove — a cron with ``session="origin"``, a webhook-resumed
    session, a monitor/autonudge re-injection, a subagent — reaches the gate
    with empty maps after a gateway restart, even though the user's
    ``!incognito`` is on disk. The gate must restore before it decides.
    """

    SLACK_KEY = "slack:1785861252.833429"

    @pytest.fixture()
    def durable(self, tmp_path, monkeypatch):
        """A real SessionMap in tmp_path, with the in-memory LRUs emptied."""
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.slack import handler as _h

        _h._thread_temporary.clear()
        _h._thread_incognito.clear()
        yield
        _h._thread_temporary.clear()
        _h._thread_incognito.clear()

    @pytest.mark.parametrize("flag", ["incognito", "temporary"])
    @pytest.mark.asyncio
    async def test_learn_add_denied_for_durable_slack_flag(self, tmp_path, durable, flag):
        from kiro_crew.session_map import SessionMap
        from kiro_crew.slack import handler as _h

        sm = SessionMap()
        sm.set_flag(self.SLACK_KEY, flag, True)
        # A loop-side mutation defers its disk write; the fresh-instance
        # precondition below reads the FILE, so force it current first.
        sm.flush()
        # Preconditions: durable on disk, absent from this process's maps.
        assert SessionMap().get_flag(self.SLACK_KEY, flag) is True
        assert _h.is_thread_incognito(self.SLACK_KEY) is False
        assert _h.is_thread_temporary(self.SLACK_KEY) is False

        state = _make_state(tmp_path)
        state.sessions._session_map = sm

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked from a slack privacy thread", "category": "knowledge"},
                headers={"X-Session-Key": self.SLACK_KEY},
            )
            assert resp.status == 403, await resp.text()

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_unflagged_slack_thread(self, tmp_path, durable):
        """An ordinary Slack thread must stay writable — no over-blocking."""
        from unittest.mock import MagicMock as _MM

        from kiro_crew.session_map import SessionMap

        state = _make_state(tmp_path)
        state.sessions._session_map = SessionMap()
        with patch(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            _MM(return_value=_MM(vector_store=None)),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "legitimate slack lesson", "category": "knowledge"},
                    headers={"X-Session-Key": self.SLACK_KEY},
                )
                assert resp.status == 200, await resp.text()


# ── API: lessons DELETE session-recognition gate ──


class TestLessonsDeleteGate:
    """DELETE /api/lessons must apply the SAME session recognition as the
    create route. Before the gate, deleting was LESS protected than adding: a
    key that create rejects with 400 ``unknown session`` could still
    substring-delete any durable lesson, and a remove-then-re-add
    consolidation from such a session lost the lesson (destructive half
    succeeded, re-add refused). Delete-specific policy is preserved: incognito
    may delete (active user action); only temporary is blocked.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _persisted_history_dir_tracks_patched_home(self, monkeypatch):
        """Same redirect as TestSessionSlotRecovery: keep the seeded
        ``<home>/.kirocrew/sessions`` layout authoritative for the probe."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.config_dir",
            lambda: Path.home() / ".kirocrew",
        )

    def _write_sessions_jsonl(self, tmp_path, stem: str, *, memory_mode=None) -> None:
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        meta = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}
        if memory_mode:
            meta["memory_mode"] = memory_mode
        (sess_dir / f"{stem}.jsonl").write_text(_json.dumps(meta) + "\n", encoding="utf-8")

    def _deletable_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Route reads the vector store through cron_lessons.py's own binding.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        state.lessons.remove = MagicMock(return_value=True)
        return state

    @pytest.mark.asyncio
    async def test_delete_rejected_without_session_header(self, tmp_path, monkeypatch):
        state = self._deletable_state(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/lessons", json={"rule": "x"})
            assert resp.status == 400
            data = await resp.json()
            assert "X-Session-Key" in data["error"]
        state.lessons.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_rejected_for_unknown_session(self, tmp_path, monkeypatch):
        """Core regression: a forged/unknown key must NOT delete lessons."""
        state = self._deletable_state(tmp_path, monkeypatch)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:deleted-slot"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"
        state.lessons.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_rejects_forged_cron_key_without_jsonl(self, tmp_path, monkeypatch):
        state = self._deletable_state(tmp_path, monkeypatch)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "cron:forged123"},
            )
            assert resp.status == 400
        state.lessons.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_allowed_for_browser_ui(self, tmp_path, monkeypatch):
        state = self._deletable_state(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:ui"},
            )
            assert resp.status == 200
        state.lessons.remove.assert_called_once_with("x", None, exact=False)

    @pytest.mark.asyncio
    async def test_delete_allowed_for_live_persistent_slot(self, tmp_path, monkeypatch):
        state = self._deletable_state(tmp_path, monkeypatch)
        state.get_or_create_slot("n1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:n1"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_delete_blocked_for_live_incognito_slot(self, tmp_path, monkeypatch):
        """Session-caused persistent mutations include lesson deletion."""
        state = self._deletable_state(tmp_path, monkeypatch)
        state.get_or_create_slot("e1", memory_mode="incognito")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:e1"},
            )
            assert resp.status == 403
        state.lessons.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_blocked_for_live_temporary_slot(self, tmp_path, monkeypatch):
        state = self._deletable_state(tmp_path, monkeypatch)
        state.get_or_create_slot("t1", memory_mode="temporary")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:t1"},
            )
            assert resp.status == 403
        state.lessons.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_allowed_for_channel_namespace(self, tmp_path, monkeypatch):
        state = self._deletable_state(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "telegram:kirocrew:forum:-1004326574849:18:gen3"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_delete_allowed_for_evicted_session_with_persistent_jsonl(
        self, tmp_path, monkeypatch
    ):
        """Evicted-but-real session recovers via the persisted-JSONL probe —
        same as create — so a cron whose transcript has flushed can still
        remove lessons after a gateway restart."""
        state = self._deletable_state(tmp_path, monkeypatch)
        self._write_sessions_jsonl(tmp_path, "cron_abc123")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "cron:abc123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_delete_blocked_for_evicted_temporary_session_jsonl(self, tmp_path, monkeypatch):
        """Archived temporary session: the persisted memory_mode is the only
        remaining evidence; deletes are blocked to mirror the live-slot
        ``blocks_reads`` policy."""
        state = self._deletable_state(tmp_path, monkeypatch)
        self._write_sessions_jsonl(tmp_path, "dashboard_t9", memory_mode="temporary")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:t9"},
            )
            assert resp.status == 403
        state.lessons.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_blocked_for_evicted_incognito_session_jsonl(self, tmp_path, monkeypatch):
        """Retained metadata cannot loosen the session's mode."""
        state = self._deletable_state(tmp_path, monkeypatch)
        self._write_sessions_jsonl(tmp_path, "dashboard_e9", memory_mode="incognito")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:e9"},
            )
            assert resp.status == 403
        state.lessons.remove.assert_not_called()


class TestSharedRecognitionGate:
    """Create and delete must run through ONE shared recognition gate.

    ``_recognize_session`` is the single implementation of the slot /
    restricted-key / channel-namespace / persisted-JSONL cascade; these tests
    pin that both routes actually call it (so the cascades cannot silently
    diverge again) and that both refuse persistent mutations from restricted
    sessions; every gate refusal emits
    machine-readable codes.
    """

    @pytest.mark.asyncio
    async def test_create_and_delete_call_the_same_gate(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import cron as cron_handlers
        from kiro_crew.history import is_incognito_transcript

        calls: dict[str, dict] = {}

        async def _spy(state, sk, operation, **kwargs):
            calls[operation] = kwargs
            return web.json_response({"error": "gate refusal"}, status=400)

        monkeypatch.setattr(cron_handlers, "_recognize_session", _spy)
        state = _make_state(tmp_path)
        state.lessons.remove = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            create = await client.post(
                "/api/lessons",
                json={"rule": "x", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:whatever"},
            )
            delete = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:whatever"},
            )
        # Both routes were refused by the gate's response — proof they route
        # every request through the shared cascade, not a private copy.
        assert create.status == 400
        assert delete.status == 400
        state.lessons.remove.assert_not_called()
        assert set(calls) == {"learn_add", "lessons.delete"}
        # Both mutations use the same retention policy.
        assert calls["learn_add"]["blocks_persisted_mode"] is is_incognito_transcript
        delete_blocks = calls["lessons.delete"]["blocks_persisted_mode"]
        assert delete_blocks("temporary") is True
        assert delete_blocks("incognito") is True
        assert delete_blocks("persistent") is False

    @pytest.mark.asyncio
    async def test_delete_unknown_session_carries_machine_code(self, tmp_path, monkeypatch):
        """Every gate refusal carries ``code: unknown_session`` (the contract
        learn_remove dispatches on) — the per-route ``error_codes`` knob was
        dropped, so create's 400 carries it too."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            delete = await client.delete(
                "/api/lessons",
                json={"rule": "x"},
                headers={"X-Session-Key": "dashboard:deleted-slot"},
            )
            assert delete.status == 400
            data = await delete.json()
            assert data["error"] == "unknown session"
            assert data["code"] == "unknown_session"
            create = await client.post(
                "/api/lessons",
                json={"rule": "x", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:deleted-slot"},
            )
            assert create.status == 400
            data = await create.json()
            assert data["error"] == "unknown session"
            assert data["code"] == "unknown_session"

    def test_http_error_body_preserves_machine_code(self):
        """``_http_error_body`` must carry the backend's ``code`` through the
        error-body flattening — learn_remove dispatches on it."""
        import io
        import urllib.error

        from kiro_crew.mcp_core import _http_error_body

        err = urllib.error.HTTPError(
            url="http://x/api/lessons",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error": "unknown session", "code": "unknown_session"}'),
        )
        assert _http_error_body(err) == {
            "error": "unknown session",
            "code": "unknown_session",
        }
        # A non-identifier code is untrusted content and must be dropped, not
        # echoed onward.
        err = urllib.error.HTTPError(
            url="http://x/api/lessons",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error": "x", "code": "https://evil.example/exfil"}'),
        )
        assert _http_error_body(err) == {"error": "x"}

    def test_learn_remove_dispatches_on_code_not_wording(self, monkeypatch):
        """A rephrased error message must not break the fail-closed mapping:
        the dispatch key is ``code == "unknown_session"``, not the wording."""
        from kiro_crew.mcp_tools.learn import learn_remove

        monkeypatch.setattr(
            "kiro_crew.mcp_core._delete",
            lambda path, body=None: {
                "error": "session not recognised (reworded)",
                "code": "unknown_session",
            },
        )
        out = learn_remove("learn_remove", {"query": "x"})
        assert "No lessons were removed" in out

    def test_learn_remove_surfaces_other_errors_verbatim(self, monkeypatch):
        from kiro_crew.mcp_tools.learn import learn_remove

        monkeypatch.setattr(
            "kiro_crew.mcp_core._delete",
            lambda path, body=None: {"error": "boom"},
        )
        assert learn_remove("learn_remove", {"query": "x"}) == "Error: boom"


# ── Memory-routes recognition gate ──


class TestMemoryRoutesSessionGate:
    """The three mutating memory routes must apply the SAME session
    recognition as the lessons routes. Before the gate: DELETE
    /api/memory/episodic/{id} had NO session check at all (even a restricted
    session passed), and PUT /api/memory/semantic + DELETE
    /api/memory/semantic/{key} gated only on ``_is_restricted_session`` —
    which returns False for an unknown key, so a forged or never-established
    X-Session-Key could mutate durable memory that the lessons routes refuse.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _persisted_history_dir_tracks_patched_home(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.config_dir",
            lambda: Path.home() / ".kirocrew",
        )

    def _memory_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        store = MagicMock()
        store.set_semantic = MagicMock(return_value=None)
        store.delete_semantic = MagicMock(return_value=True)
        store.delete_episodic = MagicMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.memory._get_vector_store",
            MagicMock(return_value=store),
        )
        state = _make_state(tmp_path)
        return state, store

    def _make_memory_app(self, state):
        from kiro_crew.dashboard.handlers import (
            api_memory_episodic_delete,
            api_memory_semantic_delete,
            api_memory_semantic_write,
        )

        app = web.Application(middlewares=[_owner_dashboard_identity()])
        app["state"] = state
        app.router.add_put("/api/memory/semantic", api_memory_semantic_write)
        app.router.add_delete("/api/memory/semantic/{key:.+}", api_memory_semantic_delete)
        app.router.add_delete("/api/memory/episodic/{id}", api_memory_episodic_delete)
        return app

    async def _call(self, client, route, headers):
        if route == "semantic_write":
            return await client.put(
                "/api/memory/semantic",
                json={"key": "k", "value": "v"},
                headers=headers,
            )
        if route == "semantic_delete":
            return await client.delete("/api/memory/semantic/k", headers=headers)
        return await client.delete("/api/memory/episodic/42", headers=headers)

    def _mutations(self, store) -> int:
        return (
            store.set_semantic.call_count
            + store.delete_semantic.call_count
            + store.delete_episodic.call_count
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["semantic_write", "semantic_delete", "episodic_delete"])
    async def test_rejected_without_session_header(self, tmp_path, monkeypatch, route):
        state, store = self._memory_state(tmp_path, monkeypatch)
        async with TestClient(TestServer(self._make_memory_app(state))) as client:
            resp = await self._call(client, route, {})
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "missing_session_key"
        assert self._mutations(store) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["semantic_write", "semantic_delete", "episodic_delete"])
    async def test_rejected_for_forged_unknown_session(self, tmp_path, monkeypatch, route):
        """Core regression: a forged/unknown key must NOT mutate memory."""
        state, store = self._memory_state(tmp_path, monkeypatch)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)
        async with TestClient(TestServer(self._make_memory_app(state))) as client:
            resp = await self._call(client, route, {"X-Session-Key": "dashboard:forged-slot"})
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "unknown_session"
        assert self._mutations(store) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "route,mode",
        [
            ("semantic_write", "incognito"),
            ("semantic_write", "temporary"),
            ("semantic_delete", "incognito"),
            ("semantic_delete", "temporary"),
            ("episodic_delete", "incognito"),
            ("episodic_delete", "temporary"),
        ],
    )
    async def test_blocked_for_live_restricted_slot(self, tmp_path, monkeypatch, route, mode):
        """Restricted live slots are refused on every route, including episodic
        delete, which must not let a restricted session tombstone."""
        state, store = self._memory_state(tmp_path, monkeypatch)
        state.get_or_create_slot("r1", memory_mode=mode)
        async with TestClient(TestServer(self._make_memory_app(state))) as client:
            resp = await self._call(client, route, {"X-Session-Key": "dashboard:r1"})
            assert resp.status == 403
        assert self._mutations(store) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["semantic_write", "semantic_delete", "episodic_delete"])
    async def test_allowed_for_browser_ui(self, tmp_path, monkeypatch, route):
        state, store = self._memory_state(tmp_path, monkeypatch)
        async with TestClient(TestServer(self._make_memory_app(state))) as client:
            resp = await self._call(client, route, {"X-Session-Key": "dashboard:ui"})
            assert resp.status == 200
        assert self._mutations(store) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["semantic_write", "semantic_delete", "episodic_delete"])
    async def test_allowed_for_live_persistent_slot(self, tmp_path, monkeypatch, route):
        state, store = self._memory_state(tmp_path, monkeypatch)
        state.get_or_create_slot("p1")
        async with TestClient(TestServer(self._make_memory_app(state))) as client:
            resp = await self._call(client, route, {"X-Session-Key": "dashboard:p1"})
            assert resp.status == 200
        assert self._mutations(store) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["semantic_write", "semantic_delete", "episodic_delete"])
    async def test_blocked_for_evicted_incognito_session_jsonl(self, tmp_path, monkeypatch, route):
        """Archived-session recovery fails closed for private modes: the
        persisted memory_mode marker is the only remaining evidence once the
        slot is evicted, and memory writes block every private mode."""
        state, store = self._memory_state(tmp_path, monkeypatch)
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "_type": "metadata",
            "created_at": "2026-01-01T00:00:00",
            "memory_mode": "incognito",
        }
        (sess_dir / "dashboard_gone1.jsonl").write_text(_json.dumps(meta) + "\n", encoding="utf-8")
        async with TestClient(TestServer(self._make_memory_app(state))) as client:
            resp = await self._call(client, route, {"X-Session-Key": "dashboard:gone1"})
            assert resp.status == 403
            data = await resp.json()
            assert data["code"] == "restricted_session"
        assert self._mutations(store) == 0
