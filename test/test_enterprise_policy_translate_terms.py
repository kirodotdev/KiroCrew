"""The existing policy-language terms Kiro Crew reads (RFC crew-lts-edition).

Each term is translated and then asked through the real parser and evaluator in an
LTS build, so a row is pinned by the decision its Crew read sites get: allow,
deny, a narrowed match, a value with no Crew form, and no rule (the baseline).
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew.platform import enterprise_policy_translate as t
from kiro_crew.platform import governance


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
