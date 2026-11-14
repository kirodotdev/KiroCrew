"""Carry a folded built-in's on/off state into the core setting that replaced it.

A built-in whose feature moved into core is listed in
``builtins._MIGRATED_BUILTINS``, and the boot cleanup
(``manager_builtins.cleanup_migrated_builtin``) retires its record. When the app's
enabled state WAS the feature's switch, retiring the record would lose the user's
choice, so ``builtins._FOLDED_BUILTIN_SWITCHES`` names the dotted ``config.json``
path that holds the switch now and :func:`carry_folded_switch` writes the record's
state there before the record goes. The record's state is read by the caller
(``manager_builtins.folded_switch_state``).

Every such setting defaults on, as the app did, so only an opt-out is written: a
record that had the feature on needs no key, and an upgrade writes nothing to
``config.json`` for it. A key already in the file is kept, because only Settings
writes it and that is the user's own later choice.
"""

from __future__ import annotations


def carry_folded_switch(name: str, on: bool) -> None:
    """Write an off state of app *name* to the setting that replaced it.

    Does nothing for an app that is not in ``_FOLDED_BUILTIN_SWITCHES`` or whose
    feature was on. Raises when the write fails, so the caller keeps the record and
    the next start tries again rather than dropping the user's opt-out.
    """
    from kiro_crew.apps.builtins import _FOLDED_BUILTIN_SWITCHES

    path = _FOLDED_BUILTIN_SWITCHES.get(name)
    if path is None or on:
        return
    *parents, key = path.split(".")
    from kiro_crew.config.loader import update_config_locked

    def _mutate(data: dict) -> dict | None:
        section = data
        for part in parents:
            child = section.get(part)
            if not isinstance(child, dict):
                # A missing or malformed section carries no setting, the same
                # recovery the dashboard's own config write makes.
                child = section[part] = {}
            section = child
        if key in section:
            return None
        section[key] = False
        return data

    update_config_locked(mutate=_mutate)
