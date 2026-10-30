"""How a completed call is scored in the call-metrics file and a failed tool call logged.

These tests pin ``_tool_call_error_text`` on both failure shapes (a JSON-RPC ``error`` and a
``result`` whose ``isError`` is the boolean ``True``, never a string), its ``None`` for a success,
an explicit ``isError`` of false or a malformed frame, its truncation to the log cap with an
ellipsis on a single line, its redaction of credentials and its removal of control characters; a
failed ``tools/call`` scored ``ok: false`` with one WARNING naming the tool, session and error, a
newline in a tool name that cannot forge a second log record, a success scored ``ok: true`` with no
WARNING, and an error on another method scored ``ok: false`` without the tool-failure WARNING;
``_log_safe_identifier`` redacting, capping and cleaning a name; and the metrics writer, which
writes nothing while ``_METRICS_PATH`` is unset, appends one JSON line per call once it is set,
drops a write to an unwritable path silently and tracks each spawned metric task until it finishes.
A topic of the ``test_mcp_gateway_backend_coverage`` family:
``test_mcp_gateway_backend_coverage.py`` keeps the frame helpers and ``forward_from_stub``, the
other siblings are ``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py``,
``test_mcp_gateway_backend_coverage_mcp_apps.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``, and the shared fixture and helpers live
in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import _line, _make_backend, _settle

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import _log_safe_identifier, _tool_call_error_text


class TestToolCallFailureLogging:
    """A failed MCP tool call leaves a greppable trace.

    The metric scores a ``result`` with ``isError: true`` as a failure
    (``ok: false``), and a WARNING line goes to the gateway log naming the
    server, tool, session and a truncated error so an operator searching the
    log for an MCP outage finds the failing call, not just the session's
    lifecycle lines.
    """

    @pytest.mark.parametrize(
        "msg, expected",
        [
            # JSON-RPC error: message string is used.
            ({"error": {"code": -32000, "message": "boom"}}, "boom"),
            # JSON-RPC error with no message: compact JSON of the error object.
            ({"error": {"code": -32000}}, '{"code":-32000}'),
            # String error (some servers send a bare string).
            ({"error": "nope"}, "nope"),
            # isError result: text content parts are joined.
            (
                {
                    "result": {
                        "isError": True,
                        "content": [
                            {"type": "text", "text": "IAM role not found"},
                        ],
                    }
                },
                "IAM role not found",
            ),
            # isError result with no text part: compact JSON fallback.
            (
                {"result": {"isError": True, "content": [{"type": "image"}]}},
                '{"isError":true,"content":[{"type":"image"}]}',
            ),
        ],
    )
    def test_detects_both_failure_shapes(self, msg, expected) -> None:
        assert _tool_call_error_text(msg) == expected

    def test_success_and_malformed_return_none(self) -> None:
        # A settled success.
        assert _tool_call_error_text({"result": {"content": []}}) is None
        # isError explicitly false.
        assert _tool_call_error_text({"result": {"isError": False}}) is None
        # Malformed frame carrying neither error nor result.
        assert _tool_call_error_text({"id": 1}) is None

    def test_long_error_is_truncated_and_single_line(self) -> None:
        text = _tool_call_error_text({"error": {"message": "x\ny\n" + "A" * 1000}})
        assert text is not None
        assert len(text) <= backend_mod._TOOL_ERROR_LOG_MAX
        assert "\n" not in text  # newlines collapsed so the log line stays one line
        assert text.endswith("\u2026")  # ellipsis marks the truncation

    def test_credential_in_error_is_redacted(self) -> None:
        # A secret in the untrusted server error must not survive into the
        # breadcrumb (credentials and exfil URLs are redacted before logging).
        text = _tool_call_error_text(
            {"error": {"message": "auth failed with key AKIAIOSFODNN7EXAMPLE"}}
        )
        assert text is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in text

    @pytest.mark.asyncio
    async def test_iserror_result_scored_not_ok_and_warned(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        metrics = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(metrics))
        backend = _make_backend()
        caller = CallerContext(session_key="cron:f3933807", session_type="cron")
        await backend.forward_from_stub(
            "s1",
            {"method": "tools/call", "id": 1, "params": {"name": "get_aws_creds"}},
            caller=caller,
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(
                _line(
                    {
                        "id": "gw-4242-1",
                        "result": {
                            "isError": True,
                            "content": [
                                {"type": "text", "text": "IAM role not found: ReadOnly"},
                            ],
                        },
                    }
                )
            )
            await _settle(backend)

        # Metric scored the tool failure as not ok.
        record = json.loads(metrics.read_text().splitlines()[-1])
        assert record["ok"] is False
        assert record["method"] == "tools/call"

        # One greppable WARNING naming server, tool, session, error.
        assert "mcp tool call failed" in caplog.text
        assert "tool=get_aws_creds" in caplog.text
        assert "session=cron:f3933807" in caplog.text
        assert "IAM role not found: ReadOnly" in caplog.text

    @pytest.mark.asyncio
    async def test_jsonrpc_error_scored_not_ok_and_warned(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        metrics = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(metrics))
        backend = _make_backend()
        await backend.forward_from_stub(
            "s1", {"method": "tools/call", "id": 1, "params": {"name": "search"}}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(
                _line(
                    {
                        "id": "gw-4242-1",
                        "error": {"code": -32000, "message": "r5 status: 429"},
                    }
                )
            )
            await _settle(backend)

        record = json.loads(metrics.read_text().splitlines()[-1])
        assert record["ok"] is False
        assert "mcp tool call failed" in caplog.text
        assert "r5 status: 429" in caplog.text

    @pytest.mark.asyncio
    async def test_newline_in_tool_name_cannot_forge_a_log_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A tool name is caller input; a newline in it must not split the
        WARNING into a second forged log record."""
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
        backend = _make_backend()
        await backend.forward_from_stub(
            "s1", {"method": "tools/call", "id": 1, "params": {"name": "evil\nWARNING forged line"}}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(
                _line(
                    {
                        "id": "gw-4242-1",
                        "error": {"code": -32000, "message": "x"},
                    }
                )
            )
            await _settle(backend)

        (rec,) = [r for r in caplog.records if "mcp tool call failed" in r.getMessage()]
        assert "\n" not in rec.getMessage()
        assert "tool=evil WARNING forged line" in rec.getMessage()

    @pytest.mark.asyncio
    async def test_success_scored_ok_and_not_warned(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        metrics = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(metrics))
        backend = _make_backend()
        await backend.forward_from_stub(
            "s1", {"method": "tools/call", "id": 1, "params": {"name": "ok_tool"}}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(
                _line(
                    {
                        "id": "gw-4242-1",
                        "result": {"content": [{"type": "text", "text": "fine"}]},
                    }
                )
            )
            await _settle(backend)

        record = json.loads(metrics.read_text().splitlines()[-1])
        assert record["ok"] is True
        assert "mcp tool call failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_non_tools_call_error_scored_not_ok_but_not_warned(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A JSON-RPC error on a non-tools/call method still scores ok:false
        (unchanged metric behaviour) but does NOT emit the tool-failure
        WARNING — the breadcrumb is scoped to tool calls."""
        metrics = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(metrics))
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "tools/list", "id": 1})
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(
                _line(
                    {
                        "id": "gw-4242-1",
                        "error": {"code": -32601, "message": "no"},
                    }
                )
            )
            await _settle(backend)

        record = json.loads(metrics.read_text().splitlines()[-1])
        assert record["ok"] is False
        assert "mcp tool call failed" not in caplog.text

    def test_stringy_iserror_is_not_a_failure(self) -> None:
        """``isError`` is scored by strict identity, not truthiness: a server
        that stringifies the flag (``"isError": "false"``) must not turn a
        successful call into a logged failure whose own content is dumped."""
        # Truthy string "false" would pass a bare ``get("isError")`` check.
        assert (
            _tool_call_error_text(
                {
                    "result": {
                        "isError": "false",
                        "content": [
                            {"type": "text", "text": "secret-looking body"},
                        ],
                    }
                }
            )
            is None
        )
        # Only a real boolean True is a failure.
        assert (
            _tool_call_error_text(
                {
                    "result": {
                        "isError": True,
                        "content": [
                            {"type": "text", "text": "real failure"},
                        ],
                    }
                }
            )
            == "real failure"
        )

    def test_control_characters_are_collapsed_in_error_text(self) -> None:
        """A terminal escape / NUL / BEL in the untrusted error must not reach
        the log raw — ``str.split()`` only strips whitespace, so these are
        filtered by ``isprintable``."""
        text = _tool_call_error_text({"error": {"message": "red\x1b[31mtext\x00\x07 end"}})
        assert text is not None
        for ch in ("\x1b", "\x00", "\x07"):
            assert ch not in text
        # The non-printable bytes are gone; the printable remainder survives.
        assert "red" in text and "text" in text and "end" in text

    def test_log_safe_identifier_redacts_and_caps(self) -> None:
        """A server/tool name is redacted (a credential-shaped name cannot leak)
        and capped (a long name cannot push structured fields off the line);
        control characters are collapsed."""
        # Credential-shaped identifier is redacted.
        redacted = _log_safe_identifier("tool-AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        # Over-long identifier is capped with an ellipsis.
        capped = _log_safe_identifier("x" * 500)
        assert len(capped) <= backend_mod._MCP_IDENT_LOG_MAX
        assert capped.endswith("\u2026")
        # Control characters collapsed; empty-after-clean falls back.
        assert "\n" not in _log_safe_identifier("a\nb")
        assert _log_safe_identifier("\x00\x1b", fallback="?") == "?"


class TestCallMetrics:
    @pytest.mark.asyncio
    async def test_disabled_metrics_write_nothing(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
        await backend_mod._emit_call_metric({"method": "tools/call"})
        assert list(tmp_path.iterdir()) == []
        backend = _make_backend()
        backend._spawn_metric_task({"method": "tools/call"})
        assert backend._metric_tasks == set()

    @pytest.mark.asyncio
    async def test_enabled_metrics_append_one_json_line_per_call(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "calls.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(path))
        await backend_mod._emit_call_metric({"method": "tools/call", "dur_ms": 1.5})
        await backend_mod._emit_call_metric({"method": "tools/list", "dur_ms": 2.5})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["method"] for line in lines] == ["tools/call", "tools/list"]

    def test_unwritable_path_is_silently_dropped(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory can never be opened for append -> OSError -> swallowed.
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(tmp_path))
        assert backend_mod._write_metric_line({"method": "tools/call"}) is None
        assert list(tmp_path.iterdir()) == []

    def test_write_metric_line_returns_early_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.mcp_gateway import backend_routing

        opened = MagicMock(side_effect=AssertionError("no path configured, nothing to open"))
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
        monkeypatch.setattr(backend_routing, "open", opened, raising=False)
        backend_mod._write_metric_line({"method": "tools/call"})
        opened.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_metric_task_is_tracked_then_discarded(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "calls.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(path))
        backend = _make_backend()
        backend._spawn_metric_task({"method": "ping"})
        assert len(backend._metric_tasks) == 1
        await _settle(backend)
        assert backend._metric_tasks == set()
        assert json.loads(path.read_text(encoding="utf-8").strip())["method"] == "ping"
