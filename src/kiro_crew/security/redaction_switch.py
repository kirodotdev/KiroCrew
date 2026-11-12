"""The owner's switch for credential redaction: ON by default, OFF on request.

Why a switch exists
-------------------
Every output surface -- the chat stream, the file viewer, the outbox download,
Slack and channel egress -- runs :func:`security.redaction.redact_credentials`
over what the agent produced before the owner sees it. The scrubber is
shape-based and deliberately over-inclusive (redacting a lookalike is the safe
direction for a secret), so it also swallows values the owner legitimately
needs to read back: the ``?token=`` value of a one-time approval-workflow link,
a base64 blob the owner generated on purpose, a test fixture that happens to
look like a key. Piping such a value to a file did not help inside the
dashboard, because the Files view runs the same pass (the built-in Terminal
streams live command output unredacted, so ``cat`` there always worked; this
switch is for the rendered Files view, where a written file is read back as a
document). It reaches the CREDENTIAL pass only: a URL the exfiltration-URL pass
classifies as carrying data out is still replaced whole, on this surface as on
every other, unless a loaded companion's own host exemptions
(``CredentialPolicy.exempt_exact_hosts``) admit its host -- so whether a given
approval link becomes readable depends on that classification, not on this
switch.

Where the switch applies: OWNER-VIEW surfaces only
-------------------------------------------------
The switch is honoured ONLY inside an explicit :func:`owner_view` scope, which
the caller enters at a seam whose audience is the owner reading their own
dashboard, and only after that caller has verified the REQUESTER is the owner
(``owner_view_for_request``): the file viewer, fed by two ``handlers/files.py``
handlers that take ONE verdict per request (``_owner_view_bypasses_credential_pass``):
``api_file_read`` (the buffer) and ``api_file_diff`` (the ``original`` the buffer
is compared against -- one side raw and the other masked would render an
unchanged credential line as a hunk). The ``api_file_watch`` stream is NOT a
seam -- it also serves artifact live reload, and no consumer renders its frame.
That is the whole surface, and it is the one the motivating workflow needs: ask the agent to
write the value to a file, open the file. Chat is deliberately NOT a seam:
``chat_runner._flush_segment`` redacts the assistant text BEFORE ``slot.append``,
so the transcript holds the redacted bytes and no display-time scope could
restore them, and the live wire streams fan one chunk to every connected client
besides. Outside that scope ``redact_credentials`` is unconditional, exactly as
before: the chat, Slack, Webex, Discord, Telegram and every other channel egress,
the ACP prompt the model reads, the persisted-history load pass (whose bytes
also feed the model prompt), and every ADMISSION predicate that decides whether
a file may reach a third party (``_gate_upload_file``, a non-owner's outbox
download) keep scanning whatever the owner chose. The owner's own delivery
surfaces (``file_send``, the outbox card, the owner's downloads) do not scan
content at all, so the switch has nothing to govern there. The design is the
opposite of a global bypass on purpose: a scope a caller must open cannot be
inherited by a third-party sink that forgot to opt out, so adding a channel
keeps the backend rule "scan before posting to any external surface" without
that channel knowing the switch exists.

What the switch does NOT do
---------------------------
* It never touches a request-BLOCKING decision. ``exfil.py`` decides whether a
  command or URL is refused through ``_contains_fixed_credential`` and its
  siblings, which read the pattern table directly and never call the redaction
  pass; those gates keep firing with the switch off.
* It never touches exfiltration-URL redaction. ``redact_exfiltration_urls`` is
  the control that stops a prompt-injected agent from carrying secrets out in a
  URL the dashboard would render and the browser would fetch; that pass has its
  own exemption seam (``CredentialPolicy.exempt_exact_hosts``) and stays
  unconditional.
* It never touches the diagnostics bundle, logs, the SEL, or any persisted
  copy: none of those paths opens the scope, so they are scrubbed whatever the
  owner chose for their own screen.

The second switch: the output seams
-----------------------------------
The record carries a second, independent owner switch, ``seams_enabled``, for the
output seams :mod:`kiro_crew.redaction_seams` lists (the conversation replayed into
the agent's prompt, and the shape passes over log lines). It is ON by default and
a record without the field reads ON, so a record written before the field existed
keeps its meaning: an old ``enabled: false`` still covers the Files view and its
diff only. ``seams_enabled: false`` is a separate, explicit owner choice. A seam
opens :func:`kiro_crew.redaction_seams.output_seam`; inside it, with the switch
off, :func:`credential_pass_bypassed` is True. Refusal gates never open a seam, so
they keep scanning, and the exfiltration-URL pass keeps running on every seam.
The governance rule below applies to both switches.

Why the record is a keystone file and not ``config.json``
---------------------------------------------------------
Switching the scrubber off is an authorization, not a preference, and the party
it constrains is the agent. ``config.json`` is writable by any auto-approved
agent shell, so a switch stored there could be flipped by a prompt-injected
agent that then prints the secrets it can read. The record therefore lives at
:func:`config.loader.credential_redaction_path`, on the read+write KEYSTONE
floor (``security._CREW_SECRET_LEAVES``) beside ``aws_service_consent.json``:
``is_sensitive_path`` refuses the tool path and the OS sandbox mounts the leaf
read-only (readable, never writable, from a sandboxed shell), so the only writer
is the owner-gated dashboard handler.

Fail direction
--------------
Every read that cannot positively establish ``enabled: false`` -- a missing
file, an unreadable one, malformed JSON, a non-boolean value -- answers
``True``. A switch whose record cannot be read is a switch that is ON.

The engine selector
-------------------
The same record carries ``engine``: ``structured`` (the default) runs the
structured credential detectors in front of main's regex passes, ``legacy`` runs
main's passes alone (identifiers such as AWS key IDs are removed again). It is
not the switch: the switch decides whether the owner's Files view is redacted at
all, the selector picks which engine redacts on every path.
``redaction_structured.compat.use_rule_table`` reads it once per process
(:func:`read_engine`) and the dashboard handler republishes it on a change. Any
value but the exact word ``legacy`` reads as structured.

On a governed host
------------------
Where a governance policy governs any scope, turning redaction off is the
administrator's call, not the owner's: only a top-tier policy that names
:data:`DISABLE_SCOPE` with ``enabled: true`` allows it (:func:`owner_may_disable`);
a lower tier folded beneath it cannot supply that opt-in.
Without that, the PUT refuses OFF and :func:`read_state` reads a stored OFF as ON.
A host with no governed scope is unchanged.

Read cost
---------
There is no cache. The one seam that consults the switch is a request handler
that already does its file I/O off the event loop, so it reads the keystone
with ``asyncio.to_thread(read_state)`` once per request and hands the verdict to
:func:`owner_view`. A verdict is therefore never older than the request it
serves, and the loop never touches the filesystem for it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from kiro_crew import redaction_seams

logger = logging.getLogger(__name__)

#: Whether the calling context is rendering for the OWNER'S OWN VIEW with the
#: switch OFF -- the only state in which ``redact_credentials`` stands down. A
#: ContextVar, not a global: an owner-view render on one task must not leak the
#: bypass into a Slack post the same gateway is making on another. The verdict
#: is supplied by the seam, read ONCE per request off the event loop, and held
#: for the scope's lifetime, so every redaction call inside one render agrees.
_OWNER_VIEW_BYPASS: ContextVar[bool] = ContextVar(
    "kirocrew_redaction_owner_view_bypass", default=False
)


@contextmanager
def owner_view() -> Iterator[None]:
    """Mark the enclosed redaction calls as the OWNER'S OWN VIEW with the switch OFF.

    Enter this ONLY at a seam whose audience is the dashboard owner reading their
    own disk, after the requester has been verified as the owner AND the switch
    has been read OFF the event loop and found disabled (``handlers/files.py``
    ``_owner_view_bypasses_credential_pass``). Inside it ``redact_credentials``
    returns its input unchanged; outside it the switch is never consulted. There
    is deliberately no ``bypass`` argument: a caller that has not established
    both facts simply does not enter the scope. Re-entrant and task-local.
    """
    token = _OWNER_VIEW_BYPASS.set(True)
    try:
        yield
    finally:
        _OWNER_VIEW_BYPASS.reset(token)


def credential_pass_bypassed() -> bool:
    """Whether ``redact_credentials`` should stand down for THIS call.

    True only inside an :func:`owner_view` scope, or inside a
    ``redaction_seams.output_seam`` scope opened while the seams switch is off.
    Outside both it is always False and reads nothing.
    """
    return _OWNER_VIEW_BYPASS.get() or redaction_seams.in_bypassed_seam()


#: Whether the calling context is redacting CODE rather than prose. A ContextVar for
#: :func:`owner_view`'s reason: a scope the caller must open cannot be inherited by a
#: sink that forgot to opt out.
_CODE_TEXT: ContextVar[bool] = ContextVar("kirocrew_redaction_code_text", default=False)


@contextmanager
def code_text() -> Iterator[None]:
    """Mark the enclosed redaction calls as running over CODE, so pass 3 spares two shapes.

    Pass 3 reads a run of base64-alphabet characters as a bare secret key by its entropy
    and shape, a guess about PROSE. In code it rewrites what the author wrote on purpose
    and the program then runs on the redaction marker with no error. Inside this scope it
    leaves a run alone only when the run is an ordered alphabet (``abc...xyz``) or a payload
    of 64 characters or more behind a ``data:<type>;base64,`` header; any other run is judged
    exactly as outside. Every other pass keys on a fixed format and runs unchanged, as does
    the exfiltration-URL pass. A scope rather than an argument because
    ``platform.context.redact_via_context`` takes none; open it only through
    ``platform.context.redact_code_via_context``. Re-entrant and task-local.
    """
    token = _CODE_TEXT.set(True)
    try:
        yield
    finally:
        _CODE_TEXT.reset(token)


def bare_secret_pass_enabled() -> bool:
    """Whether pass 3 judges every run for THIS call: false only inside :func:`code_text`,
    where it spares an ordered alphabet and a data URI payload."""
    return not _CODE_TEXT.get()


#: Serialises the read-modify-write in :func:`set_enabled`. An in-process lock,
#: because the store has
#: exactly one writer (the owner-gated dashboard handler in the gateway process),
#: and a sibling lock FILE would be an agent-reachable artifact that could block
#: the owner from turning redaction back ON.
#: A re-entrant lock so the handler can hold it around its WHOLE
#: audit-then-write transaction (see ``transaction()``) while ``set_enabled``
#: takes it again for the write itself.
_STORE_LOCK = threading.RLock()


def transaction() -> threading.RLock:
    """The lock a caller holds around an audit-and-write pair.

    Two owner PUTs that interleave -- OFF audited, ON audited, ON written, OFF
    written -- would leave the SEL's latest record contradicting the persisted
    switch. Holding this lock from the first audit to the last write makes each
    pair atomic against the other, so the audit trail and the switch always
    agree on the order of changes.
    """
    return _STORE_LOCK


#: The engine selector's two values (``engine`` in the same record). The selector picks
#: which engine redacts on every path; it is not the switch above, which decides
#: whether the owner's Files view is redacted at all.
ENGINE_STRUCTURED = "structured"
ENGINE_LEGACY = "legacy"
ENGINES = (ENGINE_STRUCTURED, ENGINE_LEGACY)


@dataclass(frozen=True)
class RedactionState:
    """Both recorded switch positions, the engine selector, and when each last changed.

    ``enabled`` is the Files-view switch; ``seams_enabled`` is the output-seams
    switch (:mod:`kiro_crew.redaction_seams`). Each is ON unless the record holds a
    literal ``false`` for it. ``engine`` picks which engine redacts on every path.
    """

    enabled: bool
    changed_at: str
    seams_enabled: bool = True
    seams_changed_at: str = ""
    engine: str = ENGINE_STRUCTURED
    engine_changed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "changed_at": self.changed_at,
            "seams_enabled": self.seams_enabled,
            "seams_changed_at": self.seams_changed_at,
            "engine": self.engine,
            "engine_changed_at": self.engine_changed_at,
        }


def _path():
    # Deferred: this module is imported by ``security.redaction`` at package
    # load, and ``config.loader`` must not be pulled onto that path.
    from kiro_crew.config.loader import credential_redaction_path

    return credential_redaction_path()


def _engine_of(raw: object) -> str:
    """The recorded engine; anything but the exact word ``legacy`` is structured."""
    if isinstance(raw, dict) and raw.get("engine") == ENGINE_LEGACY:
        return ENGINE_LEGACY
    return ENGINE_STRUCTURED


def _parse(raw: object) -> RedactionState:
    """Interpret a decoded store; anything not positively ``false`` is ON.

    A record without ``seams_enabled`` (every record written before that switch
    existed) reads the seams ON, so its ``enabled: false`` keeps covering the
    Files view and its diff only.
    """
    if not isinstance(raw, dict):
        return RedactionState(enabled=True, changed_at="")
    return RedactionState(
        enabled=raw.get("enabled") is not False,
        changed_at=str(raw.get("changed_at", "")),
        seams_enabled=raw.get("seams_enabled") is not False,
        seams_changed_at=str(raw.get("seams_changed_at", "")),
        engine=_engine_of(raw),
        engine_changed_at=str(raw.get("engine_changed_at", "")),
    )


#: The governance capability that lets the owner turn redaction off on a governed host.
DISABLE_SCOPE = "capabilities.credential_redaction_off"


def owner_may_disable() -> bool:
    """Whether this host lets the owner turn credential redaction off.

    True on an ``oss`` host whose policy governs no scope. Otherwise only when the
    top policy tier itself enables :data:`DISABLE_SCOPE`; an LTS build
    always governs, and its baseline keeps the row off. A ceiling that cannot be
    read answers False.

    The tier ladder lets a lower tier add a scope its authority leaves unnamed, so
    in a folded ceiling (``lower_tier_folded``) the opt-in may come from the lower
    tier (a home or env document), not the administrator's. The folded ceiling does
    not say which tier named it, so a folded ceiling answers False. shortcut: this
    also refuses an opt-in the top tier named itself while a lower tier is present;
    read the authority tier's own row if a fleet needs that.
    """
    try:
        # circular import: platform.context loads the security package, this module included.
        from kiro_crew.platform.context import current_context
        from kiro_crew.platform.governance import (
            _SANDBOX_FLAGS_SCOPE,
            CapabilityGate,
            policy_level_control,
        )

        ceiling = current_context().governance
        gate = policy_level_control(ceiling, DISABLE_SCOPE)
    except Exception:
        logger.warning("governance is unreadable; credential redaction stays ON")
        return False
    if gate is None:
        # The reserved sandbox boot flags are inert, not a governed scope.
        return ceiling is None or not set(ceiling.controls) - {_SANDBOX_FLAGS_SCOPE}
    folded = ceiling is not None and ceiling.lower_tier_folded
    return isinstance(gate, CapabilityGate) and gate.enabled and not folded


def _read_stored() -> RedactionState:
    """The record on disk, before the governance rule. Fails soft to ENABLED."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RedactionState(enabled=True, changed_at="")
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError AND UnicodeDecodeError (non-UTF-8 bytes).
        logger.warning("credential-redaction switch is unreadable; redaction stays ON")
        return RedactionState(enabled=True, changed_at="")
    return _parse(raw)


def _in_force(state: RedactionState) -> RedactionState:
    """*state* under the governance rule; every setter returns this, not the stored record."""
    if (not state.enabled or not state.seams_enabled) and not owner_may_disable():
        return dataclasses.replace(state, enabled=True, seams_enabled=True)
    return state


def read_state() -> RedactionState:
    """Both switches in force and the engine selector, read fresh from disk.

    Fails soft to ENABLED. A stored OFF, of either switch, reads as ON when
    :func:`owner_may_disable` says no; the engine selector is not governed.
    """
    return _in_force(_read_stored())


def read_engine() -> str:
    """The engine selector, read fresh from disk; a missing or unreadable record is structured.

    No governance read: either engine removes every secret main removes, so the
    choice needs no administrator's opt-in.
    """
    return _read_stored().engine


def _write(state: RedactionState) -> None:
    """Persist *state*; the caller holds :data:`_STORE_LOCK`.

    Rewritten in place, so a running Linux sandbox's read-only mount of it stays
    sealed; a torn read during the rewrite reads as ON. ``restrict_to_owner=True``
    is the fail-loud lockdown the sibling keystone stores use: the owner-only mode
    lands before any content, and a record it cannot protect is not written.
    """
    from kiro_crew.atomic_write import rewrite_in_place

    rewrite_in_place(
        _path(),
        json.dumps(state.to_dict(), indent=2, sort_keys=True),
        restrict_to_owner=True,
    )


def set_enabled(enabled: bool, *, changed_at: str) -> RedactionState:
    """Persist the owner's Files-view switch, keeping the seams switch and engine as stored."""
    with _STORE_LOCK:
        state = dataclasses.replace(_read_stored(), enabled=bool(enabled), changed_at=changed_at)
        _write(state)
    return _in_force(state)


def set_engine(engine: str, *, changed_at: str) -> RedactionState:
    """Persist the engine selector, keeping both switches as stored."""
    if engine not in ENGINES:
        raise ValueError(f"unknown redaction engine: {engine!r}")
    with _STORE_LOCK:
        state = dataclasses.replace(_read_stored(), engine=engine, engine_changed_at=changed_at)
        _write(state)
    return _in_force(state)


def set_seams_enabled(enabled: bool, *, changed_at: str) -> RedactionState:
    """Persist the owner's output-seams switch, keeping the Files-view switch as stored."""
    with _STORE_LOCK:
        state = dataclasses.replace(
            _read_stored(), seams_enabled=bool(enabled), seams_changed_at=changed_at
        )
        _write(state)
    return _in_force(state)


def publish_seams(state: RedactionState) -> None:
    """Tell this process's output seams the switch *state* holds (the gateway only)."""
    redaction_seams.publish(not state.seams_enabled)
