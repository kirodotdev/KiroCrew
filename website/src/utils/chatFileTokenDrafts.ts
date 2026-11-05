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

/**
 * Reserved bucket key for the slotless (new-chat) composer, which has no real
 * slot yet (`composerSlotRef.current` is null). A real slot key is
 * `chat-<counter>-<timestamp>`, so this identifier (no `chat-` prefix, no
 * hyphen) can never collide with one. The alias map uses it as the fallback
 * bucket when there is no slot (see `useFileMentionTokens`), which is what gives
 * the new-chat composer the same remove-strips-the-mention bookkeeping every
 * real slot has.
 *
 * It is deliberately NOT persisted: the no-slot text and file drafts are not
 * either (the send-clear and slot-restore only ever key on a real slot), so an
 * abandoned new-chat draft's aliases must not survive a reload and silently
 * revive a file later. `loadFileTokenDrafts`/`saveFileTokenDrafts` drop it.
 *
 * The value is a camelCase identifier, not prose: it is a bookkeeping map key,
 * never rendered, so the i18n gate's all-caps-const inspector reads it as an
 * identifier rather than user-visible copy.
 */
export const NO_SLOT_TOKEN_KEY = 'noSlotComposerBucket'

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

/** A fresh copy of `map` without the reserved no-slot bucket. The no-slot
 *  composer's aliases live only in memory, so neither a load nor a save ever
 *  touches persisted storage with them. */
const withoutNoSlot = (map: Record<string, FileTokenMap>): Record<string, FileTokenMap> => {
  if (!(NO_SLOT_TOKEN_KEY in map)) return map
  const { [NO_SLOT_TOKEN_KEY]: _dropped, ...rest } = map
  return rest
}

export const loadFileTokenDrafts = (): Record<string, FileTokenMap> => withoutNoSlot(store.load())
export const saveFileTokenDrafts = (map: Record<string, FileTokenMap>): boolean => store.save(withoutNoSlot(map))
