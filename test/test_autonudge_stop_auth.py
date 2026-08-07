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

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew import session_directive
from kiro_crew.autonudge import binding_key_for
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.mcp_core import _call_tool_inner
from kiro_crew.validation import ValidationError

# ── Tool-contract fixtures ────────────────────────────────────────────────────


@pytest.fixture()
def default_install(monkeypatch):
    """Default install: the strict resolver has no accepted identity source and
    returns ``""``, so the stateless tools RETURN a directive rather than
    short-circuiting. (Pooling off, unsandboxed, kiro-cli backend.)"""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
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


def test_monitor_start_refuses_agent_supplied_exit_gate(default_install):
    """Gates are USER-armed only (GPT review): the MCP schema no longer carries
    exit_gate_cmd, so an agent supplying one gets the unknown-field validation
    error instead of a directive that would execute an unreviewed command."""
    with pytest.raises(ValidationError, match="exit_gate_cmd"):
        _call_tool_inner(
            "monitor_start",
            {"message": "watch CI", "exit_gate_cmd": "pytest -q tests/"},
        )


def test_monitor_update_refuses_agent_supplied_exit_gate(default_install):
    """Same user-armed-only contract on the update tool: neither adding,
    changing, nor clearing ('' was previously meaningful) is accepted."""
    with pytest.raises(ValidationError, match="exit_gate_cmd"):
        _call_tool_inner("monitor_update", {"exit_gate_cmd": ""})


def test_monitor_start_defaults_interval_300_and_bounded_cap(default_install):
    """Omitting interval_secs defaults to 300; omitting max_cycles defaults to a
    BOUNDED cap (24) — never an unbounded loop."""
    result = _call_tool_inner("monitor_start", {"message": "watch CI"})
    args = session_directive.decode(result, "monitor_start")
    assert args["idle_secs"] == 300
    assert args["max_cycles"] == mcp_core._MONITOR_DEFAULT_MAX_CYCLES
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
    """The human confirmation must read as an idle gap (not a fixed period) and
    put the stop obligation on the caller, framing the cap as a backstop."""
    result = _call_tool_inner("monitor_start", {"message": "watch PR", "interval_secs": 300})
    assert "ends" in result.lower()
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
    assert session_directive.decode(result, "monitor_update")["patch"] == {
        "max_runtime_secs": 3600
    }


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
    def __init__(
        self, loop_id, *, cycle_count=0, max_cycles=0, active=True, created_ts=0.0,
        max_runtime_secs=0, stopped_reason="", exit_gate_cmd="",
        last_fire_ts=0.0, gate_last_status="", gate_last_ts=0.0,
    ):
        self.id = loop_id
        self.slot_key = "chat-3-1700000000"
        self.cycle_count = cycle_count
        self.max_cycles = max_cycles
        self.active = active
        self.created_ts = created_ts
        self.max_runtime_secs = max_runtime_secs
        self.stopped_reason = stopped_reason
        self.exit_gate_cmd = exit_gate_cmd
        self.last_fire_ts = last_fire_ts
        self.gate_last_status = gate_last_status
        self.gate_last_ts = gate_last_ts


class _FakeSvc:
    """Minimal AutoNudge service double: get_by_slot + remove + update."""

    def __init__(self, loop=None):
        self._loop = loop
        self.get_by_slot_keys: list[str] = []
        self.removed: list[str] = []
        self.updates: list[dict] = []

    def get_by_slot(self, key):
        self.get_by_slot_keys.append(key)
        return self._loop

    async def remove(self, loop_id):
        self.removed.append(loop_id)

    async def update(self, loop_id, **kwargs):
        self.updates.append({"loop_id": loop_id, **kwargs})
        return self._loop


def _fake_state():
    return object()


def _fake_slot():
    class _Slot:
        key = "chat-3-1700000000"

    return _Slot()


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
    loop = _FakeLoop("loop-8", cycle_count=3, max_cycles=24, active=True,
                     created_ts=armed_two_hours_ago)
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
            _fake_state(), _fake_slot(), _SESSION, "monitor_update", {"patch": {"message": "revised"}}
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
        "loop-budget", cycle_count=5, max_cycles=24, active=False,
        created_ts=_time.time() - 7200, max_runtime_secs=3600,
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
        "loop-paused-budget", cycle_count=5, max_cycles=24, active=False,
        created_ts=_time.time() - 7200, max_runtime_secs=3600,
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


def test_applier_monitor_update_budget_stopped_denial_names_the_budget(monkeypatch):
    """When a budget-stopped loop is NOT being revived, the refusal must name
    the bound that stopped it — not send the agent chasing max_cycles."""
    import time as _time

    loop = _FakeLoop(
        "loop-budget2", cycle_count=5, max_cycles=24, active=False,
        created_ts=_time.time() - 7200, max_runtime_secs=3600,
        stopped_reason="runtime_budget",
    )
    svc = _FakeSvc(loop)
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update",
            {"patch": {"message": "revised"}},
        )
    )
    assert not update_calls
    assert "wall-clock" in result and "max_runtime_secs" in result
    assert "max_cycles" not in result


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


def test_applier_autonudge_stop_removes_the_loop_resolved_by_binding(monkeypatch):
    """OWNERSHIP: autonudge_stop resolves the loop from the session binding key
    and removes exactly that loop id."""
    svc = _FakeSvc(_FakeLoop("loop-1"))
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert svc.get_by_slot_keys == [binding_key_for(_SESSION)]
    assert svc.removed == ["loop-1"]
    assert "stopped" in result.lower()
    assert "done" in result


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


# ── exit quality gate ──


def _install_gate_runner(monkeypatch, result: dict):
    """Stub the sandboxed gate runner (lazily imported by run_exit_gate_for_user)."""
    calls: list[dict] = []

    # **kwargs on purpose: fixed-signature stubs of this runner have broken
    # twice when the real signature grew (extra_hidden_dirs, cwd) — tolerate
    # future kwargs and record the ones the tests assert on.
    def _fake(command, timeout=300, **kwargs):
        calls.append(
            {
                "command": command,
                "timeout": timeout,
                "mode": kwargs.get("sandbox_mode", "cc"),
                "hidden": kwargs.get("extra_hidden_dirs", ()),
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env_allowlist"),
            }
        )
        return result

    monkeypatch.setattr("kiro_crew.cron_script.run_command_sandboxed", _fake)
    return calls


def test_applier_stop_with_gate_always_pauses_even_with_fresh_pass(monkeypatch):
    """A gated agent stop ALWAYS pauses (GPT round 17): no record-freshness
    fast-path — a pre-recorded pass verified the workspace when the USER ran
    it, and turns can run after that without touching last_fire_ts, so no
    timestamp comparison proves coverage. Only a user-run pass executed
    AFTER the pause closes the loop. And the applier never executes."""
    svc = _FakeSvc(
        _FakeLoop(
            "loop-g1", exit_gate_cmd="pytest -q",
            last_fire_ts=1000.0, gate_last_status="pass", gate_last_ts=1005.0,
        )
    )
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "green"}
        )
    )
    assert calls == [], "the agent-side stop path must NEVER execute the gate"
    assert svc.removed == [], "a gated stop never removes the loop directly"
    assert svc.updates and svc.updates[0]["stopped_reason"] == "gate_pending"
    assert "NOT verified" in result


def test_applier_stop_without_verification_pauses_gate_pending(monkeypatch):
    """No user-run verification on record -> the loop DEACTIVATES as
    gate_pending (no unattended turns burn while waiting — design review),
    the exit is branded NOT verified, the user is notified, and nothing
    executes."""
    notifications: list[tuple] = []

    class _State:
        def notify(self, kind, title, body, meta=None):
            notifications.append((kind, title, body, meta))

    svc = _FakeSvc(_FakeLoop("loop-g2", exit_gate_cmd="pytest -q", last_fire_ts=1000.0))
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    result = asyncio.run(
        apply_session_directive(
            _State(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done?"}
        )
    )
    assert calls == [], "no execution on the unverified path either"
    assert svc.removed == [], "an unverified gated stop must not remove the loop"
    assert svc.updates and svc.updates[0]["active"] is False
    assert svc.updates[0]["stopped_reason"] == "gate_pending"
    assert "PAUSED pending verification" in result and "NOT verified" in result
    assert notifications, "the user must get a dashboard notification"
    assert "pytest -q" in notifications[0][2]


def test_applier_stop_during_inflight_fire_pauses_despite_fresh_pass(monkeypatch):
    """A loop present in svc._firing is mid-turn: channel fires bump
    last_fire_ts only AFTER the injected turn completes, so a pre-fire pass
    still looks fresh — but the work being claimed done happened in the
    current, post-pass turn. In-flight ⇒ unverified (GPT round 16)."""
    svc = _FakeSvc(
        _FakeLoop(
            "loop-g7", exit_gate_cmd="pytest -q",
            last_fire_ts=1000.0, gate_last_status="pass", gate_last_ts=1005.0,
        )
    )
    svc._firing = {"loop-g7"}
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.removed == []
    assert svc.updates and svc.updates[0]["stopped_reason"] == "gate_pending"
    assert "NOT verified" in result


def test_applier_stop_with_stale_pass_pauses(monkeypatch):
    """A pass recorded BEFORE the loop's latest fire verified OLDER work —
    the agent worked more turns since, so the stop still pauses pending a
    fresh user-run verification."""
    svc = _FakeSvc(
        _FakeLoop(
            "loop-g3", exit_gate_cmd="pytest -q",
            last_fire_ts=2000.0, gate_last_status="pass", gate_last_ts=1500.0,
        )
    )
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.removed == []
    assert svc.updates and svc.updates[0]["stopped_reason"] == "gate_pending"
    assert "NOT verified" in result


def test_applier_stop_with_failed_gate_record_pauses(monkeypatch):
    """A fresh user-run FAIL is not a pass — the stop pauses pending a
    passing verification."""
    svc = _FakeSvc(
        _FakeLoop(
            "loop-g4", exit_gate_cmd="pytest -q",
            last_fire_ts=1000.0, gate_last_status="fail", gate_last_ts=1005.0,
        )
    )
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.removed == []
    assert svc.updates and svc.updates[0]["stopped_reason"] == "gate_pending"
    assert "NOT verified" in result


def test_applier_stop_notification_failure_never_breaks_the_pause(monkeypatch):
    """The gate-pending notification is best-effort: a raising notify() must
    not break the stop path (mirrors the expiry-notice contract)."""

    class _State:
        def notify(self, *a, **k):
            raise RuntimeError("boom")

    svc = _FakeSvc(_FakeLoop("loop-g6", exit_gate_cmd="pytest -q", last_fire_ts=1.0))
    _install_svc(monkeypatch, svc)
    result = asyncio.run(
        apply_session_directive(
            _State(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.updates and svc.updates[0]["stopped_reason"] == "gate_pending"
    assert "PAUSED pending verification" in result


def test_applier_monitor_update_cannot_touch_an_existing_gate(monkeypatch):
    """SELF-DISARM GUARD: the agent the gate constrains cannot clear or
    replace an armed gate via its own monitor_update directive — clearing
    after a refused stop would make the gate advisory."""
    svc = _FakeSvc(_FakeLoop("loop-g7", exit_gate_cmd="pytest -q", active=True))
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    for patch_value in ("", "true"):  # clear AND weaken are both denied
        result = asyncio.run(
            apply_session_directive(
                _fake_state(),
                _fake_slot(),
                _SESSION,
                "monitor_update",
                {"patch": {"exit_gate_cmd": patch_value}},
            )
        )
        assert "cannot be set, changed, or removed" in result
    assert not update_calls


def test_applier_monitor_update_cannot_add_a_gate_either(monkeypatch):
    """USER-ARMED ONLY (GPT review): the applier runs on the agent's own
    auto-approved directives, so even ADDING a gate where none exists is an
    unreviewed shell command reaching execution — refused. (Previously
    allowed as 'self-discipline'.)"""
    svc = _FakeSvc(_FakeLoop("loop-g8", exit_gate_cmd=""))
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    result = asyncio.run(
        apply_session_directive(
            _fake_state(),
            _fake_slot(),
            _SESSION,
            "monitor_update",
            {"patch": {"exit_gate_cmd": "pytest -q"}},
        )
    )
    assert "user-armed" in result
    assert not update_calls


def test_applier_monitor_update_cannot_lower_the_cap_on_a_gated_loop(monkeypatch):
    """CAP-LOWERING ESCAPE (design review): shrinking max_cycles on a gated
    loop expires it through the ungated cap path — an indirect self-disarm.
    Raising (or 0 = unlimited) stays allowed."""
    svc = _FakeSvc(_FakeLoop("loop-g9", cycle_count=3, max_cycles=24, exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    # Lowering (24 -> 4) is denied.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update",
            {"patch": {"max_cycles": 4}},
        )
    )
    assert "expire ungated" in result
    assert not update_calls
    # Raising (24 -> 40) passes.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update",
            {"patch": {"max_cycles": 40}},
        )
    )
    assert len(update_calls) == 1 and update_calls[0]["max_cycles"] == 40


def test_applier_monitor_update_cannot_lower_the_budget_on_a_gated_loop(monkeypatch):
    """BUDGET-LOWERING ESCAPE (GPT review on the merged result): the wall-clock
    budget is the SECOND deliberately-ungated terminal — shrinking
    max_runtime_secs on a gated loop expires it without its gate ever running,
    the same indirect self-disarm the cap guard denies. Raising (or 0) stays
    allowed."""
    import time as _time

    svc = _FakeSvc(
        _FakeLoop(
            "loop-g16", created_ts=_time.time() - 60, max_runtime_secs=86400,
            exit_gate_cmd="pytest -q",
        )
    )
    _install_svc(monkeypatch, svc)
    update_calls = _record_update(monkeypatch)
    # Lowering (86400 -> 120) is denied.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update",
            {"patch": {"max_runtime_secs": 120}},
        )
    )
    assert "expire ungated" in result
    assert not update_calls
    # Raising (86400 -> 172800) passes.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_update",
            {"patch": {"max_runtime_secs": 172800}},
        )
    )
    assert len(update_calls) == 1 and update_calls[0]["max_runtime_secs"] == 172800


def test_applier_monitor_start_inherits_an_existing_gate(monkeypatch):
    """RE-ARM ESCAPE (design review): replacing a gated loop via the agent's
    own monitor_start directive must carry the gate onto the replacement —
    otherwise stop-refused → re-arm ungated → stop succeeds unverified."""
    svc = _FakeSvc(_FakeLoop("loop-g10", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    add_calls = _record_add(monkeypatch)
    # Directive tries to re-arm with NO gate.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_start",
            {"message": "keep watching", "idle_secs": 300, "max_cycles": 5},
        )
    )
    assert len(add_calls) == 1
    assert add_calls[0]["exit_gate_cmd"] == "pytest -q", "gate must be inherited"
    assert "INHERITED" in result
    # Directive tries to re-arm SUPPLYING a gate (even a weaker one) — the
    # whole arm is refused now: gates are user-armed only.
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_start",
            {"message": "watch", "exit_gate_cmd": "true"},
        )
    )
    assert "user" in result and "exit_gate_cmd" in result
    assert len(add_calls) == 1, "an arm supplying a gate must not reach svc.add"


def test_applier_monitor_start_clamps_cap_when_inheriting_a_gate(monkeypatch):
    """CAP CLAMP (design review): re-arming a gated loop with a tiny cap would
    expire the replacement through the deliberately-UNGATED cap path in one
    cycle — the same indirect disarm monitor_update's cap guard denies. The
    replacement's cap is clamped to the replaced loop's remaining cycles."""
    # Replaced loop: cap 24, 4 delivered -> 20 remaining.
    svc = _FakeSvc(_FakeLoop("loop-g12", cycle_count=4, max_cycles=24, exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    add_calls = _record_add(monkeypatch)
    asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_start",
            {"message": "keep watching", "max_cycles": 1},
        )
    )
    assert add_calls[0]["max_cycles"] == 20, "tiny cap must clamp to remaining cycles"
    # An UNCAPPED replaced loop forces an uncapped replacement (0): any finite
    # cap would introduce an ungated terminal the replaced loop did not have.
    svc2 = _FakeSvc(_FakeLoop("loop-g13", cycle_count=7, max_cycles=0, exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc2)
    add_calls2 = _record_add(monkeypatch)
    asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_start",
            {"message": "watch more", "max_cycles": 3},
        )
    )
    assert add_calls2[0]["max_cycles"] == 0


def test_applier_monitor_start_clamps_budget_when_inheriting_a_gate(monkeypatch):
    """BUDGET CLAMP (GPT review on the merged result): re-arming a gated loop
    with a tiny wall-clock budget expires the replacement through the
    deliberately-UNGATED budget terminal — same escape as the cycle cap, one
    bound over. The replacement's budget clamps to the replaced loop's
    REMAINING seconds (created_ts resets on the replacement), and an
    unlimited replaced budget forces an unlimited replacement."""
    import time as _time

    # Replaced loop: 3600s budget, ~600s elapsed -> ~3000s remaining.
    svc = _FakeSvc(
        _FakeLoop(
            "loop-g17", created_ts=_time.time() - 600, max_runtime_secs=3600,
            exit_gate_cmd="pytest -q",
        )
    )
    _install_svc(monkeypatch, svc)
    add_calls = _record_add(monkeypatch)
    asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_start",
            {"message": "keep watching", "max_runtime_secs": 60},
        )
    )
    assert 2900 <= add_calls[0]["max_runtime_secs"] <= 3000, "tiny budget must clamp to remaining"
    # An UNLIMITED replaced budget forces an unlimited replacement budget.
    svc2 = _FakeSvc(
        _FakeLoop(
            "loop-g18", created_ts=_time.time() - 600, max_runtime_secs=0,
            exit_gate_cmd="pytest -q",
        )
    )
    _install_svc(monkeypatch, svc2)
    add_calls2 = _record_add(monkeypatch)
    asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "monitor_start",
            {"message": "watch more", "max_runtime_secs": 120},
        )
    )
    assert add_calls2[0]["max_runtime_secs"] == 0


def test_user_gate_runner_output_is_redacted(monkeypatch):
    """Gate stdout flows into notification/API surfaces — a credential-shaped
    value in a failing gate's output must be scrubbed."""
    from kiro_crew.dashboard.session_directive_apply import run_exit_gate_for_user

    secret = "AKIA" + "ZZZZZZZZZZZZZZZZ"  # AWS access key shape
    _install_gate_runner(
        monkeypatch,
        {
            "status": "error",
            "output": f"boom: leaked {secret} in env dump",
            "exit_code": 1,
            "error_kind": None,
        },
    )
    outcome = asyncio.run(run_exit_gate_for_user("loop-x", "pytest -q"))
    assert outcome["status"] == "fail"
    assert secret not in outcome["note"], "credential-shaped output must be redacted"


def test_user_gate_runner_redacts_before_truncating(monkeypatch):
    """REDACT-BEFORE-TRUNCATE (GPT round 19): a credential spanning the
    2000-char truncation boundary must be scrubbed as a whole — truncating
    first would leave an unmatched secret prefix in the API response."""
    from kiro_crew.dashboard.session_directive_apply import (
        _EXIT_GATE_MAX_OUTPUT,
        run_exit_gate_for_user,
    )

    secret = "AKIA" + "Y" * 16  # AWS access key shape, 20 chars
    # Place the secret so the truncation boundary falls INSIDE it.
    prefix = "x" * (_EXIT_GATE_MAX_OUTPUT - 10)
    _install_gate_runner(
        monkeypatch,
        {
            "status": "error",
            "output": prefix + secret + " trailing",
            "exit_code": 1,
            "error_kind": None,
        },
    )
    outcome = asyncio.run(run_exit_gate_for_user("loop-x", "pytest -q"))
    note = outcome["note"]
    assert secret not in note
    assert secret[:10] not in note, "no unmatched secret prefix may survive"


def test_user_gate_runner_strict_sandbox_and_data_home_hidden(monkeypatch):
    """The user-run gate keeps ALL exec hardening: strict sandbox (no ~/.ssh)
    and the entire Kiro Crew data home on the deny-list."""
    from kiro_crew.dashboard.session_directive_apply import run_exit_gate_for_user

    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: __import__("pathlib").Path("/data/home")
    )
    outcome = asyncio.run(run_exit_gate_for_user("loop-x", "pytest -q"))
    assert outcome["status"] == "pass"
    assert calls and calls[0]["mode"] == "strict"
    expected = str(__import__("pathlib").Path("/data/home"))
    assert expected in calls[0]["hidden"]
    # Minimal ALLOWLISTED env (GPT round 18): the gateway env carries user
    # secrets with arbitrary names no deny-set can enumerate; a gate gets
    # locale/path basics only.
    env = calls[0]["env"]
    assert env is not None and "PATH" in env and "HOME" in env
    assert not any("SECRET" in k.upper() or "TOKEN" in k.upper() for k in env)


def test_user_gate_runner_passes_cwd_anchor(monkeypatch, tmp_path):
    """The cwd resolved by _gate_cwd (slot project dir -> workspace fallback)
    is forwarded to the sandboxed runner so relative gates verify the loop's
    own tree."""
    from kiro_crew.dashboard.session_directive_apply import (
        _gate_cwd,
        run_exit_gate_for_user,
    )

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    class _Slot:
        key = "chat-3-1700000000"
        project = str(proj_dir)
        workspace = "default"

    class _State:
        _slots = {"chat-3-1700000000": _Slot()}

    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    cwd = _gate_cwd(_State(), "chat-3-1700000000")
    assert cwd == str(proj_dir)
    asyncio.run(run_exit_gate_for_user("loop-x", "pytest -q tests/", cwd=cwd))
    assert calls and calls[0]["cwd"] == str(proj_dir)


def test_user_gate_runner_masked_cwd_is_not_run(monkeypatch, tmp_path):
    """A cwd INSIDE a sandbox-masked directory defeats the mask (the child's
    inherited working-directory fd keeps resolving beneath the hidden
    ancestor), and silently falling back to the daemon cwd could FALSELY
    PASS a relative-path gate against the wrong tree. The runner must
    short-circuit to not_run without executing (GPT rounds 16-17)."""
    from kiro_crew.dashboard.session_directive_apply import run_exit_gate_for_user

    data_home = tmp_path / "datahome"
    inside = data_home / "workspace-default"
    inside.mkdir(parents=True)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: data_home
    )
    outcome = asyncio.run(
        run_exit_gate_for_user("loop-x", "pytest -q", cwd=str(inside))
    )
    assert outcome["status"] == "not_run"
    assert calls == [], "a masked cwd must not execute at all"

    # A cwd OUTSIDE the masked dir executes and passes through untouched.
    outside = tmp_path / "proj"
    outside.mkdir()
    asyncio.run(run_exit_gate_for_user("loop-x", "pytest -q", cwd=str(outside)))
    assert calls and calls[0]["cwd"] == str(outside)


def test_gate_cwd_resolves_channel_binding_via_linked_session_key(tmp_path):
    """A channel session surfaced into a dashboard slot carries the raw
    channel key in linked_session_key — the cwd anchor must resolve THAT
    slot's project, not fall through to the default workspace (GPT round
    21: a gate passing in an unrelated tree is no verification)."""
    from kiro_crew.dashboard.session_directive_apply import _gate_cwd

    proj = tmp_path / "chanproj"
    proj.mkdir()

    class _Slot:
        key = "chat-7-1700000000"
        linked_session_key = "slack:1700000000.12345"
        project = str(proj)
        workspace = "default"

    class _State:
        _slots = {"chat-7-1700000000": _Slot()}

    cwd = _gate_cwd(_State(), "slack:1700000000.12345")
    assert cwd == str(proj)


def test_binding_work_generation_detects_inflight_and_turnover():
    """The endpoint refuses gate runs while the bound slot's turn is in
    flight AND refuses to record when the turn-task identity changed during
    the run — _binding_work_generation must see both through the direct and
    linked-session lookups (GPT rounds 21-22)."""
    from kiro_crew.dashboard.session_directive_apply import _binding_work_generation

    class _Task:
        pass

    task_a = _Task()

    class _Slot:
        key = "chat-7-1700000000"
        linked_session_key = "slack:1700000000.12345"
        running = True
        task = task_a

    class _State:
        _slots = {"chat-7-1700000000": _Slot()}

    running, gen = _binding_work_generation(_State(), "chat-7-1700000000")
    assert running is True and gen == id(task_a)
    running, gen2 = _binding_work_generation(_State(), "slack:1700000000.12345")
    assert running is True and gen2 == gen, "both lookups see the same task"
    _Slot.running = False
    running, gen3 = _binding_work_generation(_State(), "chat-7-1700000000")
    assert running is False and gen3 == gen
    # A NEW turn task (started+finished mid-run) changes the generation.
    _Slot.task = _Task()
    _, gen4 = _binding_work_generation(_State(), "chat-7-1700000000")
    assert gen4 != gen
    assert _binding_work_generation(_State(), "unknown-key") == (False, 0)
    assert _binding_work_generation(None, "chat-7-1700000000") == (False, 0)


def test_user_gate_runner_structural_inability_is_not_run(monkeypatch):
    """Hosts that structurally cannot execute (no_shell/sandbox_unavailable,
    keyed on the machine-readable error_kind) report not_run — never a
    spoofable pass."""
    from kiro_crew.dashboard.session_directive_apply import run_exit_gate_for_user

    _install_gate_runner(
        monkeypatch,
        {"status": "error", "output": "no shell", "exit_code": -1, "error_kind": "no_shell"},
    )
    outcome = asyncio.run(run_exit_gate_for_user("loop-x", "pytest -q"))
    assert outcome["status"] == "not_run"


def test_user_gate_runner_spoofed_structural_output_still_fails(monkeypatch):
    """A command that PRINTS a structural-failure phrase but ran (error_kind
    None) is a FAIL, not not_run — outcome keys on error_kind, never output."""
    from kiro_crew.dashboard.session_directive_apply import run_exit_gate_for_user

    _install_gate_runner(
        monkeypatch,
        {
            "status": "error",
            "output": "No POSIX shell available",
            "exit_code": 1,
            "error_kind": None,
        },
    )
    outcome = asyncio.run(run_exit_gate_for_user("loop-x", "evil"))
    assert outcome["status"] == "fail"


def test_applier_stop_without_gate_never_runs_a_command(monkeypatch):
    """No gate on the loop → the runner is never imported/invoked (legacy
    behavior byte-for-byte)."""
    svc = _FakeSvc(_FakeLoop("loop-g5"))
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(monkeypatch, {"status": "ok", "output": "", "exit_code": 0})
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done"}
        )
    )
    assert calls == []
    assert svc.removed == ["loop-g5"]
    assert "stopped" in result.lower()
