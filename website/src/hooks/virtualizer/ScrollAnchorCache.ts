// Persistent reading-position anchor for the chat virtualizer.
//
// Stores, per session, the stable key of the topmost visible row plus its
// pixel offset from the scroller's viewport top — NOT a raw scrollTop. A raw
// pixel offset is meaningless before the virtualizer has measured rows (it is
// exactly what produced the historical "second visit lands in the middle"
// bug), whereas a row key survives re-measurement: the restore path mounts a
// window around that row and positions it back at the saved offset, refining
// as real measurements land.
//
// The anchor is written on scroll-settle while the user is scrolled UP, and
// cleared once they return to the bottom, so "no anchor" means "open at the
// bottom" — the existing slot-entry default. useVirtualChat owns when to
// save/restore; this module only owns the storage format.
//
// Falls back to no-op when localStorage is unavailable (private browsing,
// quota exceeded, sandboxed iframes). Corrupted or malformed blobs are
// treated as "no anchor" — never thrown.

// localStorage key prefix — a storage identifier, never rendered. Not UI copy.
// Kept in sync with SESSION_PREFIXES in `utils/storageGc.ts`, which garbage-
// collects these keys; changing it orphans every persisted anchor.
//
// v2: anchors written before the hard-input save gate existed can hold a
// SELF-SCROLL displacement as if it were the reader's position (the "opens
// mid-transcript with a skeleton wall" reports). Those blobs are
// indistinguishable from honest ones after the fact, so the version bump
// deliberately orphans them all — a one-time amnesty. The old-prefix reaper
// below removes the orphans.
export const ANCHOR_KEY_PREFIX = 'vc_anchor2_'
const LEGACY_ANCHOR_KEY_PREFIX = 'vc_anchor_'

/** One-time reap of pre-gate anchors (see the prefix doc). Runs at module
 *  load; best-effort, quota/private-mode failures are ignored. */
function reapLegacyAnchors(): void {
  try {
    const ls = typeof window !== 'undefined' ? window.localStorage : null
    if (!ls) return
    const doomed: string[] = []
    for (let i = 0; i < ls.length; i++) {
      const k = ls.key(i)
      if (k && k.startsWith(LEGACY_ANCHOR_KEY_PREFIX) && !k.startsWith(ANCHOR_KEY_PREFIX)) {
        doomed.push(k)
      }
    }
    for (const k of doomed) ls.removeItem(k)
  } catch { /* storage unavailable — nothing to reap */ }
}
reapLegacyAnchors()

/** A persisted reading position. */
export interface ScrollAnchor {
  /** Virtual row key (see ChatPage's virtualKeyFor) of the topmost visible row. */
  key: string
  /** The row's top edge offset (px) relative to the scroller viewport top.
   *  Usually <= 0 for a row that starts above the viewport top. */
  top: number
}

/** Returns the localStorage object if accessible, else null. */
function getStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null
    const ls = window.localStorage
    const probe = '__vc_anchor_probe__'
    ls.setItem(probe, probe)
    ls.removeItem(probe)
    return ls
  } catch {
    return null
  }
}

/** Persist `anchor` as the reading position for `sessionId`. Best-effort. */
export function saveScrollAnchor(sessionId: string, anchor: ScrollAnchor): void {
  const storage = getStorage()
  if (!storage || !sessionId) return
  try {
    storage.setItem(`${ANCHOR_KEY_PREFIX}${sessionId}`, JSON.stringify(anchor))
  } catch {
    // Quota exceeded or transient failure — losing a reading position is
    // strictly cosmetic (the session opens at the bottom), so swallow.
  }
}

/** Load the saved reading position for `sessionId`, or null when absent/invalid. */
export function loadScrollAnchor(sessionId: string): ScrollAnchor | null {
  const storage = getStorage()
  if (!storage || !sessionId) return null
  let raw: string | null
  try {
    raw = storage.getItem(`${ANCHOR_KEY_PREFIX}${sessionId}`)
  } catch {
    return null
  }
  if (raw === null) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    // Corrupted blob — remove so it can't keep poisoning future loads.
    try { storage.removeItem(`${ANCHOR_KEY_PREFIX}${sessionId}`) } catch { /* ignore */ }
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const key = (parsed as Record<string, unknown>).key
  const top = (parsed as Record<string, unknown>).top
  if (typeof key !== 'string' || key.length === 0) return null
  if (typeof top !== 'number' || !Number.isFinite(top)) return null
  return { key, top }
}

/** Remove the saved reading position for `sessionId`. Best-effort. */
export function clearScrollAnchor(sessionId: string): void {
  const storage = getStorage()
  if (!storage || !sessionId) return
  try {
    storage.removeItem(`${ANCHOR_KEY_PREFIX}${sessionId}`)
  } catch {
    // Best-effort — swallow.
  }
}
