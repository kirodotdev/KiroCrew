import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readPaneDraft, writePaneDraft, takePaneDraft, mergePaneDraft, subscribePaneDraft, PANE_DRAFTS_KEY, LEGACY_PANE_FILE_DRAFTS_KEY, PANE_DRAFTS_MAX_BYTES, __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import { carryPastes, mergeCarriedDraft, type PasteBlock } from '../utils/pasteTokens'

/* The pane's parked drafts must survive the storage layer refusing a write:
 * a quota that ChatPage's own 2 MiB stores may already have filled, or a
 * browser with storage disabled. The in-memory mirror is what hands the draft
 * back in that case, for as long as the tab lives. */

const block = (seq: number, content: string): PasteBlock => ({ id: `p${seq}-${content.replace(/\n/g, '_')}`, seq, lines: content.split('\n').length, content })

describe('chatPaneDrafts', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('parks and takes per slot; take clears the entry', () => {
    writePaneDraft('a', { text: 'for a', files: ['/tmp/a.png'], pastes: [] })
    expect(readPaneDraft('a')).toEqual({ text: 'for a', files: ['/tmp/a.png'], pastes: [] })
    expect(takePaneDraft('a')).toEqual({ text: 'for a', files: ['/tmp/a.png'], pastes: [] })
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
  })

  it('lives in sessionStorage, not the localStorage quota ChatPage shares', () => {
    writePaneDraft('a', { text: 'for a', files: [], pastes: [] })
    expect(sessionStorage.getItem(PANE_DRAFTS_KEY)).toContain('for a')
    expect(localStorage.getItem(PANE_DRAFTS_KEY)).toBeNull()
  })

  it('parks text, files and paste blocks as ONE stored value and takes them back together', () => {
    const b = block(1, 'l1\nl2\nl3')
    writePaneDraft('a', { text: '[ Paste #1 · 3 lines ]', files: [], pastes: [b] })
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string; pastes: PasteBlock[] }>
    expect(stored.a.text).toBe('[ Paste #1 · 3 lines ]')
    expect(stored.a.pastes).toEqual([b])
    expect(takePaneDraft('a')).toEqual({ text: '[ Paste #1 · 3 lines ]', files: [], pastes: [b] })
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
  })

  it('evicts an over-budget slot WHOLE from storage: never a token without its block', () => {
    // Two parked slots; the second is big enough to push the blob past the cap.
    // The store evicts the OLDEST slot entirely, so what a reload (mirror gone)
    // finds in storage is slot a either whole or absent — the token can never
    // come back alone. The newest slot is never the casualty.
    const small = block(1, 'a\nb\nc')
    writePaneDraft('a', { text: 'keep [ Paste #1 · 3 lines ]', files: [], pastes: [small] })
    const huge = block(1, 'x'.repeat(PANE_DRAFTS_MAX_BYTES))
    writePaneDraft('b', { text: '[ Paste #1 · 1 lines ]', files: [], pastes: [huge] })
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string; pastes: PasteBlock[] }>
    expect(stored.a).toBeUndefined()
    expect(stored.b.text).toBe('[ Paste #1 · 1 lines ]')
    expect(stored.b.pastes).toEqual([huge])
    // The tab that parked it still has slot a whole, from the mirror.
    expect(readPaneDraft('a')).toEqual({ text: 'keep [ Paste #1 · 3 lines ]', files: [], pastes: [small] })
  })

  it('folds drafts parked by the two-store layout into whole drafts on first read', () => {
    // A tab that parked under the old layout (a text string per slot, paths
    // under their own key) and then reloaded into this code gets its drafts
    // back; a slot that already has a whole draft keeps it.
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 'text only from an older build', b: { text: 'fine', files: [], pastes: [] }, c: 'stale for c' }))
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, JSON.stringify({ a: ['/tmp/a.png'], d: ['/tmp/d.pdf'], c: 'not a list' }))
    expect(readPaneDraft('a')).toEqual({ text: 'text only from an older build', files: ['/tmp/a.png'], pastes: [] })
    expect(readPaneDraft('b')).toEqual({ text: 'fine', files: [], pastes: [] })
    expect(readPaneDraft('c')).toEqual({ text: 'stale for c', files: [], pastes: [] })
    expect(readPaneDraft('d')).toEqual({ text: '', files: ['/tmp/d.pdf'], pastes: [] })
    // Folded once: the legacy key is gone and the unified blob holds the drafts.
    expect(sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)).toBeNull()
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string }>
    expect(stored.a.text).toBe('text only from an older build')
    expect(stored.d).toBeTruthy()
  })

  it('keeps the legacy path copy when the unified write is refused, and still hands the draft back', () => {
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 'old text' }))
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, JSON.stringify({ a: ['/tmp/a.png'] }))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    // This tab gets the folded draft from the mirror…
    expect(readPaneDraft('a')).toEqual({ text: 'old text', files: ['/tmp/a.png'], pastes: [] })
    // …and the only on-disk copy of the paths is untouched for the next load.
    expect(sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)).toBe(JSON.stringify({ a: ['/tmp/a.png'] }))
    expect(sessionStorage.getItem(PANE_DRAFTS_KEY)).toBe(JSON.stringify({ a: 'old text' }))
  })

  it('retries a refused fold: the next write that lands carries the legacy drafts, then the legacy key goes', () => {
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 'old text' }))
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, JSON.stringify({ a: ['/tmp/a.png'] }))
    const refuse = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    expect(readPaneDraft('a').text).toBe('old text')
    // Storage recovers; a park for ANOTHER slot must not replace the legacy
    // text blob with a unified one that lacks slot a.
    refuse.mockRestore()
    writePaneDraft('b', { text: 'for b', files: [], pastes: [] })
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string; files: string[] }>
    expect(stored.a).toEqual({ text: 'old text', files: ['/tmp/a.png'], pastes: [] })
    expect(stored.b.text).toBe('for b')
    // The fold is now complete on disk: the next access retires the legacy key.
    readPaneDraft('b')
    expect(sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)).toBeNull()
    // A cold read (mirror gone) gets slot a whole.
    __resetPaneDraftsForTests.call(null)
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify(stored))
    expect(readPaneDraft('a')).toEqual({ text: 'old text', files: ['/tmp/a.png'], pastes: [] })
  })

  it('a malformed legacy path blob costs only the paths, never the text drafts beside it', () => {
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 'old text' }))
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, '{not json')
    expect(readPaneDraft('a')).toEqual({ text: 'old text', files: [], pastes: [] })
    expect(sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)).toBeNull()
    // And the other way round: an unreadable text blob still yields the paths.
    __resetPaneDraftsForTests.call(null)
    sessionStorage.setItem(PANE_DRAFTS_KEY, '[oops')
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, JSON.stringify({ b: ['/tmp/b.pdf'] }))
    expect(readPaneDraft('b')).toEqual({ text: '', files: ['/tmp/b.pdf'], pastes: [] })
  })

  it('a taken legacy draft stays consumed while the unified write keeps being refused', () => {
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 'old text' }))
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, JSON.stringify({ a: ['/tmp/a.png'] }))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    // The pane shows slot a: take consumes the parked draft.
    expect(takePaneDraft('a')).toEqual({ text: 'old text', files: ['/tmp/a.png'], pastes: [] })
    // The legacy blob is still on disk (nothing could be written), but a re-fold
    // must not resurrect what the tab has consumed.
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
    writePaneDraft('b', { text: 'for b', files: [], pastes: [] })
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
    expect(readPaneDraft('b').text).toBe('for b')
  })

  it('never retires the legacy path blob on a refused write, even once every legacy slot was parked over', () => {
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 'old text' }))
    sessionStorage.setItem(LEGACY_PANE_FILE_DRAFTS_KEY, JSON.stringify({ a: ['/tmp/a.png'] }))
    const refuse = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    // The pane parks a NEW draft over slot a while storage refuses: the fold now
    // has nothing left to add for a, but nothing unified is on disk either.
    writePaneDraft('a', { text: 'newer text', files: ['/tmp/new.pdf'], pastes: [] })
    readPaneDraft('b')
    expect(sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)).toBe(JSON.stringify({ a: ['/tmp/a.png'] }))
    // Storage recovers: the first access writes the tab's drafts as one unified
    // blob, and only then does the legacy path blob go.
    refuse.mockRestore()
    expect(readPaneDraft('a')).toEqual({ text: 'newer text', files: ['/tmp/new.pdf'], pastes: [] })
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string; files: string[] }>
    expect(stored.a).toEqual({ text: 'newer text', files: ['/tmp/new.pdf'], pastes: [] })
    expect(sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)).toBeNull()
  })

  it('a later write carries the NEWER mirrored draft over a stale stored one, and a clear stays cleared', () => {
    writePaneDraft('a', { text: 'first', files: [], pastes: [] })
    writePaneDraft('c', { text: 'to be taken', files: [], pastes: [] })
    // Storage refuses: the newer draft for a and the take of c exist only in memory.
    const refuse = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    writePaneDraft('a', { text: 'first\n\nrecovered', files: [], pastes: [] })
    expect(takePaneDraft('c').text).toBe('to be taken')
    refuse.mockRestore()
    // A write for an unrelated slot lands: the blob must carry a's newer text
    // and not resurrect c.
    writePaneDraft('b', { text: 'for b', files: [], pastes: [] })
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string }>
    expect(stored.a.text).toBe('first\n\nrecovered')
    expect(stored.c).toBeUndefined()
    expect(stored.b.text).toBe('for b')
  })

  it('a slot written again is the newest for eviction, not stuck at its first position', () => {
    writePaneDraft('a', { text: 'a v1', files: [], pastes: [] })
    writePaneDraft('b', { text: 'b'.repeat(Math.floor(PANE_DRAFTS_MAX_BYTES * 0.4)), files: [], pastes: [] })
    // A late recovery updates a: it is now the most recent draft.
    writePaneDraft('a', { text: 'a v2 recovered', files: [], pastes: [] })
    // A park for c that pushes the blob over budget must evict the OLDEST (b),
    // never the updated a.
    writePaneDraft('c', { text: 'c', files: [], pastes: [{ id: 'big', seq: 1, lines: 1, content: 'x'.repeat(Math.floor(PANE_DRAFTS_MAX_BYTES * 0.75)) }] })
    const stored = JSON.parse(sessionStorage.getItem(PANE_DRAFTS_KEY) ?? '{}') as Record<string, { text: string }>
    expect(stored.a?.text).toBe('a v2 recovered')
    expect(stored.b).toBeUndefined()
    expect(stored.c?.text).toBe('c')
  })

  it('drops a stored value that is neither a whole draft nor a legacy string', () => {
    sessionStorage.setItem(PANE_DRAFTS_KEY, JSON.stringify({ a: 42, b: { text: 7 }, c: { text: 'fine', files: 'nope', pastes: [{ id: 'x' }] } }))
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
    expect(readPaneDraft('b')).toEqual({ text: '', files: [], pastes: [] })
    // Malformed members are dropped, the rest of the draft kept.
    expect(readPaneDraft('c')).toEqual({ text: 'fine', files: [], pastes: [] })
  })

  it('hands the draft back from the mirror when storage refuses the write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    const b = block(1, 'x\ny\nz')
    writePaneDraft('quota', { text: 'kept despite quota [ Paste #1 · 3 lines ]', files: ['/tmp/q.png'], pastes: [b] })
    expect(readPaneDraft('quota')).toEqual({ text: 'kept despite quota [ Paste #1 · 3 lines ]', files: ['/tmp/q.png'], pastes: [b] })
    // A merge on top of a refused write still accumulates.
    mergePaneDraft('quota', 'and this', [])
    expect(readPaneDraft('quota').text).toContain('kept despite quota')
    expect(readPaneDraft('quota').text).toContain('and this')
    expect(readPaneDraft('quota').pastes).toEqual([b])
  })

  it('notifies a subscriber of the slot when a late merge lands, and not other slots', () => {
    const onA = vi.fn(); const onB = vi.fn()
    const offA = subscribePaneDraft('a', onA); const offB = subscribePaneDraft('b', onB)
    mergePaneDraft('a', 'late for a', [])
    expect(onA).toHaveBeenCalledTimes(1)
    expect(onB).not.toHaveBeenCalled()
    offA(); offB()
    mergePaneDraft('a', 'after unsubscribe', [])
    expect(onA).toHaveBeenCalledTimes(1)
  })

  it('a late merge re-numbers a carried block that collides with a parked one', () => {
    const parked = block(1, 'parked\ncontent\nhere')
    writePaneDraft('a', { text: '[ Paste #1 · 3 lines ]', files: [], pastes: [parked] })
    const carried = block(1, 'late\ncontent\ntoo')
    mergePaneDraft('a', '[ Paste #1 · 3 lines ]', [], [carried])
    const merged = readPaneDraft('a')
    // Both tokens survive, and the carried one now points at seq 2.
    expect(merged.text).toContain('[ Paste #1 · 3 lines ]')
    expect(merged.text).toContain('[ Paste #2 · 3 lines ]')
    expect(merged.pastes).toEqual([parked, { ...carried, seq: 2 }])
  })
})

describe('carryPastes', () => {
  it('passes the kept blocks through untouched when nothing is carried', () => {
    const kept = [block(1, 'a\nb\nc')]
    expect(carryPastes('typed', [], kept)).toEqual({ text: 'typed', full: 'typed', pastes: kept })
  })

  it('mergeCarriedDraft: a payload the composer already holds whole (undo) is not appended, tokens and typed words alike', () => {
    const kept = [block(1, 'k\nk\nk')]
    const composer = 'why does this fail? \n[ Paste #1 · 3 lines ]'
    const carried = carryPastes(composer, kept, kept)
    // The stripped text alone would never equal the composer; `full` does.
    expect(carried.text).toBe('why does this fail? \n')
    expect(carried.full).toBe(composer)
    expect(mergeCarriedDraft(composer, carried)).toBe(composer)
    // A composer that has moved on keeps its token once and gets only the words.
    expect(mergeCarriedDraft(composer + ' and more', carried)).toBe(composer + ' and more\n\nwhy does this fail? \n')
  })

  it('drops the token of a block the composer already holds, so a retry cannot send the paste twice', () => {
    // The composer already shows block k's token (an undo put it back); the
    // refused payload carries the same block: its token leaves the payload,
    // the typed words around it stay, and the block list is unchanged.
    const kept = [block(1, 'k\nk\nk')]
    const { text, full, pastes } = carryPastes('why?\n[ Paste #1 · 3 lines ]\nthanks', kept, kept)
    expect(text).toBe('why?\nthanks')
    expect(full).toBe('why?\n[ Paste #1 · 3 lines ]\nthanks')
    expect(pastes).toEqual(kept)
  })

  it('keeps a carried seq that is free, re-numbers one that is taken, and never adds a block twice', () => {
    const kept = [block(1, 'k\nk\nk')]
    const free = block(3, 'f\nf\nf')
    const taken = block(1, 't\nt\nt')
    const { text, pastes } = carryPastes('[ Paste #3 · 3 lines ] [ Paste #1 · 3 lines ]', [free, taken, kept[0]], kept)
    expect(pastes).toEqual([kept[0], free, { ...taken, seq: 2 }])
    expect(text).toBe('[ Paste #3 · 3 lines ] [ Paste #2 · 3 lines ]')
  })
})
