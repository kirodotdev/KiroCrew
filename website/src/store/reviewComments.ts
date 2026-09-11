/**
 * Module-level store for inline review comments drafted on chat diff blocks.
 *
 * A user clicks a line number in a DiffBlock, writes a note, and the draft
 * lands here keyed by slot (chat session). ChatPage's send() drains the
 * active slot's drafts into the outgoing message; ReviewCommentBar renders
 * the pending set above the composer with per-draft removal.
 *
 * Why a module-level store and not Redux? Same trade as toolPillRegistry:
 * ephemeral UI state, no need for time-travel/devtools, and the two
 * consumers (DiffBlock, ReviewCommentBar) live in different subtrees where
 * prop-drilling through ChatPage would touch far more surface than the
 * feature warrants. `useSyncExternalStore` keeps reads tear-free.
 */
import { useSyncExternalStore } from 'react'
import { safeGetSessionItem, safeSetSessionItem } from '../utils/safeStorage'

const STORAGE_KEY = 'mc-review-comment-drafts-v1'

export interface ReviewCommentDraft {
  id: string
  /** Identity of the diff BLOCK the comment was drafted on (a content hash
   * supplied by the block). One conversation can diff the same file twice —
   * two turns iterating on it — and line N means different content in each,
   * so file+side+line alone would cross-bind the drafts. */
  blockId?: string
  /** The file entry's position in the patch's enumeration. Two entries in
   * ONE patch can share a `file` path (a crafted patch repeating the same
   * `diff --git` section), and the path alone would collapse their drafts
   * into a single anchor — the index keeps them apart. Stable within a
   * block: patch content is immutable per blockId, and streaming only
   * appends file entries. Absent on drafts persisted before this field
   * existed; identity then falls back to path-only, matching the old
   * behavior for the old data. */
  fileIndex?: number
  /** File path from the diff header, or the pathHint fallback. */
  file: string
  /** Which side of the diff the line number refers to. */
  side: 'old' | 'new'
  line: number
  /** Last line of a multi-line range (gutter drag-selection); omitted for a
   * single-line comment. Drafts stay keyed by `line` alone — one draft per
   * anchor line. */
  endLine?: number
  /** The line's text, quoted in the outgoing message so the anchor stays
   * unambiguous even if line numbers drift. */
  lineText: string
  /** The user's note. */
  text: string
  /** Last time the wording changed (epoch ms) — set on create and on every
   * edit. Restores compare it so an out-of-order pair of cancellations can
   * never resurrect older wording over newer (see restoreReviewComments). */
  updatedAt: number
}

/** One draft per anchor: same block, same file entry (index + path), same
 * side, same line. Exact `fileIndex` equality — every write site supplies
 * the entry's index, and the persistence key is introduced by this change,
 * so no stored draft without one exists to accommodate. */
function sameAnchor(a: Pick<ReviewCommentDraft, 'blockId' | 'fileIndex' | 'file' | 'side' | 'line'>, b: Pick<ReviewCommentDraft, 'blockId' | 'fileIndex' | 'file' | 'side' | 'line'>): boolean {
  return a.blockId === b.blockId && a.fileIndex === b.fileIndex && a.file === b.file && a.side === b.side && a.line === b.line
}

const EMPTY: ReviewCommentDraft[] = []
const bySlot = new Map<string, ReviewCommentDraft[]>()
const subscribers = new Set<() => void>()

let seq = 0

// Reload survival: drafts persist per tab in sessionStorage, mirroring the
// composer-draft lifecycle (unsent input outlives a refresh, not the tab).
// Best-effort on both paths via the shared safeStorage session helpers: a
// broken or full storage never breaks drafting.
function restore(): void {
  try {
    const raw = safeGetSessionItem(STORAGE_KEY)
    if (!raw) return
    for (const [slot, drafts] of Object.entries(JSON.parse(raw) as Record<string, ReviewCommentDraft[]>)) {
      if (Array.isArray(drafts) && drafts.length) {
        bySlot.set(slot, drafts)
        // Seed the ID sequence past every restored id: a fresh tab starts seq
        // at 0, so without this a new draft can mint an id a restored draft
        // already carries, and removing either would remove both.
        for (const d of drafts) {
          const n = /^rc-(\d+)$/.exec(String(d?.id ?? ''))
          if (n) seq = Math.max(seq, parseInt(n[1], 10))
        }
      }
    }
  } catch { /* corrupt or unavailable storage: start empty */ }
}
restore()

function persist(): void {
  // safeSetSessionItem carries the same per-tab rationale this store needs
  // (safeStorage.ts documents the sessionStorage mirrors explicitly); the
  // localStorage reclaim tiers simply do not engage on the session path.
  safeSetSessionItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(bySlot)))
}

function notify() { persist(); for (const fn of subscribers) fn() }

function subscribe(fn: () => void) {
  subscribers.add(fn)
  return () => { subscribers.delete(fn) }
}

export function addReviewComment(slotId: string, draft: Omit<ReviewCommentDraft, 'id' | 'updatedAt'>): void {
  const cur = bySlot.get(slotId) ?? EMPTY
  // One draft per line: adding where a draft already exists EDITS it in
  // place (same id, same position), so reopening a commented line and
  // saving never produces a duplicate. The recency stamp is the store's
  // job — set here on create AND edit, so restores can compare wording age.
  const at = cur.findIndex(d => sameAnchor(d, draft))
  const stamped = { ...draft, updatedAt: Date.now() }
  const next = at >= 0
    ? cur.map((d, i) => (i === at ? { ...d, ...stamped } : d))
    : [...cur, { ...stamped, id: `rc-${++seq}` }]
  bySlot.set(slotId, next)
  notify()
}

/** The pending draft on one line, if any — used to prefill the edit form. */
export function getReviewComment(
  slotId: string | null, blockId: string | undefined, fileIndex: number | undefined, file: string, side: 'old' | 'new', line: number,
): ReviewCommentDraft | undefined {
  if (!slotId) return undefined
  return (bySlot.get(slotId) ?? EMPTY).find(d => sameAnchor(d, { blockId, fileIndex, file, side, line }))
}

export function removeReviewComment(slotId: string, id: string): void {
  const cur = bySlot.get(slotId)
  if (!cur) return
  const next = cur.filter(d => d.id !== id)
  if (next.length === 0) bySlot.delete(slotId)
  else bySlot.set(slotId, next)
  notify()
}

export function clearReviewComments(slotId: string): void {
  if (!bySlot.delete(slotId)) return
  notify()
}

/** Put drafts back after a send failed or a queued send was cancelled.
 * MERGE BY RECENCY, never blanket-overwrite and never blanket-keep: for each
 * restored draft, the newer wording wins — a draft the user re-typed while
 * the send was in flight beats the recovered one, and a recovered draft
 * beats an OLDER live one. The second half is what makes cancellation order
 * irrelevant: cancelling send A (old wording) before send B (new wording)
 * restores A's text first, and B's restore must still land — a blanket
 * keep-what-is-there rule would discard the newest wording exactly when the
 * user cancels out of order. */
export function restoreReviewComments(slotId: string, drafts: ReviewCommentDraft[]): void {
  if (!drafts.length) return
  const cur = bySlot.get(slotId) ?? EMPTY
  let next = cur
  let changed = false
  for (const d of drafts) {
    const at = next.findIndex(c => sameAnchor(c, d))
    if (at === -1) {
      next = next === cur ? [...cur] : next
      next.push(d)
      changed = true
    } else if ((next[at].updatedAt ?? 0) < (d.updatedAt ?? 0)) {
      next = next === cur ? [...cur] : next
      next[at] = d
      changed = true
    }
  }
  if (!changed) return
  bySlot.set(slotId, next)
  notify()
}

export function peekReviewComments(slotId: string): ReviewCommentDraft[] {
  return bySlot.get(slotId) ?? EMPTY
}

/** React hook: the active slot's drafts, updating on every store change. */
export function useReviewComments(slotId: string | null): ReviewCommentDraft[] {
  return useSyncExternalStore(
    subscribe,
    () => (slotId ? bySlot.get(slotId) ?? EMPTY : EMPTY),
  )
}
