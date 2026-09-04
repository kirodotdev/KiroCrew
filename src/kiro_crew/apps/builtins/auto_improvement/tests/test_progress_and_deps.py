"""Progress series, dependency preflight, and the stdio MCP server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import deps, mcp_server, progress, store


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Pin workspace_dir == data_dir so flat test paths and the per-repo layout
    # coincide (see the note in test_finding_detail's fixture).
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(store, "workspace_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store.ensure_layout()
    return tmp_path / "data"


def _archive(root: Path, rows: list[dict]) -> None:
    (root / "results").mkdir(parents=True, exist_ok=True)
    (root / "results" / "candidates.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def _ruler(root: Path, payload: dict) -> None:
    (root / "ruler").mkdir(parents=True, exist_ok=True)
    (root / "ruler" / "ruler.json").write_text(json.dumps(payload), encoding="utf-8")


class TestProgressSeries:
    def test_empty_run_is_an_empty_series_not_an_error(self, data_home: Path) -> None:
        out = progress.read_progress()
        assert out["points"] == []
        assert out["primary"]["direction"] == "minimize"

    def test_best_advances_only_on_an_improving_keep(self, data_home: Path) -> None:
        """The staircase must not step on a rejected candidate, and must not step
        the wrong way on a keep that did not actually improve the metric."""
        _ruler(data_home, {"status": "calibrated", "anchors": [{"name": "base", "value": 100.0}]})
        _archive(
            data_home,
            [
                {"cycle": 1, "cand_id": "a", "status": "kept", "primary_delta": "-10"},
                {"cycle": 2, "cand_id": "b", "status": "discarded_noise", "primary_delta": "-40"},
                {"cycle": 3, "cand_id": "c", "status": "kept", "primary_delta": "+5"},
            ],
        )
        pts = progress.read_progress()["points"]
        assert [p["bestSoFar"] for p in pts] == [90.0, 90.0, 90.0]
        assert [p["kept"] for p in pts] == [True, False, True]

    def test_maximized_metric_steps_upward(self, data_home: Path) -> None:
        _ruler(
            data_home,
            {
                "status": "calibrated",
                "primary": {"direction": "maximize", "name": "throughput"},
                "anchors": [{"name": "base", "value": 10.0}],
            },
        )
        _archive(data_home, [{"cycle": 1, "cand_id": "a", "status": "kept", "primary_delta": "+3"}])
        assert progress.read_progress()["points"][0]["bestSoFar"] == 13.0

    def test_garbled_delta_is_missing_not_a_crash(self, data_home: Path) -> None:
        """A background writer can flush a partial row; one bad cell must not take
        out the whole endpoint."""
        _archive(
            data_home,
            [
                {"cycle": 1, "cand_id": "a", "status": "kept", "primary_delta": "n/a"},
                {"cycle": 2, "cand_id": "b", "status": "kept", "primary_delta": ""},
            ],
        )
        pts = progress.read_progress()["points"]
        assert len(pts) == 2
        assert all(p["deltaVsBest"] is None for p in pts)

    def test_corrupt_tail_line_does_not_hide_earlier_rows(self, data_home: Path) -> None:
        (data_home / "results").mkdir(parents=True, exist_ok=True)
        (data_home / "results" / "candidates.jsonl").write_text(
            json.dumps({"cycle": 1, "cand_id": "a", "status": "kept"}) + "\n{ truncated",
            encoding="utf-8",
        )
        assert len(progress.read_progress()["points"]) == 1

    def test_pr_is_joined_by_target_slug(self, data_home: Path) -> None:
        """The archive keys on a candidate id and the ledger on a content
        fingerprint — they share no id, so the join goes through the target slug.
        Joining on fingerprint alone silently produced an empty link every time."""
        (data_home / "ledger.jsonl").write_text(
            json.dumps(
                {
                    "fp": "abc",
                    "status": "filed",
                    "target": "src/search.py::negamax_root",
                    "cr": "https://github.com/o/r/pull/9",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _archive(
            data_home,
            [
                {
                    "cycle": 1,
                    "cand_id": "c1_wide_search_py_negamax_root_deadbeef",
                    "diff_ref": "c1_wide_search_py_negamax_root_deadbeef.diff",
                    "status": "kept",
                }
            ],
        )
        assert progress.read_progress()["points"][0]["pr"] == "https://github.com/o/r/pull/9"

    def test_findings_expose_cr_as_pr(self, data_home: Path) -> None:
        (data_home / "ledger.jsonl").write_text(
            json.dumps({"fp": "x", "status": "filed", "cr": "https://github.com/o/r/pull/2"})
            + "\n",
            encoding="utf-8",
        )
        assert progress.read_findings()[0]["pr"] == "https://github.com/o/r/pull/2"

    def test_ruler_calibration_gate(self, data_home: Path) -> None:
        assert progress.ruler_calibrated() is False
        _ruler(data_home, {"status": "calibrated"})
        assert progress.ruler_calibrated() is True


class TestDeps:
    def test_reports_git_and_gh_as_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "_which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(deps, "_gh_authenticated", lambda: (True, "authenticated"))
        out = deps.check_deps()
        required = {d["id"] for d in out["deps"] if d["required"]}
        assert required == {"git", "gh"}
        assert out["ok"] is True and out["blocking"] == []

    def test_unauthenticated_gh_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Presence on PATH is not enough: an unauthenticated gh fails only when a
        PR is drafted, which is the worst moment to discover it."""
        monkeypatch.setattr(deps, "_which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(deps, "_gh_authenticated", lambda: (False, "not logged in"))
        out = deps.check_deps()
        assert out["ok"] is False and "gh" in out["blocking"]

    def test_missing_ruff_is_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "_which", lambda b: "" if b == "ruff" else f"/usr/bin/{b}")
        monkeypatch.setattr(deps, "_gh_authenticated", lambda: (True, "ok"))
        out = deps.check_deps()
        assert out["ok"] is True
        assert [d for d in out["deps"] if d["id"] == "ruff"][0]["ok"] is False

    def test_install_is_a_noop_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "_which", lambda _b: "/usr/bin/ruff")
        assert deps.install_deps()["ok"] is True


class TestMcpServer:
    def test_initialize_and_tools_list(self) -> None:
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init is not None and init["result"]["serverInfo"]["name"] == "auto-improvement"
        listed = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listed is not None
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == set(mcp_server.TOOLS)

    def test_every_tool_is_read_only(self) -> None:
        """An agent inspecting a run must not be able to start, stop, or re-file
        anything — those are the operator's calls and the loop's own gates. This is
        also what makes auto-approving them safe."""
        forbidden = ("start", "stop", "run", "draft", "purge", "forget", "calibrate", "install")
        assert not [n for n in mcp_server.TOOLS if n.startswith(forbidden)]

    def test_notification_gets_no_reply(self) -> None:
        assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_unknown_method_and_tool_are_errors(self) -> None:
        bad_method = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        assert (
            bad_method is not None and bad_method["error"]["code"] == mcp_server._METHOD_NOT_FOUND
        )
        bad_tool = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}}
        )
        assert bad_tool is not None and bad_tool["error"]["code"] == mcp_server._METHOD_NOT_FOUND

    def test_every_tools_call_is_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both the served and the REJECTED dispatch reach the Security Event Log.

        The rejected one matters most: a sweep of names that are not tools is what
        probing looks like, so dropping it would leave the interesting case unlogged.
        """
        seen: list[dict] = []
        monkeypatch.setattr(
            mcp_server, "_audit", lambda name, **kw: seen.append({"tool": name, **kw})
        )
        mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_status"}}
        )
        mcp_server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}}
        )
        assert [e["tool"] for e in seen] == ["get_status", "get_status", "nope"]
        # The served call audits TWICE: the invocation before the handler runs, then the
        # outcome. The pre-dispatch event is what survives a handler that dies hard, so
        # an invocation can never be absent from the log just because the call did not
        # come back. Asserted as an ordered pair so a future refactor cannot drop it.
        assert [e["outcome"] for e in seen[:2]] == ["invoked", "success"]
        assert seen[2]["outcome"] == "denied" and seen[2]["error"] == "unknown_tool"

    def test_the_invocation_is_logged_even_when_the_handler_never_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auditing only the OUTCOME loses the record when the handler dies hard.

        The outcome events fire from `except` blocks, so anything that does not surface
        as a catchable exception here — a killed process, an interpreter-level failure —
        would execute the tool with no audit trail. `BaseException` stands in for that
        class of death: it escapes the handler's `except Exception`, yet the invocation
        must already be on the record. Raised by the GPT review of this branch.
        """
        seen: list[dict] = []
        monkeypatch.setattr(
            mcp_server, "_audit", lambda name, **kw: seen.append({"tool": name, **kw})
        )
        entry = mcp_server.TOOLS["get_status"]

        def _die(_args):
            raise KeyboardInterrupt("killed mid-call")

        monkeypatch.setitem(mcp_server.TOOLS, "get_status", (_die, *entry[1:]))
        with pytest.raises(KeyboardInterrupt):
            mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_status"},
                }
            )
        assert [e["outcome"] for e in seen] == ["invoked"], "the invocation went unaudited"

    def test_an_unauditable_call_is_refused_not_served(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """AUDIT-OR-DENY: a call that cannot be recorded must not be served.

        This REPLACES an earlier test that asserted the opposite ("a broken audit sink must
        not take out `get_status`"), on the reasoning that these six tools are pure reads so
        gating them traded capability for no security gain. That weighed blast radius, but
        the criterion `sel` states is ATTENDEDNESS: "pass `critical=True` when the caller
        enforces audit-or-deny (e.g. an unattended heartbeat auto-approve)". No human is in
        this loop and results go to an LLM, so this event is the only evidence a read
        happened — and the review that kept pressing on it was right.

        The note still goes to STDERR because stdout is the JSON-RPC channel.
        """

        def _boom() -> None:
            raise OSError("audit sink is unwritable")

        # Patch the name in the CONSUMING module: `sel` is imported at module scope there,
        # so patching `kiro_crew.sel` would leave the already-bound reference in place and
        # this test would silently stop exercising the broken-sink path.
        monkeypatch.setattr(mcp_server, "sel", _boom)
        out = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_status"}}
        )
        assert out is not None, "a tools/call must always answer something"
        assert "result" not in out, "an unauditable read was served anyway"
        assert out["error"]["code"] == mcp_server._INTERNAL_ERROR
        # The refusal message must not leak the sink's internals to the caller.
        assert "audit log is unavailable" in out["error"]["message"]
        assert "unwritable" not in out["error"]["message"]
        # The operator still gets the cause, on stderr (stdout is the JSON-RPC channel).
        # The line is a FIXED string — the CodeQL fix removed the tool name / exception
        # interpolation — so assert on the wording it actually ships with.
        assert "audit log is unavailable" in capsys.readouterr().err

    def test_missing_required_arg_is_invalid_params(self) -> None:
        out = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_finding", "arguments": {}},
            }
        )
        assert out is not None and out["error"]["code"] == mcp_server._INVALID_PARAMS

    def test_tool_result_is_capped(self, data_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tool result must not be able to blow the agent's context window."""
        monkeypatch.setattr(progress, "read_progress", lambda: {"points": [{"d": "x" * 200_000}]})
        out = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "get_progress"}}
        )
        assert out is not None
        assert len(out["result"]["content"][0]["text"]) <= mcp_server._MAX_RESULT_CHARS


class TestWorkspaceScoping:
    """Findings/PRs belong to a repository+branch: switching either shows a
    different set, not a mixed pile. These pin that the paths actually diverge —
    the fixture pins workspace_dir==data_dir elsewhere, so this test does NOT use
    that fixture and drives the real derivation from config."""

    def test_repo_and_branch_produce_distinct_ledgers(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        def _cfg(**kw):
            store.write_json_atomic(store.config_path(), kw)

        _cfg(target_display="Zedmor/chess_test", branch="origin/main")
        chess_main = store.ledger_path()
        _cfg(target_display="Zedmor/chess_test", branch="origin/dev")
        chess_dev = store.ledger_path()
        _cfg(target_display="kirodotdev/KiroCrew", branch="origin/main")
        kc_main = store.ledger_path()

        # All three differ, and each lives under a per-repo subtree.
        assert len({chess_main, chess_dev, kc_main}) == 3
        assert all("repos" in str(p) for p in (chess_main, chess_dev, kc_main))
        # origin/ prefix does not create a separate workspace from a bare name.
        _cfg(target_display="Zedmor/chess_test", branch="main")
        assert store.ledger_path() == chess_main

    def test_config_and_sessions_are_shared_not_scoped(self, tmp_path, monkeypatch) -> None:
        """config names the active repo, and a chat session may reference any
        repo — so both stay at the data root, not inside a per-repo subtree."""
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        store.write_json_atomic(store.config_path(), {"target_display": "a/b", "branch": "main"})
        assert store.config_path().parent == store.data_dir()
        assert store.sessions_dir().parent == store.data_dir()
        assert "repos" not in str(store.sessions_dir())


class TestMcpResultsAreRedacted:
    """MCP tool results are agent-authored run evidence handed to an LLM.

    A finding's `note`/`signature`/`hypothesis` are the model's own prose, so a credential
    quoted during discovery lands in the ledger and comes back out through `list_findings`.
    Measured before fixing: `AKIAIOSFODNN7EXAMPLE` in a ledger note came back verbatim in
    the tool result. This was the last unredacted reader — the browser
    (`routes._redact_for_display`) and the activity feed (`runner._redact_activity`) already
    scanned the same class of text. Raised by review of this branch.
    """

    def test_a_credential_in_a_ledger_note_does_not_reach_the_result(
        self, data_home, monkeypatch
    ) -> None:
        import json

        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server, store

        path = store.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fp": "abc123",
                    "kind": "bug",
                    "target": "m.py::f",
                    "status": "filed",
                    "cr": "https://github.com/o/r/pull/1",
                    "note": "aws_access_key_id=AKIAIOSFODNN7EXAMPLE",
                    "ts": 1.0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        out = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_findings", "arguments": {}},
            }
        )
        text = (out or {}).get("result", {}).get("content", [{}])[0].get("text", "")
        assert "AKIAIOSFODNN7EXAMPLE" not in text, "a credential reached the LLM"
        # The tool must still be USEFUL — redaction that empties the result is not a fix.
        assert "abc123" in text

    def test_redaction_runs_before_truncation(self) -> None:
        """Truncating first could split a credential across the cut and leave a fragment
        the scanner no longer matches."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server

        src = inspect.getsource(mcp_server.handle)
        assert "_redact_result(json.dumps(" in src, "redaction must wrap the serialization"
        line = next(ln for ln in src.splitlines() if "_redact_result(json.dumps(" in ln)
        assert line.index("_redact_result") < line.index("_MAX_RESULT_CHARS")

    def test_a_failed_redaction_withholds_the_result(self, monkeypatch) -> None:
        """Fail-closed: these tools are conveniences, so withholding beats leaking."""
        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server

        def _boom(_text):
            raise RuntimeError("scanner unavailable")

        # As above: patch where `redact` is BOUND (this module), not where it is defined.
        monkeypatch.setattr(mcp_server, "redact", _boom)
        out = mcp_server._redact_result('{"note": "aws_access_key_id=AKIAIOSFODNN7EXAMPLE"}')
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "withheld" in out


class TestMcpArgumentsAreValidated:
    """`tools/call` arguments came straight from the wire: a non-dict was coerced to `{}`
    and whatever survived went to the handler, so a malformed call could return `result`
    instead of `INVALID_PARAMS` and an unknown key was silently ignored.

    Uses the SHARED `validation.validate_mcp_tool_arguments` rather than a local check —
    it is fail-closed for exactly this boundary (untrusted `tools/call`), and a local copy
    would drift from the schema `tools/list` advertises. Raised by review of this branch.
    """

    @staticmethod
    def _call(name: str, args: object) -> dict:
        return (
            mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                }
            )
            or {}
        )

    def test_unknown_keys_and_wrong_types_are_invalid_params(self, data_home: Path) -> None:
        for bad in ({"nope": 1}, {"limit": "not-an-int"}, {"status": 5}):
            out = self._call("list_findings", bad)
            assert "error" in out, f"{bad!r} was accepted"
            assert out["error"]["code"] == mcp_server._INVALID_PARAMS

    def test_a_missing_required_argument_is_invalid_params(self, data_home: Path) -> None:
        out = self._call("get_finding", {})
        assert out["error"]["code"] == mcp_server._INVALID_PARAMS

    def test_a_non_dict_arguments_payload_is_rejected_not_coerced(self, data_home: Path) -> None:
        """`"arguments": [1,2]` or a bare string must return INVALID_PARAMS. The handler
        used to coerce any non-dict to `{}`, so a malformed call SUCCEEDED with `result`
        for a no-required-arg tool instead of being refused — the schema validator only
        ever saw the emptied dict. Raised by the GPT review of this branch."""
        for bad in ([1, 2, 3], "garbage", 5, True):
            out = self._call("get_status", bad)
            assert "error" in out, f"non-dict arguments {bad!r} was accepted"
            assert out["error"]["code"] == mcp_server._INVALID_PARAMS

    def test_absent_or_empty_arguments_still_reach_a_no_arg_tool(self, data_home: Path) -> None:
        """Arguments are optional — None/absent and `{}` must still succeed, so the reject
        above is scoped to a present-but-non-dict payload, not a blanket new requirement."""
        assert "result" in self._call("get_status", None)
        assert "result" in self._call("get_status", {})

    def test_valid_arguments_still_reach_the_handler(self, data_home: Path) -> None:
        """Validation that rejects legitimate calls is not a fix."""
        store.ledger_path().write_text(
            json.dumps(
                {"fp": "abc", "kind": "bug", "target": "m.py::f", "status": "filed", "ts": 1.0}
            )
            + "\n",
            encoding="utf-8",
        )
        assert "result" in self._call("list_findings", {})
        assert "result" in self._call("list_findings", {"limit": 5})
        assert "result" in self._call("get_finding", {"fp": "abc"})

    def test_the_validated_schema_is_the_advertised_schema(self) -> None:
        """A validator checking a different shape than `tools/list` advertises is worse
        than no validator, so both read one function."""
        listed = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert listed is not None
        for tool in listed["result"]["tools"]:
            assert tool["inputSchema"] == mcp_server._input_schema(tool["name"])
