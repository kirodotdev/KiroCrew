/**
 * Per-slot persistence of the `@rel` aliases each staged file chip was
 * recorded under (ChatPage's `pickedFileTokens`). Thin instance of
 * `createSlotDraftStore`, stored beside `chatFileDrafts` with the same
 * storage and lifetime: sessionStorage, no TTL, no cap.
 *
 * The staged files already survive a reload through `chatFileDrafts`; without
 * their aliases a restored chip could not tell its own mention from a
 * sibling's, so a hand-edit could not unstage it and a remove followed by
 * undo could not bring it back. Storing the aliases with the files closes
 * that gap for every consumer of the map at once.
 */
import { createSlotDraftStore } from './slotDraftStore'

export const FILE_TOKEN_DRAFTS_KEY = 'mc-chat-file-token-drafts'

export type FileTokenMap = Record<string, string[]>

/** Keep only `path -> ['@alias', ...]` entries with at least one string
 *  alias starting with `@`; return a fresh copy, or null when nothing is left. */
const sanitizeTokens = (v: unknown): FileTokenMap | null => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const out: FileTokenMap = {}
  for (const [path, aliases] of Object.entries(v as Record<string, unknown>)) {
    if (!Array.isArray(aliases)) continue
    const kept = aliases.filter((a): a is string => typeof a === 'string' && a.startsWith('@') && a.length > 1)
    if (kept.length) out[path] = kept.slice()
  }
  return Object.keys(out).length ? out : null
}

const store = createSlotDraftStore<FileTokenMap>({
  key: FILE_TOKEN_DRAFTS_KEY,
  storage: 'session',
  sanitize: sanitizeTokens,
})

export const loadFileTokenDrafts = store.load
export const saveFileTokenDrafts = store.save
