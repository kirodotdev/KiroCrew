"""Tests for the shared Slack sessions view.

Covers the helpers in ``kiro_crew.slack.sessions_view`` that the slash command
``/<command> sessions``, the ``sessions`` keyword in DMs, and the App
Home Tab all share, plus the keyword handler in
``kiro_crew.slack.handler`` which delegates to those helpers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import MockSlackClient
from kiro_crew.slack.handler import _handle_sessions_command
from kiro_crew.slack.sessions_view import (
    _SESSION_KIND_DASHBOARD,
    _SESSION_KIND_OTHER,
    _SESSION_KIND_TASKRUNNER,
    _build_sessions_blocks,
    _classify_session_key,
    _collect_recent_sessions,
    _default_session_title,
)


def _write_jsonl(path: Path, *, title: str = "", agent: str = "", messages: list | None = None) -> None:
    """Write a session JSONL file with an optional metadata line and messages."""
    lines: list[str] = []
    meta: dict = {"_type": "metadata"}
    if title:
        meta["title"] = title
    if agent:
        meta["agent"] = agent
    if title or agent:
        lines.append(json.dumps(meta))
    for role, content in messages or []:
        lines.append(json.dumps({"role": role, "content": content}))
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# _classify_session_key / _default_session_title
# ---------------------------------------------------------------------------


class TestClassifySessionKey:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("dashboard:chat-1-123", _SESSION_KIND_DASHBOARD),
            ("dashboard_chat-1-123", _SESSION_KIND_DASHBOARD),
            ("taskrunner:abc-task1", _SESSION_KIND_TASKRUNNER),
            ("taskrunner_proj_task1", _SESSION_KIND_TASKRUNNER),
            ("cron:job-id", _SESSION_KIND_OTHER),
            ("subagent:abc", _SESSION_KIND_OTHER),
            ("_bg", _SESSION_KIND_OTHER),
            ("", _SESSION_KIND_OTHER),
        ],
    )
    def test_classify(self, key, expected):
        assert _classify_session_key(key) == expected


class TestDefaultSessionTitle:
    def test_dashboard_colon(self):
        assert _default_session_title("dashboard:chat-1-123", _SESSION_KIND_DASHBOARD) == "Dashboard chat-1-123"

    def test_dashboard_underscore(self):
        assert _default_session_title("dashboard_chat-1-123", _SESSION_KIND_DASHBOARD) == "Dashboard chat-1-123"

    def test_taskrunner_underscore(self):
        assert _default_session_title("taskrunner_run-foo", _SESSION_KIND_TASKRUNNER) == "Task Runner run-foo"

    def test_taskrunner_underscore_run_id_format(self):
        # Production format is ``taskrunner_run_<task_id>`` (after _safe_key
        # mangles colons to underscores). Old slash code used split('_', 2)[-1]
        # to render this as ``Task Runner <task_id>``; verify parity.
        assert (
            _default_session_title("taskrunner_run_42", _SESSION_KIND_TASKRUNNER)
            == "Task Runner 42"
        )
        assert (
            _default_session_title("taskrunner_abc_step1", _SESSION_KIND_TASKRUNNER)
            == "Task Runner step1"
        )

    def test_taskrunner_colon(self):
        assert _default_session_title("taskrunner:run:42", _SESSION_KIND_TASKRUNNER) == "Task Runner 42"

    def test_other_returns_key_unchanged(self):
        assert _default_session_title("cron:abc", _SESSION_KIND_OTHER) == "cron:abc"


# ---------------------------------------------------------------------------
# _collect_recent_sessions
# ---------------------------------------------------------------------------


class TestCollectRecentSessions:
    @pytest.fixture
    def sess_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "sessions"
        d.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", d)
        return d

    def test_missing_dir_returns_empty(self, tmp_path, monkeypatch):
        # Point at a directory that doesn't exist
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", tmp_path / "nope")
        assert _collect_recent_sessions(None) == []

    def test_reads_dashboard_session(self, sess_dir):
        _write_jsonl(
            sess_dir / "dashboard_chat-1-100.jsonl",
            title="Hello world",
            agent="kirocrew",
            messages=[("user", "hi"), ("assistant", "hello")],
        )
        rows = _collect_recent_sessions(None)
        assert len(rows) == 1
        row = rows[0]
        # Filename uses _ but the canonical key uses :
        assert row["key"] == "dashboard:chat-1-100"
        assert row["title"] == "Hello world"
        assert row["agent"] == "kirocrew"
        assert row["kind"] == _SESSION_KIND_DASHBOARD
        assert row["active"] is False
        assert row["msgs"] == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_reads_taskrunner_session_with_default_title(self, sess_dir):
        _write_jsonl(
            sess_dir / "taskrunner_run-foo.jsonl",
            messages=[("user", "do task"), ("assistant", "done")],
        )
        rows = _collect_recent_sessions(None)
        assert len(rows) == 1
        row = rows[0]
        assert row["key"] == "taskrunner_run-foo"
        assert row["kind"] == _SESSION_KIND_TASKRUNNER
        # No metadata line → default title kicks in
        assert row["title"] == "Task Runner run-foo"
        # No metadata agent → defaults to kirocrew
        assert row["agent"] == "kirocrew"

    def test_filters_by_kind(self, sess_dir):
        _write_jsonl(sess_dir / "dashboard_a.jsonl", title="A", messages=[("user", "x")])
        _write_jsonl(sess_dir / "taskrunner_b.jsonl", title="B", messages=[("user", "y")])
        _write_jsonl(sess_dir / "cron_c.jsonl", title="C", messages=[("user", "z")])

        dash = _collect_recent_sessions(None, kind=_SESSION_KIND_DASHBOARD)
        assert {r["title"] for r in dash} == {"A"}

        tr = _collect_recent_sessions(None, kind=_SESSION_KIND_TASKRUNNER)
        assert {r["title"] for r in tr} == {"B"}

        other = _collect_recent_sessions(None, kind=_SESSION_KIND_OTHER)
        assert {r["title"] for r in other} == {"C"}

    def test_sorts_by_mtime_descending(self, sess_dir):
        old = sess_dir / "dashboard_old.jsonl"
        new = sess_dir / "dashboard_new.jsonl"
        _write_jsonl(old, title="old", messages=[("user", "a")])
        _write_jsonl(new, title="new", messages=[("user", "a")])
        # Force older mtime on `old`
        past = time.time() - 3600
        import os

        os.utime(old, (past, past))

        rows = _collect_recent_sessions(None)
        assert [r["title"] for r in rows] == ["new", "old"]

    def test_caps_at_limit(self, sess_dir):
        for i in range(15):
            _write_jsonl(
                sess_dir / f"dashboard_chat-{i}.jsonl",
                title=f"chat {i}",
                messages=[("user", "x")],
            )
        rows = _collect_recent_sessions(None, limit=5)
        assert len(rows) == 5

    def test_caps_message_preview(self, sess_dir):
        msgs = [("user", f"m{i}") for i in range(20)]
        _write_jsonl(sess_dir / "dashboard_chat-1.jsonl", title="t", messages=msgs)
        row = _collect_recent_sessions(None)[0]
        # Default preview is the last 5 messages
        assert len(row["msgs"]) == 5
        assert row["msgs"][-1]["content"] == "m19"

    def test_skips_symlinks(self, sess_dir, tmp_path):
        real = tmp_path / "real.jsonl"
        _write_jsonl(real, title="real", messages=[("user", "x")])
        link = sess_dir / "dashboard_real.jsonl"
        link.symlink_to(real)
        # Symlink-only session is skipped
        assert _collect_recent_sessions(None) == []

    def test_skips_empty_file(self, sess_dir):
        (sess_dir / "dashboard_empty.jsonl").write_text("", encoding="utf-8")
        assert _collect_recent_sessions(None) == []

    def test_skips_malformed_lines(self, sess_dir):
        path = sess_dir / "dashboard_bad.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"_type": "metadata", "title": "ok"}),
                    "not json",
                    json.dumps({"role": "user", "content": "hi"}),
                ]
            ),
            encoding="utf-8",
        )
        rows = _collect_recent_sessions(None)
        assert len(rows) == 1
        assert rows[0]["title"] == "ok"
        assert rows[0]["msgs"] == [{"role": "user", "content": "hi"}]

    def test_truncates_long_message_content(self, sess_dir):
        big = "x" * 10000
        _write_jsonl(sess_dir / "dashboard_a.jsonl", title="t", messages=[("user", big)])
        row = _collect_recent_sessions(None)[0]
        # _SESSIONS_MAX_MSG_CHARS = 4000
        assert len(row["msgs"][0]["content"]) == 4000

    def test_active_marker_uses_session_manager(self, sess_dir):
        _write_jsonl(sess_dir / "dashboard_chat-1.jsonl", title="t", messages=[("user", "x")])

        sessions = MagicMock()
        sessions.has_session.return_value = True
        rows = _collect_recent_sessions(sessions)
        assert rows[0]["active"] is True
        sessions.has_session.assert_called_with("dashboard:chat-1")

        sessions.has_session.return_value = False
        rows = _collect_recent_sessions(sessions)
        assert rows[0]["active"] is False

    def test_skips_non_user_assistant_roles(self, sess_dir):
        path = sess_dir / "dashboard_mixed.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"_type": "metadata", "title": "t"}),
                    json.dumps({"role": "system", "content": "ignore me"}),
                    json.dumps({"role": "user", "content": "keep"}),
                    json.dumps({"role": "tool", "content": "ignore"}),
                ]
            ),
            encoding="utf-8",
        )
        row = _collect_recent_sessions(None)[0]
        assert row["msgs"] == [{"role": "user", "content": "keep"}]


# ---------------------------------------------------------------------------
# _build_sessions_blocks
# ---------------------------------------------------------------------------


class TestBuildSessionsBlocks:
    def test_empty_input(self):
        assert _build_sessions_blocks([]) == []

    def test_single_row_emits_card_and_actions(self):
        rows = [
            {
                "key": "dashboard:chat-1",
                "title": "Hello",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [{"role": "user", "content": "hi"}],
            }
        ]
        blocks = _build_sessions_blocks(rows)
        # task_card + actions, no trailing divider for last row
        kinds = [b.get("type") for b in blocks]
        assert kinds == ["task_card", "actions"]
        # Resume button has the canonical action_id and value JSON
        button = blocks[1]["elements"][0]
        assert button["action_id"] == "mc_session_resume_dashboard:chat-1"
        payload = json.loads(button["value"])
        assert payload == {"key": "dashboard:chat-1", "title": "Hello"}

    def test_multiple_rows_have_divider_between(self):
        rows = [
            {
                "key": f"dashboard:chat-{i}",
                "title": f"t{i}",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [],
            }
            for i in range(3)
        ]
        blocks = _build_sessions_blocks(rows)
        # 3 rows × (task_card + actions) + 2 dividers = 8 blocks
        assert len(blocks) == 8
        assert blocks[2]["type"] == "divider"
        assert blocks[5]["type"] == "divider"

    def test_active_session_uses_in_progress_status(self):
        rows = [
            {
                "key": "dashboard:active",
                "title": "running",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": True,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [],
            }
        ]
        card = _build_sessions_blocks(rows)[0]
        assert card["status"] == "in_progress"
        assert "🟢" in card["title"]

    def test_inactive_session_uses_complete_status(self):
        rows = [
            {
                "key": "dashboard:done",
                "title": "done",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [],
            }
        ]
        card = _build_sessions_blocks(rows)[0]
        assert card["status"] == "complete"
        assert "⚫" in card["title"]

    def test_redacts_credentials_in_title(self):
        rows = [
            {
                "key": "dashboard:k",
                # ASK-style credential pattern is redacted by redact_credentials
                "title": "AKIAIOSFODNN7EXAMPLE secret",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [],
            }
        ]
        card = _build_sessions_blocks(rows)[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in card["title"]

    def test_redacts_credentials_in_agent(self):
        """Parity with title — agent strings are redacted before reaching session_task_card."""
        rows = [
            {
                "key": "dashboard:k",
                "title": "ok",
                "agent": "AKIAIOSFODNN7EXAMPLE",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [],
            }
        ]
        card = _build_sessions_blocks(rows)[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in card["title"]

    def test_redacts_credentials_in_message_content(self):
        """Content for each preview message is redacted via session_task_card → _msg_elements."""
        rows = [
            {
                "key": "dashboard:k",
                "title": "ok",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [
                    {"role": "user", "content": "leak AKIAIOSFODNN7EXAMPLE"},
                    {"role": "assistant", "content": "another AKIAIOSFODNN7EXAMPLE"},
                ],
            }
        ]
        # task_card details payload contains the rich_text_list; serialize for substring search
        card = _build_sessions_blocks(rows)[0]
        rendered = json.dumps(card)
        assert "AKIAIOSFODNN7EXAMPLE" not in rendered

    def test_redacts_exfiltration_urls_in_message_content(self):
        """Regression for review-bot security-controls comment on rev 1.

        The pre-refactor inline code applied BOTH ``redact_exfiltration_urls()``
        and ``redact_credentials()`` to message content before posting to Slack.
        The refactor delegates message redaction to ``session_task_card()`` →
        ``blocks._msg_elements()`` → ``security.redact_and_truncate()``. Locks
        in that the exfiltration-URL redaction half is still applied to message
        content even after the delegation.
        """
        # Long-query-string URL with a base64-like blob is the canonical
        # exfiltration pattern detected by redact_exfiltration_urls.
        blob = "A" * 50
        url = f"https://evil.example.com/x?data={blob}&{'x' * 200}"
        rows = [
            {
                "key": "dashboard:k",
                "title": "ok",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [
                    {"role": "user", "content": f"clicked {url} earlier"},
                ],
            }
        ]
        card = _build_sessions_blocks(rows)[0]
        rendered = json.dumps(card)
        # The full URL must not appear verbatim — redact_exfiltration_urls
        # will have replaced it with a sanitized form.
        assert url not in rendered

    def test_dual_redaction_in_home_tab_message_path(self):
        """Same dual-redaction guarantee for the Home Tab path (for_home_tab=True).

        Note: the Home Tab section row only renders title + agent (no message
        bullets), so this test exercises the title path's dual redaction.
        Message content for the Home Tab is not currently surfaced in the
        section block, but if that ever changes the dual-redaction guarantee
        must still hold.
        """
        blob = "A" * 50
        url = f"https://evil.example.com/x?data={blob}&{'x' * 200}"
        rows = [
            {
                "key": "dashboard:k",
                "title": f"AKIAIOSFODNN7EXAMPLE {url}",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": _SESSION_KIND_DASHBOARD,
                "msgs": [],
            }
        ]
        blocks = _build_sessions_blocks(rows, for_home_tab=True)
        rendered = json.dumps(blocks)
        assert "AKIAIOSFODNN7EXAMPLE" not in rendered
        assert url not in rendered


# ---------------------------------------------------------------------------
# handler._handle_sessions_command — delegates to events.py helpers
# ---------------------------------------------------------------------------


class TestHandleSessionsCommandDelegation:
    @pytest.mark.asyncio
    async def test_empty_posts_no_recent_sessions_message(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)

        slack = MockSlackClient()
        await _handle_sessions_command(
            "sessions", slack, "C123", "100.000", "100.000", "C123:100.000", None, sessions=None
        )

        # Posts a plain text message (not blocks) on empty
        kinds = [a[0] for a in slack.actions]
        assert kinds == ["post"]
        assert "No recent sessions" in slack.actions[0][1]["text"]

    @pytest.mark.asyncio
    async def test_renders_blocks_via_shared_builder(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="Hello",
            messages=[("user", "hi")],
        )
        _write_jsonl(
            sess_dir / "taskrunner_run-1.jsonl",
            title="autopilot run",
            messages=[("user", "go")],
        )

        slack = MockSlackClient()
        await _handle_sessions_command(
            "sessions", slack, "C123", "100.000", "100.000", "C123:100.000", None, sessions=None
        )

        # Posts via post_blocks, not post_message
        kinds = [a[0] for a in slack.actions]
        assert kinds == ["blocks"]
        blocks = slack.actions[0][1]["blocks"]
        # One Resume button per session
        actions_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions_blocks) == 2
        action_ids = [b["elements"][0]["action_id"] for b in actions_blocks]
        assert "mc_session_resume_dashboard:chat-1" in action_ids
        assert "mc_session_resume_taskrunner_run-1" in action_ids

    @pytest.mark.asyncio
    async def test_logs_sel_audit_on_access(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_jsonl(sess_dir / "dashboard_a.jsonl", title="t", messages=[("user", "x")])

        slack = MockSlackClient()
        with patch("kiro_crew.slack.handler.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions_command(
                "sessions",
                slack,
                "C123",
                "100.000",
                "100.000",
                "session-key",
                None,
                sessions=None,
            )

        # SEL audit fires with the operation name and the count of sessions read
        mock_sel.return_value.log_api_access.assert_called_once()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "slack.sessions_data_access"
        assert kwargs["caller"] == "session-key"
        assert "1 sessions read" in kwargs["resources"]

    @pytest.mark.asyncio
    async def test_active_marker_propagates_via_sessions_kwarg(self, tmp_path, monkeypatch):
        """The ``sessions=`` kwarg flows through to _collect_recent_sessions
        so a card for a live session renders as ``in_progress`` (🟢)."""
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="Live chat",
            messages=[("user", "hi")],
        )

        # SessionManager-shaped mock that reports the chat as live
        sm = MagicMock()
        sm.has_session = MagicMock(side_effect=lambda key: key == "dashboard:chat-1")

        slack = MockSlackClient()
        await _handle_sessions_command(
            "sessions", slack, "C123", "100.000", "100.000", "C123:100.000", None, sessions=sm
        )

        assert [a[0] for a in slack.actions] == ["blocks"]
        blocks = slack.actions[0][1]["blocks"]
        cards = [b for b in blocks if b.get("type") == "task_card"]
        assert len(cards) == 1
        assert cards[0]["status"] == "in_progress"
        assert "🟢" in cards[0]["title"]
        sm.has_session.assert_called_with("dashboard:chat-1")

    @pytest.mark.asyncio
    async def test_keyword_collector_failure_emits_error_audit(
        self, tmp_path, monkeypatch
    ):
        """Regression for review-bot security-controls. The keyword path
        previously called the collector outside any try/except, so an
        OSError would skip the SEL audit entirely. Locks in that the
        error-outcome audit fires on collector failure, mirroring the
        slash and Home Tab error-path patterns.
        """
        slack = MockSlackClient()
        with (
            patch(
                "kiro_crew.slack.handler._collect_recent_sessions",
                side_effect=OSError("disk error"),
            ),
            patch("kiro_crew.slack.handler.sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions_command(
                "sessions",
                slack,
                "C123",
                "100.000",
                "100.000",
                "session-key",
                None,
                sessions=None,
            )

        # Exactly one audit fires: the error-outcome one
        mock_sel.return_value.log_api_access.assert_called_once()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "slack.sessions_data_access"
        assert kwargs["outcome"] == "error"
        assert kwargs["caller"] == "session-key"
        assert "collector failed" in kwargs["resources"]
        assert "disk error" in kwargs["error"]
        # User sees a graceful unavailable message
        kinds = [a[0] for a in slack.actions]
        assert kinds == ["post"]
        assert "Sessions unavailable" in slack.actions[0][1]["text"]

    @pytest.mark.asyncio
    async def test_keyword_collector_failure_redacts_credentials_in_exc(
        self, tmp_path, monkeypatch
    ):
        """Defense-in-depth on the keyword error path: credential patterns
        in exception messages MUST be redacted before being written to SEL.
        """
        slack = MockSlackClient()
        leaked_key = "AKIAIOSFODNN7EXAMPLE"
        with (
            patch(
                "kiro_crew.slack.handler._collect_recent_sessions",
                side_effect=OSError(f"failed reading {leaked_key} from path"),
            ),
            patch("kiro_crew.slack.handler.sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions_command(
                "sessions",
                slack,
                "C123",
                "100.000",
                "100.000",
                "session-key",
                None,
                sessions=None,
            )

        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        # Credential MUST NOT survive into the audit field
        assert leaked_key not in kwargs["error"]


# ---------------------------------------------------------------------------
# events._handle_sessions — slash command SEL audit
# ---------------------------------------------------------------------------


class TestSlashSessionsAudit:
    """The ``/<command> sessions`` slash command must emit a SEL audit
    event so its data-access activity is captured alongside the keyword
    handler (``slack.sessions_data_access``), the keyword gate
    (``slack.sessions_command``), and the Home Tab
    (``slack.home_tab_sessions_data_access``).
    """

    @pytest.mark.asyncio
    async def test_slash_logs_sel_audit_with_caller_and_count(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.slack.events import _handle_sessions

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="t",
            messages=[("user", "x")],
        )

        orch = MagicMock()
        orch.sessions = MagicMock()
        orch.sessions.has_session = MagicMock(return_value=False)
        respond = MagicMock()

        async def _arespond(*a, **kw):
            respond(*a, **kw)

        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_owner", return_value=True),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions(orch, "UCALLER", "", _arespond)

        mock_sel.return_value.log_api_access.assert_called_once()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "slack.sessions_slash_data_access"
        assert kwargs["caller"] == "UCALLER"
        assert kwargs["outcome"] == "allowed"
        assert kwargs["source"] == "slack"
        assert "1 sessions read" in kwargs["resources"]

    @pytest.mark.asyncio
    async def test_slash_logs_sel_audit_even_when_empty(
        self, tmp_path, monkeypatch
    ):
        """The audit fires before the empty-rows check so the access
        attempt is recorded even when there is nothing to display."""
        from kiro_crew.slack.events import _handle_sessions

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)

        orch = MagicMock()
        orch.sessions = MagicMock()
        respond = MagicMock()

        async def _arespond(*a, **kw):
            respond(*a, **kw)

        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_owner", return_value=True),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions(orch, "UCALLER", "", _arespond)

        mock_sel.return_value.log_api_access.assert_called_once()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "slack.sessions_slash_data_access"
        assert "0 sessions read" in kwargs["resources"]
        # And the empty-state message is what users see
        respond.assert_called_once()
        assert "No recent sessions" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_slash_unauthorized_denied_with_audit(
        self, tmp_path, monkeypatch
    ):
        """Regression for review-bot security-controls / authorization rule.

        Per the deny-by-default guideline, the slash command must reject
        callers that are neither the owner nor an explicitly-allowed user,
        and the rejection must be recorded via SEL with outcome=denied so
        unauthorized read attempts show up in the audit pipeline.
        Mirror of the keyword path's gate in handler.py:1868.
        """
        from kiro_crew.slack.events import _handle_sessions

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        # Real session on disk so we can prove the collector was NOT called
        # via the absence of the success-path audit (only the denied audit
        # should fire, and the response should be the permission-denied msg).
        _write_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="confidential session",
            messages=[("user", "secret data")],
        )

        orch = MagicMock()
        orch.sessions = MagicMock()
        respond = MagicMock()

        async def _arespond(*a, **kw):
            respond(*a, **kw)

        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_owner", return_value=False),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions(orch, "UATTACKER", "skim", _arespond)

        # SEL audit fires exactly once with outcome=denied
        mock_sel.return_value.log_api_access.assert_called_once()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "slack.sessions_slash_data_access"
        assert kwargs["caller"] == "UATTACKER"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "slack"
        # Convention: log_api_access denied audits use error= for the reason
        # plus resources= for context (the args the caller passed).
        assert "unauthorized" in kwargs["error"].lower()
        assert kwargs["resources"] == "skim"

        # Caller sees a permission-denied message, not session contents
        respond.assert_called_once()
        body = respond.call_args[0][0]
        assert "Permission denied" in body
        # Sensitive data must NOT leak through the response
        assert "confidential session" not in body
        assert "secret data" not in body

    @pytest.mark.asyncio
    async def test_slash_authorized_via_allowed_user_branch(
        self, tmp_path, monkeypatch
    ):
        """Verify the OR's second arm: is_owner=False but is_allowed_user=True
        still grants access. Locks in that the auth check accepts allowed-list
        users, not just the owner.
        """
        from kiro_crew.slack.events import _handle_sessions

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="t",
            messages=[("user", "x")],
        )

        orch = MagicMock()
        orch.sessions = MagicMock()
        orch.sessions.has_session = MagicMock(return_value=False)
        respond = MagicMock()

        async def _arespond(*a, **kw):
            respond(*a, **kw)

        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_owner", return_value=False),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions(orch, "UALLOWED", "", _arespond)

        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_slash_collector_failure_emits_error_audit(
        self, tmp_path, monkeypatch
    ):
        """Slash command must emit an error-outcome SEL audit if the collector
        raises, mirroring the Home Tab error-path pattern. Without this, an
        IO failure would skip the audit entirely.
        """
        from kiro_crew.slack.events import _handle_sessions

        orch = MagicMock()
        orch.sessions = MagicMock()
        respond = MagicMock()

        async def _arespond(*a, **kw):
            respond(*a, **kw)

        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_owner", return_value=True),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
            patch(
                "kiro_crew.slack.events._collect_recent_sessions",
                side_effect=OSError("disk error"),
            ),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions(orch, "UCALLER", "", _arespond)

        # Exactly one audit fires: the error-outcome one
        mock_sel.return_value.log_api_access.assert_called_once()
        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "slack.sessions_slash_data_access"
        assert kwargs["outcome"] == "error"
        assert kwargs["caller"] == "UCALLER"
        assert "collector failed" in kwargs["resources"]
        # error= is redacted then truncated; raw "disk error" should still
        # pass through (no credential / exfil pattern to redact)
        assert "disk error" in kwargs["error"]
        # User sees a graceful unavailable message
        respond.assert_called_once()
        assert "Sessions unavailable" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_slash_collector_failure_redacts_credentials_in_exc_message(
        self, tmp_path, monkeypatch
    ):
        """Defense-in-depth: if a collector exception happens to contain a
        credential pattern (e.g. an OSError that includes a path with a
        leaked AWS access key), the SEL audit's ``error=`` field MUST be
        redacted before truncation. Locks in redact-then-truncate ordering.
        """
        from kiro_crew.slack.events import _handle_sessions

        orch = MagicMock()
        orch.sessions = MagicMock()
        respond = MagicMock()

        async def _arespond(*a, **kw):
            respond(*a, **kw)

        leaked_key = "AKIAIOSFODNN7EXAMPLE"
        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_owner", return_value=True),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
            patch(
                "kiro_crew.slack.events._collect_recent_sessions",
                side_effect=OSError(f"failed reading {leaked_key} from path"),
            ),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _handle_sessions(orch, "UCALLER", "", _arespond)

        kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        # Credential MUST NOT survive into the audit field
        assert leaked_key not in kwargs["error"]
