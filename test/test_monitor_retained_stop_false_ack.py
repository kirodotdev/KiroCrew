"""A retained USER_STOP must not be answered with a success-like arm ack.

The retention rule itself is correct and is NOT what these tests challenge:
``_stopped_row_is_replaceable`` refuses to let a re-arm displace a
consumer-recorded stop, and ``test_monitor_clear_retained.py`` pins the
owner-only clear that is the sanctioned way out.

What these tests pin is the END-TO-END acknowledgement. The MCP tool answers the
model over its own pipe DURING the turn; ``apply_session_directive`` runs after
the turn's result is processed. So when a retained ``USER_STOP`` occupies the
binding, the arm is already certain to be refused at the turn boundary while the
model has been handed a success-shaped "requested" ack and has ended its turn.
The observable result is an agent reporting that monitoring started on PR B when
nothing is watching anything.

``TestTheRefusalIsCertain`` establishes that the later refusal is deterministic.
The tool tests then require each tool to say so IN BAND -- naming the retained
state and the recovery, and emitting NO directive -- instead of acknowledging a
request it knows cannot be applied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.autonudge import (
    AutoNudgeService,
    MonitorUpdateConflict,
    _stopped_row_is_replaceable,
)
from kiro_crew.dashboard.handlers.autonudge import _redact_monitor_value
from kiro_crew.mcp_tools import control
from kiro_crew.monitoring.models import (
    MONITOR_PUBLIC_FIELDS,
    MONITOR_STOP_INVALID_RECORD,
    MonitorBudgets,
    MonitorOutcome,
    monitor_state_public_dict,
    retained_outcome_blocks_rearm,
)

SLOT = "chat-1-123"
PR_A = "https://github.com/acme/widgets/pull/123"
PR_B = "https://github.com/acme/widgets/pull/456"


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the feature flag so an ambient ``KIROCREW_AUTONUDGE=0`` cannot decide.

    The paths exercised here do not consult ``autonudge_enabled`` today, so this
    changes no verdict; it keeps one from depending on the host's environment.
    """
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture()
def bound(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the tools believe they are on a session that can host a monitor."""
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: SLOT)
    monkeypatch.setattr(mcp_core, "_structured_monitor_binding_key", lambda sk: SLOT)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda *a, **k: "dashboard:chat-1")
    return "dashboard:chat-1"


@pytest.fixture()
def retained_user_stop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer the session-monitor endpoint the way a retained USER_STOP reads.

    This is the shape ``api_session_monitor_get`` returns for an inactive
    structured record: ``active`` false, the monitor still naming the subject the
    user stopped watching, and ``outcome`` carrying ``user_stop``. It is the same
    endpoint ``monitor_inspect`` already reads, so nothing here grants the tool a
    capability it does not have.
    """
    seen: list[str] = []

    def _get(path: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(path)
        return {
            "enabled": True,
            "active": False,
            "monitor_id": "mon-a",
            "monitor": {
                "kind": "github_pull_request",
                "target": PR_A,
                "objective": "review_ready",
                "outcome": MonitorOutcome.USER_STOP.value,
                "stopped_reason": "user_stop",
                "user_stop_reason": "watching something else now",
            },
            "autonudge_loop": None,
        }

    monkeypatch.setattr(mcp_core, "_get", _get)
    return seen


def _looks_like_success(text: str) -> bool:
    """Whether an arm ack reads as "your request was accepted"."""
    lowered = text.lower()
    if lowered.startswith("error:"):
        return False
    return "requested" in lowered


def _names_the_blocker(text: str) -> bool:
    """Whether the text identifies the retained state AND a recovery."""
    lowered = text.lower()
    identifies = "retained" in lowered or "user_stop" in lowered or "stopped" in lowered
    recovers = "clear" in lowered
    return identifies and recovers


def _emitted(text: str, tool: str = "monitor_watch") -> bool:
    """Whether the reply actually carries a directive for the applier to run.

    Prose is not the invariant -- this is. A refusal that still emitted a
    directive would read correctly to a human and arm nothing, or worse, be
    applied anyway.
    """
    return session_directive.decode(text, tool) is not None


def _watch() -> str:
    return control.monitor_watch(
        "monitor_watch",
        {"kind": "github_pull_request", "target": PR_B, "objective": "review_ready"},
    )


class TestTheRefusalIsCertain:
    """Ground truth: the later refusal is deterministic, so the ack is false."""

    @pytest.mark.asyncio
    async def test_a_user_stopped_record_refuses_the_re_arm_and_survives_it(
        self, tmp_path: Any
    ) -> None:
        """Arm PR A, user-stop it, then make the directive-shaped re-arm for PR B.

        No tool layer here on purpose: this is what the ack is measured against.
        If this ever stops raising, the ack tests below are testing nothing.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        armed = await svc.add_monitor(
            slot_key=SLOT,
            kind="github_pull_request",
            target=PR_A,
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        stopped = await svc.stop_monitor(armed.id)
        assert stopped is not None and stopped.monitor is not None
        assert stopped.monitor.outcome is MonitorOutcome.USER_STOP

        # Exactly what _monitor_watch / _monitor_start ask for at the turn boundary.
        with pytest.raises(MonitorUpdateConflict, match="retained as evidence"):
            await svc.add_monitor(
                slot_key=SLOT,
                kind="github_pull_request",
                target=PR_B,
                objective="review_ready",
                cadence_secs=60,
                budgets=MonitorBudgets(),
                replace_existing=False,
                replace_stopped=True,
            )

        # And the record the user stopped is intact: retention is not the bug.
        retained = svc.get_by_id(armed.id)
        assert retained is not None and retained.monitor is not None
        assert retained.monitor.outcome is MonitorOutcome.USER_STOP
        assert retained.monitor.target == PR_A


class TestTheToolRefusesInBandInsteadOfAcknowledging:
    """The symptom: a success-shaped ack for an arm that cannot land."""

    def test_monitor_watch_does_not_acknowledge_an_arm_it_cannot_apply(
        self, bound: str, retained_user_stop: list[str]
    ) -> None:
        out = control.monitor_watch(
            "monitor_watch",
            {"kind": "github_pull_request", "target": PR_B, "objective": "review_ready"},
        )

        assert not _emitted(out, "monitor_watch"), (
            "a refusal must carry no directive, or the applier is still asked to "
            f"arm what the text says was not armed: {out!r}"
        )
        assert not _looks_like_success(out), (
            "monitor_watch acknowledged a structured monitor on PR B while a retained "
            "USER_STOP on PR A occupies the binding, so the arm is certain to be "
            f"refused at the turn boundary. The model reads this as success: {out!r}"
        )
        assert _names_the_blocker(out), (
            "the refusal must name the retained stop and the clear that recovers it, "
            f"so the agent can act on it instead of retrying blindly: {out!r}"
        )

    def test_monitor_start_does_not_acknowledge_an_arm_it_cannot_apply(
        self, bound: str, retained_user_stop: list[str]
    ) -> None:
        out = control.monitor_start(
            "monitor_start",
            {"message": f"Watch {PR_B} and report failures", "interval_secs": 300},
        )

        assert not _emitted(out, "monitor_start"), out
        assert not _looks_like_success(out), (
            "monitor_start acknowledged a loop while a retained USER_STOP occupies "
            f"the binding: {out!r}"
        )
        assert _names_the_blocker(out), out

    def test_monitor_update_does_not_acknowledge_a_retarget_it_cannot_apply(
        self, bound: str, retained_user_stop: list[str]
    ) -> None:
        """monitor_update is the tool the agent reaches for next, and it also lies.

        The record is inactive, so ``update_monitor`` answers 404 "not found or
        already terminal" at the turn boundary -- and unlike the two arming
        directives, a refused ``monitor_update`` gets no transcript notice at all.
        """
        out = control.monitor_update("monitor_update", {"target": PR_B})

        assert not _emitted(out, "monitor_update"), out
        assert not _looks_like_success(out), (
            "monitor_update acknowledged a retarget onto PR B although the bound "
            f"record is a retained USER_STOP and cannot be updated: {out!r}"
        )
        assert _names_the_blocker(out), out

    def test_the_ack_is_computed_from_the_retained_state(
        self, bound: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root cause, stated as an equality.

        An acknowledgement that is byte-identical whether the binding holds a
        retained ``USER_STOP`` that guarantees refusal or nothing at all is not
        reporting an uncertain outcome cautiously -- it is answering a question it
        never asked, which is why such text cannot warn about a blocker it has not
        read. These two answers must differ, or the ack carries no information
        about whether the arm can land.
        """

        def _answer() -> str:
            return _watch().split("[[KIROCREW_SESSION_DIRECTIVE]]")[0]

        monkeypatch.setattr(
            mcp_core,
            "_get",
            lambda *a, **k: {
                "enabled": True,
                "active": False,
                "monitor_id": "mon-a",
                "monitor": {
                    "kind": "github_pull_request",
                    "target": PR_A,
                    "objective": "review_ready",
                    "outcome": MonitorOutcome.USER_STOP.value,
                    "stopped_reason": "user_stop",
                },
                "autonudge_loop": None,
            },
        )
        blocked = _answer()

        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: {"enabled": True, "monitor": None})
        unblocked = _answer()

        assert blocked != unblocked, (
            "monitor_watch answers identically whether or not a retained USER_STOP "
            f"occupies the binding, so its ack cannot carry the blocker: {blocked!r}"
        )

    def test_the_retained_state_is_readable_by_the_tool_layer(
        self, bound: str, retained_user_stop: list[str]
    ) -> None:
        """The preflight needs no new capability: monitor_inspect already reads it."""
        control.monitor_inspect("monitor_inspect", {})

        assert "/api/autonudge/session-monitor" in retained_user_stop


class TestThePreflightDoesNotOverRefuse:
    """A preflight stricter than the arm path would block legitimate arming."""

    @pytest.mark.parametrize("boom", [OSError("gateway down"), RuntimeError("boom")])
    def test_an_unreadable_gateway_fails_open_and_still_arms(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, boom: Exception
    ) -> None:
        """Failing CLOSED here would let one bad read block all arming."""

        def _raise(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise boom

        monkeypatch.setattr(mcp_core, "_get", _raise)

        assert _emitted(_watch())

    @pytest.mark.parametrize(
        "reading",
        [
            {"enabled": True, "monitor": None, "autonudge_loop": None},
            {"enabled": False, "monitor": None},
            {"error": "session required"},
            {"enabled": True, "active": True, "monitor": {"outcome": None, "target": PR_A}},
            {"enabled": True, "monitor": None, "autonudge_loop": {"active": True}},
            {},
        ],
    )
    def test_a_reading_that_names_no_retained_stop_still_arms(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, reading: dict[str, Any]
    ) -> None:
        """Nothing armed, a live monitor, a legacy loop, an error: none block."""
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        assert _emitted(_watch())

    @pytest.mark.parametrize(
        "outcome",
        [
            MonitorOutcome.SUCCESS,
            MonitorOutcome.BUDGET,
            MonitorOutcome.BLOCKED,
            MonitorOutcome.TARGET_UNAVAILABLE,
        ],
    )
    def test_a_system_imposed_stop_does_not_block_the_re_arm(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, outcome: MonitorOutcome
    ) -> None:
        """``replace_stopped`` displaces these, so the arm WILL land.

        Refusing them here would re-create the deadlock the re-arm opt-in exists
        to end -- the preflight must mirror the arm path, not out-strict it.
        """
        monkeypatch.setattr(
            mcp_core,
            "_get",
            lambda *a, **k: {
                "enabled": True,
                "active": False,
                "monitor": {"outcome": outcome.value, "target": PR_A, "stopped_reason": ""},
            },
        )

        assert _emitted(_watch())

    def test_a_quarantined_record_blocks_even_though_its_outcome_is_blocked(
        self, bound: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The arm path refuses it as an inspection artifact; so must the preflight."""
        monkeypatch.setattr(
            mcp_core,
            "_get",
            lambda *a, **k: {
                "enabled": True,
                "active": False,
                "monitor": {
                    "outcome": MonitorOutcome.BLOCKED.value,
                    "target": PR_A,
                    "stopped_reason": MONITOR_STOP_INVALID_RECORD,
                },
            },
        )

        assert not _emitted(_watch())


class TestTheWireContractTheRefusalDependsOn:
    """The preflight reads decoded JSON, so pin it against the REAL serializer.

    Every other test here hands the tool a hand-written reading. That proves the
    logic but not the CONTRACT: if ``outcome`` ever leaves the endpoint's public
    field set, the preflight reads ``None``, fails open, and every mocked test
    still passes while the bug is back in production. These two tests are what
    make that impossible to do quietly.
    """

    def test_the_fields_the_preflight_reads_are_public(self) -> None:
        """Dropping either field silently disables the refusal -- fail open is quiet."""
        assert "outcome" in MONITOR_PUBLIC_FIELDS
        assert "stopped_reason" in MONITOR_PUBLIC_FIELDS

    @pytest.mark.asyncio
    async def test_a_real_user_stopped_record_reaches_the_tool_as_a_refusal(
        self, bound: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """Real service state -> real endpoint serializer -> JSON -> tool refusal.

        No hand-written shape anywhere in the path, and the JSON round-trip is
        load-bearing: it proves the ``MonitorOutcome`` str-Enum survives the wire
        as a value the predicate still recognises.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        armed = await svc.add_monitor(
            slot_key=SLOT,
            kind="github_pull_request",
            target=PR_A,
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        stopped = await svc.stop_monitor(armed.id)
        assert stopped is not None and stopped.monitor is not None

        # Byte-for-byte what api_session_monitor_get returns for this record.
        reading = json.loads(
            json.dumps(
                {
                    "enabled": True,
                    "active": bool(stopped.active),
                    "monitor_id": stopped.id,
                    "monitor": _redact_monitor_value(monitor_state_public_dict(stopped.monitor)),
                    "autonudge_loop": None,
                }
            )
        )
        assert reading["monitor"]["outcome"] == "user_stop"

        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)
        out = _watch()

        assert not _emitted(out), f"a real retained USER_STOP still emitted a directive: {out!r}"
        assert not _looks_like_success(out), out
        assert _names_the_blocker(out), out
        assert PR_A in out, f"the refusal must name the retained target: {out!r}"

        # Retention is untouched by the refused call.
        surviving = svc.get_by_id(armed.id)
        assert surviving is not None and surviving.monitor is not None
        assert surviving.monitor.outcome is MonitorOutcome.USER_STOP
        assert surviving.monitor.target == PR_A


class TestTheTwoSitesDoNotDrift:
    """The agent's answer and the applier's answer come from one predicate."""

    # The truth table is DATA independent of the predicate under test. Because
    # _stopped_row_is_replaceable delegates to that predicate, comparing only the
    # two callers would be tautological: it proves the delegation is wired but not
    # that the classification is right. This explicit table pins the contract.
    REPLACEABLE = {
        MonitorOutcome.SUCCESS,
        MonitorOutcome.BLOCKED,
        MonitorOutcome.BUDGET,
        MonitorOutcome.TARGET_UNAVAILABLE,
    }
    RETAINED = {MonitorOutcome.USER_STOP, MonitorOutcome.SESSION_CLOSE}

    def test_the_table_covers_every_outcome(self) -> None:
        """A newly added outcome must be classified here, not silently defaulted."""
        assert self.REPLACEABLE | self.RETAINED == set(MonitorOutcome)
        assert not self.REPLACEABLE & self.RETAINED

    @pytest.mark.parametrize("outcome", sorted(REPLACEABLE, key=lambda o: o.value))
    def test_a_system_imposed_outcome_is_replaceable(self, outcome: MonitorOutcome) -> None:
        assert retained_outcome_blocks_rearm(outcome, "") is False

    @pytest.mark.parametrize("outcome", sorted(RETAINED, key=lambda o: o.value))
    def test_a_consumer_recorded_outcome_is_retained(self, outcome: MonitorOutcome) -> None:
        assert retained_outcome_blocks_rearm(outcome, "") is True

    @pytest.mark.parametrize("outcome", list(MonitorOutcome))
    def test_the_applier_still_agrees_with_the_predicate(self, outcome: MonitorOutcome) -> None:
        """Delegation check: the enforcement point must consume the same answer.

        Tautological on its own -- the table above is what gives it meaning. Kept
        because it is what breaks if someone re-implements the rule locally in
        ``_stopped_row_is_replaceable`` instead of delegating.
        """
        loop = SimpleNamespace(
            monitor=SimpleNamespace(outcome=outcome, stopped_reason=""),
            stopped_reason="",
        )

        assert retained_outcome_blocks_rearm(outcome, "") is not _stopped_row_is_replaceable(loop)

    def test_an_unknown_future_outcome_fails_closed(self) -> None:
        """A version that does not recognise an outcome must treat it as evidence."""
        assert retained_outcome_blocks_rearm("some_outcome_from_a_newer_gateway") is True

    def test_no_recorded_outcome_blocks_nothing(self) -> None:
        assert retained_outcome_blocks_rearm(None) is False
        assert retained_outcome_blocks_rearm("") is False


class TestThePreflightNeverBlocksTheGatewayEventLoop:
    """The gateway replays an arming handler ON ITS OWN LOOP, synchronously.

    ``derive_directive`` re-runs the handler inside the gateway process to
    intercept the directive it publishes, called without an executor from the
    aiohttp session-directive handler. A blocking loopback read issued from there
    asks the gateway for an answer only the loop now waiting on it could produce,
    so every co-hosted session stalls until the request times out.

    The replay discards the handler's text, so the preflight has nothing to say
    there and must not read at all. These tests pin the ABSENCE of that read --
    the refusal itself is still delivered by the MCP-side run, and the turn
    boundary still refuses the arm independently.
    """

    @pytest.mark.parametrize(
        "tool,raw_args",
        [
            ("monitor_start", {"message": "keep checking"}),
            (
                "monitor_watch",
                {
                    "kind": "github_pull_request",
                    "target": PR_B,
                    "objective": "review_ready",
                },
            ),
            ("monitor_update", {"max_cycles": 7}),
        ],
    )
    def test_gateway_replay_derives_without_reading_its_own_endpoint(
        self,
        bound: str,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
        raw_args: dict[str, Any],
    ) -> None:
        """The actual replay seam must derive all three tools with zero reads."""
        reads: list[str] = []

        def _self_read(path: str, **_kwargs: Any) -> dict[str, Any]:
            reads.append(path)
            raise AssertionError("gateway replay must not make a blocking self-request")

        monkeypatch.setattr(mcp_core, "_get", _self_read)

        derived = mcp_core.derive_directive(tool, raw_args, bound)

        assert derived is not None and derived[0] == tool
        assert reads == []

    def test_the_same_call_outside_capture_does_read_and_refuse(
        self, bound: str, retained_user_stop: list[str]
    ) -> None:
        """The MCP-side run still performs the advisory in-turn preflight."""
        refusal = control._retained_stop_refusal("monitor_watch", bound)

        assert retained_user_stop == ["/api/autonudge/session-monitor"]
        assert _names_the_blocker(refusal)

    def test_the_predicate_tracks_the_capture_slot(self) -> None:
        """``directive_capture_active`` is the seam the guard depends on."""
        assert mcp_core.directive_capture_active() is False

        sink: list[tuple[str, dict[str, Any]]] = []
        previous = mcp_core._DIRECTIVE_CAPTURE.get()
        mcp_core._DIRECTIVE_CAPTURE.set(sink)
        try:
            assert mcp_core.directive_capture_active() is True
        finally:
            mcp_core._DIRECTIVE_CAPTURE.set(previous)

        assert mcp_core.directive_capture_active() is False
