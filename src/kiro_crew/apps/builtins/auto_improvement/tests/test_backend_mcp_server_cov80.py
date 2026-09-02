"""The read-only stdio MCP server.

Three contracts carry this file and each is a security boundary rather than a
convenience:

* **Nothing reaches the model unscanned.** Results AND error strings go through the
  redactor, and an unavailable redactor withholds rather than passes text through.
* **Audit-or-deny on dispatch.** The pre-dispatch ``invoked`` event is written
  synchronously; if it cannot be written the call is REFUSED, because no human is in
  this loop and the SEL record is the only evidence a read happened.
* **``TOOLS`` is the allowlist and the declared schema is enforced**, so a malformed
  ``tools/call`` comes back as ``INVALID_PARAMS`` instead of a ``result``.

Every handler is patched at the module seam (``progress``/``runner``/``deps``) so no
test touches a real run's artifacts.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server


class _FakeSel:
    """Records what would have been written to the Security Event Log."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def log_tool_invocation(self, **kwargs: Any) -> None:
        if self.fail:
            raise OSError("sel sink is down")
        self.calls.append(kwargs)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _FakeSel:
    rec = _FakeSel()
    monkeypatch.setattr(mcp_server, "sel", lambda: rec)
    return rec


def _outcomes(rec: _FakeSel) -> list[str]:
    return [c["outcome"] for c in rec.calls]


def _call(name: str, arguments: Any = None, req_id: Any = 1) -> dict[str, Any] | None:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return mcp_server.handle(
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": params}
    )


def _payload(reply: dict[str, Any] | None) -> dict[str, Any]:
    """Decode the JSON text a successful ``tools/call`` wraps in ``content``."""
    assert reply is not None
    return json.loads(reply["result"]["content"][0]["text"])


class TestRedactionIsFailClosed:
    def test_a_result_that_cannot_be_scanned_is_withheld(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(_text: str) -> str:
            raise RuntimeError("scanner unavailable")

        monkeypatch.setattr(mcp_server, "redact", _boom)
        assert mcp_server._redact_result("anything") == (
            '{"error": "result withheld: redaction unavailable"}'
        )
        assert "withholding" in capsys.readouterr().err

    def test_a_working_scanner_returns_the_scanned_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "redact", lambda text: text.upper())
        assert mcp_server._redact_result("ok") == "OK"

    def test_an_error_that_cannot_be_scanned_becomes_a_fixed_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_text: str) -> str:
            raise RuntimeError("scanner unavailable")

        monkeypatch.setattr(mcp_server, "redact", _boom)
        assert mcp_server._redact_error("secret detail") == "withheld: redaction unavailable"

    def test_an_error_is_otherwise_scanned_not_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "redact", lambda text: f"[{text}]")
        assert mcp_server._redact_error("boom") == "[boom]"


class TestAudit:
    def test_a_dispatch_is_recorded_as_a_read(self, recorder: _FakeSel) -> None:
        mcp_server._audit("get_status", outcome="success", request_id=7)
        assert recorder.calls[0]["tool_name"] == "get_status"
        assert recorder.calls[0]["tool_kind"] == "read"
        assert recorder.calls[0]["outcome"] == "success"
        assert recorder.calls[0]["request_id"] == "7"

    def test_a_missing_request_id_becomes_an_empty_string_not_the_word_none(
        self, recorder: _FakeSel
    ) -> None:
        mcp_server._audit("get_status", outcome="success", request_id=None)
        assert recorder.calls[0]["request_id"] == ""

    def test_a_non_critical_sink_failure_degrades_to_stderr_never_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stdout is the JSON-RPC channel; a stray line there corrupts the protocol."""
        monkeypatch.setattr(mcp_server, "sel", lambda: _FakeSel(fail=True))
        mcp_server._audit("get_status", outcome="success")
        captured = capsys.readouterr()
        assert "SEL audit failed" in captured.err
        assert captured.out == ""

    def test_a_critical_sink_failure_is_re_raised_for_the_caller_to_refuse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "sel", lambda: _FakeSel(fail=True))
        with pytest.raises(OSError):
            mcp_server._audit("get_status", outcome="invoked", critical=True)


class TestTools:
    def test_get_status_reports_the_supervisor_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Sup:
            def status(self) -> dict[str, Any]:
                return {"active": True, "cycles": 3}

        monkeypatch.setattr(mcp_server.runner, "get_supervisor", lambda: _Sup())
        assert mcp_server._tool_get_status({}) == {"active": True, "cycles": 3}

    def test_get_ruler_pairs_the_ruler_with_its_trust_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_ruler", lambda: {"id": "zzz-ruler"})
        monkeypatch.setattr(mcp_server.progress, "ruler_calibrated", lambda: False)
        assert mcp_server._tool_get_ruler({}) == {
            "ruler": {"id": "zzz-ruler"},
            "calibrated": False,
        }

    def test_get_progress_and_get_deps_delegate_to_the_shared_modules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_progress", lambda: {"points": []})
        monkeypatch.setattr(mcp_server.deps, "check_deps", lambda: {"ok": True})
        assert mcp_server._tool_get_progress({}) == {"points": []}
        assert mcp_server._tool_get_deps({}) == {"ok": True}


class TestListFindings:
    @pytest.fixture(autouse=True)
    def _findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"fp": f"fp{i}", "status": "open" if i % 2 else "filed"} for i in range(6)]
        monkeypatch.setattr(mcp_server.progress, "read_findings", lambda: list(rows))

    def test_unfiltered_returns_everything_with_a_total(self) -> None:
        out = mcp_server._tool_list_findings({})
        assert out["total"] == 6
        assert len(out["findings"]) == 6

    def test_status_filters_to_one_status_but_total_counts_the_filtered_rows(self) -> None:
        out = mcp_server._tool_list_findings({"status": "open"})
        assert {f["status"] for f in out["findings"]} == {"open"}
        assert out["total"] == 3

    def test_a_blank_status_is_not_a_filter(self) -> None:
        assert mcp_server._tool_list_findings({"status": "   "})["total"] == 6

    def test_limit_narrows_the_page(self) -> None:
        assert len(mcp_server._tool_list_findings({"limit": 2})["findings"]) == 2

    @pytest.mark.parametrize("bad", ["not-a-number", None, [3]])
    def test_an_unparseable_limit_falls_back_to_the_default(self, bad: object) -> None:
        assert len(mcp_server._tool_list_findings({"limit": bad})["findings"]) == 6

    def test_a_zero_or_negative_limit_is_clamped_to_at_least_one(self) -> None:
        assert len(mcp_server._tool_list_findings({"limit": 0})["findings"]) == 1
        assert len(mcp_server._tool_list_findings({"limit": -5})["findings"]) == 1

    def test_the_limit_is_capped_so_a_caller_cannot_pull_everything(self) -> None:
        """The cap is 200; asserting the clamp arithmetic, not the row count."""
        out = mcp_server._tool_list_findings({"limit": 10_000})
        assert len(out["findings"]) == 6


class TestGetFinding:
    def test_a_missing_fp_is_a_client_error(self) -> None:
        with pytest.raises(ValueError, match="fp is required"):
            mcp_server._tool_get_finding({})

    def test_an_unknown_fp_is_a_client_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_findings", lambda: [])
        with pytest.raises(ValueError, match="no finding with fingerprint"):
            mcp_server._tool_get_finding({"fp": "zz1"})

    def test_the_newest_row_supplies_the_detail_and_history_reads_oldest_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mcp_server.progress,
            "read_findings",
            lambda: [
                {"fp": "zz1", "status": "filed", "kind": "bug", "target": "m.py", "pr": "9"},
                {"fp": "zz1", "status": "open"},
                {"fp": "other", "status": "open"},
            ],
        )
        out = mcp_server._tool_get_finding({"fp": "  zz1  "})
        assert out["status"] == "filed"
        assert out["kind"] == "bug"
        assert out["target"] == "m.py"
        assert out["pr"] == "9"
        assert out["history"] == ["open", "filed"]
        assert "diff" not in out

    def test_absent_fields_render_as_empty_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_findings", lambda: [{"fp": "zz1"}])
        out = mcp_server._tool_get_finding({"fp": "zz1"})
        assert out["note"] == ""
        assert out["pr"] == ""


class TestSchema:
    def test_only_get_finding_declares_a_required_argument(self) -> None:
        assert mcp_server._input_schema("get_finding")["required"] == ["fp"]
        assert mcp_server._input_schema("get_status")["required"] == []

    def test_the_advertised_schema_is_the_one_the_validator_uses(self) -> None:
        """Factored out precisely so ``tools/list`` and the validator cannot disagree."""
        listed = {t["name"]: t["inputSchema"] for t in mcp_server._schema()}
        assert set(listed) == set(mcp_server.TOOLS)
        for name, schema in listed.items():
            assert schema == mcp_server._input_schema(name)

    def test_every_tool_is_described(self) -> None:
        assert all(t["description"] for t in mcp_server._schema())


class TestHandleProtocol:
    def test_initialize_echoes_the_protocol_version_and_tool_capability(self) -> None:
        reply = mcp_server.handle({"id": 1, "method": "initialize"})
        assert reply is not None
        assert reply["result"]["protocolVersion"] == mcp_server._PROTOCOL_VERSION
        assert reply["result"]["capabilities"] == {"tools": {}}
        assert reply["result"]["serverInfo"]["name"] == "auto-improvement"

    @pytest.mark.parametrize("method", ["notifications/initialized", "initialized"])
    def test_an_initialized_notification_gets_no_reply(self, method: str) -> None:
        assert mcp_server.handle({"method": method}) is None

    def test_tools_list_advertises_the_six_read_only_tools(self) -> None:
        reply = mcp_server.handle({"id": 2, "method": "tools/list"})
        assert reply is not None
        assert [t["name"] for t in reply["result"]["tools"]] == list(mcp_server.TOOLS)

    def test_an_unknown_method_is_method_not_found(self) -> None:
        reply = mcp_server.handle({"id": 3, "method": "resources/read"})
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._METHOD_NOT_FOUND
        assert "resources/read" in reply["error"]["message"]

    def test_a_missing_method_is_still_answered_rather_than_raising(self) -> None:
        reply = mcp_server.handle({"id": 4})
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._METHOD_NOT_FOUND

    def test_non_dict_params_are_treated_as_empty(self, recorder: _FakeSel) -> None:
        """``params`` is caller-supplied; a list must not crash the dispatcher."""
        reply = mcp_server.handle({"id": 5, "method": "tools/call", "params": ["nope"]})
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._METHOD_NOT_FOUND
        assert _outcomes(recorder) == ["denied"]


class TestToolsCallRejections:
    def test_an_unknown_tool_name_is_denied_and_audited(self, recorder: _FakeSel) -> None:
        reply = _call("not_a_tool")
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._METHOD_NOT_FOUND
        assert "not_a_tool" in reply["error"]["message"]
        assert recorder.calls[0]["outcome"] == "denied"
        assert recorder.calls[0]["error"] == "unknown_tool"

    @pytest.mark.parametrize("bad", [[1, 2], "a-string", 7])
    def test_present_but_non_dict_arguments_are_refused_not_coerced(
        self, recorder: _FakeSel, bad: Any
    ) -> None:
        reply = _call("get_status", arguments=bad)
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._INVALID_PARAMS
        assert "expected an object" in reply["error"]["message"]
        assert recorder.calls[0]["error"] == "invalid_arguments"

    def test_absent_arguments_stay_valid_for_a_no_argument_tool(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_progress", lambda: {"points": [1]})
        assert _payload(_call("get_progress")) == {"points": [1]}
        assert _outcomes(recorder) == ["invoked", "success"]

    def test_a_schema_violation_is_refused_before_the_handler_runs(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _must_not_run(_args: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("handler reached despite an invalid argument type")

        monkeypatch.setattr(mcp_server.progress, "read_findings", _must_not_run)
        reply = _call("get_finding", arguments={"fp": 123})
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._INVALID_PARAMS
        assert "invalid arguments" in reply["error"]["message"]
        assert _outcomes(recorder) == ["denied"]

    def test_an_unknown_argument_key_is_refused_rather_than_ignored(
        self, recorder: _FakeSel
    ) -> None:
        reply = _call("get_status", arguments={"surprise": 1})
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._INVALID_PARAMS
        assert _outcomes(recorder) == ["denied"]

    def test_a_call_that_cannot_be_audited_is_refused_not_served(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Audit-or-deny: the invoked event is critical, so a dead SEL blocks dispatch."""
        monkeypatch.setattr(mcp_server, "sel", lambda: _FakeSel(fail=True))

        def _must_not_run() -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("handler reached despite an unwritable audit log")

        monkeypatch.setattr(mcp_server.progress, "read_progress", _must_not_run)
        reply = _call("get_progress")
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._INTERNAL_ERROR
        assert "audit log is unavailable" in reply["error"]["message"]
        captured = capsys.readouterr()
        assert "refusing a tool call" in captured.err
        assert captured.out == ""


class TestToolsCallDispatch:
    def test_a_successful_call_audits_invoked_then_success(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_findings", lambda: [{"fp": "zz1"}])
        assert _payload(_call("list_findings", arguments={}))["total"] == 1
        assert _outcomes(recorder) == ["invoked", "success"]
        assert {c["tool_name"] for c in recorder.calls} == {"list_findings"}

    def test_a_value_error_from_a_handler_becomes_invalid_params(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_findings", lambda: [])
        reply = _call("get_finding", arguments={"fp": "zz9"})
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._INVALID_PARAMS
        assert "no finding with fingerprint" in reply["error"]["message"]
        assert _outcomes(recorder) == ["invoked", "error"]
        assert recorder.calls[-1]["error"].startswith("invalid_params:")

    def test_any_other_handler_exception_becomes_an_internal_error_with_its_type(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> dict[str, Any]:
            raise KeyError("missing-artifact")

        monkeypatch.setattr(mcp_server.progress, "read_progress", _boom)
        reply = _call("get_progress")
        assert reply is not None
        assert reply["error"]["code"] == mcp_server._INTERNAL_ERROR
        assert reply["error"]["message"].startswith("KeyError:")
        assert _outcomes(recorder) == ["invoked", "error"]

    def test_a_result_is_scanned_before_it_is_truncated(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Truncating first could split a credential across the cut."""
        scanned: list[int] = []

        def _redact(text: str) -> str:
            scanned.append(len(text))
            return text

        monkeypatch.setattr(mcp_server, "redact", _redact)
        monkeypatch.setattr(
            mcp_server.progress,
            "read_progress",
            lambda: {"blob": "q" * (mcp_server._MAX_RESULT_CHARS + 500)},
        )
        reply = _call("get_progress")
        assert reply is not None
        text = reply["result"]["content"][0]["text"]
        assert len(text) == mcp_server._MAX_RESULT_CHARS
        # The scanner saw the WHOLE serialized payload, not the already-cut prefix.
        assert max(scanned) > mcp_server._MAX_RESULT_CHARS

    def test_a_non_serializable_payload_is_stringified_rather_than_crashing(
        self, recorder: _FakeSel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server.progress, "read_progress", lambda: {"when": object()})
        reply = _call("get_progress")
        assert reply is not None
        assert "object object at" in reply["result"]["content"][0]["text"]


class TestMainLoop:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
    ) -> list[dict[str, Any]]:
        monkeypatch.setattr(mcp_server.sys, "stdin", io.StringIO(stdin))
        mcp_server.main()
        out = capsys.readouterr().out
        return [json.loads(line) for line in out.splitlines() if line]

    def test_one_request_per_line_gets_one_reply_per_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies = self._run(
            monkeypatch,
            capsys,
            json.dumps({"id": 1, "method": "initialize"})
            + "\n"
            + json.dumps({"id": 2, "method": "tools/list"})
            + "\n",
        )
        assert [r["id"] for r in replies] == [1, 2]

    def test_blank_malformed_and_non_object_lines_are_skipped_without_ending_the_session(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dying here would surface as the whole MCP server disappearing mid-session."""
        replies = self._run(
            monkeypatch,
            capsys,
            "\n   \n{not json\n[1, 2]\n" + json.dumps({"id": 9, "method": "tools/list"}) + "\n",
        )
        assert [r["id"] for r in replies] == [9]

    def test_a_notification_produces_no_line_on_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies = self._run(
            monkeypatch, capsys, json.dumps({"method": "notifications/initialized"}) + "\n"
        )
        assert replies == []
