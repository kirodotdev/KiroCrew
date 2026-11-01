"""``kirocrew.crew.session_control.calls``.

Drives the REAL production pieces: the classifiers in
``metrics/session_control_calls.py``, the ``_counted`` wrapper that every
session-control route carries, and the live router that binds them -- so the
closed enums, the exactly-once accounting and the route-to-op mapping are all
properties of production code rather than of a fixture.

The guarantee each section pins, and the one-line change that reddens it:

* ``TestOpEnum`` -- every op is in the closed set and anything else folds to
  ``other``. Reddens if ``classify_op`` returns its argument unchecked.
* ``TestCallerEnum`` -- ``caller`` is derived from
  ``agent_files.OWNED_KIRO_AGENT_FILES``, equals the ``agent_kind`` value set
  the crew-log usage fold names from its own hand-written stem table, and folds
  an unowned agent name to ``other``. Reddens if the derivation keeps a raw stem
  or drops the ``-`` to ``_`` rewrite.
* ``TestExactlyOnce`` -- one completed call is one count, a 4xx or a raised
  handler is one count with ``outcome=error``, and a refusal taken before the
  handler body runs is still one count. Reddens if the wrapper is moved inside
  the handler's auth gate, or if a second emit site is added.
* ``TestRouteOps`` -- each registered route counts its own verb, read off the
  live router. Reddens if a decorator is given the wrong op.
* ``TestPrivacy`` -- no attribute carries an id, a name, a path or free text,
  even when the request and the slot are full of them. Reddens if the wrapper
  passes a session key, a slot key or an agent name through to the recorder.
* ``TestTelemetryOff`` -- a telemetry-disabled process records nothing. Reddens
  if the emit stops going through ``get_recorder()``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import session_control as handler
from kiro_crew.metrics import session_control_calls as scc
from kiro_crew.metrics.recorder import MetricsRecorder

#: The ``agent_kind`` value set the crew-log usage fold names from its own
#: hand-written stem table, written out LITERALLY here. The production side
#: DERIVES its set, so a literal is what makes this a real cross-check rather
#: than the derivation agreeing with itself, and it is the evidence that one
#: function can serve both callers.
EXPECTED_AGENT_KINDS = frozenset(
    {
        "lite",
        "guest",
        "conductor",
        "pipeline_conductor",
        "ledger_conductor",
        "security_conductor",
        "worker",
        "dashboard_author",
        "knowledge",
        "research",
        "team_lead",
        "heartbeat",
        "dashboard_manager",
        # A crewmate's conductor and its own worker (kirocrew-mate-conductor /
        # kirocrew-mate-worker, both in OWNED_KIRO_AGENT_FILES).
        "mate_conductor",
        "mate_worker",
        "default",
        "crewmate",
        "other",
    }
)

#: A crewmate's pinned DM session key, in the spelling the header carries it.
CREWMATE_SESSION = "dashboard:member-radar"


class _CapturingRecorder:
    """Records counter adds, and nothing else."""

    def __init__(self) -> None:
        self.counts: list[dict] = []

    def counter(self, name, value=1, *, unit="1", attrs=None, **kwargs) -> None:
        self.counts.append({"name": name, "value": value, "attrs": dict(attrs or {})})


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _calls(rec) -> list[dict]:
    return [c for c in rec.counts if c["name"] == scc.SESSION_CONTROL_CALLS_METRIC]


def _request(session_key: str = "dashboard:chat-10-1789623359", *, state=None) -> web.Request:
    """A session-control request carrying *session_key*, as the middleware marks it."""
    app = web.Application()
    if state is not None:
        app["state"] = state
    request = make_mocked_request(
        "POST",
        "/api/session-control/create",
        app=app,
        headers={"X-Session-Key": session_key},
    )
    request["internal_auth"] = True
    return request


class _Slot:
    """The two fields :func:`handler._caller_kind` reads off a live slot."""

    def __init__(self, key: str, agent: str) -> None:
        self.key = key
        self.agent = agent


class _State:
    """The in-memory slot registry, with the two doors the classifier knocks on."""

    def __init__(self, slots: dict[str, _Slot] | None = None) -> None:
        self._slots = dict(slots or {})

    def get_slot(self, name: str) -> _Slot | None:
        return self._slots.get(name)


# ---------------------------------------------------------------------------
# op
# ---------------------------------------------------------------------------


class TestOpEnum:
    def test_the_closed_list_is_the_rfcs_nineteen(self):
        assert len(scc.SESSION_CONTROL_OPS) == 19

    @pytest.mark.parametrize("op", sorted(scc.SESSION_CONTROL_OPS))
    def test_a_named_op_passes_through(self, op):
        assert scc.classify_op(op) == op

    @pytest.mark.parametrize("op", ["revive", "history", "totally_new_verb_9000", "", None])
    def test_an_unnamed_op_folds_to_other(self, op):
        assert scc.classify_op(op) == scc.OP_OTHER

    def test_the_two_unnamed_routes_are_the_known_ones(self):
        """``revive`` and ``history`` are registered routes the op list omits.

        Pinned so the fold is a recorded decision rather than an accident: the
        day either joins the list, this test is the thing that says so.
        """
        assert "revive" not in scc.SESSION_CONTROL_OPS
        assert "history" not in scc.SESSION_CONTROL_OPS


# ---------------------------------------------------------------------------
# caller
# ---------------------------------------------------------------------------


class TestCallerEnum:
    def test_the_derived_set_equals_the_hand_written_agent_kind_table(self):
        assert set(scc.SESSION_CONTROL_CALLER_KINDS) == set(EXPECTED_AGENT_KINDS)

    def test_every_owned_agent_file_has_a_kind(self):
        """Derived, so a new owned file joins the enum with no table to edit."""
        from kiro_crew.agent_files import AGENT_FILENAME, OWNED_KIRO_AGENT_FILES

        for filename in OWNED_KIRO_AGENT_FILES:
            stem = filename[: -len(".json")]
            kind = scc.caller_kind_for_agent(stem)
            assert kind in scc.SESSION_CONTROL_CALLER_KINDS
            assert kind != scc.CALLER_OTHER, f"{stem} fell through to other"
            if filename == AGENT_FILENAME:
                assert kind == scc.CALLER_DEFAULT

    def test_a_hyphenated_stem_becomes_an_underscored_kind(self):
        assert scc.caller_kind_for_agent("kirocrew-pipeline-conductor") == "pipeline_conductor"
        assert scc.caller_kind_for_agent("kirocrew-team-lead") == "team_lead"

    def test_the_bare_default_agent_is_default_not_its_own_stem(self):
        assert scc.caller_kind_for_agent("kirocrew") == scc.CALLER_DEFAULT

    @pytest.mark.parametrize("agent", ["my-own-agent", "kirocrew-not-a-real-file", "zzz"])
    def test_an_unowned_agent_name_cannot_mint_a_series(self, agent):
        assert scc.caller_kind_for_agent(agent) == scc.CALLER_OTHER

    @pytest.mark.parametrize("agent", ["", None])
    def test_a_blank_agent_is_the_default_agent_not_an_unknown_one(self, agent):
        """``_ChatSlot`` takes ``agent: str = ""`` and an ordinary dashboard chat
        is minted without one, so the blank is what the largest caller
        population actually carries -- and ``crew_log/emit_session.py`` already
        reads it that way (``agent or _DEFAULT_AGENT``). Calling it ``other``
        would file that population under "an agent this build does not own"."""
        assert scc.caller_kind_for_agent(agent) == scc.CALLER_DEFAULT

    def test_an_unresolved_slot_is_other_even_though_its_agent_is_also_blank(self):
        """The line between the two blanks: no slot is genuinely unknown."""
        assert scc.classify_caller(session_key="chat-99", agent="", slot_found=False) == (
            scc.CALLER_OTHER
        )
        assert scc.classify_caller(session_key="chat-10", agent="", slot_found=True) == (
            scc.CALLER_DEFAULT
        )

    def test_a_crewmate_key_still_wins_with_no_slot(self):
        """A member DM key identifies its caller without a slot, so the crewmate
        check runs before the ``slot_found`` gate."""
        assert scc.classify_caller(session_key=CREWMATE_SESSION, slot_found=False) == (
            scc.CALLER_CREWMATE
        )

    def test_a_crewmate_dm_slot_is_crewmate_whatever_its_agent_says(self):
        """The crewmate kind lives in the KEY: a DM slot's ``agent`` is the
        crewmate's own crew name, so asking the agent name can only answer
        ``other``."""
        assert (
            scc.classify_caller(session_key=CREWMATE_SESSION, agent="Radar") == scc.CALLER_CREWMATE
        )
        assert scc.classify_caller(slot_key="member-radar", agent="Radar") == scc.CALLER_CREWMATE

    def test_classification_is_always_inside_the_declared_set(self):
        for key, agent in (("", ""), ("chat-1", "nonsense"), (CREWMATE_SESSION, "x")):
            assert scc.classify_caller(session_key=key, agent=agent) in (
                scc.SESSION_CONTROL_CALLER_KINDS
            )


class TestCallerFromARequest:
    """``handler._caller_kind`` -- live state only, no disk read on the route."""

    def test_a_crewmate_slot_reads_as_crewmate(self):
        state = _State({CREWMATE_SESSION: _Slot("member-radar", "Radar")})
        assert handler._caller_kind(_request(CREWMATE_SESSION, state=state)) == "crewmate"

    def test_an_owned_agents_slot_reads_as_its_kind(self):
        state = _State({"chat-10": _Slot("chat-10", "kirocrew-worker")})
        assert handler._caller_kind(_request("chat-10", state=state)) == "worker"

    def test_an_unknown_agent_reads_as_other(self):
        state = _State({"chat-10": _Slot("chat-10", "some-third-party-agent")})
        assert handler._caller_kind(_request("chat-10", state=state)) == "other"

    def test_an_ordinary_dashboard_chat_reads_as_default(self):
        """The common case: a slot minted with no agent at all."""
        state = _State({"chat-10": _Slot("chat-10", "")})
        assert handler._caller_kind(_request("chat-10", state=state)) == "default"

    def test_an_unresolvable_caller_reads_as_other(self):
        assert handler._caller_kind(_request("chat-99", state=_State())) == "other"

    def test_no_state_on_the_app_reads_as_other_without_raising(self):
        assert handler._caller_kind(_request("chat-10")) == "other"

    def test_a_history_key_resolves_through_the_slot_walk(self, monkeypatch):
        """The header is often the HISTORY key, not the slot key. The direct
        lookup misses then, and the walk is what answers.

        Patched on the ``session_control`` FACADE, which is the only place a
        shared mirrored name may be patched -- see
        ``test/test_facade_mirror_guards.py``.
        """
        from kiro_crew.dashboard import session_control as sc

        state = _State({"chat-10": _Slot("chat-10", "kirocrew-conductor")})
        monkeypatch.setattr(sc, "caller_slot_key", lambda _state, _key: "chat-10")
        assert handler._caller_kind(_request("dashboard:chat-10-1789623359", state=state)) == (
            "conductor"
        )


# ---------------------------------------------------------------------------
# outcome + exactly-once
# ---------------------------------------------------------------------------


class TestOutcome:
    @pytest.mark.parametrize("status", [200, 201, 204, 399])
    def test_below_400_is_ok(self, status):
        assert scc.outcome_for_status(status) == scc.OUTCOME_OK

    @pytest.mark.parametrize("status", [400, 403, 404, 409, 429, 500, 503, None, 0])
    def test_400_and_up_and_absent_are_error(self, status):
        assert scc.outcome_for_status(status) == scc.OUTCOME_ERROR


class TestExactlyOnce:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("op", sorted(scc.SESSION_CONTROL_OPS))
    async def test_one_completed_call_is_one_count(self, rec, op):
        """Every one of the nineteen ops, end to end through the real wrapper."""

        @handler._counted(op)
        async def route(_request):
            return web.json_response({"ok": True})

        await route(_request())
        assert len(_calls(rec)) == 1
        assert _calls(rec)[0]["value"] == 1
        assert _calls(rec)[0]["attrs"] == {"op": op, "caller": "other", "outcome": "ok"}

    @pytest.mark.asyncio
    async def test_an_unnamed_op_is_counted_as_other(self, rec):
        @handler._counted("revive")
        async def route(_request):
            return web.json_response({"ok": True})

        await route(_request())
        assert _calls(rec)[0]["attrs"]["op"] == "other"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 403, 404, 409, 500, 503])
    async def test_a_refused_call_is_one_count_with_outcome_error(self, rec, status):
        @handler._counted("send")
        async def route(_request):
            return web.json_response({"error": "no"}, status=status)

        response = await route(_request())
        assert response.status == status
        assert len(_calls(rec)) == 1
        assert _calls(rec)[0]["attrs"] == {"op": "send", "caller": "other", "outcome": "error"}

    @pytest.mark.asyncio
    async def test_a_handler_that_raises_is_counted_error_and_the_error_is_reraised(self, rec):
        """The wrapper only ever ADDS the count: the exception is the caller's."""

        @handler._counted("stop")
        async def route(_request):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await route(_request())
        assert len(_calls(rec)) == 1
        assert _calls(rec)[0]["attrs"]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_a_call_refused_before_the_handler_body_runs_is_still_counted(self, rec):
        """The wrapper sits OUTSIDE ``_require_internal``.

        A caller with no internal secret never reaches the handler body, but it
        DID make a session-control call and it DID fail -- so the count must
        exist. Reddens the moment the wrapper is moved under the gate.
        """
        request = make_mocked_request("POST", "/api/session-control/stop", app=web.Application())
        # No ``internal_auth``: the gate refuses, the body never runs.
        with patch.object(handler, "sel") as fake_sel:
            fake_sel.return_value.log_api_access.return_value = None
            response = await handler.api_session_control_stop(request)
        assert response.status == 403
        assert len(_calls(rec)) == 1
        assert _calls(rec)[0]["attrs"] == {"op": "stop", "caller": "other", "outcome": "error"}

    @pytest.mark.asyncio
    async def test_two_calls_are_two_counts(self, rec):
        @handler._counted("adopt")
        async def route(_request):
            return web.json_response({})

        await route(_request())
        await route(_request())
        assert len(_calls(rec)) == 2

    @pytest.mark.asyncio
    async def test_a_failing_classifier_does_not_fail_the_call(self, rec, monkeypatch):
        """Telemetry must never break the instrumented path."""
        monkeypatch.setattr(
            handler, "classify_caller", lambda **_kwargs: (_ for _ in ()).throw(ValueError("x"))
        )

        @handler._counted("close")
        async def route(_request):
            return web.json_response({"ok": True})

        response = await route(_request())
        assert response.status == 200
        assert _calls(rec)[0]["attrs"]["caller"] == "other"

    def test_a_failing_recorder_does_not_raise(self):
        class _Boom:
            def counter(self, *_a, **_k):
                raise RuntimeError("recorder down")

        with patch("kiro_crew.metrics.provider.get_recorder", return_value=_Boom()):
            scc.record_session_control_call("send", caller="worker", outcome="ok")

    def test_an_out_of_set_attribute_is_folded_at_the_emit_site(self, rec):
        """The emit site is the only place that has to be right about the bound."""
        scc.record_session_control_call("nonsense", caller="not-a-kind", outcome="maybe")
        assert _calls(rec)[0]["attrs"] == {"op": "other", "caller": "other", "outcome": "error"}


# ---------------------------------------------------------------------------
# the route table
# ---------------------------------------------------------------------------


class TestRouteOps:
    @pytest.mark.asyncio
    async def test_every_registered_route_counts_its_own_verb(self, rec):
        """Derived from the live router, so a 21st route or a mistyped op reds here.

        A decorator carrying the wrong verb is the one error a reader cannot see
        by eye -- it emits a plausible count for a call that never happened.

        The op is read off the REAL EMIT rather than off an annotation on the
        handler: each route is called with an unauthenticated request, which its
        own gate refuses, and the refusal still carries the op. So this asserts
        what the counter actually records, and needs nothing in production whose
        only consumer is a test.
        """
        from kiro_crew.dashboard.server import _register_mcp_routes

        app = web.Application()
        _register_mcp_routes(app)

        found: dict[str, str] = {}
        with patch.object(handler, "sel") as fake_sel:
            fake_sel.return_value.log_api_access.return_value = None
            for route in app.router.routes():
                path = str(getattr(route.resource, "canonical", ""))
                if not path.startswith("/api/session-control/"):
                    continue
                rec.counts.clear()
                request = make_mocked_request(route.method, path, app=web.Application())
                response = await route.handler(request)
                assert response.status == 403, f"{path} did not refuse an anonymous call"
                emitted = _calls(rec)
                assert len(emitted) == 1, f"{path} emitted {len(emitted)} counts, want 1"
                found[path] = emitted[0]["attrs"]["op"]

        # ``set_color``, ``revive`` and ``history`` appear here as ``other``: that
        # IS what the counter records for them, which is the fact worth pinning.
        assert found == {
            "/api/session-control/create": "create",
            "/api/session-control/fork": "fork",
            "/api/session-control/stop": "stop",
            "/api/session-control/end-wait": "end_wait",
            "/api/session-control/retry": "retry",
            "/api/session-control/continue": "continue",
            "/api/session-control/set-model": "set_model",
            "/api/session-control/reload": "reload",
            "/api/session-control/rename": "rename",
            "/api/session-control/generate-title": "generate_title",
            "/api/session-control/set-color": "other",
            "/api/session-control/close": "close",
            "/api/session-control/revive": "other",
            "/api/session-control/history": "other",
            "/api/session-control/send": "send",
            "/api/session-control/broadcast": "broadcast",
            "/api/session-control/status": "status",
            "/api/session-control/adopt": "adopt",
            "/api/session-control/handoff": "handoff",
            "/api/session-control/release": "release",
            "/api/session-control/read": "read_message",
            "/api/session-control/summary": "summary",
        }

    @pytest.mark.asyncio
    async def test_every_named_op_is_reachable_from_a_real_route(self, rec):
        """The nineteen ops are the routes' own verbs, not an aspiration.

        Reds if an op is named in the enum but no route ever emits it -- a value
        the dashboard would wait for forever.
        """
        from kiro_crew.dashboard.server import _register_mcp_routes

        app = web.Application()
        _register_mcp_routes(app)
        with patch.object(handler, "sel") as fake_sel:
            fake_sel.return_value.log_api_access.return_value = None
            for route in app.router.routes():
                path = str(getattr(route.resource, "canonical", ""))
                if path.startswith("/api/session-control/"):
                    await route.handler(
                        make_mocked_request(route.method, path, app=web.Application())
                    )
        emitted = {c["attrs"]["op"] for c in _calls(rec)}
        assert scc.SESSION_CONTROL_OPS <= emitted
        assert emitted - scc.SESSION_CONTROL_OPS == {scc.OP_OTHER}


# ---------------------------------------------------------------------------
# privacy + consent
# ---------------------------------------------------------------------------


class TestPrivacy:
    @pytest.mark.asyncio
    async def test_no_attribute_carries_an_id_a_name_a_path_or_text(self, rec):
        """The request and the slot are full of identifying strings; none reach
        the recorder."""
        secrets = (
            "dashboard:chat-10-1789623359",
            "member-radar",
            "Radar",
            "some-third-party-agent",
            "/api/session-control/create",
        )
        state = _State({"dashboard:chat-10-1789623359": _Slot("member-radar", "Radar")})

        @handler._counted("create")
        async def route(_request):
            return web.json_response({})

        await route(_request("dashboard:chat-10-1789623359", state=state))

        attrs = _calls(rec)[0]["attrs"]
        assert set(attrs) == {"op", "caller", "outcome"}
        for value in attrs.values():
            assert isinstance(value, str)
            for secret in secrets:
                assert secret not in value

    def test_the_whole_series_space_is_bounded_and_small(self):
        """20 ops (19 named plus ``other``) x 18 caller kinds x 2 outcomes."""
        space = (len(scc.SESSION_CONTROL_OPS) + 1) * len(scc.SESSION_CONTROL_CALLER_KINDS) * 2
        assert space <= 720


class TestTelemetryOff:
    def test_a_telemetry_disabled_process_records_nothing(self):
        """The recorder a disabled process holds has no meter, so the emit
        returns before touching an instrument -- no series is created at all."""
        disabled = MetricsRecorder(None)
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=disabled):
            scc.record_session_control_call("send", caller="worker", outcome="ok")
        assert disabled._counters == {}

    @pytest.mark.asyncio
    async def test_a_route_still_answers_with_telemetry_off(self):
        disabled = MetricsRecorder(None)

        @handler._counted("send")
        async def route(_request):
            return web.json_response({"ok": True})

        with patch("kiro_crew.metrics.provider.get_recorder", return_value=disabled):
            response = await route(_request())
        assert response.status == 200
        assert disabled._counters == {}
