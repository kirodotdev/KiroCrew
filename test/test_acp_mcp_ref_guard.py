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
from kiro_crew.acp.mcp_ref_guard import warn_unresolved_server_refs
from kiro_crew.acp.mcp_session_report import (
    NAME_CAP,
    McpSessionReport,
    roster_names,
    sanitize_sink_text,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk import mcp_refs as mcp_refs_mod
from kiro_crew.agent_sdk.mcp_refs import (
    parse_tools_refs,
    unresolved_server_refs,
    wire_server_names,
)

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
    """The module must not claim a reach it does not have.

    The resolver is provider-neutral, but the RUNTIME call site is ``AcpClient``'s
    composition. KAS runs on ``AcpRuntime``, which composes its array elsewhere, so
    a KAS session never reaches the detector -- and an over-broad claim in the one
    module written to stop unexamined claims about backend coverage would be the
    same defect it exists to catch.
    """

    def test_the_module_names_kas_as_out_of_runtime_reach(self):
        doc = mcp_refs_mod.__doc__ or ""
        assert "AcpRuntime" in doc
        assert "KAS" in doc

    def test_the_runtime_call_site_really_is_acpclient_only(self):
        from kiro_crew.acp import runtime as runtime_mod

        # If a future change wires the detector into the runtime too, this fails and
        # the docstring above has to be corrected with it.
        assert "mcp_ref_guard" not in inspect.getsource(runtime_mod)


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
