/**
 * Per-slot collapsed-paste persistence. Backs the inline `[ Paste #N · M lines ]`
 * tokens in the chat input so they survive slot switches (and tab close /
 * refresh). Thin instance of `createSlotDraftStore`.
 *
 * WHY THIS EXISTS: the textarea text (incl. the paste token string) is persisted
 * per-slot by `chatDrafts`. The `PasteBlock[]` backing each token used to be
 * plain React state, cleared on slot switch, so switching away and back left the
 * token text without its block: the chip went dead and on send the literal
 * `[ Paste #N · M lines ]` string was sent instead of the content. Persisting
 * the blocks alongside the text draft keeps the two in sync.
 *
 * Storage: localStorage with the SAME 30-day TTL as `chatDrafts` (NOT
 * sessionStorage like `chatFileDrafts`). The backing text draft already survives
 * refresh; sessionStorage blocks would reproduce the dead-token bug with refresh
 * as the trigger. Paste blocks are self-contained text, so there's no
 * dangling-reference risk that would justify sessionStorage.
 *
 * SENSITIVE DATA: this stores the FULL pasted content (may contain secrets /
 * PII), not just the inert token string. The 30-day TTL eviction bounds that
 * exposure window. Pasted content shares `chatDrafts`' retention rather than
 * inventing its own; revisit retention for ALL chat-content stores together if
 * ever (note `mc-paste-store-v1`, for already-SENT content, still has no TTL).
 *
 * BYTE BUDGET: the old per-slot 512 KB hard cap is gone. The factory's
 * store-level byte-aware LRU evicts OLDEST slots until the blob fits, never the
 * newest — so a large recent paste survives whether collapsed (here) or expanded
 * (into the `chatDrafts` text draft). That symmetry is the fix for the
 * collapsed-vs-expanded asymmetry surfaced in the review.
 */
import type { PasteBlock } from './pasteTokens'
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES, DRAFT_MAX_STORE_BYTES, DRAFT_TTL_MS } from './draftConstants'

export const PASTE_DRAFTS_KEY = 'mc-chat-paste-drafts'
/** Cap stored slots (shared with text drafts). */
export const PASTE_DRAFT_MAX_ENTRIES = DRAFT_MAX_ENTRIES
/** Discard blocks not touched within this window (shared with text drafts). */
export const PASTE_DRAFT_TTL_MS = DRAFT_TTL_MS

export type PasteDrafts = Record<string, PasteBlock[]>

/** A value is a valid PasteBlock iff it carries all four fields with the right
 *  primitive types. Anything else is corruption and is dropped. */
function isPasteBlock(v: unknown): v is PasteBlock {
  if (!v || typeof v !== 'object') return false
  const b = v as Record<string, unknown>
  return typeof b.id === 'string' && typeof b.seq === 'number'
    && typeof b.lines === 'number' && typeof b.content === 'string'
}

/** Coerce a stored value into a clean PasteBlock[] deep copy (dropping invalid
 *  members), or `null` if it isn't a non-empty array of blocks. The copy
 *  isolates the store from caller mutations and vice versa. */
function sanitizeBlocks(v: unknown): PasteBlock[] | null {
  if (!Array.isArray(v)) return null
  const arr: PasteBlock[] = []
  for (const item of v) {
    if (isPasteBlock(item)) arr.push({ id: item.id, seq: item.seq, lines: item.lines, content: item.content })
  }
  return arr.length ? arr : null
}

const store = createSlotDraftStore<PasteBlock[]>({
  key: PASTE_DRAFTS_KEY,
  storage: 'local',
  ttlMs: PASTE_DRAFT_TTL_MS,
  maxEntries: PASTE_DRAFT_MAX_ENTRIES,
  maxStoreBytes: DRAFT_MAX_STORE_BYTES,
  sanitize: sanitizeBlocks,
})

export const loadPasteDrafts = store.load
export const savePasteDrafts = store.save
export const setPasteDraft = store.set
export const __resetForTests = store.__resetForTests
