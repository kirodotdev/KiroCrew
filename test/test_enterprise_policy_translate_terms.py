"""The existing policy-language terms Kiro Crew reads (RFC crew-lts-edition).

Each term is translated and then asked through the real parser and evaluator in an
LTS build, so a row is pinned by the decision its Crew read sites get: allow,
deny, a narrowed match, a value with no Crew form, and no rule (the baseline).
"""

from __future__ import annotations

import ntpath
import sys

import pytest

from kiro_crew.platform import enterprise_policy_translate as t
from kiro_crew.platform import governance
from kiro_crew.platform.guest_paths import GuestPath

#: The remote workspace's directory on its Linux guest.
_GUEST_ROOT = "/workspace/app"


@pytest.fixture(name="enterprise", autouse=True)
def enterprise_fixture(_floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


#: The file cases use POSIX paths; on Windows an item gains a drive letter the
#: patterns lack, so POSIX path matching is the subject and Windows is skipped.
_POSIX_PATHS = pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")


def _ceiling(*rules: str):
    body = "".join(f"  - {r}\n" for r in rules) if rules else "  []\n"
    return governance.parse_policy(t.translate("rules:\n" + body))


def _permits(ceiling, scope: str, item: str) -> bool:
    return governance.resolve(ceiling, None, scope, item).permitted


def _grant(capability: str, patterns: str) -> tuple:
    """The control plane's shape for a scoped grant: the allow and its deny exclude."""
    return (
        f"{{capability: {capability}, effect: allow, match: {patterns}}}",
        f"{{capability: {capability}, effect: deny, exclude: {patterns}}}",
    )


# (term, Crew scope, the item a rule names, another item, a matching narrowed pattern)
_RULESET_TERMS = [
    ("shell", "commands", "git status", "rm -rf x", "['git *']"),
    pytest.param(
        "fs_read",
        "filesystem.read",
        "/srv/data/a.txt",
        "/etc/hosts",
        "['/srv/data/**']",
        marks=_POSIX_PATHS,
    ),
    pytest.param(
        "fs_write",
        "filesystem.write",
        "/srv/data/a.txt",
        "/etc/hosts",
        "['/srv/data']",
        marks=_POSIX_PATHS,
    ),
    ("network_egress", "network.egress", "api.corp.example", "evil.example", "['*.corp.example']"),
    ("mcp", "mcp", "@github/read", "@jira/read", "['github/*']"),
    ("subagent", "capabilities.spawn", "agents:reviewer", "agents:other", "['reviewer']"),
    ("messaging_channel", "channels", "slack", "discord", "['slack']"),
]


@pytest.mark.parametrize("term, scope, item, other, patterns", _RULESET_TERMS)
class TestRulesetTerms:
    def test_allow_permits_everything(self, term, scope, item, other, patterns):
        ceiling = _ceiling(f"{{capability: {term}, effect: allow}}")
        assert _permits(ceiling, scope, item) and _permits(ceiling, scope, other)

    def test_deny_permits_nothing(self, term, scope, item, other, patterns):
        ceiling = _ceiling(
            f"{{capability: {term}, effect: allow}}", f"{{capability: {term}, effect: deny}}"
        )
        assert not _permits(ceiling, scope, item) and not _permits(ceiling, scope, other)

    def test_a_narrowed_grant_permits_only_its_match(self, term, scope, item, other, patterns):
        ceiling = _ceiling(*_grant(term, patterns))
        assert _permits(ceiling, scope, item)
        assert not _permits(ceiling, scope, other)

    def test_a_deny_match_beside_an_allow_takes_only_its_match_away(
        self, term, scope, item, other, patterns
    ):
        ceiling = _ceiling(
            f"{{capability: {term}, effect: allow}}",
            f"{{capability: {term}, effect: deny, match: {patterns}}}",
        )
        assert not _permits(ceiling, scope, item)
        assert _permits(ceiling, scope, other)

    def test_a_malformed_value_is_refused(self, term, scope, item, other, patterns):
        with pytest.raises(t.TranslationError):
            t.translate(f"rules:\n  - {{capability: {term}, effect: deny, match: slack}}\n")
        with pytest.raises(t.TranslationError):
            t.translate(f"rules:\n  - {{capability: {term}, effect: maybe}}\n")

    def test_a_deny_excluding_everything_permits_everything(
        self, term, scope, item, other, patterns
    ):
        ceiling = _ceiling(f"{{capability: {term}, effect: deny, exclude: ['*']}}")
        assert _permits(ceiling, scope, item) and _permits(ceiling, scope, other)

    def test_absent_leaves_the_scope_unnamed(self, term, scope, item, other, patterns):
        doc = t.translate("rules:\n  - {capability: power, effect: deny}\n")
        node = doc
        for key in t.MANAGED[term].path[:-1]:
            node = node.get(key, {})
        assert t.MANAGED[term].path[-1] not in node


class TestAbsentTermsKeepTheBaseline:
    """No rule: the open controls keep Crew's default; the rest keep the baseline (off)."""

    @_POSIX_PATHS
    def test_open_controls_stay_open(self):
        ceiling = _ceiling()
        assert _permits(ceiling, "commands", "ls")
        assert _permits(ceiling, "filesystem.read", "/srv/a")
        assert _permits(ceiling, "filesystem.write", "/srv/a")
        assert _permits(ceiling, "mcp", "@github/read")
        assert _permits(ceiling, "capabilities.spawn", "agents:reviewer")

    def test_closed_controls_stay_off(self):
        ceiling = _ceiling()
        assert not _permits(ceiling, "network.egress", "example.com")
        assert not _permits(ceiling, "channels", "slack")


class TestUnknownValues:
    """A value with no Crew form: an allowed one is dropped, a denied one widened or refused."""

    def test_a_shell_pattern_reads_question_marks_and_brackets_literally(self):
        ceiling = _ceiling(*_grant("shell", "['ls [ab]?']"))
        assert _permits(ceiling, "commands", "ls [ab]?")
        assert not _permits(ceiling, "commands", "ls ax")

    def test_a_padded_star_is_not_everything_for_shell(self):
        """Spaces count in a command pattern, so ``" * "`` is not ``*``."""
        ceiling = _ceiling(*_grant("shell", "[' * ']"))
        assert not _permits(ceiling, "commands", "echo ok")
        assert _permits(ceiling, "commands", " echo ok ")

    def test_a_shell_match_is_case_sensitive(self):
        ceiling = _ceiling(
            "{capability: shell, effect: allow}",
            "{capability: shell, effect: deny, match: ['del *']}",
        )
        assert not _permits(ceiling, "commands", "del x")
        assert _permits(ceiling, "commands", "DEL x")

    @_POSIX_PATHS
    def test_a_relative_file_allow_is_dropped(self):
        ceiling = _ceiling(*_grant("fs_read", "['src/**/*.ts']"))
        assert not _permits(ceiling, "filesystem.read", "/home/u/ws/src/a.ts")

    @_POSIX_PATHS
    def test_a_single_star_file_allow_is_dropped(self):
        """A policy ``*`` stays in one segment; Crew's crosses ``/``, so it would widen."""
        ceiling = _ceiling(*_grant("fs_write", "['/srv/*']"))
        assert not _permits(ceiling, "filesystem.write", "/srv/a/b")

    def test_a_negated_file_deny_refuses_the_policy(self):
        with pytest.raises(t.Unrepresentable):
            t.translate("rules:\n  - {capability: fs_read, effect: deny, match: ['!*.pem']}\n")

    def test_a_file_deny_beside_an_allowed_glob_refuses_the_policy(self):
        """One Crew ruleset holds one mode, so allow ``A/**`` minus a deny has no form."""
        with pytest.raises(t.Unrepresentable):
            t.translate(
                "rules:\n"
                "  - {capability: fs_read, effect: deny, exclude: ['/srv/**']}\n"
                "  - {capability: fs_read, effect: deny, match: ['.env']}\n"
            )

    @_POSIX_PATHS
    @pytest.mark.parametrize("pattern", ["/srv/data**", "/data/**.log"])
    def test_a_globstar_inside_a_segment_is_dropped_from_a_file_allow(self, pattern):
        """A ``**`` that is not a whole segment reads as ``*``; Crew's would cross ``/``."""
        ceiling = _ceiling(*_grant("fs_read", f"['{pattern}']"))
        assert not _permits(ceiling, "filesystem.read", "/srv/database/x/secret")
        assert not _permits(ceiling, "filesystem.read", "/data/a/b/c.log")

    @_POSIX_PATHS
    def test_a_file_deny_that_cannot_meet_the_grant_is_applied(self):
        ceiling = _ceiling(
            *_grant("fs_read", "['/srv/data/**']"),
            "{capability: fs_read, effect: deny, match: ['/etc/shadow', '~/.aws/**']}",
        )
        assert _permits(ceiling, "filesystem.read", "/srv/data/a.txt")
        assert not _permits(ceiling, "filesystem.read", "/etc/shadow")

    def test_an_mcp_deny_on_another_server_is_applied(self):
        ceiling = _ceiling(
            *_grant("mcp", "['github/*']"),
            "{capability: mcp, effect: deny, match: ['jira/delete_*']}",
        )
        assert _permits(ceiling, "mcp", "@github/read")
        assert not _permits(ceiling, "mcp", "@jira/read")

    @_POSIX_PATHS
    def test_a_globstar_deny_covers_every_empty_segment_choice(self):
        ceiling = _ceiling(
            "{capability: fs_read, effect: allow}",
            "{capability: fs_read, effect: deny, match: ['/srv/**/private/**/key']}",
        )
        for path in ("/srv/private/team/key", "/srv/a/private/key", "/srv/private/key"):
            assert not _permits(ceiling, "filesystem.read", path)
        assert _permits(ceiling, "filesystem.read", "/srv/public/key.txt")

    @_POSIX_PATHS
    @pytest.mark.parametrize("pattern", ["~/**", "~/**/.ssh/id_rsa"])
    def test_a_home_globstar_deny_still_names_the_home_directory(self, pattern):
        ceiling = _ceiling(
            "{capability: fs_write, effect: allow}",
            f"{{capability: fs_write, effect: deny, match: ['{pattern}']}}",
        )
        assert not _permits(ceiling, "filesystem.write", "~/.ssh/id_rsa")
        assert _permits(ceiling, "filesystem.write", "/srv/out.txt")

    @_POSIX_PATHS
    def test_a_brace_with_no_comma_is_literal(self):
        ceiling = _ceiling(
            "{capability: fs_read, effect: allow}",
            "{capability: fs_read, effect: deny, match: ['/srv/key{prod}.pem', '/srv/{a,b}.pem']}",
        )
        assert not _permits(ceiling, "filesystem.read", "/srv/key{prod}.pem")
        assert not _permits(ceiling, "filesystem.read", "/srv/b.pem")
        assert _permits(ceiling, "filesystem.read", "/srv/keyprod.pem")

    def test_only_bang_negates_after_case_folding(self):
        assert t._any_case("[^x]") == "[^x]"
        assert t._any_case("[!]a]b") == "[!]a][bB]"
        assert t._any_case("[a") == "[[][aA]"

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param("/srv/[]a].txt", id="bracket-leading-close"),
            pytest.param("/srv/[^report.txt", id="bracket-caret"),
            pytest.param("/srv/{[^a],b}.txt", id="bracket-in-brace"),
            pytest.param("/[[:alpha:]]", id="bracket-posix-class"),
            pytest.param("", id="empty"),
            pytest.param("C:\\data\\x", id="backslash"),
            pytest.param("D:/data", id="drive"),
            pytest.param("D:", id="bare-drive"),
            pytest.param(".env", id="relative"),
            pytest.param("secrets/**", id="relative-dir"),
            pytest.param("~someone/a", id="named-user-home"),
            pytest.param("$HOME/a", id="variable"),
            pytest.param("/srv/@(a|b)", id="extglob"),
            pytest.param("/srv/[a.txt", id="bracket"),
            pytest.param("/srv/secrets/", id="trailing-slash"),
            pytest.param("/srv//secrets/**", id="repeated-slash"),
            pytest.param("//srv/private/**", id="leading-double-slash"),
            pytest.param("/srv/{,data}/secrets/**", id="brace-rebuilds-slash"),
            pytest.param("/srv/./e", id="dot"),
            pytest.param("/srv/ws/../d", id="dot-dot"),
            pytest.param("~/../../etc/passwd", id="home-dot-dot"),
            pytest.param("!*.pem", id="negated"),
        ],
    )
    def test_a_non_canonical_file_pattern_refuses_a_deny_and_drops_an_allow(self, pattern):
        """Only a canonical absolute or home path is read; any other shape has no Crew form."""
        with pytest.raises(t.Unrepresentable):
            t._fs_patterns(pattern, t.DENY_ROLE)
        assert t._fs_patterns(pattern, t.ALLOW_ROLE) == []

    @_POSIX_PATHS
    def test_a_relative_file_deny_refuses_the_whole_policy(self):
        with pytest.raises(t.Unrepresentable):
            t.translate(
                "rules:\n"
                "  - {capability: fs_read, effect: allow}\n"
                "  - {capability: fs_read, effect: deny, match: ['.env']}\n"
            )

    @pytest.mark.parametrize("pattern", ["/", "~", "~/.ssh/**", "/srv/**/key", "/srv/{a,b}.pem"])
    def test_a_canonical_file_pattern_is_read(self, pattern):
        assert t._fs_patterns(pattern, t.DENY_ROLE)

    def test_a_brace_range_deny_refuses_the_policy(self):
        with pytest.raises(t.Unrepresentable):
            t.translate("rules:\n  - {capability: fs_read, effect: deny, match: ['/k{1..3}']}\n")

    def test_a_permission_mode_glob_deny_keeps_the_floor(self):
        ceiling = _ceiling(
            "{capability: permission_mode, effect: allow}",
            "{capability: permission_mode, effect: deny, match: ['*o*']}",
        )
        assert not _permits(ceiling, "approval_modes", "yolo")
        for mode in ("normal", "trust", "trust_reads"):
            assert _permits(ceiling, "approval_modes", mode)

    def test_a_capability_that_is_not_a_string_is_refused(self):
        with pytest.raises(t.TranslationError):
            t.translate("rules:\n  - {capability: [a], effect: deny}\n")

    @pytest.mark.parametrize(
        "term, scope, item",
        [
            ("shell", "commands", "ls"),
            ("network_egress", "network.egress", "example.com"),
            ("mcp", "mcp", "@github/read"),
            ("subagent", "capabilities.spawn", ""),
        ],
    )
    def test_a_deny_with_an_empty_match_denies_everything(self, term, scope, item):
        """An empty ``match`` names every resource, as a missing one does."""
        ceiling = _ceiling(
            f"{{capability: {term}, effect: allow}}",
            f"{{capability: {term}, effect: deny, match: []}}",
        )
        assert not _permits(ceiling, scope, item)

    def test_an_allow_with_an_empty_match_allows_everything(self):
        ceiling = _ceiling("{capability: shell, effect: allow, match: []}")
        assert _permits(ceiling, "commands", "git status")

    def test_mcp_servers_sharing_a_name_prefix_do_not_meet(self):
        ceiling = _ceiling(
            *_grant("mcp", "['github/*']"),
            "{capability: mcp, effect: deny, match: ['github-enterprise/*']}",
        )
        assert _permits(ceiling, "mcp", "@github/read")
        assert not _permits(ceiling, "mcp", "@github-enterprise/read")

    def test_an_mcp_tool_glob_deny_covers_its_whole_server(self):
        ceiling = _ceiling(
            "{capability: mcp, effect: allow}",
            "{capability: mcp, effect: deny, match: ['github/delete_*']}",
        )
        assert not _permits(ceiling, "mcp", "@github/read")
        assert _permits(ceiling, "mcp", "@jira/read")

    def test_an_mcp_tool_glob_allow_is_dropped(self):
        ceiling = _ceiling(*_grant("mcp", "['github/read_*']"))
        assert not _permits(ceiling, "mcp", "@github/read_issue")

    def test_an_mcp_server_glob_deny_refuses_the_policy(self):
        with pytest.raises(t.Unrepresentable):
            t.translate("rules:\n  - {capability: mcp, effect: deny, match: ['corp-*/x']}\n")

    def test_a_conditional_grant_grants_nothing(self):
        ceiling = _ceiling("{capability: shell, effect: allow, when: 'groups in [\"x\"]'}")
        assert not _permits(ceiling, "commands", "ls")

    def test_an_unknown_channel_id_grants_only_itself(self):
        ceiling = _ceiling(*_grant("messaging_channel", "['carrier-pigeon']"))
        assert not _permits(ceiling, "channels", "slack")


class TestMetaCapabilities:
    @_POSIX_PATHS
    def test_a_builtin_deny_reaches_shell_files_and_sub_agents(self):
        ceiling = _ceiling("{capability: builtin, effect: deny}")
        assert not _permits(ceiling, "commands", "ls")
        assert not _permits(ceiling, "filesystem.read", "/srv/a")
        assert not _permits(ceiling, "filesystem.write", "/srv/a")
        assert not _permits(ceiling, "capabilities.spawn", "")
        # ``mcp`` sits under ``all``, not ``builtin``.
        assert _permits(ceiling, "mcp", "@github/read")

    @_POSIX_PATHS
    def test_a_filesystem_deny_reaches_only_the_file_terms(self):
        ceiling = _ceiling("{capability: filesystem, effect: deny}")
        assert not _permits(ceiling, "filesystem.read", "/srv/a")
        assert _permits(ceiling, "commands", "ls")

    def test_an_allow_all_reaches_only_the_built_in_tools_and_mcp(self):
        """The stricter reading of ``all``: an allow does not reach the Crew terms."""
        ceiling = _ceiling("{capability: all, effect: allow}")
        assert _permits(ceiling, "commands", "ls")
        assert _permits(ceiling, "mcp", "@github/read")
        assert not _permits(ceiling, "approval_modes", "yolo")
        assert not _permits(ceiling, "capabilities.browse", "")
        assert not _permits(ceiling, "channels", "slack")

    def test_a_deny_all_reaches_every_managed_term(self):
        ceiling = _ceiling("{capability: all, effect: deny}")
        assert not _permits(ceiling, "capabilities.cron", "")
        assert not _permits(ceiling, "mcp", "@github/read")
        assert not _permits(ceiling, "commands", "ls")

    def test_a_scoped_meta_allow_grants_nothing(self):
        ceiling = _ceiling("{capability: all, effect: allow, match: ['x']}")
        assert not _permits(ceiling, "capabilities.browse", "")


_SWITCH_TERMS = [
    ("tailnet_origin", "capabilities.tailnet_origin", "tailnet_origin"),
    ("browser_use", "capabilities.browse", "browse"),
    ("theme_install", "capabilities.theme_install", "theme_install"),
]


@pytest.mark.parametrize("term, scope, key", _SWITCH_TERMS)
class TestSwitchTerms:
    def test_allow_turns_it_on(self, term, scope, key):
        assert t.translate(f"rules:\n  - {{capability: {term}, effect: allow}}\n")[
            "capabilities"
        ] == {key: {"enabled": True}}
        assert _permits(_ceiling(f"{{capability: {term}, effect: allow}}"), scope, "")

    def test_deny_turns_it_off(self, term, scope, key):
        ceiling = _ceiling(
            f"{{capability: {term}, effect: allow}}", f"{{capability: {term}, effect: deny}}"
        )
        assert not _permits(ceiling, scope, "")

    def test_absent_keeps_the_baseline_off(self, term, scope, key):
        assert "capabilities" not in t.translate("rules:\n  - {capability: power, effect: allow}\n")
        assert not _permits(_ceiling(), scope, "")


# --- Policy-language batch 1 (RFC D13): harness_use, app_use, script_hook, install rows ---


def _backend_ids() -> list:
    """Every registered backend's policy id (``kiro`` for the internal ``""``)."""
    from kiro_crew.acp_backends import registered_backends
    from kiro_crew.agent_backend_governance import _policy_id

    return sorted({_policy_id(b) for b in registered_backends()})


def _selectable(ceiling) -> set:
    return {b for b in _backend_ids() if _permits(ceiling, "agent_backend", b)}


class TestHarnessUse:
    def test_the_registry_holds_more_than_the_closed_list(self):
        """The closed list matters only because other backends are registered."""
        assert set(_backend_ids()) - {"kas", "kiro", "claude"}

    @pytest.mark.parametrize(
        "rule",
        [
            "{capability: harness_use, effect: allow}",
            "{capability: harness_use, effect: allow, match: ['*']}",
            "{capability: harness_use, effect: allow, match: ['c*', 'k*', 'g*', 'o*', 'd*']}",
            "{capability: harness_use, effect: deny, exclude: ['*']}",
        ],
    )
    def test_an_allow_reaches_only_the_closed_list(self, rule):
        assert _selectable(_ceiling(rule)) == {"kas", "kiro", "claude"}

    def test_a_glob_allow_takes_only_the_entries_it_matches(self):
        ceiling = _ceiling(*_grant("harness_use", "['c*']"))
        assert _selectable(ceiling) == {"kas", "claude"}

    def test_an_unknown_harness_is_never_granted(self):
        ceiling = _ceiling(*_grant("harness_use", "['codex', goose]"))
        assert _selectable(ceiling) == {"kas"}

    def test_a_deny_of_kas_has_no_effect(self):
        ceiling = _ceiling(
            "{capability: harness_use, effect: allow}",
            "{capability: harness_use, effect: deny, match: [kas, kiro]}",
        )
        assert _selectable(ceiling) == {"kas", "claude"}
        assert _selectable(_ceiling("{capability: harness_use, effect: deny}")) == {"kas"}

    def test_absent_keeps_the_baseline(self):
        assert "agent_backend" not in t.translate("rules:\n  - {capability: power, effect: deny}\n")
        assert _selectable(_ceiling()) == {"kas"}


class TestAppUse:
    def test_allow_permits_every_app_and_version(self):
        ceiling = _ceiling("{capability: app_use, effect: allow}")
        assert _permits(ceiling, "apps", "file-explorer")
        assert _permits(ceiling, "app_versions", "file-explorer@1.2.0")

    def test_a_grant_permits_its_app_at_the_matching_versions(self):
        ceiling = _ceiling(*_grant("app_use", "['file-explorer@1.*']"))
        assert _permits(ceiling, "apps", "file-explorer")
        assert _permits(ceiling, "app_versions", "file-explorer@1.4.0")
        assert not _permits(ceiling, "app_versions", "file-explorer@2.0.0")
        assert not _permits(ceiling, "apps", "notes")
        assert not _permits(ceiling, "app_versions", "notes@1.0.0")

    def test_a_version_only_deny_does_not_deny_the_app(self):
        ceiling = _ceiling(
            "{capability: app_use, effect: allow}",
            "{capability: app_use, effect: deny, match: ['file-explorer@1.0.0']}",
        )
        assert _permits(ceiling, "apps", "file-explorer")
        assert not _permits(ceiling, "app_versions", "file-explorer@1.0.0")
        assert _permits(ceiling, "app_versions", "file-explorer@1.0.1")

    @pytest.mark.parametrize("pattern", ["bad@*", "bad"])
    def test_a_deny_of_every_version_denies_the_app(self, pattern):
        ceiling = _ceiling(
            "{capability: app_use, effect: allow}",
            f"{{capability: app_use, effect: deny, match: ['{pattern}']}}",
        )
        assert not _permits(ceiling, "apps", "bad")
        assert not _permits(ceiling, "app_versions", "bad@3.0.0")
        assert _permits(ceiling, "apps", "good")

    def test_an_allow_with_no_version_grants_nothing(self):
        ceiling = _ceiling(*_grant("app_use", "['file-explorer']"))
        assert not _permits(ceiling, "app_versions", "file-explorer@1.0.0")

    def test_deny_permits_nothing(self):
        ceiling = _ceiling(
            "{capability: app_use, effect: allow}", "{capability: app_use, effect: deny}"
        )
        assert not _permits(ceiling, "apps", "file-explorer")
        assert not _permits(ceiling, "app_versions", "file-explorer@1.0.0")

    def test_absent_keeps_apps_open_and_versions_off(self):
        doc = t.translate("rules:\n  - {capability: power, effect: deny}\n")
        assert "apps" not in doc and "app_versions" not in doc
        ceiling = _ceiling()
        assert _permits(ceiling, "apps", "file-explorer")
        assert not _permits(ceiling, "app_versions", "file-explorer@1.0.0")


class TestScriptHook:
    def test_kgl_events_are_crews_hook_events(self):
        from kiro_crew.validation_parts.input_bounds import ALLOWED_HOOK_EVENTS

        # The policy language's ``hookEvent`` enum.
        kgl = {
            "AgentSpawn",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
            "SessionLaneChanged",
            "PreTaskExecution",
            "PostTaskExecution",
            "FileCreated",
            "FileEdited",
            "FileDeleted",
            "UserTriggered",
        }
        assert kgl == set(ALLOWED_HOOK_EVENTS)

    def test_allow_turns_every_event_on(self):
        ceiling = _ceiling("{capability: script_hook, effect: allow}")
        assert _permits(ceiling, "capabilities.script_hooks", "")
        assert _permits(ceiling, "capabilities.script_hooks", "events:Stop")

    def test_a_grant_permits_only_its_events(self):
        ceiling = _ceiling(*_grant("script_hook", "[PreToolUse]"))
        assert _permits(ceiling, "capabilities.script_hooks", "events:PreToolUse")
        assert not _permits(ceiling, "capabilities.script_hooks", "events:Stop")

    def test_a_deny_match_takes_only_its_event_away(self):
        ceiling = _ceiling(
            "{capability: script_hook, effect: allow}",
            "{capability: script_hook, effect: deny, match: [UserTriggered]}",
        )
        assert not _permits(ceiling, "capabilities.script_hooks", "events:UserTriggered")
        assert _permits(ceiling, "capabilities.script_hooks", "events:Stop")

    def test_deny_and_absent_keep_it_off(self):
        ceiling = _ceiling(
            "{capability: script_hook, effect: allow}", "{capability: script_hook, effect: deny}"
        )
        assert not _permits(ceiling, "capabilities.script_hooks", "")
        assert not _permits(_ceiling(), "capabilities.script_hooks", "")


_INSTALL = "capabilities.extension_install"


def _installs(ceiling, kind: str, ident: str) -> bool:
    return _permits(ceiling, _INSTALL, f"extensions:{kind}:{ident}")


class TestInstallRows:
    @pytest.mark.parametrize(
        "term, kind", [("skill", "skill"), ("mcp", "mcp"), ("subagent", "agent")]
    )
    def test_a_scope_less_allow_grants_install_of_its_kind_only(self, term, kind):
        ceiling = _ceiling(f"{{capability: {term}, effect: allow}}")
        assert _installs(ceiling, kind, "anything")
        for other in {"skill", "mcp", "agent"} - {kind}:
            assert not _installs(ceiling, other, "anything")
        assert not _installs(ceiling, "plugin", "anything")

    def test_absent_keeps_install_off(self):
        doc = t.translate("rules:\n  - {capability: power, effect: deny}\n")
        assert "extension_install" not in doc.get("capabilities", {})
        assert not _permits(_ceiling(), _INSTALL, "")

    def test_the_mcp_use_row_is_unchanged(self):
        doc = t.translate("rules:\n" + "".join(f"  - {r}\n" for r in _grant("mcp", "['github/*']")))
        assert doc["mcp"] == {"mode": "allow", "allow": ["@github"]}
        assert doc["capabilities"]["extension_install"]["scopes"]["extensions"] == {
            "mode": "allow",
            "allow": ["mcp:github"],
        }

    @pytest.mark.parametrize(
        "pattern, server, granted",
        [
            ("github/*", "github", True),
            ("github", "github", True),
            ("io.github.owner/name/*", "io.github.owner/name", True),
            # One tool does not grant its server; a slashed id with no ``/*`` may be one tool.
            ("github/read", "github", False),
            ("io.github.owner/name", "io.github.owner/name", False),
            ("corp-*/*", "corp-x", False),
        ],
    )
    def test_an_mcp_grant_installs_a_whole_server(self, pattern, server, granted):
        ceiling = _ceiling(*_grant("mcp", f"['{pattern}']"))
        assert _installs(ceiling, "mcp", server) is granted
        assert not _installs(ceiling, "mcp", "jira")

    @pytest.mark.parametrize(
        "pattern, denied, kept",
        [
            ("github/*", ["github", "github/sub"], ["jira"]),
            ("io.github.owner/name", ["io.github.owner/name"], ["io.github.owner/other"]),
            # A one-tool deny is left to the use row; the server still installs.
            ("github/delete_*", [], ["github"]),
        ],
    )
    def test_an_mcp_deny_beside_an_allow_denies_only_whole_servers(self, pattern, denied, kept):
        ceiling = _ceiling(
            "{capability: mcp, effect: allow}",
            f"{{capability: mcp, effect: deny, match: ['{pattern}']}}",
        )
        for server in denied:
            assert not _installs(ceiling, "mcp", server)
        for server in kept:
            assert _installs(ceiling, "mcp", server)
        assert not _installs(ceiling, "plugin", "x")
        assert not _installs(ceiling, "skill", "x")

    def test_a_deny_list_beside_other_open_kinds_keeps_them_open(self):
        ceiling = _ceiling(
            "{capability: mcp, effect: allow}",
            "{capability: mcp, effect: deny, match: ['github/*']}",
            "{capability: skill, effect: allow}",
        )
        assert not _installs(ceiling, "mcp", "github")
        assert _installs(ceiling, "mcp", "jira")
        assert _installs(ceiling, "skill", "owner/repo")
        assert not _installs(ceiling, "agent", "x")
        assert not _installs(ceiling, "plugin", "x")

    def test_a_deny_list_beside_another_kinds_list_turns_it_off(self):
        """No one-mode form: the deny-list kind's install is off, which only narrows."""
        ceiling = _ceiling(
            "{capability: mcp, effect: allow}",
            "{capability: mcp, effect: deny, match: ['github/*']}",
            *_grant("skill", "['pdf']"),
        )
        assert _installs(ceiling, "skill", "pdf")
        assert not _installs(ceiling, "mcp", "jira")
        assert not _installs(ceiling, "plugin", "x")

    def test_a_named_agent_deny_turns_agent_install_off(self):
        """The install id is a package, so a denied agent name cannot be matched yet."""
        ceiling = _ceiling(
            "{capability: all, effect: allow}",
            "{capability: subagent, effect: deny, match: [evil]}",
        )
        assert not _installs(ceiling, "agent", "owner/evil")
        assert not _installs(ceiling, "agent", "owner/good")
        assert _installs(ceiling, "mcp", "github")

    def test_a_named_skill_deny_lands_as_the_skill_name(self):
        """The install seam checks each skill a package holds, so the name is kept."""
        doc = t.translate(
            "rules:\n"
            "  - {capability: all, effect: allow}\n"
            "  - {capability: skill, effect: deny, match: [evil]}\n"
        )
        extensions = doc["capabilities"]["extension_install"]["scopes"]["extensions"]
        assert extensions["mode"] == "deny" and "skill:evil" in extensions["deny"]
        ceiling = _ceiling(
            "{capability: all, effect: allow}",
            "{capability: skill, effect: deny, match: [evil]}",
        )
        assert not _installs(ceiling, "skill", "evil")
        assert _installs(ceiling, "skill", "good")
        assert _installs(ceiling, "mcp", "github")
        assert not _installs(ceiling, "plugin", "x")

    def test_a_named_skill_deny_inside_an_allowed_glob_does_not_refuse_the_policy(self):
        ceiling = _ceiling(
            *_grant("skill", "['pdf-*']"),
            "{capability: skill, effect: deny, match: ['pdf-evil']}",
            "{capability: shell, effect: allow}",
        )
        assert not _installs(ceiling, "skill", "pdf-good")
        assert _permits(ceiling, "commands", "ls")
        with pytest.raises(t.TranslationError):
            t.translate(
                "rules:\n"
                "  - {capability: skill, effect: deny, match: [evil]}\n"
                "  - {capability: skill, effect: maybe}\n"
            )

    def test_a_subagent_grant_keeps_the_spawn_row(self):
        doc = t.translate(
            "rules:\n" + "".join(f"  - {r}\n" for r in _grant("subagent", "[reviewer]"))
        )
        assert doc["capabilities"]["spawn"] == {
            "enabled": True,
            "scopes": {"agents": {"mode": "allow", "allow": ["reviewer"]}},
        }
        ceiling = _ceiling(*_grant("subagent", "[reviewer]"))
        assert _installs(ceiling, "agent", "reviewer")
        assert not _installs(ceiling, "agent", "other")


class TestBatchOneMeta:
    def test_a_deny_all_turns_every_new_row_off(self):
        ceiling = _ceiling("{capability: all, effect: deny}")
        assert _selectable(ceiling) == {"kas"}
        assert not _permits(ceiling, "apps", "file-explorer")
        assert not _permits(ceiling, "app_versions", "file-explorer@1.0.0")
        assert not _permits(ceiling, "capabilities.script_hooks", "")
        assert not _permits(ceiling, _INSTALL, "")
        for kind in ("skill", "mcp", "agent", "plugin"):
            assert not _installs(ceiling, kind, "x")

    def test_an_allow_all_opens_install_but_not_plugins_or_the_crew_terms(self):
        """RFC D13: a scope-less ``allow all`` grants install."""
        ceiling = _ceiling("{capability: all, effect: allow}")
        for kind in ("skill", "mcp", "agent"):
            assert _installs(ceiling, kind, "x")
        assert not _installs(ceiling, "plugin", "x")
        assert _selectable(ceiling) == {"kas"}
        assert not _permits(ceiling, "app_versions", "file-explorer@1.0.0")
        assert not _permits(ceiling, "capabilities.script_hooks", "")

    def test_an_allow_builtin_opens_skill_and_agent_install_only(self):
        ceiling = _ceiling("{capability: builtin, effect: allow}")
        assert _installs(ceiling, "skill", "x")
        assert _installs(ceiling, "agent", "x")
        assert not _installs(ceiling, "mcp", "x")
        assert not _installs(ceiling, "plugin", "x")

    def test_a_scoped_meta_allow_grants_no_install(self):
        assert not _permits(
            _ceiling("{capability: all, effect: allow, match: ['x']}"), _INSTALL, ""
        )


def _nt_expand(path: str) -> str:
    return ntpath.expanduser(ntpath.expandvars(path))


def _nt_norm_item(path: str) -> str:
    """``_norm_item`` as a Windows gateway running from ``C:\\gateway`` computes it."""
    return ntpath.normpath(ntpath.join("C:\\gateway", _nt_expand(path)))


class TestDrivelessPathsOnWindows:
    """A ``/``-rooted ``fs_*`` rule is kept on a Windows gateway.

    It names remote-workspace (guest) files, which are POSIX paths on every gateway
    OS, and it meets no local drive-letter path. Translation therefore neither
    refuses nor drops it. Runs on every host: the Windows reading is ``ntpath``.
    """

    @pytest.fixture(autouse=True)
    def _windows_reading(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(governance, "_norm_item", _nt_norm_item)
        _floor_monkeypatch.setattr(governance, "_expand", _nt_expand)
        _floor_monkeypatch.setattr(governance, "_WINDOWS_PATHS", True)

    @pytest.mark.parametrize(
        "capability, scope", [("fs_read", "filesystem.read"), ("fs_write", "filesystem.write")]
    )
    def test_a_driveless_deny_binds_guest_paths_and_no_local_path(self, capability, scope):
        ceiling = _ceiling(
            f"{{capability: {capability}, effect: allow}}",
            f"{{capability: {capability}, effect: deny, match: ['/srv/data/**']}}",
        )
        assert not _permits(ceiling, scope, GuestPath("/srv/data/x", _GUEST_ROOT))
        assert not _permits(ceiling, scope, GuestPath("../../srv/data/x", _GUEST_ROOT))
        assert _permits(ceiling, scope, GuestPath("/srv/other/x", _GUEST_ROOT))
        # ``/srv/data/x`` is denied under a POSIX reading, so this row also proves the
        # Windows reading is live: there the item gains a drive.
        for local in ("/srv/data/x", "C:\\srv\\data\\x", "C:/srv/data/x", "c:\\SRV\\DATA\\x"):
            assert _permits(ceiling, scope, local), local

    def test_a_driveless_allow_confines_guest_paths_and_admits_no_local_path(self):
        ceiling = _ceiling(*_grant("fs_write", "['/workspace/**']"))
        assert _permits(ceiling, "filesystem.write", GuestPath("x", _GUEST_ROOT))
        assert not _permits(ceiling, "filesystem.write", GuestPath("/etc/x", _GUEST_ROOT))
        assert not _permits(ceiling, "filesystem.write", "C:\\workspace\\app\\x")
