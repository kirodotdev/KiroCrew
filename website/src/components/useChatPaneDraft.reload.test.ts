import { describe, it, expect, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useChatPaneDraft } from './useChatPaneDraft'
import { PANE_DRAFTS_KEY, readPaneDraft, __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* A reload (an update's gateway restart reloads the tab) never unmounts the
 * pane, so the unmount park does not run. The unsent text must still come
 * back in the same chat after the reload. */
describe('useChatPaneDraft across a page reload', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
  })

  it('restores the unsent composer text after the page reloads', () => {
    const { result } = renderHook(() => useChatPaneDraft('member-a'))
    act(() => { result.current.setInput('a long unsent draft') })
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    // The reload: this tab's in-memory state is gone, sessionStorage stays.
    const kept = sessionStorage.getItem(PANE_DRAFTS_KEY)
    __resetPaneDraftsForTests()
    if (kept !== null) sessionStorage.setItem(PANE_DRAFTS_KEY, kept)
    const reloaded = renderHook(() => useChatPaneDraft('member-a'))
    expect(reloaded.result.current.input).toBe('a long unsent draft')
  })

  it('a page restored from the back/forward cache keeps one copy of the text', () => {
    const { result } = renderHook(() => useChatPaneDraft('member-a'))
    act(() => { result.current.setInput('draft') })
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    // The test environment's PageTransitionEvent ignores `persisted`, so set it.
    const restored = Object.defineProperty(new Event('pageshow'), 'persisted', { value: true })
    act(() => { window.dispatchEvent(restored) })
    // The live composer holds the text; the store holds no second copy that a
    // later arrival would merge back in on top of it.
    expect(result.current.input).toBe('draft')
    expect(readPaneDraft('member-a').text).toBe('')
  })
})
