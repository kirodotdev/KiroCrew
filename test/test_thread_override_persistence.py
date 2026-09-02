"""Tests for thread override persistence: _hydrate_thread_overrides, !project, _discover_project_agents."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from conftest import MockSlackClient
from kiro_crew.slack.handler import (
    _discover_project_agents,
    _hydrate_thread_overrides,
    _hydrated_sessions,
    _resolve_agent_name,
    _thread_agents,
    _thread_projects,
)


@pytest.fixture(autouse=True)
def _clean_thread_state():
    """Clear module-level thread state between tests."""
    _thread_agents.clear()
    _thread_projects.clear()
    _hydrated_sessions.clear()
    yield
    _thread_agents.clear()
    _thread_projects.clear()
    _hydrated_sessions.clear()


class FakeConversationLog:
    """Minimal ConversationLog stub for metadata operations."""

    def __init__(self, metadata: dict | None = None):
        self._metadata: dict[str, dict] = metadata or {}
        self.updates: list[tuple[str, dict]] = []

    def get_metadata(self, key: str) -> dict:
        return self._metadata.get(key, {})

    def update_metadata(self, key: str, fields: dict) -> None:
        self.updates.append((key, fields))
        self._metadata.setdefault(key, {}).update(fields)


class FakeSessionManager:
    """Minimal SessionManager stub."""

    def __init__(self):
        self.removed: list[str] = []

    async def remove(self, key):
        self.removed.append(key)

    def set_slack_link(self, *args):
        pass


class TestHydrateThreadOverrides:
    """Tests for _hydrate_thread_overrides function."""

    def test_hydrate_sets_agent_from_metadata(self):
        log = FakeConversationLog({"t1": {"agent": "my-agent"}})
        _hydrate_thread_overrides("t1", log)
        assert _thread_agents["t1"] == "my-agent"

    def test_hydrate_sets_project_from_metadata(self):
        log = FakeConversationLog({"t2": {"project": "/home/user/my-project"}})
        _hydrate_thread_overrides("t2", log)
        assert _thread_projects["t2"] == "/home/user/my-project"

    def test_hydrate_all_fields(self):
        log = FakeConversationLog({"t4": {"agent": "coder", "project": "/opt/proj"}})
        _hydrate_thread_overrides("t4", log)
        assert _thread_agents["t4"] == "coder"
        assert _thread_projects["t4"] == "/opt/proj"

    def test_hydrate_skips_if_already_hydrated(self):
        _hydrated_sessions.add("t5")
        log = FakeConversationLog({"t5": {"agent": "override-this"}})
        _hydrate_thread_overrides("t5", log)
        # Should not populate since session was already hydrated
        assert "t5" not in _thread_agents

    def test_hydrate_adds_to_hydrated_set(self):
        log = FakeConversationLog({"t5b": {"agent": "my-agent"}})
        _hydrate_thread_overrides("t5b", log)
        assert "t5b" in _hydrated_sessions
        assert _thread_agents["t5b"] == "my-agent"

    def test_hydrate_adds_to_hydrated_set_even_if_no_log(self):
        _hydrate_thread_overrides("t5c", None)
        assert "t5c" in _hydrated_sessions

    def test_hydrate_noop_if_no_conversation_log(self):
        _hydrate_thread_overrides("t6", None)
        assert "t6" not in _thread_agents

    def test_hydrate_noop_if_empty_metadata(self):
        log = FakeConversationLog({})
        _hydrate_thread_overrides("t7", log)
        assert "t7" not in _thread_agents
        assert "t7" not in _thread_projects

    def test_hydrate_handles_get_metadata_exception(self):
        log = FakeConversationLog({})
        log.get_metadata = MagicMock(side_effect=RuntimeError("db error"))
        _hydrate_thread_overrides("t8", log)
        assert "t8" not in _thread_agents

    def test_hydrate_ignores_falsy_agent(self):
        log = FakeConversationLog({"t9": {"agent": "", "project": ""}})
        _hydrate_thread_overrides("t9", log)
        assert "t9" not in _thread_agents
        assert "t9" not in _thread_projects

    def test_hydrate_rejects_sensitive_project_path(self):
        # Defense-in-depth: a tampered/corrupted metadata project path that
        # resolves to a sensitive credential dir must never enter the cache.
        log = FakeConversationLog({"t10": {"project": "/home/user/.aws"}})
        with patch("kiro_crew.slack.handler.is_sensitive_path", return_value=True):
            _hydrate_thread_overrides("t10", log)
        assert "t10" not in _thread_projects

    def test_hydrate_accepts_non_sensitive_project_path(self):
        log = FakeConversationLog({"t11": {"project": "/home/user/safe-proj"}})
        with patch("kiro_crew.slack.handler.is_sensitive_path", return_value=False):
            _hydrate_thread_overrides("t11", log)
        assert _thread_projects["t11"] == "/home/user/safe-proj"


class TestDiscoverProjectAgents:
    """Tests for _discover_project_agents function."""

    def test_returns_empty_when_no_project_dir(self):
        assert _discover_project_agents(None) == []
        assert _discover_project_agents("") == []

    def test_returns_empty_when_no_kiro_dir(self, tmp_path):
        assert _discover_project_agents(str(tmp_path)) == []

    def test_finds_agent_spec_json(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "my-agent.agent-spec.json"
        spec.write_text(json.dumps({"name": "my-agent"}))
        result = _discover_project_agents(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "my-agent.agent-spec.json"

    def test_finds_agents_subdir_json(self, tmp_path):
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        agent_file = agents_dir / "coder.json"
        agent_file.write_text(json.dumps({"name": "coder"}))
        result = _discover_project_agents(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "coder.json"

    def test_finds_both_spec_and_subdir(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "alpha.agent-spec.json"
        spec.write_text(json.dumps({"name": "alpha"}))
        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        agent_file = agents_dir / "beta.json"
        agent_file.write_text(json.dumps({"name": "beta"}))
        result = _discover_project_agents(str(tmp_path))
        assert len(result) == 2
        stems = [r.stem for r in result]
        assert "alpha.agent-spec" in stems
        assert "beta" in stems

    def test_results_sorted_by_stem(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        for name in ["zebra", "alpha", "mid"]:
            (kiro / f"{name}.agent-spec.json").write_text(json.dumps({"name": name}))
        result = _discover_project_agents(str(tmp_path))
        stems = [r.stem for r in result]
        assert stems == sorted(stems)


class TestResolveAgentNameWithProject:
    """Tests for _resolve_agent_name with project_dir parameter."""

    def test_project_agent_takes_priority(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "local-agent.agent-spec.json"
        spec.write_text(json.dumps({"name": "local-agent"}))
        result = _resolve_agent_name("local-agent", str(tmp_path))
        assert result == "local-agent"

    def test_project_agent_by_stem_without_suffix(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "mybot.agent-spec.json"
        spec.write_text(json.dumps({"name": "mybot-resolved"}))
        result = _resolve_agent_name("mybot", str(tmp_path))
        assert result == "mybot-resolved"

    def test_falls_through_to_global_when_not_in_project(self, tmp_path):
        # Empty project .kiro — should fall through to global search
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        # The global search will find nothing for a fake agent
        result = _resolve_agent_name("nonexistent-xyz", str(tmp_path))
        assert result is None

    def test_non_matching_specs_are_never_read(self, tmp_path, monkeypatch):
        """Resolution runs on the event loop, so it must prefilter on the FILENAME.
        Reading every spec to compare its declared name stalls Slack and the gateway
        on a checkout with many agents or slow storage; at most the one matching file
        is read."""
        import kiro_crew.slack.handler as h

        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        for other in ("alpha", "beta", "gamma"):
            (kiro / f"{other}.agent-spec.json").write_text(json.dumps({"name": other}))
        (kiro / "wanted.agent-spec.json").write_text(json.dumps({"name": "wanted-resolved"}))

        read: list[str] = []
        original = h.project_agent_name

        def _tracking(spec):
            read.append(spec.name)
            return original(spec)

        monkeypatch.setattr(h, "project_agent_name", _tracking)
        assert _resolve_agent_name("wanted", str(tmp_path)) == "wanted-resolved"
        assert read == ["wanted.agent-spec.json"]

    def test_none_project_dir_uses_global_only(self):
        result = _resolve_agent_name("nonexistent-xyz-123")
        assert result is None

    def test_project_agent_json_read_error_returns_clean_stem(self, tmp_path):
        # On JSON decode error the fallback must strip the ".agent-spec"
        # suffix — the user typed "broken", so "broken.agent-spec" would not
        # resolve downstream.
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "broken.agent-spec.json"
        spec.write_text("not valid json {{{{")
        result = _resolve_agent_name("broken", str(tmp_path))
        assert result == "broken"


@pytest.mark.asyncio
class TestProjectCommand:
    """Tests for the !project slash command."""

    async def test_project_show_current_empty(self):
        from kiro_crew.slack.handler import _handle_slash_command

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        result = await _handle_slash_command(
            "!project", slack, sessions, "C1", "t1", "m1", "t1", "U1"
        )
        assert result == ""
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("No project set" in p[1]["text"] for p in posts)

    async def test_project_show_current_set(self):
        from kiro_crew.slack.handler import _handle_slash_command

        _thread_projects["t1"] = "/my/project"
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        result = await _handle_slash_command(
            "!project", slack, sessions, "C1", "t1", "m1", "t1", "U1"
        )
        assert result == ""
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("/my/project" in p[1]["text"] for p in posts)

    async def test_project_set_valid_dir(self, tmp_path):
        from kiro_crew.slack.handler import _handle_slash_command

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        log = FakeConversationLog()
        result = await _handle_slash_command(
            f"!project {tmp_path}",
            slack,
            sessions,
            "C1",
            "t1",
            "m1",
            "t1",
            "U1",
            conversation_log=log,
        )
        assert result == ""
        assert _thread_projects["t1"] == str(tmp_path.resolve())
        assert len(log.updates) == 1
        assert log.updates[0][1]["project"] == str(tmp_path.resolve())
        # Session should be removed so new provider picks up project
        assert "t1" in sessions.removed

    async def test_project_set_invalid_dir(self):
        from kiro_crew.slack.handler import _handle_slash_command

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        result = await _handle_slash_command(
            "!project /nonexistent/path/xyz123",
            slack,
            sessions,
            "C1",
            "t1",
            "m1",
            "t1",
            "U1",
        )
        assert result == ""
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Not a directory" in p[1]["text"] for p in posts)
        assert "t1" not in _thread_projects

    async def test_project_off_clears(self):
        from kiro_crew.slack.handler import _handle_slash_command

        _thread_projects["t1"] = "/some/path"
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        log = FakeConversationLog()
        result = await _handle_slash_command(
            "!project off",
            slack,
            sessions,
            "C1",
            "t1",
            "m1",
            "t1",
            "U1",
            conversation_log=log,
        )
        assert result == ""
        assert "t1" not in _thread_projects
        assert log.updates[0][1]["project"] == ""

    async def test_project_discovers_agents(self, tmp_path):
        from kiro_crew.slack.handler import _handle_slash_command

        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "devbot.agent-spec.json"
        spec.write_text(json.dumps({"name": "devbot"}))

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        log = FakeConversationLog()
        result = await _handle_slash_command(
            f"!project {tmp_path}",
            slack,
            sessions,
            "C1",
            "t1",
            "m1",
            "t1",
            "U1",
            conversation_log=log,
        )
        assert result == ""
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("devbot" in p[1]["text"] for p in posts)


@pytest.mark.asyncio
class TestTaCommandPersistence:
    """Tests for !ta command metadata persistence."""

    async def test_ta_persists_agent_to_log(self, tmp_path):
        from kiro_crew.slack.handler import _handle_slash_command, set_owner_id

        set_owner_id("U1")
        # Create a real agent file so resolution works
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_file = agents_dir / "test-agent.json"
        agent_file.write_text(json.dumps({"name": "test-agent"}))

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        log = FakeConversationLog()

        with patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            # Create .kiro/agents structure
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            kiro_file = kiro_agents / "test-agent.json"
            kiro_file.write_text(json.dumps({"name": "test-agent"}))

            result = await _handle_slash_command(
                "!ta test-agent",
                slack,
                sessions,
                "C1",
                "t1",
                "m1",
                "t1",
                "U1",
                conversation_log=log,
            )
        assert result == ""
        assert _thread_agents["t1"] == "test-agent"
        assert len(log.updates) == 1
        assert log.updates[0][1]["agent"] == "test-agent"

    async def test_ta_off_clears_and_persists(self):
        from kiro_crew.slack.handler import _handle_slash_command, set_owner_id

        set_owner_id("U1")
        _thread_agents["t1"] = "some-agent"
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        log = FakeConversationLog()
        result = await _handle_slash_command(
            "!ta off",
            slack,
            sessions,
            "C1",
            "t1",
            "m1",
            "t1",
            "U1",
            conversation_log=log,
        )
        assert result == ""
        assert "t1" not in _thread_agents
        assert log.updates[0][1] == {"agent": ""}


@pytest.mark.asyncio
class TestProjectSensitivePathCheck:
    """Tests for sensitive path enforcement in !project command."""

    async def test_project_rejects_sensitive_path(self, tmp_path):
        from kiro_crew.slack.handler import _handle_slash_command

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        with patch("kiro_crew.slack.handler.is_sensitive_path", return_value=True):
            with patch("kiro_crew.slack.handler.os.path.realpath", return_value="/sensitive/dir"):
                with patch(
                    "kiro_crew.slack.handler.os.path.expanduser", return_value="/sensitive/dir"
                ):
                    result = await _handle_slash_command(
                        "!project /sensitive/dir",
                        slack,
                        sessions,
                        "C1",
                        "t1",
                        "m1",
                        "t1",
                        "U1",
                    )
        assert result == ""
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Cannot use sensitive path" in p[1]["text"] for p in posts)
        assert "t1" not in _thread_projects

    async def test_project_sensitive_path_emits_sel_audit(self, tmp_path):
        from kiro_crew.slack.handler import _handle_slash_command

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        with patch("kiro_crew.slack.handler.sel") as mock_sel:
            with patch("kiro_crew.slack.handler.is_sensitive_path", return_value=True):
                with patch(
                    "kiro_crew.slack.handler.os.path.realpath", return_value="/sensitive/dir"
                ):
                    with patch(
                        "kiro_crew.slack.handler.os.path.expanduser", return_value="/sensitive/dir"
                    ):
                        await _handle_slash_command(
                            "!project /sensitive/dir",
                            slack,
                            sessions,
                            "C1",
                            "t1",
                            "m1",
                            "t1",
                            "U1",
                        )
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "project_denied_sensitive"
        assert kwargs["tool_name"] == "!project"

    async def test_project_invalid_dir_emits_sel_audit(self):
        from kiro_crew.slack.handler import _handle_slash_command

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        with patch("kiro_crew.slack.handler.sel") as mock_sel:
            await _handle_slash_command(
                "!project /nonexistent/path/xyz123",
                slack,
                sessions,
                "C1",
                "t1",
                "m1",
                "t1",
                "U1",
            )
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "project_denied_invalid"
        assert kwargs["tool_name"] == "!project"

    async def test_project_off_removes_session(self):
        from kiro_crew.slack.handler import _handle_slash_command

        _thread_projects["t1"] = "/some/path"
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        log = FakeConversationLog()
        result = await _handle_slash_command(
            "!project off",
            slack,
            sessions,
            "C1",
            "t1",
            "m1",
            "t1",
            "U1",
            conversation_log=log,
        )
        assert result == ""
        assert "t1" not in _thread_projects
        # Session should be removed so provider restarts without old project
        assert "t1" in sessions.removed


class TestDiscoverProjectAgentsSensitivePath:
    """Sensitive-path enforcement for project agent discovery.

    The guard lives in ``agent_discovery.project_agent_files``, the implementation
    the Slack handler now shares with the dashboard picker and spawn validation, so
    it is patched there — patching the Slack module would miss it and the test would
    pass while enforcing nothing.
    """

    def test_returns_empty_for_sensitive_path(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "agent.agent-spec.json"
        spec.write_text(json.dumps({"name": "agent"}))
        with patch("kiro_crew.agent_discovery.is_sensitive_path", return_value=True):
            result = _discover_project_agents(str(tmp_path))
        assert result == []

    def test_returns_agents_for_non_sensitive_path(self, tmp_path):
        kiro = tmp_path / ".kiro"
        kiro.mkdir()
        spec = kiro / "agent.agent-spec.json"
        spec.write_text(json.dumps({"name": "agent"}))
        with patch("kiro_crew.agent_discovery.is_sensitive_path", return_value=False):
            result = _discover_project_agents(str(tmp_path))
        assert len(result) == 1
