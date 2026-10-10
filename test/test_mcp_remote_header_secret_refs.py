"""A ``secret://`` reference in a remote MCP server's headers is reported, not sent silently.

Only an MCP server's ``env`` is resolved against the vault. A remote server's
headers are read by the session runtime as written, so the reference would reach
the server as literal text. These tests pin the three places that say so: the
header scan itself, the session-start log line and MCP report, and the call sites
that reach them on both ACP transports.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from kiro_crew.acp import mcp_ref_guard
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_ref_guard import warn_remote_header_secret_refs
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.mcp_gateway.secret_uri import (
    header_secret_refs,
    remote_header_secret_ref_error,
)

_REMOTE = {"url": "https://mcp.example.com/mcp", "headers": {"x-api-key": "secret://MY_TOKEN"}}


class TestHeaderScan:
    def test_the_config_mapping_shape(self):
        headers = {"x-api-key": "secret://A", "Accept": "application/json"}
        assert header_secret_refs(headers) == ["x-api-key"]

    def test_the_wire_list_shape(self):
        headers = [
            {"name": "Authorization", "value": "Bearer secret://A"},
            {"name": "X", "value": "y"},
        ]
        assert header_secret_refs(headers) == ["Authorization"]

    @pytest.mark.parametrize("headers", [None, "secret://A", 7, [1, "x"], {"k": 3}])
    def test_other_shapes_yield_nothing(self, headers):
        assert header_secret_refs(headers) == []

    def test_an_env_reference_is_not_flagged(self):
        assert header_secret_refs({"x-api-key": "${env:MY_TOKEN}"}) == []

    def test_the_message_names_the_header_and_the_alternative_but_no_secret(self):
        text = remote_header_secret_ref_error({"x-api-key": "secret://MY_TOKEN"})
        assert "'x-api-key'" in text
        assert "${env:NAME}" in text
        assert "kiro-cli" in text
        assert "stdio" in text
        assert "MY_TOKEN" not in text

    def test_no_reference_means_no_message(self):
        assert remote_header_secret_ref_error({"x-api-key": "plain"}) == ""

    def test_a_control_character_in_a_header_name_cannot_break_the_line(self):
        text = remote_header_secret_ref_error({"x-key\nforged": "secret://A"})
        assert "\n" not in text


class TestTheSessionWarning:
    def test_kiro_cli_is_judged_on_the_spec(self, caplog):
        spec = {"mcpServers": {"remote": dict(_REMOTE), "local": {"command": "x"}}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found, omitted = warn_remote_header_secret_refs(
                spec, [], backend=ACP_BACKEND_KIRO, agent="kirocrew"
            )
        assert found == ["remote ('x-api-key')"]
        assert omitted == 0
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        text = records[0].getMessage()
        assert "remote" in text and "x-api-key" in text
        assert "${env:NAME}" in text
        assert "MY_TOKEN" not in text

    def test_an_array_backend_is_judged_on_the_wire(self, caplog):
        spec = {"mcpServers": {"remote": dict(_REMOTE)}}
        wire = [
            {
                "type": "http",
                "name": "remote",
                "url": "https://mcp.example.com/mcp",
                "headers": [{"name": "x-api-key", "value": "secret://MY_TOKEN"}],
            }
        ]
        assert warn_remote_header_secret_refs(
            spec, wire, backend=ACP_BACKEND_CODEX, agent="kirocrew"
        ) == (["remote ('x-api-key')"], 0)
        # The spec alone does not count on a host that only sees the array.
        assert warn_remote_header_secret_refs(spec, [], backend=ACP_BACKEND_CODEX, agent="k") == (
            [],
            0,
        )

    def test_kas_is_not_told_a_header_it_never_receives(self):
        spec = {"mcpServers": {"remote": dict(_REMOTE)}}
        assert warn_remote_header_secret_refs(spec, [], backend=ACP_BACKEND_KAS, agent="k") == (
            [],
            0,
        )

    def test_a_clean_spec_logs_nothing(self, caplog):
        spec = {"mcpServers": {"remote": {"url": "https://x", "headers": {"k": "${env:T}"}}}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            assert warn_remote_header_secret_refs(
                spec, [], backend=ACP_BACKEND_KIRO, agent="k"
            ) == ([], 0)
        assert not caplog.records

    @pytest.mark.parametrize("spec", [None, [], {"mcpServers": []}, {"mcpServers": {"r": "x"}}])
    def test_a_malformed_spec_yields_nothing(self, spec):
        assert warn_remote_header_secret_refs(spec, None, backend=ACP_BACKEND_KIRO, agent="k") == (
            [],
            0,
        )

    def test_many_headers_are_bounded_and_counted(self, caplog):
        headers = {f"h{i}": "secret://A" for i in range(1000)}
        spec = {"mcpServers": {"remote": {"url": "https://x", "headers": headers}}}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found, omitted = warn_remote_header_secret_refs(
                spec, [], backend=ACP_BACKEND_KIRO, agent="k"
            )
        assert omitted == 0
        assert len(found) == 1
        assert "(+992 more)" in found[0]
        assert len(found[0]) < 200
        assert len(caplog.records[0].getMessage()) < 1000

    def test_servers_past_the_cap_are_counted_not_dropped(self, caplog):
        servers = {f"s{i}": dict(_REMOTE) for i in range(40)}
        with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
            found, omitted = warn_remote_header_secret_refs(
                {"mcpServers": servers}, [], backend=ACP_BACKEND_KIRO, agent="k"
            )
        assert len(found) + omitted == 40
        assert omitted > 0
        assert f"(+{omitted} more servers)" in caplog.records[0].getMessage()


class TestTheReport:
    def test_it_reaches_the_problem_summary(self):
        report = McpSessionReport()
        report.begin_session([])
        report.record_header_secret_refs(["remote ('x-api-key')"])
        summary = report.problem_summary()
        assert "remote ('x-api-key')" in summary
        assert "secret://" in summary

    def test_an_omitted_count_is_said(self):
        report = McpSessionReport()
        report.record_header_secret_refs(["remote ('x-api-key')"], 5)
        assert "(+5 not listed)" in report.problem_summary()

    def test_a_new_session_attempt_clears_it(self):
        report = McpSessionReport()
        report.begin_session([])
        report.record_header_secret_refs(["remote ('x-api-key')"])
        report.begin_session([])
        assert report.header_secret_refs == ()
        assert report.header_secret_refs_omitted == 0
        assert report.problem_summary() == ""

    def test_non_strings_are_ignored(self):
        report = McpSessionReport()
        report.record_header_secret_refs([None, 3, "a (b)", "a (b)"])
        assert report.header_secret_refs == ("a (b)",)


class TestTheCallSites:
    def test_the_client_records_it_at_session_composition(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_KIRO)
        client._mcp_ref_spec = {"mcpServers": {"remote": dict(_REMOTE)}}
        client._begin_session_report([])
        client._guard_unresolved_mcp_refs([])
        assert client.mcp_session_report().header_secret_refs == ("remote ('x-api-key')",)

    def test_a_failure_in_the_header_guard_costs_nothing(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        def _boom(*_a, **_k):
            raise RuntimeError("guard exploded")

        monkeypatch.setattr(client_mod, "warn_remote_header_secret_refs", _boom)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_KIRO)
        client._mcp_ref_spec = {"mcpServers": {"remote": dict(_REMOTE)}}
        client._begin_session_report([])
        client._guard_unresolved_mcp_refs([])  # must not raise

    def test_the_runtime_guard_calls_it_too(self):
        source = inspect.getsource(AcpRuntime)
        assert "warn_remote_header_secret_refs(" in source
        assert "record_header_secret_refs(" in source
