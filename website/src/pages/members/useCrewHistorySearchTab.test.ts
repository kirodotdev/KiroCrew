import { describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { CREW_HISTORY_SEARCH_TAB_ID, useCrewHistorySearchTab } from './useCrewHistorySearchTab'

const SESSIONS = 'crew-sessions'

function setup(opts: { isMobile?: boolean; beside?: boolean } = {}) {
  const setActive = vi.fn()
  const setDockedOpen = vi.fn()
  const setOverlayOpen = vi.fn()
  const closeReplyThread = vi.fn()
  const view = renderHook(
    ({ activeTabId, panelShown }) => useCrewHistorySearchTab({
      activeTabId, setActive, fallbackTabId: 'crew-dashboard', isMobile: !!opts.isMobile, beside: opts.beside ?? true, setDockedOpen, setOverlayOpen, closeReplyThread, panelOpen: panelShown, panelShown,
    }),
    { initialProps: { activeTabId: SESSIONS as string | null, panelShown: true } },
  )
  return { view, setActive, setDockedOpen, setOverlayOpen, closeReplyThread }
}

describe('useCrewHistorySearchTab', () => {
  it('closes a reply thread when opening or refocusing search', () => {
    const { view, closeReplyThread } = setup()
    act(() => view.result.current.onHistorySearchOpenChange(true))
    expect(closeReplyThread).toHaveBeenCalledTimes(1)
    view.rerender({ activeTabId: CREW_HISTORY_SEARCH_TAB_ID })
    act(() => view.result.current.onHistorySearchOpenChange(true))
    expect(closeReplyThread).toHaveBeenCalledTimes(2)
    act(() => view.result.current.onHistorySearchOpenChange(false))
    expect(closeReplyThread).toHaveBeenCalledTimes(2)
  })

  it('opens the Search tab in the docked panel and returns to the tab before it on close', () => {
    const { view, setActive, setDockedOpen } = setup()
    act(() => view.result.current.onHistorySearchOpenChange(true))
    expect(view.result.current.historySearchOpen).toBe(true)
    expect(setActive).toHaveBeenLastCalledWith(CREW_HISTORY_SEARCH_TAB_ID)
    expect(setDockedOpen).toHaveBeenCalledWith(true)

    view.rerender({ activeTabId: CREW_HISTORY_SEARCH_TAB_ID })
    act(() => view.result.current.onHistorySearchOpenChange(false))
    expect(view.result.current.historySearchOpen).toBe(false)
    expect(setActive).toHaveBeenLastCalledWith(SESSIONS)
  })

  it('is on screen only while its tab is focused in a shown panel', () => {
    const { view } = setup()
    act(() => view.result.current.onHistorySearchOpenChange(true))
    view.rerender({ activeTabId: CREW_HISTORY_SEARCH_TAB_ID, panelShown: true })
    expect(view.result.current.historySearchOnScreen).toBe(true)
    view.rerender({ activeTabId: 'crew-dashboard', panelShown: true })
    expect(view.result.current.historySearchOpen).toBe(true)
    expect(view.result.current.historySearchOnScreen).toBe(false)
    view.rerender({ activeTabId: CREW_HISTORY_SEARCH_TAB_ID, panelShown: false })
    expect(view.result.current.historySearchOnScreen).toBe(false)
  })

  it('reveals the overlay when the panel does not sit beside the chat', () => {
    const { view, setOverlayOpen, setDockedOpen } = setup({ beside: false })
    act(() => view.result.current.onHistorySearchOpenChange(true))
    expect(setOverlayOpen).toHaveBeenCalledWith(true)
    expect(setDockedOpen).not.toHaveBeenCalled()
  })

  it('on a phone opens the overlay on the Search tab, and closing the search closes the overlay', () => {
    const { view, setActive, setOverlayOpen } = setup({ isMobile: true, beside: false })
    act(() => view.result.current.onHistorySearchOpenChange(true))
    expect(view.result.current.historySearchOpen).toBe(true)
    expect(setActive).toHaveBeenLastCalledWith(CREW_HISTORY_SEARCH_TAB_ID)
    expect(setOverlayOpen).toHaveBeenLastCalledWith(true)
    view.rerender({ activeTabId: CREW_HISTORY_SEARCH_TAB_ID, panelShown: true })
    act(() => view.result.current.onHistorySearchOpenChange(false))
    expect(setOverlayOpen).toHaveBeenLastCalledWith(false)
  })
})
