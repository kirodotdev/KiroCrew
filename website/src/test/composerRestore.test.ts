import { beforeEach, describe, expect, it } from 'vitest'
import { restoreToComposer, registerMainComposer } from '../utils/composerRestore'
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
})
