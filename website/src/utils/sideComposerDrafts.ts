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

/** One composer's draft: `s` names its slot, `t` the write stamp. */
type SideDraft = { s: string; t: number }

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
    const obj = parsed as { s?: unknown; t?: unknown }
    if (typeof obj.s !== 'string' || typeof obj.t !== 'number') return null
    return { s: obj.s, t: obj.t }
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
  const draft: SideDraft = { s: slot, t: Date.now() }
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
 * PRESENCE, not text. The close guard is the only reader and asks a yes/no question, and
 * nothing restores a side draft into a composer — so the text was rewritten on every
 * keystroke, kept for its TTL, and never read. Storing the fact answers the same question
 * without holding the user's prose. The emptiness rule now lives only at the write, so a
 * blank entry from an older build reads as present: over-reporting costs a dismissible
 * confirm, under-reporting costs the draft.
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

/** @internal test-only: drop every side draft. */
export function __resetForTests(): void {
  try {
    for (const key of draftKeys()) localStorage.removeItem(key)
  } catch {
    /* storage unavailable — nothing to reset */
  }
}
