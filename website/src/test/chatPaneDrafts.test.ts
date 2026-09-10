import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readPaneDraft, writePaneDraft, takePaneDraft, mergePaneDraft, subscribePaneDraft, PANE_DRAFTS_KEY, __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* The pane's parked drafts must survive the storage layer refusing a write:
 * a quota that ChatPage's own 2 MiB stores may already have filled, or a
 * browser with storage disabled. The in-memory mirror is what hands the draft
 * back in that case, for as long as the tab lives. */

describe('chatPaneDrafts', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('parks and takes per slot; take clears the entry', () => {
    writePaneDraft('a', { text: 'for a', files: ['/tmp/a.png'] })
    expect(readPaneDraft('a')).toEqual({ text: 'for a', files: ['/tmp/a.png'] })
    expect(takePaneDraft('a')).toEqual({ text: 'for a', files: ['/tmp/a.png'] })
    expect(readPaneDraft('a')).toEqual({ text: '', files: [] })
  })

  it('lives in sessionStorage, not the localStorage quota ChatPage shares', () => {
    writePaneDraft('a', { text: 'for a', files: [] })
    expect(sessionStorage.getItem(PANE_DRAFTS_KEY)).toContain('for a')
    expect(localStorage.getItem(PANE_DRAFTS_KEY)).toBeNull()
  })

  it('hands the draft back from the mirror when storage refuses the write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    writePaneDraft('quota', { text: 'kept despite quota', files: ['/tmp/q.png'] })
    expect(readPaneDraft('quota')).toEqual({ text: 'kept despite quota', files: ['/tmp/q.png'] })
    // A merge on top of a refused write still accumulates.
    mergePaneDraft('quota', 'and this', [])
    expect(readPaneDraft('quota').text).toContain('kept despite quota')
    expect(readPaneDraft('quota').text).toContain('and this')
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
})
