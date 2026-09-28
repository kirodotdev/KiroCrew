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
a file may leave the machine (``file_send``, ``_gate_upload_file``, the outbox
flagged-file check) keep scanning whatever the owner chose. The design is the
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

Why the record is a keystone file and not ``config.json``
---------------------------------------------------------
Switching the scrubber off is an authorization, not a preference, and the party
it constrains is the agent. ``config.json`` is writable by any auto-approved
agent shell, so a switch stored there could be flipped by a prompt-injected
agent that then prints the secrets it can read. The record therefore lives at
:func:`config.loader.credential_redaction_path`, on the read+write KEYSTONE
floor (``security._CREW_SECRET_LEAVES``) beside ``file_delivery_consent.json``:
``is_sensitive_path`` refuses the tool path and the OS sandbox mounts the leaf
read-only (readable, never writable, from a sandboxed shell), so the only writer
is the owner-gated dashboard handler.

Fail direction
--------------
Every read that cannot positively establish ``enabled: false`` -- a missing
file, an unreadable one, malformed JSON, a non-boolean value -- answers
``True``. A switch whose record cannot be read is a switch that is ON.

Read cost
---------
There is no cache. The one seam that consults the switch is a request handler
that already does its file I/O off the event loop, so it reads the keystone
with ``asyncio.to_thread(read_state)`` once per request and hands the verdict to
:func:`owner_view`. A verdict is therefore never older than the request it
serves, and the loop never touches the filesystem for it.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

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

    True only inside an :func:`owner_view` scope. Constant for the whole scope by
    construction; outside any scope it is always False and reads nothing.
    """
    return _OWNER_VIEW_BYPASS.get()


#: Serialises the read-modify-write in :func:`set_enabled`. An in-process lock,
#: for the same reason ``file_delivery_consent._STORE_LOCK`` gives: the store has
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


@dataclass(frozen=True)
class RedactionState:
    """The recorded switch position and when it was last changed."""

    enabled: bool
    changed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "changed_at": self.changed_at}


def _path():
    # Deferred: this module is imported by ``security.redaction`` at package
    # load, and ``config.loader`` must not be pulled onto that path.
    from kiro_crew.config.loader import credential_redaction_path

    return credential_redaction_path()


def _parse(raw: object) -> RedactionState:
    """Interpret a decoded store; anything not positively ``false`` is ON."""
    if not isinstance(raw, dict):
        return RedactionState(enabled=True, changed_at="")
    enabled = raw.get("enabled")
    if enabled is not False:
        return RedactionState(enabled=True, changed_at=str(raw.get("changed_at", "")))
    return RedactionState(enabled=False, changed_at=str(raw.get("changed_at", "")))


def read_state() -> RedactionState:
    """The recorded switch, read fresh from disk. Fails soft to ENABLED."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RedactionState(enabled=True, changed_at="")
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError AND UnicodeDecodeError (non-UTF-8 bytes).
        logger.warning("credential-redaction switch is unreadable; redaction stays ON")
        return RedactionState(enabled=True, changed_at="")
    return _parse(raw)


def set_enabled(enabled: bool, *, changed_at: str) -> RedactionState:
    """Persist the owner's switch position.

    Fail-loud lockdown BEFORE any content lands, as the sibling keystone stores
    do: ``restrict_to_owner=True`` applies the owner-only mode to the temp file
    before the payload reaches it, and the default ``restrict_on_error="raise"``
    refuses to write a record it cannot protect.
    """
    from kiro_crew.atomic_write import atomic_write

    state = RedactionState(enabled=bool(enabled), changed_at=changed_at)
    with _STORE_LOCK:
        atomic_write(
            _path(),
            json.dumps(state.to_dict(), indent=2, sort_keys=True),
            restrict_to_owner=True,
        )
    return state
