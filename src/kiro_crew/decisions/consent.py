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
    enabled: bool,
    *,
    endpoint: str,
    history_budget_chars: object = 0,
    tool_args: object = False,
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
    """
    if not isinstance(enabled, bool):
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
    target = normalize_endpoint(endpoint)
    if enabled and not target:
        raise ValueError("consent needs the endpoint it is given for")
    with _SAVE_LOCK:
        state: dict[str, Any] = dict(read_state_strict())
        if keep:
            history_budget_chars = consented_history_budget(state)
        if keep_scope:
            tool_args = consented_tool_args(state)
        state[STATE_KEY_ENABLED] = enabled
        state[STATE_KEY_ENDPOINT] = target if enabled else ""
        state[STATE_KEY_HISTORY_BUDGET] = history_budget_chars if enabled else 0
        state[STATE_KEY_TOOL_ARGS] = tool_args is True if enabled else False
        atomic_write(consent_path(), json.dumps(state, indent=2) + "\n", mode=_STATE_FILE_MODE)
    return state
