"""Settle one ACP tool-permission request: decide it, audit it, answer it once.

A tool call reaches a surface as ``session/request_permission`` and is answered
on the wire at most once, whatever happens on the way. Deciding it is a
ladder -- floors, the hook gate, grants, an interject, a person, and the
surface's unattended refusal -- and answering it is a fixed sequence: write the
audit row, tell the model when the HOST refused (the in-band deny notice), then
approve or reject on the wire. :func:`settle` owns that whole sequence for a
surface described by a :class:`Policy`, and :func:`bail` answers a request a run
is abandoning. A caller owns none of the ordering.

The ladder, in order:

1. each floor may refuse;
2. the gate may refuse; otherwise its auto-approve becomes a :class:`Hit`;
3. the grants are walked in order, the gate's hit standing at
   :data:`GATE_GRANT`'s position. A low-fidelity child request under an enforced
   :class:`ChildRule` walks the rule's own grants instead and admits a hit only on
   evidence that reads no agent-authored event data. Otherwise the gate's
   auto-approve must still pass :func:`name_grant.refusal_for_event`; a refused
   name is narrated and audited and DOWNGRADES to the next stage, it never
   blocks. The first admitted hit is held;
4. the interject may tear the request down;
5. a held hit is approved; otherwise the first attended responder asks the
   person; otherwise the unattended refusal answers.

Invariants (``test/test_tool_permission.py`` pins each through this interface):

* every :func:`settle` / :func:`bail` that returns has answered the wire exactly
  once. The exceptions are deliberate: a refusal row that cannot be written under
  ``on_refusal_failure="withhold"`` raises before the wire, and a cancellation
  (or an approver error the policy lets propagate) while a person is asked
  leaves the request to the caller's teardown;
* a refusal is audited before any wire I/O: the steer is one more bounded await
  on the ACP pipe, and a backend that stops reading stdin cancels the caller at
  its turn deadline, which would leave the decision acted on and never audited
  if the row came last. An approval is audited after the wire answered, because
  its row records whether the transport floor turned it into a rejection;
* the deny notice is steered exactly when the host refused (a refusal with a
  cause), and always before the reject: while the request is unanswered the turn
  is provably in flight, which is what gets the notice queued rather than
  dropped (see ``kiro_crew.deny_notice``). A person's no and a teardown stay
  bare, because kiro-cli's "User denied tool execution" is then the truth, or
  there is no continuing turn for a notice to correct.

Each surface's SEL vocabulary is a row codec here (:class:`SubagentRows`,
:class:`TaskrunnerRows`), so a surface's rows are kept byte for byte while the
sequencing is shared. The module adds no output path of its own: row fields come
from the event and the codecs, SEL scrubs its text fields at write, and the
notice is built and scrubbed by ``llm_helpers._steer_host_deny``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Protocol

from kiro_crew import name_grant
from kiro_crew.constants import DENY_CAUSE_POLICY
from kiro_crew.executors import run_in_tool_gate_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, identity_grant_covers_child
from kiro_crew.llm_helpers import _steer_host_deny
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR

#: An empty, read-only mapping: the default for every ``meta``.
EMPTY: Mapping[str, object] = MappingProxyType({})

#: The spec-hook floor's reason when the agent spec's hooks cannot be read: a
#: gate with no verdict blocks.
SPEC_HOOKS_UNREADABLE = "the agent spec's hooks could not be read"


class Evidence(Enum):
    """What a grant's decision reads, which decides whether a low-fidelity child may have it.

    ``NONE`` reads no event data (``parent_policy=auto``, a run-scoped trust
    grant); ``IDENTITY`` reads only the request's verified ``_meta.kiro``
    server/tool pair (the gate's identity-keyed grant, as
    ``hooks.identity_grant_covers_child`` judges it); ``NAME`` reads the title or the program
    name (the gate's plain auto-approve); ``CLASSIFIER`` reads the call's content
    (the gate's read-only classification).
    """

    NONE = "none"
    IDENTITY = "identity"
    NAME = "name"
    CLASSIFIER = "classifier"


@dataclass(frozen=True)
class Hit:
    """A grant's offer to approve.

    *reason* is the grant's label, which the row codec records (and the tier a
    refused program name is audited under). *vouch* marks the gate's grant, whose
    program names must still resolve to the programs they appear to name before
    it is honoured.
    """

    reason: str
    evidence: Evidence
    vouch: bool = False


@dataclass(frozen=True)
class Refusal:
    """A decision to refuse, and who made it.

    ``by="host"`` REQUIRES a *cause* (a ``kiro_crew.constants.DENY_CAUSE_*``):
    it is what the deny notice tells the model, because kiro-cli otherwise
    reports every rejection as "User denied tool execution". ``by="person"`` (an
    approver said no, or raised where the surface counts that as a no) and
    ``by="teardown"`` (the run is abandoning the request)
    carry none and stay bare: kiro-cli's wording is then the truth, or moot.
    *rung* names the stage that refused, for the row codec; *reason* is the
    host's wording for the notice; *meta* is stage data a codec records.
    """

    by: Literal["host", "person", "teardown"]
    rung: str
    reason: str = ""
    cause: str | None = None
    meta: Mapping[str, object] = EMPTY

    def __post_init__(self) -> None:
        if self.by == "host" and not self.cause:
            raise ValueError(f"a host refusal needs a deny cause (rung={self.rung!r})")
        if self.by != "host" and self.cause is not None:
            raise ValueError(f"only a host refusal carries a deny cause (rung={self.rung!r})")

    @classmethod
    def host(cls, rung: str, reason: str, cause: str) -> Refusal:
        return cls("host", rung, reason, cause)

    @classmethod
    def person(cls, rung: str) -> Refusal:
        return cls("person", rung)

    @classmethod
    def teardown(cls, rung: str, meta: Mapping[str, object] = EMPTY) -> Refusal:
        return cls("teardown", rung, meta=meta)


@dataclass(frozen=True)
class Notice:
    """What the wire steers before it rejects: the host's cause and wording."""

    cause: str
    reason: str


@dataclass(frozen=True)
class Answer:
    """A responder's answer: whether the person approved."""

    approved: bool


Outcome = Literal["approved", "auto_approved", "refused", "rejected", "floor_refused", "bailed"]

#: The limits a run abandons a request at (:func:`bail`); each is a refusal rung.
BailReason = Literal["turn_limit", "child_escalation_limit"]


@dataclass(frozen=True)
class Settled:
    """How a request was answered.

    *outcome* is ``auto_approved`` (a grant), ``approved`` (a person),
    ``refused`` (the host), ``rejected`` (a person), ``floor_refused`` (an
    approval the transport floor turned into a rejection) or ``bailed`` (a
    teardown). *rung* names the stage that decided and *by* who decided it;
    *cause* is set exactly when a deny notice was handed to the wire.
    """

    outcome: Outcome
    rung: str
    by: Literal["rule", "person", "host", "teardown"]
    cause: str | None = None
    reason: str = ""
    meta: Mapping[str, object] = EMPTY

    @property
    def approved(self) -> bool:
        return self.outcome in ("approved", "auto_approved")


class Ask:
    """One permission request on its way through :func:`settle`.

    The fidelity facts are read from the event once, on first use.
    """

    def __init__(self, event: Any, wire: Wire, session_key: str) -> None:
        self.event = event
        self.wire = wire
        self.session_key = session_key

    @property
    def request_id(self) -> Any:
        return self.event.request_id

    @cached_property
    def low_fidelity(self) -> bool:
        """A backend-child request whose security context is absent."""
        return bool(self.event.child_low_fidelity)

    @cached_property
    def grant_eligible(self) -> bool:
        """An unconditional grant may honour it: full fidelity, or a verified identity."""
        return bool(self.event.child_unconditional_grant_eligible)


# ── Ports ────────────────────────────────────────────────────────────────────


class Wire(Protocol):
    """Where the request is answered."""

    async def allow(self, ask: Ask) -> bool:
        """Approve; False when the transport floor turned the approval into a rejection."""
        ...

    async def refuse(self, ask: Ask, notice: Notice | None) -> None:
        """Reject, steering *notice* first when there is one."""
        ...


class Gate(Protocol):
    """The hook gate: a synchronous verdict."""

    def judge(self, ask: Ask) -> Refusal | Hit | None: ...


class Floor(Protocol):
    """A stage before the gate that can only refuse."""

    async def refuse(self, ask: Ask) -> Refusal | None: ...


class Grant(Protocol):
    """A stage that can only offer to approve."""

    async def offer(self, ask: Ask) -> Hit | None: ...


class Interject(Protocol):
    """A last check before the answer that can only tear the request down."""

    async def refuse(self, ask: Ask) -> Refusal | None: ...


class Responder(Protocol):
    """A person, asked through some surface; *name* is the rung it answers as."""

    name: str

    @property
    def attended(self) -> bool: ...

    async def ask(self, ask: Ask) -> Answer: ...


class Audit(Protocol):
    """Writes the audit rows; raising means a row could not be written."""

    def refused(self, ask: Ask, refusal: Refusal) -> None: ...

    def approved(self, ask: Ask, settled: Settled) -> None: ...

    def declined(self, ask: Ask, refusal: name_grant.Refusal, tier: str) -> None: ...


class Narrator:
    """A surface's own bookkeeping, called at fixed points; the base does nothing.

    :meth:`refusing` runs before a refusal is audited, :meth:`declined` before a
    refused program name is audited, and :meth:`allowed` after the wire answered
    an approval (*sent* is False when the transport floor turned it into a
    rejection) and before it is audited. A narrator may move run state (a
    counter the run's replay gates read, a watchdog stamp), not only log.
    """

    def refusing(self, ask: Ask, refusal: Refusal) -> None:
        return None

    def declined(self, ask: Ask, refusal: name_grant.Refusal) -> None:
        return None

    def allowed(self, ask: Ask, sent: bool) -> None:
        return None


#: The narrator of a surface with no bookkeeping.
SILENT = Narrator()


class _GateGrant:
    async def offer(self, ask: Ask) -> Hit | None:  # pragma: no cover - settle never calls it
        raise AssertionError("GATE_GRANT is the gate's own hit, placed by settle")


#: The gate's auto-approve's position among :attr:`Policy.grants`; listed exactly once.
GATE_GRANT: Grant = _GateGrant()


class _NoGate:
    def judge(self, ask: Ask) -> None:
        return None


#: The gate of a surface without a hook manager: no verdict and no grant.
NO_GATE: Gate = _NoGate()


@dataclass(frozen=True)
class ChildRule:
    """How a low-fidelity backend-child request is settled when the rule is enforced.

    Such a request carries no recoverable security context, so everything a name
    or content grant would judge is agent-authored. Under an enforced rule it
    walks :attr:`grants` instead of the policy's (a surface may order them
    differently for such a request), a grant on :attr:`Evidence.NONE` stands only
    when the event is eligible for an unconditional grant, one on
    :attr:`Evidence.IDENTITY` stands, and no other grant does; no program name is
    vouched for. The person is asked through :attr:`responder`, with the title
    rewritten by :attr:`annotate` so the prompt says what is missing, and with
    nobody to ask :attr:`unattended` answers.
    """

    enforced: bool = False
    grants: tuple[Grant, ...] = ()
    responder: Responder | None = None
    annotate: Callable[[str], str] | None = None
    unattended: Refusal | None = None

    @classmethod
    def ignore(cls) -> ChildRule:
        return cls()

    @classmethod
    def enforce(
        cls,
        *,
        grants: tuple[Grant, ...],
        unattended: Refusal,
        responder: Responder | None = None,
        annotate: Callable[[str], str] | None = None,
    ) -> ChildRule:
        return cls(True, grants, responder, annotate, unattended)


@dataclass(frozen=True)
class Policy:
    """A surface's permission ladder; the module docstring gives the order."""

    gate: Gate
    audit: Audit
    otherwise: Refusal
    floors: tuple[Floor, ...] = ()
    grants: tuple[Grant, ...] = (GATE_GRANT,)
    interject: Interject | None = None
    responders: tuple[Responder, ...] = ()
    child: ChildRule = field(default_factory=ChildRule.ignore)
    narrator: Narrator = SILENT

    def __post_init__(self) -> None:
        if sum(grant is GATE_GRANT for grant in self.grants) != 1:
            raise ValueError("GATE_GRANT must appear exactly once in Policy.grants")
        if sum(grant is GATE_GRANT for grant in self.child.grants) > 1:
            raise ValueError("GATE_GRANT may appear at most once in ChildRule.grants")
        if self.child.enforced and self.child.unattended is None:
            raise ValueError("an enforced child rule needs its unattended refusal")


# ── The ladder ───────────────────────────────────────────────────────────────


async def settle(ask: Ask, policy: Policy) -> Settled:
    """Decide, audit and answer *ask* under *policy*; see the module docstring."""
    for floor in policy.floors:
        refusal = await floor.refuse(ask)
        if refusal is not None:
            return await _refuse(ask, policy, refusal)
    # The gate's path tier waits on the mc-pathres resolver on the calling
    # thread; run it on the tool-gate pool so a slow resolution parks a gate
    # worker, not the event loop.
    verdict = await run_in_tool_gate_pool(policy.gate.judge, ask)
    if isinstance(verdict, Refusal):
        return await _refuse(ask, policy, verdict)
    low = _low_fidelity(ask, policy)
    held: Hit | None = None
    for grant in policy.child.grants if low else policy.grants:
        hit = verdict if grant is GATE_GRANT else await grant.offer(ask)
        if hit is not None and await _admitted(ask, policy, hit, low):
            held = hit
            break
    if policy.interject is not None:
        teardown = await policy.interject.refuse(ask)
        if teardown is not None:
            return await _refuse(ask, policy, teardown)
    if held is not None:
        return await _allow(ask, policy, held.reason, "auto_approved", "rule")
    responder = policy.child.responder if low else _first_attended(policy.responders)
    if responder is not None and responder.attended:
        if low and policy.child.annotate is not None:
            ask.event.title = policy.child.annotate(ask.event.title)
        answer = await responder.ask(ask)
        if answer.approved:
            return await _allow(ask, policy, responder.name, "approved", "person")
        return await _refuse(ask, policy, Refusal.person(responder.name))
    if low:
        assert policy.child.unattended is not None  # Policy.__post_init__
        return await _refuse(ask, policy, policy.child.unattended)
    return await _refuse(ask, policy, policy.otherwise)


async def bail(ask: Ask, policy: Policy, why: BailReason) -> Settled:
    """Answer *ask* for a run that abandons it at a limit (*why* names the limit).

    Audited and rejected bare: the run's turn ends here, so there is no continuing
    turn for a deny notice to correct.
    """
    return await _refuse(ask, policy, Refusal.teardown(why))


def _low_fidelity(ask: Ask, policy: Policy) -> bool:
    return policy.child.enforced and ask.low_fidelity


def _first_attended(responders: tuple[Responder, ...]) -> Responder | None:
    return next((responder for responder in responders if responder.attended), None)


async def _admitted(ask: Ask, policy: Policy, hit: Hit, low: bool) -> bool:
    """Whether *hit* may stand: the child rule, else the program-name check."""
    if low:
        if hit.evidence is Evidence.NONE:
            return ask.grant_eligible
        return hit.evidence is Evidence.IDENTITY
    if not hit.vouch:
        return True
    refused = await name_grant.refusal_for_event(ask.event)
    if refused is None:
        return True
    policy.narrator.declined(ask, refused)
    policy.audit.declined(ask, refused, hit.reason)
    return False


_OUTCOME_OF: Mapping[str, Outcome] = MappingProxyType(
    {"host": "refused", "person": "rejected", "teardown": "bailed"}
)


async def _refuse(ask: Ask, policy: Policy, refusal: Refusal) -> Settled:
    policy.narrator.refusing(ask, refusal)
    policy.audit.refused(ask, refusal)
    notice = Notice(refusal.cause, refusal.reason) if refusal.cause is not None else None
    await ask.wire.refuse(ask, notice)
    return Settled(
        _OUTCOME_OF[refusal.by],
        refusal.rung,
        refusal.by,
        refusal.cause,
        refusal.reason,
        refusal.meta,
    )


async def _allow(
    ask: Ask,
    policy: Policy,
    rung: str,
    outcome: Literal["approved", "auto_approved"],
    by: Literal["rule", "person"],
) -> Settled:
    sent = await ask.wire.allow(ask)
    policy.narrator.allowed(ask, sent)
    settled = Settled(outcome if sent else "floor_refused", rung, by)
    policy.audit.approved(ask, settled)
    return settled


# ── Adapters ─────────────────────────────────────────────────────────────────


class AcpWire:
    """An ACP client's permission channel: ``approve_tool`` / ``reject_tool``.

    A notice is steered through ``llm_helpers._steer_host_deny`` before the
    reject, while the request is unanswered and the turn provably in flight. That
    helper is best effort (a failed or slow steer is swallowed and the reject
    still runs) and, when cancelled mid-steer, schedules the reject itself before
    re-raising.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def allow(self, ask: Ask) -> bool:
        sent = await self.client.approve_tool(ask.request_id)
        return sent is not False

    async def refuse(self, ask: Ask, notice: Notice | None) -> None:
        if notice is not None:
            await _steer_host_deny(self.client, ask.event, notice.reason, cause=notice.cause)
        await self.client.reject_tool(ask.request_id)


class HookGate:
    """The hook gate's verdict, from the surface's own ``on_tool_call`` consult.

    *consult* is the surface's call of ``HookManager.on_tool_call`` for an event
    (each surface attributes the caller its own way). A deny refuses under the
    policy cause with the hook's own reason; an auto-approve becomes the gate's
    hit, whose evidence is the hook's own account of what it matched: the
    identity-keyed grant only where ``identity_grant_covers_child`` says the
    request's own identity is verified.

    *consult* stays in each surface's own module: it is that surface's attribution
    of the caller, and ``test_hooks``' extraction scan and
    ``test_app_spawn_capability`` read it there.
    """

    def __init__(self, consult: Callable[[Any], Any]) -> None:
        self._consult = consult

    def judge(self, ask: Ask) -> Refusal | Hit | None:
        result = self._consult(ask.event)
        if result.action == TOOL_DENY:
            return Refusal.host("hook_deny", result.reason or "", DENY_CAUSE_POLICY)
        if result.action != TOOL_AUTO_APPROVE:
            return None
        if identity_grant_covers_child(result, ask.event):
            evidence = Evidence.IDENTITY
        elif result.read_only:
            evidence = Evidence.CLASSIFIER
        else:
            evidence = Evidence.NAME
        return Hit("hook_auto_approve", evidence, vouch=True)


class DenyOnlyGate:
    """*inner*'s refusals, without its auto-approve.

    For a run under an interactive approval floor: the hook gate keeps every deny
    it would give, but its grant (``auto_approve_tools`` and the like) does not
    stand in for the person, so an allowed request goes on to a responder.
    """

    def __init__(self, inner: Gate) -> None:
        self._inner = inner

    def judge(self, ask: Ask) -> Refusal | None:
        verdict = self._inner.judge(ask)
        return verdict if isinstance(verdict, Refusal) else None


class SpecHooks:
    """The agent spec's PreToolUse hooks as a floor, on a turn they gate.

    A delivered deny, or a gate with no verdict (hooks that cannot be read),
    refuses under the policy cause with the gate's own reason. *pre_tool* is
    ``hooks.permission_pre_tool_block``; *store* is read per request; *identity*
    is the caller attribution the surface gives the hook payload.
    """

    def __init__(
        self,
        spec: Any,
        *,
        store: Callable[[], Any],
        pre_tool: Callable[..., Awaitable[str | None]],
        **identity: Any,
    ) -> None:
        self._spec = spec
        self._store = store
        self._pre_tool = pre_tool
        self._identity = identity

    async def refuse(self, ask: Ask) -> Refusal | None:
        if not self._spec.gated:
            return None
        reason: str | None
        if self._spec.unreadable:
            reason = SPEC_HOOKS_UNREADABLE
        else:
            event = ask.event
            reason = await self._pre_tool(
                self._store(),
                self._spec.hooks,
                self._spec.cwd,
                event.title,
                event.tool_input,
                tool_identity=event.tool_name,
                mcp_server=event.mcp_server_name,
                harness_tool_id=event.harness_tool_id,
                **self._identity,
            )
        if reason is None:
            return None
        return Refusal.host("spec_hook", reason, DENY_CAUSE_POLICY)


class ParentPolicyAuto:
    """``parent_policy=auto``: an unconditional grant, read once at run start."""

    def __init__(self, parent_policy: str) -> None:
        self._on = parent_policy == "auto"

    async def offer(self, ask: Ask) -> Hit | None:
        return Hit("parent_policy_auto", Evidence.NONE) if self._on else None


class ContextOverflow:
    """Tears the request down once the session's context passes *threshold* percent."""

    def __init__(self, client: Any, *, threshold: float) -> None:
        self._client = client
        self._threshold = threshold

    async def refuse(self, ask: Ask) -> Refusal | None:
        pct = self._client.context_usage_pct()
        if pct >= self._threshold:
            return Refusal.teardown("context_overflow", {"pct": pct})
        return None


class ApprovalWatch(Protocol):
    """Observes a person being asked: :meth:`opened` before the wait, :meth:`closed` after.

    *by* is ``""`` when a person answered and ``"host"`` when the wait ended with
    no answer (the approver raised, or the wait was cancelled).
    """

    def opened(self, ask: Ask) -> object: ...

    def closed(self, token: object, decision: str, by: str) -> None: ...


class CallbackResponder:
    """A person, asked through a host callback.

    *approver* is looked up once per request, before the watch opens, and gives
    the callable to await with the event; the truthiness of what it returns is
    the decision. *attended* says whether anyone is attached. When the callable
    raises, *on_error* (when given) is called and the request is the person's
    rejection; without it the exception propagates, as a cancellation does, and
    the request is left to the caller's teardown.
    """

    def __init__(
        self,
        approver: Callable[[], Callable[[Any], Awaitable[object]]],
        *,
        attended: Callable[[], bool],
        name: str,
        watch: ApprovalWatch | None = None,
        on_error: Callable[[], None] | None = None,
    ) -> None:
        self._approver = approver
        self._attended = attended
        self.name = name
        self._watch = watch
        self._on_error = on_error

    @property
    def attended(self) -> bool:
        return self._attended()

    async def ask(self, ask: Ask) -> Answer:
        approve = self._approver()
        token = self._watch.opened(ask) if self._watch is not None else None
        decision, by = "rejected", "host"
        try:
            try:
                approved = bool(await approve(ask.event))
            except Exception:
                if self._on_error is None:
                    raise
                self._on_error()
                return Answer(approved=False)
            decision, by = ("approved" if approved else "rejected"), ""
            return Answer(approved=approved)
        finally:
            if self._watch is not None:
                self._watch.closed(token, decision, by)


class RowCodec(Protocol):
    """A surface's SEL vocabulary: the keyword arguments of each row it writes."""

    @property
    def source(self) -> str: ...

    def refusal(self, ask: Ask, refusal: Refusal) -> dict[str, Any]: ...

    def approval(self, ask: Ask, settled: Settled) -> dict[str, Any]: ...

    def decline(self, ask: Ask) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SelAudit:
    """The production :class:`Audit`: rows through the surface's own ``sel`` binding.

    *on_refusal_failure* has no default, so each surface states its rule for a
    refusal row that cannot be written: ``"answer"`` logs the failure on *log*
    and still answers the wire, ``"withhold"`` raises before the wire. An
    approval row is written after the wire answered, and a failure to write it
    raises. *sel* is the surface's own binding, so that surface's audit seam
    observes every row, the program-name declines included.
    """

    rows: RowCodec
    on_refusal_failure: Literal["answer", "withhold"]
    sel: Callable[[], Any]
    log: logging.Logger

    def refused(self, ask: Ask, refusal: Refusal) -> None:
        try:
            self.sel().log_tool_invocation(**self.rows.refusal(ask, refusal))
        except Exception:
            if self.on_refusal_failure == "withhold":
                raise
            self.log.exception(
                "SEL audit of "
                + self.rows.source
                + " tool rejection failed; steering and rejecting anyway so the request "
                "is still answered"
            )

    def approved(self, ask: Ask, settled: Settled) -> None:
        self.sel().log_tool_invocation(**self.rows.approval(ask, settled))

    def declined(self, ask: Ask, refusal: name_grant.Refusal, tier: str) -> None:
        name_grant.log_decline(
            source=self.rows.source,
            session_key=ask.session_key,
            event=ask.event,
            refusal=refusal,
            tier=tier,
            sel_factory=self.sel,
            **self.rows.decline(ask),
        )


# ── Row codecs ───────────────────────────────────────────────────────────────

#: The subagent's refusal rows by rung: (``error``, ``metadata.reason``). A row
#: with an error is a ``denied`` outcome, one without a ``rejected`` one.
_SUBAGENT_REFUSALS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "spec_hook": ("hook_deny", "spec_hook"),
        "hook_deny": ("hook_deny", ""),
        "child_unattended": ("child_origin_no_command_context", ""),
        "child": ("child_interactive_rejected", ""),
        "factory": ("", "factory_rejected"),
        "callback": ("", ""),
        "hub": ("", "hub_rejected"),
        "headless": ("", "no_policy_deny_default"),
        "turn_limit": ("turn_limit", ""),
        "child_escalation_limit": ("child_escalation_limit", ""),
    }
)

#: The subagent's approval rows by rung: ``metadata.reason``, if any. A row with
#: a reason is an ``auto_approved`` outcome, one without an ``approved`` one.
_SUBAGENT_APPROVALS: Mapping[str, str] = MappingProxyType(
    {
        "parent_policy_auto": "parent_policy_auto",
        "hook_auto_approve": "hook_auto_approve",
        "child": "child_interactive_approved",
        "factory": "",
        "callback": "",
        # A floored run's person, answering through the remote hub that placed it.
        "hub": "",
    }
)


@dataclass(frozen=True)
class SubagentRows:
    """The subagent surface's SEL rows (``source="subagent"``).

    A grant that stood for a low-fidelity child is recorded with the verified
    identity it rested on and ``child_args_unverified``, and the gate's grant
    there is the identity-keyed one, ``hook_identity_auto_approve``.
    """

    subagent_id: str
    source: ClassVar[str] = "subagent"

    def error(self, refusal: Refusal) -> str:
        return _SUBAGENT_REFUSALS[refusal.rung][0]

    def refusal(self, ask: Ask, refusal: Refusal) -> dict[str, Any]:
        error, reason = _SUBAGENT_REFUSALS[refusal.rung]
        return {
            "session_key": ask.session_key,
            "source": self.source,
            "tool_name": ask.event.title,
            "tool_kind": ask.event.tool_kind,
            "outcome": "denied" if error else "rejected",
            "request_id": ask.request_id,
            "error": error,
            "metadata": {"subagent_id": self.subagent_id, "reason": reason} if reason else None,
        }

    def approval(self, ask: Ask, settled: Settled) -> dict[str, Any]:
        child_grant = settled.by == "rule" and ask.low_fidelity
        reason = _SUBAGENT_APPROVALS[settled.rung]
        if child_grant and reason == "hook_auto_approve":
            reason = "hook_identity_auto_approve"
        metadata: dict[str, object] = {"subagent_id": self.subagent_id}
        if reason:
            metadata["reason"] = reason
        if child_grant:
            event = ask.event
            metadata["child_mcp_identity"] = f"{event.mcp_server_name}/{event.tool_name}"
            metadata["child_args_unverified"] = True
        if settled.outcome == "floor_refused":
            outcome = OUTCOME_REJECTED_TRANSPORT_FLOOR
        else:
            outcome = "auto_approved" if reason else "approved"
        return {
            "session_key": ask.session_key,
            "source": self.source,
            "tool_name": ask.event.title,
            "tool_kind": ask.event.tool_kind,
            "outcome": outcome,
            "request_id": ask.request_id,
            "metadata": metadata,
        }

    def decline(self, ask: Ask) -> dict[str, Any]:
        return {"metadata": {"subagent_id": self.subagent_id}}


#: The task runner's refusal rows by rung: (``outcome``, ``error``, ``metadata.reason``).
_TASKRUNNER_REFUSALS: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "spec_hook": ("rejected", "", "spec_hook_deny"),
        "hook_deny": ("denied", "hook_deny", ""),
        "context_overflow": ("rejected", "", "context_overflow"),
        "responder": ("rejected", "", ""),
        "headless": ("rejected", "", "headless_no_authorization"),
    }
)

#: The task runner's approval rows by rung: ``metadata.reason``.
_TASKRUNNER_APPROVALS: Mapping[str, str] = MappingProxyType(
    {
        "hook_auto_approve": "hook_auto_approve",
        "run_auto_approve": "run_auto_approve",
        "responder": "interactive_approved",
    }
)


@dataclass(frozen=True)
class TaskrunnerRows:
    """The task runner's SEL rows (``source="taskrunner"``).

    Every approval is ``approved`` (or the transport floor's rejection) and
    carries the step and the run's launch surface; a refusal carries its error
    and metadata only when it has them. *run* and *task* are read when a row is
    written.
    """

    agent: str
    run: Any
    task: Any
    source: ClassVar[str] = "taskrunner"

    def refusal(self, ask: Ask, refusal: Refusal) -> dict[str, Any]:
        outcome, error, reason = _TASKRUNNER_REFUSALS[refusal.rung]
        row: dict[str, Any] = {
            "session_key": ask.session_key,
            "agent": self.agent,
            "source": self.source,
            "tool_name": ask.event.title,
            "tool_kind": ask.event.tool_kind,
            "outcome": outcome,
            "request_id": ask.request_id,
        }
        if error:
            row["error"] = error
        if reason:
            row["metadata"] = {"reason": reason, **refusal.meta}
        return row

    def approval(self, ask: Ask, settled: Settled) -> dict[str, Any]:
        sent = settled.outcome != "floor_refused"
        return {
            "session_key": ask.session_key,
            "agent": self.agent,
            "source": self.source,
            "tool_name": ask.event.title,
            "tool_kind": ask.event.tool_kind,
            "outcome": "approved" if sent else OUTCOME_REJECTED_TRANSPORT_FLOOR,
            "request_id": ask.request_id,
            "metadata": {
                "task": self.task.index,
                "task_id": self.run.task_id,
                "reason": _TASKRUNNER_APPROVALS[settled.rung],
                # Trust provenance: which launch surface granted this run.
                "source": self.run.source,
            },
        }

    def decline(self, ask: Ask) -> dict[str, Any]:
        return {"agent": self.agent}
