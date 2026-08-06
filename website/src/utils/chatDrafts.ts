/**
 * Per-slot chat draft persistence. Drafts survive tab close, refresh, and
 * browser crashes via localStorage. Thin instance of `createSlotDraftStore`
 *; all behavior (TTL, LRU, byte-aware eviction, corruption guards,
 * quota-safe write order) lives in the factory.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES, DRAFT_MAX_STORE_BYTES, DRAFT_TTL_MS, DRAFT_SAVE_DEBOUNCE_MS } from './draftConstants'

export { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS, DRAFT_SAVE_DEBOUNCE_MS }
export const DRAFTS_KEY = 'mc-chat-drafts'

/**
 * Merge a handed-off prompt into whatever the composer already holds.
 *
 * Every path that seeds the composer via `setPendingInput` goes through the same
 * consumer, and that consumer REPLACES the draft and persists the replacement —
 * so a plain set silently destroys unsent text the user was mid-way through
 * typing, unrecoverably. Both hand-off paths (a follow-up card's "add to this
 * session", and the error → agent hand-off) therefore append instead.
 *
 * One implementation on purpose: this was duplicated at the two call sites, which
 * is how the two behaviours drift.
 */
// A blank line separates the two, because a handed-off prompt is multi-line
// prose, not a word to concatenate. Built by concatenation rather than a
// template literal so the only literal here is this separator — punctuation, not
// user-visible copy.
const PARAGRAPH_BREAK = '\n\n'

export function mergeIntoDraft(draft: string | null | undefined, prompt: string): string {
  const existing = draft ?? ''
  if (!existing.trim()) return prompt
  return existing.replace(/\s+$/, '') + PARAGRAPH_BREAK + prompt
}

export type Drafts = Record<string, string>

const isNonEmptyString = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

const store = createSlotDraftStore<string>({
  key: DRAFTS_KEY,
  storage: 'local',
  ttlMs: DRAFT_TTL_MS,
  maxEntries: DRAFT_MAX_ENTRIES,
  maxStoreBytes: DRAFT_MAX_STORE_BYTES,
  sanitize: isNonEmptyString,
})

export const loadDrafts = store.load
export const saveDrafts = store.save
export const setDraft = store.set
export const __resetForTests = store.__resetForTests
