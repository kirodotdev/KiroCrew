"""Durable adoption state: the config marker (with the trust grant) and the retry ledger.

**Marker** (``agent.apps_builtin_adoption`` in ``config.json``, which the sandbox
seals read-only). Written in the SAME trust-writer transaction as the execution
grant, so a crash can never leave a grant without the record of what it added.
``adopted`` is the uninstall tombstone and is never removed.

**Ledger** (``<home>/app-adoption/<name>.json``). Operational retry state only:
tampering with it can change retry timing or the displayed state, never what code
runs (that is anchored by the wheel pin and re-verified at APPLY).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtin_adoption_markers import (
    AdoptionMarker,
    coerce_adoption_markers,
)
from kiro_crew.apps.builtin_adoption_pins import AdoptionPin

logger = logging.getLogger(__name__)

MARKER_KEY = "apps_builtin_adoption"

LEDGER_STATES = ("pending", "staged", "blocked", "adopted")


class TrustConflict(Exception):
    """The operator's grant names a different repository; it is never overwritten."""


# --------------------------------------------------------------------------- #
# Marker
# --------------------------------------------------------------------------- #


def read_adoption_markers() -> dict[str, AdoptionMarker]:
    """Markers from the base ``config.json`` UNION the effective config.

    The base file wins for a name in both: it is where the gateway writes them, so
    an overlay can add a name but never hide a tombstone. Raises when the base file
    exists and cannot be read: "unknown" must never read as "no marker".
    """
    from kiro_crew.config.loader import KiroCrewConfig

    out: dict[str, AdoptionMarker] = dict(
        coerce_adoption_markers(getattr(KiroCrewConfig.load().agent, MARKER_KEY, {}))
    )
    out.update(read_base_adoption_markers())
    return out


def read_base_adoption_markers() -> dict[str, AdoptionMarker]:
    """The markers the base ``config.json`` holds: a plain read, no config load.

    For display (``/api/apps`` rows), where a whole config load would be work, and
    side effects, the listing never had. Raises when the file cannot be read.
    """
    from kiro_crew.config.loader import config_path, read_config_text

    path = config_path()
    if not path.is_file():
        return {}
    raw = json.loads(read_config_text(path))
    agent_raw = raw.get("agent") if isinstance(raw, dict) else None
    if not isinstance(agent_raw, dict):
        return {}
    return coerce_adoption_markers(agent_raw.get(MARKER_KEY))


def _markers_raw(agent_raw: dict) -> dict[str, Any]:
    value = agent_raw.get(MARKER_KEY)
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class GrantPlan:
    """What APPLY's trust step will do. ``write`` False reuses a same-repository grant."""

    write: bool


def _marker(pin: AdoptionPin, state: str, flags: dict[str, bool]) -> dict[str, Any]:
    return {
        "state": state,
        "repository": pin.repository,
        "commit": pin.commit,
        "addedName": flags.get("addedName", False),
        "setRepository": flags.get("setRepository", False),
        "removedLocal": flags.get("removedLocal", False),
    }


async def write_grant_and_marker(name: str, pin: AdoptionPin, *, plan: GrantPlan) -> AdoptionMarker:
    """Record the grant (when *plan* writes one) and a ``granting`` marker, in ONE write.

    The grant is the three-key mutation ``default_installs._grant`` applies:
    ``apps_trusted += name``, ``apps_trusted_repositories[name] = repository``, and
    the name removed from ``apps_trusted_local``. The marker's flags are computed
    INSIDE the transaction from what the file held, so the rollback undoes exactly
    what this write changed. Raises ``TrustSettingOverlayOwned``,
    ``ConfigCorruptError`` or :class:`TrustConflict`.
    """
    from kiro_crew.apps.agent_config_writer import mutate_agent_marker, mutate_trust_settings

    written: dict[str, Any] = {}
    if not plan.write:

        def _marker_only(agent_raw: dict) -> None:
            markers = _markers_raw(agent_raw)
            markers[name] = _marker(pin, "granting", {})
            agent_raw[MARKER_KEY] = markers
            written.update(markers[name])

        await mutate_agent_marker(_marker_only)
        return coerce_adoption_markers({name: written})[name]

    from kiro_crew.apps.registry_pipeline.git_targets import _same_git_target
    from kiro_crew.dashboard.handlers.security_trusted_apps import (
        _trusted_list_raw,
        _trusted_local_list_raw,
        _trusted_repositories_raw,
    )

    def _grant(agent_raw: dict) -> None:
        trusted = _trusted_list_raw(agent_raw)
        local = _trusted_local_list_raw(agent_raw)
        repositories = _trusted_repositories_raw(agent_raw)
        current = repositories.get(name)
        if isinstance(current, str) and current and not _same_git_target(current, pin.repository):
            raise TrustConflict(name)
        flags = {
            "addedName": name not in trusted,
            "setRepository": current != pin.repository,
            "removedLocal": name in local,
        }
        if flags["addedName"]:
            trusted.append(name)
        agent_raw["apps_trusted"] = trusted
        repositories[name] = pin.repository
        agent_raw["apps_trusted_repositories"] = repositories
        agent_raw["apps_trusted_local"] = [a for a in local if a != name]
        markers = _markers_raw(agent_raw)
        markers[name] = _marker(pin, "granting", flags)
        agent_raw[MARKER_KEY] = markers
        written.update(markers[name])

    await mutate_trust_settings(_grant)
    return coerce_adoption_markers({name: written})[name]


async def finalize_marker(name: str, pin: AdoptionPin) -> None:
    """Set *name*'s marker to ``adopted``, keeping its flags for audit. Never removed after."""
    from kiro_crew.apps.agent_config_writer import mutate_agent_marker

    def _finalize(agent_raw: dict) -> None:
        markers = _markers_raw(agent_raw)
        current = markers.get(name)
        flags = current if isinstance(current, dict) else {}
        markers[name] = _marker(pin, "adopted", {k: flags.get(k) is True for k in _FLAGS})
        agent_raw[MARKER_KEY] = markers

    await mutate_agent_marker(_finalize)


_FLAGS = ("addedName", "setRepository", "removedLocal")


async def rollback_grant_and_marker(name: str, marker: AdoptionMarker) -> None:
    """Undo only what *marker*'s flags say the grant changed, and drop the marker.

    The ``_revoke`` shape of default installs. An ``adopted`` marker is never
    touched: it is the tombstone.
    """
    from kiro_crew.apps.agent_config_writer import mutate_agent_marker, mutate_trust_settings
    from kiro_crew.dashboard.handlers.security_trusted_apps import (
        _trusted_list_raw,
        _trusted_local_list_raw,
        _trusted_repositories_raw,
    )

    def _drop_marker(agent_raw: dict) -> None:
        markers = _markers_raw(agent_raw)
        current = markers.get(name)
        if isinstance(current, dict) and current.get("state") == "granting":
            markers.pop(name, None)
            agent_raw[MARKER_KEY] = markers

    if not (marker["addedName"] or marker["setRepository"] or marker["removedLocal"]):
        await mutate_agent_marker(_drop_marker)
        return

    def _undo(agent_raw: dict) -> None:
        if marker["addedName"]:
            agent_raw["apps_trusted"] = [a for a in _trusted_list_raw(agent_raw) if a != name]
        if marker["setRepository"]:
            repositories = _trusted_repositories_raw(agent_raw)
            repositories.pop(name, None)
            agent_raw["apps_trusted_repositories"] = repositories
        if marker["removedLocal"]:
            local = _trusted_local_list_raw(agent_raw)
            if name not in local:
                local.append(name)
            agent_raw["apps_trusted_local"] = local
        _drop_marker(agent_raw)

    await mutate_trust_settings(_undo)


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


@dataclass
class AdoptionLedger:
    """One pin's operational state. ``next_attempt_at`` is ISO-8601 UTC or empty."""

    name: str
    commit: str
    state: str = "pending"
    reason: str = ""
    attempts: int = 0
    next_attempt_at: str = ""
    staged: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "commit": self.commit,
            "state": self.state,
            "reason": self.reason,
            "attempts": self.attempts,
            "nextAttemptAt": self.next_attempt_at,
            "staged": dict(self.staged),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> AdoptionLedger | None:
        if not isinstance(raw, dict):
            return None
        name, commit, state = raw.get("name"), raw.get("commit"), raw.get("state")
        attempts = raw.get("attempts", 0)
        staged = raw.get("staged", {})
        if not (isinstance(name, str) and isinstance(commit, str) and state in LEDGER_STATES):
            return None
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            return None
        if not isinstance(staged, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in staged.items()
        ):
            return None
        reason = raw.get("reason", "")
        next_at = raw.get("nextAttemptAt", "")
        return cls(
            name=name,
            commit=commit,
            state=state,
            reason=reason if isinstance(reason, str) else "",
            attempts=min(attempts, 64),
            next_attempt_at=next_at if isinstance(next_at, str) else "",
            staged=dict(staged),
        )


def _ledger_path(name: str) -> Path:
    from kiro_crew.apps.builtin_adoption_fetch import adoption_dir

    return adoption_dir() / f"{name}.json"


def read_ledger(name: str) -> AdoptionLedger | None:
    """*name*'s ledger, or None when absent or malformed."""
    path = _ledger_path(name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        logger.warning("adoption ledger for %r is unreadable; starting over", name)
        return None
    ledger = AdoptionLedger.from_dict(raw)
    return ledger if ledger is not None and ledger.name == name else None


def write_ledger(ledger: AdoptionLedger) -> None:
    """Persist *ledger* atomically in the owner-only ``<home>/app-adoption/``."""
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.owner_only_files import ensure_directory

    path = _ledger_path(ledger.name)
    ensure_directory(path.parent)
    atomic_write(path, json.dumps(ledger.to_dict(), indent=2, sort_keys=True) + "\n")
