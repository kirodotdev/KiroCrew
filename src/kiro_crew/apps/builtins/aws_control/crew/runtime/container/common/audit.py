"""The container's audit record for a permission decision.

MIRRORS ``kiro_crew.sel`` -- specifically the ``SecurityEvent`` field names, its
``event_type`` vocabulary (``tool_approval`` / ``tool_denial``), and the audit-or-deny
discipline ``SecurityEventLog.log_tool_invocation(critical=True)`` exists for: "the event
is written synchronously and a filesystem failure is re-raised so the caller can deny the
tool rather than run it unaudited".

Mirrored rather than called, and the reason is the SINK, not importability. The wheel IS
installed in this image (the base Dockerfile installs Kiro Crew as a real package because
the backend is RUN as a process), so ``from kiro_crew.sel import sel`` would import. What
does not survive the move is where that log lives:

* ``sel._default_dir()`` resolves under ``config_dir()``, and the HMAC key under
  ``trust/`` beside it. In this image both land on the persistent volume, and the
  supervisor and the model worker run as the SAME user -- so the audited party can delete
  the log and the key and write a self-consistent replacement. A chain its own subject can
  re-forge is not evidence, and there is no trust root here to anchor one.
* Nothing harvests that file. The container's only writer to the owner's bucket is the
  transcript store, so a record written to the volume never reaches whoever would
  investigate.

So the record goes to the process log stream, which leaves the task the moment it is
written and cannot be rewritten from inside it. It carries no ``prev_hash`` or
``entry_hash``: those are SEL's, computed on write into its chain, and claiming them for
an unchained line would misrepresent what the record proves. Durability of the stream is
the task definition's log driver, which belongs to the deploy track rather than to this
image.

The sink is this module's OWN logger and handler, attached by ``configure_sink`` before
the app can serve a request, and ``emit`` refuses when a record would not reach a
handler. Both halves are needed: a log call on an ambient logger is dropped by level
before any handler is consulted, and a dropped record is not an error -- so an audit built
on ambient configuration reports success while writing nothing, and the audit-or-deny path
above it never fires. A guard that is live only where it is not needed cannot be told from
no guard.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone

#: The audit records go to a logger this module OWNS, at a name nothing else writes to.
#: Not the calling module's ``__name__`` logger: that one inherits whatever the process's
#: logging configuration happens to be, and under uvicorn's own configuration the front
#: process's logger resolves to an effective level of WARNING with no reachable handler --
#: so an INFO audit call writes zero bytes and raises nothing. An audit that depends on
#: ambient configuration is an audit that a future logging change can switch off silently.
AUDIT_LOGGER_NAME = "crew_container.audit"

#: SEL's own ``event_type`` values for the two halves of a permission decision
#: (``kiro_crew.sel.SecurityEvent``: "tool_invocation, tool_approval, tool_denial,
#: mcp_call, api_access").
EVENT_GRANTED = "tool_approval"
EVENT_DENIED = "tool_denial"

#: What ``caller_identity`` says when the caller is unidentified. The container's control
#: surface authenticates a SECRET, not a principal, so there is no session key to name --
#: and a blank field would read as a lost value rather than as an absent one.
UNIDENTIFIED = "unidentified:control-caller"

#: Marks the handler this module attached, so ``configure_sink`` is idempotent without
#: counting handlers or comparing streams.
_OWNED = "_crew_container_audit_sink"


class AuditUnavailable(RuntimeError):
    """The decision could not be recorded, so it must not be acted on.

    Raised INSTEAD of returning, which is the whole point: a caller that treats a failed
    emit as a warning produces exactly the unaudited grant the record exists to prevent.
    """


def sink() -> logging.Logger:
    """The audit logger. Configuration is ``configure_sink``'s job, not this one's."""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def configure_sink(*, stream=None) -> logging.Logger:
    """Attach an INFO-capable handler on the process stream, and return the logger.

    Called from ``build_app`` rather than from the process entrypoint, so the sink cannot
    be missing for a request: an app that can serve one has been built, and building it
    ran this. A lazy "configure on first emit" would leave whichever request arrives first
    deciding whether the sink exists.

    Idempotent, because ``build_app`` is called more than once in a test session and a
    handler per call would multiply every record.

    ``propagate`` is left alone. A deployment that also configures an INFO root handler
    then gets the record twice, which is the better failure: a duplicate is visible and a
    drop is not.
    """
    log = logging.getLogger(AUDIT_LOGGER_NAME)
    # Set on the LOGGER, not the root: an effective level inherited from a root left at
    # WARNING is what makes ``log.info`` a no-op before any handler is consulted.
    log.setLevel(logging.INFO)
    for handler in log.handlers:
        if getattr(handler, _OWNED, False):
            return log
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    setattr(handler, _OWNED, True)
    log.addHandler(handler)
    return log


def sink_is_live(log: logging.Logger, *, level: int = logging.INFO) -> bool:
    """Would a record at *level* from *log* actually reach a handler?

    Deliberately not ``log.hasHandlers()``, which answers a different question: it ignores
    the logger's effective level and every handler's own level, so it says yes for a
    logger whose only reachable handler is at WARNING -- exactly the arrangement that drops
    an INFO audit record. Both are checked here, and the propagation walk stops where
    ``propagate`` stops it.

    ``logging.lastResort`` is deliberately not counted. It is the fallback used when no
    handler is found at all, and it sits at WARNING, so it does not carry INFO records --
    counting it would report a live sink for the case this function exists to catch.
    """
    if not log.isEnabledFor(level):
        return False
    current: logging.Logger | None = log
    while current is not None:
        for handler in current.handlers:
            if handler.level <= level:
                return True
        if not current.propagate:
            return False
        current = current.parent
    return False


def control_decision_event(
    *,
    granted: bool,
    method: str,
    path: str,
    header_present: bool,
    crew_name: str,
) -> dict[str, object]:
    """Build the record for one control-authorization decision.

    Separate from the emit so a test can assert the CONTENT without a sink, and so the
    two failure modes stay distinguishable: a record this function cannot build is a bug
    here, while a record the sink cannot accept is the deployment's problem.

    No header value appears in the record -- not the control secret, not any other header.
    An audit log that carries the credential it is auditing turns every reader of the log
    into a holder of the secret, so what is recorded is only WHETHER the header was
    presented.
    """
    return {
        "event_id": uuid.uuid4().hex[:16],
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "event_type": EVENT_GRANTED if granted else EVENT_DENIED,
        "caller_identity": UNIDENTIFIED,
        "agent": crew_name,
        "source": "crew_container_front",
        "operation": "control_request",
        "tool_kind": "http_control",
        "outcome": "granted" if granted else "denied",
        "resources": f"{method} {path}",
        "metadata": {"control_header_present": header_present},
    }


def emit(event: dict[str, object], *, log: logging.Logger | None = None) -> None:
    """Write one record, or raise.

    One line of JSON, because the reader is a log query rather than a person, and a
    multi-line record cannot be selected reliably out of an interleaved stream.

    The sink is CHECKED before the record is handed to it. A dropped log record is not an
    error -- ``logging`` filters it by level and returns normally -- so without this check
    a misconfigured process audits nothing, raises nothing, and the caller's audit-or-deny
    path never fires. That is worse than no audit at all: the guard reports success.

    ``logging`` also swallows exceptions raised by its own handlers
    (``logging.raiseExceptions`` only prints them), so the record is serialised BEFORE the
    log call and the handlers are flushed after it, which is where a write failure
    surfaces rather than at interpreter exit.
    """
    log = log if log is not None else sink()
    if not sink_is_live(log):
        raise AuditUnavailable(
            f"logger {log.name!r} would drop an INFO record: effective level "
            f"{logging.getLevelName(log.getEffectiveLevel())}, no reachable handler at or "
            "below INFO. The audit sink must be configured before serving "
            "(common.audit.configure_sink)."
        )

    try:
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AuditUnavailable(f"the audit record could not be serialised: {exc}") from exc

    try:
        log.info("sel_shaped_audit %s", line)
        for handler in list(log.handlers) or list(logging.getLogger().handlers):
            handler.flush()
    except Exception as exc:  # noqa: BLE001 - any sink failure must deny, not pass
        raise AuditUnavailable(f"the audit record could not be written: {exc}") from exc
