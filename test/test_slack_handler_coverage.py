"""Coverage tests for :mod:`kiro_crew.slack.handler`.

Focus is the command surface that ``test_slack_handler.py`` leaves untouched:
``!bang`` slash-command dispatch (every branch of ``_handle_slash_command``),
the path-independent keyword commands, the small command helpers
(``spawn``/``cron``/``task run``/``sessions``), the agent-name resolution chain,
the privacy modifiers, and the guard/permission rejection paths in
``handle_interaction``.

Everything runs in-process against ``MockSlackClient`` and ``MagicMock``
service doubles — no network, no subprocesses, no real Slack.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import MockSlackClient
from kiro_crew.cron import CronJob, CronSchedule, CronStoreBusy
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler as h


# ──────────────────────────────────────────────────────────────────────
# state hygiene
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_handler_state():
    """Clear every module-level cache the handler mutates.

    handler.py keeps its per-thread overrides, approvals and voice settings in
    module globals, so a leaked entry would make a later test order-dependent.
    """

    def _reset() -> None:
        h._pending_approvals.clear()
        h._linked_approvals.clear()
        h._trusted_sessions.clear()
        h._thread_agents.clear()
        h._thread_projects.clear()
        h._hydrated_sessions.clear()
        h._thread_temporary.clear()
        h._thread_incognito.clear()
        h._titled_threads.clear()
        h._review_drafts.clear()
        h._vc.sessions.clear()
        h._vc.voices.clear()
        h._vc.engines.clear()
        h._vc.rates.clear()
        h._vc.pitches.clear()
        h._vc.global_enabled = False
        h._cached_default_agent = None
        h._dashboard_state = None
        h._orch_cfg = None
        h._tracking_channels = set()

    _reset()
    yield
    _reset()


@pytest.fixture()
def owner(monkeypatch):
    """Make ``U1`` the owner (and therefore an allowed user)."""
    monkeypatch.setattr(h, "_owner_id", "U1")
    return "U1"


@pytest.fixture()
def slack():
    return MockSlackClient()


@pytest.fixture()
def sessions():
    """SessionManager double: only the members the command paths touch."""
    sm = MagicMock()
    sm.remove = AsyncMock()
    sm.destroy = AsyncMock()
    sm.stop_turn = AsyncMock(return_value="soft")
    sm.try_acquire = AsyncMock(return_value=False)
    sm.has_session = MagicMock(return_value=False)
    sm.set_slack_link = MagicMock()
    sm.set_approval_policy = MagicMock()
    sm._session_map = None
    return sm


def _texts(slack_client: MockSlackClient) -> str:
    """Concatenate every text-bearing action so assertions read simply."""
    return "\n".join(
        str(a[1].get("text") or "")
        for a in slack_client.actions
        if a[0] in ("post", "update", "blocks", "ephemeral")
    )


def _reply(value: str | None) -> str:
    """Assert a handler returned a reply and narrow it to ``str``."""
    assert value is not None
    return value


async def _slash(cmd, slack_client, sessions_double, *, user="U1", log=None, session="t1"):
    return await h._handle_slash_command(
        cmd, slack_client, sessions_double, "C1", "t1", "msg1", session, user, log
    )


# ──────────────────────────────────────────────────────────────────────
# !yolo
# ──────────────────────────────────────────────────────────────────────
class TestYoloCommand:
    @pytest.mark.asyncio
    async def test_status_when_off(self, slack, sessions, owner):
        assert await _slash("!yolo", slack, sessions) == ""
        assert "OFF" in _texts(slack)

    @pytest.mark.asyncio
    async def test_on_then_status_then_already_on(self, slack, sessions, owner):
        await _slash("!yolo on", slack, sessions)
        assert h.is_yolo_mode() is True
        assert "enabled" in _texts(slack)

        slack.actions.clear()
        await _slash("!yolo on", slack, sessions)
        assert "already on" in _texts(slack)

        slack.actions.clear()
        await _slash("!yolo", slack, sessions)
        assert "ON" in _texts(slack)

    @pytest.mark.asyncio
    async def test_off_when_active_and_when_already_off(self, slack, sessions, owner):
        await _slash("!yolo on", slack, sessions)
        slack.actions.clear()
        await _slash("!yolo off", slack, sessions)
        assert h.is_yolo_mode() is False
        assert "disabled" in _texts(slack)

        slack.actions.clear()
        await _slash("!yolo off", slack, sessions)
        assert "already off" in _texts(slack)

    @pytest.mark.asyncio
    async def test_renew_requires_active_grant(self, slack, sessions, owner):
        await _slash("!yolo renew", slack, sessions)
        assert "not active" in _texts(slack)

    @pytest.mark.asyncio
    async def test_renew_when_active(self, slack, sessions, owner):
        await _slash("!yolo on", slack, sessions)
        slack.actions.clear()
        await _slash("!yolo renew", slack, sessions)
        assert "renewed" in _texts(slack)


# ──────────────────────────────────────────────────────────────────────
# !stop
# ──────────────────────────────────────────────────────────────────────
class TestStopCommand:
    @pytest.mark.asyncio
    async def test_no_session(self, slack, sessions, owner):
        sessions.has_session.return_value = False
        assert await _slash("!stop", slack, sessions) == ""
        assert "Nothing running." in _texts(slack)

    @pytest.mark.asyncio
    async def test_soft_stop_posts_stopped(self, slack, sessions, owner):
        sessions.has_session.return_value = True

        async def _stop(key, *, force=False, on_soft=None, on_hard=None):
            await on_soft()
            return "soft"

        sessions.stop_turn = AsyncMock(side_effect=_stop)
        await _slash("!stop", slack, sessions)
        assert "Execution stopped." in _texts(slack)
        assert any(a[0] == "ephemeral" for a in slack.actions)

    @pytest.mark.asyncio
    async def test_hard_stop_reports_session_reset(self, slack, sessions, owner):
        sessions.has_session.return_value = True

        async def _stop(key, *, force=False, on_soft=None, on_hard=None):
            await on_hard()
            return "hard"

        sessions.stop_turn = AsyncMock(side_effect=_stop)
        await _slash("!stop", slack, sessions)
        assert "session reset" in _texts(slack)

    @pytest.mark.asyncio
    async def test_idle_outcome_dismisses_stopping(self, slack, sessions, owner):
        sessions.has_session.return_value = True
        sessions.stop_turn = AsyncMock(return_value="idle")
        await _slash("!stop", slack, sessions)
        assert "Nothing running." in _texts(slack)


# ──────────────────────────────────────────────────────────────────────
# !voice
# ──────────────────────────────────────────────────────────────────────
class TestVoiceCommand:
    @pytest.mark.asyncio
    async def test_on_registers_session(self, slack, sessions, owner):
        await _slash("!voice on", slack, sessions)
        assert "t1" in h._vc.sessions
        assert "Voice ON" in _texts(slack)

    @pytest.mark.asyncio
    async def test_off_clears_all_per_session_settings(self, slack, sessions, owner):
        await _slash("!voice Joanna", slack, sessions)
        await _slash("!voice speed 90%", slack, sessions)
        assert h._vc.voices["t1"] == "Joanna"
        slack.actions.clear()
        await _slash("!voice off", slack, sessions)
        assert "t1" not in h._vc.sessions
        assert "t1" not in h._vc.voices
        assert "t1" not in h._vc.rates
        assert "Voice OFF" in _texts(slack)

    @pytest.mark.asyncio
    async def test_global_toggles(self, slack, sessions, owner):
        await _slash("!voice global", slack, sessions)
        assert h._vc.global_enabled is True
        await _slash("!voice global", slack, sessions)
        assert h._vc.global_enabled is False

    @pytest.mark.asyncio
    async def test_invalid_engine_rejected(self, slack, sessions, owner):
        await _slash("!voice engine ploly", slack, sessions)
        assert "Invalid engine" in _texts(slack)
        assert "t1" not in h._vc.engines

    @pytest.mark.asyncio
    async def test_valid_engine_accepted(self, slack, sessions, owner):
        from kiro_crew.voice_reply import VALID_ENGINES

        engine = sorted(VALID_ENGINES)[0]
        await _slash(f"!voice engine {engine}", slack, sessions)
        assert h._vc.engines["t1"] == engine
        assert "t1" in h._vc.sessions

    @pytest.mark.asyncio
    async def test_pitch_is_validated(self, slack, sessions, owner):
        await _slash("!voice pitch +10%", slack, sessions)
        assert "t1" in h._vc.pitches
        assert "Pitch set to" in _texts(slack)

    @pytest.mark.asyncio
    async def test_bare_voice_shows_status(self, slack, sessions, owner):
        await _slash("!voice", slack, sessions)
        body = _texts(slack)
        assert "Voice:" in body and "Engine:" in body


# ──────────────────────────────────────────────────────────────────────
# !agent / !ta
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture()
def agents_dir(tmp_path, monkeypatch):
    """An isolated ``~/.kiro/agents`` holding one ``demo`` spec."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / "demo.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    monkeypatch.setattr(h, "kiro_agents_dir", lambda: d)
    monkeypatch.setattr(h, "_iter_cc_agent_names", lambda cc_plugins_dir=None: iter(()))
    return d


class TestAgentCommand:
    @pytest.mark.asyncio
    async def test_bare_agent_reports_current(self, slack, sessions, owner, agents_dir):
        await _slash("!agent", slack, sessions)
        assert "Current agent" in _texts(slack)

    @pytest.mark.asyncio
    async def test_too_many_args_shows_usage(self, slack, sessions, owner, agents_dir):
        await _slash("!agent a b", slack, sessions)
        assert "Usage:" in _texts(slack)

    @pytest.mark.asyncio
    async def test_unknown_agent_lists_available(self, slack, sessions, owner, agents_dir):
        await _slash("!agent nope", slack, sessions)
        body = _texts(slack)
        assert "Unknown agent" in body and "demo" in body

    @pytest.mark.asyncio
    async def test_switch_persists_and_drops_session(self, slack, sessions, owner, agents_dir):
        await _slash("!agent demo", slack, sessions)
        assert h._get_default_agent() == "demo"
        sessions.remove.assert_awaited_once_with("t1")
        assert "Switched to agent" in _texts(slack)

    @pytest.mark.asyncio
    async def test_off_resets_default(self, slack, sessions, owner, agents_dir):
        await _slash("!agent demo", slack, sessions)
        slack.actions.clear()
        await _slash("!agent off", slack, sessions)
        assert h._get_default_agent() == ""
        assert "Reset to default agent." in _texts(slack)

    @pytest.mark.asyncio
    async def test_write_failure_surfaces_error(self, slack, sessions, owner, agents_dir, monkeypatch):
        def _boom(_name):
            raise ValueError("read-only config")

        monkeypatch.setattr(h, "_set_default_agent", _boom)
        await _slash("!agent demo", slack, sessions)
        assert "read-only config" in _texts(slack)


class TestThreadAgentCommand:
    @pytest.mark.asyncio
    async def test_bare_ta_without_override(self, slack, sessions, owner, agents_dir):
        await _slash("!ta", slack, sessions)
        assert "No thread agent set." in _texts(slack)

    @pytest.mark.asyncio
    async def test_bare_ta_with_override(self, slack, sessions, owner, agents_dir):
        h._thread_agents["t1"] = "demo"
        await _slash("!ta", slack, sessions)
        assert "Thread agent: *demo*" in _texts(slack)

    @pytest.mark.asyncio
    async def test_set_persists_to_conversation_log(self, slack, sessions, owner, agents_dir):
        log = MagicMock()
        await _slash("!ta demo", slack, sessions, log=log)
        assert h._thread_agents["t1"] == "demo"
        log.update_metadata.assert_called_once_with("t1", {"agent": "demo"})

    @pytest.mark.asyncio
    async def test_off_clears_and_survives_log_failure(self, slack, sessions, owner, agents_dir):
        h._thread_agents["t1"] = "demo"
        log = MagicMock()
        log.update_metadata.side_effect = OSError("disk full")
        await _slash("!ta off", slack, sessions, log=log)
        assert "t1" not in h._thread_agents
        assert "Thread agent reset." in _texts(slack)

    @pytest.mark.asyncio
    async def test_unknown_thread_agent(self, slack, sessions, owner, agents_dir):
        await _slash("!ta nope", slack, sessions)
        assert "Unknown agent" in _texts(slack)


# ──────────────────────────────────────────────────────────────────────
# !project
# ──────────────────────────────────────────────────────────────────────
class TestProjectCommand:
    @pytest.mark.asyncio
    async def test_bare_without_project(self, slack, sessions, owner):
        await _slash("!project", slack, sessions)
        assert "No project set." in _texts(slack)

    @pytest.mark.asyncio
    async def test_bare_with_project(self, slack, sessions, owner):
        h._thread_projects["t1"] = "/srv/app"
        await _slash("!project", slack, sessions)
        assert "/srv/app" in _texts(slack)

    @pytest.mark.asyncio
    async def test_clear(self, slack, sessions, owner):
        h._thread_projects["t1"] = "/srv/app"
        await _slash("!project off", slack, sessions)
        assert "t1" not in h._thread_projects
        assert "Thread project cleared." in _texts(slack)

    @pytest.mark.asyncio
    async def test_sensitive_path_denied(self, slack, sessions, owner, monkeypatch, tmp_path):
        monkeypatch.setattr(h, "is_sensitive_path", lambda p: True)
        await _slash(f"!project {tmp_path}", slack, sessions)
        assert "sensitive path" in _texts(slack)
        assert "t1" not in h._thread_projects

    @pytest.mark.asyncio
    async def test_non_directory_rejected(self, slack, sessions, owner, tmp_path):
        missing = tmp_path / "nope"
        await _slash(f"!project {missing}", slack, sessions)
        assert "Not a directory" in _texts(slack)

    @pytest.mark.asyncio
    async def test_valid_dir_lists_discovered_agents(self, slack, sessions, owner, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        (kiro / "local.agent-spec.json").write_text("{}", encoding="utf-8")
        log = MagicMock()
        await _slash(f"!project {tmp_path}", slack, sessions, log=log)
        body = _texts(slack)
        assert "Agents found" in body and "local" in body
        assert h._thread_projects["t1"] == str(Path(tmp_path).resolve())
        log.update_metadata.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# !dashboard / !link-to-dashboard / !allowlist / !title
# ──────────────────────────────────────────────────────────────────────
class TestMiscSlashCommands:
    @pytest.mark.asyncio
    async def test_dashboard_rejects_bad_duration(self, slack, sessions, owner):
        await _slash("!dashboard 5furlongs", slack, sessions)
        assert "Usage:" in _texts(slack)

    @pytest.mark.asyncio
    async def test_dashboard_link_sent(self, slack, sessions, owner, monkeypatch):
        import kiro_crew.slack.allowlist as al

        monkeypatch.setattr(al, "send_dashboard_link", AsyncMock(return_value="https://x/y"))
        await _slash("!dashboard 2h", slack, sessions)
        assert "Dashboard link sent" in _texts(slack)

    @pytest.mark.asyncio
    async def test_dashboard_link_failure(self, slack, sessions, owner, monkeypatch):
        import kiro_crew.slack.allowlist as al

        monkeypatch.setattr(al, "send_dashboard_link", AsyncMock(return_value=""))
        await _slash("!dashboard", slack, sessions)
        assert "Failed to send dashboard link." in _texts(slack)

    @pytest.mark.asyncio
    async def test_link_to_dashboard_denies_stranger(self, slack, sessions, owner):
        await _slash("!link-to-dashboard", slack, sessions, user="UZZZ")
        assert "Not authorized." in _texts(slack)

    @pytest.mark.asyncio
    async def test_link_to_dashboard_without_dashboard(self, slack, sessions, owner):
        await _slash("!link-to-dashboard", slack, sessions)
        assert "Dashboard not available." in _texts(slack)

    @pytest.mark.asyncio
    async def test_link_to_dashboard_outside_thread(self, slack, sessions, owner, monkeypatch):
        monkeypatch.setattr(h, "_dashboard_state", MagicMock(get_or_create_slot=MagicMock()))
        # reply_ts == msg_ts means the message is not inside a thread.
        await h._handle_slash_command(
            "!link-to-dashboard", slack, sessions, "C1", "msg1", "msg1", "t1", "U1", None
        )
        assert "inside a thread" in _texts(slack)

    @pytest.mark.asyncio
    async def test_link_to_dashboard_empty_thread(self, slack, sessions, owner, monkeypatch):
        monkeypatch.setattr(h, "_dashboard_state", MagicMock(get_or_create_slot=MagicMock()))
        import kiro_crew.slack.interactions as inter

        monkeypatch.setattr(inter, "_import_thread_to_slot", AsyncMock(return_value=None))
        await _slash("!link-to-dashboard", slack, sessions)
        assert "Could not fetch thread history." in _texts(slack)

    @pytest.mark.asyncio
    async def test_link_to_dashboard_imports_thread(self, slack, sessions, owner, monkeypatch):
        monkeypatch.setattr(h, "_dashboard_state", MagicMock(get_or_create_slot=MagicMock()))
        import kiro_crew.slack.interactions as inter

        slot = MagicMock(key="slot-3", messages=["a", "b", "c"])
        monkeypatch.setattr(inter, "_import_thread_to_slot", AsyncMock(return_value=slot))
        await _slash("!link-to-dashboard", slack, sessions)
        body = _texts(slack)
        assert "Imported 3 messages" in body and "slot-3" in body

    @pytest.mark.asyncio
    async def test_allowlist_is_disabled(self, slack, sessions, owner):
        assert await _slash("!allowlist", slack, sessions) == ""
        assert "Multi-user access is disabled" in _texts(slack)

    @pytest.mark.asyncio
    async def test_title_sets_thread_title(self, slack, sessions, owner):
        log = MagicMock()
        await _slash("!title Release notes", slack, sessions, log=log)
        titles = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert titles and titles[0][1]["title"] == "Release notes"
        assert h._titled_threads["t1"] == "manual"
        log.set_title.assert_called_once_with("t1", "Release notes")

    @pytest.mark.asyncio
    async def test_title_without_text_shows_usage(self, slack, sessions, owner):
        await _slash("!title", slack, sessions)
        assert "Usage: `!title <text>`" in _texts(slack)

    @pytest.mark.asyncio
    async def test_title_survives_log_failure(self, slack, sessions, owner):
        log = MagicMock()
        log.set_title.side_effect = OSError("nope")
        await _slash("!title Anything", slack, sessions, log=log)
        assert any(a[0] == "set_thread_title" for a in slack.actions)


# ──────────────────────────────────────────────────────────────────────
# !channel
# ──────────────────────────────────────────────────────────────────────
class TestChannelCommand:
    @pytest.mark.asyncio
    async def test_non_owner_denied(self, slack, sessions, owner):
        await _slash("!channel always", slack, sessions, user="UZZZ")
        assert "Only the bot owner" in _texts(slack)

    @pytest.mark.asyncio
    async def test_status(self, slack, sessions, owner):
        await _slash("!channel", slack, sessions)
        assert "activation" in _texts(slack)

    @pytest.mark.asyncio
    async def test_invalid_mode(self, slack, sessions, owner):
        await _slash("!channel sometimes", slack, sessions)
        assert "Invalid mode" in _texts(slack)

    @pytest.mark.asyncio
    async def test_valid_mode_persists(self, slack, sessions, owner):
        from kiro_crew.config.loader import KiroCrewConfig, config_path

        await _slash("!channel observe", slack, sessions)
        assert "activation set to *observe*" in _texts(slack)
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert saved["slack"]["channels"]["C1"]["activation"] == "observe"
        assert KiroCrewConfig.load().channel_config("C1").activation == "observe"

    @pytest.mark.asyncio
    async def test_agent_subcommand_needs_argument(self, slack, sessions, owner):
        await _slash("!channel agent", slack, sessions)
        assert "Usage: `!channel agent" in _texts(slack)

    @pytest.mark.asyncio
    async def test_agent_subcommand_unknown_name(self, slack, sessions, owner, agents_dir):
        await _slash("!channel agent nope", slack, sessions)
        assert "Unknown agent" in _texts(slack)

    @pytest.mark.asyncio
    async def test_agent_subcommand_sets_and_clears(self, slack, sessions, owner, agents_dir):
        from kiro_crew.config.loader import config_path

        await _slash("!channel agent demo", slack, sessions)
        assert json.loads(config_path().read_text(encoding="utf-8"))["slack"]["channels"]["C1"][
            "agent"
        ] == "demo"
        slack.actions.clear()
        await _slash("!channel agent off", slack, sessions)
        assert "default" in _texts(slack)


# ──────────────────────────────────────────────────────────────────────
# maybe_handle_keyword_command
# ──────────────────────────────────────────────────────────────────────
class TestKeywordCommands:
    @pytest.mark.asyncio
    async def test_plain_text_is_not_a_command(self, slack, sessions, owner):
        handled = await h.maybe_handle_keyword_command(
            "how do I rebase?", slack, sessions, "C1", "t1", "msg1", "t1", "U1"
        )
        assert handled is False
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_sessions_keyword_allowed(self, slack, sessions, owner, monkeypatch):
        monkeypatch.setattr(h, "_collect_recent_sessions", lambda s, limit=0: [])
        handled = await h.maybe_handle_keyword_command(
            "sessions", slack, sessions, "C1", "t1", "msg1", "t1", "U1"
        )
        assert handled is True
        assert "_No recent sessions._" in _texts(slack)

    @pytest.mark.asyncio
    async def test_sessions_keyword_denied_for_stranger(self, slack, sessions, owner):
        handled = await h.maybe_handle_keyword_command(
            "sessions", slack, sessions, "C1", "t1", "msg1", "t1", "UZZZ"
        )
        assert handled is True
        assert "_Permission denied._" in _texts(slack)

    @pytest.mark.asyncio
    async def test_sessions_branch_can_be_opted_out(self, slack, sessions, owner):
        handled = await h.maybe_handle_keyword_command(
            "sessions", slack, sessions, "C1", "t1", "msg1", "t1", "U1", handle_sessions=False
        )
        assert handled is False

    @pytest.mark.asyncio
    async def test_spawn_keyword_saves_turn(self, slack, sessions, owner, monkeypatch):
        saver = AsyncMock()
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", saver)
        mgr = MagicMock(max_concurrent=4)
        mgr.spawn.return_value = MagicMock(id="a1")
        log = MagicMock()
        handled = await h.maybe_handle_keyword_command(
            "spawn audit the docs",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            log,
            subagent_manager=mgr,
        )
        assert handled is True
        assert "Spawned subagent" in _texts(slack)
        saver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_keyword_skips_log_when_incognito(self, slack, sessions, owner, monkeypatch):
        saver = AsyncMock()
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", saver)
        h._mark_incognito("t1")
        mgr = MagicMock(max_concurrent=4)
        mgr.spawn.return_value = MagicMock(id="a1")
        await h.maybe_handle_keyword_command(
            "spawn x",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            MagicMock(),
            subagent_manager=mgr,
        )
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_keyword_saves_turn(self, slack, sessions, owner, monkeypatch):
        saver = AsyncMock()
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", saver)
        runner = MagicMock(running=True)
        await h.maybe_handle_keyword_command(
            "task run /tmp/spec.md",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            MagicMock(),
            task_runner=runner,
        )
        saver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cron_keyword_saves_turn(self, slack, sessions, owner, monkeypatch):
        saver = AsyncMock()
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", saver)
        svc = MagicMock()
        svc.list_jobs.return_value = []
        await h.maybe_handle_keyword_command(
            "cron list",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            MagicMock(),
            cron_service=svc,
        )
        saver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_keyword(self, slack, sessions, owner):
        runner = MagicMock(running=True)
        handled = await h.maybe_handle_keyword_command(
            "task run /tmp/spec.md",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            task_runner=runner,
        )
        assert handled is True
        assert "already running" in _texts(slack)

    @pytest.mark.asyncio
    async def test_cron_keyword(self, slack, sessions, owner, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        svc._jobs = []
        handled = await h.maybe_handle_keyword_command(
            "cron list", slack, sessions, "C1", "t1", "msg1", "t1", "U1", cron_service=svc
        )
        assert handled is True
        assert "No cron jobs scheduled." in _texts(slack)


# ──────────────────────────────────────────────────────────────────────
# spawn helpers
# ──────────────────────────────────────────────────────────────────────
class TestSpawnHelpers:
    def test_no_prefix_is_ignored(self):
        assert h._handle_spawn_command("please spawn later", MagicMock()) is None

    def test_bg_prefix_accepted(self):
        mgr = MagicMock(max_concurrent=2)
        mgr.spawn.return_value = MagicMock(id="z9")
        assert "z9" in _reply(h._handle_spawn_command("bg do it", mgr))

    def test_empty_task_returns_none(self):
        assert h._handle_spawn_command("spawn   ", MagicMock()) is None

    def test_list_with_no_agents(self):
        assert h._do_spawn("list", MagicMock(running=[])) == "No subagents running."

    def test_status_lists_running_agents(self):
        agent = MagicMock(id="a7", started=time.time() - 5, task="reindex the corpus")
        out = _reply(h._do_spawn("status", MagicMock(running=[agent])))
        assert "a7" in out and "reindex the corpus" in out

    def test_capacity_reached(self):
        mgr = MagicMock(max_concurrent=3)
        mgr.spawn.return_value = None
        assert "capacity reached (3)" in _reply(h._do_spawn("work", mgr))


# ──────────────────────────────────────────────────────────────────────
# cron helpers
# ──────────────────────────────────────────────────────────────────────
def _job(job_id: str = "j1", *, name: str = "nightly") -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do the thing",
        schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
    )


class TestCronHelpers:
    @pytest.mark.asyncio
    async def test_non_cron_text_ignored(self):
        assert await h._handle_cron_command("hello", MagicMock(), "C1", "t1") is None

    @pytest.mark.asyncio
    async def test_empty_list(self):
        svc = MagicMock()
        svc.list_jobs.return_value = []
        assert await h._handle_cron_command("cron list", svc, "C1", "t1") == (
            "No cron jobs scheduled."
        )

    @pytest.mark.asyncio
    async def test_action_without_job_id_returns_none(self):
        assert await h._handle_cron_command("cron remove", MagicMock(), "C1", "t1") is None

    @pytest.mark.asyncio
    async def test_unknown_action_returns_none(self):
        assert await h._handle_cron_command("cron frobnicate j1", MagicMock(), "C1", "t1") is None

    @pytest.mark.asyncio
    async def test_remove_found_and_missing(self):
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        assert "Removed cron job" in _reply(await h._handle_cron_command("cron remove j1", svc, "C", "t"))
        svc.remove_job_async = AsyncMock(return_value=False)
        assert "not found" in _reply(await h._handle_cron_command("cron remove j1", svc, "C", "t"))

    @pytest.mark.asyncio
    async def test_remove_busy_store(self):
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy())
        assert "busy" in _reply(await h._handle_cron_command("cron remove j1", svc, "C", "t"))

    @pytest.mark.asyncio
    async def test_pause_and_resume(self):
        svc = MagicMock()
        svc.enable_job_async = AsyncMock(return_value=True)
        assert "Paused" in _reply(await h._handle_cron_command("cron pause j1", svc, "C", "t"))
        assert "Resumed" in _reply(await h._handle_cron_command("cron resume j1", svc, "C", "t"))
        svc.enable_job_async = AsyncMock(return_value=False)
        assert "not found" in _reply(await h._handle_cron_command("cron pause j1", svc, "C", "t"))
        assert "not found" in _reply(await h._handle_cron_command("cron resume j1", svc, "C", "t"))

    @pytest.mark.asyncio
    async def test_pause_resume_busy_store(self):
        svc = MagicMock()
        svc.enable_job_async = AsyncMock(side_effect=CronStoreBusy())
        assert "busy" in _reply(await h._handle_cron_command("cron pause j1", svc, "C", "t"))
        assert "busy" in _reply(await h._handle_cron_command("cron resume j1", svc, "C", "t"))

    @pytest.mark.asyncio
    async def test_remove_all_empty(self):
        svc = MagicMock()
        svc.list_jobs.return_value = []
        assert await h._remove_all_jobs(svc) == "No cron jobs to remove."

    @pytest.mark.asyncio
    async def test_remove_all_reports_each_job(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1"), _job("j2")]
        svc.remove_jobs = AsyncMock()
        out = await h._remove_all_jobs(svc)
        assert "Removed 2 cron job(s)" in out and "`j1`" in out and "`j2`" in out

    @pytest.mark.asyncio
    async def test_remove_all_busy_store(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job()]
        svc.remove_jobs = AsyncMock(side_effect=CronStoreBusy())
        assert "busy" in (await h._remove_all_jobs(svc))

    @pytest.mark.asyncio
    async def test_remove_all_via_cron_remove_all(self):
        svc = MagicMock()
        svc.list_jobs.return_value = []
        out = await h._handle_cron_command("cron remove all", svc, "C", "t")
        assert out == "No cron jobs to remove."


# ──────────────────────────────────────────────────────────────────────
# task-runner helper
# ──────────────────────────────────────────────────────────────────────
class TestRunHelper:
    @pytest.mark.asyncio
    async def test_non_matching_text(self, slack):
        assert await h._handle_run_command("deploy it", MagicMock(), slack, "C", "t") is None

    @pytest.mark.asyncio
    async def test_bare_prefix_without_arg(self, slack):
        assert await h._handle_run_command("task run", MagicMock(), slack, "C", "t") is None
        assert await h._handle_run_command("task run   ", MagicMock(), slack, "C", "t") is None

    @pytest.mark.asyncio
    async def test_project_run_alias_maps_to_task_run(self, slack):
        runner = MagicMock(running=False)
        runner.status.return_value = {"running": False, "status": ""}
        assert await h._handle_run_command("project run status", runner, slack, "C", "t") == (
            "No task running."
        )

    @pytest.mark.asyncio
    async def test_status_when_running(self, slack):
        runner = MagicMock(running=True)
        runner.status.return_value = {
            "running": True,
            "status": "executing",
            "completed": 2,
            "steps": 5,
            "current_step": 3,
        }
        out = _reply(await h._handle_run_command("task run status", runner, slack, "C", "t"))
        assert "2/5" in out and "executing" in out

    @pytest.mark.asyncio
    async def test_cancel_paths(self, slack):
        idle = MagicMock(running=False)
        assert await h._handle_run_command("task run cancel", idle, slack, "C", "t") == (
            "No task running."
        )
        busy = MagicMock(running=True)
        assert "cancelled" in _reply(await h._handle_run_command("task run cancel", busy, slack, "C", "t"))
        busy.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_spec_file(self, slack, tmp_path):
        runner = MagicMock(running=False)
        out = _reply(
            await h._handle_run_command(f"task run {tmp_path / 'absent.md'}", runner, slack, "C", "t")
        )
        assert "Spec file not found" in out

    @pytest.mark.asyncio
    async def test_start_failure_is_reported(self, slack, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# task", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock(side_effect=RuntimeError("no runner"))
        out = _reply(await h._handle_run_command(f"task run {spec}", runner, slack, "C", "t"))
        assert "Failed to start: no runner" in out

    @pytest.mark.asyncio
    async def test_successful_start(self, slack, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# task", encoding="utf-8")
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        out = _reply(await h._handle_run_command(f"task run {spec}", runner, slack, "C", "t"))
        assert "Task started: `spec.md`" in out


# ──────────────────────────────────────────────────────────────────────
# sessions helper
# ──────────────────────────────────────────────────────────────────────
class TestSessionsHelper:
    @pytest.mark.asyncio
    async def test_collector_failure_is_audited(self, slack, monkeypatch):
        def _boom(_sessions, limit=0):
            raise OSError("history unreadable")

        monkeypatch.setattr(h, "_collect_recent_sessions", _boom)
        await h._handle_sessions_command("sessions", slack, "C1", "t1", "msg1", "t1", None)
        assert "_Sessions unavailable._" in _texts(slack)

    @pytest.mark.asyncio
    async def test_rows_render_blocks(self, slack, monkeypatch):
        monkeypatch.setattr(h, "_collect_recent_sessions", lambda s, limit=0: [{"key": "s1"}])
        monkeypatch.setattr(h, "_build_sessions_blocks", lambda rows: [{"type": "divider"}])
        await h._handle_sessions_command("sessions", slack, "C1", "t1", "msg1", "t1", None)
        assert [a for a in slack.actions if a[0] == "blocks"]


# ──────────────────────────────────────────────────────────────────────
# agent-name resolution
# ──────────────────────────────────────────────────────────────────────
class TestAgentResolution:
    def test_discover_without_project(self):
        assert h._discover_project_agents(None) == []

    def test_discover_sensitive_project(self, monkeypatch, tmp_path):
        monkeypatch.setattr(h, "is_sensitive_path", lambda p: True)
        assert h._discover_project_agents(str(tmp_path)) == []

    def test_discover_without_kiro_dir(self, tmp_path):
        assert h._discover_project_agents(str(tmp_path)) == []

    def test_discover_merges_agents_subdir(self, tmp_path):
        kiro = tmp_path / ".kiro"
        (kiro / "agents").mkdir(parents=True)
        (kiro / "a.agent-spec.json").write_text("{}", encoding="utf-8")
        (kiro / "agents" / "b.json").write_text("{}", encoding="utf-8")
        found = [p.name for p in h._discover_project_agents(str(tmp_path))]
        assert found == ["a.agent-spec.json", "b.json"]

    def test_project_agent_name_from_json(self, tmp_path, agents_dir):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        (kiro / "local.agent-spec.json").write_text(
            json.dumps({"name": "local-real"}), encoding="utf-8"
        )
        assert h._resolve_agent_name("local", str(tmp_path)) == "local-real"

    def test_project_agent_falls_back_on_bad_json(self, tmp_path, agents_dir):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        (kiro / "local.agent-spec.json").write_text("{not json", encoding="utf-8")
        assert h._resolve_agent_name("local", str(tmp_path)) == "local"

    def test_suffix_match_in_agents_dir(self, agents_dir):
        (agents_dir / "team-helper.json").write_text(
            json.dumps({"name": "team-helper"}), encoding="utf-8"
        )
        assert h._resolve_agent_name("helper") == "team-helper"

    def test_unresolvable_name_returns_none(self, agents_dir):
        assert h._resolve_agent_name("ghost") is None

    def test_bad_json_in_agents_dir_falls_back_to_stem(self, agents_dir):
        (agents_dir / "broken.json").write_text("{oops", encoding="utf-8")
        assert h._resolve_agent_name("broken") == "broken"

    def test_cc_agent_names_are_parsed(self, tmp_path):
        plug = tmp_path / "pack" / "agents"
        plug.mkdir(parents=True)
        (plug / "ok.md").write_text('---\nname: "cc-one"\n---\nbody\n', encoding="utf-8")
        (plug / "nofm.md").write_text("no frontmatter here\n", encoding="utf-8")
        (plug / "noname.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
        assert list(h._iter_cc_agent_names(tmp_path)) == ["cc-one"]
        assert h._resolve_cc_agent_name("cc-one", tmp_path) == "cc-one"
        assert h._resolve_cc_agent_name("cc-two", tmp_path) is None

    def test_cc_dir_absent(self, tmp_path):
        assert list(h._iter_cc_agent_names(tmp_path / "missing")) == []

    def test_list_all_agent_names_hides_lite_variant(self, tmp_path, monkeypatch):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "demo.json").write_text("{}", encoding="utf-8")
        (d / "kirocrew-lite.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(h, "kiro_agents_dir", lambda: d)
        monkeypatch.setattr(h, "_iter_cc_agent_names", lambda cc_plugins_dir=None: iter(["extra"]))
        out = h._list_all_agent_names()
        assert "demo" in out and "extra" in out and "kirocrew-lite" not in out

    def test_list_all_agent_names_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "kiro_agents_dir", lambda: tmp_path / "missing")
        monkeypatch.setattr(h, "_iter_cc_agent_names", lambda cc_plugins_dir=None: iter(()))
        assert h._list_all_agent_names() == "(none found)"

    def test_get_agent_for_session_prefers_thread_override(self, agents_dir):
        h._thread_agents["t1"] = "thread-agent"
        assert h._get_agent_for_session("t1") == "thread-agent"

    def test_set_default_agent_rejects_sensitive_config_path(self, monkeypatch):
        monkeypatch.setattr(h, "is_sensitive_path", lambda p: True)
        with pytest.raises(ValueError, match="sensitive path"):
            h._set_default_agent("demo")

    def test_persist_channel_config_rejects_sensitive_path(self, monkeypatch):
        monkeypatch.setattr(h, "is_sensitive_path", lambda p: True)
        with pytest.raises(ValueError, match="sensitive path"):
            h._persist_channel_config("C1", activation="always")

    def test_persist_channel_config_merges(self):
        from kiro_crew.config.loader import config_path

        h._persist_channel_config("C1", activation="always")
        h._persist_channel_config("C1", agent="demo")
        ch = json.loads(config_path().read_text(encoding="utf-8"))["slack"]["channels"]["C1"]
        assert ch == {"activation": "always", "agent": "demo"}


# ──────────────────────────────────────────────────────────────────────
# thread-override hydration
# ──────────────────────────────────────────────────────────────────────
class TestThreadOverrideHydration:
    def test_second_call_is_a_no_op(self):
        log = MagicMock()
        log.get_metadata.return_value = {}
        h._hydrate_thread_overrides("t1", log)
        h._hydrate_thread_overrides("t1", log)
        log.get_metadata.assert_called_once()

    def test_without_log_only_marks_hydrated(self):
        h._hydrate_thread_overrides("t1", None)
        assert "t1" in h._hydrated_sessions

    def test_metadata_failure_is_swallowed(self):
        log = MagicMock()
        log.get_metadata.side_effect = OSError("corrupt")
        h._hydrate_thread_overrides("t1", log)
        assert "t1" not in h._thread_agents

    def test_agent_and_project_hydrated(self):
        log = MagicMock()
        log.get_metadata.return_value = {"agent": "demo", "project": "/srv/app"}
        h._hydrate_thread_overrides("t1", log)
        assert h._thread_agents["t1"] == "demo"
        assert h._thread_projects["t1"] == "/srv/app"

    def test_sensitive_project_is_dropped(self, monkeypatch):
        monkeypatch.setattr(h, "is_sensitive_path", lambda p: True)
        log = MagicMock()
        log.get_metadata.return_value = {"project": "/home/u/.aws"}
        h._hydrate_thread_overrides("t1", log)
        assert "t1" not in h._thread_projects


# ──────────────────────────────────────────────────────────────────────
# privacy modifiers
# ──────────────────────────────────────────────────────────────────────
class TestPrivacyModifiers:
    def test_token_strippers(self):
        assert h._strip_temporary_token("hi there") == ("hi there", False)
        assert h._strip_temporary_token("!temporary  do  it") == ("do it", True)
        assert h._strip_incognito_token("hi") == ("hi", False)
        assert h._strip_incognito_token("!INCOGNITO now") == ("now", True)
        # Embedded in a larger token — must not match.
        assert h._strip_incognito_token("x!incognito")[1] is False

    @pytest.mark.asyncio
    async def test_temporary_only_returns_early(self, slack, sessions, owner):
        text, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!temporary", "!temporary", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert only is True and cmd == ""
        assert h.is_thread_temporary("t1") is True
        assert "Temporary mode ON" in _texts(slack)
        sessions.set_slack_link.assert_called_once_with("t1", "t1", "C1")

    @pytest.mark.asyncio
    async def test_incognito_only_returns_early(self, slack, sessions, owner):
        _, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!incognito", "!incognito", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert only is True and cmd == ""
        assert h.is_thread_incognito("t1") is True
        assert h._is_slack_restricted("t1") is True

    @pytest.mark.asyncio
    async def test_both_modifiers_with_remaining_text(self, slack, sessions, owner):
        text, cmd, only = await h.maybe_apply_privacy_modifiers(
            "!temporary !incognito summarize",
            "!temporary !incognito summarize",
            "t1",
            "U1",
            "C1",
            slack,
            sessions,
            "t1",
        )
        assert only is False
        assert cmd == "summarize" and text == "summarize"
        assert h.is_thread_temporary("t1") and h.is_thread_incognito("t1")

    @pytest.mark.asyncio
    async def test_no_modifier_is_passthrough(self, slack, sessions, owner):
        out = await h.maybe_apply_privacy_modifiers(
            "hello", "hello", "t1", "U1", "C1", slack, sessions, "t1"
        )
        assert out == ("hello", "hello", False)
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_repeat_application_is_idempotent(self, slack, sessions, owner):
        await h._apply_temporary_modifier("t1", "U1", "C1", slack, sessions, "t1")
        posts = len(slack.actions)
        await h._apply_temporary_modifier("t1", "U1", "C1", slack, sessions, "t1")
        assert len(slack.actions) == posts

    @pytest.mark.asyncio
    async def test_flags_are_persisted_on_the_session_map(
        self, slack, sessions, owner, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        from kiro_crew.session_map import SessionMap

        sessions._session_map = SessionMap()
        await h._apply_temporary_modifier("t1", "U1", "C1", slack, sessions, "t1")
        await h._apply_incognito_modifier("t1", "U1", "C1", slack, sessions, "t1")
        # Assert real durability rather than that set_flag was called: a FRESH
        # map must read both flags back off disk, which is the property the
        # restart path actually depends on.
        fresh = SessionMap()
        assert fresh.get_flag("t1", "temporary") is True
        assert fresh.get_flag("t1", "incognito") is True

    def test_hydrate_conv_flags_without_session_map(self, sessions):
        h._hydrate_conv_flags(sessions, "t1")
        assert not h.is_thread_temporary("t1")

    def test_conv_state_map_rejects_auto_attribute_stub(self, sessions):
        """An auto-attribute stub must NOT be mistaken for a real SessionMap.

        ``MagicMock().get_flag(...)`` returns a truthy mock, so accepting one
        here would mark every session both temporary and incognito.
        """
        sessions._session_map = MagicMock()
        assert h._conv_state_map(sessions) is None
        h._hydrate_conv_flags(sessions, "t1")
        assert not h.is_thread_temporary("t1")
        assert not h.is_thread_incognito("t1")

    def test_hydrate_conv_flags_restores_both(self, sessions, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        from kiro_crew.session_map import SessionMap

        sm = SessionMap()
        sm.set_flag("t1", "temporary", True)
        sm.set_flag("t1", "incognito", True)
        sessions._session_map = sm
        h._hydrate_conv_flags(sessions, "t1")
        assert h.is_thread_temporary("t1") and h.is_thread_incognito("t1")

    def test_temporary_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(h, "_THREAD_TEMPORARY_MAX", 2)
        for key in ("a", "b", "c"):
            h._mark_temporary(key)
        assert not h.is_thread_temporary("a")
        assert h.is_thread_temporary("c")

    def test_incognito_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(h, "_THREAD_INCOGNITO_MAX", 1)
        h._mark_incognito("a")
        h._mark_incognito("b")
        assert not h.is_thread_incognito("a")
        assert h.is_thread_incognito("b")

    def test_titled_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(h, "_TITLED_THREADS_MAX", 1)
        h._mark_titled("a", "auto")
        h._mark_titled("b", "manual")
        assert "a" not in h._titled_threads


# ──────────────────────────────────────────────────────────────────────
# review drafts
# ──────────────────────────────────────────────────────────────────────
class TestReviewDrafts:
    def test_missing_key(self):
        assert h._review_drafts_get("nope") == ("", "")
        assert h._review_drafts_pop("nope") == ("", "")

    def test_roundtrip(self):
        h._review_drafts_set("k", "draft body", "U1")
        assert h._review_drafts_get("k") == ("draft body", "U1")
        assert h._review_drafts_pop("k") == ("draft body", "U1")
        assert h._review_drafts_get("k") == ("", "")

    def test_expired_entry_is_evicted_on_get(self, monkeypatch):
        monkeypatch.setattr(h, "_REVIEW_DRAFT_TTL", -1)
        h._review_drafts["k"] = ("body", "U1", time.monotonic())
        assert h._review_drafts_get("k") == ("", "")
        assert "k" not in h._review_drafts

    def test_expired_entry_is_dropped_on_pop(self, monkeypatch):
        monkeypatch.setattr(h, "_REVIEW_DRAFT_TTL", -1)
        h._review_drafts["k"] = ("body", "U1", time.monotonic())
        assert h._review_drafts_pop("k") == ("", "")

    def test_set_evicts_expired_and_oldest(self, monkeypatch):
        monkeypatch.setattr(h, "_REVIEW_DRAFT_MAX", 2)
        now = time.monotonic()
        h._review_drafts["stale"] = ("x", "U1", now - h._REVIEW_DRAFT_TTL - 10)
        h._review_drafts["old"] = ("y", "U1", now - 100)
        h._review_drafts["new"] = ("z", "U1", now)
        h._review_drafts_set("fresh", "w", "U1")
        assert "stale" not in h._review_drafts
        assert "old" not in h._review_drafts
        assert "fresh" in h._review_drafts


# ──────────────────────────────────────────────────────────────────────
# handle_interaction guards
# ──────────────────────────────────────────────────────────────────────
class TestHandleInteractionGuards:
    @pytest.mark.asyncio
    async def test_missing_user_is_rejected(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "") is None

    @pytest.mark.asyncio
    async def test_stranger_is_rejected(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "UZZZ") is None

    @pytest.mark.asyncio
    async def test_no_pending_approval(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "U1") is None

    @pytest.mark.asyncio
    async def test_late_trust_without_slack_client(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1") is None

    @pytest.mark.asyncio
    async def test_late_trust_when_thread_fetch_fails(self, owner, slack):
        slack.fetch_thread_replies = AsyncMock(side_effect=RuntimeError("api down"))
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack)
        assert out is None

    @pytest.mark.asyncio
    async def test_late_trust_rejects_non_thread_owner(self, owner, slack):
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "UOTHER"}])
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack)
        assert out is None
        assert not h.is_slack_session_trusted("t1")

    @pytest.mark.asyncio
    async def test_late_trust_grants_when_owner_matches(self, owner, slack, sessions, monkeypatch):
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1"}])
        fake_map = MagicMock()
        fake_map.get_session_for_thread.return_value = ""
        monkeypatch.setattr("kiro_crew.session.SessionMap", lambda: fake_map)
        out = await h.handle_interaction(
            "C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack, sessions=sessions
        )
        assert out == h._ACTION_TRUST
        assert h.is_slack_session_trusted("t1")
        sessions.set_approval_policy.assert_called_once_with("t1", "auto")

    @pytest.mark.asyncio
    async def test_late_trust_refuses_when_session_map_fails(self, owner, slack, monkeypatch):
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1"}])

        def _boom():
            raise RuntimeError("no map")

        monkeypatch.setattr("kiro_crew.session.SessionMap", _boom)
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack)
        assert out is None
        assert not h.is_slack_session_trusted("t1")

    @pytest.mark.asyncio
    async def test_pending_approve_resolves_future(self, owner):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        pending = h._PendingApproval(provider, "r1", "t1")
        h._pending_approvals["C1:m1"] = pending
        out = await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "U1")
        assert out == h._ACTION_APPROVE
        assert pending.future.result() == h._OUTCOME_APPROVED
        provider.approve_tool.assert_awaited_once_with("r1")
        assert "C1:m1" not in h._pending_approvals

    @pytest.mark.asyncio
    async def test_pending_reject_resolves_future(self, owner):
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        pending = h._PendingApproval(provider, "r1", "t1")
        h._pending_approvals["C1:m1"] = pending
        out = await h.handle_interaction("C1", "m1", h._ACTION_REJECT, "U1")
        assert out == h._ACTION_REJECT
        assert pending.future.result() == h._OUTCOME_REJECTED
        provider.reject_tool.assert_awaited_once_with("r1")

    @pytest.mark.asyncio
    async def test_pending_trust_grants_session(self, owner, sessions):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        h._pending_approvals["C1:m1"] = h._PendingApproval(provider, "r1", "sess-9")
        out = await h.handle_interaction(
            "C1", "m1", h._ACTION_TRUST, "U1", sessions=sessions
        )
        assert out == h._ACTION_TRUST
        assert h.is_slack_session_trusted("sess-9")
        sessions.set_approval_policy.assert_called_once_with("sess-9", "auto")

    @pytest.mark.asyncio
    async def test_pending_trust_without_session_key_still_approves(self, owner):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        h._pending_approvals["C1:m1"] = h._PendingApproval(provider, "r1", "")
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1")
        assert out == h._ACTION_TRUST
        provider.approve_tool.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# linked (dashboard-owned) approvals
# ──────────────────────────────────────────────────────────────────────
class TestLinkedApprovals:
    @pytest.mark.asyncio
    async def test_post_registers_entry(self, slack):
        ts = _reply(
            await h.post_linked_approval(slack, "C1", "t1", "r1", "slot-1", "Run tests", "pytest")
        )
        assert f"C1:{ts}" in h._linked_approvals
        h.resolve_linked_approval("C1", ts)
        assert f"C1:{ts}" not in h._linked_approvals

    @pytest.mark.asyncio
    async def test_post_failure_returns_none(self):
        broken = MagicMock()
        broken.post_blocks = AsyncMock(side_effect=RuntimeError("slack down"))
        assert await h.post_linked_approval(broken, "C1", "t1", "r1", "s", "T") is None
        assert not h._linked_approvals

    @pytest.mark.asyncio
    async def test_click_resolves_dashboard_future(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.resolve_approval.return_value = True
        monkeypatch.setattr(h, "_dashboard_state", state)
        ts = _reply(await h.post_linked_approval(slack, "C1", "t1", 7, "slot-1", "Run tests"))
        out = await h.handle_interaction("C1", ts, h._ACTION_APPROVE, "U1")
        assert out == h._ACTION_APPROVE
        state.resolve_approval.assert_called_once_with("7", True)
        assert f"C1:{ts}" not in h._linked_approvals

    @pytest.mark.asyncio
    async def test_reject_click_passes_false(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.resolve_approval.return_value = False
        monkeypatch.setattr(h, "_dashboard_state", state)
        ts = _reply(
            await h.post_linked_approval(slack, "C1", "t1", "r9", "slot-1", "Delete prod")
        )
        out = await h.handle_interaction("C1", ts, h._ACTION_REJECT, "U1")
        assert out == h._ACTION_REJECT
        state.resolve_approval.assert_called_once_with("r9", False)

    @pytest.mark.asyncio
    async def test_resolve_failure_is_swallowed(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.resolve_approval.side_effect = RuntimeError("gone")
        monkeypatch.setattr(h, "_dashboard_state", state)
        ts = _reply(await h.post_linked_approval(slack, "C1", "t1", "r1", "slot-1", "T"))
        assert await h.handle_interaction("C1", ts, h._ACTION_APPROVE, "U1") == h._ACTION_APPROVE


# ──────────────────────────────────────────────────────────────────────
# maybe_route_linked_thread
# ──────────────────────────────────────────────────────────────────────
class TestRouteLinkedThread:
    @pytest.mark.asyncio
    async def test_no_dashboard_state(self, slack):
        assert await h.maybe_route_linked_thread("hi", "t1", "U1", "C1", slack, "t1") is False

    @pytest.mark.asyncio
    async def test_no_linked_slot(self, slack, monkeypatch):
        state = MagicMock()
        state.get_linked_slot.return_value = None
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("hi", "t1", "U1", "C1", slack, "t1") is False

    @pytest.mark.asyncio
    async def test_unauthorized_user_denied(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.get_linked_slot.return_value = MagicMock(key="slot-1")
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("hi", "t1", "UZZZ", "C1", slack, "t1") is True
        assert "Not authorized." in _texts(slack)

    @pytest.mark.asyncio
    async def test_bang_command_falls_through(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.get_linked_slot.return_value = MagicMock(key="slot-1")
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("!yolo on", "t1", "U1", "C1", slack, "t1") is False

    @pytest.mark.asyncio
    async def test_running_slot_queues_message(self, slack, owner, monkeypatch):
        slot = MagicMock(key="slot-1", running=True)
        state = MagicMock()
        state.get_linked_slot.return_value = slot
        monkeypatch.setattr(h, "_dashboard_state", state)
        assert await h.maybe_route_linked_thread("do it", "t1", "U1", "C1", slack, "t1") is True
        slot.queue_append.assert_called_once_with("do it")
        slot.append.assert_called_once()
        state.push_slots_update.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# approval block builder
# ──────────────────────────────────────────────────────────────────────
def _perm_event(
    *,
    request_id: str | int = "r1",
    title: str = "Bash",
    tool_input: str = "",
    tool_purpose: str = "",
) -> LLMEvent:
    return LLMEvent(
        kind="permission_request",
        request_id=request_id,
        title=title,
        tool_input=tool_input,
        tool_purpose=tool_purpose,
    )


class TestApprovalBlocks:
    def test_dm_offers_trust(self):
        blocks = h._build_approval_blocks(_perm_event(), is_dm=True)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        ids = [e["action_id"] for e in actions["elements"]]
        assert ids == [h._ACTION_APPROVE, h._ACTION_TRUST, h._ACTION_REJECT]

    def test_channel_omits_trust(self):
        blocks = h._build_approval_blocks(_perm_event(), is_dm=False)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        ids = [e["action_id"] for e in actions["elements"]]
        assert h._ACTION_TRUST not in ids

    def test_integer_request_id_is_stringified(self):
        blocks = h._build_approval_blocks(_perm_event(request_id=42))
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert all(e["value"] == "42" for e in actions["elements"])

    def test_source_tag_and_purpose_in_footer(self):
        blocks = h._build_approval_blocks(
            _perm_event(tool_purpose="run the suite"), source="subagent"
        )
        footer = blocks[-1]["elements"][0]["text"]
        assert "[subagent]" in footer and "run the suite" in footer

    def test_long_tool_input_is_truncated(self):
        blocks = h._build_approval_blocks(_perm_event(tool_input="x" * 5000))
        detail = blocks[1]["text"]["text"]
        assert h._TRUNCATION_MARKER in detail
        assert len(detail) < 5000

    def test_short_tool_input_is_verbatim(self):
        blocks = h._build_approval_blocks(_perm_event(tool_input="ls -la"))
        assert "```ls -la```" == blocks[1]["text"]["text"]


# ──────────────────────────────────────────────────────────────────────
# _request_approval
# ──────────────────────────────────────────────────────────────────────
class TestRequestApproval:
    @pytest.mark.asyncio
    async def test_post_failure_rejects_the_orphaned_tool(self):
        """A prompt that cannot be posted must not leave the ACP request unanswered."""
        broken = MagicMock()
        broken.post_blocks = AsyncMock(side_effect=RuntimeError("slack down"))
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        with pytest.raises(RuntimeError):
            await h._request_approval(broken, provider, "C1", "t1", _perm_event())
        provider.reject_tool.assert_awaited_once_with("r1")
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_resolved_future_returns_outcome_and_deletes_prompt(self, slack):
        provider = MagicMock()

        async def _post(channel, blocks, text, thread_ts=None, **kw):
            ts = "9001.0"
            # Resolve as soon as the prompt exists, so no timer is involved.
            asyncio.get_running_loop().call_soon(
                lambda: h._pending_approvals[f"{channel}:{ts}"].future.set_result(
                    h._OUTCOME_APPROVED
                )
            )
            slack.actions.append(("blocks", {"channel": channel, "text": text, "ts": ts}))
            return ts

        slack.post_blocks = _post
        outcome = await h._request_approval(slack, provider, "C1", "t1", _perm_event())
        assert outcome == h._OUTCOME_APPROVED
        assert [a for a in slack.actions if a[0] == "delete"]
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_timeout_rejects_the_tool(self, slack, monkeypatch):
        # timeout=0 makes wait_for fail its first check — deterministic, no sleep.
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        outcome = await h._request_approval(slack, provider, "C1", "t1", _perm_event())
        assert outcome == h._OUTCOME_REJECTED
        provider.reject_tool.assert_awaited_once_with("r1")
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_delete_failure_falls_back_to_an_edit(self, monkeypatch):
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        slack_client = MockSlackClient()
        monkeypatch.setattr(
            slack_client, "delete_message", AsyncMock(side_effect=RuntimeError("too old"))
        )
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        await h._request_approval(slack_client, provider, "C1", "t1", _perm_event())
        assert "🚫 Rejected" in _texts(slack_client)


# ──────────────────────────────────────────────────────────────────────
# status reactions
# ──────────────────────────────────────────────────────────────────────
class TestStatusReactionController:
    @pytest.mark.asyncio
    async def test_disabled_controller_is_inert(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1", enabled=False)
        ctrl.set_phase("queued")
        ctrl.on_progress()
        ctrl.resume_stall_watchdog()
        ctrl.finalize()
        await asyncio.sleep(0)
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_immediate_phase_then_finalize_swaps_emoji(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            assert [a for a in slack.actions if a[0] == "react"]
            slack.actions.clear()
            ctrl.set_phase("done")  # terminal phase routes to finalize()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            kinds = [a[0] for a in slack.actions]
            assert "unreact" in kinds and "react" in kinds
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_error_phase_uses_error_emoji(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.set_phase("error")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        emojis = [a[1]["emoji"] for a in slack.actions if a[0] == "react"]
        assert emojis == [h._PHASE_EMOJIS["error"]]

    @pytest.mark.asyncio
    async def test_finalize_is_idempotent(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.finalize()
        await asyncio.sleep(0)
        count = len(slack.actions)
        ctrl.finalize()
        await asyncio.sleep(0)
        assert len(slack.actions) == count

    @pytest.mark.asyncio
    async def test_pause_stops_the_watchdog(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl.on_progress()
            ctrl.pause_stall_watchdog()
            assert ctrl._stall_soft_handle is None
            ctrl.on_progress()  # paused — must stay disarmed
            assert ctrl._stall_soft_handle is None
            ctrl.resume_stall_watchdog()
            assert ctrl._stall_soft_handle is not None
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_stall_emoji_upgrades_then_clears(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            await ctrl._add_stall_emoji(h._STALL_EMOJI_SOFT)
            await ctrl._add_stall_emoji(h._STALL_EMOJI_HARD)
            assert ctrl._stall_emoji == h._STALL_EMOJI_HARD
            unreacted = [a[1]["emoji"] for a in slack.actions if a[0] == "unreact"]
            assert h._STALL_EMOJI_SOFT in unreacted
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_stall_emoji_not_added_after_finalize(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.finalize()
        await asyncio.sleep(0)
        slack.actions.clear()
        await ctrl._add_stall_emoji(h._STALL_EMOJI_SOFT)
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_suppressed_phase_removes_without_adding(self, slack, monkeypatch):
        monkeypatch.setitem(h._PHASE_EMOJIS, "queued", None)
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl._current_emoji = "eyes"
            await ctrl._swap_emoji(None)
            kinds = [a[0] for a in slack.actions]
            assert kinds == ["unreact"]
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_swap_to_same_emoji_is_a_no_op(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl._current_emoji = "eyes"
            await ctrl._swap_emoji("eyes")
            assert not slack.actions
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_debounced_phase_applies_on_fire(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl.set_phase("coding")
            assert ctrl._pending_phase == "coding"
            await ctrl._apply_pending()
            assert ctrl._pending_phase is None
            assert [a for a in slack.actions if a[0] == "react"]
            # A second apply with nothing pending must not react again.
            slack.actions.clear()
            await ctrl._apply_pending()
            assert not slack.actions
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_add_phase_reaction_honours_suppression(self, slack, monkeypatch):
        monkeypatch.setitem(h._PHASE_EMOJIS, "done", None)
        await h._add_phase_reaction(slack, "C1", "m1", "done")
        assert not slack.actions


# ──────────────────────────────────────────────────────────────────────
# small pure helpers
# ──────────────────────────────────────────────────────────────────────
class TestPureHelpers:
    def test_tool_to_phase_by_kind(self):
        assert h._tool_to_phase("Whatever", "bash") == "coding"
        assert h._tool_to_phase("Whatever", "webfetch") == "browsing"

    def test_tool_to_phase_by_name(self):
        assert h._tool_to_phase("Edit") == "coding"
        assert h._tool_to_phase("WebSearch") == "browsing"
        assert h._tool_to_phase("mcp__example-mcp__Bash") == "coding"
        assert h._tool_to_phase("SomethingElse") == "tool"

    def test_build_phase_emojis_collects_unknown_keys(self):
        result, unknown = h._build_phase_emojis({"done": "tada", "bogus": "x"})
        assert result["done"] == "tada"
        assert unknown == ["bogus"]

    def test_build_phase_emojis_accepts_suppression(self):
        result, unknown = h._build_phase_emojis({"done": None})
        assert result["done"] is None and unknown == []

    def test_filter_options_brackets_drops_options_tag(self):
        hold, buf = h._filter_options_brackets("hi [OPTIONS: a | b] bye", "", "")
        assert hold == "" and buf == "hi  bye"

    def test_filter_options_brackets_keeps_other_brackets(self):
        _, buf = h._filter_options_brackets("see [1] here", "", "")
        assert buf == "see [1] here"

    def test_filter_options_brackets_holds_open_bracket(self):
        hold, buf = h._filter_options_brackets("tail [OPTI", "", "")
        assert hold == "[OPTI" and buf == "tail "

    def test_timing_footer_seconds_and_minutes(self):
        _, text = h.build_timing_footer(42.0)
        assert text == "Finished in 42s"
        _, text = h.build_timing_footer(125.0)
        assert text == "Finished in 2m 5s"

    @pytest.mark.parametrize(
        ("pct", "icon"), [(80, "🔴"), (60, "🟠"), (40, "🟡"), (5, "🟢")]
    )
    def test_timing_footer_context_icon(self, pct, icon):
        client = MagicMock()
        client.context_usage_pct.return_value = pct
        _, text = h.build_timing_footer(1.0, client)
        assert icon in text and f"ctx {pct}%" in text

    def test_timing_footer_survives_broken_client(self):
        client = MagicMock()
        client.context_usage_pct.side_effect = RuntimeError("no ctx")
        _, text = h.build_timing_footer(1.0, client)
        assert text == "Finished in 1s"

    def test_append_footer_actions_adds_options(self):
        blocks = h._append_footer_actions([{"type": "context"}], ["a", "b"], None, None, None)
        assert len(blocks) > 1

    def test_append_footer_actions_appends_link_button_to_existing_actions(self):
        footer = [{"type": "actions", "elements": []}]
        out = h._append_footer_actions(footer, None, "t1", None, MagicMock())
        assert len(out[-1]["elements"]) == 1

    def test_append_footer_actions_creates_actions_block(self):
        out = h._append_footer_actions([{"type": "context"}], None, "t1", None, MagicMock())
        assert out[-1]["type"] == "actions"

    def test_append_footer_actions_skips_when_already_linked(self):
        footer = [{"type": "context"}]
        assert h._append_footer_actions(footer, None, "t1", "slot-1", MagicMock()) == footer

    def test_condense_thinking_short_text(self):
        out = h._condense_thinking("one\n\ntwo")
        assert out.startswith("💭 *Thinking*")
        assert "> one" in out and "\n>\n" in out
        assert "full reasoning" not in out

    def test_condense_thinking_truncates_on_whitespace(self):
        out = h._condense_thinking(" ".join(["word"] * 300), limit=40)
        assert "full reasoning in dashboard Activity" in out
        assert len(out) < 200

    def test_condense_thinking_hard_cut_without_early_boundary(self):
        out = h._condense_thinking("a" * 50 + " tail", limit=20)
        assert "full reasoning" in out

    def test_fmt_duration(self):
        assert h._fmt_duration(7200) == "2h"
        assert h._fmt_duration(1800) == "30min"

    def test_describe_new_grant(self):
        assert h.describe_new_grant(0) == h._NO_EXPIRY_TEXT
        assert h.describe_new_grant(3600) == "auto-expires in 1h"

    def test_describe_grant_lifetime_off(self):
        assert h.describe_grant_lifetime() == "off"

    def test_describe_grant_lifetime_when_active(self):
        h.enable_yolo_with_ttl(600)
        try:
            assert "remaining" in h.describe_grant_lifetime()
        finally:
            h.disable_yolo()

    def test_disable_yolo_when_inactive_is_a_no_op(self):
        h._trusted_sessions.add("t1")
        h.disable_yolo()
        assert "t1" in h._trusted_sessions

    def test_disable_yolo_clears_trusted_sessions(self):
        h.enable_yolo_with_ttl(600)
        h._trusted_sessions.add("t1")
        h.disable_yolo()
        assert not h._trusted_sessions

    def test_is_owner_cross_matches_w_and_u_prefixes(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "U123")
        assert h.is_owner("U123") is True
        assert h.is_owner("W123") is True
        assert h.is_owner("U999") is False
        assert h.is_owner("") is False

    def test_is_owner_without_configured_owner(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "")
        assert h.is_owner("U1") is False

    def test_is_allowed_user_requires_owner(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "U1")
        assert h.is_allowed_user("") is False
        assert h.is_allowed_user("U1") is True
        assert h.is_allowed_user("U2") is False

    def test_open_channels_are_disabled(self):
        h.set_open_channels({"C1"})
        assert h.is_open_channel("C1") is False

    def test_tracked_channels(self):
        h.set_tracking_channels({"C1"})
        assert h.is_tracked_channel("C1") is True
        assert h.is_tracked_channel("C2") is False
        assert h.is_tracked_channel("") is False

    def test_set_allowed_users_and_owner_setters(self):
        h.set_allowed_users({"U9"})
        h.set_owner_id("U9")
        try:
            assert h.is_allowed_user("U9") is True
        finally:
            h.set_owner_id("")

    def test_trusted_session_requires_non_empty_key(self):
        assert h.is_slack_session_trusted("") is False
        h.add_trusted_session("")
        assert not h._trusted_sessions

    def test_add_trusted_session_survives_policy_failure(self):
        sm = MagicMock()
        sm.set_approval_policy.side_effect = RuntimeError("closed")
        h.add_trusted_session("t1", sm)
        assert h.is_slack_session_trusted("t1")

    def test_dashboard_state_accessors(self):
        state = object()
        h.set_dashboard_state(state)
        assert h.get_dashboard_state() is state

    def test_orch_cfg_accessor_defaults_to_none(self):
        assert h.get_orch_cfg() is None

    def test_reload_orch_cfg_without_config_is_a_no_op(self):
        h._reload_orch_cfg()
        assert h.get_orch_cfg() is None

    def test_reload_orch_cfg_refreshes_channel_state(self):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        h.set_orch_cfg(cfg)
        h._persist_channel_config("C1", activation="observe")
        h._reload_orch_cfg()
        assert cfg.slack_channels["C1"].activation == "observe"

    def test_cancel_background_tasks_empties_the_set(self, monkeypatch):
        # Swap in a private set: the module-level one may hold real leaked tasks
        # from earlier tests whose loops are already closed.
        own: set = set()
        monkeypatch.setattr(h, "_background_tasks", own)
        task = MagicMock()
        own.add(task)
        h.cancel_background_tasks()
        task.cancel.assert_called_once()
        assert not own

    def test_should_auto_approve_spawn(self):
        builder = MagicMock()
        builder.is_auto_approved_spawn.return_value = True
        assert h._should_auto_approve_spawn(builder, "spawn_run") is True
        assert h._should_auto_approve_spawn(None, "spawn_run") is False


# ──────────────────────────────────────────────────────────────────────
# safe update paths
# ──────────────────────────────────────────────────────────────────────
class TestSafeUpdates:
    @pytest.mark.asyncio
    async def test_update_truncates_over_limit(self, slack):
        from kiro_crew.slack.format import SLACK_MSG_LIMIT

        await h._safe_update(slack, "C1", "m1", "x" * (SLACK_MSG_LIMIT + 50))
        text = slack.actions[0][1]["text"]
        assert len(text) > SLACK_MSG_LIMIT  # limit + truncation notice
        assert text.startswith("x")

    @pytest.mark.asyncio
    async def test_update_failure_is_swallowed(self):
        broken = MagicMock()
        broken.update_message = AsyncMock(side_effect=RuntimeError("gone"))
        await h._safe_update(broken, "C1", "m1", "hi")

    @pytest.mark.asyncio
    async def test_final_update_splits_long_text(self, slack):
        from kiro_crew.slack.format import SLACK_MSG_LIMIT

        await h._safe_final_update(slack, "C1", "m1", "y " * SLACK_MSG_LIMIT, "t1")
        assert [a for a in slack.actions if a[0] == "update"]
        assert [a for a in slack.actions if a[0] == "post"]

    @pytest.mark.asyncio
    async def test_final_update_survives_both_failures(self):
        broken = MagicMock()
        broken.update_message = AsyncMock(side_effect=RuntimeError("x"))
        broken.post_message = AsyncMock(side_effect=RuntimeError("y"))
        await h._safe_final_update(broken, "C1", "m1", "z " * 8000, "t1")

    @pytest.mark.asyncio
    async def test_reject_orphaned_tool_swallows_failure(self):
        provider = MagicMock()
        provider.reject_tool = AsyncMock(side_effect=RuntimeError("dead"))
        await h._reject_orphaned_tool(provider, "r1")
        provider.reject_tool.assert_awaited_once_with("r1")

    @pytest.mark.asyncio
    async def test_safe_voice_reply_never_raises(self, slack, monkeypatch):
        monkeypatch.setattr(h, "_voice_reply_fn", AsyncMock(side_effect=RuntimeError("no tts")))
        await h._safe_voice_reply(slack, "C1", "t1", "hello")

    @pytest.mark.asyncio
    async def test_safe_voice_reply_forwards_settings(self, slack, monkeypatch):
        fake = AsyncMock()
        monkeypatch.setattr(h, "_voice_reply_fn", fake)
        await h._safe_voice_reply(slack, "C1", "t1", "hello", voice_id="Joanna")
        call = fake.await_args
        assert call is not None
        assert call.kwargs["voice_id"] == "Joanna"


# ──────────────────────────────────────────────────────────────────────
# !compact
# ──────────────────────────────────────────────────────────────────────
class TestCompactCommand:
    @pytest.mark.asyncio
    async def test_busy_session_asks_to_retry(self, slack, sessions):
        sessions.try_acquire = AsyncMock(return_value=False)
        sessions.has_session.return_value = True
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "Still working on your last message" in _texts(slack)

    @pytest.mark.asyncio
    async def test_no_session_at_all(self, slack, sessions):
        sessions.try_acquire = AsyncMock(return_value=False)
        sessions.has_session.return_value = False
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "No active session to compact." in _texts(slack)

    @pytest.mark.asyncio
    async def test_acquired_but_no_provider(self, slack, sessions):
        sessions.try_acquire = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=None)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "No active session to compact." in _texts(slack)

    @pytest.mark.asyncio
    async def test_completed_compaction_reports_success(self, slack, sessions):
        provider = MagicMock()
        provider.compact = AsyncMock()
        provider.wait_for_compaction = AsyncMock(
            return_value={"type": "completed", "summary": "model-facing context dump"}
        )
        sessions.try_acquire = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        body = _texts(slack)
        assert "Context compacted." in body
        # ``summary`` is model-facing context, never a user-facing receipt.
        assert "model-facing context dump" not in body
        assert [a for a in slack.actions if a[0] == "unreact"]

    @pytest.mark.asyncio
    async def test_failed_compaction_reports_error(self, slack, sessions):
        provider = MagicMock()
        provider.compact = AsyncMock()
        provider.wait_for_compaction = AsyncMock(
            return_value={"type": "failed", "summary": "backend refused"}
        )
        sessions.try_acquire = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "backend refused" in _texts(slack)

    @pytest.mark.asyncio
    async def test_timed_out_compaction(self, slack, sessions):
        provider = MagicMock()
        provider.compact = AsyncMock()
        provider.wait_for_compaction = AsyncMock(return_value={"type": "timeout"})
        sessions.try_acquire = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "Compaction timed out." in _texts(slack)

    @pytest.mark.asyncio
    async def test_exception_destroys_the_session(self, slack, sessions):
        provider = MagicMock()
        provider.compact = AsyncMock(side_effect=RuntimeError("stdio died"))
        sessions.try_acquire = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "Compaction failed unexpectedly." in _texts(slack)
        sessions.destroy.assert_awaited_once_with("t1")
