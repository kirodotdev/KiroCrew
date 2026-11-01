"""The auto-nudge loop's own text is credential-scrubbed on every surface that serves it.

A loop's ``message`` and ``stopped_reason`` are agent-authored free text, and
several producers reach ``svc.add`` without passing through the arming
authorizer, so the stored value can be text nothing has scanned. These pin that
it is scrubbed on each surface that publishes it, and that the two fields a
client ADDRESSES a row by survive verbatim. Two shape invariants sit beside the
scrub, because a denylist that flattens is as much a defect as one that leaks:
numbers stay numbers, and the typed ``judge`` readings stay objects the goal
popover can read.

The last group pins the status code. A host that declares a credential policy it
cannot compose raises while RENDERING, which answered 500 -- on a write, for a
mutation that had already committed. Reads refuse 503 before the lookup; writes
refuse before the mutation.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge_authz as authz
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h
from kiro_crew.platform import PlatformCompositionError

SECRET = "AKIAIOSFODNN7EXAMPLE"


def _install_broken_policy() -> None:
    """Replace the active credential policy with one that cannot compose."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context

    class _Broken:
        def redact(self, text: str) -> str:
            raise PlatformCompositionError("companion credential policy unreadable")

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, credentials=_Broken()))


@pytest.fixture
def broken_policy() -> Any:
    """Install the uncomposable policy for one test and restore the real one after."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context

    _install_broken_policy()
    yield
    set_context(build_default_context(KiroCrewConfig()))


def _loop(**kw: Any) -> NudgeLoop:
    base: dict[str, Any] = dict(
        id="loop-abc", slot_key="chat-1-111", message="keep checking", idle_secs=300
    )
    base.update(kw)
    return NudgeLoop(**base)


def _monitor_loop(**kw: Any) -> NudgeLoop:
    """A loop the reducing arm actually projects.

    ``_serialize_for_legacy_reader`` hands a PLAIN loop to ``_serialize`` and only
    reduces when ``is_structured_monitor_loop`` holds, which needs a monitor and
    no gate. The stand-in carries just the three attributes
    ``_MONITOR_MAPPED_LEGACY_FIELDS`` walks.
    """
    loop = _loop(**kw)
    loop.monitor = SimpleNamespace(
        budgets=SimpleNamespace(max_agent_turns=5),
        agent_turns=2,
        last_completed_at=1700000000.0,
    )
    loop.gate = False
    return loop


class _FakeSvc:
    def __init__(self, loops: list[NudgeLoop] | None = None) -> None:
        self.loops = loops or []

    def list_all(self) -> list[NudgeLoop]:
        return list(self.loops)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return next((lp for lp in self.loops if lp.slot_key == slot_key), None)


class _FakeState:
    """The three attributes ``NudgeAuthzState`` declares, and nothing else.

    The slot is PRESENT and persistent on purpose: an absent one is refused 404
    and a non-persistent one is refused as unable to host a loop, both ahead of
    the scrub preflight, so either would pass this file's 503 tests vacuously.
    """

    def __init__(self, slot_key: str = "chat-1-111") -> None:
        self._slots: dict = {slot_key: SimpleNamespace(memory_mode="persistent", mode="")}
        self.sessions: Any = None
        self.channel_transports: Any = None


class TestTheRestProjectionScrubsLoopText:
    def test_a_credential_in_message_does_not_reach_the_client(self) -> None:
        out = h._serialize(_loop(message=f"deploy with {SECRET} now"))
        assert SECRET not in out["message"], "the REST surface served an unredacted credential"

    def test_a_credential_in_stopped_reason_does_not_reach_the_client(self) -> None:
        out = h._serialize(_loop(stopped_reason=f"failed on {SECRET}"))
        assert SECRET not in out["stopped_reason"], "stopped_reason was served raw"

    def test_the_redactor_really_rewrites_this_value(self) -> None:
        """The positive control the exemption test below needs.

        Without it, asserting that a credential-shaped id SURVIVES would pass
        just as well on a redactor that rewrites nothing at all.
        """
        out = h._serialize(_loop(message=SECRET))
        assert out["message"] != SECRET, "the redactor left a known credential untouched"

    def test_the_addressing_fields_survive_verbatim(self) -> None:
        out = h._serialize(_loop(id=SECRET, slot_key=SECRET))
        assert out["id"] == SECRET, "a scrubbed id addresses no row"
        assert out["slot_key"] == SECRET, "a scrubbed slot_key addresses no row"

    def test_ordinary_text_round_trips_unchanged(self) -> None:
        out = h._serialize(_loop(message="keep checking the PR"))
        assert out["message"] == "keep checking the PR"


class TestTheGoalTokenSurvivesVerbatim:
    def test_a_token_the_detector_would_redact_is_served_unchanged(self) -> None:
        """The popover sends ``goal_token`` back as ``expect_fingerprint``.

        A scrubbed token never equals the stored one, so every save would be
        refused as a stale baseline. ``SECRET`` stands in for a token a future
        detector matches; today's 32-hex tokens match nothing.
        """
        out = h._serialize(_loop(goal_token=SECRET, message=SECRET))
        assert out["goal_token"] == SECRET, "goal_token was scrubbed, breaking every save"
        assert SECRET not in out["message"], "the control: this value IS redacted elsewhere"


class TestTheScrubPreservesShape:
    def test_numeric_fields_are_not_rendered_as_text(self) -> None:
        out = h._serialize(_loop(idle_secs=300, cycle_count=4, active=True))
        assert out["idle_secs"] == 300, "a client does arithmetic on this"
        assert out["cycle_count"] == 4
        assert out["active"] is True, "a bool became something else"

    def test_judge_keeps_its_object_shape(self) -> None:
        """The popover reads ``loop.judge.wake_when``, so a string breaks it."""
        out = h._serialize(_loop(judge={"wake_when": "a review lands"}))
        assert isinstance(out["judge"], dict), "judge was flattened into text"
        assert out["judge"]["wake_when"] == "a review lands"

    def test_judge_last_verdict_keeps_its_object_shape(self) -> None:
        verdict = {"outcome": "blocked", "evidence_items": 2, "at": 1700000000}
        out = h._serialize(_loop(judge_last_verdict=verdict))
        assert isinstance(out["judge_last_verdict"], dict), "the verdict was flattened"
        assert out["judge_last_verdict"]["evidence_items"] == 2, "a nested number became text"

    def test_a_credential_nested_in_judge_is_still_scrubbed(self) -> None:
        """Walking the container must not mean skipping its leaves."""
        out = h._serialize(_loop(judge={"wake_when": f"when {SECRET} rotates"}))
        assert SECRET not in out["judge"]["wake_when"], "a nested credential was served raw"


class TestEveryProjectionArmUsesTheSameRule:
    def test_the_in_session_reading_scrubs_stopped_reason(self) -> None:
        out = h._autonudge_loop_reading(_loop(stopped_reason=f"died on {SECRET}"))
        assert SECRET not in out["stopped_reason"], "the in-session reading served it raw"

    def test_the_reduced_row_scrubs_stopped_reason(self) -> None:
        """The reduced row is what ``GET /api/autonudge`` serves for a structured
        monitor, and that route has no owner gate. ``message`` is withheld there
        but ``stopped_reason`` is not, so this arm needs its own bound: a plain
        loop never reaches it, because the reducer hands one straight to
        ``_serialize``.
        """
        out = h._serialize_for_legacy_reader(_monitor_loop(stopped_reason=f"died on {SECRET}"))
        assert "message" not in out, "the reduction stopped withholding message"
        assert SECRET not in out["stopped_reason"], "the ungated list route served it raw"

    def test_the_reduced_row_keeps_the_addressing_fields_verbatim(self) -> None:
        out = h._serialize_for_legacy_reader(_monitor_loop(id=SECRET, slot_key=SECRET))
        assert out["id"] == SECRET, "a scrubbed id addresses no row"
        assert out["slot_key"] == SECRET, "a scrubbed slot_key addresses no row"

    def test_a_nested_addressing_name_is_not_exempted(self) -> None:
        """``ADDRESSING_FIELDS`` names top-level fields, and nothing deeper."""
        out = h._serialize(_loop(judge={"id": SECRET}))
        assert SECRET not in out["judge"]["id"], "a nested key inherited the exemption"


class TestAReadThatCannotScrubRefusesWith503:
    @pytest.mark.asyncio
    async def test_the_list_route_refuses(self, broken_policy: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(h, "_autonudge_get", lambda: _FakeSvc([_loop()]))
        resp = await h.api_autonudge_list(make_mocked_request("GET", "/api/autonudge"))
        assert resp.status == 503, "a read that cannot scrub answered something other than 503"

    @pytest.mark.asyncio
    async def test_the_slot_route_refuses_even_on_an_EMPTY_slot(
        self, broken_policy: Any, monkeypatch: Any
    ) -> None:
        """The preflight sits ahead of the lookup, so an absent loop refuses too.

        An empty slot never reaches the projection, so a guard placed after the
        lookup answered 200 ``{"loop": null}`` on a host whose policy was broken --
        the one shape that looks healthy while the audited refusal is missing.
        """
        monkeypatch.setattr(h, "_autonudge_get", lambda: _FakeSvc([]))
        request = make_mocked_request("GET", "/api/autonudge/slot/chat-9-999")
        request.match_info["slot_key"] = "chat-9-999"
        resp = await h.api_autonudge_get(request)
        assert resp.status == 503, "an empty slot answered 200 on a broken policy"


class TestAWriteThatCannotScrubRefusesBeforeMutating:
    @pytest.mark.asyncio
    async def test_an_arm_is_refused_with_503(self, broken_policy: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(authz, "sel", MagicMock())
        svc = MagicMock()
        svc.add = AsyncMock()
        loop, error, status = await authz.authorize_and_add_nudge(
            svc=svc,
            state=_FakeState(),
            slot_key="chat-1-111",
            message="keep checking",
            idle_secs=300,
            source="dashboard",
        )
        assert status == 503, "an unscrubbable host armed a loop or answered 500"
        assert loop is None
        assert error == authz.SCRUB_UNAVAILABLE_SAVE_ERROR
        assert not svc.add.await_count, "the mutation ran before the refusal"

    @pytest.mark.asyncio
    async def test_a_save_is_refused_with_503(self, broken_policy: Any, monkeypatch: Any) -> None:
        """A save carrying NEITHER a message nor a banner.

        Those two branches already refuse 503 from inside their own echoed-projection
        checks. A save that touches only a cadence field reaches NEITHER, so without
        the preflight it committed, was audited as a success, and only then raised
        while the handler serialized the reply -- a 500 for work that had happened,
        which a retry applies a second time.
        """
        audit = MagicMock()
        monkeypatch.setattr(h, "sel", audit)
        monkeypatch.setattr(authz, "sel", MagicMock())
        monkeypatch.setattr(h, "_require_monitor_owner", AsyncMock(return_value=None))
        svc = MagicMock()
        svc.get_by_id = MagicMock(return_value=_loop())
        svc.update = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
        request = SimpleNamespace(
            match_info={"loop_id": "loop-abc"},
            remote="test",
            json=AsyncMock(return_value={"idle_secs": 600}),
        )
        resp = await h.api_autonudge_update(request)
        assert resp.status == 503, "an unscrubbable host saved a goal or answered 500"
        assert authz.SCRUB_UNAVAILABLE_SAVE_ERROR in resp.text
        assert '"code": "scrub_policy_unavailable"' in resp.text
        assert not svc.update.await_count, "the mutation ran before the refusal"
        logged = audit.return_value.log_tool_invocation.call_args.kwargs
        assert (logged["tool_name"], logged["outcome"]) == ("autonudge_update", "denied")


class TestAHaltIsNeverBlockedByAnUnscrubbableHost:
    """A caller that sends no loop back must still be able to stop or pause one.

    The preflight above exists because an echoing caller renders the returned
    loop, so committing first would answer 503 after a write. Two of the three
    stop callers echo nothing -- the delete route answers ``{"ok": True}`` and the
    session directive reads only the error -- and one update caller carries
    ``active=False``, a pause. Refusing those withholds no text and leaves a live
    loop nobody can halt, still waking the agent and spending its budget.
    """

    @pytest.mark.asyncio
    async def test_a_stop_that_echoes_nothing_is_not_refused(
        self, broken_policy: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(authz, "sel", MagicMock())
        svc = MagicMock()
        svc.stop_monitor = AsyncMock(return_value=_loop())
        loop, error, status = await authz.authorize_and_stop_monitor(
            svc=svc,
            loop_id="loop-abc",
            session_key="chat-1-111",
            source="mcp-directive",
        )
        assert status == 200, f"a stop was refused {status} on a host with nothing to scrub"
        assert error is None
        assert svc.stop_monitor.await_count == 1, "the monitor was never actually stopped"

    @pytest.mark.asyncio
    async def test_a_pause_that_echoes_nothing_is_not_refused(
        self, broken_policy: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(authz, "sel", MagicMock())
        svc = MagicMock()
        svc.get_by_id = MagicMock(return_value=_loop())
        svc.update = AsyncMock(return_value=_loop())
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-abc",
            active=False,
            source="mcp-directive",
        )
        assert status != 503, "a pause was refused 503 on a host with nothing to scrub"
        assert error != authz.SCRUB_UNAVAILABLE_SAVE_ERROR

    @pytest.mark.asyncio
    async def test_the_serializing_stop_route_refuses_before_mutating(
        self, broken_policy: Any, monkeypatch: Any
    ) -> None:
        """``api_monitor_stop`` renders the stopped loop, so it must refuse first.

        Without the refusal it retains the outcome, audits the stop as a success,
        and only then raises while serializing -- a 503 for work that happened.
        """
        audit = MagicMock()
        monkeypatch.setattr(h, "sel", audit)
        monkeypatch.setattr(authz, "sel", MagicMock())
        monkeypatch.setattr(h, "_require_monitor_owner", AsyncMock(return_value=None))
        svc = MagicMock()
        svc.get_by_id = MagicMock(return_value=_monitor_loop())
        svc.stop_monitor = AsyncMock(return_value=_monitor_loop())
        monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
        request = SimpleNamespace(match_info={"monitor_id": "loop-abc"}, remote="test")
        resp = await h.api_monitor_stop(request)
        assert resp.status == 503, "the serializing stop answered something other than 503"
        assert not svc.stop_monitor.await_count, "the monitor was stopped before the refusal"
        logged = audit.return_value.log_tool_invocation.call_args.kwargs
        assert (logged["tool_name"], logged["outcome"]) == ("monitor_stop", "denied")

    @pytest.mark.parametrize(
        "route, authorizer",
        [
            ("api_monitor_update", "authorize_and_update_monitor"),
            ("api_monitor_stop", "authorize_and_stop_monitor"),
            ("api_autonudge_update", "authorize_and_update_nudge"),
        ],
    )
    def test_each_serializing_route_refuses_ahead_of_its_authorizer(
        self, route: str, authorizer: str
    ) -> None:
        """The authorizers do not refuse, so the order in the route is the guard.

        A route that called its authorizer first would commit and then answer 503
        while serializing, which a retry applies twice.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(h, route))))
        lines = {
            node.func.id: node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_refuse_unscrubbable_write" in lines, f"`{route}` no longer refuses at all"
        assert (
            lines["_refuse_unscrubbable_write"] < lines[authorizer]
        ), f"`{route}` calls `{authorizer}` before refusing"


class TestTheProbeItself:
    def test_it_reports_a_broken_policy(self, broken_policy: Any) -> None:
        assert authz.scrub_policy_unavailable() is True

    def test_it_reports_a_working_policy(self) -> None:
        """The negative control: the probe must not answer True unconditionally."""
        assert authz.scrub_policy_unavailable() is False


def _wiring_tree() -> Any:
    """The parsed source of the module holding the broadcast's observer."""
    import ast
    import pathlib

    import kiro_crew

    path = pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway_runtime" / "nudge_wiring.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _enclosing_try(tree: Any, call_name: str) -> Any:
    """The innermost ``try`` holding a call to *call_name*, or None."""
    import ast

    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == call_name
                ):
                    if found is None or node.lineno > found.lineno:
                        found = node
    return found


class TestAnUnscrubbableHostStillRecordsThePatrolTransition:
    """The broadcast's scrub must not take the member event-log append down with it.

    Pinned on the source, as ``test_members_eventlog_wiring_append_ordering``
    pins the same invariant, because the observer is a closure built during
    gateway construction and reaching it needs the whole gateway stood up.

    The append -- not the live loop -- is what the Crew Members drawer reads for a
    patrol's stop reason across a restart, and it runs AFTER the payload is built.
    So an uncomposable credential policy raising inside the scrub skips the append,
    and ``reconcile_members_at_startup`` then closes the log as ``interrupted``,
    replacing a real ``runtime_budget`` permanently. A plain loop is the reachable
    case: it calls no redactor while the payload is assembled.
    """

    def test_the_scrub_is_guarded_against_an_uncomposable_policy(self) -> None:
        import ast

        guard = _enclosing_try(_wiring_tree(), "_scrub_serialized_field")
        assert guard is not None, (
            "the broadcast's scrub sits in no `try`, so an uncomposable policy "
            "raises out of the observer and skips the event-log append"
        )
        caught = {
            handler.type.id for handler in guard.handlers if isinstance(handler.type, ast.Name)
        }
        assert "PlatformCompositionError" in caught, (
            f"the scrub's guard catches {sorted(caught)}, not PlatformCompositionError, "
            "which is the error a host that cannot compose its policy raises"
        )

    def test_the_guard_does_not_hand_the_error_onward(self) -> None:
        """A bare ``raise`` in the handler restores the skipped append exactly."""
        import ast

        guard = _enclosing_try(_wiring_tree(), "_scrub_serialized_field")
        assert guard is not None
        reraises = [
            node
            for handler in guard.handlers
            for node in ast.walk(handler)
            if isinstance(node, ast.Raise)
        ]
        assert not reraises, "the guard re-raises, so the append is skipped as before"

    def test_the_guard_runs_before_the_append_it_protects(self) -> None:
        import ast

        tree = _wiring_tree()
        guard = _enclosing_try(tree, "_scrub_serialized_field")
        assert guard is not None
        appends = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_emit_patrol"
        ]
        assert appends, "`_emit_patrol` is gone; the append is no longer offloaded"
        assert guard.lineno < appends[0].lineno, (
            "the scrub's guard sits after the append, where it protects nothing: "
            "the raise it catches has already skipped that append"
        )

    def test_an_unscrubbed_frame_is_never_broadcast(self) -> None:
        """Fail closed. Catching the error must not publish the raw payload.

        The loop mutates the payload in place, so a policy failing partway leaves
        some fields scrubbed and the rest raw -- the one shape that looks handled.
        """
        import ast

        tree = _wiring_tree()
        publishers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_publish"
        ]
        assert publishers, "`_publish` is gone; the frame is broadcast somewhere else"
        guarded = [
            node
            for node in ast.walk(publishers[0])
            if isinstance(node, ast.If) and any(isinstance(b, ast.Return) for b in node.body)
        ]
        assert guarded, (
            "`_publish` broadcasts unconditionally, so a payload the scrub could "
            "not finish reaches every dashboard client raw"
        )


class TestTheSurfacesAreRegisteredInThePosture:
    def test_the_rest_inventory_is_a_declared_sink(self) -> None:
        from kiro_crew.security_posture import _REDACTION_SINKS

        names = {name for name, _path, _why in _REDACTION_SINKS}
        assert "Auto-nudge loop inventory" in names

    def test_the_sink_names_the_broadcast_it_also_covers(self) -> None:
        """The frame's rule lives in the handler, so the handler's row declares it."""
        from kiro_crew.security_posture import _REDACTION_SINKS

        why = next(w for n, _p, w in _REDACTION_SINKS if n == "Auto-nudge loop inventory")
        assert "autonudge_state" in why, "the broadcast is scrubbed but undeclared"
