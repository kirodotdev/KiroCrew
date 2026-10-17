/**
 * Message-targeted scroll intent (pages/chat/page/transcriptJumps.ts), driven as
 * a hook against a stand-in search state and a spy for the transcript's
 * `navToDisplayIndex`.
 *
 * Pins the parts ChatPage-level suites never reach:
 *  - clicking a search result is a direct jump: it snaps (`behavior: 'auto'`)
 *    rather than gliding to an estimated offset;
 *  - re-clicking the SELECTED result still scrolls back to it (next frame) and
 *    re-centres the active mark, cancelling a previous converge loop first;
 *  - stepping (Enter/Arrow) centres each match and leaves the glide-vs-snap
 *    choice to `pickSearchScrollBehavior`;
 *  - the approval bar's "Show in chat" scrolls to the tool row by call id;
 *  - a row that is not in the display map is never scrolled to.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'

const scroll = vi.hoisted(() => ({ cancel: vi.fn(), converge: vi.fn(), behavior: 'smooth' as ScrollBehavior }))
vi.mock('../utils/searchScroll', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/searchScroll')>()),
  pickSearchScrollBehavior: () => scroll.behavior,
  scrollCurrentMatchIntoView: () => { scroll.converge(); return scroll.cancel },
}))

import { useTranscriptJumps } from '../pages/chat/page/transcriptJumps'
import { openActivityToTool } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { createTestStore } from './helpers'

type Opts = Parameters<typeof useTranscriptJumps>[0]
type Search = Opts['search']

function searchState(over: Partial<Search> = {}): Search {
  return {
    currentIdx: 0,
    currentMessageIdx: -1,
    matches: [{ msgIdx: 0 }, { msgIdx: 2 }, { msgIdx: 5 }],
    goTo: vi.fn(),
    ...over,
  } as unknown as Search
}

function harness(over: Partial<Opts> = {}) {
  const store = createTestStore()
  const map = new Map<number, number>([[0, 10], [2, 12], [3, 13]])
  const messages = [
    { role: 'user', content: 'a' },
    { role: 'assistant', content: 'b' },
    { role: 'assistant', content: 'c' },
    { role: 'tool', content: 'd', meta: { tool_call_id: 'call-9' } },
  ] as ChatMessage[]
  const opts: Opts = {
    search: searchState(),
    messages,
    messageToDisplayIdx: map,
    messageToDisplayIdxRef: { current: map },
    navToDisplayIndex: vi.fn(),
    activeSlot: 'slot-a',
    cursorIsForActiveSlot: true,
    slotHasMore: false,
    slotOldestIndex: 0,
    initialMsgRef: { current: null },
    initialMidRef: { current: null },
    initialSidRef: { current: null },
    setHighlightTs: vi.fn(),
    handleJumpToPinnedMessage: vi.fn(),
    ...over,
  }
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook((p: Opts) => useTranscriptJumps(p), { initialProps: opts, wrapper })
  return { opts, hook, store }
}

beforeEach(() => {
  scroll.cancel.mockClear()
  scroll.converge.mockClear()
  scroll.behavior = 'smooth'
})

describe('search result jumps', () => {
  it('a click on another result selects it and the jump snaps to the centred row', () => {
    const { opts, hook } = harness()
    act(() => { hook.result.current.jumpToSearchResult(1) })
    expect(opts.search.goTo).toHaveBeenCalledWith(1)
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, goTo: opts.search.goTo }) })
    expect(opts.navToDisplayIndex).toHaveBeenCalledWith(12, { behavior: 'auto', align: 'center' })
  })

  it('stepping centres each match and takes the glide/snap choice from the step cadence', () => {
    const { opts, hook } = harness()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2 }) })
    expect(opts.navToDisplayIndex).toHaveBeenLastCalledWith(12, { behavior: 'smooth', align: 'center' })
    scroll.behavior = 'auto'
    hook.rerender({ ...opts, search: searchState({ currentIdx: 0, currentMessageIdx: 0 }) })
    expect(opts.navToDisplayIndex).toHaveBeenLastCalledWith(10, { behavior: 'auto', align: 'center' })
  })

  it('never scrolls to a match whose row is not in the display map', () => {
    const { opts, hook } = harness()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 2, currentMessageIdx: 5 }) })
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
  })

  it('a re-click on the selected result scrolls back next frame and re-centres its mark', () => {
    const rafs: FrameRequestCallback[] = []
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { rafs.push(cb); return rafs.length })
    try {
      const { opts, hook } = harness({ search: searchState({ currentIdx: 1, currentMessageIdx: -1 }) })
      act(() => { hook.result.current.jumpToSearchResult(1) })
      expect(opts.search.goTo).not.toHaveBeenCalled()
      expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
      act(() => { rafs.shift()!(0) })
      expect(opts.navToDisplayIndex).toHaveBeenCalledWith(12, { behavior: 'auto', align: 'center' })
      expect(scroll.converge).toHaveBeenCalledTimes(1)
      expect(scroll.cancel).not.toHaveBeenCalled()
      // A second re-click cancels the first converge loop before starting one.
      act(() => { hook.result.current.jumpToSearchResult(1) })
      act(() => { rafs.shift()!(0) })
      expect(scroll.cancel).toHaveBeenCalledTimes(1)
      expect(scroll.converge).toHaveBeenCalledTimes(2)
    } finally { raf.mockRestore() }
  })

  it('a re-click on a selected result with no row does nothing', () => {
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    try {
      const { opts, hook } = harness({ search: searchState({ currentIdx: 2 }) })
      act(() => { hook.result.current.jumpToSearchResult(2) })
      expect(raf).not.toHaveBeenCalled()
      expect(opts.search.goTo).not.toHaveBeenCalled()
    } finally { raf.mockRestore() }
  })
})

describe('"Show in chat" from the approval bar', () => {
  it('scrolls smoothly to the tool row that carries the focused call id', () => {
    const { opts, store } = harness()
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(opts.navToDisplayIndex).toHaveBeenCalledWith(13, { behavior: 'smooth', align: 'center' })
  })

  it('ignores a call id that no loaded tool row carries', () => {
    const { opts, store } = harness()
    act(() => { store.dispatch(openActivityToTool('call-missing')) })
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
  })

  it('ignores a tool row that is not in the display map', () => {
    const { opts, store } = harness({ messageToDisplayIdx: new Map([[0, 10]]) })
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
  })
})
