"""Carry a folded built-in's on/off state into the core setting that replaced it.

A built-in whose feature moved into core is listed in
``builtins._MIGRATED_BUILTINS``, and the boot cleanup
(``manager_builtins.cleanup_migrated_builtin``) retires its record. When the app's
enabled state WAS the feature's switch, retiring the record would lose the user's
choice, so ``builtins._FOLDED_BUILTIN_SWITCHES`` names the dotted ``config.json``
path that holds the switch now and :func:`carry_folded_switch` writes the record's
state there before the record goes. The record's state is read by the caller
(``manager_builtins.folded_switch_state``).

Every such setting defaults to what the app's ``defaultEnabled`` was: on, unless the
name is in ``builtins._FOLDED_OFF_BY_DEFAULT``. Only a state that differs from that
default is written, so a record that matched it needs no key and an upgrade writes
nothing to ``config.json`` for it. A key already in the file is kept, because only
Settings (or ``kirocrew config set``) writes it and that is the user's own later
choice.
"""

from __future__ import annotations


def carry_folded_switch(name: str, on: bool) -> None:
    """Write app *name*'s state *on* to the setting that replaced it.

    Does nothing for an app that is not in ``_FOLDED_BUILTIN_SWITCHES`` or whose
    state is the setting's default. Raises when the write fails, so the caller keeps
    the record and the next start tries again rather than dropping the user's choice.
    """
    from kiro_crew.apps.builtins import _FOLDED_BUILTIN_SWITCHES, _FOLDED_OFF_BY_DEFAULT

    path = _FOLDED_BUILTIN_SWITCHES.get(name)
    if path is None or on is (name not in _FOLDED_OFF_BY_DEFAULT):
        return
    *parents, key = path.split(".")
    from kiro_crew.config.loader import update_config_locked

    def _mutate(data: dict) -> dict | None:
        section = _section_for(data, parents)
        if key in section:
            return None
        section[key] = on
        return data

    update_config_locked(mutate=_mutate)


def set_folded_switch(name: str, on: bool) -> str | None:
    """Turn the setting that replaced folded app *name* on or off, for ``app enable``.

    ``kirocrew app enable <name>`` and ``app disable <name>`` reach a folded app's
    feature through here, so the command a user ran before the fold still works.
    Returns the dotted path written, or ``None`` when *name* is no folded switch or
    an app a user installed under that name holds it (that app is its own app, and
    the ordinary enable path answers for it). Unlike the boot carry it always
    writes, because this is the user's own choice. Raises when the write fails.
    """
    from kiro_crew.apps.builtins import _FOLDED_BUILTIN_SWITCHES
    from kiro_crew.apps.manager import _read_installed

    path = _FOLDED_BUILTIN_SWITCHES.get(name)
    if path is None:
        return None
    record = _read_installed(name)
    if record is not None and record.origin != "builtin":
        return None
    *parents, key = path.split(".")
    from kiro_crew.config.loader import update_config_locked

    def _mutate(data: dict) -> dict | None:
        section = _section_for(data, parents)
        if section.get(key) is on:
            return None
        section[key] = on
        return data

    update_config_locked(mutate=_mutate)
    return path


def _section_for(data: dict, parents: list[str]) -> dict:
    """The section of config document *data* at the dotted *parents*, made if absent."""
    section = data
    for part in parents:
        child = section.get(part)
        if not isinstance(child, dict):
            # A missing or malformed section carries no setting, the same recovery
            # the dashboard's own config write makes.
            child = section[part] = {}
        section = child
    return section
