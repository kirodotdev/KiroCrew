/**
 * Message-targeted scroll intent (pages/chat/page/transcriptJumps.ts), driven as
 * a hook against a stand-in search state and a spy for the transcript's
 * `navToMessage`, which records the display row each jump was for.
 *
 * Pins the parts ChatPage-level suites never reach:
 *  - clicking a search result is a direct jump: it snaps (`behavior: 'auto'`)
 *    rather than gliding to an estimated offset;
 *  - re-clicking the SELECTED result still scrolls back to it (next frame) and
 *    re-centres the active mark, cancelling a previous converge loop first;
 *  - stepping (Enter/Arrow) centres each match and leaves the glide-vs-snap
 *    choice to `pickSearchScrollBehavior`;
 *  - the reader moves once per find action (`search.reveal`): rows arriving
 *    that renumber the current match move nobody, and a step inside the
 *    message the reader is on is left to its row while the row is mounted;
 *  - the approval bar's "Show in chat" scrolls to the tool row by call id;
 *  - a message with no row on screen is never scrolled to.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act } from '@testing-library/react'
import { useEffect, type ReactNode } from 'react'
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
    reveal: 0,
    currentRowKey: null,
    revealLedger: { pending: vi.fn(() => true), consume: vi.fn() },
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
  // The rows on screen, as the render being driven has them: message index to
  // display row through `messageToDisplayIdx`, the way the page's rows draw them.
  const live = { messages, map }
  const messagesRef = { current: messages }
  const rowOfMessage = (m: ChatMessage) => live.map.get(live.messages.indexOf(m)) ?? -1
  /** The display row of each jump that found one, and its options. */
  const jumped: Array<[number, unknown]> = []
  const navToMessage = vi.fn((m: ChatMessage, o?: unknown) => {
    const di = rowOfMessage(m)
    if (di < 0) return false
    jumped.push([di, o])
    return true
  })
  const opts: Opts = {
    search: searchState(),
    messages,
    messagesRef,
    messageToDisplayIdx: map,
    rowsTrailList: false,
    navToMessage,
    rowOfMessage,
    scrollerRef: { current: null },
    activeSlot: 'slot-a',
    cursorIsForActiveSlot: true,
    slotHasMore: false,
    slotOldestIndex: 0,
    initialMsgRef: { current: null },
    initialMidRef: { current: null },
    initialSidRef: { current: null },
    markJump: vi.fn(),
    handleJumpToPinnedMessage: vi.fn(),
    ...over,
  }
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook((p: Opts) => {
    live.messages = p.messages
    live.map = p.messageToDisplayIdx
    messagesRef.current = p.messages
    return useTranscriptJumps(p)
  }, { initialProps: opts, wrapper })
  return { opts, hook, store, jumped }
}

beforeEach(() => {
  scroll.cancel.mockClear()
  scroll.converge.mockClear()
  scroll.behavior = 'smooth'
})

describe('search result jumps', () => {
  it('a click on another result selects it and the jump snaps to the centred row', () => {
    const { opts, hook, jumped } = harness()
    act(() => { hook.result.current.jumpToSearchResult(1) })
    expect(opts.search.goTo).toHaveBeenCalledWith(1)
    expect(jumped).toEqual([])
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, reveal: 1, goTo: opts.search.goTo }) })
    expect(jumped).toEqual([[12, { behavior: 'auto', align: 'center' }]])
  })

  it('stepping centres each match and takes the glide/snap choice from the step cadence', () => {
    const { opts, hook, jumped } = harness()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, reveal: 1 }) })
    expect(jumped.at(-1)).toEqual([12, { behavior: 'smooth', align: 'center' }])
    scroll.behavior = 'auto'
    hook.rerender({ ...opts, search: searchState({ currentIdx: 0, currentMessageIdx: 0, reveal: 2 }) })
    expect(jumped.at(-1)).toEqual([10, { behavior: 'auto', align: 'center' }])
  })

  it('never scrolls to a match whose message has no row on screen, and spends that find action', () => {
    const { opts, hook } = harness()
    // Message 1 is loaded, and the rows on screen do not draw it.
    const search = searchState({ currentIdx: 2, currentMessageIdx: 1, reveal: 1 })
    hook.rerender({ ...opts, search })
    expect(opts.navToMessage).not.toHaveBeenCalled()
    expect(search.revealLedger.consume).toHaveBeenCalledWith(1)
  })

  // The rows on screen draw the deferred snapshot, a commit behind the list
  // while a landed page waits to render: a match on that page has no row yet,
  // and the action waits for the rows instead of being spent.
  it('leaves a find action for a message the rows do not draw yet unanswered while the rows trail the list, then moves to it', () => {
    const { opts, hook, jumped } = harness({ rowsTrailList: true })
    const search = searchState({ currentIdx: 2, currentMessageIdx: 1, reveal: 1 })
    hook.rerender({ ...opts, search })
    expect(opts.navToMessage).not.toHaveBeenCalled()
    expect(search.revealLedger.consume).not.toHaveBeenCalled()
    // The rows catch up and draw the message.
    const caughtUp = new Map(opts.messageToDisplayIdx).set(1, 11)
    hook.rerender({ ...opts, search, rowsTrailList: false, messageToDisplayIdx: caughtUp })
    expect(jumped).toEqual([[11, { behavior: 'smooth', align: 'center' }]])
    expect(search.revealLedger.consume).not.toHaveBeenCalled()
    // Rows changing afterwards do not move the reader again for that action.
    hook.rerender({ ...opts, search, rowsTrailList: false, messageToDisplayIdx: new Map(caughtUp).set(2, 13) })
    expect(jumped).toHaveLength(1)
  })

  it('spends a find action for a message the rows draw no row for once the rows are current', () => {
    const { opts, hook } = harness({ rowsTrailList: true })
    const search = searchState({ currentIdx: 2, currentMessageIdx: 1, reveal: 1 })
    hook.rerender({ ...opts, search })
    expect(search.revealLedger.consume).not.toHaveBeenCalled()
    hook.rerender({ ...opts, search, rowsTrailList: false })
    expect(opts.navToMessage).not.toHaveBeenCalled()
    expect(search.revealLedger.consume).toHaveBeenCalledWith(1)
  })

  // The row poll the jump starts must claim the scroll before the row's own
  // occurrence poll, which then takes it over and centers the occurrence. Both
  // run in the same commit: the jump in a layout effect, the row in a passive one.
  it('starts the jump before a row effect of the same commit runs', () => {
    const { opts, store } = harness()
    const order: string[] = []
    const Row = () => {
      useEffect(() => { order.push('row') })
      return null
    }
    const Host = () => {
      useTranscriptJumps({
        ...opts,
        search: searchState({ currentIdx: 1, currentMessageIdx: 2, reveal: 1 }),
        navToMessage: () => { order.push('jump'); return true },
      })
      return <Row />
    }
    render(<Provider store={store}><Host /></Provider>)
    expect(order).toEqual(['jump', 'row'])
  })

  it('moves nobody when rows arriving renumber the current match, or hand "current" on without a find action', () => {
    const { opts, hook } = harness()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, reveal: 1 }) })
    expect(opts.navToMessage).toHaveBeenCalledTimes(1)
    // An older page landed: the same match, at a new index, and no new action.
    const shifted = new Map<number, number>([[3, 13]])
    hook.rerender({ ...opts, messageToDisplayIdx: shifted, search: searchState({ currentIdx: 4, currentMessageIdx: 3, reveal: 1 }) })
    hook.rerender({ ...opts, search: searchState({ currentIdx: 0, currentMessageIdx: 0, reveal: 0 }) })
    expect(opts.navToMessage).toHaveBeenCalledTimes(1)
  })

  it('leaves a step inside the same message to its row while that row is in the window, and brings it back once it is not', () => {
    const scroller = document.createElement('div')
    const turn = document.createElement('div')
    turn.dataset.displayIndex = '12'
    scroller.appendChild(turn)
    const { opts, hook, jumped } = harness({ scrollerRef: { current: scroller } })
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, currentRowKey: 'row-c', reveal: 1 }) })
    expect(jumped).toHaveLength(1)
    // The next occurrence in the message the reader was just moved to, after the
    // store copied that message: the same key, so the same message.
    const copied = opts.messages.map(m => ({ ...m }))
    hook.rerender({ ...opts, messages: copied, search: searchState({ currentIdx: 2, currentMessageIdx: 2, currentRowKey: 'row-c', reveal: 2 }) })
    expect(jumped).toHaveLength(1)
    // The reader scrolled the row out of the window: the next step brings it back.
    turn.remove()
    hook.rerender({ ...opts, messages: copied, search: searchState({ currentIdx: 1, currentMessageIdx: 2, currentRowKey: 'row-c', reveal: 3 }) })
    expect(jumped).toEqual([[12, { behavior: 'smooth', align: 'center' }], [12, { behavior: 'smooth', align: 'center' }]])
    expect(opts.navToMessage).toHaveBeenLastCalledWith(copied[2], { behavior: 'smooth', align: 'center' })
  })

  it('moves to another message through the page even when its row is in the window', () => {
    const scroller = document.createElement('div')
    for (const di of ['10', '12']) {
      const turn = document.createElement('div')
      turn.dataset.displayIndex = di
      scroller.appendChild(turn)
    }
    const { opts, hook, jumped } = harness({ scrollerRef: { current: scroller } })
    hook.rerender({ ...opts, search: searchState({ currentIdx: 0, currentMessageIdx: 0, currentRowKey: 'row-a', reveal: 1 }) })
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, currentRowKey: 'row-c', reveal: 2 }) })
    expect(jumped).toHaveLength(2)
    expect(jumped.at(-1)).toEqual([12, { behavior: 'smooth', align: 'center' }])
  })

  it('a re-click on the selected result scrolls back next frame and re-centres its mark', () => {
    const rafs: FrameRequestCallback[] = []
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { rafs.push(cb); return rafs.length })
    try {
      const { opts, hook, jumped } = harness({ search: searchState({ currentIdx: 1, currentMessageIdx: -1 }) })
      act(() => { hook.result.current.jumpToSearchResult(1) })
      expect(opts.search.goTo).not.toHaveBeenCalled()
      expect(jumped).toEqual([])
      act(() => { rafs.shift()!(0) })
      expect(jumped).toEqual([[12, { behavior: 'auto', align: 'center' }]])
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
      // Match 2 is past the loaded messages.
      const past = harness({ search: searchState({ currentIdx: 2 }) })
      act(() => { past.hook.result.current.jumpToSearchResult(2) })
      // Match 1's message is loaded, and the rows on screen do not draw it.
      const matches = [{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }]
      const undrawn = harness({ search: searchState({ currentIdx: 1, matches }) })
      act(() => { undrawn.hook.result.current.jumpToSearchResult(1) })
      expect(raf).not.toHaveBeenCalled()
      expect(past.opts.search.goTo).not.toHaveBeenCalled()
      expect(undrawn.opts.search.goTo).not.toHaveBeenCalled()
    } finally { raf.mockRestore() }
  })
})

describe('"Show in chat" from the approval bar', () => {
  it('scrolls smoothly to the tool row that carries the focused call id', () => {
    const { store, jumped } = harness()
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(jumped).toEqual([[13, { behavior: 'smooth', align: 'center' }]])
  })

  it('ignores a call id that no loaded tool row carries', () => {
    const { opts, store } = harness()
    act(() => { store.dispatch(openActivityToTool('call-missing')) })
    expect(opts.navToMessage).not.toHaveBeenCalled()
  })

  it('scrolls to a tool row the rows on screen do not draw yet once they draw it', () => {
    const { opts, hook, store, jumped } = harness({ messageToDisplayIdx: new Map([[0, 10]]) })
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(jumped).toEqual([])
    hook.rerender({ ...opts, messageToDisplayIdx: new Map([[0, 10], [3, 13]]) })
    expect(jumped).toEqual([[13, { behavior: 'smooth', align: 'center' }]])
  })
})

describe('a ?msg= link', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('waits while the rows on screen do not draw the linked message, then lands on it', () => {
    const messages = [
      { role: 'user', content: 'a', ts: 't0' },
      { role: 'assistant', content: 'b', ts: 't1' },
    ] as ChatMessage[]
    const { opts, hook, jumped } = harness({ messages, messageToDisplayIdx: new Map([[0, 10]]), initialMsgRef: { current: 't1' } })
    // The link scrolls 500ms after its row is found.
    act(() => { vi.advanceTimersByTime(600) })
    expect(jumped).toEqual([])
    expect(opts.markJump).not.toHaveBeenCalled()
    expect(opts.initialMsgRef.current).toBe('t1')

    hook.rerender({ ...opts, messageToDisplayIdx: new Map([[0, 10], [1, 11]]) })
    act(() => { vi.advanceTimersByTime(600) })
    expect(jumped).toEqual([[11, { behavior: 'auto', align: 'center' }]])
    expect(opts.markJump).toHaveBeenCalledWith('t1', true, undefined)
  })
})
