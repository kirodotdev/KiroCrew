"""kiro-cli ``toolsSettings`` alias spellings on KAS, and the shell-family ids.

kiro-cli folds each alias group to one key, keeping the spelling first in its
group (``ToolsSettings::ALIAS_GROUPS``, ``canonicalize_tools_settings_aliases``).
Crew's translation must read the same one key, and a key it cannot place must
restrict, never grant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import spec_keys_without_carrier, to_client_custom_agent
from kiro_crew.acp.kas_permissions import (
    _ANSWERED_CAPABILITIES,
    WITHHELD_FROM_AUTO_APPROVE,
    authored_restricted_capabilities,
    canonical_tools_settings,
    crew_answered_commands,
    tools_settings_to_permissions,
    trusted_agents_withheld,
    withhold_answered_under_authored_rules,
)
from kiro_crew.agent_sdk import kas_tool_view
from kiro_crew.agent_sdk.kas_tool_view import tools_settings_family, tools_settings_unenforced
from kiro_crew.platform import allowed_commands

_ALL = frozenset({"fs_read", "fs_write", "shell"})


def _spec(**over):
    base = {"name": "a1", "prompt": "p", "tools": ["*"], "includeMcpJson": False}
    base.update(over)
    return base


class TestCanonical:
    @pytest.mark.parametrize(
        ("alias", "canonical"),
        [
            ("executeBash", "shell"),
            ("executeCmd", "shell"),
            ("execute_cmd", "shell"),
            ("fsRead", "read"),
            ("fsWrite", "write"),
            ("webFetch", "web_fetch"),
            ("crew", "subagent"),
            ("agentCrew", "subagent"),
            ("agent_crew", "subagent"),
        ],
    )
    def test_an_unknown_spelling_is_renamed(self, alias, canonical):
        out, dropped = canonical_tools_settings({alias: {"k": 1}})
        assert out == {canonical: {"k": 1}}
        assert dropped == []

    @pytest.mark.parametrize("key", ["shell", "execute_bash", "fs_read", "write"])
    def test_a_spelling_crew_reads_keeps_its_name(self, key):
        assert canonical_tools_settings({key: {"k": 1}}) == ({key: {"k": 1}}, [])

    def test_the_sub_agent_group_is_always_filed_under_subagent(self):
        """The spawn gate and the notice read only `subagent`, so a kept
        `use_subagent` is filed there too and the two cannot disagree."""
        assert canonical_tools_settings({"use_subagent": {"k": 1}}) == ({"subagent": {"k": 1}}, [])

    def test_the_first_spelling_of_the_group_wins(self):
        # kiro-cli group order, not the spec's key order.
        raw = {"execute_bash": {"a": 1}, "executeBash": {"b": 2}, "shell": {"c": 3}}
        out, dropped = canonical_tools_settings(raw)
        assert out == {"shell": {"c": 3}}
        assert dropped == ["executeBash", "execute_bash"]

    def test_a_renamed_winner_drops_the_later_spelling(self):
        out, dropped = canonical_tools_settings({"crew": {"a": 1}, "subagent": {"b": 2}})
        assert out == {"subagent": {"a": 1}}
        assert dropped == ["subagent"]

    def test_other_keys_and_non_objects_pass_through(self):
        raw = {"@srv": {"x": 1}, "glob": {"y": 2}}
        assert canonical_tools_settings(raw) == (raw, [])
        assert canonical_tools_settings(None) == (None, [])
        assert canonical_tools_settings(["x"]) == (["x"], [])

    def test_it_is_idempotent_and_does_not_mutate(self):
        raw = {"fsRead": {"deniedPaths": ["x"]}, "read": {"deniedPaths": ["y"]}}
        once, _ = canonical_tools_settings(raw)
        assert canonical_tools_settings(once) == (once, [])
        assert raw == {"fsRead": {"deniedPaths": ["x"]}, "read": {"deniedPaths": ["y"]}}


class TestTranslationAfterCanonical:
    def test_a_camel_case_shell_deny_travels(self):
        settings, _ = canonical_tools_settings({"executeBash": {"deniedCommands": ["rm .*"]}})
        block, lost = tools_settings_to_permissions(settings)
        assert block == {"rules": [{"capability": "shell", "match": ["rm *"], "effect": "deny"}]}
        assert lost == []

    def test_only_the_kept_shell_key_answers_commands(self):
        settings, _ = canonical_tools_settings(
            {
                "shell": {"allowedCommands": ["git status"]},
                "execute_bash": {"allowedCommands": ["rm -rf /"]},
            }
        )
        assert crew_answered_commands(settings) == ["git status"]

    def test_the_projection_reads_the_alias(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        out = to_client_custom_agent(
            "a1", _spec(toolsSettings={"fsRead": {"deniedPaths": ["~/.ssh"]}}), "p"
        )
        assert {
            "capability": "fs_read",
            "match": ["~/.ssh", "~/.ssh/**"],
            "effect": "deny",
        } in out["permissions"]["rules"]


class TestFailClosed:
    @pytest.mark.parametrize("key", ["executeBash", "fsRead", "fsWrite"])
    def test_an_alias_restricts_its_capability(self, key):
        family = {"executeBash": "shell", "fsRead": "fs_read", "fsWrite": "fs_write"}[key]
        assert authored_restricted_capabilities(None, {key: {"x": 1}}) == {family}

    def test_an_unplaceable_key_restricts_all_three(self):
        assert authored_restricted_capabilities(None, {"execute_shell": {"x": 1}}) == _ALL

    @pytest.mark.parametrize(
        "key", ["@srv", "@srv/tool", "web_fetch", "webFetch", "crew", "use_aws"]
    )
    def test_a_placed_non_builtin_key_restricts_nothing(self, key):
        assert authored_restricted_capabilities(None, {key: {"x": 1}}) == frozenset()

    def test_an_unplaceable_key_withholds_the_shell_grant(self):
        assert (
            withhold_answered_under_authored_rules(
                {"execute_bash", "fs_read"}, None, tools_settings={"bashTool": {"x": 1}}
            )
            == frozenset()
        )


class TestNotice:
    def test_a_dropped_duplicate_is_named(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: True)
        spec = _spec(
            toolsSettings={
                "read": {"deniedPaths": ["x"]},
                "fs_read": {"deniedPaths": ["y"]},
            }
        )
        assert spec_keys_without_carrier(spec, translates_tools_settings=True) == [
            "toolsSettings.fs_read"
        ]

    def test_trusted_agents_dropped_by_the_ceiling_are_named(self, monkeypatch):
        spec = _spec(toolsSettings={"subagent": {"trustedAgents": ["reviewer"]}})
        withhold, permit = (lambda ref: False), (lambda ref: True)
        monkeypatch.setattr(kas_agents, "may_skip_gate_for_session", lambda *_a: withhold)
        assert spec_keys_without_carrier(spec, translates_tools_settings=True) == [
            "toolsSettings.subagent.trustedAgents"
        ]
        monkeypatch.setattr(kas_agents, "may_skip_gate_for_session", lambda *_a: permit)
        assert spec_keys_without_carrier(spec, translates_tools_settings=True) == []

    @pytest.mark.parametrize(
        ("settings", "named"),
        [
            (
                {"subagent": {"trustedAgents": ["r"], "x": 1}},
                ["subagent.trustedAgents", "subagent.x"],
            ),
            (
                {"use_subagent": {"trustedAgents": ["r"], "x": 1}},
                ["use_subagent.trustedAgents", "use_subagent.x"],
            ),
            ({"agentCrew": {"trustedAgents": ["r"]}}, ["agentCrew.trustedAgents"]),
            # kiro-cli keeps `subagent` whole, so only its keys are read.
            (
                {"subagent": {"trustedAgents": ["r"]}, "use_subagent": {"trustedAgents": ["r"]}},
                ["subagent.trustedAgents", "use_subagent"],
            ),
            (
                {"subagent": {"trustedAgents": ["r"]}, "use_subagent": {"x": 1}},
                ["subagent.trustedAgents", "use_subagent"],
            ),
        ],
    )
    def test_a_kept_alias_is_named_as_the_spec_wrote_it(self, monkeypatch, settings, named):
        # The session's own predicate, which the trustedAgents check asks.
        monkeypatch.setattr(kas_agents, "may_skip_gate_for_session", lambda *_a: lambda ref: False)
        spec = _spec(toolsSettings=settings)
        got = spec_keys_without_carrier(spec, translates_tools_settings=True)
        assert got == [f"toolsSettings.{path}" for path in named]
        # The worker policy's tool view names the same spellings.
        unenforced = [p for p in named if not p.endswith(".trustedAgents")]
        assert tools_settings_unenforced(settings) == unenforced

    def test_an_unreadable_entry_is_named_as_the_spec_wrote_it(self, monkeypatch):
        # `\p{N}` is outside the regex syntax Crew reads, so the entry is unusable: the
        # session notice names the entry and why, the worker policy's view the key.
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        entry = r"ls \p{N}"
        settings = {"executeBash": {"allowedCommands": [entry]}}
        reason = allowed_commands.refusal_reason(entry)
        spec = _spec(toolsSettings=settings)
        got = spec_keys_without_carrier(spec, translates_tools_settings=True)
        assert reason and got == [f"toolsSettings.executeBash.allowedCommands: {entry} ({reason})"]
        assert tools_settings_unenforced(settings) == ["executeBash.allowedCommands"]
        # kirocrew doctor's row names the same spelling. Looked up when called, so a
        # module without it fails this test, not the file.
        unread = kas_tool_view.unreadable_allowed_commands(settings)
        assert unread == [("executeBash", entry, reason)]

    def test_an_ascii_class_entry_is_named_for_the_crewmate_as_the_spec_wrote_it(self):
        # `\d` is carried for the session, but read over ASCII, so a crewmate's
        # policy names it under the alias the spec used.
        settings = {"executeBash": {"allowedCommands": [r"ls \d"]}}
        assert tools_settings_unenforced(settings) == ["executeBash.allowedCommands"]

    def test_withheld_write_paths_are_named_as_the_spec_wrote_them(self, monkeypatch):
        # The ceiling withholds the write grant, so `allowedPaths` go unanswered.
        monkeypatch.setattr(kas_agents, "may_grant_builtin_at_gate_now", lambda tool: False)
        spec = _spec(toolsSettings={"fsWrite": {"allowedPaths": ["/tmp/x"]}})
        got = spec_keys_without_carrier(spec, translates_tools_settings=True)
        assert got == ["toolsSettings.fsWrite.allowedPaths"]

    def test_an_untranslatable_trusted_agents_entry_is_not_named_twice(self):
        # `?` has no shared reading, so the translation already names it.
        assert (
            trusted_agents_withheld({"subagent": {"trustedAgents": ["rev?"]}}, lambda r: False)
            == []
        )

    def test_the_tool_view_names_the_dropped_duplicate(self):
        assert tools_settings_unenforced(
            {"shell": {"deniedCommands": ["rm"]}, "executeBash": {"deniedCommands": ["ls"]}}
        ) == ["executeBash"]

    @pytest.mark.parametrize(
        ("key", "family"),
        [
            ("executeBash", "shell"),
            ("execute_bash", "shell"),
            ("fsWrite", "write"),
            ("crew", "subagent"),
            ("aws", "use_aws"),
            ("useAws", "use_aws"),
        ],
    )
    def test_the_tool_view_reads_every_spelling_as_one_family(self, key, family):
        assert tools_settings_family(key) == family


class TestPwshProcessControl:
    def test_it_is_withheld_and_read_as_shell(self):
        assert "control_pwsh_process" in WITHHELD_FROM_AUTO_APPROVE
        assert _ANSWERED_CAPABILITIES["control_pwsh_process"] == frozenset({"shell"})

    def test_every_recorded_process_control_id_is_withheld_as_shell(self):
        reg = json.loads(
            (Path(__file__).parent / "fixtures" / "kas_builtin_tool_ids.json").read_text()
        )
        ids = reg["process_control"]["ids"]
        assert set(ids) == {"control_bash_process", "control_pwsh_process"}
        for kas_id in ids:
            assert kas_id in WITHHELD_FROM_AUTO_APPROVE, kas_id
            assert _ANSWERED_CAPABILITIES[kas_id] == frozenset({"shell"}), kas_id


class TestOtherReadersAgree:
    def test_the_spawn_gate_reads_the_alias_kiro_cli_keeps(self):
        from kiro_crew.subagent_parts.spawn_allowlists import spawn_allowlist

        assert spawn_allowlist({"toolsSettings": {"crew": {"availableAgents": []}}}) == ()
        both = {"crew": {"availableAgents": []}, "subagent": {"availableAgents": ["*"]}}
        assert spawn_allowlist({"toolsSettings": both}) == ()
        only = {"use_subagent": {"availableAgents": []}}
        assert spawn_allowlist({"toolsSettings": only}) == ()

    def test_the_read_only_side_spec_keeps_a_base_fs_read_deny(self):
        from kiro_crew.dashboard.side_readonly_spec import derive_readonly_spec

        base = {
            "name": "base",
            "prompt": "p",
            "toolsSettings": {"fs_read": {"deniedPaths": ["/secret/**"]}},
        }
        derived = derive_readonly_spec(base, base_name="base", denied_read_paths=["~/.ssh/**"])
        settings = derived["toolsSettings"]
        assert "read" not in settings
        assert settings["fs_read"]["deniedPaths"] == ["/secret/**", "~/.ssh/**"]
        assert settings["grep"]["deniedPaths"] == ["~/.ssh/**"]
        block, _ = tools_settings_to_permissions(canonical_tools_settings(settings)[0])
        matches = {m for r in block["rules"] for m in r["match"]}
        assert {"/secret/**", "~/.ssh/**"} <= matches

    def test_the_direct_client_allowlists_read_the_alias_kiro_cli_keeps(self):
        from kiro_crew.acp.spec_tools_allowlists import spec_shell_patterns

        aliased = {"executeBash": {"denyByDefault": True, "allowedCommands": ["make"]}}
        assert spec_shell_patterns({"toolsSettings": aliased}) == ("make",)
        both = {
            "shell": {"denyByDefault": True, "allowedCommands": ["make"]},
            "execute_bash": {"denyByDefault": True, "allowedCommands": [".*"]},
        }
        assert spec_shell_patterns({"toolsSettings": both}) == ("make",)

    def test_the_gate_allowlist_notice_reads_the_alias_kiro_cli_keeps(self):
        from kiro_crew.acp.spec_tools_allowlists import spec_shell_patterns
        from kiro_crew.agent_sdk.tools_settings_reach import tools_settings_unapplied

        both = {
            "shell": {"allowedCommands": ["git status"]},
            "execute_bash": {"denyByDefault": True, "allowedCommands": ["ls"]},
        }
        assert spec_shell_patterns({"toolsSettings": both}) is None
        assert tools_settings_unapplied(both, gate_allowlists=True) == ["toolsSettings"]
        aliased = {"executeBash": {"denyByDefault": True, "allowedCommands": ["ls"]}}
        assert spec_shell_patterns({"toolsSettings": aliased}) == ("ls",)
        assert tools_settings_unapplied(aliased, gate_allowlists=True) == []
        spawn = {"crew": {"availableAgents": []}, "subagent": {"availableAgents": ["*"]}}
        assert tools_settings_unapplied(spawn) == ["toolsSettings.subagent"]

    def test_the_notice_never_calls_an_enforced_use_subagent_list_unapplied(self):
        from kiro_crew.agent_sdk.tools_settings_reach import tools_settings_unapplied
        from kiro_crew.subagent_parts.spawn_allowlists import spawn_allowlist

        only = {"use_subagent": {"availableAgents": ["reviewer"]}}
        assert spawn_allowlist({"toolsSettings": only}) == ("reviewer",)
        assert tools_settings_unapplied(only) == []
        assert tools_settings_unapplied(only, gate_allowlists=True) == []
