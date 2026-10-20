/**
 * Whether what this window's sessionStorage holds under one feature's keys is its own.
 *
 * A window opened by another starts with a copy of its opener's sessionStorage. Each feature keeps
 * this browsing context's id beside its data, so a clone is recognised by still carrying the
 * opener's id: that is a popout's first load, before it has written anything of its own. The
 * window then takes a fresh id, so every later load (reload, back/forward, or an ordinary
 * navigation inside the popout) reads as its own. A window with no id yet takes one and keeps
 * whatever is stored.
 */

function browserStorage(): Storage | null {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage } catch { return null }
}

function newWindowId(): string {
  try { if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID() } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

/** The id the opener's sessionStorage holds under `idKey`, or null when there is no live
 *  same-origin opener to read. */
function openerWindowId(idKey: string): string | null {
  try {
    if (typeof window === 'undefined') return null
    const opener = window.opener as Window | null
    if (!opener || opener.closed) return null
    return opener.sessionStorage.getItem(idKey)
  } catch { return null }
}

/** Whether the data kept beside `idKey` belongs to this window. Call it once per page load per
 *  key: a clone is answered `false` once, and the fresh id it then takes makes later calls `true`. */
export function ownsStoredSession(idKey: string): boolean {
  const store = browserStorage()
  if (!store) return true
  try {
    const mine = store.getItem(idKey)
    const cloned = !!mine && mine === openerWindowId(idKey)
    if (!mine || cloned) store.setItem(idKey, newWindowId())
    return !cloned
  } catch { return true }
}
