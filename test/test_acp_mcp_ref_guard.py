"""The unresolved-``@server``-ref guard (:mod:`kiro_crew.acp.mcp_ref_guard`).

The defect this guards against has shipped on three harnesses: a session comes up
holding ``tools: ["@kirocrew-core", ...]`` while nothing in its effective
``mcpServers`` defines ``kirocrew-core``, so every Crew tool is silently absent
with the harness otherwise working. These tests pin the two backend semantics
(kiro-cli reads the spec itself, everyone else gets only the wire array), the ref
spellings that are NOT server refs, and that the composition path in
``acp/client.py`` actually reaches the guard on both ``session/new`` and
``session/load``.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import mcp_ref_guard, session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_ref_guard import (
    parse_tools_refs,
    unresolved_server_refs,
    warn_unresolved_server_refs,
)
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


def _wire(*names: str) -> list[dict[str, Any]]:
    """A ``session/new`` ``mcpServers`` array carrying exactly *names*."""
    return [{"name": n, "command": "/bin/x", "args": [], "env": [], "type": "stdio"} for n in names]


class TestRefParsing:
    """The one reader of the ``tools`` ref vocabulary, shared with session_mcp."""

    def test_both_ref_forms_name_the_same_server(self):
        # A whole-server ref and a per-tool ref both require the server to exist.
        assert parse_tools_refs(["@srv", "@other/tool"]) == (False, ["srv", "other"])

    def test_a_bare_tool_name_is_not_a_server_ref(self):
        assert parse_tools_refs(["fs_read", "execute_bash", "tool_search"]) == (False, [])

    def test_the_bare_star_is_grant_all_and_names_no_server(self):
        assert parse_tools_refs(["*"]) == (True, [])

    def test_at_star_is_a_server_literally_named_star(self):
        # Matching connections.tool_aliases and kas_permissions: reading `@*` as
        # grant-all here would mount every declared server on a backend where
        # kiro-cli mounted none.
        assert parse_tools_refs(["@*"]) == (False, ["*"])

    @pytest.mark.parametrize("ref", ["@", "@/tool", ""])
    def test_a_ref_naming_no_server_is_skipped(self, ref):
        assert parse_tools_refs([ref]) == (False, [])

    def test_duplicates_collapse_in_first_seen_order(self):
        assert parse_tools_refs(["@b", "@a/x", "@b/y", "@a"]) == (False, ["b", "a"])

    @pytest.mark.parametrize("tools", [None, "@srv", 7, {"@srv": True}])
    def test_a_non_list_tools_does_not_raise(self, tools):
        # The spec is hand-editable JSON, so this is ordinary input, not an error.
        assert parse_tools_refs(tools) == (False, [])

    def test_non_string_entries_are_ignored(self):
        assert parse_tools_refs([None, 3, ["@srv"], "@real"]) == (False, ["real"])

    def test_session_mcp_mounts_through_this_parser(self):
        """The mounting decision and the guard read one vocabulary.

        A guard that read ``@srv`` where the projection read nothing would report a
        ref as unresolved while the server mounted; the reverse would mount a
        server the guard called absent. Both directions are the same defect, so the
        two must not have separate parsers.
        """
        assert session_mcp._tools_grant(["@srv/tool"], "srv") is True
        assert session_mcp._tools_grant(["*"], "anything") is True
        assert session_mcp._tools_grant(["@*"], "srv") is False
        assert session_mcp._tools_grant(["fs_read"], "srv") is False


class TestResolution:
    def test_a_wire_served_ref_resolves(self):
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert (
            unresolved_server_refs(spec, _wire("kirocrew-core"), backend=ACP_BACKEND_CLAUDE) == []
        )

    def test_codex_today_reports_every_ref(self):
        """The live state of a plain public build, which is why the guard exists.

        ``_codex_session_mcp_servers`` returns ``[]``, so with the shared gateway
        off a codex session receives nothing at all -- while its spec declares and
        references Crew's whole control plane.
        """
        spec = {
            "tools": ["@kirocrew-core", "@kirocrew-cron", "fs_read"],
            "mcpServers": {"kirocrew-core": _CORE, "kirocrew-cron": _CRON},
        }
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CODEX) == [
            "@kirocrew-core",
            "@kirocrew-cron",
        ]

    def test_kiro_resolves_its_refs_against_the_spec_not_the_wire(self):
        """kiro-cli is handed ``--agent`` and loads the spec itself.

        Crew passes it an EMPTY array by design, so judging its refs against the
        wire would report every ref on the healthiest install there is -- the
        guard's own false-positive failure mode, and the one that would get it
        deleted rather than fixed.
        """
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_KIRO) == []
        # ...and the same spec on a backend that reads no agent file does report it.
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CLAUDE) == ["@kirocrew-core"]

    def test_kiro_still_reports_a_ref_the_spec_never_defines(self):
        # The spec being the satisfier does not make every ref satisfied: a typo'd
        # or removed server name names nothing on kiro-cli either.
        spec = {"tools": ["@typo-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_KIRO) == ["@typo-core"]

    def test_a_broker_stub_satisfies_a_ref(self):
        """A pooled server arrives on the wire under the name it wraps.

        The projection yields the raw entry to its stub (two elements with one name
        would either shadow the broker or start it twice), so the stub is the ONLY
        thing carrying that name -- and it must count.
        """
        spec = {"tools": ["@pooled"], "mcpServers": {"pooled": {"command": "/bin/raw"}}}
        assert unresolved_server_refs(spec, _wire("pooled"), backend=ACP_BACKEND_CLAUDE) == []

    def test_a_spec_server_the_projection_dropped_is_reported(self):
        """Declared, referenced, and still absent from the session.

        A registry-marked entry, or one with neither ``command`` nor ``url``, is
        dropped by the translation -- so the spec's own ``mcpServers`` proves
        nothing about what the session receives on a backend that reads no spec.
        """
        spec = {
            "tools": ["@marked", "@ok"],
            "mcpServers": {
                "marked": {"type": "registry", "command": "/bin/placeholder"},
                "ok": {"command": "/bin/ok"},
            },
        }
        assert unresolved_server_refs(spec, _wire("ok"), backend=ACP_BACKEND_CLAUDE) == ["@marked"]

    def test_builtin_namespace_is_never_reported(self):
        """``@builtin`` addresses kiro's built-in tools, not a server.

        kiro's own configuration reference documents it beside ``@server``, so a
        spec written to that reference is correct -- reporting it would put a
        permanent false warning on every such spec.
        """
        spec = {"tools": ["@builtin", "fs_read"], "mcpServers": {}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CLAUDE) == []

    def test_a_server_actually_called_builtin_is_still_mountable(self):
        # The exclusion belongs to the guard, not to the parser: session_mcp must
        # still mount a server whose real name is `builtin`.
        assert session_mcp._tools_grant(["@builtin"], "builtin") is True

    def test_grant_all_does_not_satisfy_a_ref_naming_nothing(self):
        # `*` grants every DEFINED server; it defines none, so a ref beside it to
        # something undefined still names nothing.
        spec = {"tools": ["*", "@ghost"], "mcpServers": {}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CLAUDE) == ["@ghost"]

    def test_a_spec_with_no_refs_is_silent(self):
        assert unresolved_server_refs({"tools": ["fs_read"]}, [], backend=ACP_BACKEND_CODEX) == []

    def test_disabled_tools_never_make_a_ref_unresolved(self):
        # It narrows what a MOUNTED server delivers; the server is still there.
        spec = {
            "tools": ["@srv"],
            "mcpServers": {"srv": {"command": "/bin/srv", "disabledTools": ["dangerous"]}},
        }
        assert unresolved_server_refs(spec, _wire("srv"), backend=ACP_BACKEND_CLAUDE) == []

    @pytest.mark.parametrize("spec", [None, "not a spec", 7, []])
    def test_a_malformed_spec_yields_no_finding(self, spec):
        # This runs on a session-establishment path; raising there would cost the
        # session over a diagnostic.
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CLAUDE) == []

    @pytest.mark.parametrize("wire", [None, "servers", {"name": "x"}, [None, 3, {}]])
    def test_a_malformed_wire_array_yields_a_finding_not_an_exception(self, wire):
        spec = {"tools": ["@srv"], "mcpServers": {"srv": {"command": "/bin/srv"}}}
        assert unresolved_server_refs(spec, wire, backend=ACP_BACKEND_CLAUDE) == ["@srv"]

    def test_the_answer_is_sorted(self):
        spec = {"tools": ["@zeta", "@alpha", "@mid"], "mcpServers": {}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CLAUDE) == [
            "@alpha",
            "@mid",
            "@zeta",
        ]


class TestTheWarning:
    def test_one_line_naming_backend_agent_refs_and_gateway(self, caplog):
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec,
                [],
                backend=ACP_BACKEND_CODEX,
                agent="kirocrew",
                gateway_enabled=False,
            )
        assert found == ["@kirocrew-core"]
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        text = records[0].getMessage()
        assert "codex" in text
        assert "kirocrew" in text
        assert "@kirocrew-core" in text
        assert "mcp_gateway=off" in text

    def test_the_gateway_state_rides_along_because_it_decides_the_remedy(self, caplog):
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=True
            )
        assert "mcp_gateway=on" in caplog.records[-1].getMessage()

    def test_a_healthy_spec_logs_nothing(self, caplog):
        spec = {"tools": ["@srv"], "mcpServers": {"srv": {"command": "/bin/srv"}}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            assert (
                warn_unresolved_server_refs(
                    spec,
                    _wire("srv"),
                    backend=ACP_BACKEND_CLAUDE,
                    agent="a",
                    gateway_enabled=False,
                )
                == []
            )
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_the_line_is_bounded_but_the_count_is_not_lost(self, caplog):
        many = [f"@srv{i:03d}" for i in range(mcp_ref_guard._REPORT_CAP + 5)]
        spec = {"tools": many, "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=False
            )
        assert len(found) == len(many)
        assert "(+5 more)" in caplog.records[-1].getMessage()


class TestTheReportSlot:
    def test_refs_reach_the_payload(self):
        r = McpSessionReport()
        r.begin_session(_wire("srv"))
        r.record_unresolved_refs(["@ghost"])
        payload = r.payload()
        assert payload is not None
        assert payload["unresolved_refs"] == ["@ghost"]

    def test_a_new_session_attempt_clears_them(self):
        # Same cross-attempt leak `begin_session` closes for every other bucket: a
        # failed session/load's finding must not be published as the replacement
        # session's own.
        r = McpSessionReport()
        r.record_unresolved_refs(["@ghost"])
        r.begin_session(_wire("srv"))
        payload = r.payload()
        assert payload is not None
        assert payload["unresolved_refs"] == []

    def test_a_second_evaluation_replaces_rather_than_appends(self):
        r = McpSessionReport()
        r.record_unresolved_refs(["@a", "@b"])
        r.record_unresolved_refs(["@b"])
        assert r.unresolved_refs == ("@b",)

    def test_refs_are_sanitized_and_deduplicated(self):
        r = McpSessionReport()
        r.record_unresolved_refs(["@a\nb", "@a b", "@x", "@x", 7, None, ""])
        # The newline collapses to a space, so the first two are one ref: a name
        # reaching a log line must not be able to forge a second line. The
        # non-strings are dropped rather than stringified -- a row reading `None`
        # would read as a server the spec asked for.
        assert r.unresolved_refs == ("@a b", "@x")

    @pytest.mark.parametrize("refs", [None, "@srv", 7])
    def test_a_non_list_records_nothing(self, refs):
        r = McpSessionReport()
        r.record_unresolved_refs(refs)
        assert r.unresolved_refs == ()


class TestTheCompositionPath:
    """The guard is reached where the wire array is actually built."""

    @pytest.fixture
    def agents_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
        monkeypatch.setattr(
            session_mcp,
            "managed_mcp_spec_entry",
            lambda name: {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}.get(name),
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
        return d

    def _spec(self, agents_dir, *, servers: dict, tools: list) -> None:
        (agents_dir / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": servers, "tools": tools}),
            encoding="utf-8",
        )

    def _client(self, tmp_path, agents_dir, backend: str) -> AcpClient:
        """A client whose spawn-path warms have run, as ``_spawn`` does.

        Both are deliberately off-loop in production: the composition site is
        shared with kiro-cli and must stay a synchronous in-memory read
        (harness-parity H13), so a test that skips the warm exercises the
        guard's silent path rather than its finding.
        """
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=backend)
        client._write_claude_local_settings()
        client._mcp_ref_spec = client._read_mcp_ref_spec()
        return client

    def _compose(self, client: AcpClient) -> list[dict[str, Any]]:
        """The array the session/new call site builds, then the guard over it."""
        wire = [
            *(client._claude_session_mcp_servers() if client._is_claude else []),
            *(client._codex_session_mcp_servers() if client._is_codex else []),
        ]
        client._begin_session_report(wire)
        client._guard_unresolved_mcp_refs(wire)
        return wire

    def test_a_codex_session_today_records_the_finding(self, tmp_path, agents_dir, caplog):
        self._spec(
            agents_dir,
            servers={"kirocrew-core": dict(_CORE)},
            tools=["@kirocrew-core", "@kirocrew-cron"],
        )
        client = self._client(tmp_path, agents_dir, ACP_BACKEND_CODEX)
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            assert self._compose(client) == []
        assert client.mcp_session_report().unresolved_refs == ("@kirocrew-core", "@kirocrew-cron")
        assert "@kirocrew-core" in caplog.text

    def test_a_claude_session_whose_mirror_projects_the_server_is_silent(
        self, tmp_path, agents_dir, caplog
    ):
        self._spec(
            agents_dir,
            servers={"kirocrew-core": dict(_CORE)},
            tools=["@kirocrew-core", "@kirocrew-cron"],
        )
        client = self._client(tmp_path, agents_dir, ACP_BACKEND_CLAUDE)
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            names = {e["name"] for e in self._compose(client)}
        assert {"kirocrew-core", "kirocrew-cron"} <= names
        assert client.mcp_session_report().unresolved_refs == ()
        assert "name no MCP server" not in caplog.text

    def test_a_kiro_session_is_silent_on_its_empty_array(self, tmp_path, agents_dir, caplog):
        # kiro-cli receives no array by design and loads the spec via --agent, so
        # the guard must read its refs against the spec.
        self._spec(agents_dir, servers={"kirocrew-core": dict(_CORE)}, tools=["@kirocrew-core"])
        client = self._client(tmp_path, agents_dir, ACP_BACKEND_KIRO)
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            assert self._compose(client) == []
        assert client.mcp_session_report().unresolved_refs == ()

    def test_the_guard_reads_no_disk_at_the_shared_call_site(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """H13: the composition site is a pure in-memory read for every backend.

        A cold snapshot silences the guard rather than resolving inline, because
        nothing about the session depends on the answer -- unlike the MCP array
        itself, which does and therefore may.
        """
        self._spec(agents_dir, servers={}, tools=["@ghost"])
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)

        def _boom(*_a, **_k):
            raise AssertionError("the guard read the agent spec at the call site")

        monkeypatch.setattr(client_mod, "agent_spec_snapshot", _boom)
        client._begin_session_report([])
        client._guard_unresolved_mcp_refs([])
        assert client.mcp_session_report().unresolved_refs == ()

    def test_a_reset_drops_the_snapshot_so_an_edited_spec_is_reread(self, tmp_path, agents_dir):
        self._spec(agents_dir, servers={}, tools=["@ghost"])
        client = self._client(tmp_path, agents_dir, ACP_BACKEND_CODEX)
        assert client._mcp_ref_spec is not None
        client._reset_state()
        assert client._mcp_ref_spec is None

    def test_the_guard_never_raises_out_of_the_call_site(self, tmp_path, agents_dir, monkeypatch):
        # It runs on a session-establishment path shared with kiro-cli, so a
        # failure here must cost a log line and nothing else.
        self._spec(agents_dir, servers={}, tools=["@ghost"])
        client = self._client(tmp_path, agents_dir, ACP_BACKEND_CODEX)

        def _boom(*_a, **_k):
            raise RuntimeError("guard exploded")

        monkeypatch.setattr(client_mod, "warn_unresolved_server_refs", _boom)
        client._begin_session_report([])
        client._guard_unresolved_mcp_refs([])  # must not raise

    def test_a_spec_that_cannot_be_read_leaves_no_snapshot(self, tmp_path, agents_dir, monkeypatch):
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)

        def _boom(*_a, **_k):
            raise OSError("spec unreadable")

        monkeypatch.setattr(client_mod, "agent_spec_snapshot", _boom)
        assert client._read_mcp_ref_spec() is None


class TestTheCallSitesAreWired:
    """The guard is reached from the real composition path, not only from a test.

    ``TestTheCompositionPath`` above reproduces the array the call site builds, so
    it proves the GUARD is right while proving nothing about whether anything calls
    it. These two do that half: one drives ``session/new`` for real, and one holds
    every roster hand-off in the client to the pairing, which is the only way to
    cover the ``session/load`` twin without standing up a resume.
    """

    @pytest.mark.asyncio
    async def test_session_new_reaches_the_guard(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        client._mcp_ref_spec = {"tools": ["@kirocrew-core"], "mcpServers": {}}

        async def _work_dir():
            return str(tmp_path)

        async def _send(_method, _params):
            return 1

        async def _wait(_rid, **_kw):
            return {"sessionId": "s-1"}

        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)

        resp = await client._new_session_following_substitution()

        assert resp["sessionId"] == "s-1"
        assert client.mcp_session_report().unresolved_refs == ("@kirocrew-core",)

    def test_every_roster_handoff_is_paired_with_the_guard(self):
        """Both call sites, held to the pairing by structure.

        ``_begin_session_report`` marks the point at which the wire array is final,
        which is exactly where the guard can be evaluated -- so a new
        session-establishment path that hands over a roster and forgets the guard is
        the omission this pins. It also pins the ORDER: the report clear runs first,
        and a guard that ran before it would have its row erased.
        """
        lines = inspect.getsource(client_mod).splitlines()
        handoffs = [i for i, ln in enumerate(lines) if "self._begin_session_report(" in ln]
        assert len(handoffs) == 2, "a session-establishment path was added or removed"
        for i in handoffs:
            roster = lines[i].split("_begin_session_report(", 1)[1]
            # The next statement, skipping the comments that explain the pairing.
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            guard = lines[j]
            assert (
                "self._guard_unresolved_mcp_refs(" in guard
            ), f"line {i} hands over a final roster and never reaches the guard: {guard!r}"
            # Same argument, so the guard judges the array that actually went out
            # rather than a stale or differently-composed one.
            assert guard.split("_guard_unresolved_mcp_refs(", 1)[1] == roster
