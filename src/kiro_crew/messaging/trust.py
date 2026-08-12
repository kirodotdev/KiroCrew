"""The channel trust roster — which connections may attach to this instance.

A credential is not a permission. Before this roster existed, putting a bot
token into ``config.json`` and enabling the transport was the whole admission
decision: the gateway connected whatever it found configured, and the only thing
that could refuse was an enterprise ``channels`` policy — which most installs do
not have, so the effective default was "any bot that has a token may attach".
That is the gap this file closes: an operator-owned list names the connections
allowed to attach, and one that is not named does not connect.

WHY A SEPARATE FILE, and not the ``channels`` scope of ``security_policy.json``:
that document is the ENTERPRISE ceiling, and its ABSENCE means ungoverned. Making
attachment fail closed there would force every standalone operator to author an
enterprise policy just to keep their own bot working. So this follows the
:mod:`kiro_crew.platform.admission` pattern instead, which solved the same
problem for plugins: a permissive default is SEEDED once at first run, so
"file present and permissive" is distinguishable from "file absent", and only the
latter fails closed. An upgrade is therefore transparent — the roster is seeded
from what is already configured — while a later deletion or tampering is a
fail-closed event rather than a silent return to open.

The roster is a TRUST ROOT: its path sits on ``security._SENSITIVE_HOME_DIRS``,
so the agent can neither read nor rewrite the list of principals allowed to talk
to it. It is read by the gateway process (which is not the agent sandbox), the
same way the Security page reads the security policy.

Relationship to the ``channels`` ceiling: this roster answers "may this
connection attach at all", the ceiling answers "and what may it then do". Both
apply, tightest-wins — a connection must be on the roster AND permitted by the
policy. The roster is the operator's own control; the ceiling is the enterprise's.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from kiro_crew.config.paths import config_dir
from kiro_crew.messaging.connections import ConnectionNameError, ConnectionRef, parse_item

logger = logging.getLogger(__name__)

#: Schema version this loader understands. A file declaring another version fails
#: closed rather than being read under guessed semantics.
ROSTER_VERSION = 1

_ROSTER_LEAF = "channel_trust.json"
_SEED_MARKER_LEAF = "channel_trust_seeded"


def roster_path() -> Path:
    """Where the trust roster lives. Resolved lazily (never at import time).

    Deferred so importing this module does not fire ``config_dir()`` — and thus
    the one-time data-home migration — as an import side effect, matching the
    other trust-root readers.
    """
    return config_dir() / _ROSTER_LEAF


def _seed_marker_path() -> Path:
    return config_dir() / ".migrations" / _SEED_MARKER_LEAF


@dataclass(frozen=True)
class TrustRoster:
    """The connections an operator has allowed to attach."""

    #: Governance items (``telegram/default``) of every trusted connection.
    trusted: frozenset[str] = field(default_factory=frozenset)
    #: True when the roster was actually read from disk. ``False`` means it was
    #: absent or unreadable, and :attr:`trusted` is empty because we FAILED
    #: CLOSED — not because the operator trusts nothing. Callers that report
    #: state to a human must tell those two apart.
    loaded: bool = False
    #: Why the load failed, for the operator-facing message. Empty on success.
    error: str = ""

    def admits(self, ref: ConnectionRef) -> bool:
        return ref.governance_item() in self.trusted


def _fail_closed(reason: str) -> TrustRoster:
    """The roster used when the real one cannot be read: admit nothing."""
    return TrustRoster(trusted=frozenset(), loaded=False, error=reason)


def feature_enabled(cfg: object = None) -> bool:
    """Whether connection governance is in force at all.

    TWO independent switches, and the difference between them is the point:

    * ``messaging.connection_governance`` in config — the operator's. Reachable
      from ``config.json``, the ``config.local.json`` overlay and the CLI, so the
      running app (and its agent) can lift it.
    * ``capabilities.channel_connections`` in the trust-root policy — the
      fleet's. Read from ``security_policy.json``, which the agent can neither
      read nor write, so a pinned-off fleet stays off. This is how an
      organization whose per-surface scoping will arrive as crew members turns
      the whole surface off instead of half-adopting it.

    Either being off disables the feature: attachment falls back to credentials +
    config alone (the behaviour before the roster existed) and the connection
    surfaces render nothing.

    Deliberately NOT fail-closed, unlike every gate this feature adds. A gate
    answers "may this principal act?", where refusing on doubt costs one blocked
    message. This answers "is this feature switched on?", and refusing on doubt
    would mean a governance-evaluation hiccup silently STOPS EVERY CHANNEL on a
    host that never opted out — turning an unrelated glitch into a total outage.
    So an unevaluable ceiling leaves the feature ON and the gates behind it do
    their own fail-closed work.

    ``cfg`` is injectable for tests; production reads the live config.
    """
    try:
        if cfg is None:
            from kiro_crew.config.loader import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        messaging = getattr(cfg, "messaging", None)
        if not bool(getattr(messaging, "connection_governance", True)):
            return False
    except Exception:
        logger.debug("connection-governance config unreadable; treating as on", exc_info=True)
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.channel_connections", "", log_warning=False
        )
        # A POLICY-layer denial is the fleet pin. A profile-layer denial is not:
        # this is a process-wide feature switch, not a per-surface permission, so a
        # per-surface profile is not the question it answers (the same
        # policy-layer-only rule ``beacon.is_governance_pinned_off`` applies).
        if not bool(getattr(decision, "permitted", True)):
            return getattr(decision, "layer", "") != "policy"
    except Exception:
        logger.debug("channel-connections capability probe failed; treating as on", exc_info=True)
    return True


def load_roster() -> TrustRoster:
    """Read the trust roster. A missing or malformed file admits NOTHING.

    Failing closed is the entire point of the file's existence: if a deleted or
    corrupt roster fell back to "admit everything", an attacker (or an accident)
    could restore the default-open behaviour by removing one file. The permissive
    first-run seed is what keeps that from breaking ordinary installs —
    see :func:`seed_roster`.

    Never raises. Blocking filesystem I/O, so callers on the event loop must
    offload it (the transport-start gate already runs in an executor).
    """
    path = roster_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error(
            "channel trust roster %s is absent; refusing to attach any chat "
            "connection (fail closed). Restore the file, or delete %s to let the "
            "next start re-seed it from the currently-configured channels.",
            path,
            _seed_marker_path(),
        )
        return _fail_closed("absent")
    except Exception as exc:
        logger.error(
            "channel trust roster %s is unreadable (%s); refusing to attach any "
            "chat connection (fail closed).",
            path,
            exc,
        )
        return _fail_closed("unreadable")
    if not isinstance(data, dict):
        return _fail_closed("not_an_object")
    version = data.get("version")
    if version != ROSTER_VERSION:
        logger.error(
            "channel trust roster %s declares version %r (expected %d); failing closed.",
            path,
            version,
            ROSTER_VERSION,
        )
        return _fail_closed("version_mismatch")
    raw = data.get("connections")
    if not isinstance(raw, list):
        return _fail_closed("connections_not_a_list")
    trusted: set[str] = set()
    for entry in raw:
        # Accept both the terse string form and an object carrying a note, so an
        # operator can annotate WHY a connection is trusted without the loader
        # caring. An entry that is neither is skipped rather than fatal: one typo
        # must not take every other connection offline with it.
        item = entry if isinstance(entry, str) else (
            entry.get("id") if isinstance(entry, dict) else None
        )
        if not isinstance(item, str) or not item.strip():
            logger.warning("channel trust roster: skipping malformed entry %r", entry)
            continue
        try:
            trusted.add(parse_item(item).governance_item())
        except ConnectionNameError as exc:
            logger.warning("channel trust roster: skipping unusable entry %r (%s)", item, exc)
    return TrustRoster(trusted=frozenset(trusted), loaded=True)


def seed_roster(configured: Iterable[ConnectionRef]) -> bool:
    """One-time: write a roster trusting the connections already configured.

    Makes an upgrade transparent — an install whose channels worked before this
    gate existed keeps working, because what it had configured becomes what it
    trusts. Guarded by its own marker so a LATER deletion of the roster is never
    silently re-seeded: that is the tamper signal :func:`load_roster` fails closed
    on, and re-creating it automatically would erase the signal.

    Returns True only when it wrote the file. Never raises (first-run work is
    best-effort and must not block a start).
    """
    marker = _seed_marker_path()
    path = roster_path()
    try:
        # A pinned-off fleet must not gain a roster file it never asked for: the
        # file's PRESENCE is this feature's "intended open" signal, so writing one
        # while the feature is disabled would leave a misleading artifact behind
        # for whoever later turns it on.
        if not feature_enabled():
            return False
        if marker.exists():
            return False
        wrote = False
        if not path.exists():
            entries = sorted({ref.governance_item() for ref in configured})
            body = {
                "version": ROSTER_VERSION,
                "connections": [{"id": item, "note": "seeded from config at first run"} for item in entries],
                "_comment": (
                    "Chat connections allowed to attach to this instance. A connection "
                    "absent from this list does not connect, even with valid credentials. "
                    "Deleting this file does NOT restore the old admit-everything "
                    "behaviour: the loader then fails closed and no channel attaches."
                ),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
            wrote = True
        # Written even when no file was created, so a roster the operator authored
        # by hand before first start is never clobbered on a later start either.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(tz=timezone.utc).isoformat() + "\n", encoding="utf-8")
        return wrote
    except Exception:
        logger.warning("first-run: channel trust roster seed failed", exc_info=True)
        return False


def admits(ref: ConnectionRef, roster: Optional[TrustRoster] = None) -> bool:
    """Whether *ref* may attach. Loads the roster when one is not supplied."""
    return (roster if roster is not None else load_roster()).admits(ref)


__all__ = [
    "ROSTER_VERSION",
    "TrustRoster",
    "roster_path",
    "feature_enabled",
    "load_roster",
    "seed_roster",
    "admits",
]
