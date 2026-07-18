/**
 * Per-file inline comment draft persistence. Pending (unsubmitted) comments
 * survive `MarkdownPanel` close, page refresh, and browser crashes via
 * localStorage. Thin instance of `createSlotDraftStore` (Mesh-1978); keyed by
 * filePath, capped at COMMENT_DRAFT_MAX_FILES, no TTL. Uses `evictAfterWrite` so
 * a failed persist (e.g. QuotaExceeded) never silently drops in-memory drafts.
 */
import type { InlineComment } from '../components/CommentOverlay'
import { createSlotDraftStore } from './slotDraftStore'

export const COMMENT_DRAFTS_KEY = 'mc-comment-drafts'
/** Cap stored files to prevent unbounded growth from long-term reviewers. */
export const COMMENT_DRAFT_MAX_FILES = 20

export type CommentDrafts = Record<string, InlineComment[]>

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
