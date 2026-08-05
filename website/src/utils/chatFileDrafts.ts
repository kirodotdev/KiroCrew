/**
 * Per-slot pending-file-attachment persistence (paths staged in the compose box
 * before send). Thin instance of `createSlotDraftStore`.
 *
 * Storage differs from `chatDrafts` intentionally: sessionStorage, not
 * localStorage. Attachment paths reference uploaded files that may be
 * garbage-collected server-side after the session ends; persisting them across
 * tab close would leave dangling references in the UI. No TTL / LRU cap either:
 * file-path arrays are tiny and session-scoped.
 */
import { createSlotDraftStore } from './slotDraftStore'

export const FILE_DRAFTS_KEY = 'mc-chat-file-drafts'

export type FileDrafts = Record<string, string[]>

/** Coerce to a non-empty string[] (dropping non-string members), or null. The
 *  returned copy isolates the store from caller mutations and vice versa. */
const sanitizePaths = (v: unknown): string[] | null => {
  if (!Array.isArray(v)) return null
  const arr = v.filter((x): x is string => typeof x === 'string')
  return arr.length ? arr.slice() : null
}

const store = createSlotDraftStore<string[]>({
  key: FILE_DRAFTS_KEY,
  storage: 'session',
  sanitize: sanitizePaths,
})

export const loadFileDrafts = store.load
export const saveFileDrafts = store.save
export const setFileDraft = store.set
