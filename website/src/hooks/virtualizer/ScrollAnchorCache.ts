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
//
// v3: anchors written before the restore resolved rows by their STABLE id hold
// a key from ChatPage's `rowKeys` vocabulary, which that module documents as
// correct ONLY against the displayItems of its own render. Persisted across a
// session switch it therefore never resolves, and a failed restore also clears
// the anchor — so the reader lands at the bottom every time and the position is
// erased on the way. Orphaning those blobs costs nothing (not one of them was
// resolvable) and spares each session one guaranteed failed restore.
export const ANCHOR_KEY_PREFIX = 'vc_anchor3_'
import { devLog, keyShape, shortId } from '../../dev/scrollInspector'
const LEGACY_ANCHOR_KEY_PREFIXES = ['vc_anchor_', 'vc_anchor2_']

/** One-time reap of pre-gate anchors (see the prefix doc). Runs at module
 *  load; best-effort, quota/private-mode failures are ignored. */
function reapLegacyAnchors(): void {
  try {
    const ls = typeof window !== 'undefined' ? window.localStorage : null
    if (!ls) return
    const doomed: string[] = []
    for (let i = 0; i < ls.length; i++) {
      const k = ls.key(i)
      if (k && !k.startsWith(ANCHOR_KEY_PREFIX) && LEGACY_ANCHOR_KEY_PREFIXES.some((pfx) => k.startsWith(pfx))) {
        doomed.push(k)
      }
    }
    for (const k of doomed) ls.removeItem(k)
  } catch { /* storage unavailable — nothing to reap */ }
}
reapLegacyAnchors()

/** A persisted reading position. */
export interface ScrollAnchor {
  /** STABLE row id (see ChatPage's stableAnchorIdFor) of the topmost visible
   *  row -- the row's TAIL message, which a page landing does not rename. Never
   *  the per-render `virtualKeyFor` key: that one is only valid inside the
   *  render that produced it, so a persisted copy cannot be resolved later. */
  key: string
  /** SECOND identity for the same row: its LEAD message (`l-` prefixed, so it
   *  can never be confused with a tail id). The two fail in opposite cases and
   *  cover each other:
   *
   *   - a turn growing at its TAIL (streaming appends) renames `key`;
   *   - a turn growing at its HEAD (an older page landing regroups messages
   *     into it) renames the lead.
   *
   *  A switch always prepends a page AND the slot may be mid-turn, so both
   *  happen -- which is why neither id alone resolves reliably, and why matching
   *  either one is what makes a restore survive a live turn. Optional: an anchor
   *  persisted before this field existed still resolves by `key` alone. */
  alt?: string
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
/**
 * Smallest change in a saved reading position worth a storage write.
 *
 * Deliberately the same magnitude the RESTORE treats as already-landed: a
 * difference the restore would call converged is not a difference worth
 * persisting. (Kept here rather than imported from the virtualizer, which
 * imports this module.)
 */
const ANCHOR_SAVE_EPSILON_PX = 1.5

/**
 * Whether writing `next` would change what a later load actually reads.
 *
 * The save is driven by a debounce that a STREAMING transcript re-arms on its
 * own: growth fires scroll events, and inside the save's 3s intent window each
 * one triggers another write. Measured parked at the bottom: five `clear`s in
 * 0.6s, none of which changed anything, plus repeated saves of the same row
 * differing by a single pixel.
 *
 * A pure de-duplication, and that is the whole safety argument: the readable
 * state after a skipped write is identical to the state after performing it. A
 * slow drift is still persisted, because each attempt compares against what is
 * STORED rather than against the last attempt -- so the difference accumulates
 * until it crosses the epsilon instead of being lost.
 */
/** Parse the stored anchor WITHOUT logging or the legacy migration `load` does.
 *  Used by the write de-duplication, which must not narrate a read as a load or
 *  it reports a `STORE.load` for every save attempt. */
function peekStoredAnchor(storage: Storage, sessionId: string): ScrollAnchor | null {
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
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const key = (parsed as Record<string, unknown>).key
  const top = (parsed as Record<string, unknown>).top
  if (typeof key !== 'string' || key.length === 0) return null
  if (typeof top !== 'number' || !Number.isFinite(top)) return null
  const alt = (parsed as Record<string, unknown>).alt
  return typeof alt === 'string' && alt.length > 0 ? { key, top, alt } : { key, top }
}

export function anchorWriteChangesState(prev: ScrollAnchor | null, next: ScrollAnchor): boolean {
  if (!prev) return true
  if (prev.key !== next.key) return true
  if ((prev.alt ?? '') !== (next.alt ?? '')) return true
  return Math.abs(prev.top - next.top) > ANCHOR_SAVE_EPSILON_PX
}

export function saveScrollAnchor(sessionId: string, anchor: ScrollAnchor): void {
  const storage = getStorage()
  if (!storage || !sessionId) return
  if (!anchorWriteChangesState(peekStoredAnchor(storage, sessionId), anchor)) {
    devLog('STORE.skip', `${shortId(sessionId)} ${keyShape(anchor.key)}@${Math.round(anchor.top)}`)
    return
  }
  try {
    storage.setItem(`${ANCHOR_KEY_PREFIX}${sessionId}`, JSON.stringify(anchor))
    devLog('STORE.save', `${shortId(sessionId)} ${keyShape(anchor.key)}@${Math.round(anchor.top)}`)
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
  if (raw === null) { devLog('STORE.load', `${shortId(sessionId)} ABSENT`); return null }
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
  const alt = (parsed as Record<string, unknown>).alt
  const altOk = typeof alt === 'string' && alt.length > 0 ? alt : undefined
  devLog('STORE.load', `${shortId(sessionId)} ${keyShape(key)}@${Math.round(top)}${altOk ? '+alt' : ''}`)
  return altOk ? { key, top, alt: altOk } : { key, top }
}

/** Remove the saved reading position for `sessionId`. Best-effort. */
export function clearScrollAnchor(sessionId: string): void {
  const storage = getStorage()
  if (!storage || !sessionId) return
  try {
    // Nothing stored means nothing to clear. The at-bottom branch of the save
    // debounce lands here on every streaming scroll event, so without this the
    // common case is a burst of removals of a key that is already absent
    // (measured: five in 0.6s, parked at the bottom).
    if (storage.getItem(`${ANCHOR_KEY_PREFIX}${sessionId}`) === null) return
    storage.removeItem(`${ANCHOR_KEY_PREFIX}${sessionId}`)
    devLog('STORE.CLEAR', shortId(sessionId))
  } catch {
    // Best-effort — swallow.
  }
}
