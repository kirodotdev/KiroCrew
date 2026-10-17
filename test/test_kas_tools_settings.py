"""``toolsSettings`` on KAS: translated into ``permissions`` instead of dropped.

KAS has no wire slot for ``toolsSettings`` and replaces it with ``permissions``.
These tests pin the translation (``kas_permissions.tools_settings_to_permissions``),
its path through the wire projection, and the session-start notice, which now
names only what the translation cannot carry.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import spec_keys_without_carrier, to_client_custom_agent
from kiro_crew.acp.kas_permissions import _regex_to_glob, tools_settings_to_permissions


def _rules(tools_settings):
    block, _ = tools_settings_to_permissions(tools_settings)
    return block["rules"] if block else []


def _lost(tools_settings):
    return tools_settings_to_permissions(tools_settings)[1]


def _spec(**over):
    base = {"name": "a1", "prompt": "p", "tools": ["*"], "includeMcpJson": False}
    base.update(over)
    return base


class TestRegexToGlob:
    @pytest.mark.parametrize(
        ("regex", "glob"),
        [
            ("git push .*", "git push *"),
            ("^rm -rf .*$", "rm -rf *"),
            (r".*pastebin\.com.*", "*pastebin.com*"),
            (r"\Agit status\z", "git status"),
            (".*", "*"),
        ],
    )
    def test_literal_plus_dot_star_translates(self, regex, glob):
        assert _regex_to_glob(regex) == glob

    @pytest.mark.parametrize(
        "regex",
        [
            "git (push|commit) .*",
            "docs.aws",
            "rm -r[f]",
            "ls +",
            r"echo \*",
            r"\d+",
            "^$",
            "a?",
            # Literal glob syntax: KAS would read these as a class or a negation.
            r"git commit -m \[wip\].*",
            r"echo \{a\}",
            "rm !important",
        ],
    )
    def test_other_regex_is_refused(self, regex):
        assert _regex_to_glob(regex) is None


class TestTranslation:
    def test_denied_paths_become_filesystem_denies(self):
        rules = _rules(
            {
                "fs_read": {"deniedPaths": ["~/.ssh/**"]},
                "glob": {"deniedPaths": ["~/.netrc"]},
                "write": {"deniedPaths": ["~/.aws/**"]},
            }
        )
        assert {
            "capability": "fs_read",
            "match": ["~/.ssh/**"],
            "effect": "deny",
        } in rules
        assert {
            "capability": "fs_read",
            "match": ["~/.netrc", "~/.netrc/**"],
            "effect": "deny",
        } in rules
        assert {
            "capability": "fs_write",
            "match": ["~/.aws/**"],
            "effect": "deny",
        } in rules

    def test_denied_commands_become_shell_denies(self):
        rules = _rules({"execute_bash": {"deniedCommands": ["git push .*", "(a|b)"]}})
        assert rules == [{"capability": "shell", "match": ["git push *"], "effect": "deny"}]
        assert _lost({"execute_bash": {"deniedCommands": ["(a|b)"]}}) == [
            "execute_bash.deniedCommands"
        ]

    def test_a_bare_path_also_denies_its_subtree(self):
        # KAS globs match a path exactly, so a bare directory gets a `/**` twin.
        assert _rules({"read": {"deniedPaths": ["~/.ssh", "~/.aws/", "~/k/*"]}}) == [
            {
                "capability": "fs_read",
                "match": ["~/.ssh", "~/.aws/", "~/k/*", "~/.ssh/**", "~/.aws/**"],
                "effect": "deny",
            }
        ]

    def test_shell_and_filesystem_allows_never_become_rules(self):
        settings = {
            "shell": {"allowedCommands": ["git status"]},
            "read": {"allowedPaths": ["./src/**"]},
            "write": {"allowedPaths": ["./out/**"]},
        }
        assert _rules(settings) == []
        assert _lost(settings) == [
            "read.allowedPaths",
            "shell.allowedCommands",
            "write.allowedPaths",
        ]

    def test_deny_by_default_is_reported_not_translated(self):
        settings = {"shell": {"allowedCommands": ["git status"], "denyByDefault": True}}
        assert _rules(settings) == []
        assert _lost(settings) == ["shell.allowedCommands", "shell.denyByDefault"]

    def test_write_fallback_deny_is_reported_not_translated(self):
        settings = {"fs_write": {"allowedPaths": ["./out/**"], "fallbackAction": "deny"}}
        assert _rules(settings) == []
        assert _lost(settings) == ["fs_write.allowedPaths", "fs_write.fallbackAction"]
        assert _lost({"fs_write": {"fallbackAction": "interactive"}}) == []

    def test_web_fetch_blocked_matches_the_host_only(self):
        settings = {"web_fetch": {"blocked": [r".*pastebin\.com.*", r"https://evil\.com/.*"]}}
        assert _rules(settings) == [
            {"capability": "web_fetch", "match": ["*pastebin.com*"], "effect": "deny"}
        ]
        assert _lost(settings) == ["web_fetch.blocked"]

    def test_available_agents_is_reported_not_translated(self):
        # An empty list means "spawn nothing", so it is named too.
        for value in (["rev*"], []):
            settings = {"subagent": {"availableAgents": value}}
            assert _rules(settings) == []
            assert _lost(settings) == ["subagent.availableAgents"]

    def test_allow_lists_are_never_emitted(self):
        settings = {
            "web_fetch": {"trusted": [r".*docs\.example\.com.*"]},
            "subagent": {"trustedAgents": ["reviewer"]},
        }
        assert _rules(settings) == []
        assert _lost(settings) == ["subagent.trustedAgents", "web_fetch.trusted"]

    def test_an_over_long_pattern_is_reported_and_the_rest_still_travel(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        long = "/x" * 600
        spec = _spec(toolsSettings={"read": {"deniedPaths": [long, "~/.ssh"]}})
        assert _lost(spec["toolsSettings"]) == ["read.deniedPaths"]
        rules = to_client_custom_agent("a1", spec, "p")["permissions"]["rules"]
        assert {
            "capability": "fs_read",
            "match": ["~/.ssh", "~/.ssh/**"],
            "effect": "deny",
        } in rules

    def test_unknown_and_unsupported_entries_are_reported(self):
        settings = {
            "use_aws": {"allowedServices": ["s3"]},
            "shell": {"autoAllowReadonly": True},
            "grep": {"allowReadOnly": True},
            "empty": {},
            "off": {"autoAllowReadonly": False},
        }
        assert _rules(settings) == []
        assert _lost(settings) == [
            "grep.allowReadOnly",
            "shell.autoAllowReadonly",
            "use_aws.allowedServices",
        ]

    def test_large_lists_are_split_not_refused(self):
        rules = _rules({"read": {"deniedPaths": [f"/p{i}/**" for i in range(130)]}})
        assert [len(r["match"]) for r in rules] == [64, 64, 2]

    @pytest.mark.parametrize("value", [None, [], "x", {}])
    def test_nothing_to_translate(self, value):
        assert tools_settings_to_permissions(value) == (None, [])


class TestWireProjection:
    def test_denies_travel_whatever_the_ceiling(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: False)
        out = to_client_custom_agent(
            "a1", _spec(toolsSettings={"read": {"deniedPaths": ["~/.ssh"]}}), "p"
        )
        assert {"capability": "fs_read", "match": ["~/.ssh", "~/.ssh/**"], "effect": "deny"} in out[
            "permissions"
        ]["rules"]

    def test_a_shell_allow_never_travels(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: True)
        spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
        rules = to_client_custom_agent("a1", spec, "p").get("permissions", {}).get("rules", [])
        assert not [r for r in rules if r["capability"] == "shell" and r["effect"] == "allow"]


class TestNotice:
    def test_fully_translated_settings_are_not_reported_on_kas(self):
        spec = _spec(toolsSettings={"read": {"deniedPaths": ["x"]}})
        assert spec_keys_without_carrier(spec, translates_tools_settings=True) == []

    def test_only_the_untranslated_entries_are_named_on_kas(self):
        spec = _spec(
            slashCommand="x",
            toolsSettings={
                "read": {"deniedPaths": ["x"]},
                "shell": {"autoAllowReadonly": True},
            },
        )
        assert spec_keys_without_carrier(spec, translates_tools_settings=True) == [
            "slashCommand",
            "toolsSettings.shell.autoAllowReadonly",
        ]

    def test_a_backend_without_the_translation_names_the_whole_key(self):
        """goose and opencode fire spec hooks too, but never receive the rules."""
        spec = _spec(toolsSettings={"read": {"deniedPaths": ["x"]}})
        assert spec_keys_without_carrier(spec) == ["toolsSettings"]
