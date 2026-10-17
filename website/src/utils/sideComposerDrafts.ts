/**
 * Per-COMPOSER draft persistence for the SIDE and EMBEDDED composers.
 *
 * One key per composer, not one blob holding every slot. A shared blob makes each write a
 * read-modify-write over entries this window does not own, so a popout persisting a DIFFERENT
 * slot between the read and the write is erased — and the close guard then reads that slot
 * clean and deletes it with the draft unsent. `slotDraftStore` states the limitation in its
 * own header: it overwrites the whole key, is last-write-wins across tabs, and accepts that
 * "because the dashboard is effectively single-tab". Cross-window guarding is precisely what
 * stops that being true, so this store cannot inherit the assumption.
 *
 * The generic store's other policies came with the shared view: `maxEntries` and
 * `maxStoreBytes` order evictions across slots, which no per-key layout can see. Its TTL
 * carries over unchanged, and pruning on read reclaims what a crashed window left behind.
 */
import { DRAFT_TTL_MS } from './draftConstants'
import { safeSetItem } from './safeStorage'

export const SIDE_DRAFT_KEY_PREFIX = 'mc-side-draft:'

/** One composer's draft: `s` names its slot, `t` the write stamp, `x` the text. */
type SideDraft = { s: string; t: number; x: string }

function draftKey(composerId: string): string {
  return `${SIDE_DRAFT_KEY_PREFIX}${composerId}`
}

/** Every draft key present, collected before any removal shifts the indices. */
function draftKeys(): string[] {
  const keys: string[] = []
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i)
    if (key && key.startsWith(SIDE_DRAFT_KEY_PREFIX)) keys.push(key)
  }
  return keys
}

function parseDraft(raw: string | null): SideDraft | null {
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const obj = parsed as { s?: unknown; t?: unknown; x?: unknown }
    if (typeof obj.s !== 'string' || typeof obj.t !== 'number') return null
    // An entry from the presence-only build has no `x`, so it still reports PRESENT and
    // the guard keeps working; it simply has no prose to give back.
    return { s: obj.s, t: obj.t, x: typeof obj.x === 'string' ? obj.x : '' }
  } catch {
    return null
  }
}

/**
 * Persist THIS composer's draft against *slot*, writing only the key it owns.
 *
 * Returns whether storage now HOLDS the current text. A nearly-full or disabled store fails
 * the write, and reporting that as durable is what hands the claim the short TTL while no
 * recoverable copy exists — so the result is the caller's evidence, not a courtesy.
 */
export function writeSideDraft(composerId: string, slot: string, text: string): boolean {
  if (text.trim().length === 0) {
    clearSideDraft(composerId)
    // Nothing to lose: storage agrees with the composer.
    return true
  }
  const draft: SideDraft = { s: slot, t: Date.now(), x: text }
  return safeSetItem(draftKey(composerId), JSON.stringify(draft))
}

/** Drop THIS composer's draft, leaving every other composer's entry untouched. */
export function clearSideDraft(composerId: string): void {
  try {
    localStorage.removeItem(draftKey(composerId))
  } catch {
    /* storage unavailable — nothing to clear */
  }
}

/**
 * Which composers hold a draft, as `Record<slot, composerId[]>`.
 *
 * PRESENCE for the close guard, which asks a yes/no question. The prose is read back by
 * `readSideDraftForSlot`: a record proving a draft existed but unable to return it is no
 * recovery path, and the composer holding the only other copy is the one a close unmounts.
 * The emptiness rule lives at the write, so a blank entry from an older build reads as
 * present: over-reporting costs a dismissible confirm, under-reporting costs the draft.
 *
 * Expired entries are RECLAIMED here, not merely skipped: nothing refreshes a draft in place,
 * so past the TTL its window is long gone and the entry can never become live again. That is
 * what the shared store did on load, and it bounds what a crashed window leaves behind.
 */
export function loadSideDrafts(): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  const cutoff = Date.now() - DRAFT_TTL_MS
  try {
    for (const key of draftKeys()) {
      const draft = parseDraft(localStorage.getItem(key))
      if (!draft || draft.t < cutoff) {
        localStorage.removeItem(key)
        continue
      }
      const composerId = key.slice(SIDE_DRAFT_KEY_PREFIX.length)
      out[draft.s] = [...(out[draft.s] ?? []), composerId]
    }
  } catch {
    return out
  }
  return out
}

/**
 * The newest live draft text for *slot*, or `null`.
 *
 * Keyed by SLOT, not composer: an id is minted per mount, so a composer torn down by a
 * close cannot ask for its own key back. Newest wins when two panes both left one.
 */
export function readSideDraftForSlot(slot: string): string | null {
  const cutoff = Date.now() - DRAFT_TTL_MS
  let best: SideDraft | null = null
  try {
    for (const key of draftKeys()) {
      const draft = parseDraft(localStorage.getItem(key))
      if (!draft || draft.s !== slot || draft.t < cutoff || draft.x === '') continue
      if (best === null || draft.t > best.t) best = draft
    }
  } catch {
    return null
  }
  return best === null ? null : best.x
}


/** @internal test-only: drop every side draft. */
export function __resetForTests(): void {
  try {
    for (const key of draftKeys()) localStorage.removeItem(key)
  } catch {
    /* storage unavailable — nothing to reset */
  }
}
