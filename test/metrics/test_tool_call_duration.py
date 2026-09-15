"""``kirocrew.tool.call.duration``.

Drives the REAL production helpers in ``metrics/tool_calls.py`` plus the two
parser layers that call them, so the kind normalisation, the terminal-status
rule and the exactly-once accounting across layers live in production code.
"""

from unittest.mock import patch

import pytest

from kiro_crew.metrics import tool_calls as tc


class _CapturingRecorder:
    def __init__(self) -> None:
        self.hist: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.hist.append({"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})})


@pytest.fixture(autouse=True)
def clean_registry():
    tc.reset_open_calls()
    yield
    tc.reset_open_calls()


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _calls(rec):
    return [c for c in rec.hist if c["name"] == "kirocrew.tool.call.duration"]


class TestKindClassification:
    @pytest.mark.parametrize(
        "kind", ["read", "fetch", "search", "edit", "write", "create", "delete", "move", "execute"]
    )
    def test_known_kinds_pass_through(self, kind):
        assert tc.classify_tool_kind(kind) == kind

    def test_an_mcp_call_is_labelled_by_its_transport(self):
        # The kind an MCP server reports is its own vocabulary; keeping it would
        # put third-party strings in the allowlist's path.
        assert tc.classify_tool_kind("read", mcp_server_name="some-server") == "mcp"
        assert tc.classify_tool_kind("whatever", mcp_server_name="srv") == "mcp"

    def test_an_agent_authored_kind_cannot_mint_a_series(self):
        """``kind`` arrives verbatim from the agent -- an allowlist is the bound."""
        assert tc.classify_tool_kind("totally_new_kind_9000") == "other"
        assert tc.classify_tool_kind("unknown") == "other"

    def test_absent_kind_is_other(self):
        assert tc.classify_tool_kind(None) == "other"
        assert tc.classify_tool_kind("") == "other"

    def test_every_classification_is_inside_the_declared_set(self):
        for candidate in (None, "", "read", "unknown", "execute", "nonsense"):
            assert tc.classify_tool_kind(candidate) in tc.TOOL_KINDS


class TestRoundTrip:
    def test_a_completed_call_is_recorded_once(self, rec):
        tc.note_tool_call_started("t1", kind="execute")
        tc.record_tool_call_finished("t1", status="completed")
        calls = _calls(rec)
        assert len(calls) == 1
        assert calls[0]["unit"] == "ms"
        assert calls[0]["attrs"] == {"tool_kind": "execute", "outcome": "completed"}
        assert calls[0]["value"] > 0

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_every_terminal_status_is_an_outcome(self, rec, status):
        tc.note_tool_call_started("t1", kind="read")
        tc.record_tool_call_finished("t1", status=status)
        assert _calls(rec)[0]["attrs"]["outcome"] == status

    @pytest.mark.parametrize("status", ["pending", "in_progress", None, ""])
    def test_a_non_terminal_update_leaves_the_clock_running(self, rec, status):
        tc.note_tool_call_started("t1", kind="read")
        tc.record_tool_call_finished("t1", status=status)
        assert not _calls(rec)
        assert tc.open_call_count() == 1
        tc.record_tool_call_finished("t1", status="completed")
        assert len(_calls(rec)) == 1

    def test_a_finish_with_no_start_records_nothing(self, rec):
        """This is what makes the two instrumented layers add up to one sample."""
        tc.record_tool_call_finished("never-started", status="completed")
        assert not _calls(rec)

    def test_a_second_finish_records_nothing(self, rec):
        tc.note_tool_call_started("t1", kind="read")
        tc.record_tool_call_finished("t1", status="completed")
        tc.record_tool_call_finished("t1", status="completed")
        assert len(_calls(rec)) == 1

    def test_a_repeated_start_does_not_restart_the_clock(self, rec):
        tc.note_tool_call_started("t1", kind="execute")
        tc.note_tool_call_started("t1", kind="read")
        tc.record_tool_call_finished("t1", status="completed")
        # The refinement's kind loses to the original, which is what keeps the
        # measured span the whole round-trip.
        assert _calls(rec)[0]["attrs"]["tool_kind"] == "execute"

    def test_an_empty_id_is_a_no_op(self, rec):
        tc.note_tool_call_started("", kind="read")
        tc.record_tool_call_finished("", status="completed")
        assert not _calls(rec)
        assert tc.open_call_count() == 0

    def test_the_registry_is_bounded(self, rec):
        for i in range(tc._MAX_OPEN_CALLS + 5):
            tc.note_tool_call_started(f"t{i}", kind="read")
        assert tc.open_call_count() <= tc._MAX_OPEN_CALLS


class TestParserWiring:
    """The production parsers must be the ones driving the registry."""

    def test_the_shared_dispatch_parser_opens_and_closes_a_call(self, rec):
        from kiro_crew.acp import _dispatch

        _dispatch._build_tool_call_event(
            {"sessionUpdate": "tool_call", "toolCallId": "tc-1", "kind": "execute", "title": "sh"},
            None,
        )
        assert tc.open_call_count() == 1
        _dispatch._build_tool_result_event(
            {"toolCallId": "tc-1", "status": "completed", "content": []},
        )
        calls = _calls(rec)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"tool_kind": "execute", "outcome": "completed"}

    def test_an_mcp_served_call_is_labelled_mcp_by_the_parser(self, rec):
        """The trusted ``_meta.kiro.mcpServerName``, not the reported kind."""
        from kiro_crew.acp import _dispatch

        _dispatch._build_tool_call_event(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-m",
                "kind": "read",
                "title": "t",
                "_meta": {"kiro": {"mcpServerName": "srv", "toolName": "x"}},
            },
            None,
        )
        _dispatch._build_tool_result_event({"toolCallId": "tc-m", "status": "completed"})
        assert _calls(rec)[0]["attrs"]["tool_kind"] == "mcp"

    def test_an_output_less_completion_is_still_measured(self, rec):
        """_build_tool_result_event returns None with no output; the sample stays."""
        from kiro_crew.acp import _dispatch

        _dispatch._build_tool_call_event(
            {"sessionUpdate": "tool_call", "toolCallId": "tc-2", "kind": "read", "title": "r"},
            None,
        )
        event = _dispatch._build_tool_result_event({"toolCallId": "tc-2", "status": "completed"})
        assert event is None
        assert len(_calls(rec)) == 1

    def test_both_layers_together_still_yield_one_sample(self, rec):
        """A frame seen by AcpClient and by the shared parser is not doubled."""
        from kiro_crew.acp import _dispatch

        _dispatch._build_tool_call_event(
            {"sessionUpdate": "tool_call", "toolCallId": "tc-3", "kind": "read", "title": "r"},
            None,
        )
        for _ in range(2):
            _dispatch._build_tool_result_event({"toolCallId": "tc-3", "status": "completed"})
        assert len(_calls(rec)) == 1


class TestScopeIsolation:
    """Found in review: toolCallId is backend-scoped, the registry is global."""

    def test_two_sessions_reusing_an_id_each_get_their_own_sample(self, rec):
        tc.note_tool_call_started("t1", kind="execute", scope="sessA")
        tc.note_tool_call_started("t1", kind="read", scope="sessB")
        assert tc.open_call_count() == 2, "the second start must not look like a duplicate"
        tc.record_tool_call_finished("t1", status="completed", scope="sessA")
        tc.record_tool_call_finished("t1", status="completed", scope="sessB")
        kinds = sorted(c["attrs"]["tool_kind"] for c in _calls(rec))
        assert kinds == ["execute", "read"], "each session keeps its own kind"

    def test_a_finish_in_the_wrong_scope_does_not_steal_the_entry(self, rec):
        tc.note_tool_call_started("t1", kind="execute", scope="sessA")
        tc.record_tool_call_finished("t1", status="completed", scope="sessB")
        assert not _calls(rec)
        assert tc.open_call_count() == 1
        tc.record_tool_call_finished("t1", status="completed", scope="sessA")
        assert len(_calls(rec)) == 1

    def test_the_key_matches_the_dispatch_cache_spelling(self):
        """Same composition as _dispatch's _ck, so the two cannot disagree."""
        assert tc._registry_key("tc-1", "sess") == "sess|tc-1"
        assert tc._registry_key("tc-1", "") == "tc-1"

    def test_both_layers_derive_one_frames_scope_from_the_same_session_id(self, rec):
        """Design review: agreeing scopes are what make the second layer a no-op.

        Layer one takes the scope from the frame's ``sessionId``, which
        ``session_handle`` forwards as ``cache_scope``; layer two reads the
        client's own ``_session_id``. For one frame those are the same value, so
        whichever layer sees it second pops the entry the first opened and the
        call is sampled once. Were they ever to diverge, the disjoint-parser
        assumption would fail by DOUBLE-counting rather than by harmlessly
        missing -- which is why this is pinned at both the source and the
        behaviour.
        """
        import inspect

        from kiro_crew.acp import _dispatch
        from kiro_crew.acp.client import AcpClient

        # Source: neither layer may invent a scope of its own.
        layer_two = inspect.getsource(AcpClient._extract_tool_call_update)
        assert 'scope=getattr(self, "_session_id", "") or ""' in layer_two
        assert "scope=cache_scope" in inspect.getsource(_dispatch._build_tool_call_event)

        # Behaviour: layer two opens the call, layer one closes it, one sample.
        sid = "acp-sess-7"
        client = AcpClient.__new__(AcpClient)  # scope only; no process is spawned
        client._session_id = sid
        tc.note_tool_call_started(
            "tc-x", kind="read", scope=getattr(client, "_session_id", "") or ""
        )
        assert tc.open_call_count() == 1
        _dispatch._build_tool_result_event({"toolCallId": "tc-x", "status": "completed"}, sid)
        assert len(_calls(rec)) == 1
        assert tc.open_call_count() == 0, "the cross-layer pop must consume the entry"

    def test_the_dispatch_parser_threads_its_cache_scope_through(self, rec):
        from kiro_crew.acp import _dispatch

        for scope in ("sessA", "sessB"):
            _dispatch._build_tool_call_event(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "dup",
                    "kind": "execute" if scope == "sessA" else "read",
                    "title": "t",
                },
                None,
                cache_scope=scope,
            )
        assert tc.open_call_count() == 2
        for scope in ("sessA", "sessB"):
            _dispatch._build_tool_result_event({"toolCallId": "dup", "status": "completed"}, scope)
        assert len(_calls(rec)) == 2


class TestContract:
    def test_the_histogram_has_registered_bounds(self):
        from kiro_crew.metrics.provider import _HISTOGRAM_BUCKETS_MS

        bounds = _HISTOGRAM_BUCKETS_MS[tc.TOOL_CALL_METRIC]
        # Sub-ms reads keep resolution; a multi-minute build stays off +Inf.
        assert bounds[0] < 1
        assert bounds[-1] >= 60 * 60 * 1000

    def test_no_tool_name_reaches_the_attributes(self, rec):
        """MCP tool names are unbounded, so they must never be a label."""
        tc.note_tool_call_started("t1", kind="read", mcp_server_name="srv")
        tc.record_tool_call_finished("t1", status="completed")
        assert set(_calls(rec)[0]["attrs"]) == {"tool_kind", "outcome"}
