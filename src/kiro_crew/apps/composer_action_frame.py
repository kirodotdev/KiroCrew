"""``contributes.composerActions`` on the shared app frame (``apps/app_frame.py``).

The host hands an action's frame nothing from the composer: its invoke message
carries only the invocation id and the deadline. The frame offers text
(``result.offer { kind: "insert" }``) and the dashboard inserts it as literal text
the person reads and sends. This module says which entry a frame loads (an enabled
app's declared action whose grant the owner approved) and what the frame script
hands the app's module. It is registered at the bottom of ``app_frame.py``.
"""

from __future__ import annotations

from kiro_crew.apps.composer_action_grants import approved_composer_actions
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

#: The manifest key, which is the point's name in the frame routes.
COMPOSER_ACTION_POINT = "composerActions"


def resolve_composer_action(name: str, action_id: str) -> tuple[str, bool, str] | None:
    """``(entry, process_backed, app)`` of an enabled app's approved action, else None."""
    # Imported here: app_frame imports this module at its bottom to register it.
    from kiro_crew.apps.app_frame import clean_entry, process_backed

    if not is_app_enabled(name):
        return None
    manifest = get_app_manifest(name)
    if manifest is None or manifest.name != name or not manifest.contributes.composerActions:
        return None
    action = next((a for a in manifest.contributes.composerActions if a.id == action_id), None)
    if action is None:
        return None
    if action_id not in {str(a.get("id")) for a in approved_composer_actions(name, manifest)}:
        return None
    entry = clean_entry(action.entry)
    if entry is None or "\\" in entry:
        return None
    return entry, process_backed(manifest), manifest.name


# A composer action. One invocation at a time: a second invoke while one is open is
# not delivered. The handler gets the invocation's frozen controls plus
# `insert(text)`; a returned string is the same as `insert`. Nothing from the
# composer is handed over.
COMPOSER_ACTION_SCRIPT = r"""
let openInvocation = null;
const ctx = Object.freeze({
  appName: CFG.app,
  point: 'composerActions',
  theme: () => CFG.theme,
  fetch: capFetch,
});
const POINT = {
  ctx,
  accepts: (h) => !!h && typeof h.invoke === 'function',
  missing: 'activate(ctx) returned no invoke(call)',
  message(m) {
    if (m.type !== 'composer.invoke' || typeof m.invocationId !== 'string' || openInvocation) return;
    const id = m.invocationId;
    // Null for a malformed or reused id: one id starts one invocation.
    const c = invocationControls(id);
    if (!c) return;
    openInvocation = id;
    const settle = (r) => { if (r && r.ok && openInvocation === id) openInvocation = null; return r; };
    const callObj = Object.freeze({
      invocationId: id,
      deadlineMs: clampNum(m.deadlineMs),
      signal: c.signal,
      insert: (text) => c.offerInsert(text).then(settle),
      resize: c.resize,
      close: () => { if (openInvocation === id) openInvocation = null; c.close(); },
      error: c.error,
    });
    Promise.resolve()
      .then(() => handler.invoke(callObj))
      .then((out) => { if (typeof out === 'string' && openInvocation === id) return callObj.insert(out); return undefined; })
      .catch((err) => { if (openInvocation === id) { openInvocation = null; c.error(String((err && err.message) || err)); } });
  },
};
// The runtime hands `invoke.cancel` to the handler's own `cancel`; this listener
// only frees the slot, so the next click is delivered.
window.addEventListener('message', (e) => {
  const m = e.data;
  if (e.source === P && m && m[TAG] === 1 && m.type === 'invoke.cancel' && m.invocationId === openInvocation) openInvocation = null;
});
"""


__all__ = [
    "COMPOSER_ACTION_POINT",
    "COMPOSER_ACTION_SCRIPT",
    "resolve_composer_action",
]
