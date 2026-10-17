import { describe, it, expect, beforeEach } from 'vitest'
import {
  COMMENT_DRAFTS_KEY,
  COMMENT_DRAFT_MAX_FILES,
  loadCommentDrafts,
  saveCommentDrafts,
  setCommentsForFile,
} from '../utils/commentDrafts'
import type { InlineComment } from '../components/CommentOverlay'

// Instance-specific behavior only. The generic load/sanitize/cap/persist and the
// evictAfterWrite branch are covered by slotDraftStore.test.ts; these tests pin
// how commentDrafts configures the factory: the InlineComment shape guard, the
// 20-file cap, the exact storage key, and evictAfterWrite=true.

const mk = (id: string, anchor: string, text: string): InlineComment => ({ id, anchor, text })

describe('commentDrafts', () => {
  beforeEach(() => { localStorage.clear() })

  it('roundtrips InlineComment drafts through the comment-drafts key', () => {
    const drafts = {
      '/a/README.md': [mk('1', 'hello', 'fix typo')],
      '/b/notes.md': [mk('2', 'world', 'add context'), mk('3', 'foo', 'rename')],
    }
    saveCommentDrafts(drafts)
    expect(loadCommentDrafts()).toEqual(drafts)
    expect(localStorage.getItem(COMMENT_DRAFTS_KEY)).toBe(JSON.stringify(drafts))
  })

  it('drops entries with the wrong shape (isValidComments guard)', () => {
    localStorage.setItem(COMMENT_DRAFTS_KEY, JSON.stringify({
      '/good.md': [mk('1', 'a', 'b')],
      '/bad-not-array.md': 'oops',
      '/bad-empty.md': [],
      '/bad-missing-fields.md': [{ id: 'x' }],
    }))
    expect(loadCommentDrafts()).toEqual({ '/good.md': [mk('1', 'a', 'b')] })
  })

  it('preserves optional comment fields (line/column/startOffset) across a roundtrip', () => {
    const rich: InlineComment = { id: '1', anchor: 'a', text: 't', line: 4, column: 2, startOffset: 87 }
    saveCommentDrafts({ '/a.md': [rich] })
    expect(loadCommentDrafts()['/a.md'][0]).toEqual(rich)
  })

  it('evicts oldest files past COMMENT_DRAFT_MAX_FILES, syncing back to the caller', () => {
    const drafts: Record<string, InlineComment[]> = {}
    for (let i = 0; i < COMMENT_DRAFT_MAX_FILES + 5; i++) {
      drafts[`/f${i}.md`] = [mk(`id${i}`, 'a', `c${i}`)]
    }
    saveCommentDrafts(drafts)
    expect(Object.keys(drafts).length).toBe(COMMENT_DRAFT_MAX_FILES)
    expect(drafts['/f0.md']).toBeUndefined()
    expect(drafts['/f5.md']).toBeDefined()
    expect(Object.keys(loadCommentDrafts()).length).toBe(COMMENT_DRAFT_MAX_FILES)
  })

  it('does not evict the caller when a persist throws (evictAfterWrite=true, no silent loss)', () => {
    const drafts: Record<string, InlineComment[]> = {}
    for (let i = 0; i < COMMENT_DRAFT_MAX_FILES + 5; i++) {
      drafts[`/f${i}.md`] = [mk(`id${i}`, 'a', `c${i}`)]
    }
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try { saveCommentDrafts(drafts) } finally { Storage.prototype.setItem = orig }
    // Persist failed, so no eviction is mirrored back — caller retains every file.
    expect(Object.keys(drafts).length).toBe(COMMENT_DRAFT_MAX_FILES + 5)
    expect(drafts['/f0.md']).toBeDefined()
  })

  it('submitting a file (setCommentsForFile with []) does not resurrect its draft on reload', () => {
    saveCommentDrafts({ '/a.md': [mk('1', 'x', 'y')] })
    const drafts = loadCommentDrafts()
    setCommentsForFile(drafts, '/a.md', [])
    saveCommentDrafts(drafts)
    expect(loadCommentDrafts()).toEqual({})
  })
})
