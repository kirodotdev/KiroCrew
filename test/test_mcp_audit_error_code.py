"""The MCP audit row of a failed tool call names the code the tool answered with.

``call_tool_with_logging`` writes one ``tool_invocation`` row for each answer a tool
handler returns. A failed answer ending ``[code]`` or ``[code, field=...]``
(``mcp_work._refusal``) records ``metadata.code``; nothing else about the answer
reaches the row.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core, mcp_shared, mcp_work
from kiro_crew.mcp_shared_calls import DeferredTool, call_tool_with_logging

_CODED = (
    "Error: could not record your report: this conductor has no crew log unit "
    "to record into [crew_log_off]"
)


class _RecordingSel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_tool_invocation(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def _passthrough(_name: str, raw: dict[str, Any]) -> dict[str, Any]:
    return raw


def _one_row(answer: str) -> dict[str, Any]:
    audit = _RecordingSel()
    with patch.object(mcp_shared, "sel", return_value=audit):
        result = call_tool_with_logging(
            "work_report",
            {"status": "done"},
            _passthrough,
            lambda _name, _args: answer,
            session_key="chat-x",
            downstream_service="kirocrew-work",
        )
    assert result == answer
    assert len(audit.rows) == 1, audit.rows
    return audit.rows[0]


def test_a_coded_refusal_records_its_code_and_none_of_its_text():
    row = _one_row(_CODED)
    assert row["outcome"] == "failed"
    assert row["error"] == "execution_failed"
    assert row.get("metadata") == {"code": "crew_log_off"}
    logged = json.dumps(row)
    for fragment in ("could not record", "conductor", "crew log unit"):
        assert fragment not in logged


def test_a_code_with_a_field_records_the_code_alone():
    row = _one_row("Error: could not record your report: too long [field_too_long, field=summary]")
    assert row.get("metadata") == {"code": "field_too_long"}


@pytest.mark.parametrize(
    "answer",
    [
        "Error: could not record your report: the gateway is unreachable",
        "Error: bad path [/etc/x]",
        "Error: x [Mixed Case]",
        "Error: x [crew_log_off] and more",
        "Error: x [1st_code]",
        "Error: x [" + "a" * 65 + "]",
        "Error: x []",
        "Error: expected one of [read, write]",
    ],
)
def test_a_failed_answer_without_a_trailing_code_shaped_bracket_records_none(answer):
    row = _one_row(answer)
    assert row["outcome"] == "failed"
    assert row["error"] == "execution_failed"
    assert not row.get("metadata")


def test_a_completed_answer_never_records_a_code():
    row = _one_row("Recorded [ok]")
    assert row["outcome"] == "completed"
    assert row["error"] == ""
    assert not row.get("metadata")


class _Parked(DeferredTool):
    def due_at(self) -> float:
        return 0.0

    def step(self) -> str | None:
        return None


def test_a_deferred_call_settling_with_a_coded_error_records_the_code():
    audit = _RecordingSel()
    with patch.object(mcp_shared, "sel", return_value=audit):
        deferred = call_tool_with_logging(
            "work_report",
            {"status": "done"},
            _passthrough,
            lambda _name, _args: _Parked(),
            session_key="chat-x",
            downstream_service="kirocrew-work",
        )
        assert isinstance(deferred, DeferredTool)
        assert audit.rows == []
        deferred.on_settled(_CODED)
    assert len(audit.rows) == 1, audit.rows
    assert audit.rows[0]["outcome"] == "failed"
    assert audit.rows[0].get("metadata") == {"code": "crew_log_off"}


def test_the_work_tool_refusal_reaches_the_row_with_its_code(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {"error": "no crew log unit to record into", "code": "crew_log_off"},
    )
    audit = _RecordingSel()
    with patch.object(mcp_shared, "sel", return_value=audit):
        out = mcp_work._call_tool("work_report", {"status": "done", "summary": "x"})
    assert out.endswith("[crew_log_off]")
    assert len(audit.rows) == 1, audit.rows
    assert audit.rows[0]["tool_name"] == "work_report"
    assert audit.rows[0].get("metadata") == {"code": "crew_log_off"}
