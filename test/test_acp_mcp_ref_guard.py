"""The unresolved-``@server``-ref guard (:mod:`kiro_crew.acp.mcp_ref_guard`).

The defect this guards against has shipped on three harnesses: a session comes up
holding ``tools: ["@kirocrew-core", ...]`` while nothing in its effective
``mcpServers`` defines ``kirocrew-core``, so every Crew tool is silently absent
with the harness otherwise working. These tests pin the two backend semantics
(kiro-cli reads the spec itself, everyone else gets only the wire array), the ref
spellings that are NOT server refs, and that the composition path in
``acp/client.py`` actually reaches the detector on both ``session/new`` and
``session/load``.

The resolver itself is provider-neutral and lives in
:mod:`kiro_crew.agent_sdk.mcp_refs`, which is what lets ``kirocrew doctor`` ask the
same question without taking an ACP edge; ``acp/mcp_ref_guard`` is only the log
line. Both are exercised from here, because they are one behaviour.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import mcp_ref_guard, session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.mcp_ref_guard import warn_unresolved_server_refs
from kiro_crew.acp.mcp_session_report import (
    NAME_CAP,
    McpSessionReport,
    roster_names,
    sanitize_sink_text,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE,
)
from kiro_crew.agent_sdk import mcp_refs as mcp_refs_mod
from kiro_crew.agent_sdk.mcp_refs import (
    parse_tools_refs,
    unresolved_server_refs,
    wire_server_names,
)
from kiro_crew.providers.mirrors.codex import codex_projection

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

    def test_the_two_array_readers_agree(self):
        """``wire_server_names`` and the report's ``roster_names`` read one array.

        The SDK resolver may not import the ACP layer, so it carries its own
        reader -- and a detector warning that a server is missing while the
        dashboard panel lists it two rows down is worse than no detector. Their
        agreement is therefore pinned here instead of by a shared import. The two
        differ only where the report deliberately bounds a browser payload, which
        no case below reaches.
        """
        for array in (
            _wire("a", "b"),
            _wire("a", "a"),
            [],
            [{"name": ""}, {"name": "ok"}],
            [None, 3, {}, {"name": "late"}],
            "not an array",
        ):
            assert wire_server_names(array) == list(roster_names(array)), array

    def test_a_large_spec_is_parsed_in_linear_time(self):
        """Both readers must not be quadratic: a session waits on them.

        The spec is only SIZE-capped (50 MB, ``hooks.MAX_FILE_BYTES``), never
        entry-capped, so the number of refs is operator-controlled and large. A
        linear ``in`` over the growing result list would make N distinct refs cost
        O(N**2) on the session-establishment path -- minutes for the input below,
        long enough for a watchdog to kill the gateway mid-session.

        The ceiling is deliberately loose. Linear finishes this in milliseconds, so
        a wide margin still separates the two shapes by orders of magnitude and
        cannot flake on a loaded host.
        """
        n = 60_000
        tools = [f"@srv{i}" for i in range(n)]
        wire = [{"name": f"srv{i}"} for i in range(n)]

        start = time.monotonic()
        grant_all, refs = parse_tools_refs(tools)
        names = wire_server_names(wire)
        elapsed = time.monotonic() - start

        assert grant_all is False
        assert len(refs) == n
        assert len(names) == n
        assert elapsed < 5.0, f"parsing {n} refs took {elapsed:.1f}s -- shape is not linear"

    def test_dedup_is_set_backed_while_output_stays_ordered(self):
        """The property the timing test measures, stated directly.

        Order is contractual (a stable warning across two sessions on one spec) and
        so is the dedup, so the two structures have to coexist -- and nothing may
        add to one without the other.
        """
        assert parse_tools_refs(["@b", "@a", "@b", "@a", "@c"]) == (False, ["b", "a", "c"])
        assert wire_server_names([{"name": "b"}, {"name": "a"}, {"name": "b"}]) == ["b", "a"]
        source = inspect.getsource(mcp_refs_mod.parse_tools_refs)
        assert "seen: set[str] = set()" in source
        assert "server not in seen" in source

    def test_session_mcp_mounts_through_this_parser(self):
        """The mounting decision and the guard read one vocabulary.

        A guard that read ``@srv`` where the projection read nothing would report a
        ref as unresolved while the server mounted; the reverse would mount a
        server the guard called absent. Both directions are the same defect, so the
        two must not have separate parsers.
        """
        allow = lambda tools: session_mcp._tools_allowlist({"tools": tools})  # noqa: E731
        assert allow(["@srv/tool"]).grants("srv") is True
        assert allow(["*"]).grants("anything") is True
        assert allow(["@*"]).grants("srv") is False
        assert allow(["fs_read"]).grants("srv") is False
        # No spec at all: nothing to apply, everything stands. A spec with no list:
        # an EMPTY allowlist, not "no filter".
        assert session_mcp._tools_allowlist(None).grants("anything") is True
        assert session_mcp._tools_allowlist({}).grants("srv") is False
        assert session_mcp._tools_allowlist({"tools": "@srv"}).grants("srv") is False


class TestResolution:
    def test_a_wire_served_ref_resolves(self):
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert (
            unresolved_server_refs(spec, _wire("kirocrew-core"), backend=ACP_BACKEND_CLAUDE) == []
        )

    def test_an_empty_array_on_a_mirrored_backend_reports_every_ref(self):
        """The shape that made the guard necessary, held as a pure-function case.

        A backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` whose array comes out empty
        receives nothing at all while its spec declares and references Crew's whole
        control plane -- the defect this guard reports, and the reason a session
        could be fully broken with nothing anywhere saying so. Driven here with the
        wire passed in, so it stays true of any backend that reaches this state
        rather than of one release's hook.
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

    def test_kas_resolves_its_refs_against_the_spec_not_the_wire(self):
        """KAS mounts the spec's servers as a projected agent definition, off the wire.

        On the shared runtime the array KAS receives carries broker stubs at most --
        its own ``mcpServers`` travel in ``_meta.kiro.customAgents`` -- so a guard
        judging it by the wire alone would record every ref unresolved on a default
        install, for tools that are present. The same false positive the kiro
        exemption exists for, on the second host that mounts its spec off-wire.
        """
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_KAS) == []
        # A ref the spec never declares is still nothing on KAS.
        typo = {"tools": ["@typo-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert unresolved_server_refs(typo, [], backend=ACP_BACKEND_KAS) == ["@typo-core"]

    def test_the_off_wire_exemption_is_read_from_membership_not_identity(self):
        """codex mounts exactly the array it is sent, so it is judged by the array.

        The exemption is a membership question and codex is not a member: the same
        spec that resolves on kiro and KAS is reported on codex with an empty wire
        and satisfied once the server is on it. A host that mounts its spec by its
        own channel joins the set; nothing here spells a backend id.
        """
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": _CORE}}
        assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_CODEX) == ["@kirocrew-core"]
        assert unresolved_server_refs(spec, _wire("kirocrew-core"), backend=ACP_BACKEND_CODEX) == []
        for member in ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE:
            assert unresolved_server_refs(spec, [], backend=member) == []

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
        assert session_mcp._tools_allowlist({"tools": ["@builtin"]}).grants("builtin") is True

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

    def test_a_credential_shaped_ref_is_redacted_before_it_is_logged(self, caplog):
        """A ref is untrusted text, and this warning fires in normal operation.

        The ref is whatever follows ``@`` in a ``tools`` entry, authored by an
        operator, a cloned repository's project spec, or an installed app. The log
        ring fans out to the dashboard, so a credential-shaped ref would appear
        there verbatim. Redaction is not optional on a sink like that.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        spec = {"tools": [f"@{secret}"], "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="kirocrew", gateway_enabled=False
            )
        assert secret not in caplog.text
        # The RETURN value is sanitized too, so the next consumer to log or render
        # it inherits the redaction instead of re-opening the hole.
        assert all(secret not in ref for ref in found)

    def test_a_ref_cannot_forge_a_second_log_line(self, caplog):
        spec = {"tools": ["@srv\nWARNING  everything is fine"], "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=False
            )
        assert "\n" not in found[0]
        assert len(caplog.records) == 1

    def test_the_agent_name_is_redacted_on_the_same_line(self, caplog):
        """The agent name reaches the same sink and is config-derived too.

        Asserted as REDACTION rather than as control-character stripping, because
        the format spells it ``%r`` -- which already escapes a newline, so a forged
        second line was never the exposure there. Redaction is the half ``%r``
        cannot do: it would print a secret-shaped name in full, just quoted.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        spec = {"tools": ["@ghost"], "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent=secret, gateway_enabled=False
            )
        assert len(caplog.records) == 1
        assert secret not in caplog.records[0].getMessage()

    def test_a_ref_is_length_bounded_in_the_line(self, caplog):
        spec = {"tools": ["@" + "x" * 5000], "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=False
            )
        assert len(found[0]) <= NAME_CAP

    def test_the_logged_text_is_rebuilt_from_the_modules_own_alphabet(self):
        """The log line must not carry a string DERIVED from the spec.

        ``py/clear-text-logging-sensitive-data`` follows the spec-derived dataflow
        into this sink and does not model the sanitizer as a barrier, and code
        scanning does not honour a per-line ``lgtm`` suppression either. This
        repository's own answer is to return characters the module owns --
        ``keeper._locus`` / ``_metric_slug``, ``name_grant``'s constant tables --
        which is what ``_log_safe`` does. Pinned as the property, not as a comment:
        every character out is one this module holds.
        """
        out = mcp_ref_guard._log_safe("@srv-1.x_Y")
        assert out == "@srv-1.x_Y"
        assert all(ch in mcp_ref_guard._LOG_ALPHABET for ch in out)

    def test_url_punctuation_never_reaches_the_log_line(self, caplog):
        """``:`` is outside the alphabet, so an endpoint cannot ride along.

        Two independent cuts, and both are worth pinning because either alone would
        look sufficient. ``parse_tools_refs`` keeps only the text before the first
        ``/``, so the path half of a URL never becomes a ref at all; the alphabet
        then drops the ``:`` that a host:port needs. Hardening on top of the
        redactors, not instead of them.
        """
        spec = {
            "tools": ["@host.example:8080", "@sneaky/../../etc/passwd"],
            "mcpServers": {},
        }
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=False
            )
        # The parser already cut at the first slash, so no path segment is a ref.
        assert found == ["@host.example:8080", "@sneaky"]
        line = caplog.records[0].getMessage()
        assert "host.example8080" in line  # still identifiable
        assert ":8080" not in line
        assert "passwd" not in line

    def test_the_rebuild_runs_after_the_redactors_not_instead_of_them(self, caplog):
        """Order is the load-bearing part: the alphabet alone would leak a key.

        ``AKIAIOSFODNN7EXAMPLE`` is pure alphanumerics, so a rebuild would pass it
        through verbatim. Redaction is what removes the secret; the rebuild removes
        the punctuation and the dataflow.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        assert mcp_ref_guard._log_safe(secret) == secret  # the alphabet alone: no help
        spec = {"tools": [f"@{secret}"], "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=False
            )
        assert secret not in caplog.records[0].getMessage()

    def test_the_agent_name_is_rebuilt_too_not_only_redacted(self, caplog):
        # It reaches the same sink through the same dataflow, so it needs both
        # halves; redaction alone would leave the punctuation and the taint edge.
        spec = {"tools": ["@ghost"], "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            warn_unresolved_server_refs(
                spec,
                [],
                backend=ACP_BACKEND_CODEX,
                agent="crew:9100/x",
                gateway_enabled=False,
            )
        line = caplog.records[0].getMessage()
        assert "crew9100" in line
        assert ":9100" not in line and "/x" not in line

    def test_the_rebuild_returns_the_alphabets_own_characters(self):
        """The taint-severance property, which no output assertion can reach.

        ``_LOG_ALPHABET[idx]`` and ``ch`` are equal strings, so swapping them
        changes nothing observable -- and everything about whether a taint query
        sees the result as derived from the input. The property therefore lives in
        the source shape, which is exactly what the analyser reads, so that is
        where it is pinned. Same reason ``keeper._locus`` spells out "append the
        ALPHABET's own character object, not the input's" in a comment.
        """
        body = inspect.getsource(mcp_ref_guard._log_safe)
        assert "out.append(_LOG_ALPHABET[idx])" in body
        assert "out.append(ch)" not in body

    def test_an_all_dropped_name_still_prints_something(self):
        # A row reading nothing would look like a bug in the report rather than a
        # name made entirely of characters the log will not carry.
        assert mcp_ref_guard._log_safe("::://") == "?"

    def test_a_logged_token_is_length_bounded(self):
        assert len(mcp_ref_guard._log_safe("x" * 10_000)) == mcp_ref_guard._LOG_TOKEN_MAX

    def test_one_sanitizer_serves_the_log_line_and_the_report(self):
        """The guard shares the report's cleaner rather than repeating its order.

        Two sanitizers with the same job are two that can drift, and the one that
        drifts is the one that stops redacting -- which is the same
        two-readers-of-one-thing failure this whole PR is about, in miniature.
        """
        assert mcp_ref_guard.sanitize_sink_text is sanitize_sink_text
        source = inspect.getsource(mcp_ref_guard)
        assert "redact_credentials" not in source
        assert "isprintable" not in source

    def test_the_line_is_bounded_but_the_count_is_not_lost(self, caplog):
        many = [f"@srv{i:03d}" for i in range(mcp_ref_guard._REPORT_CAP + 5)]
        spec = {"tools": many, "mcpServers": {}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found = warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_CODEX, agent="a", gateway_enabled=False
            )
        assert len(found) == len(many)
        assert "(+5 more)" in caplog.records[-1].getMessage()


class TestTheDocumentedReach:
    """The module must claim exactly the reach it has, and the reach is both transports.

    The resolver is provider-neutral and is evaluated at BOTH session-establishment
    transports: ``AcpClient``'s composition and ``AcpRuntime``'s. A host served by
    the shared runtime that reads no agent file of Crew's is the case the detector
    was written for, so a runtime that skipped it would leave the one host that
    needs it unreported -- and a docstring still naming the runtime as out of reach
    would be the unexamined coverage claim this module exists to stop.
    """

    def test_the_module_names_both_transports_as_in_reach(self):
        doc = mcp_refs_mod.__doc__ or ""
        assert "AcpClient" in doc and "AcpRuntime" in doc
        assert "never reaches" not in doc, "the module still disclaims the runtime"

    def test_the_runtime_call_site_is_wired(self):
        from kiro_crew.acp import runtime as runtime_mod

        src = inspect.getsource(runtime_mod)
        assert "warn_unresolved_server_refs(" in src
        assert "agent_spec_snapshot" in src, "the runtime must read the spec the detector reads"


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
        # The global settings file is a second source of per-tool restrictions;
        # these tests supply it (or leave it absent) rather than read the machine's.
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
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
        """The array the client's session/new call site builds, then the guard over it.

        Every backend the CLIENT still composes for. A backend served by the shared
        runtime composes elsewhere -- see :meth:`_codex_wire`, which is the same two
        steps on that path.
        """
        wire = [
            *(client._claude_session_mcp_servers() if client._is_claude else []),
        ]
        client._begin_session_report(wire)
        client._guard_unresolved_mcp_refs(wire)
        return wire

    def _codex_wire(self, tmp_path) -> tuple[list[dict[str, Any]], McpSessionReport]:
        """The array a codex session receives, plus the report the guard wrote into.

        codex is served by AcpRuntime, so its array is composed by the mirror
        (:func:`codex_projection`, the producer) and then narrowed by the host
        (:meth:`CodexHarness.session_mcp_servers`, which drops any element whose
        transport this session's handshake did not advertise). Those two ARE the
        composition on that path, and the guard question is asked over their result
        exactly as the client asks it over its own: the detector and the report are
        provider-neutral, which is what lets one behaviour be pinned on both
        transports without a second detector.

        The handshake advertises stdio, which every ACP agent must support, so the
        narrowing keeps what the mirror projected and the finding below is the
        mirror's withhold rather than a dropped transport.
        """
        projected = codex_projection("kirocrew", work_dir=tmp_path).params["mcpServers"]
        wire = CodexHarness().session_mcp_servers(
            list(projected), agent_capabilities={"mcpCapabilities": {"stdio": True}}
        )
        report = McpSessionReport()
        report.begin_session(wire)
        report.record_unresolved_refs(
            warn_unresolved_server_refs(
                session_mcp.agent_spec_snapshot("kirocrew", work_dir=tmp_path),
                wire,
                backend=ACP_BACKEND_CODEX,
                agent="kirocrew",
                # No shared MCP gateway in this session, so the remedy the line
                # names is the projection rather than stub routing.
                gateway_enabled=False,
            )
        )
        return wire, report

    def test_a_codex_session_records_what_its_mirror_withholds(self, tmp_path, agents_dir, caplog):
        """On a mirrored codex session the finding narrows to the withheld set.

        The refs the mirror projects resolve, so the guard is silent about them.
        What it still reports is what the mirror deliberately WITHHOLDS: an
        identity-bound Crew server reaches codex from the spec unreplaced and would
        answer ``not_bound`` to every call, so it is not mounted -- and this guard's
        own sentence is then exactly right, the tools are absent from the session.
        Two lines, two jobs: the mirror logs WHY it withheld, and this one records
        that the spec asked for it.

        Driven through the runtime-path composition (:meth:`_codex_wire`), because
        that is where a codex array is built. The claim under test is unchanged by
        the transport: what the mirror keeps back, the report names.
        """
        self._spec(
            agents_dir,
            servers={"kirocrew-core": dict(_CORE), "kirocrew-work": dict(_CORE)},
            tools=["@kirocrew-core", "@kirocrew-cron", "@kirocrew-work"],
        )
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            wire, report = self._codex_wire(tmp_path)
        names = {e["name"] for e in wire}
        assert {"kirocrew-core", "kirocrew-cron"} <= names
        assert "kirocrew-work" not in names
        assert report.unresolved_refs == ("@kirocrew-work",)
        assert "@kirocrew-work" in caplog.text

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
        # A ref no projection can resolve, so this proves the CALL SITE rather than
        # re-testing the mirror: codex's array re-derives the control plane, so
        # ``@kirocrew-core`` would resolve and leave the guard nothing to report.
        client._mcp_ref_spec = {"tools": ["@ghost"], "mcpServers": {}}

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
        assert client.mcp_session_report().unresolved_refs == ("@ghost",)

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


class TestTheRuntimeCallSitesAreWired:
    """The shared-runtime twin of the class above.

    Same two halves: one drives a runtime ``session/new`` for real against a host
    whose harness NARROWS the array, and one holds every roster hand-off in the
    runtime to the pairing. The narrowing host is the point -- it is what makes
    "the array that went on the wire" and "the roster the caller composed" two
    different lists, and every consumer that means the former must read it.
    """

    @pytest.mark.asyncio
    async def test_a_runtime_session_reports_and_guards_the_wire_roster(self, monkeypatch):
        """Three facts, one session: the wire carries the narrowed array; the report
        says the narrowed array was sent (not the pre-filter roster); the guard judges
        refs against the narrowed array, so a ref satisfied only by a dropped element
        is reported. codex's harness drops an ``sse`` element its adapter did not
        advertise, which is the narrowing this uses.
        """
        from contextlib import ExitStack
        from unittest.mock import MagicMock, patch

        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CODEX, expect_mcp_reports=False)
        proc = MagicMock()
        proc.stdout = None
        proc.stdin = MagicMock()
        proc.returncode = None
        proc.pid = 4242
        rt._process = proc
        rt._pid = 4242
        rt._initialized = True
        # stdio only -- so the sse element below is one the harness drops.
        rt._agent_capabilities = {"mcpCapabilities": {"http": False, "sse": False}}

        # The caller's roster: one element the wire will carry, one it will not.
        kept = {"name": "kept", "type": "stdio", "command": "x", "args": [], "env": []}
        dropped = {"name": "dropped", "type": "sse", "url": "http://127.0.0.1:1/sse", "headers": []}
        # A spec whose refs name BOTH: ``@kept`` resolves on the wire, ``@dropped``
        # resolves only against the pre-filter roster -- which is the mistake.
        spec = {"tools": ["@kept", "@dropped"], "mcpServers": {}}

        sent: list[tuple[str, dict[str, Any]]] = []

        async def _send_and_await(method, params, timeout=None):
            sent.append((method, params))
            if method == "session/new":
                return {
                    "sessionId": "sid-1",
                    "modes": {"currentModeId": "agent"},
                    "configOptions": [
                        {"id": "mode", "options": [{"value": "read-only"}, {"value": "agent"}]}
                    ],
                }
            return {}

        async def _send_request(method, params):
            return 999

        async def _wait_for_response(_self, req_id, timeout=None):
            return {}

        with ExitStack() as stack:
            stack.enter_context(patch.object(rt, "_send_and_await", _send_and_await))
            stack.enter_context(patch.object(rt, "send_request", _send_request))
            stack.enter_context(
                patch.object(AcpSessionHandle, "_wait_for_response", _wait_for_response)
            )
            # codex is a mirrored host, so its array and the guard's snapshot arrive
            # together from the mirror hop; stand that hop in with the roster above.
            from kiro_crew.acp.runtime import _MirroredSessionMcp

            async def _mirrored(*_a, **_k):
                return _MirroredSessionMcp(
                    servers=[kept, dropped],
                    denied_tools=frozenset(),
                    stub_token="",
                    derived_spec_snapshot=None,
                    ref_spec=spec,
                )

            stack.enter_context(patch.object(rt, "_mirrored_session_mcp", _mirrored))
            handle = await rt.create_session(cwd="/w", agent="kirocrew")

        wire = [p for m, p in sent if m == "session/new"][0]["mcpServers"]
        assert [e["name"] for e in wire] == ["kept"], "the harness did not narrow the array"

        report = handle.mcp_session_report()
        # The report describes what was SENT: the narrowed array, not the roster.
        assert report.configured == ("kept",), report.configured
        # The guard judged against the narrowed array: ``@dropped`` names a server the
        # wire never carried, and is reported as such.
        assert report.unresolved_refs == ("@dropped",), report.unresolved_refs

    def test_every_runtime_roster_handoff_is_paired_with_the_guard(self):
        """Both runtime call sites (session/new, session/load), held by structure.

        ``begin_session(`` marks where the wire array is final. The guard must be the
        next statement, and must take the SAME roster, so a new establishment path
        that hands over a roster and forgets the guard -- or guards a different list
        than it reported -- is the omission this pins.
        """
        from kiro_crew.acp import runtime as runtime_mod

        lines = inspect.getsource(runtime_mod).splitlines()
        handoffs = [i for i, ln in enumerate(lines) if ".begin_session(" in ln]
        assert len(handoffs) == 2, "a runtime session-establishment path was added or removed"
        for i in handoffs:
            roster = lines[i].split(".begin_session(", 1)[1].rstrip(")")
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            guard = lines[j]
            assert (
                "self._guard_unresolved_mcp_refs(" in guard
            ), f"line {i} hands over a final roster and never reaches the guard: {guard!r}"
            # The guard's LAST argument is the roster; it must be the one reported.
            assert (
                guard.rstrip(")").split(",")[-1].strip() == roster.strip()
            ), f"line {i}: report and guard read different rosters: {roster!r} vs {guard!r}"

    def test_the_runtime_guard_never_raises_out_of_the_call_site(self, monkeypatch):
        """A diagnostic that can fail a session is a worse defect than the one it
        detects: a detector that explodes must resolve to silence."""
        from unittest.mock import MagicMock

        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.runtime import AcpRuntime

        def _boom(*_a, **_k):
            raise RuntimeError("detector exploded")

        monkeypatch.setattr(runtime_mod, "warn_unresolved_server_refs", _boom)
        rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CODEX, expect_mcp_reports=False)
        handle = MagicMock()
        rt._guard_unresolved_mcp_refs(handle, {"tools": ["@x"]}, "kirocrew", [])  # must not raise
        handle.mcp_session_report.return_value.record_unresolved_refs.assert_not_called()

    def test_the_runtime_guard_is_synchronous_and_reads_no_disk(self):
        """H13: the guard adds no suspension point to any host's session start.

        The spec is read as a passenger of the off-loop hop each branch already
        makes to resolve its array -- the mirror's projection hop, or the pooled-stub
        hop -- never as a hop of its own. So the guard is a plain method that takes
        the snapshot it is handed, and the module's only ``agent_spec_snapshot``
        read sits in ``_ref_spec_snapshot``, which only those two hop bodies call.
        """
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.runtime import (
            AcpRuntime,
            _pooled_session_servers_and_ref_spec,
            _ref_spec_snapshot,
        )

        assert not inspect.iscoroutinefunction(AcpRuntime._guard_unresolved_mcp_refs)
        guard_src = inspect.getsource(AcpRuntime._guard_unresolved_mcp_refs)
        assert "await" not in guard_src
        assert "agent_spec_snapshot" not in guard_src
        # ONE read in the module, inside the fail-soft wrapper.
        module_src = inspect.getsource(runtime_mod)
        reads = module_src.count("agent_spec_snapshot(")
        assert reads == 1, f"expected one wrapped read of the spec, found {reads} call(s)"
        assert "agent_spec_snapshot(" in inspect.getsource(_ref_spec_snapshot)
        # The two hops that carry the read, and nothing else in the module.
        wrapper_calls = module_src.count("_ref_spec_snapshot(")
        assert wrapper_calls == 3, f"expected the def and two hop-body calls, found {wrapper_calls}"
        assert "_ref_spec_snapshot(" in inspect.getsource(_pooled_session_servers_and_ref_spec)
        assert "_ref_spec_snapshot(" in inspect.getsource(AcpRuntime._mirrored_session_mcp)
        # And no standalone hop for it anywhere.
        assert "to_thread(agent_spec_snapshot" not in module_src
        assert "to_thread(_ref_spec_snapshot" not in module_src

    def test_the_snapshot_read_cannot_fail_the_session(self, monkeypatch):
        """A refused or unreadable spec is ``None`` to the guard, never an exception.

        ``agent_spec_snapshot`` runs the derived-spec freshness gate, which raises
        for a stale mirror it could not repair. That is the right answer for the
        projection -- its output IS the session's MCP surface -- and the wrong one
        for a diagnostic riding in the same hop: on the pooled kiro path the array
        never read the derived spec at all, so a raise here would fail a
        ``session/new`` that was going to succeed. The client twin
        (``AcpClient._read_mcp_ref_spec``) resolves every failure to ``None``; the
        runtime's wrapper must too, and the pooled hop must still hand back its
        array when the snapshot fails.
        """
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.runtime import _pooled_session_servers_and_ref_spec, _ref_spec_snapshot
        from kiro_crew.agent_sdk.backends import overlay_project_scope

        def _stale(agent, *, work_dir=None):
            raise RuntimeError("derived spec is stale and could not be repaired")

        monkeypatch.setattr(runtime_mod, "agent_spec_snapshot", _stale)
        assert _ref_spec_snapshot("kirocrew-worker", "/tmp") is None

        seen: dict[str, object] = {}

        def _pooled(overlay, agent, **scope):
            seen.update(scope)
            return ["s"]

        monkeypatch.setattr(runtime_mod, "pooled_session_servers", _pooled)
        servers, spec = _pooled_session_servers_and_ref_spec(
            None, "kirocrew-worker", "kiro", "/tmp"
        )
        assert servers == ["s"]
        assert spec is None
        # The overlay lookup was scoped by the ONE decider, at this call: kiro-cli
        # resolves ``--agent`` from the checkout itself, so its scope names it.
        assert seen == overlay_project_scope("kiro", "/tmp")
        assert seen["work_dir"] == "/tmp"


class TestTheSpawnHopCarriesTheSnapshot:
    """The warm rides in the hop ``_spawn`` already had, and adds no await.

    The reviewer finding that produced this shape was specific: an added
    ``await asyncio.to_thread`` on the Kiro construction path is a new suspension
    point in service of a diagnostic (harness-parity H13). Folding the snapshot
    into the pre-existing ``mkdir`` hop answers it by making the added await not
    exist -- which is only true while nothing re-adds one, hence the structural
    pin below.
    """

    def test_the_hop_creates_the_dir_and_warms_the_snapshot(self, tmp_path, monkeypatch):
        work = tmp_path / "nested" / "work"
        client = AcpClient(work_dir=work, agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        monkeypatch.setattr(
            client_mod, "agent_spec_snapshot", lambda _a, **_k: {"tools": ["@ghost"]}
        )
        client._mcp_ref_spec = None

        client._prepare_spawn_workspace()

        assert work.is_dir()
        assert client._mcp_ref_spec == {"tools": ["@ghost"]}

    def test_the_mkdir_still_raises_and_the_snapshot_never_does(self, tmp_path, monkeypatch):
        """Two halves, two failure contracts, and the order is what separates them.

        The spawn genuinely cannot proceed without the work dir, so that half must
        still raise. The diagnostic must never cost a session, so its half is
        swallowed -- and it runs SECOND, so a failed mkdir skips it rather than
        reporting on a session that has no workspace.
        """
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        client = AcpClient(
            work_dir=blocker / "work", agent="kirocrew", acp_backend=ACP_BACKEND_CODEX
        )
        with pytest.raises(OSError):
            client._prepare_spawn_workspace()
        assert client._mcp_ref_spec is None

        # The other direction: a spec that cannot be read leaves the dir made.
        def _boom(*_a, **_k):
            raise OSError("spec unreadable")

        ok = AcpClient(work_dir=tmp_path / "ok", agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
        monkeypatch.setattr(client_mod, "agent_spec_snapshot", _boom)
        ok._prepare_spawn_workspace()
        assert (tmp_path / "ok").is_dir()
        assert ok._mcp_ref_spec is None

    def test_the_detector_adds_no_await_to_the_construction_path(self):
        """The snapshot is never awaited on its own -- only inside the mkdir hop.

        Pinned as the absence of a separate hop rather than as a total await count,
        so an unrelated await added to ``_spawn`` later cannot fail this for the
        wrong reason. What must stay true is narrow: the detector's read reaches
        the executor ONLY as a passenger of the hop ``_spawn`` already had, which
        is what keeps it off kiro-cli's suspension-point budget (H13).
        """
        spawn = inspect.getsource(AcpClient._spawn)
        assert "await asyncio.to_thread(self._prepare_spawn_workspace)" in spawn
        assert "_read_mcp_ref_spec" not in spawn
        assert "_mcp_ref_spec" not in spawn

        # ...and the fold itself is not split back apart: one hop, both halves.
        hop = inspect.getsource(AcpClient._prepare_spawn_workspace)
        assert "self._work_dir.mkdir(" in hop
        assert "self._mcp_ref_spec = self._read_mcp_ref_spec()" in hop
