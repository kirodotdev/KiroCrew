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
    ):
        self.id = loop_id
        self.cycle_count = cycle_count
        self.max_cycles = max_cycles
        self.active = active
        self.created_ts = created_ts
        self.max_runtime_secs = max_runtime_secs
        self.stopped_reason = stopped_reason
        self.exit_gate_cmd = exit_gate_cmd


class _FakeSvc:
    """Minimal AutoNudge service double: only get_by_slot + remove are used."""

    def __init__(self, loop=None):
        self._loop = loop
        self.get_by_slot_keys: list[str] = []
        self.removed: list[str] = []

    def get_by_slot(self, key):
        self.get_by_slot_keys.append(key)
        return self._loop

    async def remove(self, loop_id):
        self.removed.append(loop_id)


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
    """Stub the sandboxed gate runner (lazily imported by _run_exit_gate)."""
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
            }
        )
        return result

    monkeypatch.setattr("kiro_crew.cron_script.run_command_sandboxed", _fake)
    return calls


def test_applier_stop_gate_pass_allows_the_stop(monkeypatch):
    """Exit 0 from the gate → the stop proceeds and says the gate passed.
    The gate must run in the STRICT sandbox (no ~/.ssh), not cron's 'cc'."""
    svc = _FakeSvc(_FakeLoop("loop-g1", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "42 passed", "exit_code": 0, "error_kind": None}
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "green"}
        )
    )
    assert calls and calls[0]["command"] == "pytest -q"
    assert calls[0]["mode"] == "strict", "exit gates must not inherit cc's ~/.ssh exposure"
    assert svc.removed == ["loop-g1"]
    assert "gate passed" in result.lower()


def test_applier_stop_revets_gate_and_skips_execution_when_policy_tightened(monkeypatch):
    """TOCTOU REVALIDATION (GPT review): the gate was vetted at ARM time, but
    governance can tighten between arm and stop — a stored command the current
    policy forbids must NOT execute on yesterday's approval. Execution is
    skipped (fail closed for exec), the stop still proceeds (refusing would
    force the forbidden execution on retry), and the note is loudly
    unverified."""
    svc = _FakeSvc(_FakeLoop("loop-g20", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    monkeypatch.setattr(
        "kiro_crew.autonudge_authz.vet_exit_gate_cmd",
        lambda cmd, session_key="": "blocked by governance policy: tightened",
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert not calls, "a gate that no longer vets clean must never execute"
    assert svc.removed == ["loop-g20"], "the stop itself still proceeds"
    assert "NOT run" in result and "NOT verified" in result


def test_applier_stop_gate_failure_refuses_the_stop_with_output(monkeypatch):
    """QUALITY GATE: a non-zero gate exit refuses the stop, keeps the loop, and
    feeds the agent the (bounded) gate output so it can finish and retry."""
    svc = _FakeSvc(_FakeLoop("loop-g2", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    long_output = "FAILED test_x - AssertionError\n" + ("y" * 5000)
    _install_gate_runner(
        monkeypatch,
        {"status": "error", "output": long_output, "exit_code": 2, "error_kind": None},
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {"reason": "done?"}
        )
    )
    assert svc.removed == [], "a failed gate must not remove the loop"
    assert "REFUSED" in result
    assert "FAILED test_x" in result
    assert "[gate output truncated]" in result
    assert len(result) < 3000, "gate output must be bounded in the refusal"


def test_applier_stop_gate_timeout_refuses_the_stop(monkeypatch):
    """A gate that cannot COMPLETE has verified nothing — timeout refuses."""
    svc = _FakeSvc(_FakeLoop("loop-g3", exit_gate_cmd="sleep 999"))
    _install_svc(monkeypatch, svc)
    _install_gate_runner(
        monkeypatch,
        {
            "status": "error",
            "output": "❌ Command timed out after 120s",
            "exit_code": -1,
            "error_kind": "timeout",
        },
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.removed == []
    assert "REFUSED" in result


def test_applier_stop_gate_structural_inability_fails_open_with_warning(monkeypatch):
    """A host that CANNOT run the gate (typed no_shell / sandbox_unavailable)
    still gets its stop — loudly marked unverified. Failing closed would trap
    the loop on exactly the hosts that can never satisfy the gate."""
    svc = _FakeSvc(_FakeLoop("loop-g4", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    _install_gate_runner(
        monkeypatch,
        {
            "status": "error",
            "output": "❌ No POSIX shell available to run this command cron. ...",
            "exit_code": -1,
            "error_kind": "no_shell",
        },
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.removed == ["loop-g4"]
    assert "WARNING" in result and "NOT verified" in result


def test_applier_stop_gate_spoofed_structural_output_still_refuses(monkeypatch):
    """SPOOF GUARD: fail-open keys on the runner's typed error_kind, never on
    output text — a gate that PRINTS a structural-failure phrase and exits
    non-zero (`echo "No POSIX shell available"; exit 1`) must be refused."""
    svc = _FakeSvc(_FakeLoop("loop-g6", exit_gate_cmd="evil"))
    _install_svc(monkeypatch, svc)
    _install_gate_runner(
        monkeypatch,
        {
            "status": "error",
            # Attacker-controlled stdout containing BOTH marker phrases.
            "output": (
                "⚠️ Exit code 1\n\nNo POSIX shell available — "
                "could not run in an OS sandbox"
            ),
            "exit_code": 1,
            "error_kind": None,  # the command RAN — its output is its own
        },
    )
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert svc.removed == [], "spoofed structural text must not fail open"
    assert "REFUSED" in result


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


def test_applier_stop_gate_output_is_redacted(monkeypatch):
    """Gate stdout flows into transcript/dashboard/channel surfaces — a
    credential-shaped value in a failing gate's output must be scrubbed."""
    svc = _FakeSvc(_FakeLoop("loop-g11", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
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
    result = asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert "REFUSED" in result
    assert secret not in result, "credential-shaped gate output must be redacted"


def test_applier_gate_runs_with_data_home_hidden(monkeypatch):
    """The Kiro Crew data home is the keystone (security policy, loop store);
    the gate's sandbox must hide it — text vetting cannot see inside a
    referenced interpreter script."""
    svc = _FakeSvc(_FakeLoop("loop-g12", exit_gate_cmd="pytest -q"))
    _install_svc(monkeypatch, svc)
    calls: list[dict] = []

    def _fake(command, timeout=300, **kwargs):
        calls.append(
            {"mode": kwargs.get("sandbox_mode", "cc"), "hidden": kwargs.get("extra_hidden_dirs", ())}
        )
        return {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}

    monkeypatch.setattr("kiro_crew.cron_script.run_command_sandboxed", _fake)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: __import__("pathlib").Path("/data/home")
    )
    asyncio.run(
        apply_session_directive(
            _fake_state(), _fake_slot(), _SESSION, "autonudge_stop", {}
        )
    )
    assert calls and calls[0]["mode"] == "strict"
    # Compare via pathlib so the assertion is separator-correct on Windows
    # (str(Path("/data/home")) is "\\data\\home" there, not "/data/home").
    expected = str(__import__("pathlib").Path("/data/home"))
    assert expected in calls[0]["hidden"]


def test_applier_gate_runs_anchored_to_the_slots_project_dir(monkeypatch, tmp_path):
    """CWD ANCHORING (design review): without an anchor the gate inherits the
    gateway DAEMON's cwd, so a relative-path gate (`pytest -q tests/` — the
    former schema's own example) fails for reasons unrelated to work quality
    and every stop is refused. The gate must run in the slot's project dir."""
    svc = _FakeSvc(_FakeLoop("loop-g14", exit_gate_cmd="pytest -q tests/"))
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    class _Slot:
        key = "chat-3-1700000000"
        # The REAL attribute on dashboard slots (state.py) — the regression
        # this locks in: reading only a nonexistent `project_dir` made the
        # anchor always fall through to the workspace dir.
        project = str(proj_dir)
        workspace = "default"

    class _State:
        _slots = {"chat-3-1700000000": _Slot()}

    asyncio.run(
        apply_session_directive(_State(), _Slot(), _SESSION, "autonudge_stop", {})
    )
    assert calls and calls[0]["cwd"] == str(proj_dir)


def test_applier_gate_cwd_falls_back_to_workspace_dir(monkeypatch, tmp_path):
    """No project dir on the slot → the gate anchors to the slot's WORKSPACE
    directory (never the daemon cwd)."""
    svc = _FakeSvc(_FakeLoop("loop-g15", exit_gate_cmd="true"))
    _install_svc(monkeypatch, svc)
    calls = _install_gate_runner(
        monkeypatch, {"status": "ok", "output": "", "exit_code": 0, "error_kind": None}
    )
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    monkeypatch.setattr(
        "kiro_crew.config.loader.workspace_dir_for", lambda ws=None: ws_dir
    )

    class _Slot:
        key = "chat-3-1700000000"
        project = ""
        workspace = "default"

    class _State:
        _slots = {"chat-3-1700000000": _Slot()}

    asyncio.run(
        apply_session_directive(_State(), _Slot(), _SESSION, "autonudge_stop", {})
    )
    assert calls and calls[0]["cwd"] == str(ws_dir)


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
