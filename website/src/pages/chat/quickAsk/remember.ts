/**
 * "Remember on <origin>": a username, name or email the person chose to keep,
 * in this browser's localStorage (the dashboard owner's own profile), keyed by
 * the ask's EXACT origin, so `https://github.com` never offers a value to
 * `http://github.com` or `https://gist.github.com`. A saved value is only ever
 * offered as a "Use <value>" chip; nothing here fills a field by itself.
 *
 * Passwords and one-time codes are never stored: `RememberKey` has no slot
 * for them, so no caller can pass one.
 *
 * Saved logins come from the Secrets vault instead, under its preview: the
 * gateway matches vault logins to the ask by base domain
 * (`browser_harness/vault_fill.py`) and the card shows them by username, never
 * through this module. On a credentials ask with that vault part the
 * "Remember" box is hidden and nothing here is written; a username kept
 * earlier is still offered as its chip when no vault login matches. The
 * sheet's inputs opt OUT of the
 * browser's own manager (`autocomplete="off"` / `"new-password"`): the sheet
 * lives on the dashboard's origin, so the manager would file every site's
 * password under that one origin and offer site A's password on site B's ask.
 */
export type RememberKey = 'username' | 'name' | 'email'

const STORAGE_KEY = 'kc.quickAsk.remembered.v1'
const VALUE_MAX = 200

type Saved = Record<string, Partial<Record<RememberKey, string>>>

function read(): Saved {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const d = raw ? JSON.parse(raw) as unknown : null
    return d && typeof d === 'object' && !Array.isArray(d) ? d as Saved : {}
  } catch {
    return {}
  }
}

function write(d: Saved): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(d)) } catch { /* storage full or off: remembering is best effort */ }
}

export function recall(origin: string, key: RememberKey): string {
  const v = read()[origin]?.[key]
  return typeof v === 'string' ? v : ''
}

/** Keep `value` for `origin`, or forget it when `value` is empty. */
export function remember(origin: string, key: RememberKey, value: string): void {
  const d = read()
  const entry = { ...(d[origin] ?? {}) }
  const v = value.trim().slice(0, VALUE_MAX)
  if (v) entry[key] = v
  else delete entry[key]
  if (Object.keys(entry).length) d[origin] = entry
  else delete d[origin]
  write(d)
}

export const __REMEMBER_STORAGE_KEY = STORAGE_KEY
