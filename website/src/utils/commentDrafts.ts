/**
 * Per-file inline comment draft persistence. Pending (unsubmitted) comments
 * survive `MarkdownPanel` close, page refresh, and browser crashes via
 * localStorage. Thin instance of `createSlotDraftStore`; keyed by
 * filePath, capped at COMMENT_DRAFT_MAX_FILES, no TTL. Uses `evictAfterWrite` so
 * a failed persist (e.g. QuotaExceeded) never silently drops in-memory drafts.
 */
import type { InlineComment } from '../components/CommentOverlay'
import { createSlotDraftStore } from './slotDraftStore'

export const COMMENT_DRAFTS_KEY = 'mc-comment-drafts'
/** Cap stored files to prevent unbounded growth from long-term reviewers. */
export const COMMENT_DRAFT_MAX_FILES = 20

/** Accept only non-empty arrays of comments carrying the required string keys;
 *  return a deep copy isolating the store from caller mutations, or null to
 *  drop. The per-comment spread is a full copy ONLY because every InlineComment
 *  field is a primitive; adding a nested object/array field would silently make
 *  this a shallow copy (no compile error) and must switch to a structured clone. */
function isValidComments(v: unknown): InlineComment[] | null {
  if (!Array.isArray(v) || v.length === 0) return null
  const ok = v.every(c => c && typeof c === 'object'
    && typeof (c as InlineComment).id === 'string'
    && typeof (c as InlineComment).anchor === 'string'
    && typeof (c as InlineComment).text === 'string')
  return ok ? v.map(c => ({ ...(c as InlineComment) })) : null
}

const store = createSlotDraftStore<InlineComment[]>({
  key: COMMENT_DRAFTS_KEY,
  storage: 'local',
  maxEntries: COMMENT_DRAFT_MAX_FILES,
  evictAfterWrite: true,
  sanitize: isValidComments,
})

export const loadCommentDrafts = store.load
export const saveCommentDrafts = store.save
/** Set (or delete if empty) the comments for a file path. */
export const setCommentsForFile = store.set
/** Save one file's comments without touching any other file in storage.
 *  Several panels hold their own copy of the store, so a whole-copy save
 *  would overwrite what another panel saved since this one loaded. This
 *  re-reads storage, sets only `filePath`, and saves that; `local` (the
 *  caller's copy) gets the same entry so it never goes stale for that file
 *  and keeps every entry a failed write left only in memory. */
export function persistFileComments(local: Record<string, InlineComment[]>, filePath: string, comments: InlineComment[]): void {
  store.set(local, filePath, comments)
  const fresh = store.load()
  store.set(fresh, filePath, comments)
  store.save(fresh)
}

/** Remove delivered comments for `filePath` from both storage and the
 *  caller's copy, each filtered on its own. Storage may hold a newer version
 *  of the file (another panel instance reopened it and saved edits), so it is
 *  filtered as stored rather than overwritten from `local`; `local` keeps any
 *  entry a failed write left only in memory. When storage has no entry for the
 *  file, the filtered local entry is saved. */
export function dropDeliveredComments(local: Record<string, InlineComment[]>, filePath: string, delivered: (c: InlineComment) => boolean): void {
  const mine = (local[filePath] ?? []).filter(c => !delivered(c))
  store.set(local, filePath, mine)
  const fresh = store.load()
  const stored = fresh[filePath]
  store.set(fresh, filePath, stored ? stored.filter(c => !delivered(c)) : mine)
  store.save(fresh)
}
