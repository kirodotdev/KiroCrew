"""Reading and writing the personal Remote Control setting, ``tunnel.relay_enabled``.

There is one control. Turning Remote Control off writes an explicit ``false``, so it
stays off across a restart, whatever the Crew's credential, and across a later
change to the default
(:data:`~kiro_crew.remote_control.defaults.REMOTE_CONTROL_ON_BY_DEFAULT`). An
overlay (``config.local.json``) outranks ``config.json``, so when the overlay names
the key it is written too; an overlay that does not name it is left alone.

Both writes are blocking file IO; a handler calls them off the event loop. A
running gateway notices the change on its own (:mod:`kiro_crew.remote_control.guard`).
"""

from __future__ import annotations

SETTING_PATH = "tunnel.relay_enabled"


def requested() -> bool:
    """Whether config asks for Remote Control, overlay and default included."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.remote_control.hosting import relay_requested

    return relay_requested(KiroCrewConfig.load().tunnel)


def by_default() -> bool:
    """Whether Remote Control is on only because of the default (nobody chose)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.remote_control.hosting import hosting_by_default

    return hosting_by_default(KiroCrewConfig.load().tunnel)


def start_problem() -> str | None:
    """Why config keeps the relay from starting, or ``None``. Loads the relay client."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.relay_client.provider import RelaySettings

    problem = RelaySettings.from_config(KiroCrewConfig.load()).lease_region_problem()
    if not problem or problem.startswith("tunnel."):  # a config key keeps its own spelling
        return problem or None
    return f"{problem[:1].upper()}{problem[1:]}"


def locked() -> bool:
    """Whether this build ignores the setting (an LTS build's tunnel lock).

    There the relay never starts whatever config says (``edition_locks.relay_selected``),
    so a restart does not connect and turning it on is refused.
    """
    from kiro_crew.edition_locks import config_path_locked

    return config_path_locked(SETTING_PATH)


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


def turn_on() -> None:
    """Write ``tunnel.relay_enabled = true``; it takes effect at the next restart."""
    _write(True)
