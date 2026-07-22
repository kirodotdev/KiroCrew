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
