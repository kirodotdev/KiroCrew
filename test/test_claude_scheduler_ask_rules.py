"""Claude Code's own scheduler tools ask Crew before they run.

In ``default`` and ``auto`` mode claude-agent-acp runs ``CronCreate``, ``CronDelete``
and ``ScheduleWakeup`` with no ``session/request_permission``, so Crew's approval path
never sees work Claude schedules for later. A ``permissions.ask`` rule for each sends
the call to the host gate. These tests pin that every settings payload Crew renders
carries those rules: the seed on disk for an ordinary chat, for one whose spec also
gives deny rules and a model, and for a chat that asked for ``auto``, the inline
settings of the exclusion path (merged with the project's own ask rules), a Side Chat
session's inline settings, and the re-seed of a seed an older build left behind.
"""

from __future__ import annotations

import json

from acp_seed_provenance_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_seed_provenance_helpers import isolated_records  # noqa: F401
from acp_seed_provenance_helpers import (
    _client,
    _seed,
    _settings,
    _the_owning_process_died,
)
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import _write_spec

from kiro_crew import model_registry as mr
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import client_claude_seed
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO

_SCHEDULER_TOOLS = ["CronCreate", "CronDelete", "ScheduleWakeup"]


def _cold_cache(monkeypatch) -> None:
    """No advertised models, so the payload carries only the permission keys."""
    monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})


def _project_owned(tmp_path, payload: dict) -> None:
    """A project's OWN ``.claude/settings.local.json``, written before Crew runs."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestSchedulerAskRules:
    def test_the_rules_name_the_scheduling_tools_and_not_cron_list(self):
        rules = getattr(client_claude_seed, "_CLAUDE_SCHEDULER_ASK_RULES", ())
        assert list(rules) == _SCHEDULER_TOOLS
        assert "CronList" not in rules

    def test_an_ordinary_chat_seed_asks_before_scheduling(self, tmp_path, monkeypatch):
        _cold_cache(monkeypatch)
        client = _client(tmp_path)
        client._write_claude_local_settings()

        assert client._claude_settings_authored is True
        assert _seed(tmp_path) == {"permissions": {"ask": _SCHEDULER_TOOLS}}

    def test_a_seed_with_deny_rules_and_a_model_still_asks(self, tmp_path, monkeypatch, agents_dir):
        """The rules ride beside the spec's deny rules and a pinned model, not instead."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": ["claude-sonnet-4-5"]})
        monkeypatch.setattr(
            client_claude_seed, "other_sources_define_available_models", lambda *a, **k: False
        )
        _write_spec(
            agents_dir,
            servers={"srv": {"command": "/bin/srv", "disabledTools": ["danger"]}},
            tools=["@srv"],
        )
        client = _client(tmp_path, agent="kirocrew", model="claude-sonnet-4-5")
        client._write_claude_local_settings()

        seed = _seed(tmp_path)
        assert "mcp__srv__danger" in seed["permissions"]["deny"]
        assert seed["model"] == "claude-sonnet-4-5"
        assert seed["permissions"].get("ask") == _SCHEDULER_TOOLS

    def test_a_requested_auto_chat_seed_still_asks(self, tmp_path, monkeypatch):
        """``auto`` lets Claude's classifier approve a call; an ask rule outranks it."""
        _cold_cache(monkeypatch)
        client = _client(tmp_path, permission_mode=CC_PERMISSION_MODE_AUTO)
        client._write_claude_local_settings()

        permissions = _seed(tmp_path)["permissions"]
        assert permissions["defaultMode"] == CC_PERMISSION_MODE_AUTO
        assert permissions["ask"] == _SCHEDULER_TOOLS

    def test_the_exclusion_path_carries_the_rules_inline(self, tmp_path, monkeypatch):
        """A project that owns its file gets Crew's rules inline, merged with its own.

        The project's duplicate ``CronCreate`` is dropped by the exclusion path's own
        merge in ``_exclude_foreign_local_settings``, not by the payload's ask block.
        """
        _cold_cache(monkeypatch)
        _project_owned(tmp_path, {"permissions": {"ask": ["Bash(git push:*)", "CronCreate"]}})
        client = _client(tmp_path)
        client._claude_adapter_disk_version = "0.84.0"
        client._write_claude_local_settings()

        assert client._claude_local_settings_excluded is True
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]
        assert options["settings"]["permissions"]["ask"] == [
            *_SCHEDULER_TOOLS,
            "Bash(git push:*)",
        ]
        assert {("ask", tool) for tool in _SCHEDULER_TOOLS} <= client._claude_shipped_restrictions

    def test_a_side_chat_on_the_exclusion_path_keeps_both_rule_sets(self, tmp_path, monkeypatch):
        _cold_cache(monkeypatch)
        _project_owned(tmp_path, {"permissions": {}})
        client = _client(tmp_path, side_read_only=True)
        client._claude_adapter_disk_version = "0.84.0"
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True
        client._claude_side_read_denies = client_mod._claude_side_read_deny_rules()

        ask = client._claude_session_meta()["claudeCode"]["options"]["settings"]["permissions"][
            "ask"
        ]
        assert ask == [*_SCHEDULER_TOOLS, *client_mod._CLAUDE_SIDE_READ_ONLY_ASK_RULES]

    def test_a_seed_an_older_build_left_behind_is_upgraded(self, tmp_path, monkeypatch):
        """An orphan with no ask rules is adopted by digest and re-seeded with them."""
        _cold_cache(monkeypatch)
        path = _settings(tmp_path)
        path.parent.mkdir(parents=True)
        older = json.dumps({"permissions": {"defaultMode": "default"}}, indent=2) + "\n"
        path.write_text(older, encoding="utf-8")
        assert sp.record(path, older, "older-build") is True
        _the_owning_process_died()

        client = _client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()

        assert client._claude_settings_authored is True
        assert _seed(tmp_path) == {
            "permissions": {"defaultMode": "default", "ask": _SCHEDULER_TOOLS}
        }

    def test_a_live_older_seed_without_the_rules_is_not_shared(self, tmp_path, monkeypatch):
        """A sibling's seed that lacks the ask rules does not govern this session."""
        _cold_cache(monkeypatch)
        path = _settings(tmp_path)
        path.parent.mkdir(parents=True)
        older = json.dumps({"permissions": {"defaultMode": "default"}}, indent=2) + "\n"
        path.write_text(older, encoding="utf-8")
        assert sp.record(path, older, "live-sibling") is True

        client = _client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()

        assert client._claude_settings_shared is False
        assert client._claude_settings_authored is False
        assert path.read_text(encoding="utf-8") == older
