"""Coverage tests for ``kiro_crew.mcp_core`` helpers and thin tool bodies.

Focus areas (the largest previously-uncovered blocks):

* the browser-snapshot compressors (``_compress_snapshot_to_outline`` /
  ``_search_snapshot``) and the ``browse_outline`` / ``browse_search`` tools
  that wrap them,
* the loopback HTTP verb helpers (``_get`` / ``_patch`` / ``_put`` /
  ``_delete``) plus ``_http_error_body`` error decoding + redaction,
* the chat-history snippet helpers and ``_format_anchor``,
* ``_do_select_crew`` roster / unknown-crew / bound-crew paths,
* the ``spawn_*`` argument-validation and error-propagation branches,
* the whole ``workflow_*`` tool family (malformed / failed / transport
  responses included).

Every HTTP call is mocked at ``mcp_core.loopback_urlopen`` or at mcp_core's own
``_post`` / ``_get`` seams — nothing here touches the network, a real gateway,
a subprocess or a fixed port.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_core import (
    _call_tool,
    _casefold_match_span,
    _compress_snapshot_to_outline,
    _do_select_crew,
    _extract_history_snippet,
    _format_anchor,
    _history_is_incognito,
    _http_error_body,
    _parse_iso_date_epoch,
    _search_snapshot,
    _validate_args,
    _ws_bucket,
)


class _FakeResponse:
    """Minimal ``urlopen`` return value: a JSON-body context manager."""

    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://localhost:5476/api/x",
        status,
        "Bad Request",
        email.message.Message(),
        io.BytesIO(body),
    )


@pytest.fixture(autouse=True)
def _no_session_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin session-key resolution so the HTTP helpers take a deterministic path.

    ``_resolve_session_key`` walks env vars and PID files; the verb helpers only
    care whether it returns something header-safe, so freeze it.
    """
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s3cr3t")


# ── snapshot compression helpers ──────────────────────────────────────────


class TestCompressSnapshotToOutline:
    def test_empty_snapshot_is_reported_not_crashed(self):
        assert "Empty snapshot" in _compress_snapshot_to_outline("")

    def test_keeps_interactive_lines_and_drops_noise(self):
        snapshot = "\n".join(
            [
                "- generic",
                "",
                "-",
                '    - button "Save" [ref=e7]',
                '  - heading "Title"',
                "  - decorative-thing",
            ]
        )
        out = _compress_snapshot_to_outline(snapshot)
        assert "Page outline (2 elements)" in out
        assert 'button "Save" [ref=e7]' in out
        assert "decorative-thing" not in out

    def test_indent_is_compacted_and_capped(self):
        deep = " " * 40 + '- button "Deep" [ref=e1]'
        out = _compress_snapshot_to_outline(deep)
        body = out.split("\n")[1]
        # min(indent // 2, 4) levels of two spaces → 8 leading spaces max.
        assert body.startswith(" " * 8)
        assert not body.startswith(" " * 10)

    def test_truncates_at_max_lines(self):
        snapshot = "\n".join(f'- button "b{i}" [ref=e{i}]' for i in range(20))
        out = _compress_snapshot_to_outline(snapshot, max_lines=5)
        assert "... (truncated at 5 lines)" in out
        assert 'button "b19"' not in out

    def test_no_interactive_elements_reports_total_lines(self):
        out = _compress_snapshot_to_outline("- plain\n- text\n\n- words")
        assert "No interactive elements found in snapshot (3 total lines)" in out


class TestSearchSnapshot:
    def test_empty_snapshot_and_empty_query_are_distinct_errors(self):
        assert _search_snapshot("", "x") == "Empty snapshot."
        assert _search_snapshot("some page", "") == "Error: query is required"

    def test_matches_are_numbered_from_one(self):
        out = _search_snapshot("alpha\nbeta\nBETA again", "beta")
        assert "Found 2 matches" in out
        assert "L2: beta" in out
        assert "L3: BETA again" in out

    def test_invalid_regex_falls_back_to_literal_search(self):
        out = _search_snapshot("cost is 5 (approx)", "(approx")
        assert "L1: cost is 5 (approx)" in out

    def test_no_match_reports_line_count(self):
        out = _search_snapshot("a\nb", "zzz")
        assert "No matches for 'zzz' in snapshot (2 lines)." == out

    def test_max_results_caps_output(self):
        out = _search_snapshot("\n".join(["hit"] * 10), "hit", max_results=3)
        assert "Found 3 matches" in out


class TestBrowseTools:
    def test_browse_outline_tool_returns_compressed_outline(self):
        result = _call_tool(
            "browse_outline", {"snapshot": '- button "Go" [ref=e1]', "max_lines": 10}
        )
        assert "Page outline (1 elements)" in result
        assert "[ref=e1]" in result

    def test_browse_search_tool_returns_matches(self):
        result = _call_tool("browse_search", {"snapshot": "alpha\nbeta", "query": "beta"})
        assert "Found 1 matches" in result

    def test_browse_search_tool_reports_missing_query(self):
        result = _call_tool("browse_search", {"snapshot": "alpha"})
        assert result == "Error: query is required"


# ── loopback HTTP verb helpers ────────────────────────────────────────────


class TestHttpErrorBody:
    def test_structured_json_error_is_surfaced(self):
        err = _http_error(400, b'{"error": "unknown session"}')
        assert _http_error_body(err) == {"error": "unknown session"}

    def test_counted_marker_survives_flattening(self):
        err = _http_error(429, b'{"error": "at capacity", "counted": true}')
        out = _http_error_body(err)
        assert out == {"error": "at capacity", "counted": True}

    def test_non_json_body_is_used_verbatim(self):
        err = _http_error(502, b"upstream exploded")
        assert _http_error_body(err)["error"] == "upstream exploded"

    def test_json_without_error_key_falls_back_to_raw_body(self):
        err = _http_error(400, b'{"detail": "nope"}')
        assert _http_error_body(err)["error"] == '{"detail": "nope"}'

    def test_unreadable_body_falls_back_to_str_exc(self):
        err = _http_error(500, b"")

        def _boom(*_a: object) -> bytes:
            raise OSError("socket gone")

        err.read = _boom  # type: ignore[method-assign,assignment]
        assert "HTTP Error 500" in _http_error_body(err)["error"]

    def test_credentials_in_error_body_are_redacted(self):
        secret = "AKIA" + "I" * 16
        err = _http_error(400, json.dumps({"error": f"bad key {secret}"}).encode())
        assert secret not in _http_error_body(err)["error"]


@pytest.mark.parametrize("verb", ["_get", "_patch", "_put", "_delete"])
class TestVerbHelpers:
    def test_success_decodes_json_body(self, verb: str):
        fn = getattr(mcp_core, verb)
        with patch("kiro_crew.mcp_core.loopback_urlopen", return_value=_FakeResponse({"ok": True})):
            assert fn("/api/thing") == {"ok": True}

    def test_http_error_is_decoded_through_http_error_body(self, verb: str):
        fn = getattr(mcp_core, verb)
        err = _http_error(404, b'{"error": "not found"}')
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=err):
            assert fn("/api/thing") == {"error": "not found"}

    def test_generic_exception_becomes_error_dict(self, verb: str):
        fn = getattr(mcp_core, verb)
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=RuntimeError("boom")):
            assert fn("/api/thing") == {"error": "boom"}

    def test_non_latin1_session_key_short_circuits(
        self, verb: str, monkeypatch: pytest.MonkeyPatch
    ):
        fn = getattr(mcp_core, verb)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat—1")
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=AssertionError("must not be called")):
            out = fn("/api/thing")
        assert "invalid in HTTP headers" in out["error"]


class TestVerbHelperBodies:
    def test_patch_and_put_send_a_json_body(self):
        for verb, method in (("_patch", "PATCH"), ("_put", "PUT")):
            fn = getattr(mcp_core, verb)
            with patch(
                "kiro_crew.mcp_core.loopback_urlopen", return_value=_FakeResponse({"ok": True})
            ) as m:
                fn("/api/thing", {"a": 1})
            req = m.call_args[0][0]
            assert req.get_method() == method
            assert json.loads(req.data.decode()) == {"a": 1}
            assert req.headers["Content-type"] == "application/json"

    def test_delete_without_body_sends_no_content_type(self):
        with patch("kiro_crew.mcp_core.loopback_urlopen", return_value=_FakeResponse({"ok": True})) as m:
            mcp_core._delete("/api/thing")
        req = m.call_args[0][0]
        assert req.data is None
        assert "Content-type" not in req.headers

    def test_delete_with_body_sends_json(self):
        with patch("kiro_crew.mcp_core.loopback_urlopen", return_value=_FakeResponse({"ok": True})) as m:
            mcp_core._delete("/api/thing", {"rule": "x"})
        req = m.call_args[0][0]
        assert json.loads(req.data.decode()) == {"rule": "x"}


class TestPostTransportClassification:
    def test_connection_refused_is_not_a_transport_error(self):
        err = urllib.error.URLError(ConnectionRefusedError("refused"))
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=err):
            out = mcp_core._post("/api/spawn", {})
        assert "transport_error" not in out
        assert out["error"]

    def test_other_urlerror_is_flagged_transport_error(self):
        err = urllib.error.URLError(TimeoutError("timed out"))
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=err):
            out = mcp_core._post("/api/spawn", {})
        assert out["transport_error"] is True

    def test_unexpected_exception_is_flagged_transport_error(self):
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=RuntimeError("read timeout")):
            out = mcp_core._post("/api/spawn", {})
        assert out == {"error": "read timeout", "transport_error": True}

    def test_http_error_is_not_flagged_transport_error(self):
        with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=_http_error(400, b'{"error": "nope"}')):
            out = mcp_core._post("/api/spawn", {})
        assert out == {"error": "nope"}


# ── chat-history helpers ──────────────────────────────────────────────────


class TestHistoryScalarHelpers:
    def test_incognito_detection_is_case_insensitive(self):
        mode = sorted(mcp_core._HISTORY_INCOGNITO_MODES)[0]
        assert _history_is_incognito({"memory_mode": mode.upper()}) is True
        assert _history_is_incognito({"memory_mode": "persistent"}) is False
        assert _history_is_incognito({}) is False

    def test_iso_date_parses_to_utc_midnight_epoch(self):
        assert _parse_iso_date_epoch("1970-01-02") == 86400.0

    @pytest.mark.parametrize("bad", ["not-a-date", "1970-13-99", ""])
    def test_bad_iso_date_returns_none(self, bad: str):
        assert _parse_iso_date_epoch(bad) is None

    @pytest.mark.parametrize(
        "value,expected",
        [("team", "team"), ("", "default"), (None, "default"), (42, "default"), ({}, "default")],
    )
    def test_ws_bucket_normalizes_non_strings(self, value: object, expected: str):
        assert _ws_bucket(value) == expected

    def test_caller_workspace_defaults_without_session_key(self):
        assert mcp_core._caller_workspace(object(), "") == "default"

    def test_caller_workspace_reads_session_metadata(self):
        cl = SimpleNamespace(get_metadata=lambda _sk: {"workspace": "team"})
        assert mcp_core._caller_workspace(cl, "dashboard:chat-1") == "team"

    def test_caller_workspace_buckets_missing_metadata_workspace(self):
        cl = SimpleNamespace(get_metadata=lambda _sk: {})
        assert mcp_core._caller_workspace(cl, "dashboard:chat-1") == "default"


class TestCasefoldMatchSpan:
    def test_empty_needle_has_no_span(self):
        assert _casefold_match_span("abc", "") is None

    def test_missing_needle_has_no_span(self):
        assert _casefold_match_span("abc", "zzz") is None

    def test_simple_case_insensitive_span(self):
        assert _casefold_match_span("Hello World", "world") == (6, 11)

    def test_multi_char_fold_maps_back_to_source_boundaries(self):
        # "ß".casefold() == "ss": the needle is longer than the source char, so
        # the span must snap back to whole source characters.
        start, end = _casefold_match_span("straße!", "ss")  # type: ignore[misc]
        assert "straße!"[start:end] == "ß"


class TestExtractHistorySnippet:
    def test_blank_needle_returns_empty(self):
        assert _extract_history_snippet([{"role": "user", "content": "hi"}], "   ") == ""

    def test_tool_roles_are_skipped(self):
        msgs = [
            {"role": "tool", "content": "needle in a tool trace"},
            {"role": "assistant", "content": "needle in the answer"},
        ]
        out = _extract_history_snippet(msgs, "needle")
        assert "<<<needle>>>" in out
        assert "tool trace" not in out

    def test_non_string_and_empty_content_are_skipped(self):
        msgs: list[dict] = [
            {"role": "user", "content": None},
            {"role": "user", "content": ""},
            {"role": "user", "content": "found needle here"},
        ]
        assert "<<<needle>>>" in _extract_history_snippet(msgs, "needle")

    def test_no_match_returns_empty_string(self):
        assert _extract_history_snippet([{"role": "user", "content": "abc"}], "zzz") == ""

    def test_long_content_is_elided_on_both_sides(self):
        content = "x" * 500 + "needle" + "y" * 500
        out = _extract_history_snippet([{"role": "user", "content": content}], "needle")
        assert out.startswith("…")
        assert len(out) <= mcp_core._SNIPPET_MAX_LEN

    def test_hard_cap_never_leaves_a_dangling_open_marker(self):
        needle = "n" * 400
        content = "pre " + needle + " post"
        out = _extract_history_snippet([{"role": "user", "content": content}], needle)
        assert "<<<" in out and out.endswith(">>>")
        assert len(out) <= mcp_core._SNIPPET_MAX_LEN

    def test_credentials_inside_the_snippet_are_redacted(self):
        secret = "AKIA" + "J" * 16
        content = f"deploy needle with {secret} attached"
        out = _extract_history_snippet([{"role": "user", "content": content}], "needle")
        assert secret not in out


class TestFormatAnchor:
    def test_short_quote_is_shown_in_full_with_offsets(self):
        out = _format_anchor({"quote": "hello", "start_offset": 3, "end_offset": 8})
        assert out == ' [on: "hello", chars 3:8]'

    def test_quote_without_offsets_omits_offset_info(self):
        assert _format_anchor({"quote": "hello"}) == ' [on: "hello"]'

    def test_long_quote_is_bookended_with_a_truncated_marker(self):
        quote = "a" * 200 + "b" * 200
        out = _format_anchor({"quote": quote, "start_offset": 0, "end_offset": 400})
        assert "TRUNCATED: 200 chars omitted" in out
        assert "chars 0:400" in out
        assert out.count("a" * 100) == 1
        assert out.count("b" * 100) == 1


# ── select_crew ───────────────────────────────────────────────────────────


def _crew_config(agents: dict[str, Any], default: str) -> SimpleNamespace:
    return SimpleNamespace(agents=agents, default_agent=default)


class TestDoSelectCrew:
    def _patch_cfg(self, monkeypatch: pytest.MonkeyPatch, cfg: SimpleNamespace) -> None:
        monkeypatch.setattr(
            mcp_core, "KiroCrewConfig", SimpleNamespace(load=staticmethod(lambda: cfg))
        )

    def test_empty_crew_returns_roster_of_triggered_non_default_crews(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        cfg = _crew_config(
            {
                "main": SimpleNamespace(triggers="anything", model="auto"),
                "docs": SimpleNamespace(triggers="write docs", model="auto"),
                "silent": SimpleNamespace(triggers="   ", model="auto"),
            },
            "main",
        )
        self._patch_cfg(monkeypatch, cfg)
        out = json.loads(_do_select_crew(""))
        assert out["default_agent"] == "main"
        assert [c["name"] for c in out["crews"]] == ["docs"]
        assert "high confidence" in out["guidance"]

    def test_unknown_crew_returns_error_with_available_names(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        cfg = _crew_config({"main": SimpleNamespace(triggers="", model="auto")}, "main")
        self._patch_cfg(monkeypatch, cfg)
        out = json.loads(_do_select_crew("ghost"))
        assert out["error"] == "unknown crew 'ghost'"
        assert out["available"] == "main"

    def test_unknown_crew_with_no_agents_reports_none(self, monkeypatch: pytest.MonkeyPatch):
        self._patch_cfg(monkeypatch, _crew_config({}, ""))
        assert json.loads(_do_select_crew("ghost"))["available"] == "(none)"

    def test_named_crew_returns_resolved_bindings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        cfg = _crew_config({"docs": SimpleNamespace(triggers="d", model="opus")}, "main")
        self._patch_cfg(monkeypatch, cfg)
        monkeypatch.setattr(
            mcp_core,
            "resolve_agent_bindings",
            lambda _cfg, _name: SimpleNamespace(
                kiro_agent="ka", workspace_dir=tmp_path / "ws", memory_store_name="ms"
            ),
        )
        out = json.loads(_do_select_crew("docs"))
        assert out["crew"] == "docs"
        assert out["bound"] == {
            "kiro_agent": "ka",
            "workspace": str(tmp_path / "ws"),
            "memory_store": "ms",
            "model": "opus",
        }

    def test_select_crew_tool_routes_through_do_select_crew(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(mcp_core, "_do_select_crew", lambda crew: f"crew={crew!r}")
        assert _call_tool("select_crew", {"crew": "docs"}) == "crew='docs'"

    def test_select_crew_tool_defaults_to_empty_roster_request(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(mcp_core, "_do_select_crew", lambda crew: f"crew={crew!r}")
        assert _call_tool("select_crew", {}) == "crew=''"


# ── spawn_* argument validation + error propagation ───────────────────────


class TestSpawnRunArgumentHandling:
    def test_missing_task_and_tasks_is_rejected(self):
        assert _call_tool("spawn_run", {}) == "Error: task or tasks is required"

    def test_blank_entries_in_tasks_are_dropped_leaving_nothing(self):
        assert _call_tool("spawn_run", {"tasks": ["  ", ""]}) == "Error: no subagents were started."

    def test_agents_length_must_match_tasks_length(self):
        out = _call_tool("spawn_run", {"tasks": ["a", "b"], "agents": ["one"]})
        assert out == "Error: agents length (1) must match tasks length (2)"

    def test_batch_spawn_forwards_batch_identity_and_extras(self):
        with patch.object(mcp_core, "_post", return_value={"id": "ag1"}) as m:
            _call_tool(
                "spawn_run",
                {
                    "tasks": ["one", "two"],
                    "max_turns": 7,
                    "model": "claude-opus-5",
                    "keep": True,
                },
            )
        bodies = [c[0][1] for c in m.call_args_list]
        assert len(bodies) == 2
        assert bodies[0]["batch_total"] == 2
        assert bodies[0]["batch_id"] == bodies[1]["batch_id"]
        assert bodies[0]["max_turns"] == 7
        assert bodies[0]["model"] == "claude-opus-5"
        assert bodies[0]["keep"] is True

    def test_keep_spawn_advertises_continuability(self):
        with patch.object(mcp_core, "_post", return_value={"id": "ag1"}):
            out = _call_tool("spawn_run", {"task": "one", "keep": True})
        assert "GUARANTEED continuability" in out
        assert "spawn_release" in out

    def test_transport_error_is_reported_as_unknown_acceptance(self):
        err = {"error": "read timeout", "transport_error": True}
        with patch.object(mcp_core, "_post", return_value=err) as m:
            out = _call_tool("spawn_run", {"task": "one"})
        assert "acceptance status is unknown" in out
        assert "Do not retry automatically" in out
        # A transport failure must NOT be reconciled as a lost wave member.
        assert all("/api/spawn/lost" not in c[0][0] for c in m.call_args_list)

    def test_explicit_rejection_in_a_batch_is_reconciled_as_lost(self):
        with patch.object(mcp_core, "_post", return_value={"error": "at capacity"}) as m:
            out = _call_tool("spawn_run", {"tasks": ["one", "two"]})
        lost = [c for c in m.call_args_list if c[0][0] == "/api/spawn/lost"]
        assert len(lost) == 2
        assert lost[0][0][1]["batch_total"] == 2
        assert "none of the requested subagents were started" in out

    def test_counted_rejection_is_not_double_reconciled(self):
        rejected = {"error": "at capacity", "counted": True}
        with patch.object(mcp_core, "_post", return_value=rejected) as m:
            _call_tool("spawn_run", {"tasks": ["one", "two"]})
        assert all(c[0][0] != "/api/spawn/lost" for c in m.call_args_list)

    def test_lost_reconcile_delivery_failure_is_swallowed(self):
        def _post(path: str, body: dict | None = None, **_kw: object) -> dict:
            if path == "/api/spawn/lost":
                raise RuntimeError("gateway gone")
            return {"error": "at capacity"}

        with patch.object(mcp_core, "_post", side_effect=_post):
            out = _call_tool("spawn_run", {"tasks": ["one", "two"]})
        assert "failed to start" in out

    def test_partial_batch_mixes_successes_failures_and_unknowns(self):
        responses = [
            {"id": "ag1"},
            {"error": "at capacity", "counted": True},
            {"error": "read timeout", "transport_error": True},
        ]
        with patch.object(mcp_core, "_post", side_effect=responses):
            out = _call_tool("spawn_run", {"tasks": ["a", "b", "c"]})
        assert "Spawned 1 subagent(s)" in out
        assert "1 task(s) failed to start" in out
        assert "1 task(s) have unknown acceptance status" in out
        assert "END YOUR TURN NOW" in out

    def test_orphaned_spawn_warns_and_switches_to_polling_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
        with patch.object(mcp_core, "_post", return_value={"id": "ag1"}):
            out = _call_tool("spawn_run", {"task": "one", "agent": "kirocrew"})
        assert "parent_session UNRESOLVED" in out
        assert "Monitor results via polling" in out
        assert "Do NOT wait for completion events" in out
        assert "ag1 (kirocrew)" in out

    def test_errors_plus_unknowns_without_success_keeps_error_prefix(self):
        responses = [
            {"error": "at capacity", "counted": True},
            {"error": "read timeout", "transport_error": True},
        ]
        with patch.object(mcp_core, "_post", side_effect=responses):
            out = _call_tool("spawn_run", {"tasks": ["a", "b"]})
        assert out.startswith("Error: 1 task(s) failed to start")

    def test_approval_mode_env_is_forwarded(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIROCREW_APPROVAL_MODE", "auto")
        with patch.object(mcp_core, "_post", return_value={"id": "ag1"}) as m:
            _call_tool("spawn_run", {"task": "one"})
        assert m.call_args[0][1]["approval_mode"] == "auto"


class TestSpawnLifecycleTools:
    def test_continue_forwards_optional_overrides(self):
        with patch.object(mcp_core, "_post", return_value={"id": "run9"}) as m:
            out = _call_tool(
                "spawn_continue",
                {
                    "conversation": "conv1",
                    "task": "keep going",
                    "agent": "kirocrew",
                    "model": "claude-opus-5",
                    "max_turns": 3,
                },
            )
        path, body = m.call_args[0][0], m.call_args[0][1]
        assert path == "/api/spawn/conv1/continue"
        assert body["agent"] == "kirocrew"
        assert body["model"] == "claude-opus-5"
        assert body["max_turns"] == 3
        assert "run9" in out and "END YOUR TURN" in out

    def test_continue_propagates_backend_error(self):
        with patch.object(mcp_core, "_post", return_value={"error": "conversation_gone"}):
            out = _call_tool("spawn_continue", {"conversation": "c", "task": "t"})
        assert out == "Error: conversation_gone"

    def test_steer_posts_the_message_and_confirms(self):
        with patch.object(mcp_core, "_post", return_value={"ok": True}) as m:
            out = _call_tool("spawn_steer", {"agent_id": "a1", "message": "stop that"})
        assert m.call_args[0][0] == "/api/spawn/a1/steer"
        # mode defaults to "interrupt" (spawn_steer's original semantics);
        # "follow_up" queues for delivery after the run's turn completes.
        assert m.call_args[0][1] == {"message": "stop that", "mode": "interrupt"}
        assert "Steered run a1" in out

    def test_steer_propagates_backend_error(self):
        with patch.object(mcp_core, "_post", return_value={"error": "session_starting"}):
            out = _call_tool("spawn_steer", {"agent_id": "a1", "message": "m"})
        assert out == "Error: session_starting"

    def test_release_confirms_and_propagates_error(self):
        with patch.object(mcp_core, "_post", return_value={"ok": True}) as m:
            out = _call_tool("spawn_release", {"conversation": "conv1"})
        assert m.call_args[0][0] == "/api/spawn/conv1/release"
        assert "no longer be continued" in out

        with patch.object(mcp_core, "_post", return_value={"error": "not_found"}):
            assert _call_tool("spawn_release", {"conversation": "conv1"}) == "Error: not_found"


class TestSpawnSubAgentsValidation:
    def test_empty_agents_array_is_rejected_by_the_schema(self):
        # The schema marks ``agents`` required, so an empty array never reaches
        # the tool body — the validation error is the user-visible outcome.
        assert "agents" in _call_tool("spawn_sub_agents", {"agents": []})


# ── workflow_* family ─────────────────────────────────────────────────────


class TestWorkflowAuthor:
    def test_transport_error_is_surfaced(self):
        with patch.object(mcp_core, "_post", return_value={"error": "refused"}):
            out = _call_tool("workflow_author", {"intent": "do a thing"})
        assert out == "workflow_author failed: refused"

    def test_not_ok_response_lists_validation_errors(self):
        resp = {"ok": False, "errors": ["bad ctx call", "syntax error"]}
        with patch.object(mcp_core, "_post", return_value=resp):
            out = _call_tool("workflow_author", {"intent": "do a thing"})
        assert out == "Could not author a valid workflow: bad ctx call; syntax error"

    def test_malformed_response_without_ok_or_errors_still_reports(self):
        with patch.object(mcp_core, "_post", return_value={}):
            out = _call_tool("workflow_author", {"intent": "do a thing"})
        assert out == "Could not author a valid workflow: "

    def test_authored_source_is_returned(self):
        with patch.object(mcp_core, "_post", return_value={"ok": True, "source": "ctx.agent()"}):
            out = _call_tool("workflow_author", {"intent": "do a thing"})
        assert "Authored workflow" in out
        assert "ctx.agent()" in out

    def test_authored_source_is_redacted(self):
        secret = "AKIA" + "K" * 16
        resp = {"ok": True, "source": f"token = '{secret}'"}
        with patch.object(mcp_core, "_post", return_value=resp):
            out = _call_tool("workflow_author", {"intent": "do a thing"})
        assert secret not in out


class TestWorkflowRun:
    def test_neither_source_nor_intent_is_rejected(self):
        out = _call_tool("workflow_run", {})
        assert out == "Error: provide either 'source' or 'intent'"

    def test_intent_only_takes_the_author_in_run_path(self):
        with patch.object(mcp_core, "_post", return_value={"run_id": "r1"}) as m:
            out = _call_tool(
                "workflow_run",
                {"intent": "ship it", "name": "shipper", "args": {"k": 1}, "budget_total": 5},
            )
        path, body = m.call_args[0][0], m.call_args[0][1]
        assert path == "/api/workflows/run_intent"
        assert body == {"name": "shipper", "args": {"k": 1}, "budget_total": 5, "intent": "ship it"}
        assert "Started workflow run `r1`" in out
        assert "authoring the workflow" in out

    def test_intent_path_propagates_error(self):
        with patch.object(mcp_core, "_post", return_value={"error": "no model"}):
            out = _call_tool("workflow_run", {"intent": "ship it"})
        assert out == "workflow_run failed: no model"

    def test_source_path_posts_to_run_and_reports_name(self):
        resp = {"run_id": "r2", "name": "nightly"}
        with patch.object(mcp_core, "_post", return_value=resp) as m:
            out = _call_tool("workflow_run", {"source": "ctx.agent('x')"})
        assert m.call_args[0][0] == "/api/workflows/run"
        assert m.call_args[0][1]["source"] == "ctx.agent('x')"
        assert "`r2`" in out and "nightly" in out

    def test_source_path_falls_back_to_em_dash_name(self):
        with patch.object(mcp_core, "_post", return_value={"run_id": "r3"}):
            out = _call_tool("workflow_run", {"source": "ctx.agent('x')"})
        assert "(name: —)" in out

    def test_source_path_propagates_error(self):
        with patch.object(mcp_core, "_post", return_value={"error": "sandbox denied"}):
            out = _call_tool("workflow_run", {"source": "ctx.agent('x')"})
        assert out == "workflow_run failed: sandbox denied"

    def test_source_wins_over_intent(self):
        with patch.object(mcp_core, "_post", return_value={"run_id": "r4"}) as m:
            _call_tool("workflow_run", {"source": "ctx.agent('x')", "intent": "ignored"})
        assert m.call_args[0][0] == "/api/workflows/run"
        assert "intent" not in m.call_args[0][1]

    def test_non_int_budget_total_is_rejected_by_the_schema(self):
        out = _call_tool("workflow_run", {"source": "ctx.agent('x')", "budget_total": "lots"})
        assert "budget_total" in out


class TestWorkflowStatusAndResult:
    def test_bare_transport_error_bails_early(self):
        with patch.object(mcp_core, "_get", return_value={"error": "refused"}):
            assert _call_tool("workflow_status", {"run_id": "r1"}) == "workflow_status: refused"
            assert _call_tool("workflow_result", {"run_id": "r1"}) == "workflow_result: refused"

    def test_failed_run_still_reports_its_own_error(self):
        snap = {"run_id": "r1", "status": "failed", "error": "step 2 blew up", "event_count": 4}
        with patch.object(mcp_core, "_get", return_value=snap):
            out = _call_tool("workflow_status", {"run_id": "r1"})
        assert "**failed**" in out
        assert "4 events" in out
        assert "error: step 2 blew up" in out

    def test_status_without_error_omits_the_error_clause(self):
        snap = {"run_id": "r1", "status": "running", "name": "nightly"}
        with patch.object(mcp_core, "_get", return_value=snap):
            out = _call_tool("workflow_status", {"run_id": "r1"})
        assert "nightly" in out
        assert "0 events" in out
        assert "error:" not in out

    def test_status_missing_name_uses_em_dash(self):
        with patch.object(mcp_core, "_get", return_value={"run_id": "r1", "status": "queued"}):
            assert "(—)" in _call_tool("workflow_status", {"run_id": "r1"})

    def test_result_projects_partials_and_agent_errors(self):
        snap = {
            "run_id": "r1",
            "status": "failed",
            "result": None,
            "error": "no return value",
            "events": [{"phase": "a"}],
            "partial_results": {"agent1": "half done"},
            "agent_errors": {"agent2": "timed out"},
        }
        with patch.object(mcp_core, "_get", return_value=snap):
            payload = json.loads(_call_tool("workflow_result", {"run_id": "r1"}))
        assert payload["partial_results"] == {"agent1": "half done"}
        assert payload["agent_errors"] == {"agent2": "timed out"}
        assert payload["events"] == [{"phase": "a"}]

    def test_result_omits_absent_partial_sections(self):
        snap = {"run_id": "r1", "status": "finished", "result": "ok", "events": []}
        with patch.object(mcp_core, "_get", return_value=snap):
            payload = json.loads(_call_tool("workflow_result", {"run_id": "r1"}))
        assert "partial_results" not in payload
        assert "agent_errors" not in payload
        assert payload["result"] == "ok"

    def test_result_redacts_credentials_in_keys_and_values(self):
        secret = "AKIA" + "L" * 16
        snap = {
            "run_id": "r1",
            "status": "finished",
            "result": {secret: [f"value {secret}"]},
            "events": [],
        }
        with patch.object(mcp_core, "_get", return_value=snap):
            out = _call_tool("workflow_result", {"run_id": "r1"})
        assert secret not in out

    def test_result_leaves_non_str_scalars_untouched(self):
        snap = {"run_id": "r1", "status": "finished", "result": {"n": 3, "flag": True}, "events": []}
        with patch.object(mcp_core, "_get", return_value=snap):
            payload = json.loads(_call_tool("workflow_result", {"run_id": "r1"}))
        assert payload["result"] == {"n": 3, "flag": True}


class TestWorkflowListCancelRerun:
    def test_list_propagates_error(self):
        with patch.object(mcp_core, "_get", return_value={"error": "refused"}):
            assert _call_tool("workflow_list", {}) == "workflow_list: refused"

    def test_list_reports_empty_registry(self):
        with patch.object(mcp_core, "_get", return_value={"runs": []}):
            assert _call_tool("workflow_list", {}) == "No workflow runs yet."

    def test_list_renders_one_line_per_run(self):
        runs = {
            "runs": [
                {"run_id": "r1", "name": "nightly", "status": "finished", "event_count": 12},
                {"run_id": "r2", "status": "running"},
            ]
        }
        with patch.object(mcp_core, "_get", return_value=runs):
            out = _call_tool("workflow_list", {})
        assert "`r1` nightly → finished (12 events)" in out
        assert "`r2` — → running (0 events)" in out

    def test_cancel_reports_both_outcomes_and_errors(self):
        with patch.object(mcp_core, "_post", return_value={"cancelled": True}) as m:
            out = _call_tool("workflow_cancel", {"run_id": "r1"})
        assert m.call_args[0][0] == "/api/workflows/runs/r1/cancel"
        assert "cancelled" in out

        with patch.object(mcp_core, "_post", return_value={"cancelled": False}):
            assert "not cancellable" in _call_tool("workflow_cancel", {"run_id": "r1"})

        with patch.object(mcp_core, "_post", return_value={"error": "unknown run"}):
            assert _call_tool("workflow_cancel", {"run_id": "r1"}) == "workflow_cancel: unknown run"

    def test_rerun_forwards_from_index_and_reports_new_run(self):
        resp = {"run_id": "r9", "replayed_before": 2}
        with patch.object(mcp_core, "_post", return_value=resp) as m:
            out = _call_tool("workflow_rerun_subtree", {"run_id": "r1", "from_index": 2})
        assert m.call_args[0][1] == {"from_index": 2}
        assert "Re-running `r1` as `r9`" in out
        assert "index 2" in out

    def test_rerun_defaults_from_index_to_zero(self):
        with patch.object(mcp_core, "_post", return_value={"run_id": "r9"}) as m:
            _call_tool("workflow_rerun_subtree", {"run_id": "r1"})
        assert m.call_args[0][1] == {"from_index": 0}

    def test_rerun_rejects_a_non_int_from_index(self):
        out = _call_tool("workflow_rerun_subtree", {"run_id": "r1", "from_index": "nope"})
        assert "from_index" in out

    def test_rerun_propagates_error(self):
        with patch.object(mcp_core, "_post", return_value={"error": "no such run"}):
            out = _call_tool("workflow_rerun_subtree", {"run_id": "r1"})
        assert out == "workflow_rerun_subtree: no such run"


# ── skill_search / register_hook / read_slack_profile / knowledge_dedup ───


class TestSkillSearch:
    def test_matches_are_rendered_with_load_hints(self, monkeypatch: pytest.MonkeyPatch):
        matches = [
            {
                "name": "babysit",
                "key": "kirocrew-dev/babysit",
                "description": "Monitor  a   PR",
                "path": "/skills/kirocrew-dev/babysit/SKILL.md",
            }
        ]
        monkeypatch.setattr(
            mcp_core,
            "SkillsLoader",
            lambda **_kw: SimpleNamespace(search_skills=lambda _q, limit: matches),
        )
        out = _call_tool("skill_search", {"query": "babysit"})
        assert "Skills matching 'babysit' (top 1)" in out
        # Whitespace in the description is collapsed.
        assert "Monitor a PR" in out
        assert "cat /skills/kirocrew-dev/babysit/SKILL.md" in out
        assert "$babysit" in out

    def test_long_description_is_truncated(self, monkeypatch: pytest.MonkeyPatch):
        matches = [{"name": "s", "key": "s", "description": "d" * 500, "path": "/p"}]
        monkeypatch.setattr(
            mcp_core,
            "SkillsLoader",
            lambda **_kw: SimpleNamespace(search_skills=lambda _q, limit: matches),
        )
        out = _call_tool("skill_search", {"query": "s"})
        assert "d" * 300 + "..." in out
        assert "d" * 301 not in out

    def test_no_matches_suggests_broader_keywords(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            mcp_core,
            "SkillsLoader",
            lambda **_kw: SimpleNamespace(search_skills=lambda _q, limit: []),
        )
        out = _call_tool("skill_search", {"query": "zzz"})
        assert "No skills matched 'zzz'" in out

    @pytest.mark.parametrize("raw,expected", [(0, 20), (999, 50), (5, 5)])
    def test_limit_is_defaulted_and_clamped(
        self, monkeypatch: pytest.MonkeyPatch, raw: int, expected: int
    ):
        seen: list[int] = []

        def _loader(**_kw: object) -> SimpleNamespace:
            def _search(_q: str, limit: int) -> list[dict]:
                seen.append(limit)
                return []

            return SimpleNamespace(search_skills=_search)

        monkeypatch.setattr(mcp_core, "SkillsLoader", _loader)
        _call_tool("skill_search", {"query": "x", "limit": raw})
        assert seen == [expected]

    def test_limit_defaults_when_omitted(self, monkeypatch: pytest.MonkeyPatch):
        seen: list[int] = []

        def _loader(**_kw: object) -> SimpleNamespace:
            def _search(_q: str, limit: int) -> list[dict]:
                seen.append(limit)
                return []

            return SimpleNamespace(search_skills=_search)

        monkeypatch.setattr(mcp_core, "SkillsLoader", _loader)
        _call_tool("skill_search", {"query": "x"})
        assert seen == [20]

    def test_loader_failure_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch):
        def _boom(**_kw: object) -> SimpleNamespace:
            raise RuntimeError("skills dir unreadable")

        monkeypatch.setattr(mcp_core, "SkillsLoader", _boom)
        out = _call_tool("skill_search", {"query": "x"})
        assert out.startswith("skill_search failed: RuntimeError")


class TestRegisterHook:
    def test_registration_persists_and_returns_the_webhook_url(self):
        out = _call_tool(
            "register_hook", {"hook_id": "review-bot", "context_summary": "pending CR"}
        )
        assert "Hook registered: review-bot" in out
        assert "Session key: hook:review-bot" in out
        assert "/api/hooks/agent" in out

        stored = json.loads((mcp_core.config_dir() / "hooks.json").read_text())
        assert stored["review-bot"]["session_key"] == "hook:review-bot"
        assert stored["review-bot"]["context_summary"] == "pending CR"

    def test_second_registration_is_merged_under_the_lock(self):
        _call_tool("register_hook", {"hook_id": "first", "context_summary": "a"})
        _call_tool("register_hook", {"hook_id": "second", "context_summary": "b"})
        stored = json.loads((mcp_core.config_dir() / "hooks.json").read_text())
        assert set(stored) == {"first", "second"}

    def test_corrupt_hooks_json_is_reported_not_overwritten(self):
        hook_file = mcp_core.config_dir() / "hooks.json"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("{not json")
        out = _call_tool("register_hook", {"hook_id": "h", "context_summary": "c"})
        assert "hooks.json is corrupted" in out
        assert hook_file.read_text() == "{not json"


class TestReadSlackProfile:
    def test_profile_values_are_redacted_but_id_is_preserved(self):
        secret = "AKIA" + "M" * 16
        resp = {"profile": {"id": "U123", "title": f"owner {secret}", "tz": "UTC"}}
        with patch.object(mcp_core, "_post", return_value=resp) as m:
            out = _call_tool("read_slack_profile", {"user": "U123"})
        assert m.call_args[0][0] == "/api/slack-profile"
        parsed = json.loads(out)
        assert parsed["id"] == "U123"
        assert secret not in parsed["title"]
        assert parsed["tz"] == "UTC"

    def test_backend_error_is_propagated(self):
        with patch.object(mcp_core, "_post", return_value={"error": "not in workspace"}):
            out = _call_tool("read_slack_profile", {"user": "U123"})
        assert out == "Error: not in workspace"


class TestKnowledgeDedup:
    def _db(self) -> Any:
        db = mcp_core.config_dir() / "workspace" / "knowledge" / "knowledge.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")
        return db

    def test_missing_db_reports_not_configured(self):
        out = _call_tool("knowledge_dedup", {})
        assert "Knowledge Library is not configured" in out

    def _patch_store(self, monkeypatch: pytest.MonkeyPatch, results: list[dict]) -> list[bool]:
        applied: list[bool] = []
        monkeypatch.setattr(
            mcp_core,
            "KnowledgeStore",
            lambda _p: SimpleNamespace(db=SimpleNamespace(close=lambda: None)),
        )

        def _sweep(_store: object, apply: bool) -> list[dict]:
            applied.append(apply)
            return results

        monkeypatch.setattr(mcp_core, "dedup_sweep", _sweep)
        return applied

    def test_no_duplicates_reports_clean(self, monkeypatch: pytest.MonkeyPatch):
        self._db()
        self._patch_store(monkeypatch, [])
        assert _call_tool("knowledge_dedup", {}) == "No cross-source duplicate documents found."

    def test_dry_run_lists_would_delete(self, monkeypatch: pytest.MonkeyPatch):
        self._db()
        applied = self._patch_store(
            monkeypatch,
            [{"loser": "a.md", "winner": "b.md", "items_deleted": 3, "reason": "same hash"}],
        )
        out = _call_tool("knowledge_dedup", {})
        assert applied == [False]
        assert "Would delete (dry run" in out
        assert "a.md (3 chunks) -> kept b.md [same hash]" in out

    def test_apply_reports_deletions(self, monkeypatch: pytest.MonkeyPatch):
        self._db()
        applied = self._patch_store(
            monkeypatch,
            [{"loser": "a.md", "winner": "b.md", "items_deleted": 1, "reason": "same hash"}],
        )
        out = _call_tool("knowledge_dedup", {"apply": True})
        assert applied == [True]
        assert out.startswith("Deleted — 1 duplicate document(s):")

    def test_output_is_redacted(self, monkeypatch: pytest.MonkeyPatch):
        self._db()
        secret = "AKIA" + "N" * 16
        self._patch_store(
            monkeypatch,
            [{"loser": f"{secret}.md", "winner": "b.md", "items_deleted": 1, "reason": "dup"}],
        )
        assert secret not in _call_tool("knowledge_dedup", {})


class TestFileSend:
    @pytest.fixture(autouse=True)
    def _isolated_outbox(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the workspace root so ``outbox_dir()`` lands inside ``tmp_path``.

        ``workspace_root`` falls back to a platform default outside
        ``KIROCREW_HOME``, so without this the tool would write to the real
        outbox and tests would collide on filenames across runs.
        """
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setattr(mcp_core, "_classify_slack_identity", lambda: ("dashboard", None))

    def test_text_file_is_copied_to_the_outbox_and_notified(self, tmp_path):
        src = tmp_path / "report.txt"
        src.write_text("all green")
        with patch.object(mcp_core, "_post", return_value={"ok": True}) as m:
            out = _call_tool("file_send", {"path": str(src), "description": "CI report"})
        assert out == "File sent: report.txt (CI report)"
        dest = mcp_core.outbox_dir() / "report.txt"
        assert dest.read_text() == "all green"
        notify = next(c for c in m.call_args_list if c[0][0] == "/api/outbox/notify")
        assert notify[0][1]["filename"] == "report.txt"
        assert notify[0][1]["size"] == len("all green")

    def test_name_collision_gets_a_unique_suffix(self, tmp_path):
        src = tmp_path / "report.txt"
        src.write_text("v1")
        (mcp_core.outbox_dir()).mkdir(parents=True, exist_ok=True)
        (mcp_core.outbox_dir() / "report.txt").write_text("older")
        with patch.object(mcp_core, "_post", return_value={"ok": True}):
            out = _call_tool("file_send", {"path": str(src)})
        assert out.startswith("File sent: report_")
        assert out.endswith(".txt")
        assert (mcp_core.outbox_dir() / "report.txt").read_text() == "older"

    def test_missing_file_is_refused(self, tmp_path):
        out = _call_tool("file_send", {"path": str(tmp_path / "nope.txt")})
        assert "file not found or access denied" in out

    def test_sensitive_content_aborts_the_send(self, tmp_path):
        src = tmp_path / "creds.txt"
        src.write_text("AKIA" + "P" * 16)
        out = _call_tool("file_send", {"path": str(src)})
        assert out == "Error: file content contains sensitive data; send aborted"

    def test_disallowed_binary_mime_is_refused(self, tmp_path):
        src = tmp_path / "payload.bin"
        src.write_bytes(b"\xff\xfe\x00\x01")
        out = _call_tool("file_send", {"path": str(src)})
        assert "binary file type not allowed" in out

    def test_allowlisted_binary_skips_the_content_scan(self, tmp_path):
        src = tmp_path / "shot.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        with patch.object(mcp_core, "_post", return_value={"ok": True}):
            out = _call_tool("file_send", {"path": str(src)})
        assert out == "File sent: shot.png"

    def test_notify_error_is_propagated(self, tmp_path):
        src = tmp_path / "report.txt"
        src.write_text("ok")
        with patch.object(mcp_core, "_post", return_value={"error": "gateway down"}):
            out = _call_tool("file_send", {"path": str(src)})
        assert out == "Error: gateway down"

    def test_unresolved_slack_identity_refuses_the_upload(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(mcp_core, "_classify_slack_identity", lambda: ("unresolved", None))
        src = tmp_path / "report.txt"
        src.write_text("ok")
        with patch.object(mcp_core, "_post", return_value={"ok": True}) as m:
            out = _call_tool("file_send", {"path": str(src)})
        assert "Slack upload skipped" in out
        assert all(c[0][0] != "/api/slack/upload-file" for c in m.call_args_list)

    def test_slack_upload_failure_is_surfaced_as_a_warning(self, tmp_path):
        src = tmp_path / "report.txt"
        src.write_text("ok")

        def _post(path: str, body: dict | None = None, **_kw: object) -> dict:
            if path == "/api/slack/upload-file":
                return {"error": "not in channel"}
            return {"ok": True}

        with patch.object(mcp_core, "_post", side_effect=_post):
            out = _call_tool("file_send", {"path": str(src)})
        assert out == "File sent: report.txt (Slack upload failed: not in channel)"


# ── argument validation seam ──────────────────────────────────────────────

class TestValidateArgs:
    def test_schema_backed_tool_is_validated_and_cleaned(self):
        cleaned = _validate_args("workflow_rerun_subtree", {"run_id": "r1"})
        assert cleaned["run_id"] == "r1"

    def test_unknown_field_is_rejected(self):
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            _validate_args("workflow_status", {"run_id": "r1", "junk": "x"})

    def test_schemaless_tool_passes_through_untouched(self):
        raw = {"anything": 1}
        assert _validate_args("learn_list", raw) == raw

    def test_invalid_run_id_pattern_is_rejected(self):
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            _validate_args("workflow_status", {"run_id": "bad id/../etc"})

    def test_call_tool_surfaces_validation_errors_as_text(self):
        out = _call_tool("workflow_status", {"run_id": "bad id/../etc"})
        assert "run_id" in out
