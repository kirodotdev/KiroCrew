"""The tool-permission ladder's contract, pinned through ``settle`` and ``bail``.

One suite at the module's interface. The engine runs with test adapters -- a
recording wire, a memory audit, scripted grants and responders -- that log onto
one shared timeline, so every ordering claim is an assertion on that timeline.
The production adapters (``AcpWire``, ``HookGate``, ``SpecHooks``,
``CallbackResponder``, ``SelAudit`` and the two row codecs) are pinned the same
way, and the two production ladders -- the subagent's and the task runner's --
are driven through their own builders with a recording ``sel``.

What each surface does end to end on a real run (``SubagentManager._run_inner``,
``task_executor.execute_task``) is pinned by the deny-notice suites; this file
pins what the ladder guarantees to every surface.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import llm_helpers, name_grant, task_executor, tool_permission
from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.constants import DENY_CAUSE_POLICY, DENY_CAUSE_SURFACE_POLICY
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, ToolHookResult
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.subagent import (
    _HEADLESS_DENY_REASON,
    _LOW_FIDELITY_DENY_REASON,
    SubagentInfo,
    SubagentManager,
)
from kiro_crew.subagent_manager import run as run_mod
from kiro_crew.task_models import Project, Task
from kiro_crew.tool_permission import (
    GATE_GRANT,
    NO_GATE,
    AcpWire,
    Answer,
    Ask,
    CallbackResponder,
    ChildRule,
    ContextOverflow,
    Evidence,
    Hit,
    HookGate,
    Narrator,
    Notice,
    ParentPolicyAuto,
    Policy,
    Refusal,
    SelAudit,
    Settled,
    SpecHooks,
    SubagentRows,
    TaskrunnerRows,
    bail,
    settle,
)

pytestmark = pytest.mark.usefixtures("close_subagent_managers")

_SHADOWED = name_grant.Refusal(code="shadowed", detail="ls resolves to /srv/agent/bin/ls")


# ── Test adapters, all logging onto one timeline ─────────────────────────────


class RecordingWire:
    """The test :class:`tool_permission.Wire`: records each answer in order."""

    def __init__(self, log: list, *, floor_refuses: bool = False) -> None:
        self.log = log
        self.floor_refuses = floor_refuses

    async def allow(self, ask: Ask) -> bool:
        self.log.append(("allow", ask.request_id))
        return not self.floor_refuses

    async def refuse(self, ask: Ask, notice: Notice | None) -> None:
        if notice is not None:
            self.log.append(("steer", notice.cause, notice.reason))
        self.log.append(("reject", ask.request_id))

    def answers(self) -> list:
        return [entry for entry in self.log if entry[0] in ("allow", "reject")]


class MemoryAudit:
    """The test :class:`tool_permission.Audit`, with injectable write failures."""

    def __init__(self, log: list, *, fail: str = "") -> None:
        self.log = log
        self.fail = fail

    def refused(self, ask: Ask, refusal: Refusal) -> None:
        if self.fail == "refused":
            raise RuntimeError("audit sink unavailable")
        self.log.append(("audit", "refused", refusal.rung))

    def approved(self, ask: Ask, settled: Settled) -> None:
        if self.fail == "approved":
            raise RuntimeError("audit sink unavailable")
        self.log.append(("audit", "approved", settled.rung, settled.outcome))

    def declined(self, ask: Ask, refusal: name_grant.Refusal, tier: str) -> None:
        self.log.append(("audit", "declined", refusal.code, tier))


class MemoryNarrator(Narrator):
    def __init__(self, log: list) -> None:
        self.log = log

    def refusing(self, ask: Ask, refusal: Refusal) -> None:
        self.log.append(("narrate", "refusing", refusal.rung))

    def declined(self, ask: Ask, refusal: name_grant.Refusal) -> None:
        self.log.append(("narrate", "declined", refusal.code))

    def allowed(self, ask: Ask, sent: bool) -> None:
        self.log.append(("narrate", "allowed", sent))


class Offers:
    """A grant that offers one fixed hit (or none), recording that it was asked."""

    def __init__(self, log: list, name: str, hit: Hit | None) -> None:
        self.log = log
        self.name = name
        self.hit = hit

    async def offer(self, ask: Ask) -> Hit | None:
        self.log.append(("offer", self.name))
        return self.hit


class Refuses:
    """A floor or interject that refuses with a fixed refusal (or not)."""

    def __init__(self, log: list, name: str, refusal: Refusal | None) -> None:
        self.log = log
        self.name = name
        self.refusal = refusal

    async def refuse(self, ask: Ask) -> Refusal | None:
        self.log.append(("check", self.name))
        return self.refusal


class Judges:
    """A gate with a fixed verdict."""

    def __init__(self, log: list, verdict: Refusal | Hit | None) -> None:
        self.log = log
        self.verdict = verdict

    def judge(self, ask: Ask) -> Refusal | Hit | None:
        self.log.append(("gate",))
        return self.verdict


class Scripted:
    """A responder answering from a script."""

    def __init__(self, log: list, name: str, approved: bool, *, attended: bool = True):
        self.log = log
        self.name = name
        self._approved = approved
        self._attended = attended

    @property
    def attended(self) -> bool:
        return self._attended

    async def ask(self, ask: Ask) -> Answer:
        self.log.append(("ask", self.name, ask.event.title))
        return Answer(approved=self._approved)


def _event(**kw) -> LLMEvent:
    fields: dict = {
        "kind": EVENT_PERMISSION_REQUEST,
        "title": "Run: ls -la",
        "request_id": "req-1",
        "tool_name": "execute_bash",
        "tool_kind": "execute",
        "is_shell": True,
        "shell_classified": True,
        "raw_params_trusted": True,
        "raw_tool_params": {"command": "ls -la"},
    }
    fields.update(kw)
    return LLMEvent(**fields)  # type: ignore[arg-type]


def _low_fidelity_event(*, identity_verified: bool) -> LLMEvent:
    """A backend child's request with no recoverable security context.

    With *identity_verified* it is the remote-MCP shape: its ``_meta.kiro``
    server/tool pair came from the verified caches, only its arguments did not.
    """
    if identity_verified:
        return _event(
            sub_session_id="child-1",
            raw_params_trusted=False,
            raw_tool_params=None,
            is_shell=False,
            tool_name="search",
            mcp_server_name="remote",
            mcp_identity_trusted=True,
            title="remote/search",
        )
    return _event(sub_session_id="child-1", shell_classified=False, raw_params_trusted=False)


_HOST = Refusal.host("headless", "nothing here can approve it", DENY_CAUSE_SURFACE_POLICY)


def _policy(log: list, **kw) -> Policy:
    kw.setdefault("gate", Judges(log, None))
    kw.setdefault("audit", MemoryAudit(log))
    kw.setdefault("otherwise", _HOST)
    kw.setdefault("narrator", MemoryNarrator(log))
    return Policy(**kw)


@pytest.fixture
def names(monkeypatch):
    """The program-name check, scripted: each event title maps to a refusal or None."""
    verdicts: dict[str, name_grant.Refusal | None] = {}
    asked: list[str] = []

    async def _refusal_for_event(event):
        asked.append(event.title)
        return verdicts.get(event.title)

    monkeypatch.setattr(name_grant, "refusal_for_event", _refusal_for_event)
    return SimpleNamespace(verdicts=verdicts, asked=asked)


# ── The ladder: exactly one answer, audited first, steered only for the host ──


def _exits(log: list) -> dict[str, Policy]:
    """One policy per way the ladder can end."""
    grant = Hit("parent_policy_auto", Evidence.NONE)
    return {
        "floor": _policy(
            log, floors=(Refuses(log, "floor", Refusal.host("spec_hook", "x", DENY_CAUSE_POLICY)),)
        ),
        "gate_deny": _policy(
            log, gate=Judges(log, Refusal.host("hook_deny", "no", DENY_CAUSE_POLICY))
        ),
        "gate_grant": _policy(
            log, gate=Judges(log, Hit("hook_auto_approve", Evidence.NAME, vouch=True))
        ),
        "grant": _policy(log, grants=(GATE_GRANT, Offers(log, "parent", grant))),
        "interject": _policy(
            log, interject=Refuses(log, "overflow", Refusal.teardown("context_overflow"))
        ),
        "person_yes": _policy(log, responders=(Scripted(log, "callback", True),)),
        "person_no": _policy(log, responders=(Scripted(log, "callback", False),)),
        "otherwise": _policy(log),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_", list(_exits([])))
async def test_every_settled_request_is_answered_on_the_wire_exactly_once(names, exit_):
    log: list = []
    wire = RecordingWire(log)
    await settle(Ask(_event(), wire, "k"), _exits(log)[exit_])
    assert len(wire.answers()) == 1, log


@pytest.mark.asyncio
async def test_a_host_refusal_is_narrated_then_audited_then_steered_then_rejected():
    log: list = []
    policy = _policy(log, gate=Judges(log, Refusal.host("hook_deny", "denied: rm", "policy")))
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert log == [
        ("gate",),
        ("narrate", "refusing", "hook_deny"),
        ("audit", "refused", "hook_deny"),
        ("steer", "policy", "denied: rm"),
        ("reject", "req-1"),
    ]
    assert settled == Settled("refused", "hook_deny", "host", "policy", "denied: rm")


@pytest.mark.asyncio
async def test_settle_judges_the_gate_on_the_tool_gate_pool_not_the_loop():
    """The gate's path tier waits on the resolver child on the calling thread, so
    settle must not run it on the event loop's thread."""
    import threading

    log: list = []
    seen: list[str] = []

    class _Recording(Judges):
        def judge(self, ask: Ask) -> Refusal | Hit | None:
            seen.append(threading.current_thread().name)
            return super().judge(ask)

    policy = _policy(log, gate=_Recording(log, Refusal.host("hook_deny", "denied: rm", "policy")))
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert settled.rung == "hook_deny"
    assert seen and seen[0].startswith("mc-toolgate"), seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_kw", "rung", "outcome"),
    [
        ({"responders": "no"}, "callback", "rejected"),
        ({"interject": "teardown"}, "context_overflow", "bailed"),
    ],
)
async def test_a_person_or_a_teardown_is_audited_and_rejected_bare(policy_kw, rung, outcome):
    log: list = []
    kw: dict = {}
    if "responders" in policy_kw:
        kw["responders"] = (Scripted(log, "callback", False),)
    else:
        kw["interject"] = Refuses(log, "overflow", Refusal.teardown("context_overflow"))
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), _policy(log, **kw))
    assert [entry for entry in log if entry[0] in ("audit", "steer", "reject")] == [
        ("audit", "refused", rung),
        ("reject", "req-1"),
    ]
    assert settled.outcome == outcome and settled.cause is None


@pytest.mark.asyncio
async def test_bail_audits_and_rejects_bare():
    log: list = []
    settled = await bail(Ask(_event(), RecordingWire(log), "k"), _policy(log), "turn_limit")
    assert log == [
        ("narrate", "refusing", "turn_limit"),
        ("audit", "refused", "turn_limit"),
        ("reject", "req-1"),
    ]
    assert settled == Settled("bailed", "turn_limit", "teardown")


@pytest.mark.asyncio
@pytest.mark.parametrize("floor_refuses", [False, True])
async def test_an_approval_is_audited_after_the_wire_answered(floor_refuses):
    log: list = []
    policy = _policy(log, grants=(GATE_GRANT, Offers(log, "p", Hit("p", Evidence.NONE))))
    settled = await settle(
        Ask(_event(), RecordingWire(log, floor_refuses=floor_refuses), "k"), policy
    )
    outcome = "floor_refused" if floor_refuses else "auto_approved"
    assert log[-3:] == [
        ("allow", "req-1"),
        ("narrate", "allowed", not floor_refuses),
        ("audit", "approved", "p", outcome),
    ]
    assert settled.outcome == outcome and settled.approved is not floor_refuses


@pytest.mark.asyncio
async def test_a_refusal_the_audit_cannot_write_leaves_the_wire_to_the_policy():
    """``SelAudit`` decides per surface; a raising audit adapter raises before the wire."""
    log: list = []
    policy = _policy(log, audit=MemoryAudit(log, fail="refused"))
    with pytest.raises(RuntimeError):
        await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert RecordingWire(log).answers() == []


@pytest.mark.asyncio
async def test_an_approval_the_audit_cannot_write_raises_after_the_answer():
    log: list = []
    policy = _policy(
        log,
        audit=MemoryAudit(log, fail="approved"),
        grants=(GATE_GRANT, Offers(log, "p", Hit("p", Evidence.NONE))),
    )
    with pytest.raises(RuntimeError):
        await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert ("allow", "req-1") in log


# ── Stage order ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_floor_refusal_runs_before_the_gate_and_every_grant():
    log: list = []
    policy = _policy(
        log,
        floors=(Refuses(log, "spec", Refusal.host("spec_hook", "blocked", DENY_CAUSE_POLICY)),),
        grants=(Offers(log, "early", Hit("p", Evidence.NONE)), GATE_GRANT),
    )
    await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert ("gate",) not in log and ("offer", "early") not in log


@pytest.mark.asyncio
async def test_a_gate_deny_outranks_every_grant_even_one_listed_before_it():
    log: list = []
    policy = _policy(
        log,
        gate=Judges(log, Refusal.host("hook_deny", "no", DENY_CAUSE_POLICY)),
        grants=(Offers(log, "early", Hit("p", Evidence.NONE)), GATE_GRANT),
    )
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert settled.rung == "hook_deny" and ("offer", "early") not in log


@pytest.mark.asyncio
async def test_grants_are_walked_in_order_and_the_first_admitted_hit_answers():
    log: list = []
    policy = _policy(
        log,
        gate=Judges(log, Hit("hook_auto_approve", Evidence.IDENTITY)),
        grants=(
            Offers(log, "first", None),
            GATE_GRANT,
            Offers(log, "last", Hit("x", Evidence.NONE)),
        ),
    )
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert [e for e in log if e[0] in ("gate", "offer")] == [("gate",), ("offer", "first")]
    assert settled == Settled("auto_approved", "hook_auto_approve", "rule")


@pytest.mark.asyncio
async def test_a_refused_program_name_downgrades_to_the_next_stage(names):
    names.verdicts["Run: ls -la"] = _SHADOWED
    log: list = []
    policy = _policy(
        log,
        gate=Judges(log, Hit("hook_auto_approve", Evidence.NAME, vouch=True)),
        grants=(GATE_GRANT, Offers(log, "next", None)),
        responders=(Scripted(log, "callback", True),),
    )
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert log[1:] == [
        ("narrate", "declined", "shadowed"),
        ("audit", "declined", "shadowed", "hook_auto_approve"),
        ("offer", "next"),
        ("ask", "callback", "Run: ls -la"),
        ("allow", "req-1"),
        ("narrate", "allowed", True),
        ("audit", "approved", "callback", "approved"),
    ]
    assert settled.by == "person"


@pytest.mark.asyncio
async def test_only_a_name_grant_is_vouched_for(names):
    names.verdicts["Run: ls -la"] = _SHADOWED
    log: list = []
    policy = _policy(log, grants=(GATE_GRANT, Offers(log, "p", Hit("p", Evidence.NONE))))
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert names.asked == [] and settled.outcome == "auto_approved"


@pytest.mark.asyncio
async def test_the_interject_runs_after_the_grants_and_before_any_answer():
    log: list = []
    policy = _policy(
        log,
        grants=(GATE_GRANT, Offers(log, "p", Hit("p", Evidence.NONE))),
        interject=Refuses(log, "overflow", Refusal.teardown("context_overflow", {"pct": 95.0})),
    )
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert log.index(("offer", "p")) < log.index(("check", "overflow"))
    assert ("allow", "req-1") not in log
    assert settled.outcome == "bailed" and settled.meta == {"pct": 95.0}


@pytest.mark.asyncio
async def test_the_first_attended_responder_asks_and_an_unattended_one_never_does():
    log: list = []
    policy = _policy(
        log,
        responders=(
            Scripted(log, "factory", True, attended=False),
            Scripted(log, "callback", False),
        ),
    )
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert [e for e in log if e[0] == "ask"] == [("ask", "callback", "Run: ls -la")]
    assert settled == Settled("rejected", "callback", "person")


@pytest.mark.asyncio
async def test_with_nobody_attended_the_surface_refusal_answers():
    log: list = []
    policy = _policy(log, responders=(Scripted(log, "callback", True, attended=False),))
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert settled == Settled(
        "refused", "headless", "host", DENY_CAUSE_SURFACE_POLICY, _HOST.reason
    )
    assert ("steer", DENY_CAUSE_SURFACE_POLICY, _HOST.reason) in log


# ── A low-fidelity child under an enforced child rule ────────────────────────

_CHILD_UNATTENDED = Refusal.host("child_unattended", "no security context", "surface_policy")


def _child_policy(log: list, *, gate_hit: Hit | None = None, **kw) -> Policy:
    kw.setdefault(
        "child",
        ChildRule.enforce(
            grants=kw.pop("child_grants", (GATE_GRANT,)),
            unattended=_CHILD_UNATTENDED,
            responder=kw.pop("child_responder", None),
            annotate=lambda title: f"UNVERIFIED: {title}",
        ),
    )
    return _policy(log, gate=Judges(log, gate_hit), **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "identity_verified", "admitted"),
    [
        (Evidence.NONE, True, True),  # eligible: only the arguments are unverified
        (Evidence.NONE, False, False),  # nothing verified: no unconditional grant
        # The gate reports IDENTITY only for a verified identity (HookGate below).
        (Evidence.IDENTITY, True, True),
        (Evidence.NAME, True, False),  # a name grant reads the agent-authored title
        (Evidence.CLASSIFIER, True, False),  # a classifier reads the agent-authored content
    ],
)
async def test_a_low_fidelity_child_admits_only_evidence_that_reads_no_agent_text(
    names, evidence, identity_verified, admitted
):
    log: list = []
    if evidence is Evidence.NONE:
        policy = _child_policy(
            log, child_grants=(Offers(log, "p", Hit("p", Evidence.NONE)), GATE_GRANT)
        )
    else:
        hit = Hit("hook_auto_approve", evidence, vouch=True)
        policy = _child_policy(log, gate_hit=hit, child_grants=(Offers(log, "p", None), GATE_GRANT))
    settled = await settle(
        Ask(_low_fidelity_event(identity_verified=identity_verified), RecordingWire(log), "k"),
        policy,
    )
    assert settled.approved is admitted
    assert names.asked == [], "no program name is vouched for on a low-fidelity child"
    if not admitted:
        assert settled.rung == "child_unattended"


@pytest.mark.asyncio
async def test_a_low_fidelity_child_is_asked_under_the_annotated_title_by_the_child_responder():
    log: list = []
    event = _low_fidelity_event(identity_verified=False)
    policy = _child_policy(
        log,
        child_responder=Scripted(log, "child", True),
        responders=(Scripted(log, "callback", True),),
    )
    settled = await settle(Ask(event, RecordingWire(log), "k"), policy)
    assert [e for e in log if e[0] == "ask"] == [("ask", "child", "UNVERIFIED: Run: ls -la")]
    assert event.title == "UNVERIFIED: Run: ls -la", "the rows record what the person saw"
    assert settled == Settled("approved", "child", "person")


@pytest.mark.asyncio
async def test_a_full_fidelity_request_never_reaches_the_child_rule():
    log: list = []
    policy = _child_policy(
        log,
        child_responder=Scripted(log, "child", True),
        child_grants=(Offers(log, "child_grant", Hit("p", Evidence.NONE)), GATE_GRANT),
    )
    settled = await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert settled.rung == "headless"
    assert [e for e in log if e[0] in ("ask", "offer")] == []


@pytest.mark.asyncio
async def test_a_low_fidelity_child_walks_the_child_rules_own_grants_in_their_order():
    log: list = []
    policy = _child_policy(
        log,
        gate_hit=Hit("hook_auto_approve", Evidence.IDENTITY, vouch=True),
        grants=(GATE_GRANT, Offers(log, "full", Hit("parent_policy_auto", Evidence.NONE))),
        child_grants=(Offers(log, "low", Hit("parent_policy_auto", Evidence.NONE)), GATE_GRANT),
    )
    settled = await settle(
        Ask(_low_fidelity_event(identity_verified=True), RecordingWire(log), "k"), policy
    )
    assert [e for e in log if e[0] == "offer"] == [("offer", "low")]
    assert settled.rung == "parent_policy_auto"


@pytest.mark.asyncio
async def test_an_ignored_child_rule_settles_a_low_fidelity_child_like_any_request(names):
    log: list = []
    policy = _policy(log, gate=Judges(log, Hit("hook_auto_approve", Evidence.NAME, vouch=True)))
    settled = await settle(
        Ask(_low_fidelity_event(identity_verified=False), RecordingWire(log), "k"), policy
    )
    assert settled.outcome == "auto_approved" and names.asked == ["Run: ls -la"]


@pytest.mark.asyncio
async def test_parent_policy_auto_is_an_unconditional_grant_only_under_auto():
    ask = Ask(_event(), RecordingWire([]), "k")
    assert await ParentPolicyAuto("auto").offer(ask) == Hit("parent_policy_auto", Evidence.NONE)
    assert await ParentPolicyAuto("ask").offer(ask) is None
    assert await ParentPolicyAuto("").offer(ask) is None


# ── Construction rules ───────────────────────────────────────────────────────


def test_a_host_refusal_without_a_cause_and_a_bare_one_with_a_cause_are_rejected():
    with pytest.raises(ValueError, match="needs a deny cause"):
        Refusal("host", "headless", "x")
    with pytest.raises(ValueError, match="only a host refusal"):
        Refusal("person", "callback", cause=DENY_CAUSE_POLICY)
    with pytest.raises(ValueError, match="only a host refusal"):
        Refusal("teardown", "turn_limit", cause=DENY_CAUSE_POLICY)


@pytest.mark.parametrize("grants", [(), (GATE_GRANT, GATE_GRANT)])
def test_the_gate_grant_must_be_placed_exactly_once(grants):
    with pytest.raises(ValueError, match="exactly once"):
        Policy(gate=NO_GATE, audit=MemoryAudit([]), otherwise=_HOST, grants=grants)
    with pytest.raises(ValueError, match="at most once"):
        Policy(
            gate=NO_GATE,
            audit=MemoryAudit([]),
            otherwise=_HOST,
            child=ChildRule.enforce(grants=(GATE_GRANT, GATE_GRANT), unattended=_HOST),
        )


def test_an_enforced_child_rule_needs_its_unattended_refusal():
    with pytest.raises(ValueError, match="unattended refusal"):
        Policy(
            gate=NO_GATE,
            audit=MemoryAudit([]),
            otherwise=_HOST,
            child=ChildRule(enforced=True),
        )


def test_without_a_hook_manager_there_is_no_gate_verdict():
    assert NO_GATE.judge(Ask(_event(), RecordingWire([]), "k")) is None


# ── AcpWire ──────────────────────────────────────────────────────────────────


class _Client:
    """An ACP client double that records steer / approve / reject in order."""

    def __init__(self, log: list, *, approve: object = None, steer: str = "ok") -> None:
        self.log = log
        self._approve = approve
        self._steer = steer
        self.supports_refusal_steer = steer != "unsupported"
        self.parked = asyncio.Event()

    async def steer(self, message: str) -> bool:
        self.log.append(("steer", message))
        if self._steer == "raise":
            raise RuntimeError("pipe closed")
        if self._steer == "park":
            self.parked.set()
            await asyncio.Event().wait()
        return True

    async def approve_tool(self, request_id):
        self.log.append(("approve", request_id))
        return self._approve

    async def reject_tool(self, request_id):
        self.log.append(("reject", request_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(("returned", "sent"), [(None, True), (True, True), (False, False)])
async def test_acp_wire_reports_whether_the_transport_floor_let_the_approval_through(
    returned, sent
):
    log: list = []
    assert await AcpWire(_Client(log, approve=returned)).allow(Ask(_event(), None, "k")) is sent
    assert log == [("approve", "req-1")]


@pytest.mark.asyncio
async def test_acp_wire_steers_the_notice_before_it_rejects():
    log: list = []
    wire = AcpWire(_Client(log))
    await wire.refuse(Ask(_event(), wire, "k"), Notice(DENY_CAUSE_POLICY, "Blocked: rm -rf"))
    assert [entry[0] for entry in log] == ["steer", "reject"]
    assert "Blocked: rm -rf" in log[0][1] and "NOT a user action" in log[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", ["raise", "unsupported"])
async def test_acp_wire_rejects_even_when_the_steer_fails_or_is_unsupported(steer):
    log: list = []
    wire = AcpWire(_Client(log, steer=steer))
    await wire.refuse(Ask(_event(), wire, "k"), Notice(DENY_CAUSE_POLICY, "denied"))
    assert log[-1] == ("reject", "req-1")


@pytest.mark.asyncio
async def test_acp_wire_without_a_notice_only_rejects():
    log: list = []
    wire = AcpWire(_Client(log))
    await wire.refuse(Ask(_event(), wire, "k"), None)
    assert log == [("reject", "req-1")]


@pytest.mark.asyncio
async def test_a_cancel_mid_steer_still_answers_the_request():
    log: list = []
    client = _Client(log, steer="park")
    wire = AcpWire(client)
    refusing = asyncio.ensure_future(
        wire.refuse(Ask(_event(), wire, "k"), Notice(DENY_CAUSE_POLICY, "denied"))
    )
    await asyncio.wait_for(client.parked.wait(), 10)
    refusing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refusing
    for _ in range(100):
        if ("reject", "req-1") in log:
            break
        await asyncio.sleep(0)
    assert log[-1] == ("reject", "req-1"), "the orphan reject answers the stranded request"


# ── HookGate, SpecHooks, ContextOverflow ─────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "verdict"),
    [
        (
            ToolHookResult(action=TOOL_DENY, reason="Blocked by security policy"),
            Refusal.host("hook_deny", "Blocked by security policy", DENY_CAUSE_POLICY),
        ),
        (ToolHookResult(action=TOOL_DENY), Refusal.host("hook_deny", "", DENY_CAUSE_POLICY)),
        (
            # An identity-keyed grant on a request whose own identity is not
            # verified rests on nothing better than the hook's name match.
            ToolHookResult(action=TOOL_AUTO_APPROVE, identity_grant=True, read_only=True),
            Hit("hook_auto_approve", Evidence.CLASSIFIER, vouch=True),
        ),
        (
            ToolHookResult(action=TOOL_AUTO_APPROVE, read_only=True),
            Hit("hook_auto_approve", Evidence.CLASSIFIER, vouch=True),
        ),
        (
            ToolHookResult(action=TOOL_AUTO_APPROVE),
            Hit("hook_auto_approve", Evidence.NAME, vouch=True),
        ),
        (ToolHookResult(action=TOOL_ALLOW), None),
    ],
)
def test_hook_gate_reads_the_hooks_own_account_of_what_it_matched(result, verdict):
    seen: list = []

    def consult(event):
        seen.append(event)
        return result

    event = _event()
    assert HookGate(consult).judge(Ask(event, None, "k")) == verdict
    assert seen == [event]


@pytest.mark.parametrize(
    ("identity_grant", "verified", "evidence"),
    [
        (True, True, Evidence.IDENTITY),
        (True, False, Evidence.NAME),
        (False, True, Evidence.NAME),
    ],
)
def test_hook_gate_reports_identity_evidence_only_for_a_verified_identity_grant(
    identity_grant, verified, evidence
):
    result = ToolHookResult(action=TOOL_AUTO_APPROVE, identity_grant=identity_grant)
    event = _low_fidelity_event(identity_verified=verified)
    assert HookGate(lambda _e: result).judge(Ask(event, None, "k")).evidence is evidence


@pytest.mark.asyncio
async def test_spec_hooks_consult_the_gate_only_on_a_gated_turn():
    calls: list = []

    async def pre_tool(store, hooks, cwd, title, tool_input, **kw):
        calls.append((store, hooks, cwd, title, tool_input, kw))
        return "guard.sh: hook denied"

    event = _event(tool_input='{"command": "ls"}', mcp_server_name="srv", harness_tool_id="bash")
    ask = Ask(event, None, "k")
    floor = SpecHooks(
        TurnSpecHooks(["h"], "/w", False, True),
        store=lambda: "store",
        pre_tool=pre_tool,
        parent_session_key="p",
        agent_role="role",
    )
    assert await floor.refuse(ask) == Refusal.host(
        "spec_hook", "guard.sh: hook denied", DENY_CAUSE_POLICY
    )
    assert calls == [
        (
            "store",
            ["h"],
            "/w",
            "Run: ls -la",
            '{"command": "ls"}',
            {
                "tool_identity": "execute_bash",
                "mcp_server": "srv",
                "harness_tool_id": "bash",
                "parent_session_key": "p",
                "agent_role": "role",
            },
        )
    ]
    ungated = SpecHooks(TurnSpecHooks([], None, False, False), store=lambda: 1, pre_tool=pre_tool)
    assert await ungated.refuse(ask) is None and len(calls) == 1


@pytest.mark.asyncio
async def test_spec_hooks_that_cannot_be_read_block_and_a_passing_gate_does_not():
    async def passes(*_a, **_kw):
        return None

    ask = Ask(_event(), None, "k")
    unreadable = SpecHooks(TurnSpecHooks([], None, True, True), store=lambda: 1, pre_tool=passes)
    assert (await unreadable.refuse(ask)).reason == tool_permission.SPEC_HOOKS_UNREADABLE
    passing = SpecHooks(TurnSpecHooks([], None, False, True), store=lambda: 1, pre_tool=passes)
    assert await passing.refuse(ask) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("pct", "torn"), [(89.9, False), (90.0, True), (95.0, True)])
async def test_context_overflow_tears_down_at_its_threshold(pct, torn):
    client = SimpleNamespace(context_usage_pct=lambda: pct)
    refusal = await ContextOverflow(client, threshold=90.0).refuse(Ask(_event(), None, "k"))
    assert (refusal == Refusal.teardown("context_overflow", {"pct": pct})) is torn
    assert torn or refusal is None


# ── CallbackResponder ────────────────────────────────────────────────────────


class _Watch:
    def __init__(self, log: list) -> None:
        self.log = log

    def opened(self, ask: Ask) -> str:
        self.log.append(("opened", ask.event.title))
        return "token"

    def closed(self, token: object, decision: str, by: str) -> None:
        self.log.append(("closed", token, decision, by))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "approved"), [(True, True), (False, False), (1, True), ("", False)]
)
async def test_the_callback_is_resolved_before_the_watch_opens_and_its_answer_closes_it(
    returned, approved
):
    log: list = []

    async def approve(event):
        log.append(("approve", event.title))
        return returned

    def approver():
        log.append(("resolve",))
        return approve

    responder = CallbackResponder(
        approver, attended=lambda: True, name="callback", watch=_Watch(log)
    )
    answer = await responder.ask(Ask(_event(), None, "k"))
    decision = "approved" if approved else "rejected"
    assert log == [
        ("resolve",),
        ("opened", "Run: ls -la"),
        ("approve", "Run: ls -la"),
        ("closed", "token", decision, ""),
    ]
    assert answer == Answer(approved=approved)


@pytest.mark.asyncio
async def test_an_approver_error_is_a_rejection_only_when_the_policy_says_so():
    log: list = []

    async def broken(event):
        raise RuntimeError("the approval surface exploded")

    tolerant = CallbackResponder(
        lambda: broken,
        attended=lambda: True,
        name="child",
        watch=_Watch(log),
        on_error=lambda: log.append(("on_error",)),
    )
    assert await tolerant.ask(Ask(_event(), None, "k")) == Answer(approved=False)
    assert log[1:] == [("on_error",), ("closed", "token", "rejected", "host")]

    log.clear()
    strict = CallbackResponder(lambda: broken, attended=lambda: True, name="cb", watch=_Watch(log))
    wire = RecordingWire(log)
    policy = _policy(log, responders=(strict,))
    with pytest.raises(RuntimeError):
        await settle(Ask(_event(), wire, "k"), policy)
    assert wire.answers() == [], "the request is left to the caller's teardown"
    assert ("closed", "token", "rejected", "host") in log


@pytest.mark.asyncio
async def test_a_cancelled_wait_closes_as_a_host_decline_and_leaves_the_wire_alone():
    log: list = []
    entered = asyncio.Event()

    async def parked(event):
        entered.set()
        await asyncio.Event().wait()

    responder = CallbackResponder(
        lambda: parked, attended=lambda: True, name="callback", watch=_Watch(log)
    )
    wire = RecordingWire(log)
    settling = asyncio.ensure_future(
        settle(Ask(_event(), wire, "k"), _policy(log, responders=(responder,)))
    )
    await asyncio.wait_for(entered.wait(), 10)
    settling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settling
    assert log[-1] == ("closed", "token", "rejected", "host")
    assert wire.answers() == []


@pytest.mark.asyncio
async def test_attended_is_asked_of_the_responder_each_time():
    attached: list = []
    responder = CallbackResponder(
        lambda: AsyncMock(return_value=True), attended=lambda: bool(attached), name="cb"
    )
    assert responder.attended is False
    attached.append(1)
    assert responder.attended is True
    assert await responder.ask(Ask(_event(), None, "k")) == Answer(approved=True)


# ── SelAudit and the row codecs ──────────────────────────────────────────────


class _Sel:
    """A recording ``sel()`` whose ``log_tool_invocation`` may raise."""

    def __init__(self, log: list, *, fail: bool = False) -> None:
        self.log = log
        self.fail = fail

    def __call__(self) -> _Sel:
        return self

    def log_tool_invocation(self, **row) -> None:
        self.log.append(("row", row))
        if self.fail:
            raise RuntimeError("SEL trust root unloadable")


def _audit(log: list, mode: str, *, fail: bool = False) -> SelAudit:
    return SelAudit(
        SubagentRows("a1"),
        on_refusal_failure=mode,  # type: ignore[arg-type]
        sel=_Sel(log, fail=fail),
        log=logging.getLogger("test_tool_permission"),
    )


@pytest.mark.asyncio
async def test_sel_audit_in_answer_mode_logs_a_failed_refusal_row_and_still_answers(caplog):
    log: list = []
    policy = _policy(log, audit=_audit(log, "answer", fail=True))
    with caplog.at_level(logging.ERROR, logger="test_tool_permission"):
        await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert [entry[0] for entry in log] == ["gate", "narrate", "row", "steer", "reject"]
    (record,) = [r for r in caplog.records if r.name == "test_tool_permission"]
    assert record.getMessage() == (
        "SEL audit of subagent tool rejection failed; steering and rejecting anyway so the "
        "request is still answered"
    )


@pytest.mark.asyncio
async def test_sel_audit_in_withhold_mode_raises_before_the_wire():
    log: list = []
    policy = _policy(log, audit=_audit(log, "withhold", fail=True))
    with pytest.raises(RuntimeError):
        await settle(Ask(_event(), RecordingWire(log), "k"), policy)
    assert RecordingWire(log).answers() == []


@pytest.mark.asyncio
async def test_sel_audit_records_a_declined_name_through_the_surfaces_own_sel(names):
    names.verdicts["Run: ls -la"] = _SHADOWED
    log: list = []
    policy = _policy(
        log,
        audit=_audit(log, "answer"),
        gate=Judges(log, Hit("hook_auto_approve", Evidence.NAME, vouch=True)),
    )
    await settle(Ask(_event(), RecordingWire(log), "subagent:a1"), policy)
    (decline,) = [
        row
        for kind, *rest in log
        if kind == "row"
        for row in rest
        if row["outcome"] == "auto_approve_declined"
    ]
    assert decline["source"] == "subagent" and decline["session_key"] == "subagent:a1"
    assert decline["metadata"] == {
        "subagent_id": "a1",
        "reason": "name_grant",
        "code": "shadowed",
        "tier": "hook_auto_approve",
    }
    assert _SHADOWED.detail not in repr(decline)


def _row(codec, kind: str, ask: Ask, decided) -> dict:
    return codec.refusal(ask, decided) if kind == "refusal" else codec.approval(ask, decided)


_SUB_REFUSALS = [
    (
        Refusal.host("spec_hook", "x", DENY_CAUSE_POLICY),
        "denied",
        "hook_deny",
        {"subagent_id": "a1", "reason": "spec_hook"},
    ),
    (Refusal.host("hook_deny", "x", DENY_CAUSE_POLICY), "denied", "hook_deny", None),
    (
        Refusal.host("child_unattended", "x", DENY_CAUSE_SURFACE_POLICY),
        "denied",
        "child_origin_no_command_context",
        None,
    ),
    (Refusal.person("child"), "denied", "child_interactive_rejected", None),
    (
        Refusal.person("factory"),
        "rejected",
        "",
        {"subagent_id": "a1", "reason": "factory_rejected"},
    ),
    (Refusal.person("callback"), "rejected", "", None),
    (
        Refusal.host("headless", "x", DENY_CAUSE_SURFACE_POLICY),
        "rejected",
        "",
        {"subagent_id": "a1", "reason": "no_policy_deny_default"},
    ),
    (Refusal.teardown("turn_limit"), "denied", "turn_limit", None),
    (Refusal.teardown("child_escalation_limit"), "denied", "child_escalation_limit", None),
]


@pytest.mark.parametrize(("refusal", "outcome", "error", "metadata"), _SUB_REFUSALS)
def test_the_subagent_refusal_rows(refusal, outcome, error, metadata):
    ask = Ask(_event(), None, "subagent:a1")
    assert SubagentRows("a1").refusal(ask, refusal) == {
        "session_key": "subagent:a1",
        "source": "subagent",
        "tool_name": "Run: ls -la",
        "tool_kind": "execute",
        "outcome": outcome,
        "request_id": "req-1",
        "error": error,
        "metadata": metadata,
    }


_CHILD = {"child_mcp_identity": "remote/search", "child_args_unverified": True}


@pytest.mark.parametrize(
    ("settled", "low", "outcome", "metadata"),
    [
        (
            Settled("auto_approved", "hook_auto_approve", "rule"),
            False,
            "auto_approved",
            {"subagent_id": "a1", "reason": "hook_auto_approve"},
        ),
        (
            Settled("auto_approved", "hook_auto_approve", "rule"),
            True,
            "auto_approved",
            {"subagent_id": "a1", "reason": "hook_identity_auto_approve", **_CHILD},
        ),
        (
            Settled("auto_approved", "parent_policy_auto", "rule"),
            False,
            "auto_approved",
            {"subagent_id": "a1", "reason": "parent_policy_auto"},
        ),
        (
            Settled("auto_approved", "parent_policy_auto", "rule"),
            True,
            "auto_approved",
            {"subagent_id": "a1", "reason": "parent_policy_auto", **_CHILD},
        ),
        (
            Settled("approved", "child", "person"),
            True,
            "auto_approved",
            {"subagent_id": "a1", "reason": "child_interactive_approved"},
        ),
        (Settled("approved", "factory", "person"), False, "approved", {"subagent_id": "a1"}),
        (Settled("approved", "callback", "person"), False, "approved", {"subagent_id": "a1"}),
        (
            Settled("floor_refused", "callback", "person"),
            False,
            OUTCOME_REJECTED_TRANSPORT_FLOOR,
            {"subagent_id": "a1"},
        ),
        (
            Settled("floor_refused", "parent_policy_auto", "rule"),
            True,
            OUTCOME_REJECTED_TRANSPORT_FLOOR,
            {"subagent_id": "a1", "reason": "parent_policy_auto", **_CHILD},
        ),
    ],
)
def test_the_subagent_approval_rows(settled, low, outcome, metadata):
    event = _low_fidelity_event(identity_verified=True) if low else _event()
    row = SubagentRows("a1").approval(Ask(event, None, "subagent:a1"), settled)
    assert row == {
        "session_key": "subagent:a1",
        "source": "subagent",
        "tool_name": event.title,
        "tool_kind": "execute",
        "outcome": outcome,
        "request_id": "req-1",
        "metadata": metadata,
    }
    assert list(row["metadata"]) == list(metadata), "metadata key order is the row's bytes"


def _step() -> tuple[Project, Task]:
    run = Project(spec_path="t.md", spec_content="s", status="running", task_id="tid")
    run.source = "dashboard"
    task = Task(index=3, title="T", description="d")
    return run, task


@pytest.mark.parametrize(
    ("refusal", "extra"),
    [
        (
            Refusal.host("spec_hook", "x", DENY_CAUSE_POLICY),
            {"outcome": "rejected", "metadata": {"reason": "spec_hook_deny"}},
        ),
        (
            Refusal.host("hook_deny", "x", DENY_CAUSE_POLICY),
            {"outcome": "denied", "error": "hook_deny"},
        ),
        (
            Refusal.teardown("context_overflow", {"pct": 92.5}),
            {"outcome": "rejected", "metadata": {"reason": "context_overflow", "pct": 92.5}},
        ),
        (Refusal.person("responder"), {"outcome": "rejected"}),
        (
            Refusal.host("headless", "x", DENY_CAUSE_SURFACE_POLICY),
            {"outcome": "rejected", "metadata": {"reason": "headless_no_authorization"}},
        ),
    ],
)
def test_the_task_runner_refusal_rows(refusal, extra):
    run, task = _step()
    row = TaskrunnerRows("kirocrew", run, task).refusal(Ask(_event(), None, "k"), refusal)
    expected = {
        "session_key": "k",
        "agent": "kirocrew",
        "source": "taskrunner",
        "tool_name": "Run: ls -la",
        "tool_kind": "execute",
        "outcome": extra["outcome"],
        "request_id": "req-1",
    }
    expected.update({key: value for key, value in extra.items() if key != "outcome"})
    assert row == expected and list(row) == list(expected)


@pytest.mark.parametrize(
    ("settled", "outcome", "reason"),
    [
        (Settled("auto_approved", "hook_auto_approve", "rule"), "approved", "hook_auto_approve"),
        (Settled("auto_approved", "run_auto_approve", "rule"), "approved", "run_auto_approve"),
        (Settled("approved", "responder", "person"), "approved", "interactive_approved"),
        (
            Settled("floor_refused", "responder", "person"),
            OUTCOME_REJECTED_TRANSPORT_FLOOR,
            "interactive_approved",
        ),
    ],
)
def test_the_task_runner_approval_rows(settled, outcome, reason):
    run, task = _step()
    row = TaskrunnerRows("kirocrew", run, task).approval(Ask(_event(), None, "k"), settled)
    assert row["outcome"] == outcome and row["agent"] == "kirocrew"
    assert list(row["metadata"].items()) == [
        ("task", 3),
        ("task_id", "tid"),
        ("reason", reason),
        ("source", "dashboard"),
    ]
    assert TaskrunnerRows("kirocrew", run, task).decline(Ask(_event(), None, "k")) == {
        "agent": "kirocrew"
    }


# ── The production ladders ───────────────────────────────────────────────────


def _subagent_ladder(
    *,
    parent_policy: str = "ask",
    factory=None,
    callback=None,
    sel=None,
    hook=TOOL_ALLOW,
    spec=None,
    consult=None,
    approval_floor: str = "",
):
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=None,
        on_tool_approval=callback,
        on_tool_approval_factory=factory,
    )
    info = SubagentInfo(
        id="a1", task="t", parent_session_key="dashboard:chat-1", approval_floor=approval_floor
    )
    log: list = []
    policy = manager._run_events._permission_policy(
        info,
        spec=spec or TurnSpecHooks([], None, False, False),
        parent_policy=parent_policy,
        consult=consult or (lambda event: ToolHookResult(action=hook, identity_grant=True)),
        sel=sel or _Sel(log),
        log=logging.getLogger("kiro_crew.subagent"),
    )
    return policy, info, log


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kw", "low", "rung", "steered"),
    [
        ({}, False, "headless", True),
        ({"parent_policy": "auto"}, False, "parent_policy_auto", False),
        ({"parent_policy": "auto"}, True, "parent_policy_auto", False),
        ({"hook": TOOL_AUTO_APPROVE}, True, "hook_auto_approve", False),
        # Both grants stand: the gate's goes first at full fidelity, while for a
        # low-fidelity child parent_policy=auto is tried before the gate's grant.
        ({"parent_policy": "auto", "hook": TOOL_AUTO_APPROVE}, False, "hook_auto_approve", False),
        ({"parent_policy": "auto", "hook": TOOL_AUTO_APPROVE}, True, "parent_policy_auto", False),
        ({"hook": TOOL_DENY}, False, "hook_deny", True),
    ],
)
async def test_the_subagent_ladder(names, kw, low, rung, steered):
    policy, _info, log = _subagent_ladder(**kw)
    event = _low_fidelity_event(identity_verified=True) if low else _event()
    settled = await settle(Ask(event, RecordingWire(log), "subagent:a1"), policy)
    assert settled.rung == rung
    assert any(entry[0] == "steer" for entry in log) is steered
    if rung == "headless":
        assert settled.reason == _HEADLESS_DENY_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook", "low", "attended", "answer", "rung"),
    [
        # The gate's own grant (auto_approve_tools=["*"]) does not stand in for
        # the hub's person, at full fidelity or for a low-fidelity child.
        (TOOL_AUTO_APPROVE, False, False, True, "hub"),
        (TOOL_AUTO_APPROVE, True, False, True, "hub"),
        # A person on this gateway is not the floor's person: only the hub is asked.
        (TOOL_AUTO_APPROVE, False, True, True, "hub"),
        (TOOL_ALLOW, False, True, False, "hub"),
        # A deny still refuses before anyone is asked.
        (TOOL_DENY, False, True, None, "hook_deny"),
    ],
)
async def test_an_interactive_floor_hands_every_request_to_the_hub(
    names, hook, low, attended, answer, rung
):
    from kiro_crew.subagent_manager import hub_approvals

    asked: list = []

    async def callback(event, parent_session_key=""):
        asked.append(event.approval_floor)
        return True

    policy, _info, log = _subagent_ladder(
        hook=hook, callback=callback if attended else None, approval_floor="interactive"
    )
    event = _low_fidelity_event(identity_verified=True) if low else _event()
    pending = asyncio.create_task(settle(Ask(event, RecordingWire(log), "subagent:a1"), policy))
    if answer is not None:
        for _ in range(200):
            listed = hub_approvals.pending_for("a1")
            if listed:
                break
            await asyncio.sleep(0.01)
        assert len(listed) == 1 and listed[0]["title"] == event.title
        assert hub_approvals.resolve("a1", listed[0]["id"], answer)
    settled = await asyncio.wait_for(pending, timeout=5)
    assert settled.rung == rung
    if answer is not None:
        assert settled.outcome == ("approved" if answer else "rejected")
    assert asked == []
    assert hub_approvals.pending_for("a1") == []


@pytest.mark.asyncio
async def test_a_floored_runs_template_hooks_refuse_before_the_hub_is_asked(names):
    from kiro_crew.subagent_manager import hub_approvals

    # Hooks that cannot be read give no verdict, so they refuse like a deny.
    policy, _info, log = _subagent_ladder(
        spec=TurnSpecHooks([], None, True, True), approval_floor="interactive"
    )
    settled = await asyncio.wait_for(
        settle(Ask(_event(), RecordingWire(log), "subagent:a1"), policy), timeout=5
    )
    assert settled.rung == "spec_hook"
    assert hub_approvals.pending_for("a1") == []


@pytest.mark.asyncio
async def test_without_a_floor_the_callback_sees_no_floor():
    asked: list = []

    async def callback(event, parent_session_key=""):
        asked.append(event.approval_floor)
        return True

    policy, _info, log = _subagent_ladder(callback=callback)
    settled = await settle(Ask(_event(), RecordingWire(log), "subagent:a1"), policy)
    assert settled.rung == "callback" and asked == [""]


@pytest.mark.asyncio
async def test_the_subagent_ladder_asks_the_factory_before_the_gateway_and_counts_child_work():
    seen: list = []

    async def callback(event, parent_session_key=""):
        seen.append(("callback", parent_session_key))
        return True

    def factory(info):
        seen.append(("factory", info.id, info._awaiting_approval))

        async def _cb(event):
            seen.append(("factory_cb", info._awaiting_approval))
            return True

        return _cb

    policy, info, _log = _subagent_ladder(factory=factory, callback=callback)
    event = _event(sub_session_id="child-1")
    settled = await settle(Ask(event, RecordingWire([]), "subagent:a1"), policy)
    assert settled == Settled("approved", "factory", "person")
    assert seen == [("factory", "a1", False), ("factory_cb", True)]
    assert info.tool_count == 1 and info._awaiting_approval is False


@pytest.mark.asyncio
async def test_the_subagent_ladder_asks_for_a_low_fidelity_child_under_an_unverified_title():
    asked: list = []

    async def callback(event, parent_session_key=""):
        asked.append(event.title)
        raise RuntimeError("the approval surface exploded")

    policy, _info, log = _subagent_ladder(callback=callback)
    event = _low_fidelity_event(identity_verified=False)
    settled = await settle(Ask(event, RecordingWire(log), "subagent:a1"), policy)
    assert asked == [
        "⚠️ UNVERIFIED child request (security context missing — title is "
        "agent-authored): Run: ls -la"
    ]
    assert settled == Settled("rejected", "child", "person")
    (row,) = [entry[1] for entry in log if entry[0] == "row"]
    assert row["error"] == "child_interactive_rejected" and row["tool_name"] == asked[0]


@pytest.mark.asyncio
async def test_the_subagent_ladder_refuses_an_unattended_low_fidelity_child():
    policy, _info, log = _subagent_ladder()
    settled = await settle(
        Ask(_low_fidelity_event(identity_verified=False), RecordingWire(log), "subagent:a1"),
        policy,
    )
    assert settled == Settled(
        "refused", "child_unattended", "host", DENY_CAUSE_SURFACE_POLICY, _LOW_FIDELITY_DENY_REASON
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approver", "error"),
    [("factory", ""), ("callback", ""), ("child", "child_interactive_rejected")],
)
async def test_every_subagent_approvers_no_is_bare(approver, error):
    # A person's no: kiro-cli's own wording is the truth, so nothing is steered.
    no = AsyncMock(return_value=False)
    if approver == "factory":
        policy, _info, log = _subagent_ladder(factory=lambda info: no)
    else:
        policy, _info, log = _subagent_ladder(callback=no)
    event = _low_fidelity_event(identity_verified=False) if approver == "child" else _event()
    settled = await settle(Ask(event, RecordingWire(log), "subagent:a1"), policy)
    assert settled == Settled("rejected", approver, "person")
    assert [entry[0] for entry in log] == ["row", "reject"]
    assert log[0][1]["error"] == error


@pytest.mark.asyncio
@pytest.mark.parametrize("why", get_args(tool_permission.BailReason))
async def test_every_subagent_bail_is_audited_and_bare(why):
    policy, _info, log = _subagent_ladder(callback=AsyncMock(return_value=True))
    settled = await bail(Ask(_event(), RecordingWire(log), "subagent:a1"), policy, why)
    assert settled == Settled("bailed", why, "teardown")
    assert [entry[0] for entry in log] == ["row", "reject"]
    assert (log[0][1]["outcome"], log[0][1]["error"]) == ("denied", why)


def _taskrunner_ladder(
    *, ctx_result=None, on_tool_approval=None, auto_approve=False, pct=0.0, spec=None, consult=None
):
    run, task = _step()
    run.auto_approve = auto_approve
    run.last_task_time = 0.0
    log: list = []
    ctx = None
    if consult is not None:
        ctx = SimpleNamespace(hooks=SimpleNamespace(on_tool_call=lambda title, **kw: consult()))
    elif ctx_result is not None:
        ctx = SimpleNamespace(hooks=SimpleNamespace(on_tool_call=lambda title, **kw: ctx_result))
    policy = task_executor._step_permission_policy(
        client=SimpleNamespace(context_usage_pct=lambda: pct),
        ctx=ctx,
        spec=spec or TurnSpecHooks([], None, False, False),
        run=run,
        task=task,
        agent="",
        session_key="k",
        on_tool_approval=on_tool_approval,
    )
    return policy, run, log


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kw", "rung", "outcome", "touched"),
    [
        ({}, "headless", "refused", False),
        (
            {"ctx_result": ToolHookResult(action=TOOL_DENY, reason="no")},
            "hook_deny",
            "refused",
            False,
        ),
        (
            {"ctx_result": ToolHookResult(action=TOOL_AUTO_APPROVE)},
            "hook_auto_approve",
            "auto_approved",
            True,
        ),
        ({"on_tool_approval": AsyncMock(return_value=True)}, "responder", "approved", True),
        # The watchdog's stamp goes on before the person is asked.
        ({"on_tool_approval": AsyncMock(return_value=False)}, "responder", "rejected", True),
        (
            {"pct": 95.0, "on_tool_approval": AsyncMock(return_value=True)},
            "context_overflow",
            "bailed",
            False,
        ),
    ],
)
async def test_the_task_runner_ladder(names, monkeypatch, kw, rung, outcome, touched):
    monkeypatch.setattr(task_executor, "sel", _Sel([]))
    policy, run, _log = _taskrunner_ladder(**kw)
    settled = await settle(Ask(_event(), RecordingWire([]), "k"), policy)
    assert (settled.rung, settled.outcome) == (rung, outcome)
    assert (run.last_task_time > 0) is touched


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_the_task_runner_ladder_honours_the_runs_trust_only_while_it_is_live(
    monkeypatch, active
):
    calls: list = []
    override = SimpleNamespace(
        is_scope_active=lambda scope: calls.append(("active", scope)) or active,
        renew_scoped=lambda scope, source: calls.append(("renew", scope, source)),
        deactivate_scope=lambda scope: calls.append(("deactivate", scope)),
    )
    api: list = []
    monkeypatch.setattr(task_executor, "safety_override", lambda: override)
    sel = SimpleNamespace(
        log_api_access=lambda **kw: api.append(kw), log_tool_invocation=lambda **kw: None
    )
    monkeypatch.setattr(task_executor, "sel", lambda: sel)
    policy, run, _log = _taskrunner_ladder(auto_approve=True)
    settled = await settle(Ask(_event(), RecordingWire([]), "k"), policy)
    scope = "taskrunner:tid:autoapprove"
    if active:
        assert settled.rung == "run_auto_approve"
        assert calls == [("active", scope), ("renew", scope, "dashboard")]
        assert run.auto_approve is True and api == []
    else:
        assert settled.rung == "headless"
        assert calls == [("active", scope), ("deactivate", scope)]
        assert run.auto_approve is False
        assert api == [
            {
                "caller": "taskrunner",
                "operation": "task.auto_approve_expired",
                "outcome": "expired",
                "source": "taskrunner",
                "resources": "task-3",
            }
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["subagent", "taskrunner"])
async def test_each_ladder_states_its_own_rule_for_a_refusal_row_it_cannot_write(
    monkeypatch, surface
):
    """The subagent answers anyway (an unanswered request hangs its turn); the
    task runner withholds the answer (a delivered rejection with no audit row is a
    guarantee it cannot meet)."""
    log: list = []
    failing = _Sel(log, fail=True)
    if surface == "subagent":
        policy, _info, _log = _subagent_ladder(sel=failing)
    else:
        monkeypatch.setattr(task_executor, "sel", failing)
        policy, _run, _log = _taskrunner_ladder()
    wire = RecordingWire(log)
    if surface == "subagent":
        await settle(Ask(_event(), wire, "k"), policy)
        assert wire.answers() == [("reject", "req-1")]
    else:
        with pytest.raises(RuntimeError):
            await settle(Ask(_event(), wire, "k"), policy)
        assert wire.answers() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["subagent", "taskrunner"])
async def test_each_ladder_gives_its_spec_hooks_the_callers_identity(monkeypatch, surface):
    seen: list = []

    async def pre_tool(store, hooks, cwd, title, tool_input, **kw):
        seen.append((store, hooks, cwd, title, tool_input, kw))
        return None

    # Each ladder is built BEFORE the seam is patched: both read it per request.
    spec = TurnSpecHooks(["hook"], "/work", False, True)
    if surface == "subagent":
        policy, _info, _log = _subagent_ladder(spec=spec)
        monkeypatch.setattr(run_mod, "permission_pre_tool_block", pre_tool)
        store, identity = None, {
            "subagent_id": "a1",
            "parent_session_key": "dashboard:chat-1",
            "agent_role": None,
        }
    else:
        policy, _run, _log = _taskrunner_ladder(spec=spec)
        monkeypatch.setattr(task_executor, "permission_pre_tool_block", pre_tool)
        store = object()
        monkeypatch.setattr(task_executor, "get_global_hook_store", lambda: store)
        identity = {"parent_session_key": "k", "agent_role": "kirocrew"}
    event = _event(mcp_server_name="srv", harness_tool_id="h1")
    refusal = await policy.floors[0].refuse(Ask(event, RecordingWire([]), "k"))
    assert refusal is None
    assert seen == [
        (
            store,
            ["hook"],
            "/work",
            "Run: ls -la",
            event.tool_input,
            {
                "tool_identity": "execute_bash",
                "mcp_server": "srv",
                "harness_tool_id": "h1",
                **identity,
            },
        )
    ]


# ── Every rung a production ladder can settle on has its SEL row ─────────────


def _alternating_gate():
    """A hook gate that denies, then auto-approves: both of its verdicts, in turn."""
    verdicts = iter(
        [
            ToolHookResult(action=TOOL_DENY, reason="no"),
            ToolHookResult(action=TOOL_AUTO_APPROVE, identity_grant=True),
        ]
    )
    return lambda *_args: next(verdicts)


async def _ladder_rungs(policy: Policy, ask: Ask) -> tuple[set[str], set[str]]:
    """The (refusal, approval) rungs *policy* can settle on, read from its own stages.

    The policy is built so that every stage fires on *ask*; a stage that does not
    fails here, so a stage added to a builder cannot slip past the enumeration.
    """
    refusals = {policy.otherwise.rung}
    approvals: set[str] = set()
    if policy.child.enforced:
        assert policy.child.unattended is not None
        refusals.add(policy.child.unattended.rung)
    for floor in policy.floors:
        refusal = await floor.refuse(ask)
        assert refusal is not None, f"floor {floor!r} did not refuse"
        refusals.add(refusal.rung)
    verdicts = [policy.gate.judge(ask), policy.gate.judge(ask)]
    refusals |= {verdict.rung for verdict in verdicts if isinstance(verdict, Refusal)}
    approvals |= {verdict.reason for verdict in verdicts if isinstance(verdict, Hit)}
    assert {type(verdict) for verdict in verdicts} == {Refusal, Hit}, verdicts
    for grant in (*policy.grants, *policy.child.grants):
        if grant is GATE_GRANT:
            continue
        hit = await grant.offer(ask)
        assert hit is not None, f"grant {grant!r} made no offer"
        approvals.add(hit.reason)
    if policy.interject is not None:
        teardown = await policy.interject.refuse(ask)
        assert teardown is not None, "the interject did not tear the request down"
        refusals.add(teardown.rung)
    responders = [*policy.responders]
    if policy.child.responder is not None:
        responders.append(policy.child.responder)
    for responder in responders:
        assert responder.attended, f"responder {responder.name!r} is not attached"
        # A person's yes is approved under the responder's name, a no refused under it.
        refusals.add(responder.name)
        approvals.add(responder.name)
    return refusals, approvals


@pytest.mark.asyncio
async def test_every_rung_the_subagent_ladder_can_settle_on_has_its_row():
    policy, _info, _log = _subagent_ladder(
        parent_policy="auto",
        factory=lambda info: AsyncMock(return_value=True),
        callback=AsyncMock(return_value=True),
        spec=TurnSpecHooks([], None, True, True),
        consult=_alternating_gate(),
    )
    refusals, approvals = await _ladder_rungs(policy, Ask(_event(), RecordingWire([]), "k"))
    # The run bails at its own limits.
    refusals |= set(get_args(tool_permission.BailReason))
    # A floored run swaps every person on this gateway for the remote hub's.
    floored, _info, _log = _subagent_ladder(approval_floor="interactive")
    for responder in (*floored.responders, floored.child.responder):
        refusals.add(responder.name)
        approvals.add(responder.name)
    assert refusals == set(tool_permission._SUBAGENT_REFUSALS)
    assert approvals == set(tool_permission._SUBAGENT_APPROVALS)


@pytest.mark.asyncio
async def test_every_rung_the_task_runner_ladder_can_settle_on_has_its_row(monkeypatch):
    override = SimpleNamespace(
        is_scope_active=lambda scope: True, renew_scoped=lambda scope, source: None
    )
    monkeypatch.setattr(task_executor, "safety_override", lambda: override)
    policy, _run, _log = _taskrunner_ladder(
        on_tool_approval=AsyncMock(return_value=True),
        auto_approve=True,
        pct=95.0,
        spec=TurnSpecHooks([], None, True, True),
        consult=_alternating_gate(),
    )
    refusals, approvals = await _ladder_rungs(policy, Ask(_event(), RecordingWire([]), "k"))
    assert refusals == set(tool_permission._TASKRUNNER_REFUSALS)
    assert approvals == set(tool_permission._TASKRUNNER_APPROVALS)


# ── Where the wire is answered (a source pin with no behavioural equivalent) ──
#
# Kept as a pin: no behaviour test can see a FUTURE answer path that bypasses the
# ladder. The surfaces' own suites pin that their modules hold none.


def _wire_calls(path) -> list[tuple[str, str]]:
    """``(enclosing class.function, called name)`` for every answer-shaped call in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                # Any ``.allow(`` / ``.refuse(`` is the port's answer unless it asks a
                # stage, so an alias such as ``w = ask.wire`` cannot hide one.
                on_wire = isinstance(func, ast.Attribute) and (
                    ast.unparse(func.value) not in _STAGE_RECEIVERS
                )
                if name in _ANSWER_CALLS or (on_wire and name in _WIRE_ANSWERS):
                    found.append((scope, name))
            visit(child, inner)

    visit(tree, "")
    return found


_ANSWER_CALLS = frozenset({"approve_tool", "reject_tool", "_steer_host_deny"})
_WIRE_ANSWERS = frozenset({"allow", "refuse"})
# The ladder's stages that also expose ``refuse``: a floor and the interject.
_STAGE_RECEIVERS = frozenset({"floor", "policy.interject"})
_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def test_the_ladder_answers_the_wire_in_exactly_one_adapter():
    assert sorted(_wire_calls(_SRC / "tool_permission.py")) == [
        ("AcpWire.allow", "approve_tool"),
        ("AcpWire.refuse", "_steer_host_deny"),
        ("AcpWire.refuse", "reject_tool"),
        ("_allow", "allow"),
        ("_refuse", "refuse"),
    ]
    assert tool_permission._steer_host_deny is llm_helpers._steer_host_deny
