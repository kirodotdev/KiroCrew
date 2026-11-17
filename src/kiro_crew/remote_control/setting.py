"""Reading and writing the personal Remote Control setting, ``tunnel.relay_enabled``.

There is one control. Turning Remote Control off writes an explicit ``false`` and
``tunnel.relay_turned_off`` true, so it stays off across a restart, across a later
change to the default and with a lease credential set
(:func:`~kiro_crew.remote_control.defaults.hosting_requested`). An overlay
(``config.local.json``) outranks ``config.json``, so when the overlay names either
key both are written there too; an overlay that names neither is left alone.

Both writes are blocking file IO; a handler calls them off the event loop. A
running gateway notices the change on its own (:mod:`kiro_crew.remote_control.guard`).
"""

from __future__ import annotations

SETTING_PATH = "tunnel.relay_enabled"
#: Written true by :func:`turn_off` and false by :func:`turn_on`, so off outranks
#: a lease credential (:func:`~kiro_crew.remote_control.defaults.hosting_requested`).
OFF_MARKER = "relay_turned_off"


def requested() -> bool:
    """Whether config asks for Remote Control, overlay and default included."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.remote_control.defaults import hosting_requested

    return hosting_requested(KiroCrewConfig.load().tunnel)


def credential_set() -> bool:
    """Whether ``tunnel.relay_lease_credential`` names a host credential."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.remote_control import defaults

    return defaults.credential_set(KiroCrewConfig.load().tunnel.relay_lease_credential)


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
    # Off is its own marker: a stored relay_enabled false is also every install's
    # saved default, and a lease credential would start hosting over it.
    section[OFF_MARKER] = not value
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
        if not isinstance(section, dict) or not {"relay_enabled", OFF_MARKER} & section.keys():
            return None
        return _set_in(doc, value)

    update_config_locked(overlay, mutate=_overlay, stamp_meta=False)


def turn_off() -> None:
    """Write ``tunnel.relay_enabled = false`` wherever it decides the value."""
    _write(False)


def turn_on() -> None:
    """Write ``tunnel.relay_enabled = true``; it takes effect at the next restart."""
    _write(True)
