"""In-memory governance health signal for the dashboard indicator (AVP-23427).

A process-global record of the most recent governance fail-closed / integrity
incident, set on the audit path (``governance_profiles.audit_governance_degraded``
and ``admission._audit_admission_fail_closed``) and read by
``dashboard.state.status_snapshot`` to surface Active / Degraded / Disabled.

Deliberately tiny and dependency-free so both ``platform.admission`` and
``platform.governance_profiles`` can import it without an import cycle.  Purely
in-memory (resets on restart); the DURABLE record is the SEL
``governance_degraded`` audit event written alongside each mark.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

_lock = threading.Lock()
_last_incident: Optional[Dict[str, object]] = None
# Last admission posture recorded by load_admission_policy (no I/O to read):
#   "enforcing"  — a policy with active enforcement (mode/allowlist/signature/ceiling)
#   "permissive" — the seeded open default (governance present, not restricting)
#   "unverified" — absent/unreadable policy (loader returned the fail-closed default)
#   "unknown"    — not loaded yet this process
_admission_posture: str = "unknown"


def set_admission_posture(posture: str) -> None:
    """Record the admission posture from the last ``load_admission_policy`` call.

    Cheap in-memory setter so ``status_snapshot`` can derive the governance
    indicator WITHOUT re-loading the policy (which would do file I/O and, on an
    absent file, re-emit a critical SEL on every dashboard poll).
    """
    global _admission_posture
    with _lock:
        _admission_posture = posture


def governance_status() -> str:
    """Derive the dashboard governance indicator (Active / Degraded / Disabled).

    Pure in-memory (no I/O), safe to call on every status poll:

    * ``degraded`` — an incident was recorded this session (a chokepoint denied
      because governance could not be verified, an integrity mismatch, or a
      fail-open degrade), OR the admission policy is unverified (absent/unreadable
      → loader fell back to the fail-closed default).  This is the "investigate"
      state.
    * ``active`` — admission policy is enforcing and no incident recorded.
    * ``disabled`` — admission is the permissive open default (no fleet policy)
      and no incident: governance layer present but not restricting.
    * ``unknown`` — policy not yet loaded this process.
    """
    with _lock:
        inc = _last_incident
        posture = _admission_posture
    if inc is not None or posture == "unverified":
        return "degraded"
    if posture == "enforcing":
        return "active"
    if posture == "permissive":
        return "disabled"
    return "unknown"


def mark_governance_incident(kind: str, detail: str = "") -> None:
    """Record the most recent governance incident.

    ``kind`` is ``"failed_closed"`` (a chokepoint DENIED because governance could
    not be verified) or ``"degraded"`` (a chokepoint fell open to no-opinion).
    ``detail`` is a short, non-secret ``chokepoint:scope`` tag.
    """
    global _last_incident
    with _lock:
        _last_incident = {"kind": kind, "detail": detail[:200], "ts": time.time()}


def last_incident() -> Optional[Dict[str, object]]:
    """Return a copy of the most recent incident, or None if none recorded."""
    with _lock:
        return dict(_last_incident) if _last_incident is not None else None


def reset() -> None:
    """Test helper — clear the recorded incident and admission posture."""
    global _last_incident, _admission_posture
    with _lock:
        _last_incident = None
        _admission_posture = "unknown"
