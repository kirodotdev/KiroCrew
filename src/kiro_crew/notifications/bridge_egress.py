"""The governed half of a bridged delivery: vet, redact, audit.

Every transport's delivery passes through these before and after its sink runs
(``BridgeDispatcher._deliver_one``), so no sink can skip them:

* :func:`vet_delivery` -- ``capabilities.messaging`` and ``channels/<transport>``
  through ``vet_and_audit``, fail-closed, for the host surface, for every session
  the note names, and for its publishing app (tightest wins: any denial denies).
  Each decision lands in the SEL trail, grant and denial alike.
* :func:`redact_note` -- ``redact_exfiltration_urls`` then ``redact_credentials``
  on the title and body, so nothing a producer wrote leaves the gateway unredacted.
* :func:`audit_delivery` -- one SEL record per delivery outcome, attributed to the
  first session the note names (or the host when it names none).

The sessions reach these as an argument, not a note field: the local sink pops
``publisher_session`` before the bridge sees the note, so ``composite_sink`` reads
it first, and the dispatcher's subject resolver turns it and the note's ``slot``
into the session keys governance classifies (``BridgeDispatcher``).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

#: The tool name the bridge's governance decisions and delivery records carry.
BRIDGE_TOOL_NAME = "notification_bridge"

#: Note fields that reach the transport as text and so are redacted first.
_REDACTED_FIELDS = ("title", "body")


def _host_session_key() -> str:
    # Lazy: settings.py imports the bridge, and governance pulls in the platform.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    return HOST_SESSION_KEY


def caller_identity(publisher: str) -> str:
    """The session a delivery is attributed to: the publisher, else the host."""
    return publisher or _host_session_key()


def _app_of(note: Mapping[str, Any]) -> str:
    """The app slug a note's server-set ``source`` names (``app:<slug>``), or ``""``."""
    source = note.get("source")
    if isinstance(source, str) and source.startswith("app:"):
        return source.removeprefix("app:")
    return ""


def vet_delivery(
    transport: str, note: Mapping[str, Any], subjects: tuple[str, ...] = ()
) -> str | None:
    """Return why governance denies bridging *note* to *transport*, or None.

    Vetted for the host, for each session in *subjects* (the sessions the note
    names, already resolved by the dispatcher's caller), and for the publishing
    app when the note's ``source`` names one. Any denial denies, so a subject can
    only narrow what is sent, never widen it.

    Synchronous: the gate reads the profile directory, so the dispatcher runs it
    on a worker thread. Any error, including an invalid governance ceiling,
    denies: a wrong permit here sends the operator's notes off the host.
    """
    from kiro_crew.platform import governance_profiles

    host = _host_session_key()
    calls = [(host, "")] + [(key, "") for key in subjects if key and key != host]
    app = _app_of(note)
    if app:
        calls.append((host, app))
    tool_name = f"{BRIDGE_TOOL_NAME}:{transport}"
    try:
        for key, app_name in calls:
            for scope, item in (("capabilities.messaging", ""), ("channels", transport)):
                # Looked up on the module, so a test patching the definition site
                # is honoured.
                decision = governance_profiles.vet_and_audit(
                    scope,
                    item,
                    session_key=key,
                    tool_name=tool_name,
                    app=app_name,
                    fail_closed=True,
                    log_warning=False,
                )
                if not getattr(decision, "permitted", False):
                    reason = getattr(decision, "reason", "") or ""
                    return reason or f"{scope} denied by governance policy"
    except Exception:
        return "governance evaluation failed; denying (fail-closed)"
    return None


def redact_note(note: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of *note* whose title and body are redacted for egress."""
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials

    out = copy.deepcopy(dict(note))
    for field in _REDACTED_FIELDS:
        value = out.get(field)
        if not isinstance(value, str) or not value:
            continue
        # URLs first: the credential pass would otherwise rewrite a token inside a
        # URL's query and hide the URL from the exfiltration scan.
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        out[field] = value
    return out


def audit_delivery(
    transport: str,
    note: Mapping[str, Any],
    outcome: str,
    *,
    publisher: str = "",
    error: str = "",
) -> None:
    """Write the SEL record for one delivery outcome. Never raises."""
    channel = note.get("channel")
    source = note.get("source")
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=caller_identity(publisher),
            tool_name=f"{BRIDGE_TOOL_NAME}:{transport}",
            tool_kind=BRIDGE_TOOL_NAME,
            outcome=outcome,
            downstream_service=transport,
            resources=f"channel={channel if isinstance(channel, str) else ''} "
            f"source={source if isinstance(source, str) else ''}",
            error=error,
        )
    except Exception:
        # A best-effort audit must neither block nor fail the delivery.
        pass
