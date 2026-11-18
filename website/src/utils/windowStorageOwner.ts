/** Whether this window's sessionStorage data under one feature's id key is its own. A popout starts
 *  with a copy of its opener's, recognised by still carrying the opener's id; it then takes a fresh one. */

function browserStorage(): Storage | null {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage } catch { return null }
}

function newWindowId(): string {
  try { if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID() } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

/** The opener's id under `idKey`, or null with no live same-origin opener. */
function openerWindowId(idKey: string): string | null {
  try {
    if (typeof window === 'undefined') return null
    const opener = window.opener as Window | null
    if (!opener || opener.closed) return null
    return opener.sessionStorage.getItem(idKey)
  } catch { return null }
}

/** Call once per page load per key: a clone answers `false` once, then its fresh id answers `true`. */
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
