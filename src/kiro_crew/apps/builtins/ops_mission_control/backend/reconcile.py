from __future__ import annotations

from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    LEGAL_TRANSITIONS,
    STATE_OK,
    STATUS_RESOLVED,
    Incident,
    Signal,
)

MAX_RECONCILE_INCIDENTS = 10
EVIDENCE_EXPLICIT_CLEAR = "explicit_clear"
EVIDENCE_COMPLETE_SNAPSHOT_ABSENCE = "complete_snapshot_absence"


def resolution_evidence(
    incident: Incident,
    signals: list[Signal],
    errors: dict[str, str],
    health: dict[str, dict[str, Any]],
) -> str:
    if STATUS_RESOLVED not in LEGAL_TRANSITIONS.get(incident.status, frozenset()):
        return ""

    source = incident.signal.source
    source_health = health.get(source)
    if source in errors or not source_health or source_health.get("ok") is not True:
        return ""

    matching_signals = [
        signal for signal in signals if signal.source == source and signal.id == incident.signal.id
    ]
    if matching_signals:
        if all(signal.state == STATE_OK for signal in matching_signals):
            return EVIDENCE_EXPLICIT_CLEAR
        return ""

    if source_health.get("snapshot") is True:
        return EVIDENCE_COMPLETE_SNAPSHOT_ABSENCE
    return ""
