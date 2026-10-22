/**
 * Per-file inline comment draft persistence. Pending (unsubmitted) comments
 * survive `MarkdownPanel` close, page refresh, and browser crashes via
 * localStorage. Thin instance of `createSlotDraftStore`; keyed by
 * filePath, capped at COMMENT_DRAFT_MAX_FILES, no TTL. Uses `evictAfterWrite` so
 * a failed persist (e.g. QuotaExceeded) never silently drops in-memory drafts.
 */
import type { InlineComment, InlineCommentImage } from '../components/CommentOverlay'
import { createSlotDraftStore } from './slotDraftStore'

export const COMMENT_DRAFTS_KEY = 'mc-comment-drafts'
/** Cap stored files to prevent unbounded growth from long-term reviewers. */
export const COMMENT_DRAFT_MAX_FILES = 20

/** Accept only non-empty arrays of comments carrying the required string keys;
 *  return a deep copy isolating the store from caller mutations, or null to
 *  drop. Every InlineComment field is a primitive except `image`, which is
 *  copied on its own; adding another nested field would silently make this a
 *  shallow copy (no compile error) and must copy that field too. A malformed
 *  `image` drops the batch like any other malformed comment, because a box
 *  that is not inside its image cannot be drawn or reported. */
function isValidImage(v: unknown): boolean {
  if (v === undefined) return true
  if (!v || typeof v !== 'object') return false
  const img = v as InlineCommentImage
  const inUnit = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 1
  // A box has area and ends inside the image. The slack absorbs float noise
  // from a box drawn to the image's far edge (0.7 + 0.3 = 1.0000000000000002).
  const boxOk = inUnit(img.x) && inUnit(img.y) && inUnit(img.w) && inUnit(img.h)
    && img.w > 0 && img.h > 0 && img.x + img.w <= 1 + 1e-9 && img.y + img.h <= 1 + 1e-9
  // Which use of the file the box is on: both or neither, and the use exists.
  const count = (n: unknown): n is number => typeof n === 'number' && Number.isInteger(n) && n >= 0
  const useOk = (img.nth === undefined && img.uses === undefined)
    || (count(img.nth) && count(img.uses) && img.nth < img.uses)
  return typeof img.src === 'string' && (img.path === undefined || typeof img.path === 'string') && boxOk && useOk
}

function isValidComments(v: unknown): InlineComment[] | null {
  if (!Array.isArray(v) || v.length === 0) return null
  const ok = v.every(c => c && typeof c === 'object'
    && typeof (c as InlineComment).id === 'string'
    && typeof (c as InlineComment).anchor === 'string'
    && typeof (c as InlineComment).text === 'string'
    && isValidImage((c as InlineComment).image))
  return ok
    ? v.map(c => {
      const copy = { ...(c as InlineComment) }
      if (copy.image) copy.image = { ...copy.image }
      return copy
    })
    : null
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
