"""The KEYSTONE consent for the decision seam.

Whether Jev may be asked at all lives in ``<config_dir>/decisions_consent.json``,
**not** in ``config.json``. Enabling the seam sends message text and skill
descriptions to an external, paid provider, and ``config.json`` is writable by an
auto-approved agent shell: a prompt-injected agent could set
``decisions.enabled: true`` and the live config watcher would start the egress
without a restart. The precedent is ``aws_service_consent.json`` (consent to spend
the operator's money) and ``computer_use.json`` (consent to drive the desktop):
an authorization goes where the agent cannot write it.

What makes it un-flippable by the agent:

* the leaf is on ``security._CREW_SECRET_LEAVES``, so ``is_sensitive_path`` blocks
  agent reads AND writes on the file-TOOL path; and it is a ``READONLY`` leaf in
  ``sandbox._CREW_READONLY_LEAVES``, so the OS sandbox denies every WRITE from the
  agent's shell in every mode. A sandboxed shell can still READ it -- that is the
  documented keystone posture (masking a ceiling would remove it, not protect it),
  and the file holds only a flag and an endpoint, nothing secret;
* the only writer is the browser-only dashboard PUT handler, which does not route
  through the agent tool gate and refuses app tokens;
* every read fails soft to ``{}`` -> **NOT CONSENTED**. A missing, unreadable,
  truncated or hand-mangled file must never mean "send".

Four scopes sit on the keystone beside the switch, and each is a SEPARATE yes:
``tool_args`` (the arguments of the one call about to run), ``compaction`` (a whole
slot transcript, conversation text and every tool input in it), ``memory_text``
(the text of recalled memories) and ``nudge_evidence`` (a watched session's
transcript tail, and a watched pull request's typed facts plus a fixed-width
fingerprint of its comment bodies, never their text). A ceiling sits beside them --
``history_budget_chars``, on prior turns. Absent reads as not consented in every
case, so a record written before a scope existed authorizes exactly the text its
owner reviewed and never a category added later.

The ``decisions`` section of ``config.json`` keeps the knobs that grant nothing on
their own -- the sampling share and the provider -- so there is exactly one place
the seam can be switched on.

Consent is bound to a DESTINATION. ``provider.endpoint`` lives in ``config.json``
too, so a switch that only said "yes" would let the same prompt-injected shell
redirect consented messages to an endpoint it controls. The keystone therefore
records the endpoint the owner consented to, and the gate sends only while the
configured endpoint still equals it; a changed endpoint is a refusal until the
owner consents again through the dashboard.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import loader as config_loader

logger = logging.getLogger(__name__)

STATE_KEY_ENABLED = "enabled"
STATE_KEY_ENDPOINT = "endpoint"
#: The prior-conversation budget the owner reviewed, in characters. A CEILING, not
#: the value in force: ``config.json`` still names what to use, and the gate takes
#: the smaller of the two. It lives here because ``config.json`` is agent-writable,
#: so a budget recorded only there could be raised by the very agent whose
#: conversation would be sent -- the same argument that put ``endpoint`` here.
STATE_KEY_HISTORY_BUDGET = "history_budget_chars"

#: Whether the owner consented to sending TOOL-CALL ARGUMENTS, the category
#: ``tool.risk`` needs and ``skills.select`` does not. A separate leaf rather than a
#: wider reading of ``enabled``, for the reason the history ceiling is one: consent
#: is recorded against the text the owner reviewed -- a message excerpt and skill
#: descriptions -- so a record written before this key existed authorizes exactly
#: that and nothing more. Absent reads as NOT consented, which is what keeps every
#: such record meaning what its owner agreed to.
STATE_KEY_TOOL_ARGS = "tool_args"

#: Whether the owner consented to sending a WHOLE SLOT TRANSCRIPT -- the
#: conversation text and every tool-call input in it -- which is what
#: ``compaction.keep`` scores and neither of the scopes above covers. A third leaf
#: for the reason there is a second: consent is recorded against the text the owner
#: reviewed, and ``tool_args`` was reviewed as "the arguments of the one call about
#: to run", not as "everything this session has ever run". Absent reads as NOT
#: consented, so an install that granted either of the others is inert here.
STATE_KEY_COMPACTION = "compaction"

#: Whether the owner consented to sending the TEXT OF RECALLED MEMORIES, the
#: category ``memory.recall`` needs and no other point does. A separate leaf for
#: exactly the reason ``tool_args`` is one, and the category is genuinely new: a
#: message excerpt is text the owner just typed and a skill description is text this
#: build shipped, while a recalled memory is text the AGENT wrote down turns or days
#: ago about whatever it was working on then. Consent recorded against the first two
#: cannot stand for the third, so absent reads as NOT consented.
STATE_KEY_MEMORY_TEXT = "memory_text"

#: Whether the owner consented to sending EVIDENCE GATHERED FROM OTHER SESSIONS
#: AND THIRD PARTIES -- a watched worker's transcript tail, a watched pull
#: request's typed facts plus a fixed-width FINGERPRINT of its comment bodies and
#: never their text, a work-ledger event -- the category ``nudge.wake`` needs and no other
#: point does. A fourth leaf for exactly the reason there is a third, and the
#: category is again genuinely new: ``compaction`` was reviewed as the OWNING
#: session's own transcript, text the owner was present for, while this is text
#: from conversations the owner was not in and from a forge they do not control.
#: Absent reads as NOT consented, so an install that granted any of the other
#: three is inert here.
STATE_KEY_NUDGE_EVIDENCE = "nudge_evidence"

#: "Keep whatever ceiling is recorded" for :func:`save_enabled`. A distinct object,
#: because ``0`` is a ceiling an owner may choose and no number can mean "not asked".
#: Resolved inside the read-modify-write, so the value written comes from the same
#: read the write is based on: a caller that resolved it first would hold a ceiling
#: read before another writer lowered it, and hand that stale number back.
KEEP_HISTORY_BUDGET: object = object()

#: "Keep whatever tool-argument scope is recorded", on the same terms. A distinct
#: object for the same reason: ``False`` is a scope an owner may choose, so no
#: boolean can also mean "not asked", and it is resolved inside the lock so an
#: enabling PUT cannot restore a scope a concurrent revoking PUT just cleared.
KEEP_TOOL_ARGS: object = object()

#: "Keep whatever compaction scope is recorded", on the same terms as the two
#: sentinels above and resolved inside the same lock, so an enabling PUT cannot
#: restore a scope a concurrent revoking PUT just cleared.
KEEP_COMPACTION: object = object()

#: "Keep whatever recalled-memory scope is recorded", on the same terms and for the
#: same reason as :data:`KEEP_TOOL_ARGS`: ``False`` is a scope an owner may choose,
#: so no boolean can also mean "not asked", and it is resolved inside the lock so an
#: enabling PUT cannot restore a scope a concurrent revoking PUT just cleared.
KEEP_MEMORY_TEXT: object = object()

#: "Keep whatever wake-evidence scope is recorded", on the same terms and for the
#: same reason as :data:`KEEP_MEMORY_TEXT`: ``False`` is a scope an owner may
#: choose, so no boolean can also mean "not asked", and it is resolved inside the
#: lock so an enabling PUT cannot restore a scope a concurrent revoking PUT just
#: cleared.
KEEP_NUDGE_EVIDENCE: object = object()

#: "Keep whatever the keystone records" -- the switch AND the endpoint it is bound to.
#: A distinct object for the reason the four above are: ``False`` is a state an owner
#: may choose, so no boolean can also mean "not asked".
#:
#: It exists so a write that only moves a SCOPE never carries the switch. A caller that
#: had to pass ``enabled`` could only pass what it last read, and a view read before a
#: revoke would hand ``True`` back and re-grant egress the owner just withdrew. With this,
#: the switch is resolved from the keystone inside :data:`_SAVE_LOCK`, so a scope write
#: cannot move it in either direction whatever the caller believes.
KEEP_ENABLED: object = object()

# Owner-only: the file records a security decision.
_STATE_FILE_MODE = 0o600

#: Serializes :func:`save_enabled`'s read-modify-write. The handler runs it on a
#: thread, so two owner PUTs genuinely interleave, and resolving
#: :data:`KEEP_HISTORY_BUDGET` from this function's own read only narrows that --
#: it cannot order two reads against one write. Without the lock the enable PUT
#: writes back a ceiling the lowering PUT had already reduced, which raises an
#: egress limit by losing a race. One writer exists in-process, so a thread lock
#: is the whole boundary.
_SAVE_LOCK = threading.Lock()


class ConsentEndpointMovedError(RuntimeError):
    """A scope-only write met a keystone bound to a different endpoint.

    Raised by :func:`save_enabled` under :data:`KEEP_ENABLED` when the address the
    caller established consent for differs from the one the keystone records at
    write time. Carries the RECORDED address, which is the one a caller has to show
    before asking again.
    Nothing is written.
    """

    def __init__(self, recorded: str) -> None:
        super().__init__("the recorded consent endpoint moved since it was checked")
        self.recorded = recorded


class ConsentCorruptError(RuntimeError):
    """The keystone exists but cannot be parsed; a writer must not clobber it."""


def consent_path() -> Path:
    """Path to the keystone, resolved through the loader so tests can redirect it."""
    return config_loader.decisions_consent_path()


def load_state() -> dict:
    """Read the keystone (fail-soft to ``{}``, which :func:`is_enabled` reads as off)."""
    try:
        raw = json.loads(consent_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("decisions_consent.json load failed; treating as not consented", exc_info=True)
        return {}


def is_enabled(state: "dict | None" = None) -> bool:
    """True only when the keystone explicitly says ``enabled: true``.

    Strict identity against ``True``: a hand-edited ``"enabled": "false"`` or
    ``"enabled": 1`` is not consent. The only spelling that enables the seam is a
    real JSON ``true``, which is what the dashboard writes.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_ENABLED) is True


def normalize_endpoint(value: object) -> str:
    """The comparable spelling of an endpoint: a stripped string, ``""`` otherwise."""
    return value.strip() if isinstance(value, str) else ""


def consented_endpoint(state: "dict | None" = None) -> str:
    """The endpoint the owner consented to, or ``""`` when none was recorded."""
    data = load_state() if state is None else state
    return normalize_endpoint(data.get(STATE_KEY_ENDPOINT))


def consented_history_budget(state: "dict | None" = None) -> int:
    """Characters of PRIOR conversation the owner consented to, or 0.

    0 for absent, for a non-integer, for a bool (``True`` is not a budget) and for
    a negative number: every reading that is not an explicit non-negative whole
    number means the owner reviewed no prior-turn egress, which is what every
    consent recorded before this ceiling existed did.

    A ceiling only. The gate takes ``min`` of this and the configured value, so
    lowering the budget stays an ordinary config edit while RAISING it past what
    was reviewed takes a new consent.
    """
    data = load_state() if state is None else state
    raw = data.get(STATE_KEY_HISTORY_BUDGET)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, raw)


def consented_tool_args(state: "dict | None" = None) -> bool:
    """Whether the owner consented to sending tool-call arguments. Absent reads False.

    Only a literal ``True`` consents. Absent, a string, ``1``, ``"true"`` and every
    other truthy stand-in read as NOT consented -- the same exactness
    :func:`is_enabled` applies to the switch itself, and for the same reason: this
    value decides whether a new category of conversation content leaves the
    machine, so a value nobody can read back as a deliberate yes is a no.

    That default is the whole point of the key. Every consent recorded before it
    existed was given against a request carrying the message excerpt and the
    candidate descriptions; reading those records as permission to send tool
    arguments would widen egress with no new choice, which is exactly what the
    history ceiling prevents one field over.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_TOOL_ARGS) is True


def consented_compaction(state: "dict | None" = None) -> bool:
    """Whether the owner consented to sending a whole transcript. Absent reads False.

    Only a literal ``True``, the exactness :func:`consented_tool_args` applies and
    for the same reason: this value decides whether a new -- and by far the
    largest -- category of conversation content leaves the machine, so a value
    nobody can read back as a deliberate yes is a no.

    NOT implied by :func:`consented_tool_args`. That scope was reviewed as the
    arguments of the one call that is about to run; this one sends the conversation
    and every tool input the session has accumulated, in a request one to two
    orders of magnitude larger. Reading the narrower record as permission for the
    wider egress is exactly what a separate leaf exists to prevent.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_COMPACTION) is True


def consented_memory_text(state: "dict | None" = None) -> bool:
    """Whether the owner consented to sending recalled-memory text. Absent reads False.

    Only a literal ``True`` consents, on the same terms as
    :func:`consented_tool_args` and for the same reason: this value decides whether a
    new category of content leaves the machine, so a value nobody can read back as a
    deliberate yes is a no.

    The default is the whole point of the key. Every consent recorded before it
    existed was given against a request carrying the message excerpt and the
    candidate skill descriptions. A recalled memory is neither: it is text the agent
    wrote down in an earlier conversation, about work the owner was not reviewing
    when they flipped the switch. Reading those records as permission to send it
    would widen egress with no new choice.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_MEMORY_TEXT) is True


def consented_nudge_evidence(state: "dict | None" = None) -> bool:
    """Whether the owner consented to sending other sessions' evidence. Absent reads False.

    Only a literal ``True`` consents, on the same terms as
    :func:`consented_tool_args` and for the same reason: this value decides whether
    a new category of content leaves the machine, so a value nobody can read back as
    a deliberate yes is a no.

    NOT implied by :func:`consented_compaction`, which is the nearest neighbour and
    still a different decision. That scope was reviewed as a whole transcript of the
    session the owner is looking at -- text they were present for. This one carries
    the tail of a DIFFERENT session and the body of a comment written by a bot or a
    reviewer on a forge, so consent to the first cannot stand for the second.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_NUDGE_EVIDENCE) is True


def permits(endpoint: object, state: "dict | None" = None) -> bool:
    """Whether the keystone consents to sending to *endpoint*, exactly.

    Both halves must hold: ``enabled`` is a literal ``true`` AND the recorded
    endpoint equals the one asked about. An empty recorded endpoint permits
    nothing -- a keystone with the flag but no destination never came from the
    dashboard writer.
    """
    data = load_state() if state is None else state
    if not is_enabled(data):
        return False
    wanted = normalize_endpoint(endpoint)
    recorded = consented_endpoint(data)
    return bool(recorded) and recorded == wanted


def read_state_strict() -> dict:
    """Read the keystone for a MUTATION: raise on corrupt, ``{}`` when absent.

    A populated-but-unparseable ceiling must be reported, not overwritten: resetting
    it to defaults would be a silent change to a security decision.
    """
    path = consent_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConsentCorruptError(str(exc)) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConsentCorruptError(f"decisions_consent.json is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConsentCorruptError("decisions_consent.json top level is not a JSON object")
    return loaded


def save_enabled(
    enabled: object,
    *,
    endpoint: str,
    history_budget_chars: object = 0,
    tool_args: object = False,
    compaction: object = False,
    memory_text: object = False,
    nudge_evidence: object = False,
) -> dict:
    """Record *enabled* for *endpoint* atomically, owner-only; return the state written.

    Enabling records the endpoint the owner is consenting to -- the caller passes
    the one currently configured, so the keystone says where the messages will go
    at the moment consent is given. Disabling clears it, so a later re-enable
    cannot inherit a stale destination. Read-modify-write so an unknown key an
    operator added by hand survives. Raises :class:`ConsentCorruptError` rather
    than clobbering a corrupt file, and ``OSError`` on a write failure, so the
    HTTP handler can report a real error.

    *history_budget_chars* is the prior-conversation ceiling the owner reviewed, and
    it is recorded on the same terms as the endpoint: written on enable, cleared to
    0 on disable so a later re-enable cannot inherit a budget nobody re-reviewed.
    Its default is 0, so a caller that does not mention prior turns consents to none.

    Pass :data:`KEEP_HISTORY_BUDGET` to leave a recorded ceiling as it is. It is
    resolved from THIS function's own read, not the caller's, so the number written
    comes from the same state the write is based on. A caller that read the ceiling
    first and passed the number would hold a value read before a concurrent writer
    lowered it, and handing that back would restore a ceiling somebody just reduced --
    raising an egress limit by losing a race.

    Resolving it here is necessary and not sufficient: the handler runs this on a
    thread, so two owner PUTs interleave and one read still lands before the other's
    write. :data:`_SAVE_LOCK` is held across the read AND the write, which is what
    makes the paragraph above true rather than merely narrow -- the lowering PUT's
    ceiling survives the enable PUT that ran beside it, whichever order they took.

    *tool_args* is the TOOL-ARGUMENT egress scope, recorded on exactly those terms:
    written on enable, cleared on disable so a re-enable cannot inherit a scope
    nobody re-reviewed, and defaulting to ``False`` so a caller that does not
    mention tool arguments consents to none. :data:`KEEP_TOOL_ARGS` leaves a
    recorded scope alone and is resolved inside the same lock, so an enabling PUT
    cannot hand back a scope a revoking PUT had already cleared.

    *compaction* is the WHOLE-TRANSCRIPT egress scope ``compaction.keep`` needs, on
    exactly the terms *tool_args* is recorded on: written on enable, cleared on
    disable, defaulting to ``False`` so a caller that does not mention it consents
    to none, and :data:`KEEP_COMPACTION` leaves a recorded scope alone from inside
    this same lock.

    *memory_text* is the RECALLED-MEMORY egress scope and behaves identically, down
    to :data:`KEEP_MEMORY_TEXT` and the lock. The two scopes are independent fields
    because they are independent decisions: an owner may want risky tool calls
    flagged without the contents of their memory store leaving the machine, and
    either order of those two answers has to be recordable.

    Pass :data:`KEEP_ENABLED` for a write that moves only a scope. The switch and the
    endpoint are then taken from the keystone inside this lock and written back
    unchanged, and the scopes are applied under the RECORDED switch -- so a scope write
    against a revoked keystone leaves it revoked and stores no scope, because a scope is
    only meaningful while the seam is on. *endpoint* is ignored in that case rather than
    trusted: the recorded binding is the one the owner reviewed.

    *nudge_evidence* is the OTHER-SESSION EVIDENCE scope ``nudge.wake`` needs, on the
    same terms again, down to :data:`KEEP_NUDGE_EVIDENCE` and the lock. It is a fourth
    independent field because it is a fourth independent decision: an owner may want
    their own transcript scored for compaction without the tail of every session they
    are watching leaving the machine.

    This is what makes the route safe rather than careful. A caller that must supply
    ``enabled`` can only supply what it last read, so a view read before a revoke
    re-grants egress; with the switch resolved here, no scope write can move it.

    A scope-only write also has to name the endpoint it believes is in force,
    and this function re-checks the RECORDED address against it under the lock:
    the caller establishes that consent stands for the configured endpoint
    before calling, and that check runs outside this lock, so a re-bind landing
    in the window would otherwise leave the scope written onto whatever the
    keystone records by then. A difference raises
    :class:`ConsentEndpointMovedError` and writes nothing.
    """
    keep_enabled = enabled is KEEP_ENABLED
    if not keep_enabled and not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    keep = history_budget_chars is KEEP_HISTORY_BUDGET
    if not keep:
        if isinstance(history_budget_chars, bool) or not isinstance(history_budget_chars, int):
            raise ValueError("history_budget_chars must be a whole number")
        if history_budget_chars < 0:
            raise ValueError("history_budget_chars cannot be negative")
    keep_scope = tool_args is KEEP_TOOL_ARGS
    if not keep_scope and not isinstance(tool_args, bool):
        raise ValueError("tool_args must be a bool")
    keep_compaction = compaction is KEEP_COMPACTION
    if not keep_compaction and not isinstance(compaction, bool):
        raise ValueError("compaction must be a bool")
    keep_memory = memory_text is KEEP_MEMORY_TEXT
    if not keep_memory and not isinstance(memory_text, bool):
        raise ValueError("memory_text must be a bool")
    keep_nudge = nudge_evidence is KEEP_NUDGE_EVIDENCE
    if not keep_nudge and not isinstance(nudge_evidence, bool):
        raise ValueError("nudge_evidence must be a bool")
    target = normalize_endpoint(endpoint)
    if enabled is True and not target:
        raise ValueError("consent needs the endpoint it is given for")
    with _SAVE_LOCK:
        state: dict[str, Any] = dict(read_state_strict())
        if keep_enabled:
            # Both, together: the switch and the address it is bound to are one fact,
            # and writing the switch from the record while re-deriving the address from
            # a caller's argument would rebind a consent nobody re-reviewed.
            enabled = is_enabled(state)
            recorded = consented_endpoint(state)
            # And the caller's own endpoint-in-force has to still BE that address. The
            # caller establishes consent stands for the configured endpoint before
            # calling; that check ran outside this lock, so a re-bind landing in between
            # would leave the scope written onto whatever the keystone now records --
            # authorizing a wider egress category for an address the owner never
            # reviewed. Comparing the two here closes that window: the write is refused
            # and the caller re-reads, which is the same answer it would have got had
            # the re-bind landed one moment earlier. Only meaningful while the switch is
            # on; a recorded-off keystone has no address, and the clearing branch below
            # writes the fail-closed values.
            if enabled and recorded != target:
                raise ConsentEndpointMovedError(recorded)
            target = recorded
        if keep:
            history_budget_chars = consented_history_budget(state)
        if keep_scope:
            tool_args = consented_tool_args(state)
        if keep_compaction:
            compaction = consented_compaction(state)
        if keep_memory:
            memory_text = consented_memory_text(state)
        if keep_nudge:
            nudge_evidence = consented_nudge_evidence(state)
        state[STATE_KEY_ENABLED] = enabled
        state[STATE_KEY_ENDPOINT] = target if enabled else ""
        state[STATE_KEY_HISTORY_BUDGET] = history_budget_chars if enabled else 0
        state[STATE_KEY_TOOL_ARGS] = tool_args is True if enabled else False
        state[STATE_KEY_COMPACTION] = compaction is True if enabled else False
        state[STATE_KEY_MEMORY_TEXT] = memory_text is True if enabled else False
        state[STATE_KEY_NUDGE_EVIDENCE] = nudge_evidence is True if enabled else False
        atomic_write(consent_path(), json.dumps(state, indent=2) + "\n", mode=_STATE_FILE_MODE)
    return state
