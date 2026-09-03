"""Contract tests for the stateless session-directive tools (issue #755).

``monitor_start`` / ``monitor_update`` / ``autonudge_stop`` no longer resolve a
session identity or make HTTP calls. Each VALIDATES its arguments and returns a
DIRECTIVE string — a human-readable confirmation plus an opaque marker carrying
the validated payload (and NO session key). The session-aware consumer
(``dashboard.chat_runner._run_chat``'s ``EVENT_TOOL_RESULT`` handler) decodes the
marker and applies the effect against ITS OWN slot via
``dashboard.session_directive_apply.apply_session_directive``.

The tests split along that seam:

* **Tool contract** — call the tool via ``_call_tool_inner`` and assert the
  returned directive decodes to the expected validated payload. The tool
  short-circuits with a plain "only works from … dashboard, Slack, or Discord"
  message (NO directive) ONLY when the strict resolver returns a non-empty but
  non-nudge-able key (``cron:``/``subagent:`` …); on a default install the
  resolver returns ``""`` and the tool DOES emit a directive.
* **Applier invariants** — call ``apply_session_directive`` with a fake
  AutoNudge service and fake state/slot, preserving the security invariants
  that used to live inside the tool: capped-loop refusal, paused-loop
  protection, and ownership by the session binding key (never a caller-supplied
  loop id).

The former mock-dashboard HTTP server, user-token handshake, and
arm-failure/lost-response recheck tests are gone: that logic no longer exists —
the tools are stateless and the loop mutation happens in-process in the applier.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew import autonudge_authz, session_directive
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    AUTONUDGE_STOP_REASON,
    MONITOR_TERMINAL_REASON,
    AutoNudgeService,
    binding_key_for,
)
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.mcp_core import _call_tool_inner
from kiro_crew.mcp_tools._limits import _MONITOR_DEFAULT_MAX_CYCLES
from kiro_crew.validation import ValidationError

# ── Tool-contract fixtures ────────────────────────────────────────────────────


@pytest.fixture()
def default_install(monkeypatch):
    """Default install: the strict resolver has no accepted identity source and
    returns a dashboard identity, so the monitor tools can emit a directive.
    (Pooling off, unsandboxed, kiro-cli backend.)"""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1-1")
    return monkeypatch


# ── monitor_start ──


def test_monitor_start_returns_directive_with_validated_payload(default_install):
    """A valid call returns a directive decoding to the validated payload with
    interval_secs mapped to idle_secs."""
    result = _call_tool_inner(
        "monitor_start",
        {"message": "check PR #1 until green", "interval_secs": 300, "max_cycles": 5},
    )
    args = session_directive.decode(result, "monitor_start")
    assert args == {
        "message": "check PR #1 until green",
        "idle_secs": 300,
        "max_cycles": 5,
        "max_runtime_secs": 0,
        # Whether the loop may be observation-gated. Always present and True
        # unless the caller opted out, so whichever surface applies this
        # directive reads the same decision the ack reported.
        "gate": True,
    }


def test_monitor_start_runtime_budget_passes_through(default_install):
    """An explicit wall-clock budget lands in the directive payload and is
    echoed in the confirmation; omitting it defaults to 0 (unlimited)."""
    result = _call_tool_inner(
        "monitor_start",
        {"message": "watch CI", "max_runtime_secs": 7200},
    )
    args = session_directive.decode(result, "monitor_start")
    assert args["max_runtime_secs"] == 7200
    assert "7200s" in result


def test_monitor_start_defaults_interval_300_and_bounded_cap(default_install):
    """Omitting interval_secs defaults to 300; omitting max_cycles defaults to a
    BOUNDED cap (24) — never an unbounded loop."""
    result = _call_tool_inner("monitor_start", {"message": "watch CI"})
    args = session_directive.decode(result, "monitor_start")
    assert args["idle_secs"] == 300
    assert args["max_cycles"] == _MONITOR_DEFAULT_MAX_CYCLES
    assert args["max_cycles"] == 24
    assert "no cycle cap" not in result.lower()


def test_monitor_start_explicit_zero_cap_stays_zero(default_install):
    """An explicit 0 means the caller really wants unlimited — 0 stays 0 and the
    confirmation says so."""
    result = _call_tool_inner("monitor_start", {"message": "watch PR", "max_cycles": 0})
    assert session_directive.decode(result, "monitor_start")["max_cycles"] == 0
    assert "no cycle cap" in result.lower()


def test_monitor_start_interval_maps_to_idle_secs(default_install):
    result = _call_tool_inner("monitor_start", {"message": "watch", "interval_secs": 900})
    assert session_directive.decode(result, "monitor_start")["idle_secs"] == 900


def test_monitor_start_confirmation_states_idle_semantics_and_stop_duty(default_install):
    """The human confirmation must state the deadline-preserving cadence (user
    messages defer, never restart) and put the stop obligation on the caller,
    framing the cap as a backstop."""
    result = _call_tool_inner("monitor_start", {"message": "watch PR", "interval_secs": 300})
    assert "every 300s" in result.lower()
    assert "without restarting" in result.lower()
    assert "autonudge_stop" in result
    assert "backstop" in result.lower()


def test_monitor_start_short_circuits_for_non_nudgeable_session(monkeypatch):
    """A non-empty but non-nudge-able key (cron/subagent) yields a plain refusal
    and NO directive."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "cron:job-9")
    result = _call_tool_inner("monitor_start", {"message": "watch"})
    assert "only works" in result.lower()
    assert session_directive.decode(result, "monitor_start") is None


# ── monitor_update ──


def test_monitor_update_returns_patch_directive_with_mapped_fields(default_install):
    """A revision returns a directive whose patch contains only the changed
    fields, with interval_secs mapped to idle_secs."""
    result = _call_tool_inner(
        "monitor_update",
        {"message": "PR moved on — now check the Coverage Gate only", "max_cycles": 40},
    )
    patch = session_directive.decode(result, "monitor_update")["patch"]
    assert patch == {
        "message": "PR moved on — now check the Coverage Gate only",
        "max_cycles": 40,
    }
    # Untouched fields are omitted, not defaulted over.
    assert "idle_secs" not in patch


def test_monitor_update_interval_maps_to_idle_secs(default_install):
    result = _call_tool_inner("monitor_update", {"interval_secs": 900})
    assert session_directive.decode(result, "monitor_update")["patch"] == {"idle_secs": 900}


def test_monitor_update_runtime_budget_passes_through(default_install):
    """A revised wall-clock budget lands in the patch; untouched fields are
    omitted, not defaulted over."""
    result = _call_tool_inner("monitor_update", {"max_runtime_secs": 3600})
    assert session_directive.decode(result, "monitor_update")["patch"] == {"max_runtime_secs": 3600}


def test_monitor_update_empty_patch_returns_plain_message_no_directive(default_install):
    """A no-field call is a plain 'nothing to change' message — no directive."""
    result = _call_tool_inner("monitor_update", {})
    assert "nothing to change" in result.lower()
    assert session_directive.decode(result, "monitor_update") is None


def test_monitor_update_rejects_blank_message(default_install):
    """A whitespace-only message would blank the instruction — refuse it, and
    emit no directive."""
    result = _call_tool_inner("monitor_update", {"message": "   "})
    assert "must not be empty" in result.lower()
    assert session_directive.decode(result, "monitor_update") is None


def test_monitor_update_exposes_no_loop_id_parameter(default_install):
    """OWNERSHIP (schema level): the tool exposes NO loop-id parameter, so a
    model-supplied id is rejected by the schema and can never reach the applier
    to target another session's loop."""
    with pytest.raises(ValidationError, match="loop_id"):
        _call_tool_inner("monitor_update", {"message": "x", "loop_id": "someone-elses-loop"})


def test_monitor_update_short_circuits_for_non_nudgeable_session(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "subagent:abc")
    result = _call_tool_inner("monitor_update", {"message": "x"})
    assert "only works" in result.lower()
    assert session_directive.decode(result, "monitor_update") is None


# ── autonudge_stop ──


def test_autonudge_stop_returns_directive_with_stripped_reason(default_install):
    result = _call_tool_inner("autonudge_stop", {"reason": "  PR is green  "})
    assert session_directive.decode(result, "autonudge_stop") == {"reason": "PR is green"}


def test_autonudge_stop_empty_reason_yields_empty_string(default_install):
    result = _call_tool_inner("autonudge_stop", {})
    assert session_directive.decode(result, "autonudge_stop") == {"reason": ""}


def test_autonudge_stop_short_circuits_for_non_nudgeable_session(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "cron:job-1")
    result = _call_tool_inner("autonudge_stop", {"reason": "x"})
    assert "only works" in result.lower()
    assert session_directive.decode(result, "autonudge_stop") is None


# ── Applier invariants (dashboard.session_directive_apply) ────────────────────
#
# These preserve the security invariants that used to live inside the tool,
# moved to the consumer that actually mutates loop state. The applier resolves
# the loop by ``svc.get_by_slot(binding_key_for(session_key))`` and calls the
# authz cores; the fakes below record those calls without touching a real
# AutoNudge service. The authz helpers are imported LAZILY inside the applier
# from ``kiro_crew.autonudge`` / ``kiro_crew.autonudge_authz``, so they are
# patched on those modules (not on session_directive_apply).


class _FakeLoop:
    """Prompt-loop double.

    ``gate`` mirrors ``NudgeLoop.gate``: a prompt loop carries probe state in
    ``monitor`` only when it is gated, and ``is_structured_monitor_loop`` reads
    ``monitor is not None and not gate`` to tell a controller-owned structured
    monitor apart from a prompt loop. A double that sets ``monitor`` without
    ``gate=True`` is therefore routed to the structured ``monitor_update`` path,
    which refuses ``message``/``max_cycles``/``active`` as legacy fields.
    """

    def __init__(
        self,
        loop_id,
        *,
        cycle_count=0,
        max_cycles=0,
        active=True,
        created_ts=0.0,
        max_runtime_secs=0,
        stopped_reason="",
        slot_key="",
        monitor=None,
        gate=False,
    ):
        self.id = loop_id
        self.cycle_count = cycle_count
        self.max_cycles = max_cycles
        self.active = active
        self.created_ts = created_ts
        self.max_runtime_secs = max_runtime_secs
        self.stopped_reason = stopped_reason
        self.slot_key = slot_key
        self.monitor = monitor
        self.gate = gate


class _FakeMonitor:
    """The two monitor fields the paused-loop branch reads about a subject.

    ``terminal_pending`` is the owed final turn (``"success"``/``"blocked"``) a
    channel loop records on observing a terminal subject; ``outcome`` is the
    settled classification written once that turn lands.
    """

    def __init__(self, *, terminal_pending="", outcome=None):
        self.terminal_pending = terminal_pending
        self.outcome = outcome


class _FakeSvc:
    """Minimal AutoNudge service double for session-directive mutations."""

    def __init__(self, loop=None, *, all_loops=None):
        self._loop = loop
        self._all = list(all_loops) if all_loops is not None else ([loop] if loop else [])
        self.get_by_slot_keys: list[str] = []
        self.removed: list[str] = []
        self.updated: list[tuple[str, dict]] = []

    def get_by_slot(self, key):
        self.get_by_slot_keys.append(key)
        return self._loop

    def list_all(self):
        return list(self._all)

    async def remove(self, loop_id):
        self.removed.append(loop_id)

    async def update(self, loop_id, **patch):
        self.updated.append((loop_id, patch))
        return self._loop


def _fake_state():
    return object()


def _fake_slot(*, key="chat-3-1700000000", app=""):
    class _Slot:
        pass

    slot = _Slot()
    slot.key = key
    slot._app = app
    return slot


def _install_svc(monkeypatch, svc):
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)


def _record_add(monkeypatch, *, loop=None, error=None):
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return (loop or _FakeLoop("loop-new"), error, "ok")

    monkeypatch.setattr("kiro_crew.autonudge_authz.authorize_and_add_nudge", _fake)
    return calls


def _record_update(monkeypatch, *, loop=None, error=None):
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return (loop or _FakeLoop("loop-updated"), error, "ok")

    monkeypatch.setattr("kiro_crew.autonudge_authz.authorize_and_update_nudge", _fake)
    return calls


_SESSION = "dashboard:chat-3-1700000000"
_RESEARCH_SESSION = "dashboard:research-a1b2c3d4"


def test_applier_ack_discloses_the_gated_cadence(monkeypatch):
    """A gated loop must not be acknowledged with an every-interval promise.

    This applier defaults ``gate`` to True, so the unconditional "re-injects every
    {idle_secs}s" was wrong for its own default: a quiet tick on a gated loop spends no
    turn at all. The MCP tool's ack already disclosed this; the dashboard directive
    applier did not, and the pull request's own description claims the arming surface
    says so.

    The cadence is read off the ARMED loop rather than the request, because this surface
    knows what the tool has to infer -- whether a monitor was actually attached.
    """
    from kiro_crew.monitoring.models import MonitorState

    armed = _FakeLoop("loop-gated")
    armed.monitor = MonitorState(
        kind="gh-pr",
        target="acme/widgets#42",
        objective="watch until green",
        created_ts=0.0,
    )
    armed.gate = True
    svc = _FakeSvc()
    _install_svc(monkeypatch, svc)
    _record_add(monkeypatch, loop=armed)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_start",
            {"message": "watch https://github.com/acme/widgets/pull/42", "idle_secs": 300},
        )
    )
    assert "only when it changes" in result, "the ack must state the gated cadence"
    assert "acme/widgets#42" in result, "and name the subject it is watching"
    assert "message re-injects every 300s" not in result, "not the plain promise"


def test_applier_ack_keeps_the_plain_promise_for_an_ungated_loop(monkeypatch):
    """An ungated loop DOES re-inject every interval, so its ack must still say so."""
    plain = _FakeLoop("loop-plain")
    plain.monitor = None
    plain.gate = False
    svc = _FakeSvc()
    _install_svc(monkeypatch, svc)
    _record_add(monkeypatch, loop=plain)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_start",
            {"message": "keep checking", "idle_secs": 300, "gate": False},
        )
    )
    assert "re-injects every 300s" in result
    assert "only when it changes" not in result


def test_applier_monitor_start_arms_via_the_session_binding_key(monkeypatch):
    """monitor_start arms the loop through the authz core keyed on the session's
    binding key — never anything the caller supplied."""
    svc = _FakeSvc()
    _install_svc(monkeypatch, svc)
    add_calls = _record_add(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_start",
            {"message": "watch", "idle_secs": 300, "max_cycles": 5},
        )
    )
    assert len(add_calls) == 1
    call = add_calls[0]
    assert call["slot_key"] == binding_key_for(_SESSION)
    assert call["message"] == "watch"
    assert call["idle_secs"] == 300
    assert call["max_cycles"] == 5
    assert "started" in result.lower()


def test_applier_resolves_loop_by_binding_key_never_a_supplied_id(monkeypatch):
    """OWNERSHIP: the applier resolves the target loop from the session binding
    key via get_by_slot; a stray id in the directive args is ignored and the
    patched loop id is the resolved one."""
    loop = _FakeLoop("mine-1", cycle_count=3, max_cycles=24, active=True)
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch, loop=loop)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            # A hostile payload cannot smuggle a loop id past the applier.
            {"patch": {"message": "x"}, "loop_id": "someone-elses-loop"},
        )
    )
    assert svc.get_by_slot_keys == [binding_key_for(_SESSION)]
    assert len(update_calls) == 1
    assert update_calls[0]["loop_id"] == "mine-1"
    assert "updated" in result.lower()


def test_applier_monitor_update_refuses_cap_at_or_below_cycle_count(monkeypatch):
    """CAPPED-LOOP: a cap at/below the delivered cycle count deactivates the loop
    without firing again, so it is refused — no update call."""
    svc = _FakeSvc(_FakeLoop("loop-7", cycle_count=12, max_cycles=24, active=True))
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update", {"patch": {"max_cycles": 12}}
        )
    )
    assert "at or below" in result
    assert "12" in result
    assert not update_calls


def test_applier_monitor_update_refuses_spent_runtime_budget(monkeypatch):
    """SPENT-BUDGET: a wall-clock budget at/below the loop's elapsed age would
    deactivate it on the next timer without firing again, so it is refused —
    same shape as the cycle-cap guard. A larger budget passes through."""
    import time as _time

    armed_two_hours_ago = _time.time() - 7200
    loop = _FakeLoop(
        "loop-8", cycle_count=3, max_cycles=24, active=True, created_ts=armed_two_hours_ago
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch, loop=loop)
    # 3600s budget on a loop already 7200s old → refused, no update call.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_runtime_secs": 3600}},
        )
    )
    assert "at or below" in result
    assert not update_calls
    # A budget beyond the elapsed age is applied.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_runtime_secs": 86400}},
        )
    )
    assert len(update_calls) == 1
    assert update_calls[0]["max_runtime_secs"] == 86400
    assert "updated" in result.lower()


def test_applier_monitor_update_refuses_to_resume_a_paused_loop(monkeypatch):
    """PAUSED-LOOP: an inactive loop that did NOT stop at its cap is not resumed
    as a side effect of a metadata edit — refused, no update call."""
    svc = _FakeSvc(_FakeLoop("loop-paused", cycle_count=3, max_cycles=24, active=False))
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"message": "revised"}},
        )
    )
    assert "PAUSED" in result
    assert "will not resume" in result
    assert not update_calls


def test_applier_monitor_update_revives_a_capped_loop_only_when_cap_is_raised(monkeypatch):
    """PAUSED-LOOP: a cap-stopped loop IS revived when the cap is actually
    raised — active=True is injected into the patch and the loop is re-armed."""
    svc = _FakeSvc(_FakeLoop("loop-capped", cycle_count=24, max_cycles=24, active=False))
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update", {"patch": {"max_cycles": 40}}
        )
    )
    assert len(update_calls) == 1
    assert update_calls[0]["max_cycles"] == 40
    assert update_calls[0]["active"] is True
    assert "re-armed" in result


def test_applier_monitor_update_revives_a_budget_stopped_loop_on_budget_raise(monkeypatch):
    """PAUSED-LOOP symmetry (design-review on #2116): a loop stopped by its
    wall-clock budget gets the SAME agent-side recovery as a cap-stopped one —
    raising the budget above the loop's elapsed age revives it. Keyed on the
    persisted stopped_reason, not elapsed-time inference."""
    import time as _time

    loop = _FakeLoop(
        "loop-budget",
        cycle_count=5,
        max_cycles=24,
        active=False,
        created_ts=_time.time() - 7200,
        max_runtime_secs=3600,
        stopped_reason="runtime_budget",
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch, loop=loop)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_runtime_secs": 86400}},
        )
    )
    assert len(update_calls) == 1
    assert update_calls[0]["max_runtime_secs"] == 86400
    assert update_calls[0]["active"] is True
    assert "re-armed" in result


def test_applier_manual_pause_is_never_revived_by_a_budget_raise(monkeypatch):
    """GPT P1 repro on #2116: pause a loop manually, let wall-clock pass its
    budget, then raise max_runtime_secs — the loop must STAY paused. Elapsed
    time cannot distinguish a pause from an expiry; only the persisted
    stopped_reason can, and 'manual' never auto-resumes."""
    import time as _time

    loop = _FakeLoop(
        "loop-paused-budget",
        cycle_count=5,
        max_cycles=24,
        active=False,
        created_ts=_time.time() - 7200,
        max_runtime_secs=3600,
        stopped_reason="manual",
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_runtime_secs": 86400}},
        )
    )
    assert not update_calls, "a manual pause must not be resumed by a budget raise"
    assert "paused manually" in result


def test_applier_approval_stalled_denial_names_the_authorization(monkeypatch):
    """A stall is not revivable by raising a bound, and must not be mislabelled.

    Raising a cap or budget cannot restore an authorization, so this stays in the
    deny path — but the generic 'paused manually' wording would send the agent to
    ask a human who already answered by letting the grant lapse.
    """
    loop = _FakeLoop(
        "loop-stalled",
        cycle_count=3,
        max_cycles=24,
        active=False,
        stopped_reason=APPROVAL_STALL_REASON,
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_cycles": 48}},
        )
    )
    assert not update_calls, "raising a cap must not resume a loop that lost approval"
    assert "approval prompt" in result
    assert "auto-approve" in result
    assert "paused manually" not in result


def test_applier_monitor_update_budget_stopped_denial_names_the_budget(monkeypatch):
    """When a budget-stopped loop is NOT being revived, the refusal must name
    the bound that stopped it — not send the agent chasing max_cycles."""
    import time as _time

    loop = _FakeLoop(
        "loop-budget2",
        cycle_count=5,
        max_cycles=24,
        active=False,
        created_ts=_time.time() - 7200,
        max_runtime_secs=3600,
        stopped_reason="runtime_budget",
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"message": "revised"}},
        )
    )
    assert not update_calls
    assert "wall-clock" in result and "max_runtime_secs" in result
    assert "max_cycles" not in result


def test_a_probe_state_double_is_a_legacy_loop_only_while_it_carries_the_gate():
    """Pin ``_FakeLoop``'s premise against the production predicate itself.

    ``monitor_update`` splits on record KIND before it reads a single bound, and a
    ``monitor`` object alone does not decide that kind. So every paused-loop test
    below depends on ``gate=True`` putting its double on the LEGACY side — a
    dependency the class docstring states but nothing executes, so an edit to
    ``is_structured_monitor_loop`` reports itself only as five assertion failures
    against a refusal string that names neither the flag nor the routing. This one
    fails alongside them naming the predicate, so the batch has a cause in it.
    """
    from kiro_crew.autonudge import is_structured_monitor_loop

    gated_prompt_loop = _FakeLoop("loop-gated", monitor=_FakeMonitor(), gate=True)
    controller_record = _FakeLoop("loop-structured", monitor=_FakeMonitor())
    plain_prompt_loop = _FakeLoop("loop-plain")

    assert not is_structured_monitor_loop(gated_prompt_loop)
    assert is_structured_monitor_loop(controller_record)
    assert not is_structured_monitor_loop(plain_prompt_loop)


def test_applier_owed_terminal_turn_is_not_reported_as_a_spent_cap(monkeypatch):
    """A channel loop whose subject MERGED must not be told it ran out of cycles.

    A channel-bound loop does not settle on observation: the probe records the owed
    final turn in ``monitor.terminal_pending`` and leaves the loop active with no
    ``outcome``. If that turn is refused (a busy thread) and the retry finds the cap
    spent, the loop deactivates with ``stopped_reason="cycle_cap"`` before the
    settlement that would promote the debt ever runs. Reading the reason alone then
    contradicts a fact already durably on disk — and because the cap is also being
    raised here, the loop would be REVIVED to watch a subject that already merged,
    which is the wasted fresh loop this costs.
    """
    loop = _FakeLoop(
        "loop-owed-merged",
        cycle_count=24,
        max_cycles=24,
        active=False,
        stopped_reason="cycle_cap",
        slot_key="slack:C123:170.5",
        monitor=_FakeMonitor(terminal_pending="success"),
        gate=True,
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_cycles": 48}},
        )
    )
    assert not update_calls, "a terminal subject must not be re-armed by a cap raise"
    assert "cycle cap" not in result, result
    assert "merged" in result
    assert "monitor_start" in result


def test_applier_owed_blocked_turn_is_not_reported_as_a_merge(monkeypatch):
    """A subject closed WITHOUT merging is terminal but is not good news.

    It stopped on a question the operator has to answer — reopen, or abandon — so it
    must not be worded as a finish. The debt carries the distinction in the same
    vocabulary the settled outcome uses (``success``/``blocked``).
    """
    loop = _FakeLoop(
        "loop-owed-closed",
        cycle_count=24,
        max_cycles=24,
        active=False,
        stopped_reason="cycle_cap",
        slot_key="slack:C123:170.5",
        monitor=_FakeMonitor(terminal_pending="blocked"),
        gate=True,
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_cycles": 48}},
        )
    )
    assert not update_calls
    assert "cycle cap" not in result, result
    assert "without merging" in result
    assert "merged" not in result.replace("without merging", "")


def test_applier_settled_terminal_loop_is_not_reported_as_a_manual_pause(monkeypatch):
    """A SETTLED terminal loop had no branch here either, so it fell to the
    generic wording and was announced as if a human had pressed Stop."""
    from kiro_crew.monitoring.models import MonitorOutcome

    loop = _FakeLoop(
        "loop-settled",
        cycle_count=7,
        max_cycles=24,
        active=False,
        stopped_reason=MONITOR_TERMINAL_REASON,
        monitor=_FakeMonitor(outcome=MonitorOutcome.SUCCESS),
        gate=True,
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"message": "revised"}},
        )
    )
    assert not update_calls
    assert "paused manually" not in result, result
    assert "merged" in result


def test_applier_a_settled_outcome_outranks_a_stale_owed_turn(monkeypatch):
    """The settled ``outcome`` is authoritative; the debt is only the fallback.

    Both fields can be populated at once — the settlement writes ``outcome`` and
    clears the debt in the same pass — so a reader that preferred the debt could
    announce a stale classification.
    """
    from kiro_crew.monitoring.models import MonitorOutcome

    loop = _FakeLoop(
        "loop-both",
        cycle_count=24,
        max_cycles=24,
        active=False,
        stopped_reason="cycle_cap",
        monitor=_FakeMonitor(terminal_pending="success", outcome=MonitorOutcome.BLOCKED),
        gate=True,
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_cycles": 48}},
        )
    )
    assert not update_calls
    assert "without merging" in result


def test_applier_a_spent_cap_with_no_terminal_news_still_revives(monkeypatch):
    """Control: the terminal carve-out must not swallow a genuine cap.

    A cap-stopped loop with a monitor that saw nothing terminal keeps the revival
    affordance a raised cap is supposed to give it.
    """
    loop = _FakeLoop(
        "loop-plain-cap",
        cycle_count=24,
        max_cycles=24,
        active=False,
        stopped_reason="cycle_cap",
        monitor=_FakeMonitor(),
        gate=True,
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch, loop=loop)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"max_cycles": 48}},
        )
    )
    assert len(update_calls) == 1
    assert update_calls[0]["active"] is True
    assert "re-armed" in result


def test_applier_monitor_update_without_a_loop_is_a_clean_noop(monkeypatch):
    """No loop bound to this session -> nothing to update, no authz call."""
    svc = _FakeSvc(None)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update", {"patch": {"message": "x"}}
        )
    )
    assert "no active monitor loop" in result.lower()
    assert svc.get_by_slot_keys == [binding_key_for(_SESSION)]
    assert not update_calls


def test_applier_autonudge_stop_records_tombstone_for_loop_resolved_by_binding(monkeypatch):
    """Research Lab retains source-owned stop evidence for its watchdog."""
    svc = _FakeSvc(_FakeLoop("loop-1"))
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(key="research-a1b2c3d4", app="auto-research"),
            _RESEARCH_SESSION,
            "autonudge_stop",
            {"reason": "done"},
        )
    )
    assert svc.get_by_slot_keys == [binding_key_for(_RESEARCH_SESSION)]
    assert svc.updated == [("loop-1", {"active": False, "stopped_reason": AUTONUDGE_STOP_REASON})]
    assert svc.removed == []
    assert "stopped" in result.lower()
    assert "done" in result


def test_applier_autonudge_stop_removes_ordinary_monitor_loop(monkeypatch):
    """Loops without a tombstone consumer retain the historical remove UX."""
    svc = _FakeSvc(_FakeLoop("loop-ordinary"))
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert svc.get_by_slot_keys == [binding_key_for(_SESSION)]
    assert svc.removed == ["loop-ordinary"]
    assert svc.updated == []
    assert "stopped" in result.lower()


@pytest.mark.parametrize(
    "session_key",
    (
        "dashboard:research-notes",
        "dashboard:research-a1b2c3d",
        "dashboard:research-a1b2c3d4-extra",
        "dashboard:research-A1B2C3D4",
    ),
)
def test_applier_autonudge_stop_removes_research_prefix_lookalikes(monkeypatch, session_key):
    """App provenance cannot turn a non-canonical lookalike into a worker."""
    svc = _FakeSvc(_FakeLoop("loop-lookalike"))
    _install_svc(monkeypatch, svc)

    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(key=binding_key_for(session_key), app="auto-research"),
            session_key,
            "autonudge_stop",
            {"reason": "done"},
        )
    )

    assert svc.get_by_slot_keys == [binding_key_for(session_key)]
    assert svc.removed == ["loop-lookalike"]
    assert svc.updated == []
    assert "stopped" in result.lower()


def test_applier_autonudge_stop_removes_canonical_user_named_slot(monkeypatch):
    """A canonical-looking name is not proof of Research Lab ownership."""
    svc = _FakeSvc(_FakeLoop("loop-user-named"))
    _install_svc(monkeypatch, svc)

    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(key="research-a1b2c3d4"),
            _RESEARCH_SESSION,
            "autonudge_stop",
            {"reason": "done"},
        )
    )

    assert svc.get_by_slot_keys == [binding_key_for(_RESEARCH_SESSION)]
    assert svc.removed == ["loop-user-named"]
    assert svc.updated == []
    assert "stopped" in result.lower()


@pytest.mark.asyncio
async def test_applier_autonudge_stop_tombstone_survives_api_retry_and_restart(
    tmp_path, monkeypatch
):
    """A reasonless inactive API retry cannot erase source stop evidence.

    The Research Lab watchdog may observe the record only after the worker turn
    exits or after a gateway restart, so both boundaries must retain it.
    """
    svc1 = AutoNudgeService(base_dir=tmp_path)
    await svc1.start()
    binding = binding_key_for(_RESEARCH_SESSION)
    loop = await svc1.add(slot_key=binding, message="watch", idle_secs=60)
    # Simulate an app-disable pause racing ahead of the worker's source stop.
    # A deliberate stop must replace the manual reason so re-enable cannot
    # revive work the worker already finished.
    await svc1.update(loop.id, active=False)
    _install_svc(monkeypatch, svc1)

    await apply_session_directive(
        _fake_state(),
        _fake_slot(key="research-a1b2c3d4", app="auto-research"),
        _RESEARCH_SESSION,
        "autonudge_stop",
        {"reason": "goal met"},
    )
    stopped = svc1.get_by_slot(binding)
    assert stopped is not None
    assert stopped.id == loop.id
    assert stopped.active is False
    assert stopped.stopped_reason == AUTONUDGE_STOP_REASON

    audit = type("Audit", (), {"log_tool_invocation": lambda self, **_kwargs: None})()
    monkeypatch.setattr(autonudge_authz, "sel", lambda: audit)
    updated, error, status = await autonudge_authz.authorize_and_update_nudge(
        svc=svc1,
        loop_id=loop.id,
        active=False,
        source="dashboard",
    )
    assert error is None
    assert status == 200
    assert updated is stopped
    assert stopped.stopped_reason == AUTONUDGE_STOP_REASON
    assert loop.id not in svc1._timers
    svc1.stop()

    svc2 = AutoNudgeService(base_dir=tmp_path)
    await svc2.start()
    restored = svc2.get_by_slot(binding)
    assert restored is not None
    assert restored.id == loop.id
    assert restored.active is False
    assert restored.stopped_reason == AUTONUDGE_STOP_REASON
    assert loop.id not in svc2._timers
    svc2.stop()


def test_applier_autonudge_stop_no_loop_is_a_clean_noop(monkeypatch):
    svc = _FakeSvc(None)
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert "nothing to stop" in result.lower()
    assert svc.removed == []
    assert svc.updated == []


def test_applier_autonudge_stop_reports_a_binding_miss_instead_of_success(monkeypatch):
    """A loop active on ANOTHER slot is a lookup miss, not an idempotent success.

    ``get_by_slot`` resolves only the calling session's binding, so a loop armed
    against a different slot key is unreachable here. The result must say
    nothing was stopped and name this session's own binding — and it must not
    remove or pause the loop it could not resolve.
    """
    elsewhere = _FakeLoop("loop-elsewhere", slot_key="chat-99-1700009999")
    svc = _FakeSvc(None, all_loops=[elsewhere])
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert "nothing to stop" not in result.lower()
    assert "NOTHING WAS STOPPED" in result
    assert binding_key_for(_SESSION) in result
    assert "1 auto-nudge loop(s) are running on other sessions" in result
    assert svc.removed == []
    assert svc.updated == []


def test_applier_autonudge_stop_miss_names_no_other_session_identifier(monkeypatch):
    """OWNERSHIP: the miss diagnostic reports a COUNT, never an id or slot key.

    The stop tool exposes no loop-id parameter so a session cannot target
    another session's loop; a message naming other sessions' loops would hand a
    model the identifiers that schema withholds. Cross-session enumeration
    belongs to the token-authed dashboard API, not to a tool result.
    """
    # Slot keys deliberately disjoint from the CALLER's own binding: the message
    # prints that legitimately, so an overlapping fixture would fail on the
    # caller's own identity rather than on a leak.
    loops = [_FakeLoop(f"loop-{n}", slot_key=f"chat-9{n}-1700009999") for n in range(4)]
    svc = _FakeSvc(None, all_loops=loops)
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert "4 auto-nudge loop(s) are running on other sessions" in result
    for lp in loops:
        assert lp.id not in result
        assert lp.slot_key not in result
    # No dead-end remedy: the message must not advertise a route whose path it
    # does not print, and a loop's sentinel path can legitimately be empty.
    assert "stop_sentinel_path" not in result
    assert svc.removed == []


def test_applier_autonudge_stop_ignores_inactive_loops_in_the_miss_diagnostic(monkeypatch):
    """A deactivated loop fires no nudges, so it is not evidence of a miss.

    Only active loops make the difference between "nothing exists" and "the
    lookup failed"; a paused or tombstoned loop keeps the plain no-loop answer.
    """
    svc = _FakeSvc(None, all_loops=[_FakeLoop("loop-dead", active=False, slot_key="chat-99-1")])
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert "nothing to stop" in result.lower()
    assert "loop-dead" not in result
    assert svc.removed == []
    assert svc.updated == []


# The miss message is asserted here as a LITERAL, not rebuilt from the applier's
# own f-string: the diagnostic below must stay server-side, so a change that
# leaks a slot key into the model's tool result has to fail this comparison
# rather than be recomputed into agreement with itself.
_MISS_MESSAGE = (
    "NOTHING WAS STOPPED. No auto-nudge loop is bound to this session "
    "(binding: chat-3-1700000000), but 2 auto-nudge loop(s) are running on "
    "other sessions. A loop can only be stopped from the session it is bound "
    "to, so this call could not reach them."
)


def test_applier_autonudge_stop_miss_logs_caller_binding_and_active_slot_keys(monkeypatch, caplog):
    """The miss branch records the pair that identifies WHICH miss this is.

    A miss has two possible causes — a slot-key spelling the lookup does not
    model, or an arming path that registered a key the session later resolves
    differently — and they are told apart only by the caller's resolved binding
    next to the slot keys the store actually holds. Nothing else captures that
    pair, so the diagnostic has to be emitted where the miss is detected.

    Server-side ONLY: the log carries the slot keys, the returned message must
    stay byte-identical and keep reporting a count.
    """
    loops = [
        _FakeLoop("loop-a", slot_key="chat-91-1700009991"),
        _FakeLoop("loop-b", slot_key="slack:T1/C2/1700009992"),
    ]
    svc = _FakeSvc(None, all_loops=loops)
    _install_svc(monkeypatch, svc)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_directive_apply"):
        result = asyncio.run(
            apply_session_directive(
                _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
            )
        )

    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "kiro_crew.dashboard.session_directive_apply"
    ]
    assert len(warnings) == 1
    logged = warnings[0].getMessage()
    assert binding_key_for(_SESSION) in logged
    for lp in loops:
        assert lp.slot_key in logged

    # The model-visible half is unchanged, and the keys stay out of it.
    assert result == _MISS_MESSAGE
    for lp in loops:
        assert lp.slot_key not in result
    assert svc.removed == []
    assert svc.updated == []


def test_applier_autonudge_stop_logs_nothing_when_no_loop_exists(monkeypatch, caplog):
    """No loop anywhere is an idempotent success, not a resolution failure.

    Warning on it would fire on every ordinary duplicate stop and bury the miss
    the log exists to catch.
    """
    svc = _FakeSvc(None)
    _install_svc(monkeypatch, svc)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_directive_apply"):
        result = asyncio.run(
            apply_session_directive(
                _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
            )
        )
    assert "nothing to stop" in result.lower()
    assert [
        r for r in caplog.records if r.name == "kiro_crew.dashboard.session_directive_apply"
    ] == []


def test_autonudge_stop_directive_does_not_read_as_confirmation(default_install):
    """The tool's OWN return is the only text the model receives in-turn.

    The consumer applies the effect after the model already has this string, and
    the applier's outcome lands on the transcript rather than rewriting the
    model's tool result — so this wording must not let a caller conclude a loop
    was found or stopped. The measured failure it guards is a loop that called
    stop repeatedly, read a success-shaped reply each time, and never checked.
    """
    result = _call_tool_inner("autonudge_stop", {"reason": "done"})
    assert session_directive.decode(result, "autonudge_stop") == {"reason": "done"}
    assert "REQUESTED" in result
    assert "not confirmation" in result.lower()
    assert "nothing was stopped" in result.lower()
