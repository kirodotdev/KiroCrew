/**
 * activePaneMemory — which pane this WINDOW was showing, kept across a reload.
 *
 * `instances.activeId` lives only in the Redux store, so a reload (Cmd+R in the
 * desktop app, a browser refresh) used to land every window back on Local.
 * sessionStorage is the right lifetime: it survives a reload of the same
 * window but starts empty in a new one (the desktop "New Window" is a fresh
 * BrowserWindow), so a new window still opens on Local.
 *
 * Only the crew id (a name slug, not a secret) is stored. Never the port or
 * token: those stay in memory only (see instancesSlice). Whether the id may be
 * restored is the caller's call: it must still name a crew in the switcher, or
 * the window falls back to Local.
 */
const KEY = 'mc-active-pane'
// Crew ids are name slugs; anything longer than this was not written by us.
const MAX_ID_LEN = 256

/** Record the pane this window shows; `null` (Local) forgets it. */
export function rememberActivePane(id: string | null): void {
  try {
    if (id) sessionStorage.setItem(KEY, id)
    else sessionStorage.removeItem(KEY)
  } catch {
    // Storage disabled or full: a reload lands on Local, as before.
  }
}

/** The pane this window showed before its last reload, or `null` for Local. */
export function recallActivePane(): string | null {
  try {
    const id = sessionStorage.getItem(KEY)
    return id && id.length <= MAX_ID_LEN ? id : null
  } catch {
    return null
  }
}
