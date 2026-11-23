import { beforeEach, describe, expect, it } from 'vitest'
import { keepInComposer, restoreToComposer, registerMainComposer } from '../utils/composerRestore'
import { vi } from 'vitest'
import { __resetForTests, loadDrafts } from '../utils/chatDrafts'
import { __resetPaneDraftsForTests, readPaneDraft, subscribePaneDraft } from '../utils/chatPaneDrafts'

describe('restoreToComposer', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests(); __resetPaneDraftsForTests() })

  it('prefers the main composer showing the slot over a pane', () => {
    const got: string[] = []
    const off = registerMainComposer((slot, text, showingOnly) => { if (slot === 'a' && showingOnly) { got.push(text); return true } return false })
    const unsub = subscribePaneDraft('a', () => {})
    restoreToComposer('a', 'yes')
    expect(got).toEqual(['yes'])
    expect(readPaneDraft('a').text).toBe('')
    off(); unsub()
  })

  it('uses a pane showing the slot when no main composer shows it', () => {
    const off = registerMainComposer(() => false)
    const unsub = subscribePaneDraft('a', () => {})
    restoreToComposer('a', 'yes')
    expect(readPaneDraft('a').text).toBe('yes')
    off(); unsub()
  })

  it('stores the draft in a mounted main composer that shows another slot', () => {
    const stored: Record<string, string> = {}
    const off = registerMainComposer((slot, text, showingOnly) => { if (showingOnly) return false; stored[slot] = text; return true })
    restoreToComposer('a', 'yes')
    expect(stored).toEqual({ a: 'yes' })
    off()
  })

  it('persists the draft when nothing is mounted, appending to what is there', () => {
    restoreToComposer('a', 'first')
    restoreToComposer('a', 'second')
    expect(loadDrafts().a).toBe('first\n\nsecond')
  })

  it('ignores blank text', () => {
    restoreToComposer('a', '  ')
    expect(loadDrafts().a).toBeUndefined()
  })

  it('answers the same visibility as before for every destination', () => {
    const offMain = registerMainComposer((slot, _t, showingOnly) => slot === 'main' && showingOnly)
    const offOther = registerMainComposer((slot, _t, showingOnly) => slot === 'other' && !showingOnly)
    const unsub = subscribePaneDraft('pane', () => {})
    expect([restoreToComposer('main', 'x'), keepInComposer('main', 'x')]).toEqual([true, 'shown'])
    expect([restoreToComposer('pane', 'x'), keepInComposer('pane', 'x')]).toEqual([true, 'shown'])
    expect([restoreToComposer('other', 'x'), keepInComposer('other', 'x')]).toEqual([false, 'kept'])
    expect([restoreToComposer('none', 'x'), keepInComposer('none', 'y')]).toEqual([false, 'kept'])
    expect([restoreToComposer('none', ' '), keepInComposer('none', ' ')]).toEqual([false, 'lost'])
    offMain(); offOther(); unsub()
  })

  it('says lost when the browser refuses to store the draft', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
    expect(keepInComposer('a', 'yes')).toBe('lost')
    expect(restoreToComposer('a', 'yes')).toBe(false)
    setItem.mockRestore()
  })
})
