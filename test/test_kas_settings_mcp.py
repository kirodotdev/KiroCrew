"""kiro-cli's mcp.json / cli.json settings carried onto the KAS agent block.

Covers ``useLegacyMcpJson`` (P1-17), glob ``disabledTools`` on the mcp.json
servers KAS starts (P0-14), the undeclared ones a not-opted-in agent does not
start (P0-13), the 120 s default (300 s for a remote server) and the
``mcp.initTimeout`` floor (P0-21, P1-37).
"""

from __future__ import annotations

import json
import logging

import pytest
from kas_agents_helpers import _spec

from kiro_crew import agent as agent_mod
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_agents import KasAgentTranslationError, to_client_custom_agent
from kiro_crew.acp.kas_permissions import CREW_SETTINGS_MCP_KEY
from kiro_crew.acp.kas_settings_mcp import (
    finish_settings_mcp,
    include_mcp_json,
    stamp_settings_mcp,
    without_excluded_stubs,
)
from kiro_crew.acp.kas_tool_limits import (
    kas_globs,
    unmatched_disabled_globs,
    unmatched_globs_notice,
    with_switched_off_tools,
)
from kiro_crew.agent_materialization.auto_approve import govern_spec


@pytest.fixture
def home(tmp_path, monkeypatch):
    settings = tmp_path / "home" / ".kiro" / "settings"
    settings.mkdir(parents=True)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings / "mcp.json")
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    return settings


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _agent(**over):
    base = {"id": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
    base.update(over)
    return base


def _finish(agent, work, session=(), declared=(), opted_in=False):
    (stamped,) = stamp_settings_mcp([agent], "a", work, declared=declared, opted_in=opted_in)
    return finish_settings_mcp(stamped)


class TestLegacySpelling:
    def test_use_legacy_mcp_json_travels_as_include_mcp_json(self):
        spec = _spec(useLegacyMcpJson=False)
        del spec["includeMcpJson"]
        assert to_client_custom_agent("a", spec, "p")["includeMcpJson"] is False

    def test_a_true_opt_in_under_the_alias_is_read_but_never_sent(self):
        """KAS's ``true`` mounts every MCP tool past ``tools``, whichever key said it."""
        spec = _spec(useLegacyMcpJson=True)
        del spec["includeMcpJson"]
        assert include_mcp_json(spec) is True
        assert "includeMcpJson" not in to_client_custom_agent("a", spec, "p")

    def test_include_mcp_json_wins_over_the_alias(self):
        out = to_client_custom_agent("a", _spec(includeMcpJson=False, useLegacyMcpJson=True), "p")
        assert out["includeMcpJson"] is False


class TestUndeclaredFileServers:
    @pytest.mark.parametrize("opt_in", [{}, {"includeMcpJson": False}])
    def test_undeclared_servers_are_excluded_whole(self, home, work, opt_in):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["w*"]}, "own": {}}})
        _write(work / ".kiro" / "settings" / "mcp.json", {"mcpServers": {"proj": {"command": "p"}}})
        out = _finish(_agent(**opt_in), work, [{"name": "stubbed"}])
        assert out["excludedTools"] == ["@extra", "@proj"]

    def test_spec_declared_and_crew_and_mandatory_names_are_kept(self, home, work, monkeypatch):
        monkeypatch.setenv("ASBX_KIRO_MANDATORY_MCPS", "must")
        _write(
            home / "mcp.json",
            {"mcpServers": {"hoisted": {}, "kirocrew-core": {}, "must": {"disabledTools": ["x*"]}}},
        )
        out = _finish(_agent(), work, [{"name": "hoisted"}], declared=["hoisted"])
        assert out["excludedTools"] == ["@must/x*"]

    def test_a_pooled_stub_is_not_a_declaration(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"pooled": {}}})
        out = _finish(_agent(), work, [{"name": "pooled"}])
        assert out["excludedTools"] == ["@pooled"]

    def test_opted_in_agent_keeps_them_and_gets_their_glob_disabled_tools(self, home, work):
        _write(
            home / "mcp.json",
            {"mcpServers": {"extra": {"disabledTools": ["write_*", "exact", "de?"]}}},
        )
        out = _finish(_agent(), work, opted_in=True)
        assert out["excludedTools"] == ["@extra/write_*", "@extra/de?"]

    def test_a_wire_flag_is_not_the_opt_in(self, home, work):
        """The opt-in is the stamp's: the projected entry never carries ``true``."""
        _write(home / "mcp.json", {"mcpServers": {"extra": {}}})
        assert _finish(_agent(includeMcpJson=True), work)["excludedTools"] == ["@extra"]

    def test_an_opted_in_spec_keeps_the_server_its_tools_names(self, home, work):
        """Leaving ``true`` off the wire does not cost the agent its ``tools`` refs."""
        _write(home / "mcp.json", {"mcpServers": {"notes-mcp": {}, "extra": {}}})
        spec = _spec(includeMcpJson=True, tools=["fs_read", "@notes-mcp"])
        entry = {**to_client_custom_agent("a", spec, "p"), "id": "a"}
        assert "includeMcpJson" not in entry
        out = _finish(entry, work, opted_in=include_mcp_json(spec) is True)
        assert "@notes-mcp" not in out.get("excludedTools", [])

    def test_declared_and_session_level_servers_get_no_file_globs(self, home, work):
        _write(
            home / "mcp.json",
            {
                "mcpServers": {
                    "own": {"disabledTools": ["a*"]},
                    "stubbed": {"disabledTools": ["b*"]},
                }
            },
        )
        out = _finish(_agent(), work, [{"name": "stubbed"}], declared=["stubbed"])
        assert "excludedTools" not in out

    def test_a_legacy_agent_keeps_them_and_its_pooled_stubs(self, home, work):
        _write(
            home / "mcp.json", {"mcpServers": {"pooled": {}, "extra": {"disabledTools": ["w*"]}}}
        )
        params = {"cwd": str(work), "mcpServers": [{"name": "pooled"}]}
        stamped = stamp_settings_mcp([_agent()], "a", work, legacy_mcp_json=True)
        assert without_excluded_stubs(params["mcpServers"], stamped) == [{"name": "pooled"}]
        attach_kas_custom_agents(params, stamped)
        assert params["_meta"]["kiro"]["customAgents"][0]["excludedTools"] == ["@extra/w*"]

    def test_a_pooled_stub_keeps_its_workspace_glob_disabled_tools(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"pooled": {}}})
        _write(
            work / ".kiro" / "settings" / "mcp.json",
            {"mcpServers": {"pooled": {"disabledTools": ["write_*"]}}},
        )
        out = _finish(_agent(), work, [{"name": "pooled"}], opted_in=True)
        assert out["excludedTools"] == ["@pooled/write_*"]

    def test_an_unreadable_workspace_file_keeps_the_global_exclusions(
        self, home, work, monkeypatch
    ):
        from kiro_crew.acp import session_mcp

        _write(home / "mcp.json", {"mcpServers": {"pooled": {}}})
        real = session_mcp.native_settings_sources

        def workspace_unreadable(work_dir):
            if work_dir:
                raise session_mcp.NativeSettingsUnreadable("project", work_dir, "unreadable")
            return real(work_dir)

        monkeypatch.setattr(session_mcp, "native_settings_sources", workspace_unreadable)
        stamped = stamp_settings_mcp([_agent()], "a", work)
        assert without_excluded_stubs([{"name": "pooled"}], stamped) == []
        assert finish_settings_mcp(stamped[0])["excludedTools"] == ["@pooled"]

    @pytest.mark.parametrize("mark", [b"\xff\xfe", b""], ids=["marked", "unmarked"])
    def test_each_unreadable_file_gets_one_notice_line(self, home, work, mark):
        utf16 = mark + json.dumps({"mcpServers": {"x": {}}}).encode("utf-16-le")
        (home / "mcp.json").write_bytes(utf16)
        (work / ".kiro" / "settings").mkdir(parents=True)
        (work / ".kiro" / "settings" / "mcp.json").write_bytes(utf16)
        notices: list[str] = []
        stamp_settings_mcp([_agent()], "a", work, notices=notices)
        assert [line.split(" (")[0] for line in notices] == [
            "⚠ Kiro Crew could not read the project's MCP settings",
            "⚠ Kiro Crew could not read the global MCP settings",
        ]
        assert all("saved as UTF-16; re-save it as UTF-8" in line for line in notices)

    def test_existing_exclusions_are_kept(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {}}})
        out = _finish(_agent(excludedTools=["fs_write"]), work)
        assert out["excludedTools"] == ["fs_write", "@extra"]

    def test_workspace_entry_overrides_the_global_one(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["a*"]}}})
        _write(
            work / ".kiro" / "settings" / "mcp.json",
            {"mcpServers": {"extra": {"disabledTools": ["b*"]}}},
        )
        out = _finish(_agent(), work, opted_in=True)
        assert out["excludedTools"] == ["@extra/b*"]

    def test_only_the_active_entry_is_stamped_and_an_unstamped_one_passes(self, home, work):
        other = _agent(id="other")
        stamped = stamp_settings_mcp([other, _agent()], "a", work)
        assert stamped[0] is other
        assert CREW_SETTINGS_MCP_KEY in stamped[1]
        assert finish_settings_mcp(other) is other

    def test_the_wire_envelope_applies_the_stamp_and_strips_it(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["w*"]}}})
        params = {"cwd": str(work), "mcpServers": [{"name": "stubbed"}]}
        attach_kas_custom_agents(params, stamp_settings_mcp([_agent()], "a", work))
        (sent,) = params["_meta"]["kiro"]["customAgents"]
        assert CREW_SETTINGS_MCP_KEY not in sent
        assert sent["excludedTools"] == ["@extra"]
        assert sent["mcpServers"]["own"]["timeout"] == 120_000

    def test_the_pooled_stub_of_an_excluded_server_is_dropped(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"pooled": {}, "hoisted": {}}})
        session = [{"name": "pooled"}, {"name": "hoisted"}, {"name": "kirocrew-core"}]
        stamped = stamp_settings_mcp([_agent()], "a", work, declared=["own", "hoisted"])
        kept = without_excluded_stubs(session, stamped)
        assert [e["name"] for e in kept] == ["hoisted", "kirocrew-core"]

    def test_a_dropped_stub_name_cannot_forge_a_log_line(self, home, work, caplog):
        forged = "x\nERROR forged"
        _write(home / "mcp.json", {"mcpServers": {forged: {}}})
        stamped = stamp_settings_mcp([_agent()], "a", work)
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_settings_mcp"):
            assert without_excluded_stubs([{"name": forged}], stamped) == []
        lines = [r.getMessage() for r in caplog.records if "pooled stub" in r.getMessage()]
        assert lines and all("\n" not in line for line in lines)

    def test_an_opted_in_agent_keeps_its_pooled_stubs(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"pooled": {}}})
        session = [{"name": "pooled"}]
        stamped = stamp_settings_mcp([_agent()], "a", work, opted_in=True)
        assert without_excluded_stubs(session, stamped) is session


class TestIncludeMcpJsonDefault:
    """An absent ``includeMcpJson`` reads as ``false`` on the KAS projection.

    Crew's own readers (``agent_capabilities``, ``client_mcp_array``,
    ``fork_refresh``, ``auto_approve``) read absent as ``true``; they agree with
    the projection because every spec Crew writes goes through ``govern_spec``,
    which pins the key. This pins both halves.
    """

    def test_absent_is_not_forwarded_and_is_read_as_not_opted_in(self, home, work):
        spec = _spec()
        del spec["includeMcpJson"]
        assert "includeMcpJson" not in to_client_custom_agent("a", spec, "p")
        _write(home / "mcp.json", {"mcpServers": {"extra": {}}})
        assert _finish(_agent(), work)["excludedTools"] == ["@extra"]

    def test_crews_writer_pins_false_so_the_readers_default_never_applies(self, home, work):
        config = {"name": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
        govern_spec(config, agent_filename="a.json", source="test")
        assert config["includeMcpJson"] is False
        projected = to_client_custom_agent("a", config, "p")
        assert projected["includeMcpJson"] is False


class TestTimeout:
    def test_unset_timeout_gets_kiro_cli_default(self, home, work):
        out = _finish(_agent(mcpServers={"s": {"command": "x"}, "r": {"url": "https://h"}}), work)
        assert out["mcpServers"]["s"]["timeout"] == 120_000
        assert out["mcpServers"]["r"]["timeout"] == 300_000

    def test_only_a_server_kas_reaches_by_url_gets_the_sign_in_wait(self, home, work):
        # A remote entry Crew relays through a local launcher is a stdio server to
        # KAS, and a longer ``mcp.initTimeout`` still raises the remote floor.
        servers = {"r": {"url": "https://h"}, "bridged": {"url": "https://h", "command": "x"}}
        out = _finish(_agent(mcpServers=servers), work)["mcpServers"]
        assert out["bridged"]["timeout"] == 120_000
        _write(home / "cli.json", {"mcp.initTimeout": 450_000})
        out = _finish(_agent(mcpServers=servers), work)["mcpServers"]
        assert out["r"]["timeout"] == 450_000

    def test_set_timeout_is_kept_and_alias_is_mapped(self, home, work):
        servers = {
            "s": {"command": "x", "timeout": 5000},
            "t": {"command": "y", "timeoutMs": 90000},
        }
        out = _finish(_agent(mcpServers=servers), work)
        assert out["mcpServers"]["s"] == {"command": "x", "timeout": 5000}
        assert out["mcpServers"]["t"] == {"command": "y", "timeout": 90000}

    @pytest.mark.parametrize(
        ("init", "want"), [(300_000, 300_000), (900_000, 600_000), (10, 120_000)]
    )
    def test_init_timeout_raises_the_default(self, home, work, init, want):
        _write(home / "cli.json", {"mcp.initTimeout": init})
        out = _finish(_agent(), work)
        assert out["mcpServers"]["own"]["timeout"] == want

    def test_workspace_cli_json_overrides_global(self, home, work):
        _write(home / "cli.json", {"mcp.initTimeout": 300_000})
        _write(work / ".kiro" / "settings" / "cli.json", {"mcp.initTimeout": 200_000})
        assert _finish(_agent(), work)["mcpServers"]["own"]["timeout"] == 200_000


class TestBraceGlobs:
    @pytest.mark.parametrize(
        ("pattern", "want"),
        [
            ("write_file", ["write_file"]),
            ("x_{a,b}", ["x_{a,b}"]),  # no wildcard: exact on kiro-cli and KAS alike
            ("{read,write}_*", ["read_*", "write_*"]),
            ("{a,b}?{c,d}*", ["a?c*", "a?d*", "b?c*", "b?d*"]),
            ("{a*,b}", ["a*", "b"]),
            ("{x,x}*", ["x*"]),
        ],
    )
    def test_braces_expand_into_one_glob_per_branch(self, pattern, want):
        assert kas_globs(pattern) == want

    @pytest.mark.parametrize(
        "pattern",
        ["fs_[rw]*", "a]*", r"a\**", "**", "a**b", "{a,{b,c}}*", "{a,}*", "{}*", "{a*", "a}*"]
        + ["{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}*"]
        + ["{a,b}" * 6 + "{" + ",".join(f"x{i}" for i in range(150_000)) + "}*"]
        + ["{a}" * 100_000 + "*", "a" * 513 + "*"]
        + ["{a,b}" * 6 + "{c,d,e}*"],  # 192 branches, refused before it is built
        ids=lambda pattern: pattern[:40],
    )
    def test_what_kas_cannot_match_is_none(self, pattern):
        assert kas_globs(pattern) is None

    def test_a_spec_server_brace_glob_is_carried_per_branch(self):
        spec = _spec(mcpServers={"s": {"command": "x", "disabledTools": ["{read,write}_*"]}})
        out = to_client_custom_agent("a", spec, "p")
        assert "@s/read_*" in out["excludedTools"]
        assert "@s/write_*" in out["excludedTools"]

    def test_a_spec_server_glob_kas_cannot_match_keeps_the_agent_and_is_named(self, caplog):
        servers = {"s": {"command": "x", "disabledTools": ["fs_[rw]*", "{a,b}_*"]}}
        out = to_client_custom_agent("a", _spec(mcpServers=servers), "p")
        assert {"@s/fs_[rw]*", "@s/a_*", "@s/b_*"} <= set(out["excludedTools"])
        with caplog.at_level(logging.WARNING):
            notice = unmatched_globs_notice(unmatched_disabled_globs(servers))
        assert notice == (
            "⚠ disabledTools pattern `fs_[rw]*` for `s` can't be applied on KAS,"
            " so that tool stays on."
        )
        assert "fs_[rw]*" in caplog.text

    def test_a_non_list_disabled_tools_still_refuses_the_agent(self):
        spec = _spec(mcpServers={"s": {"command": "x", "disabledTools": "fs_write"}})
        with pytest.raises(KasAgentTranslationError, match="list of tool names"):
            to_client_custom_agent("a", spec, "p")

    def test_the_notice_cannot_be_forged_and_names_every_server(self, caplog):
        servers = {"x\nERROR forged`": {"disabledTools": [f"p{i}[a]*" for i in range(9)]}}
        servers["last"] = {"disabledTools": ["q[a]*"]}
        with caplog.at_level(logging.WARNING):
            notice = unmatched_globs_notice(unmatched_disabled_globs(servers))
        lines = notice.split("\n")
        assert len(lines) == 9
        assert lines[-1] == "…and 2 more such patterns, for `x ERROR forged'`, `last`."
        assert all("`x ERROR forged'`" in line for line in lines[:8])
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 10 and all("\n" not in w for w in warnings)

    def test_a_global_switch_brace_glob_is_carried_per_branch(self):
        pairs = frozenset({("own", "fs_{read,write}*"), ("own", "x_[ab]*")})
        (out,) = with_switched_off_tools([{"id": "a"}], pairs)
        assert out["excludedTools"] == ["@own/fs_read*", "@own/fs_write*", "@own/x_[ab]*"]

    def test_a_file_server_brace_glob_is_carried_per_branch(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["{a,b}_*"]}}})
        out = _finish(_agent(), work, opted_in=True)
        assert out["excludedTools"] == ["@extra/a_*", "@extra/b_*"]

    def test_a_file_server_glob_kas_cannot_match_keeps_it_running_with_a_notice(
        self, home, work, caplog
    ):
        _write(
            home / "mcp.json",
            {"mcpServers": {"extra": {"disabledTools": ["ok_*", "fs_[rw]*"]}, "fine": {}}},
        )
        unmatched: set[tuple[str, str]] = set()
        (stamped,) = stamp_settings_mcp([_agent()], "a", work, unmatched=unmatched, opted_in=True)
        out = finish_settings_mcp(stamped)
        assert "@extra" not in out["excludedTools"]
        assert out["excludedTools"] == ["@extra/ok_*", "@extra/fs_[rw]*"]
        assert unmatched == {("extra", "fs_[rw]*")}
        with caplog.at_level(logging.WARNING):
            notice = unmatched_globs_notice(sorted(unmatched))
        assert notice == (
            "⚠ disabledTools pattern `fs_[rw]*` for `extra` can't be applied on KAS,"
            " so that tool stays on."
        )
        assert "fs_[rw]*" in caplog.text and "extra" in caplog.text

    def test_nothing_reported_for_a_server_kas_does_not_start(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["fs_[rw]*"]}}})
        unmatched: set[tuple[str, str]] = set()
        stamp_settings_mcp([_agent()], "a", work, unmatched=unmatched)
        assert unmatched == set()
