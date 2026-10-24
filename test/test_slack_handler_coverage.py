"""Coverage tests for :mod:`kiro_crew.slack.handler`.

Focus is the command surface that ``test_slack_handler.py`` leaves untouched:
``!bang`` slash-command dispatch (every branch of ``_handle_slash_command``),
the path-independent keyword commands, the small command helpers
(``spawn``/``cron``/``task run``/``sessions``), the agent-name resolution chain,
the thread-override hydration and ``!compact``. The sibling files pin the privacy
modifiers and linked-thread routing (``test_slack_handler_coverage_inbound.py``),
the guard/permission rejection paths in ``handle_interaction`` with the other
approval prompts (``test_slack_handler_coverage_approvals.py``), status reactions,
review drafts and the safe update paths (``test_slack_handler_coverage_delivery.py``)
and the small pure helpers (``test_slack_handler_coverage_pure_helpers.py``); the
shared fixtures live in ``slack_handler_coverage_helpers.py``.

Everything runs in-process against ``MockSlackClient`` and ``MagicMock``
service doubles — no network, no subprocesses, no real Slack.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_handler_coverage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _clean_handler_state,
    _reply,
    _texts,
    owner_fixture,
    sessions_fixture,
    slack_fixture,
)

from kiro_crew.cron import CronJob, CronSchedule, CronStoreBusy
from kiro_crew.messaging import commands as mc
from kiro_crew.slack import handler as h
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_reporter import build_status
from kiro_crew.testing.wait import await_until


class _LiveTask:
    """Stand-in for an in-flight asyncio task; ``build_status`` only calls done()."""

    @staticmethod
    def done() -> bool:
        return False


def _running_status(*, completed: int = 2, total: int = 5, current: int = 3) -> dict:
    """A real ``build_status()`` payload for one in-flight run.

    Built from real ``Project``/``Task`` objects instead of hand-written keys:
    progress lives per run inside ``runs``, and there is no top-level
    ``completed``/``steps``/``current_step``. Asserting against an invented
    top-level shape is what let a renderer read fields the payload never
    carries.
    """
    statuses = [TaskStatus.PASSED] * completed + [TaskStatus.PENDING] * (total - completed)
    run = Project(
        spec_path="/tmp/spec.md",
        spec_content="",
        task_id="live",
        name="Live Task",
        status="executing",
        current_task=current,
        tasks=[
            Task(index=i + 1, title=f"t{i + 1}", description="", status=s)
            for i, s in enumerate(statuses)
        ],
    )
    return build_status({"live": run}, {"live": _LiveTask()}, "kirocrew")


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
    async def test_off_revokes_while_policy_masks_the_grant(
        self, slack, sessions, owner, monkeypatch
    ):
        """An explicit off must not be gated on the POLICY-FILTERED liveness.

        ``!yolo`` computes ``yolo_active = is_yolo_mode()``, which policy can veto. A
        grant that policy masks but that still EXISTS therefore reads False, so the
        off branch reported "already off" and never called ``disable_yolo()`` --
        leaving a grant standing that the operator had explicitly revoked.
        ``disable_yolo`` itself was corrected to read ``has_grant``; this covers its
        CALLER, which was still gating it out.

        A deny normally destroys the grant at ceiling-install time, so the mask is the
        fail-closed floor for a teardown that did not complete -- which is exactly the
        state where an explicit off has work to do.

        Driven through ``_slash`` on purpose: an earlier attempt re-implemented the
        gate inside the test and therefore exercised no handler code, passing even
        with the fix reverted.
        """
        from kiro_crew import safety_override as so

        await _slash("!yolo on", slack, sessions)
        assert h.is_yolo_mode() is True
        slack.actions.clear()

        # Push a DENY without going through a ceiling install, so the grant survives
        # and only the policy mask is in force -- which is the state that misled the
        # branch under test.
        monkeypatch.setattr(so, "_yolo_policy_permitted", False)
        monkeypatch.setattr(so, "_yolo_policy_resolved", True)
        assert h.is_yolo_mode() is False, "the filtered reading is what misled the branch"
        assert so.safety_override().has_grant() is True, "the grant is still standing"

        await _slash("!yolo off", slack, sessions)

        assert "disabled" in _texts(slack), "the off must be reported as an off"
        with so.safety_override()._lock:
            assert (
                so.safety_override()._active is False
            ), "the grant must be torn down even while the verdict is unknown"

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

        async def _stop(key, *, force=False, owned_by=None, on_soft=None, on_hard=None):
            await on_soft()
            return "soft"

        sessions.stop_turn = AsyncMock(side_effect=_stop)
        await _slash("!stop", slack, sessions)
        assert "Execution stopped." in _texts(slack)
        assert any(a[0] == "ephemeral" for a in slack.actions)

    @pytest.mark.asyncio
    async def test_hard_stop_reports_session_reset(self, slack, sessions, owner):
        sessions.has_session.return_value = True

        async def _stop(key, *, force=False, owned_by=None, on_soft=None, on_hard=None):
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
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("!agent", "Current agent", id="test_bare_agent_reports_current"),
            pytest.param("!agent a b", "Usage:", id="test_too_many_args_shows_usage"),
        ],
    )
    async def test_agent_reply(self, slack, sessions, owner, agents_dir, command, expected):
        await _slash(command, slack, sessions)
        assert expected in _texts(slack)

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
    async def test_write_failure_surfaces_error(
        self, slack, sessions, owner, agents_dir, monkeypatch
    ):
        def _boom(_name):
            raise ValueError("read-only config")

        monkeypatch.setattr(h, "_set_default_agent", _boom)
        await _slash("!agent demo", slack, sessions)
        assert "read-only config" in _texts(slack)


class TestThreadAgentCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("!ta", "No thread agent set.", id="test_bare_ta_without_override"),
            pytest.param("!ta nope", "Unknown agent", id="test_unknown_thread_agent"),
        ],
    )
    async def test_thread_agent_reply(self, slack, sessions, owner, agents_dir, command, expected):
        await _slash(command, slack, sessions)
        assert expected in _texts(slack)

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
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param(
                "!dashboard 5furlongs", "Usage:", id="test_dashboard_rejects_bad_duration"
            ),
            pytest.param(
                "!link-to-dashboard",
                "Dashboard not available.",
                id="test_link_to_dashboard_without_dashboard",
            ),
            pytest.param(
                "!title", "Usage: `!title <text>`", id="test_title_without_text_shows_usage"
            ),
        ],
    )
    async def test_misc_command_reply(self, slack, sessions, owner, command, expected):
        await _slash(command, slack, sessions)
        assert expected in _texts(slack)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "link", "expected"),
        [
            pytest.param(
                "!dashboard 2h", "https://x/y", "Dashboard link sent", id="test_dashboard_link_sent"
            ),
            pytest.param(
                "!dashboard",
                "",
                "Failed to send dashboard link.",
                id="test_dashboard_link_failure",
            ),
        ],
    )
    async def test_dashboard_link_reply(
        self, slack, sessions, owner, monkeypatch, command, link, expected
    ):
        import kiro_crew.slack.allowlist as al

        monkeypatch.setattr(al, "send_dashboard_link", AsyncMock(return_value=link))
        await _slash(command, slack, sessions)
        assert expected in _texts(slack)

    @pytest.mark.asyncio
    async def test_link_to_dashboard_denies_stranger(self, slack, sessions, owner):
        await _slash("!link-to-dashboard", slack, sessions, user="UZZZ")
        assert "Not authorized." in _texts(slack)

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
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("!channel", "activation", id="test_status"),
            pytest.param("!channel sometimes", "Invalid mode", id="test_invalid_mode"),
            pytest.param(
                "!channel agent",
                "Usage: `!channel agent",
                id="test_agent_subcommand_needs_argument",
            ),
        ],
    )
    async def test_channel_reply(self, slack, sessions, owner, command, expected):
        await _slash(command, slack, sessions)
        assert expected in _texts(slack)

    @pytest.mark.asyncio
    async def test_valid_mode_persists(self, slack, sessions, owner):
        from kiro_crew.config.loader import KiroCrewConfig, config_path

        await _slash("!channel observe", slack, sessions)
        assert "activation set to *observe*" in _texts(slack)
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert saved["slack"]["channels"]["C1"]["activation"] == "observe"
        assert KiroCrewConfig.load().channel_config("C1").activation == "observe"

    @pytest.mark.asyncio
    async def test_agent_subcommand_unknown_name(self, slack, sessions, owner, agents_dir):
        await _slash("!channel agent nope", slack, sessions)
        assert "Unknown agent" in _texts(slack)

    @pytest.mark.asyncio
    async def test_agent_subcommand_sets_and_clears(self, slack, sessions, owner, agents_dir):
        from kiro_crew.config.loader import config_path

        await _slash("!channel agent demo", slack, sessions)
        assert (
            json.loads(config_path().read_text(encoding="utf-8"))["slack"]["channels"]["C1"][
                "agent"
            ]
            == "demo"
        )
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
        monkeypatch.setattr(
            "kiro_crew.slack.sessions_view._collect_recent_sessions",
            lambda s, limit=0, kind=None, include_ended=False: [],
        )
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
    async def test_spawn_keyword_says_queued_for_a_deferred_row(
        self, slack, sessions, owner, monkeypatch
    ):
        """The gate parked the row (memory floor): the channel reply relays the
        gate's reason instead of announcing a start that did not happen."""
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", AsyncMock())
        mgr = MagicMock(max_concurrent=4)
        mgr.spawn.return_value = MagicMock(
            id="a1",
            queued=True,
            queued_reason="low_memory",
            queued_reason_detail="low memory: 3.2 GB available, need 4 GB",
        )
        handled = await h.maybe_handle_keyword_command(
            "spawn audit the docs",
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
        assert handled is True
        text = _texts(slack)
        assert "Queued subagent" in text
        assert "low memory: 3.2 GB available, need 4 GB" in text
        assert "Spawned subagent" not in text

    @pytest.mark.asyncio
    async def test_spawn_keyword_skips_log_when_incognito(
        self, slack, sessions, owner, monkeypatch
    ):
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
        # The spawn itself still ran and was announced; only the log write is skipped.
        mgr.spawn.assert_called_once()
        assert "Spawned subagent" in _texts(slack)
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_keyword_saves_turn(self, slack, sessions, owner, monkeypatch):
        saver = AsyncMock()
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", saver)
        runner = MagicMock(running=True)
        log = MagicMock()
        await h.maybe_handle_keyword_command(
            "task run /tmp/spec.md",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            log,
            task_runner=runner,
        )
        saver.assert_awaited_once()
        args = saver.await_args.args
        assert args[:3] == (log, "t1", "task run /tmp/spec.md")
        assert args[3] in _texts(slack)
        assert saver.await_args.kwargs["source_user"] == "U1"

    @pytest.mark.asyncio
    async def test_cron_keyword_saves_turn(self, slack, sessions, owner, monkeypatch):
        saver = AsyncMock()
        monkeypatch.setattr(h, "save_conversation_turn_off_loop", saver)
        svc = MagicMock()
        svc.list_jobs.return_value = []
        log = MagicMock()
        await h.maybe_handle_keyword_command(
            "cron list",
            slack,
            sessions,
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            log,
            cron_service=svc,
        )
        saver.assert_awaited_once()
        args = saver.await_args.args
        assert args[:3] == (log, "t1", "cron list")
        assert args[3] in _texts(slack)
        assert saver.await_args.kwargs["source_user"] == "U1"

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
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("please spawn later", id="test_no_prefix_is_ignored"),
            pytest.param("spawn   ", id="test_empty_task_returns_none"),
        ],
    )
    async def test_non_spawn_text_returns_none(self, text):
        mgr = MagicMock()
        assert await h._handle_spawn_command(text, mgr) is None
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_bg_prefix_accepted(self):
        mgr = MagicMock(max_concurrent=2)
        mgr.spawn.return_value = MagicMock(id="z9")
        assert "z9" in _reply(await h._handle_spawn_command("bg do it", mgr))

    @pytest.mark.asyncio
    async def test_list_with_no_agents(self):
        assert await mc.spawn_task_reply("list", MagicMock(running=[])) == "No subagents running."

    @pytest.mark.asyncio
    async def test_status_lists_running_agents(self):
        agent = MagicMock(id="a7", started=time.time() - 5, task="reindex the corpus")
        out = _reply(await mc.spawn_task_reply("status", MagicMock(running=[agent])))
        assert "a7" in out and "reindex the corpus" in out

    @pytest.mark.asyncio
    async def test_capacity_reached(self):
        mgr = MagicMock(max_concurrent=3)
        mgr.spawn.return_value = None
        assert "capacity reached (3)" in _reply(await mc.spawn_task_reply("work", mgr))


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
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("hello", id="test_non_cron_text_ignored"),
            pytest.param("cron remove", id="test_action_without_job_id_returns_none"),
            pytest.param("cron frobnicate j1", id="test_unknown_action_returns_none"),
        ],
    )
    async def test_unhandled_cron_text_returns_none(self, text):
        assert await h._handle_cron_command(text, MagicMock(), "C1", "t1") is None

    @pytest.mark.asyncio
    async def test_empty_list(self):
        svc = MagicMock()
        svc.list_jobs.return_value = []
        assert await h._handle_cron_command("cron list", svc, "C1", "t1") == (
            "No cron jobs scheduled."
        )

    @pytest.mark.asyncio
    async def test_remove_found_and_missing(self):
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        assert "Removed cron job" in _reply(
            await h._handle_cron_command("cron remove j1", svc, "C", "t")
        )
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
        assert await mc.cron_remove_all_reply(svc, source="slack", caller="U1") == (
            "No cron jobs to remove."
        )

    @pytest.mark.asyncio
    async def test_remove_all_reports_each_job(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1"), _job("j2")]
        # remove_jobs returns (removed_ids, missing_ids); the shared reply unpacks
        # it for the SEL batch audit, which now covers every channel rather than
        # only Slack's own copy of the command.
        svc.remove_jobs = AsyncMock(return_value=(["j1", "j2"], []))
        out = await mc.cron_remove_all_reply(svc, source="slack", caller="U1")
        assert "Removed 2 cron job(s)" in out and "`j1`" in out and "`j2`" in out

    @pytest.mark.asyncio
    async def test_remove_all_busy_store(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job()]
        svc.remove_jobs = AsyncMock(side_effect=CronStoreBusy())
        assert "busy" in (await mc.cron_remove_all_reply(svc, source="slack", caller="U1"))

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
        runner.status.return_value = _running_status()
        out = _reply(await h._handle_run_command("task run status", runner, slack, "C", "t"))
        assert "2/5" in out and "executing" in out

    @pytest.mark.asyncio
    async def test_cancel_paths(self, slack):
        idle = MagicMock(running=False)
        assert await h._handle_run_command("task run cancel", idle, slack, "C", "t") == (
            "No task running."
        )
        busy = MagicMock(running=True)
        assert "cancelled" in _reply(
            await h._handle_run_command("task run cancel", busy, slack, "C", "t")
        )
        busy.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_spec_file(self, slack, tmp_path):
        runner = MagicMock(running=False)
        out = _reply(
            await h._handle_run_command(
                f"task run {tmp_path / 'absent.md'}", runner, slack, "C", "t"
            )
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
        def _boom(_sessions, limit=0, kind=None, include_ended=False):
            raise OSError("history unreadable")

        monkeypatch.setattr("kiro_crew.slack.sessions_view._collect_recent_sessions", _boom)
        await h._handle_sessions_command("sessions", slack, "C1", "t1", "msg1", "t1", None)
        assert "_Sessions unavailable._" in _texts(slack)

    @pytest.mark.asyncio
    async def test_rows_render_blocks(self, slack, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.slack.sessions_view._collect_recent_sessions",
            lambda s, limit=0, kind=None, include_ended=False: [{"key": "s1"}],
        )
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
def _metadata_log(read) -> MagicMock:
    """A conversation-log mock whose metadata line is *read(key)*, reported readable.

    Hydration reads through ``get_metadata_status``, which the real log answers with the
    metadata and whether the line could be read.
    """
    log = MagicMock()
    log.get_metadata_status.side_effect = lambda key: (read(key), True)
    return log


class TestThreadOverrideHydration:
    @pytest.mark.asyncio
    async def test_execution_identity_reads_run_off_loop_and_live_maps_stay_on_loop(
        self, monkeypatch
    ):
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        loop_thread = threading.get_ident()

        class LoopOwnedMap(dict):
            def __setitem__(self, key, value):
                assert threading.get_ident() == loop_thread
                super().__setitem__(key, value)

        monkeypatch.setattr(h, "_thread_agents", LoopOwnedMap())
        monkeypatch.setattr(h, "_thread_projects", LoopOwnedMap())
        execution = ExecutionContext(
            "reviewer-id", MemoryStoreRef("member-reviewer", "reviewer-id"), "member", "kirocrew"
        )

        def read_metadata(_key):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return {
                "agent": "reviewer",
                "execution_context": execution.to_record(),
                "project": "/srv/app",
            }

        log = _metadata_log(read_metadata)
        await h._hydrate_thread_overrides("t1", log)
        await h._hydrate_thread_overrides("t1", log)
        log.get_metadata_status.assert_called_once_with("t1")
        assert h._thread_agents["t1"] == "kirocrew"
        assert h._thread_projects["t1"] == "/srv/app"
        assert "t1" in h._hydrated_sessions

    @pytest.mark.asyncio
    @pytest.mark.parametrize("clear", [False, True])
    async def test_worker_result_cannot_overwrite_a_newer_live_selection(self, monkeypatch, clear):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        h._thread_agents["t1"] = "before-agent"
        h._thread_projects["t1"] = "before-project"

        def read(_key, _log):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "test did not release the metadata read"
            return "saved-agent", "saved-project"

        monkeypatch.setattr(h, "_read_thread_overrides", read)
        task = asyncio.create_task(
            h._hydrate_thread_overrides("t1", _metadata_log(lambda _key: {}))
        )
        try:
            await await_until(started, what="the metadata read starting")
            assert "t1" not in h._hydrated_sessions
            if clear:
                h._thread_agents.pop("t1")
                h._thread_projects.pop("t1")
            else:
                h._thread_agents["t1"] = "new-agent"
                h._thread_projects["t1"] = "new-project"
        finally:
            release.set()
            await await_until(task, what="the hydration finishing")
        assert h._thread_agents.get("t1") == (None if clear else "new-agent")
        assert h._thread_projects.get("t1") == (None if clear else "new-project")

    @pytest.mark.asyncio
    async def test_second_call_is_a_no_op(self):
        log = _metadata_log(lambda _key: {})
        await h._hydrate_thread_overrides("t1", log)
        await h._hydrate_thread_overrides("t1", log)
        log.get_metadata_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_without_log_only_marks_hydrated(self):
        await h._hydrate_thread_overrides("t1", None)
        assert "t1" in h._hydrated_sessions

    @pytest.mark.asyncio
    async def test_metadata_failure_is_swallowed(self):
        log = MagicMock()
        log.get_metadata_status.side_effect = OSError("corrupt")
        await h._hydrate_thread_overrides("t1", log)
        assert "t1" not in h._thread_agents

    @pytest.mark.asyncio
    async def test_agent_and_project_hydrated(self):
        log = _metadata_log(lambda _key: {"agent": "demo", "project": "/srv/app"})
        await h._hydrate_thread_overrides("t1", log)
        assert h._thread_agents["t1"] == "demo"
        assert h._thread_projects["t1"] == "/srv/app"

    @pytest.mark.asyncio
    async def test_sensitive_project_is_dropped(self, monkeypatch):
        monkeypatch.setattr(h, "is_sensitive_path", lambda p: True)
        log = MagicMock()
        log.get_metadata.return_value = {"project": "/home/u/.aws"}
        log.get_metadata_status.side_effect = lambda key: (log.get_metadata(key), True)
        await h._hydrate_thread_overrides("t1", log)
        assert "t1" not in h._thread_projects


# ──────────────────────────────────────────────────────────────────────
# !compact
# ──────────────────────────────────────────────────────────────────────
class TestCompactCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("has_session", "expected"),
        [
            pytest.param(
                True, "Still working on your last message", id="test_busy_session_asks_to_retry"
            ),
            pytest.param(False, "No active session to compact.", id="test_no_session_at_all"),
        ],
    )
    async def test_unacquired_session_reply(self, slack, sessions, has_session, expected):
        sessions.try_acquire = AsyncMock(return_value=False)
        sessions.has_session.return_value = has_session
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert expected in _texts(slack)

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
    async def test_exception_discards_the_conversation(self, slack, sessions):
        """The wedged conversation goes; the session's channel identity stays.

        ``discard_conversation`` shuts the provider down and drops the resume
        sid exactly like ``destroy``, but keeps the session-map entry that
        carries the thread linkage.
        """
        provider = MagicMock()
        provider.compact = AsyncMock(side_effect=RuntimeError("stdio died"))
        sessions.try_acquire = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "msg1", "t1")
        assert "Compaction failed unexpectedly." in _texts(slack)
        sessions.discard_conversation.assert_awaited_once_with("t1")
        sessions.destroy.assert_not_awaited()
