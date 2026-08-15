"""Failure-branch coverage for :mod:`kiro_crew.slack.handler`.

``test_slack_handler.py`` and ``test_slack_handler_coverage.py`` drive the happy
paths of the Slack command surface. This module deliberately targets what they
leave behind: the ``except`` arms that only run when the Slack API, the config
store, or the SEL audit sink misbehave, plus the early keyword dispatch inside
``handle_message`` (``status`` / ``sessions`` / ``!compact`` / the
not-authorized replies).

Everything runs in-process against doubles: no network, no subprocess, and no
write outside ``tmp_path`` (``config_path`` is redirected per test).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import MockSlackClient
from kiro_crew.config.loader import ConfigReadError
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler as h

# ──────────────────────────────────────────────────────────────────────
# doubles
# ──────────────────────────────────────────────────────────────────────


class FlakySlack(MockSlackClient):
    """MockSlackClient whose named methods raise instead of recording.

    The handler wraps almost every Slack call in ``try/except`` so a transient
    API error degrades cosmetics rather than aborting a turn. Those arms are
    only reachable with a client that actually fails.
    """

    def __init__(self, *fail: str) -> None:
        super().__init__()
        self.fail: set[str] = set(fail)

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        if "add_reaction" in self.fail:
            raise RuntimeError("slack add_reaction unavailable")
        await super().add_reaction(channel, ts, emoji, raise_on_error)

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        if "remove_reaction" in self.fail:
            raise RuntimeError("slack remove_reaction unavailable")
        await super().remove_reaction(channel, ts, emoji, raise_on_error)

    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None,
                           unfurl_media=None):
        if "post_message" in self.fail:
            raise RuntimeError("slack post_message unavailable")
        return await super().post_message(channel, text, thread_ts, unfurl_links, unfurl_media)

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None,
                          unfurl_media=None):
        if "post_blocks" in self.fail:
            raise RuntimeError("slack post_blocks unavailable")
        return await super().post_blocks(channel, blocks, text, thread_ts, unfurl_links,
                                         unfurl_media)


def _raising_sel(*, method: str) -> MagicMock:
    """A ``sel()`` factory whose *method* raises, for audit-sink failure arms."""
    audit = MagicMock()
    getattr(audit, method).side_effect = RuntimeError("audit sink down")
    return MagicMock(return_value=audit)


async def _drain() -> None:
    """Let ``asyncio.ensure_future`` callbacks queued by the code under test run."""
    for _ in range(4):
        await asyncio.sleep(0)


def _texts(slack: MockSlackClient) -> str:
    return "\n".join(
        str(a[1].get("text") or "")
        for a in slack.actions
        if a[0] in ("post", "update", "blocks", "ephemeral")
    )


# ──────────────────────────────────────────────────────────────────────
# state hygiene
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_state():
    """Reset the module globals these tests touch, before and after."""

    def _reset() -> None:
        h._titled_threads.clear()
        # The auto-title lock is a module global created inside whichever event
        # loop ran first. Reusing one across loops deadlocks, so drop it and let
        # each test's loop build its own.
        h._auto_title_lock = None
        h._thread_agents.clear()
        h._thread_projects.clear()
        h._hydrated_sessions.clear()
        h._thread_temporary.clear()
        h._thread_incognito.clear()
        h._pending_approvals.clear()
        h._linked_approvals.clear()
        h._trusted_sessions.clear()
        h._cached_default_agent = None
        h._dashboard_state = None
        h._orch_cfg = None
        h._tracking_channels = set()

    _reset()
    yield
    _reset()


@pytest.fixture()
def owner(monkeypatch):
    monkeypatch.setattr(h, "_owner_id", "U1")
    return "U1"


@pytest.fixture()
def sessions():
    """SessionManager double. ``_session_map=None`` keeps flag hydration inert."""
    sm = MagicMock()
    sm._session_map = None
    sm.try_acquire = AsyncMock(return_value=True)
    sm.has_session = MagicMock(return_value=False)
    sm.release = MagicMock()
    sm.destroy = AsyncMock()
    sm.discard_conversation = AsyncMock()
    sm.get_session_for_thread = MagicMock(return_value=None)
    return sm


# ──────────────────────────────────────────────────────────────────────
# StatusReactionController — Slack API failures and the stall watchdog
# ──────────────────────────────────────────────────────────────────────
class TestStatusReactionControllerFailures:
    """Every reaction call is best-effort; none may propagate."""

    @pytest.mark.asyncio
    async def test_swap_emoji_swallows_add_and_remove_failures(self):
        slack = FlakySlack("add_reaction", "remove_reaction")
        ctrl = h.StatusReactionController(slack, "C1", "1.0")
        try:
            # Immediate phase: add_reaction raises inside _swap_emoji.
            ctrl.set_phase("queued")
            await _drain()
            assert ctrl._current_emoji == h._PHASE_EMOJIS["queued"]

            # Debounced phase fired by hand: now there IS an old emoji, so the
            # remove_reaction failure arm runs too.
            ctrl.set_phase("coding")
            ctrl._cancel_debounce()
            ctrl._fire_debounce()
            await _drain()
            assert ctrl._current_emoji == h._PHASE_EMOJIS["coding"]
        finally:
            ctrl._cancel_debounce()
            ctrl._cancel_stall_timers()

    @pytest.mark.asyncio
    async def test_apply_pending_is_a_noop_once_finalized(self):
        slack = FlakySlack()
        ctrl = h.StatusReactionController(slack, "C1", "1.0")
        ctrl._pending_phase = "coding"
        ctrl._finalized = True
        await ctrl._apply_pending()
        assert ctrl._current_emoji is None

    @pytest.mark.asyncio
    async def test_stall_emojis_upgrade_and_finalize_despite_failures(self):
        slack = FlakySlack("add_reaction", "remove_reaction")
        ctrl = h.StatusReactionController(slack, "C1", "1.0")
        try:
            ctrl._on_stall_soft()
            await _drain()
            assert ctrl._stall_emoji == h._STALL_EMOJI_SOFT

            # Upgrading soft -> hard removes the previous stall emoji first;
            # that remove_reaction raises and must be swallowed.
            ctrl._on_stall_hard()
            await _drain()
            assert ctrl._stall_emoji == h._STALL_EMOJI_HARD

            # finalize() clears the stall emoji before the terminal swap.
            ctrl.finalize(error=True)
            await _drain()
            assert ctrl._stall_emoji is None
            assert ctrl._current_emoji == h._PHASE_EMOJIS["error"]
        finally:
            ctrl._cancel_stall_timers()

    @pytest.mark.asyncio
    async def test_add_stall_emoji_after_finalize_is_dropped(self):
        slack = FlakySlack()
        ctrl = h.StatusReactionController(slack, "C1", "1.0")
        ctrl._finalized = True
        await ctrl._add_stall_emoji(h._STALL_EMOJI_SOFT)
        assert ctrl._stall_emoji is None

    @pytest.mark.asyncio
    async def test_reset_watchdog_clears_stall_emoji_and_swallows_failure(self):
        slack = FlakySlack("remove_reaction")
        ctrl = h.StatusReactionController(slack, "C1", "1.0")
        try:
            ctrl._stall_emoji = h._STALL_EMOJI_SOFT
            ctrl._reset_stall_watchdog()
            await _drain()
            assert ctrl._stall_emoji is None
            assert ctrl._stall_soft_handle is not None
        finally:
            ctrl._cancel_stall_timers()

    @pytest.mark.asyncio
    async def test_reset_watchdog_arms_nothing_while_paused(self):
        slack = FlakySlack()
        ctrl = h.StatusReactionController(slack, "C1", "1.0")
        ctrl.pause_stall_watchdog()
        ctrl._reset_stall_watchdog()
        assert ctrl._stall_soft_handle is None
        assert ctrl._stall_hard_handle is None
        # on_progress() must stay inert while paused.
        ctrl.on_progress()
        assert ctrl._stall_soft_handle is None

    @pytest.mark.asyncio
    async def test_disabled_controller_never_arms_timers(self):
        slack = FlakySlack()
        ctrl = h.StatusReactionController(slack, "C1", "1.0", enabled=False)
        ctrl._reset_stall_watchdog()
        ctrl.resume_stall_watchdog()
        ctrl.set_phase("coding")
        ctrl.finalize()
        assert ctrl._stall_soft_handle is None
        assert ctrl._debounce_handle is None
        assert ctrl._current_emoji is None


# ──────────────────────────────────────────────────────────────────────
# config writers — read/write failures must surface as ValueError
# ──────────────────────────────────────────────────────────────────────
class TestConfigWriteFailures:
    """Both writers fail CLOSED: a bad read must never write a {} baseline."""

    @pytest.fixture(autouse=True)
    def _local_config(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8", newline="\n")
        monkeypatch.setattr(h, "config_path", lambda: cfg)
        return cfg

    def test_set_default_agent_read_error(self, monkeypatch):
        def _boom(_path, *, mutate):
            raise ConfigReadError("config.json is not valid JSON")

        monkeypatch.setattr(h, "update_config_locked", _boom)
        with pytest.raises(ValueError, match="Failed to read config"):
            h._set_default_agent("kirocrew")
        assert h._cached_default_agent is None

    def test_set_default_agent_write_error(self, monkeypatch):
        def _boom(_path, *, mutate):
            raise OSError("disk full")

        monkeypatch.setattr(h, "update_config_locked", _boom)
        with pytest.raises(ValueError, match="Failed to write config"):
            h._set_default_agent("kirocrew")
        # Cache must NOT advance past a failed write.
        assert h._cached_default_agent is None

    def test_persist_channel_config_read_error(self, monkeypatch):
        def _boom(_path, *, mutate):
            raise ConfigReadError("truncated file")

        monkeypatch.setattr(h, "update_config_locked", _boom)
        with pytest.raises(ValueError, match="Failed to read config"):
            h._persist_channel_config("C1", activation="mention")

    def test_persist_channel_config_write_error(self, monkeypatch):
        def _boom(_path, *, mutate):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(h, "update_config_locked", _boom)
        with pytest.raises(ValueError, match="Failed to write config"):
            h._persist_channel_config("C1", agent="kirocrew")

    def test_persist_channel_config_merges_both_fields(self, monkeypatch, tmp_path):
        # Real file through the real locked primitive: the write must merge
        # into existing settings, not overwrite them.
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"slack": {"bot": "x"}}), encoding="utf-8", newline="\n")
        monkeypatch.setattr(h, "config_path", lambda: cfg)
        h._persist_channel_config("C9", activation="always", agent="kirocrew")
        written = json.loads(cfg.read_text(encoding="utf-8"))
        ch = written["slack"]["channels"]["C9"]
        assert ch == {"activation": "always", "agent": "kirocrew"}
        # Sibling keys survive the merge.
        assert written["slack"]["bot"] == "x"


class TestSensitivePathRefusal:
    def test_both_writers_refuse_a_sensitive_config_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(h, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(h, "is_sensitive_path", lambda _p: True)
        with pytest.raises(ValueError, match="sensitive path"):
            h._set_default_agent("kirocrew")
        with pytest.raises(ValueError, match="sensitive path"):
            h._persist_channel_config("C1", activation="mention")


# ──────────────────────────────────────────────────────────────────────
# set_yolo_mode — the headless --slack-only startup path
# ──────────────────────────────────────────────────────────────────────
class TestSetYoloMode:
    def test_enabled_grants_a_standing_yolo(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(h, "apply_config_duration", lambda: calls.append("duration"))
        monkeypatch.setattr(h, "grant_declared_yolo", lambda: calls.append("grant"))
        h.set_yolo_mode(True)
        assert calls == ["duration", "grant"]

    def test_disabled_only_applies_the_configured_duration(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(h, "apply_config_duration", lambda: calls.append("duration"))
        monkeypatch.setattr(h, "grant_declared_yolo", lambda: calls.append("grant"))
        h.set_yolo_mode(False)
        assert calls == ["duration"]


# ──────────────────────────────────────────────────────────────────────
# agent-name resolution — project specs, path validation, bad JSON
# ──────────────────────────────────────────────────────────────────────
class TestResolveAgentName:
    def test_project_spec_wins_over_user_level_agents(self, monkeypatch, tmp_path):
        spec = tmp_path / "reviewer.agent-spec.json"
        spec.write_text("{}", encoding="utf-8", newline="\n")
        monkeypatch.setattr(h, "_discover_project_agents", lambda _d: [spec])
        monkeypatch.setattr(h, "project_agent_name", lambda p: "project-reviewer")
        assert h._resolve_agent_name("reviewer", str(tmp_path)) == "project-reviewer"

    def test_unsafe_agent_path_resolves_to_none(self, monkeypatch, tmp_path):
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "helper.json").write_text('{"name": "helper"}', encoding="utf-8", newline="\n")
        monkeypatch.setattr(h, "_discover_project_agents", lambda _d: [])
        monkeypatch.setattr(h, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(h, "validate_file_path", lambda _s: None)
        assert h._resolve_agent_name("helper") is None

    def test_unparseable_spec_falls_back_to_the_file_stem(self, monkeypatch, tmp_path):
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "helper.json").write_text("{not json", encoding="utf-8", newline="\n")
        monkeypatch.setattr(h, "_discover_project_agents", lambda _d: [])
        monkeypatch.setattr(h, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(h, "validate_file_path", lambda s: s)
        assert h._resolve_agent_name("helper") == "helper"


class TestIterCcAgentNames:
    def _cc_dir(self, tmp_path: Path) -> Path:
        agents = tmp_path / "plugin" / "agents"
        agents.mkdir(parents=True)
        (agents / "one.md").write_text(
            "---\nname: reviewer\n---\nbody\n", encoding="utf-8", newline="\n"
        )
        return tmp_path

    def test_unreadable_spec_is_skipped(self, monkeypatch, tmp_path):
        cc = self._cc_dir(tmp_path)
        monkeypatch.setattr(h, "safe_read_file_bytes", lambda _p: None)
        assert list(h._iter_cc_agent_names(cc)) == []

    def test_reader_exception_is_swallowed(self, monkeypatch, tmp_path):
        cc = self._cc_dir(tmp_path)

        def _boom(_p):
            raise OSError("permission denied")

        monkeypatch.setattr(h, "safe_read_file_bytes", _boom)
        assert list(h._iter_cc_agent_names(cc)) == []

    def test_missing_directory_yields_nothing(self, tmp_path):
        assert list(h._iter_cc_agent_names(tmp_path / "absent")) == []

    def test_declared_name_is_yielded(self, tmp_path):
        assert list(h._iter_cc_agent_names(self._cc_dir(tmp_path))) == ["reviewer"]


# ──────────────────────────────────────────────────────────────────────
# !compact — every cosmetic/teardown failure arm
# ──────────────────────────────────────────────────────────────────────
def _provider(*, compact_raises: bool = False, result: str = "completed") -> MagicMock:
    provider = MagicMock()
    if compact_raises:
        provider.compact = AsyncMock(side_effect=RuntimeError("transport closed"))
    else:
        provider.compact = AsyncMock(return_value=None)
    summary = "boom" if result == "failed" else ""
    provider.wait_for_compaction = AsyncMock(return_value={"type": result, "summary": summary})
    return provider


class TestCompactCommandFailureArms:
    @pytest.mark.asyncio
    async def test_pre_ui_failure_does_not_abort_compaction(self, sessions):
        slack = FlakySlack("add_reaction")
        sessions.get_provider = MagicMock(return_value=_provider())
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        assert "✅ Context compacted." in _texts(slack)
        sessions.release.assert_called_once_with("slack:t1")

    @pytest.mark.asyncio
    async def test_compact_failure_tears_down_even_when_every_cleanup_fails(
        self, sessions, monkeypatch
    ):
        slack = FlakySlack("post_message", "remove_reaction")
        sessions.get_provider = MagicMock(return_value=_provider(compact_raises=True))
        sessions.discard_conversation = AsyncMock(side_effect=RuntimeError("registry locked"))
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        sessions.discard_conversation.assert_awaited_once_with("slack:t1")
        sessions.release.assert_called_once_with("slack:t1")

    @pytest.mark.asyncio
    async def test_reporting_and_audit_failures_are_swallowed(self, sessions, monkeypatch):
        slack = FlakySlack("post_message", "remove_reaction")
        sessions.get_provider = MagicMock(return_value=_provider())
        monkeypatch.setattr(h, "sel", _raising_sel(method="log_tool_invocation"))
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        sessions.release.assert_called_once_with("slack:t1")

    @pytest.mark.asyncio
    async def test_failed_verdict_reports_the_backend_error(self, sessions):
        slack = FlakySlack()
        sessions.get_provider = MagicMock(return_value=_provider(result="failed"))
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        assert "Compaction failed: boom" in _texts(slack)

    @pytest.mark.asyncio
    async def test_unknown_verdict_reports_a_timeout(self, sessions):
        slack = FlakySlack()
        sessions.get_provider = MagicMock(return_value=_provider(result="pending"))
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        assert "Compaction timed out." in _texts(slack)

    @pytest.mark.asyncio
    async def test_busy_session_refuses_instead_of_destroying_it(self, sessions):
        slack = FlakySlack()
        sessions.try_acquire = AsyncMock(return_value=False)
        sessions.has_session = MagicMock(return_value=True)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        assert "Still working on your last message" in _texts(slack)
        sessions.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_after_acquire(self, sessions):
        slack = FlakySlack()
        sessions.get_provider = MagicMock(return_value=None)
        await h._handle_compact_command(slack, sessions, "C1", "t1", "m1", "slack:t1")
        assert "No active session to compact." in _texts(slack)
        sessions.release.assert_called_once_with("slack:t1")


# ──────────────────────────────────────────────────────────────────────
# !unknown — the catch-all that must not fall through to the LLM
# ──────────────────────────────────────────────────────────────────────
class TestUnknownBangCommand:
    @pytest.mark.asyncio
    async def test_unknown_command_is_answered_not_forwarded(self, sessions, owner):
        slack = FlakySlack()
        reply = await h._handle_slash_command(
            "!definitelynotacommand", slack, sessions, "C1", "t1", "m1", "slack:t1", owner
        )
        # "" (not None) is what stops handle_message forwarding to the agent.
        assert reply == ""
        assert "Unknown command" in _texts(slack)


# ──────────────────────────────────────────────────────────────────────
# small command helpers — empty-argument early returns
# ──────────────────────────────────────────────────────────────────────
class TestCommandHelperEarlyReturns:
    def test_spawn_with_no_task_declines(self):
        manager = MagicMock()
        assert h._do_spawn("", manager) is None
        manager.spawn.assert_not_called()

    def test_spawn_keyword_without_prefix_declines(self):
        assert h._handle_spawn_command("summarize this", MagicMock()) is None

    @pytest.mark.asyncio
    async def test_task_run_with_no_argument_declines(self):
        runner = MagicMock()
        out = await h._handle_run_command("task run", runner, FlakySlack(), "C1", "t1")
        assert out is None
        out = await h._handle_run_command("task run   ", runner, FlakySlack(), "C1", "t1")
        assert out is None

    @pytest.mark.asyncio
    async def test_task_run_missing_spec_file(self, tmp_path):
        runner = MagicMock()
        runner.running = False
        missing = tmp_path / "no-such-spec.yaml"
        out = await h._handle_run_command(
            f"task run {missing}", runner, FlakySlack(), "C1", "t1"
        )
        assert out is not None and "Spec file not found" in out

    @pytest.mark.asyncio
    async def test_cron_command_needs_an_action(self):
        cron = MagicMock()
        assert await h._handle_cron_command("cron", cron, "C1", "t1") is None
        assert await h._handle_cron_command("notcron list", cron, "C1", "t1") is None
        assert await h._handle_cron_command("cron remove", cron, "C1", "t1") is None
        assert await h._handle_cron_command("cron bogus job1", cron, "C1", "t1") is None


# ──────────────────────────────────────────────────────────────────────
# auto-title — manual-title race and conversation-log failure
# ──────────────────────────────────────────────────────────────────────
class _TitleClient:
    """Minimal ACP client double that streams a one-chunk title."""

    def __init__(self, title: str = "Deploy the gateway") -> None:
        self._title = title

    async def stream(self, _prompt):
        yield LLMEvent(kind=h.EVENT_TEXT_CHUNK, text=self._title)
        yield LLMEvent(kind=h.EVENT_COMPLETE)

    async def reject_tool(self, _request_id):  # pragma: no cover - not reached here
        return None


@pytest.fixture()
def title_sessions():
    sm = MagicMock()
    sm.get_or_create = AsyncMock(return_value=(_TitleClient(), None, None))
    sm.release = MagicMock()
    return sm


class TestAutoTitle:
    @pytest.mark.asyncio
    async def test_manual_title_set_mid_stream_wins(self, title_sessions):
        slack = FlakySlack()
        h._titled_threads["slack:t1"] = "manual"
        await h._maybe_auto_title_slack(
            slack, title_sessions, "C1", "slack:t1", None, "hello", "hi there"
        )
        assert not [a for a in slack.actions if a[0] == "set_thread_title"]

    @pytest.mark.asyncio
    async def test_conversation_log_failure_still_titles_the_thread(self, title_sessions):
        slack = FlakySlack()
        log = MagicMock()
        log.set_title = MagicMock(side_effect=RuntimeError("log locked"))
        await h._maybe_auto_title_slack(
            slack, title_sessions, "C1", "slack:t1", log, "hello", "hi there"
        )
        titled = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert titled and titled[0][1]["title"] == "Deploy the gateway"
        # A log failure must not mark the thread untitled (no retry storm).
        assert "slack:t1" not in h._titled_threads

    @pytest.mark.asyncio
    async def test_skip_verdict_allows_a_later_retry(self, title_sessions):
        slack = FlakySlack()
        title_sessions.get_or_create = AsyncMock(return_value=(_TitleClient("SKIP"), None, None))
        h._titled_threads["slack:t1"] = "auto"
        await h._maybe_auto_title_slack(
            slack, title_sessions, "C1", "slack:t1", None, "hello", "hi"
        )
        assert "slack:t1" not in h._titled_threads
        assert not [a for a in slack.actions if a[0] == "set_thread_title"]


# ──────────────────────────────────────────────────────────────────────
# handle_message — early keyword dispatch and permission replies
# ──────────────────────────────────────────────────────────────────────
async def _msg(slack, sessions, text, *, user="U1", **kw):
    await h.handle_message(slack, sessions, "C1", text, None, "m1", user, **kw)


class TestHandleMessageKeywordDispatch:
    @pytest.mark.asyncio
    async def test_linked_thread_intercept_short_circuits(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "maybe_route_linked_thread", AsyncMock(return_value=True))
        await _msg(slack, sessions, "anything at all")
        assert slack.actions == []

    # NOTE: the bare ``status`` keyword (handler.py:2642) is NOT covered here.
    # ``handle_message`` re-imports ``current_context`` locally further down its
    # own body (handler.py:3705), which makes the name local to the whole
    # function, so the ``status`` branch raises ``UnboundLocalError`` before it
    # can reply. Covering it would mean asserting the broken behaviour; the
    # defect is reported instead.

    @pytest.mark.asyncio
    async def test_sessions_keyword_is_audited_and_rendered(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        called: dict = {}

        async def _fake(cmd_text, _slack, channel, reply_ts, msg_ts, session_key, log,
                        *, sessions=None):
            called["session_key"] = session_key

        monkeypatch.setattr(h, "_handle_sessions_command", _fake)
        await _msg(slack, sessions, "sessions")
        assert called["session_key"] == h.canonical_key("m1")

    @pytest.mark.asyncio
    async def test_sessions_keyword_denies_a_non_owner(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "_handle_sessions_command", AsyncMock())
        await _msg(slack, sessions, "sessions", user="U999")
        assert "Permission denied" in _texts(slack)
        h._handle_sessions_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_compact_keyword_dispatches_for_the_owner(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "_handle_compact_command", AsyncMock())
        await _msg(slack, sessions, "!compact")
        h._handle_compact_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compact_keyword_denied_for_a_stranger(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "_handle_compact_command", AsyncMock())
        await _msg(slack, sessions, "!compact", user="U999")
        assert "Not authorized to compact." in _texts(slack)
        h._handle_compact_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_user_command_denied_for_a_stranger(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "_handle_slash_command", AsyncMock(return_value=""))
        await _msg(slack, sessions, "!dashboard", user="U999")
        assert "Not authorized." in _texts(slack)
        h._handle_slash_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_only_command_denied_for_a_stranger(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "_handle_slash_command", AsyncMock(return_value=""))
        await _msg(slack, sessions, "!agent kirocrew", user="U999")
        assert "Owner-only command." in _texts(slack)
        h._handle_slash_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mention_prefix_is_stripped_before_command_matching(
        self, sessions, owner, monkeypatch
    ):
        slack = FlakySlack()
        monkeypatch.setattr(h, "_handle_compact_command", AsyncMock())
        await _msg(slack, sessions, "<@UBOT|kirocrew> !compact")
        h._handle_compact_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_governance_denial_drops_the_message(self, sessions, owner, monkeypatch):
        slack = FlakySlack()
        monkeypatch.setattr(h, "channel_inbound_permitted", AsyncMock(return_value=False))
        await _msg(slack, sessions, "status")
        assert slack.actions == []

    @pytest.mark.asyncio
    async def test_hook_auto_reply_answers_without_touching_acp(self, sessions, owner):
        slack = FlakySlack()
        builder = MagicMock()
        builder.hooks.on_message = MagicMock(
            return_value=MagicMock(action=h.HOOK_REPLY, text="canned answer")
        )
        await _msg(slack, sessions, "ping", context_builder=builder)
        assert "canned answer" in _texts(slack)
        sessions.get_or_create.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# _safe_update / _safe_final_update — Slack edit failures
# ──────────────────────────────────────────────────────────────────────
class TestSafeUpdates:
    @pytest.mark.asyncio
    async def test_safe_update_truncates_and_swallows_failure(self):
        class _Boom(MockSlackClient):
            async def update_message(self, channel, ts, text):
                raise RuntimeError("message_not_found")

        await h._safe_update(_Boom(), "C1", "1.0", "x" * (h.SLACK_MSG_LIMIT + 50))

    @pytest.mark.asyncio
    async def test_safe_final_update_swallows_both_edit_and_followup_failures(self):
        slack = FlakySlack("post_message")

        class _BoomUpdate(FlakySlack):
            async def update_message(self, channel, ts, text):
                raise RuntimeError("message_not_found")

        boom = _BoomUpdate("post_message")
        await h._safe_final_update(boom, "C1", "1.0", "y" * (h.SLACK_MSG_LIMIT * 2), "t1")
        assert isinstance(slack, FlakySlack)


# ──────────────────────────────────────────────────────────────────────
# agent listing — union across both sources, dedup, hidden variant
# ──────────────────────────────────────────────────────────────────────
class TestListAllAgentNames:
    def test_union_dedups_and_hides_the_internal_variant(self, monkeypatch, tmp_path):
        agents = tmp_path / "agents"
        agents.mkdir()
        for stem in ("alpha", "shared", "kirocrew-lite"):
            (agents / f"{stem}.json").write_text(
                json.dumps({"name": stem}), encoding="utf-8", newline="\n"
            )
        monkeypatch.setattr(h, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(
            h, "_iter_cc_agent_names", lambda _d=None: iter(["shared", "beta", "kirocrew-lite"])
        )
        out = h._list_all_agent_names()
        names = [n.strip() for n in out.split(",")]
        assert names == ["alpha", "shared", "beta"]

    def test_empty_sources_report_none_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(h, "kiro_agents_dir", lambda: tmp_path / "absent")
        monkeypatch.setattr(h, "_iter_cc_agent_names", lambda _d=None: iter([]))
        assert h._list_all_agent_names() == "(none found)"
