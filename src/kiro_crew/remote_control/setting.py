"""Reading and writing the personal Remote Control setting, ``tunnel.relay_enabled``.

There is one control. Turning Remote Control off writes an explicit ``false``, so it
stays off across a restart and across a later change to the default
(:data:`~kiro_crew.remote_control.defaults.REMOTE_CONTROL_ON_BY_DEFAULT`). An
overlay (``config.local.json``) outranks ``config.json``, so when the overlay names
the key it is written too; an overlay that does not name it is left alone.

Turning it on is refused while the sign-in is refused
(:func:`~kiro_crew.remote_control.policy.current_decision`), and nothing is written.

Both writes are blocking file IO; a handler calls them off the event loop. A
running gateway notices the change on its own (:mod:`kiro_crew.remote_control.guard`).
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.remote_control.policy import Decision, current_decision

SETTING_PATH = "tunnel.relay_enabled"


class RemoteControlRefused(Exception):
    """Turning Remote Control on was refused; ``decision`` says why."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.notice or decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class SettingView:
    """What the setting says and whether the sign-in lets it run."""

    requested: bool
    decision: Decision

    @property
    def effective(self) -> bool:
        """On in config AND allowed by the sign-in: the relay may run."""
        return self.requested and self.decision.ok


def requested() -> bool:
    """Whether config asks for Remote Control, overlay and default included."""
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().tunnel.relay_enabled is True


def read() -> SettingView:
    return SettingView(requested=requested(), decision=current_decision())


def _set_in(doc: dict, value: bool) -> dict:
    section = doc.get("tunnel")
    if not isinstance(section, dict):
        section = {}
        doc["tunnel"] = section
    section["relay_enabled"] = value
    return doc


def _write(value: bool) -> None:
    from kiro_crew.config.loader import config_local_path, config_path, update_config_locked
    from kiro_crew.config.superseded_defaults import ack_explicit_write

    def _base(doc: dict) -> dict:
        ack_explicit_write(SETTING_PATH, value)
        return _set_in(doc, value)

    update_config_locked(config_path(), mutate=_base)

    overlay = config_local_path()
    if not overlay.exists():
        return

    def _overlay(doc: dict) -> dict | None:
        section = doc.get("tunnel")
        if not isinstance(section, dict) or "relay_enabled" not in section:
            return None
        return _set_in(doc, value)

    update_config_locked(overlay, mutate=_overlay, stamp_meta=False)


def turn_off() -> None:
    """Write ``tunnel.relay_enabled = false`` wherever it decides the value."""
    _write(False)


def turn_on() -> Decision:
    """Write ``tunnel.relay_enabled = true``, or raise :class:`RemoteControlRefused`."""
    decision = current_decision()
    if not decision.ok:
        raise RemoteControlRefused(decision)
    _write(True)
    return decision
