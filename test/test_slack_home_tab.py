"""Tests for Slack Home Tab (_publish_home_tab)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack import events as events_mod
from kiro_crew.slack.events import _publish_home_tab

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeCronJob:
    name: str = "test-job"
    enabled: bool = True
    schedule: object = field(default_factory=lambda: MagicMock())


@dataclass
class FakeLesson:
    rule: str = "always do X"


class FakeCronService:
    def __init__(self, jobs: list | None = None):
        self._jobs = jobs if jobs is not None else []

    def list_jobs(self, include_disabled: bool = False) -> list:
        return self._jobs


class FakeLessonStore:
    def __init__(self, lessons: list | None = None):
        self._lessons = lessons if lessons is not None else []

    def load_all(self) -> list:
        return self._lessons


class FakeCtxBuilder:
    def __init__(self, lessons: FakeLessonStore | None = None):
        self.lessons = lessons or FakeLessonStore()


class FakeSessions:
    def __init__(self, count: int = 0):
        self.count = count

    def has_session(self, key: str) -> bool:
        return False


def _make_orch(
    *,
    slack: AsyncMock | None = "auto",
    sessions: FakeSessions | None = "auto",
    cron_svc: FakeCronService | None = "auto",
    ctx_builder: FakeCtxBuilder | None = "auto",
):
    orch = MagicMock()
    orch.slack = AsyncMock() if slack == "auto" else slack
    orch.sessions = FakeSessions(2) if sessions == "auto" else sessions
    orch.cron_svc = FakeCronService([FakeCronJob()]) if cron_svc == "auto" else cron_svc
    orch.ctx_builder = (
        FakeCtxBuilder(FakeLessonStore([FakeLesson()])) if ctx_builder == "auto" else ctx_builder
    )
    return orch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPublishHomeTabHappyPath:
    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_builds_valid_home_view(self, _mw, _fmt, _yolo):
        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        orch.slack.views_publish.assert_awaited_once()
        call_kwargs = orch.slack.views_publish.call_args[1]
        assert call_kwargs["user_id"] == "U123"
        view = call_kwargs["view"]
        assert view["type"] == "home"

        blocks = view["blocks"]
        text = str(blocks)
        assert "Kiro Crew Status" in text
        assert "Cron Jobs" in text
        assert "Recent Lessons" in text
        assert "Commands" in text
        assert "test-job" in text
        assert "always do X" in text
        assert "Active sessions" in text
        assert "SSO" in text
        assert "Uptime" in text
        assert "Capabilities" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=True)
    @patch("kiro_crew.slack.events.format_schedule", return_value="daily")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_yolo_on_shown(self, _mw, _fmt, _yolo):
        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        assert "🟢 ON" in str(view["blocks"])


class TestPublishHomeTabEmptyState:
    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    async def test_empty_crons_and_lessons(self, _yolo):
        orch = _make_orch(
            cron_svc=FakeCronService([]),
            ctx_builder=FakeCtxBuilder(FakeLessonStore([])),
            sessions=FakeSessions(0),
        )
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        text = str(view["blocks"])
        assert "No cron jobs" in text
        assert "No lessons yet" in text


class TestPublishHomeTabNoneServices:
    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    async def test_all_services_none(self, _yolo):
        orch = _make_orch(sessions=None, cron_svc=None, ctx_builder=None)
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        text = str(view["blocks"])
        assert "Cron service unavailable" in text
        assert "Lessons unavailable" in text
        assert "Active sessions" not in text


class TestPublishHomeTabErrorHandling:
    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    async def test_error_publishes_fallback(self, _yolo):
        orch = _make_orch()
        orch.slack.views_publish = AsyncMock(side_effect=[RuntimeError("API down"), None])
        await _publish_home_tab(orch, "U123")

        assert orch.slack.views_publish.await_count == 2
        fallback = orch.slack.views_publish.call_args_list[1][1]["view"]
        assert fallback["type"] == "home"
        assert "Failed to load Home Tab" in str(fallback["blocks"])

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    async def test_slack_none_logs_warning(self, _yolo):
        orch = _make_orch(slack=None)
        # Should not raise
        await _publish_home_tab(orch, "U123")


class TestViewTypeAlwaysHome:
    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="*")
    async def test_view_type_is_home(self, _fmt, _yolo):
        orch = _make_orch()
        await _publish_home_tab(orch, "U999")

        view = orch.slack.views_publish.call_args[1]["view"]
        assert view["type"] == "home"


class TestPublishHomeTabCapabilities:
    """Tests for the Capabilities section (MCP integrations + skills)."""

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch(
        "kiro_crew.slack.events.list_servers",
        return_value=[SimpleNamespace(name="builder-mcp"), SimpleNamespace(name="kirocrew-core")],
    )
    @patch(
        "kiro_crew.slack.events._get_skills_loader",
    )
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_shows_mcp_servers_and_skills(self, _mw, mock_loader, _servers, _yolo):
        mock_loader.return_value.list_skills.return_value = [
            {"name": "taskei", "key": "taskei", "description": "d", "always": False},
        ]
        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "Capabilities" in text
        assert "MCP Integrations (2)" in text
        assert "Skills (1)" in text
        assert "taskei" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.list_servers", return_value=[])
    @patch("kiro_crew.slack.events._get_skills_loader")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_empty_capabilities(self, _mw, mock_loader, _servers, _yolo):
        mock_loader.return_value.list_skills.return_value = []
        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "No MCP servers or skills configured" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.list_servers", side_effect=RuntimeError("boom"))
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_capabilities_error_handled(self, _mw, _servers, _yolo):
        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "Capabilities unavailable" in text


class TestPublishHomeTabUptime:
    """Tests for the Uptime line in Status section."""

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.Stats")
    @patch("kiro_crew.slack.events.list_servers", return_value=[])
    @patch("kiro_crew.slack.events._get_skills_loader")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_uptime_shown_in_status(self, _mw, mock_loader, _servers, mock_stats_cls, _yolo):
        mock_loader.return_value.list_skills.return_value = []
        mock_stats_cls.return_value.uptime_str.return_value = "2h 30m"
        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "Uptime" in text
        assert "2h 30m" in text


class TestPublishHomeTabVectorStore:
    """Tests for the vector store lesson path in the home tab."""

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_reads_from_vector_store(self, _mw, _fmt, _yolo):
        """When vector_memory.get_lessons() returns a list, lessons display from it."""
        orch = _make_orch()
        # Replace the auto-created MagicMock with a real-ish vector store mock
        orch.vector_memory = MagicMock()
        orch.vector_memory.get_lessons.return_value = [
            {"key": "lesson.1", "value_json": '{"rule": "always test", "category": "preference"}'},
            {"key": "lesson.2", "value_json": '"simple string lesson"'},
        ]
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        text = str(view["blocks"])
        assert "always test" in text
        assert "simple string lesson" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_mapping_row_keeps_its_not_clause(self, _mw, _fmt, _yolo):
        """A mapping-shaped lesson renders rule AND negative, not rule-only."""
        orch = _make_orch()
        orch.vector_memory = MagicMock()
        orch.vector_memory.get_lessons.return_value = [
            {
                "key": "lesson.1",
                "value_json": '{"rule": "prefer X", "category": "preference",'
                ' "negative": "never Y"}',
            },
        ]
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        text = str(view["blocks"])
        assert "prefer X" in text
        assert "never Y" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅ 5.0h remaining",
    )
    async def test_vector_store_error_falls_back_to_jsonl(self, _mw, _fmt, _yolo):
        """When vector store raises, falls back to JSONL lessons."""
        orch = _make_orch()
        orch.vector_memory = MagicMock()
        orch.vector_memory.get_lessons.side_effect = RuntimeError("DB locked")
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        text = str(view["blocks"])
        # Should fall back to the FakeLesson from ctx_builder
        assert "always do X" in text


# ---------------------------------------------------------------------------
# Sessions section
# ---------------------------------------------------------------------------


def _write_session_jsonl(path, *, title="", agent="", messages=None):
    """Write a session JSONL with optional metadata + user/assistant messages."""
    import json as _json

    lines: list[str] = []
    meta: dict = {"_type": "metadata"}
    if title:
        meta["title"] = title
    if agent:
        meta["agent"] = agent
    if title or agent:
        lines.append(_json.dumps(meta))
    for role, content in messages or []:
        lines.append(_json.dumps({"role": role, "content": content}))
    path.write_text("\n".join(lines), encoding="utf-8")


class TestPublishHomeTabSessions:
    """Tests for the 🧵 Sessions section of the Home Tab."""

    @pytest.fixture(autouse=True)
    def _grant_home_tab_auth(self, monkeypatch):
        """Patch is_owner/is_allowed_user to True so the Sessions section's
        defense-in-depth auth gate doesn't block these tests by default.
        Individual tests that need to exercise the deny path override this
        within a ``with patch(...)`` block.
        """
        monkeypatch.setattr("kiro_crew.slack.events.is_owner", lambda _: True)
        monkeypatch.setattr(
            "kiro_crew.slack.events.is_allowed_user", lambda _: True
        )

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_section_header_always_rendered(self, _mw, _fmt, _yolo, tmp_path, monkeypatch):
        """The Sessions header appears even when there are no sessions on disk."""
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)

        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "🧵 Sessions" in text
        assert "No recent sessions" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_dashboard_session_rendered_under_main_chat(
        self, _mw, _fmt, _yolo, tmp_path, monkeypatch
    ):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_session_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="Pipeline triage",
            messages=[("user", "what's broken?")],
        )

        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        blocks = orch.slack.views_publish.call_args[1]["view"]["blocks"]
        text = str(blocks)
        assert "🧵 Sessions" in text
        assert "Main chat" in text
        assert "Pipeline triage" in text
        # Resume button wiring uses the canonical action_id
        assert "mc_session_resume_dashboard:chat-1" in text
        # And the empty-state placeholder is NOT rendered when there is content
        assert "No recent sessions" not in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_taskrunner_session_rendered_under_autopilot(
        self, _mw, _fmt, _yolo, tmp_path, monkeypatch
    ):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_session_jsonl(
            sess_dir / "taskrunner_run-foo.jsonl",
            title="Refactor login",
            messages=[("user", "do it")],
        )

        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "Autopilot / task runner" in text
        assert "Refactor login" in text
        assert "mc_session_resume_taskrunner_run-foo" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_caps_at_five_rows_per_kind(self, _mw, _fmt, _yolo, tmp_path, monkeypatch):
        """Home Tab requests at most _HOME_TAB_SESSIONS_PER_KIND rows per kind."""
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        for i in range(8):
            _write_session_jsonl(
                sess_dir / f"dashboard_chat-{i}.jsonl",
                title=f"d{i}",
                messages=[("user", "x")],
            )
            _write_session_jsonl(
                sess_dir / f"taskrunner_run-{i}.jsonl",
                title=f"t{i}",
                messages=[("user", "x")],
            )

        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        blocks = orch.slack.views_publish.call_args[1]["view"]["blocks"]
        # Count Resume action_ids per kind
        text = str(blocks)
        dashboard_count = text.count("mc_session_resume_dashboard:")
        taskrunner_count = text.count("mc_session_resume_taskrunner_")
        assert dashboard_count == 5
        assert taskrunner_count == 5

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_uses_section_blocks_not_task_card(
        self, _mw, _fmt, _yolo, tmp_path, monkeypatch
    ):
        """Slack ``views.publish`` rejects ``task_card`` blocks with
        ``unsupported type: task_card``. The Home Tab MUST render sessions
        as plain ``section`` blocks. Regression test for the rendering
        path discovered during local Slack-app testing.
        """
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        _write_session_jsonl(
            sess_dir / "dashboard_chat-1.jsonl", title="d1", messages=[("user", "x")]
        )
        _write_session_jsonl(
            sess_dir / "taskrunner_run-1.jsonl", title="t1", messages=[("user", "y")]
        )

        orch = _make_orch()
        await _publish_home_tab(orch, "U123")

        view = orch.slack.views_publish.call_args[1]["view"]
        block_types = {b.get("type") for b in view["blocks"]}
        # No task_card anywhere — every type must be a documented views block kind
        assert "task_card" not in block_types
        # Sanity: the supported types we actually emit
        assert {"section", "actions", "divider", "header", "context"}.issuperset(block_types)
        # And the Resume button wiring is still present
        assert "mc_session_resume_dashboard:chat-1" in str(view["blocks"])
        assert "mc_session_resume_taskrunner_run-1" in str(view["blocks"])

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_collector_failure_falls_back_to_unavailable_message(
        self, _mw, _fmt, _yolo, monkeypatch
    ):
        """If the collector raises, the section degrades gracefully."""
        orch = _make_orch()
        with patch(
            "kiro_crew.slack.sessions_view._collect_recent_sessions",
            side_effect=RuntimeError("disk error"),
        ):
            await _publish_home_tab(orch, "U123")

        text = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        # Section header still present, but content shows the unavailable message
        assert "🧵 Sessions" in text
        assert "Sessions unavailable" in text
        # And the rest of the home tab still renders (no full-page fallback)
        assert "Recent Lessons" in text
        assert "Commands" in text

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_collector_failure_emits_error_sel_audit(
        self, _mw, _fmt, _yolo, monkeypatch
    ):
        """Regression for review-bot security-controls finding on rev-after-rebase.

        SEL audit must record the data-access attempt even when the collector
        raises, so a failure mode can't silently bypass the audit trail. The
        success-path audit fires INSIDE the try block AFTER the collector,
        so an exception skips it — the except branch is the only place the
        audit fires in that case.
        """
        orch = _make_orch()
        with (
            patch(
                "kiro_crew.slack.sessions_view._collect_recent_sessions",
                side_effect=RuntimeError("disk error"),
            ),
            patch("kiro_crew.slack.events.sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _publish_home_tab(orch, "U123")

        # Find the home_tab_sessions_data_access audit call(s)
        sessions_audits = [
            c
            for c in mock_sel.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "slack.home_tab_sessions_data_access"
        ]
        # Exactly one audit fires (the error-path one) — the success-path
        # audit is skipped because the collector raised before reaching it.
        assert len(sessions_audits) == 1
        kwargs = sessions_audits[0].kwargs
        assert kwargs["caller"] == "U123"
        assert kwargs["outcome"] == "error"
        assert kwargs["source"] == "slack"
        assert "collector failed" in kwargs["resources"]
        # Parity with slash + keyword: error= field carries the redacted exc.
        assert "disk error" in kwargs["error"]

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_collector_failure_redacts_credentials_in_exc_message(
        self, _mw, _fmt, _yolo, monkeypatch
    ):
        """Defense-in-depth on Home Tab error path: credential patterns in
        exception messages MUST be redacted before being written to SEL,
        mirroring the slash and keyword paths. Locks in redact-then-truncate
        ordering across all three surfaces.
        """
        orch = _make_orch()
        leaked_key = "AKIAIOSFODNN7EXAMPLE"
        with (
            patch(
                "kiro_crew.slack.sessions_view._collect_recent_sessions",
                side_effect=OSError(f"failed reading {leaked_key} from path"),
            ),
            patch("kiro_crew.slack.events.sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _publish_home_tab(orch, "U123")

        sessions_audits = [
            c
            for c in mock_sel.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "slack.home_tab_sessions_data_access"
        ]
        assert len(sessions_audits) == 1
        kwargs = sessions_audits[0].kwargs
        # Credential MUST NOT survive into the audit field
        assert leaked_key not in kwargs["error"]

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ✅",
    )
    async def test_unauthorized_user_blocked_with_denied_audit(
        self, _mw, _fmt, _yolo, tmp_path, monkeypatch
    ):
        """Regression for review-bot security-controls / authorization rule on Home Tab.

        Defense-in-depth: even though the dispatcher already gates app_home_opened
        events via is_allowed_user, the Sessions section must also enforce
        deny-by-default. An unauthorized user_id (e.g. via a future refactor that
        bypasses the dispatcher gate) must NOT see session contents and MUST emit
        a denied SEL audit. Mirrors the slash command's defense-in-depth gate.
        """
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        monkeypatch.setattr("kiro_crew.slack.sessions_view._SESSIONS_DIR", sess_dir)
        # Real session on disk so we can prove the collector was NOT called
        _write_session_jsonl(
            sess_dir / "dashboard_chat-1.jsonl",
            title="confidential triage",
            messages=[("user", "secret data")],
        )

        orch = _make_orch()
        with (
            patch("kiro_crew.slack.events.is_owner", return_value=False),
            patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
            patch("kiro_crew.slack.events.sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await _publish_home_tab(orch, "UATTACKER")

        # Find the home_tab_sessions_data_access audit call(s)
        sessions_audits = [
            c
            for c in mock_sel.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "slack.home_tab_sessions_data_access"
        ]
        # Exactly one audit fires: the denied one. Success and error
        # audits must NOT fire because the auth gate short-circuited.
        assert len(sessions_audits) == 1
        kwargs = sessions_audits[0].kwargs
        assert kwargs["caller"] == "UATTACKER"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "slack"
        assert "unauthorized" in kwargs["error"].lower()

        # Sensitive data must NOT leak into the rendered Home Tab view
        rendered = str(orch.slack.views_publish.call_args[1]["view"]["blocks"])
        assert "confidential triage" not in rendered
        assert "secret data" not in rendered
        # Section header still rendered (so the user sees "Sessions unavailable"
        # rather than no section at all — visible signal that a section exists)
        assert "🧵 Sessions" in rendered
        assert "Sessions unavailable" in rendered


class TestHomeTabCollectorConcurrency:
    """The sessions collector runs on the process-wide default executor, shared
    with history appends, cron store writes and session storage. A burst of tab
    opens must not put N multi-MB scans in it at once."""

    @staticmethod
    def _tracking_collector(observed: list[int]):
        """Stand in for the collector, recording how many run at once."""
        live = 0

        async def collect(*_a, **_kw):
            nonlocal live
            live += 1
            observed.append(live)
            # Yield so a second caller would interleave here if nothing gated it.
            await asyncio.sleep(0)
            live -= 1
            return []

        return collect

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ok",
    )
    async def test_a_burst_of_publishes_runs_one_collector_at_a_time(
        self, _mw, _fmt, _yolo, monkeypatch
    ):
        monkeypatch.setattr(events_mod, "is_owner", lambda _: True)
        monkeypatch.setattr(events_mod, "is_allowed_user", lambda _: True)
        monkeypatch.setattr(events_mod, "_home_tab_collect_sem", None, raising=False)
        observed: list[int] = []
        monkeypatch.setattr(
            events_mod,
            "_collect_recent_sessions_off_loop",
            self._tracking_collector(observed),
        )

        await asyncio.gather(*(
            _publish_home_tab(_make_orch(), f"U{i}") for i in range(6)
        ))

        assert observed, "the collector never ran"
        assert max(observed) == 1, f"collectors overlapped: {observed}"

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ok",
    )
    async def test_every_publish_still_completes(self, _mw, _fmt, _yolo, monkeypatch):
        """Preservation: the gate serializes the scan, it does not drop a tab."""
        monkeypatch.setattr(events_mod, "is_owner", lambda _: True)
        monkeypatch.setattr(events_mod, "is_allowed_user", lambda _: True)
        monkeypatch.setattr(events_mod, "_home_tab_collect_sem", None, raising=False)
        observed: list[int] = []
        monkeypatch.setattr(
            events_mod,
            "_collect_recent_sessions_off_loop",
            self._tracking_collector(observed),
        )
        orchs = [_make_orch() for _ in range(4)]

        await asyncio.gather(*(
            _publish_home_tab(o, f"U{i}") for i, o in enumerate(orchs)
        ))

        assert len(observed) == 4
        for o in orchs:
            o.slack.views_publish.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("kiro_crew.slack.events.is_yolo_mode", return_value=False)
    @patch("kiro_crew.slack.events.format_schedule", return_value="every 5m")
    @patch(
        "kiro_crew.sso_status.get_sso_status_line",
        new_callable=AsyncMock,
        return_value="*SSO:* ok",
    )
    async def test_a_failing_collector_does_not_strand_the_gate(
        self, _mw, _fmt, _yolo, monkeypatch
    ):
        """A raising scan must release the gate, or the first failure wedges
        every later Home Tab open."""
        monkeypatch.setattr(events_mod, "is_owner", lambda _: True)
        monkeypatch.setattr(events_mod, "is_allowed_user", lambda _: True)
        monkeypatch.setattr(events_mod, "_home_tab_collect_sem", None, raising=False)

        async def boom(*_a, **_kw):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(events_mod, "_collect_recent_sessions_off_loop", boom)
        orch = _make_orch()
        await _publish_home_tab(orch, "U1")

        observed: list[int] = []
        monkeypatch.setattr(
            events_mod,
            "_collect_recent_sessions_off_loop",
            self._tracking_collector(observed),
        )
        await asyncio.wait_for(_publish_home_tab(_make_orch(), "U2"), timeout=5)

        assert observed == [1]

    def test_the_gate_is_not_bound_at_import(self):
        """Created lazily: a module-level Semaphore would bind to whichever loop
        was current at import, not the gateway's."""
        gate = getattr(events_mod, "_home_tab_collect_sem", None)
        assert gate is None or isinstance(gate, asyncio.Semaphore)
