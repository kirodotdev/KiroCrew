"""The ``selectionActions`` point on the shared app frame (``app_frame.py``).

Two things the frame model asks of a point: a resolver, which says whether one
item of an app may run (enabled, declared, its current grant entry approved), and
the point's frame script, which turns the host's ``selection.invoke`` into a call on
the module's handler. The invocation itself (the id, sequenced offers, resize,
close, error, cancel) is the shared contract; the item hands its handler
``invocationControls(invocationId)`` and nothing else.

Registered at the bottom of ``app_frame.py``.
"""

from __future__ import annotations

from collections.abc import Mapping

from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.apps.selection_action_grants import (
    approved_selection_action,
    selection_action_entry,
)

#: The point's manifest key under ``contributes`` and its frame-point name.
SELECTION_ACTIONS_POINT = "selectionActions"


def resolve_selection_action(name: str, item_id: str) -> tuple[str, bool, str] | None:
    """The served entry of an enabled app's item whose current grant is approved."""
    # Imported here: ``app_frame`` imports this module to register the point.
    from kiro_crew.apps.app_frame import clean_entry, process_backed

    if not is_app_enabled(name):
        return None
    manifest = get_app_manifest(name)
    if manifest is None or manifest.name != name:
        return None
    item = approved_selection_action(name, manifest, item_id)
    if item is None:
        return None
    entry = clean_entry(str(item.get("entry", "")))
    if entry is None:
        return None
    return entry, process_backed(manifest), manifest.name


def admit_selection_action(name: str, item_id: str, query: Mapping[str, str]) -> bool:
    """Whether the mint's ``grant`` is the item's current approved grant entry.

    The dashboard resolves an item from one read of the app list and truncates the
    selection to that item's ``maxLength``. Every mint and renewal names the entry it
    was resolved at, so a pick made against an item the owner has since changed (a
    lower ``maxLength``, a dropped effect) is refused before any text is sent, and a
    live frame's renewal is refused once the item changes.
    """
    grant = query.get("grant")
    if not grant:
        return False
    manifest = get_app_manifest(name)
    if manifest is None or manifest.name != name:
        return False
    item = approved_selection_action(name, manifest, item_id)
    return item is not None and selection_action_entry(item) == grant


# The handler gets the selection and that invocation's controls; `ctx` carries no
# selection, no session and no DOM. `cancel(invocationId, reason)` comes from the
# shared runtime's `invoke.cancel`.
SELECTION_ACTION_SCRIPT = r"""
const POINT = {
  ctx: Object.freeze({ appName: CFG.app, theme: () => CFG.theme, fetch: capFetch }),
  accepts: (h) => !!h && typeof h.invoke === 'function',
  missing: 'activate(ctx) returned no handler with invoke(selection, call)',
  message(m) {
    if (m.type !== 'selection.invoke' || typeof m.invocationId !== 'string') return;
    if (m.theme === 'dark' || m.theme === 'light') CFG.theme = m.theme;
    const selection = Object.freeze({
      text: typeof m.text === 'string' ? m.text : '',
      truncated: m.truncated === true,
      source: typeof m.source === 'string' ? m.source : 'chat',
    });
    call('invoke', selection, invocationControls(m.invocationId));
  },
};
"""


__all__ = [
    "SELECTION_ACTIONS_POINT",
    "SELECTION_ACTION_SCRIPT",
    "admit_selection_action",
    "resolve_selection_action",
]
