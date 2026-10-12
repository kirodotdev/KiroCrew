import { describe, it, expect, vi, afterEach, onTestFinished } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { createElement, useLayoutEffect, useState, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { useScrollManager } from '../pages/chat/useScrollManager'
import { useChatPageTranscriptEarlyController } from '../pages/chat/useChatPageTranscriptController'
import { useTranscriptPins } from '../pages/chat/page/transcriptPins'
import { useTranscriptJumps } from '../pages/chat/page/transcriptJumps'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { DisplayItem } from '../pages/chat/types'
import { openActivityToTool } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import { applySearchHighlights, clearSearchHighlights } from '../utils/domHighlight'
import { coveredTopInset, scrollElementToCenter, scrollRangeToCenter } from '../utils/searchScroll'
import { createTestStore, renderWithProviders } from './helpers'

// The pins hook reads the chat's pins through React Query; no test here needs a pin list.
vi.mock('../hooks/useChatPins', () => ({
  useChatPins: () => ({
    pins: [], loading: false, error: null, clearError: () => {}, isPinned: () => false,
    pinMessage: async () => undefined, unpinMessage: async () => undefined, unpinById: async () => undefined, refresh: () => {},
  }),
}))

/**
 * Centred jumps in the top-down layout keep targets below the composer dock.
 *
 * The composer dock floats over the scroller's top edge and the scroller pays
 * for that strip with its top padding. Pins, quotes, edit-last, search, tool
 * focus and deep links all centre their target; centring against the full
 * scroller put the target under the dock. They must centre in the band below
 * it, and every other layout (inset 0) must keep the old result.
 *
 * Geometry: a 600 px scroller whose top 300 px the dock covers, so the visible
 * band is [300, 600] and its centre is y = 450.
 *
 * The first group pins the shared helper arithmetic. The "jump kind" group
 * starts each caller from the hook or component reached by a reader's action,
 * so one caller cannot move its target outside the visible band.
 */

const CLIENT_H = 600
const COVERED = 300

function makeScroller({ topDown, paddingTop = COVERED }: { topDown: boolean; paddingTop?: number }) {
  const el = document.createElement('div')
  el.className = 'chat-container'
  el.style.paddingTop = `${paddingTop}px`
  el.style.overflowY = 'auto'
  // The shell renders this leading block only with the live edge at the top.
  if (topDown) {
    const leading = document.createElement('div')
    leading.setAttribute('data-vc-leading', '')
    el.appendChild(leading)
  }
  let scrollTop = 0
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v } })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => CLIENT_H })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 5000 })
  el.getBoundingClientRect = () => ({ top: 0, bottom: CLIENT_H, height: CLIENT_H, left: 0, right: 800, width: 800, x: 0, y: 0, toJSON: () => ({}) })
  const scrollTo = vi.fn((opts: ScrollToOptions) => { scrollTop = opts.top ?? scrollTop })
  ;(el as unknown as { scrollTo: unknown }).scrollTo = scrollTo
  document.body.appendChild(el)
  return { el, scrollTo }
}

function addRow(scroller: HTMLElement, index: number, top: number, height: number): HTMLElement {
  const row = document.createElement('div')
  row.setAttribute('data-display-index', String(index))
  Object.defineProperty(row, 'offsetHeight', { configurable: true, get: () => height })
  row.getBoundingClientRect = () => ({ top, bottom: top + height, height, left: 0, right: 800, width: 800, x: 0, y: top, toJSON: () => ({}) })
  scroller.appendChild(row)
  return row
}

afterEach(() => { document.body.replaceChildren() })

describe('coveredTopInset', () => {
  it('is the scroller top padding in the top-down layout', () => {
    const { el } = makeScroller({ topDown: true })
    expect(coveredTopInset(el)).toBe(COVERED)
  })
  it('is 0 in the bottom layout even when the host pads the top', () => {
    // ChatPane's bottom layout pads the top (12 + title inset) with no dock there.
    const { el } = makeScroller({ topDown: false, paddingTop: 48 })
    expect(coveredTopInset(el)).toBe(0)
  })
})

describe('scrollToDisplayIndex align:center', () => {
  it('top-down: centres the row in the band below the dock', () => {
    const { result } = renderHook(() => useScrollManager())
    const { el, scrollTo } = makeScroller({ topDown: true })
    addRow(el, 7, 1000, 40)
    ;(result.current.scrollerRef as { current: HTMLDivElement | null }).current = el
    expect(result.current.scrollToDisplayIndex(7, { behavior: 'auto', align: 'center' })).toBe(true)
    const top = scrollTo.mock.calls[0][0].top as number
    // Row centre (1020) lands at the band centre: 1020 - top = (300 + 600) / 2.
    expect(top).toBe(570)
    expect(1020 - top).toBe((COVERED + CLIENT_H) / 2)
  })
  it('reads the inset at jump time, so a grown dock moves the landing', () => {
    const { result } = renderHook(() => useScrollManager())
    const { el, scrollTo } = makeScroller({ topDown: true })
    addRow(el, 7, 1000, 40)
    ;(result.current.scrollerRef as { current: HTMLDivElement | null }).current = el
    el.style.paddingTop = '400px'
    result.current.scrollToDisplayIndex(7, { behavior: 'auto', align: 'center' })
    expect(scrollTo.mock.calls[0][0].top).toBe(1020 - (400 + CLIENT_H) / 2)
  })
  it('inset 0: the bottom layout keeps the full-scroller centre', () => {
    const { result } = renderHook(() => useScrollManager())
    const { el, scrollTo } = makeScroller({ topDown: false, paddingTop: COVERED })
    addRow(el, 7, 1000, 40)
    ;(result.current.scrollerRef as { current: HTMLDivElement | null }).current = el
    result.current.scrollToDisplayIndex(7, { behavior: 'auto', align: 'center' })
    // The pre-fix formula: elTop - clientHeight / 2 + offsetHeight / 2.
    expect(scrollTo.mock.calls[0][0].top).toBe(1000 - CLIENT_H / 2 + 20)
  })
  it('align:start is unchanged by the inset (its caller passes the offset)', () => {
    const { result } = renderHook(() => useScrollManager())
    const { el, scrollTo } = makeScroller({ topDown: true })
    addRow(el, 7, 1000, 40)
    ;(result.current.scrollerRef as { current: HTMLDivElement | null }).current = el
    result.current.scrollToDisplayIndex(7, { behavior: 'auto', align: 'start', offset: -324 })
    expect(scrollTo.mock.calls[0][0].top).toBe(1000 - 324)
  })
})

function fakeRange(scroller: HTMLElement, top: number, height: number): Range {
  const p = document.createElement('p')
  p.textContent = 'needle'
  scroller.appendChild(p)
  return {
    startContainer: p.firstChild as Node,
    getBoundingClientRect: () => ({ top, bottom: top + height, height, left: 0, right: 10, width: 10, x: 0, y: top, toJSON: () => ({}) }),
  } as unknown as Range
}

describe('scrollRangeToCenter (search match refinement)', () => {
  const scrollBy = vi.fn()
  afterEach(() => { scrollBy.mockReset() })

  it('top-down: centres the match in the band below the dock', () => {
    window.scrollBy = scrollBy as unknown as typeof window.scrollBy
    const { el } = makeScroller({ topDown: true })
    scrollRangeToCenter(fakeRange(el, 500, 20))
    // Match centre 510 moves to the band centre 450.
    expect(el.scrollTop).toBe(60)
  })
  it('inset 0: the bottom layout keeps the full-scroller centre', () => {
    window.scrollBy = scrollBy as unknown as typeof window.scrollBy
    const { el } = makeScroller({ topDown: false })
    scrollRangeToCenter(fakeRange(el, 500, 20))
    expect(el.scrollTop).toBe(510 - CLIENT_H / 2)
  })
})

describe('scrollElementToCenter (tool focus refinement)', () => {
  it('top-down: centres the pill in the band below the dock', () => {
    const { el, scrollTo } = makeScroller({ topDown: true })
    const pill = addRow(el, 3, 500, 20)
    const native = vi.fn()
    pill.scrollIntoView = native
    scrollElementToCenter(pill)
    expect(native).not.toHaveBeenCalled()
    expect(scrollTo).toHaveBeenCalledWith({ top: 60, behavior: 'smooth' })
  })
  it('inset 0: falls through to the native centre scroll', () => {
    const { el, scrollTo } = makeScroller({ topDown: false })
    const pill = addRow(el, 3, 500, 20)
    const native = vi.fn()
    pill.scrollIntoView = native
    scrollElementToCenter(pill)
    expect(native).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' })
    expect(scrollTo).not.toHaveBeenCalled()
  })
})

// ── Each jump kind, started from its real entry point ────────────────────────
//
// Each kind runs through ChatPage's real scroll chain: the early transcript
// controller's `navToDisplayIndex` (mount, then poll the row each frame) into
// `useScrollManager().scrollToDisplayIndex`. The caller suites
// (ChatPage.transcriptPins, useChatPageTranscriptJumps, ToolCallLine) replace
// that chain or the match scroll with spies, so the band cannot be seen there.
// A top-down test sets up the scroller as TranscriptScrollShell writes it in
// that layout (top padding = the dock strip, a `[data-vc-leading]` child) and
// asserts the target's centre lands at the band centre, (inset + clientHeight)
// / 2 = 450. Its bottom-layout twin asserts the full-scroller centre, 300.

const TARGET_TOP = 2000
const TARGET_H = 40
const BAND_CENTRE = (COVERED + CLIENT_H) / 2
const FULL_CENTRE = CLIENT_H / 2

const rectAt = (top: number, height: number): DOMRect =>
  ({ top, bottom: top + height, height, left: 0, right: 800, width: 800, x: 0, y: top, toJSON: () => ({}) }) as DOMRect

/** A target at content offset `contentTop`. Its viewport rect follows the
 *  scroller's scrollTop, so a poll that re-scrolls every frame converges. */
function mountTarget(scroller: HTMLElement, di: number, contentTop = TARGET_TOP, height = TARGET_H): HTMLElement {
  const row = document.createElement('div')
  row.setAttribute('data-display-index', String(di))
  Object.defineProperty(row, 'offsetHeight', { configurable: true, get: () => height })
  row.getBoundingClientRect = () => rectAt(contentTop - scroller.scrollTop, height)
  scroller.appendChild(row)
  return row
}

/** Where a target's centre sits in the scroller's viewport now. */
const centreInView = (scroller: HTMLElement, contentTop = TARGET_TOP, height = TARGET_H) =>
  contentTop + height / 2 - scroller.scrollTop

/** A manual frame queue, restored when the test ends. Returns a flush that
 *  runs the queued frames (each may queue its successor) a bounded number of times. */
function installFrames() {
  const origRaf = globalThis.requestAnimationFrame
  const origCaf = globalThis.cancelAnimationFrame
  const pending = new Map<number, FrameRequestCallback>()
  let nextId = 1
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    const id = nextId++
    pending.set(id, cb)
    return id
  }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = ((id: number) => { pending.delete(id) }) as typeof cancelAnimationFrame
  onTestFinished(() => {
    globalThis.requestAnimationFrame = origRaf
    globalThis.cancelAnimationFrame = origCaf
  })
  return (count = 8) => {
    for (let i = 0; i < count && pending.size > 0; i++) {
      const batch = [...pending.values()]
      pending.clear()
      act(() => { batch.forEach(cb => cb(0)) })
    }
  }
}

function fakeTimeouts() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  onTestFinished(() => { vi.useRealTimers() })
}

/** ChatPage's scroll chain for one scroller, as ChatPage wires it. */
function useCentreChain(scroller: HTMLElement, topDown: boolean) {
  const sm = useScrollManager()
  // The page attaches this ref to the shell's scroller during commit.
  useLayoutEffect(() => {
    (sm.scrollerRef as { current: HTMLDivElement | null }).current = scroller as HTMLDivElement
  }, [sm.scrollerRef, scroller])
  const [refs] = useState(() => ({
    // A NEAR jump: the target row is already mounted.
    mountIndexRef: { current: () => false },
    estimateRowTopRef: { current: () => null },
    slotRunningRef: { current: false },
    vGetFollowRef: { current: () => false },
    vScrollToBottomRef: { current: () => {} },
  }))
  const early = useChatPageTranscriptEarlyController({
    activeTip: null,
    ...refs,
    scrollerRef: sm.scrollerRef,
    scrollToDisplayIndex: sm.scrollToDisplayIndex,
    topDown,
  })
  return { scrollerRef: sm.scrollerRef, early }
}

const tmsg = (role: string, ts: string, meta: Record<string, unknown>) =>
  ({ role, content: `${role} ${ts}`, ts, cls: '', meta }) as unknown as ChatMessage
const MESSAGES: ChatMessage[] = [
  tmsg('user', 't1', { mid: 'm1' }),
  tmsg('assistant', 't2', { mid: 'm2' }),
  tmsg('user', 't3', { mid: 'm3' }),
  tmsg('assistant', 't4', { mid: 'm4' }),
  tmsg('tool', 't5', { tool_call_id: 'call-9' }),
]
const ROWS = MESSAGES.map((m, idx) => ({ kind: 'single', msg: m, idx })) as DisplayItem[]

type PinsApi = ReturnType<typeof useTranscriptPins>

/** useTranscriptPins on the real chain. Returns where the target of
 *  `trigger` (message `msgIdx`) is centred once the jump has run. */
function landPinsJump(topDown: boolean, msgIdx: number, trigger: (api: PinsApi) => void): number {
  fakeTimeouts()
  const flush = installFrames()
  const { el: scroller } = makeScroller({ topDown })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children)
  const fixed = {
    messagesRef: { current: MESSAGES },
    editSeqRef: { current: 0 },
    renderedTranscript: { messages: MESSAGES, displayItems: ROWS },
    dispatch: vi.fn(), markStep: vi.fn(), markJump: vi.fn(), setEditLast: vi.fn(), insertIntoComposer: vi.fn(),
  }
  const { result } = renderHook(() => {
    const { scrollerRef, early } = useCentreChain(scroller, topDown)
    return useTranscriptPins({
      embedded: false, dispatch: fixed.dispatch as never, isMobile: false, activeSlot: 'slot-a', messages: MESSAGES,
      messagesRef: fixed.messagesRef, slotRunning: false, splitMode: false, crewWindow: null, slotHasMore: false, slotOldestIndex: 0,
      cursorIsForActiveSlot: true, loadingOlder: false, activeViewIsBoundedPage: false, isWelcomeState: false, activeSlotRemoteBound: false,
      scrollerRef, search: { isOpen: false } as never, navToDisplayIndex: early.navToDisplayIndex, pinnedJumpChrome: early.pinnedJumpChrome,
      scrollToPinnedPrompt: early.scrollToPinnedPrompt, markStep: fixed.markStep, markJump: fixed.markJump, splitFeatureEnabled: false, regenerating: false,
      setEditLast: fixed.setEditLast as never, editSeqRef: fixed.editSeqRef, renderedDisplayItems: ROWS,
      renderedTranscript: fixed.renderedTranscript, insertIntoComposer: fixed.insertIntoComposer, topDown,
    })
  }, { wrapper })
  const di = result.current.messageToDisplayIdx.get(msgIdx)
  expect(di).toBeDefined()
  mountTarget(scroller, di as number)
  act(() => { trigger(result.current) })
  flush()
  // The jump ran through the chain, not a no-op that left the view at 0.
  expect(scroller.scrollTop).toBeGreaterThan(0)
  return centreInView(scroller)
}

type JumpProps = Parameters<typeof useTranscriptJumps>[0]
type Search = JumpProps['search']
const DISPLAY = new Map(MESSAGES.map((_, i) => [i, i] as [number, number]))

/** useTranscriptJumps on the real chain, inside a store Provider. */
function renderJumps(topDown: boolean, over: Partial<Omit<JumpProps, 'navToDisplayIndex'>> = {}) {
  const { el: scroller, scrollTo } = makeScroller({ topDown })
  const store = createTestStore()
  const wrapper = ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children)
  const props: Omit<JumpProps, 'navToDisplayIndex'> = {
    search: { currentIdx: -1, currentMessageIdx: -1, matches: [], goTo: vi.fn() } as unknown as Search,
    messages: MESSAGES,
    messageToDisplayIdx: DISPLAY,
    messageToDisplayIdxRef: { current: DISPLAY },
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
  const { result } = renderHook(() => {
    const { early } = useCentreChain(scroller, topDown)
    return useTranscriptJumps({ ...props, navToDisplayIndex: early.navToDisplayIndex })
  }, { wrapper })
  return { scroller, scrollTo, store, result }
}

describe('jump kind: pin (useTranscriptPins.handleJumpToPin)', () => {
  it('top-down: the pinned message centres in the band below the dock', () => {
    expect(landPinsJump(true, 1, api => api.handleJumpToPin('t2', 'm2'))).toBe(BAND_CENTRE)
  })
  it('bottom layout: the pinned message keeps the full-scroller centre', () => {
    expect(landPinsJump(false, 1, api => api.handleJumpToPin('t2', 'm2'))).toBe(FULL_CENTRE)
  })
})

describe('jump kind: quote (useTranscriptPins.handleJumpToQuote)', () => {
  const quote = { role: 'user' as const, text: 'user t3', ts: 't3', mid: 'm3' }
  it('top-down: the quoted message centres in the band below the dock', () => {
    expect(landPinsJump(true, 2, api => api.handleJumpToQuote(quote))).toBe(BAND_CENTRE)
  })
  it('bottom layout: the quoted message keeps the full-scroller centre', () => {
    expect(landPinsJump(false, 2, api => api.handleJumpToQuote(quote))).toBe(FULL_CENTRE)
  })
})

describe('jump kind: edit-last (useTranscriptPins.handleEditLastRequest)', () => {
  // ⌘↑ / Ctrl+↑ targets the newest user message, index 2.
  it('top-down: the last sent message centres in the band below the dock', () => {
    expect(landPinsJump(true, 2, api => api.handleEditLastRequest())).toBe(BAND_CENTRE)
  })
  it('bottom layout: the last sent message keeps the full-scroller centre', () => {
    expect(landPinsJump(false, 2, api => api.handleEditLastRequest())).toBe(FULL_CENTRE)
  })
})

describe('jump kind: ?msg= deep link (useTranscriptJumps cold-load effect)', () => {
  function landDeepLink(topDown: boolean): number {
    fakeTimeouts()
    const flush = installFrames()
    const { scroller } = renderJumps(topDown, { initialMsgRef: { current: 't2' }, initialMidRef: { current: 'm2' } })
    mountTarget(scroller, 1)
    // The effect waits 500 ms for the first page to paint before it jumps.
    act(() => { vi.advanceTimersByTime(500) })
    flush()
    expect(scroller.scrollTop).toBeGreaterThan(0)
    return centreInView(scroller)
  }
  it('top-down: the linked message centres in the band below the dock', () => {
    expect(landDeepLink(true)).toBe(BAND_CENTRE)
  })
  it('bottom layout: the linked message keeps the full-scroller centre', () => {
    expect(landDeepLink(false)).toBe(FULL_CENTRE)
  })
})

describe('jump kind: search (useTranscriptJumps step, then the match refinement)', () => {
  // A tall row with the match well below its centre, so the row landing and
  // the match landing are different scroll positions.
  const ROW_H = 400
  const MATCH_TOP = 2300
  const MATCH_H = 20

  function landSearch(topDown: boolean) {
    const flush = installFrames()
    const origScrollBy = window.scrollBy
    window.scrollBy = vi.fn() as unknown as typeof window.scrollBy
    onTestFinished(() => { window.scrollBy = origScrollBy })
    const search = { currentIdx: 0, currentMessageIdx: 1, matches: [{ msgIdx: 1 }], goTo: vi.fn() } as unknown as Search
    // Stepping to a match: the effect centres the match's row.
    const { scroller, result } = renderJumps(topDown, { search })
    const row = mountTarget(scroller, 1, TARGET_TOP, ROW_H)
    flush()
    const rowCentre = centreInView(scroller, TARGET_TOP, ROW_H)
    // Re-clicking the selected result re-centres the row and then converges on
    // the painted match itself (scrollCurrentMatchIntoView -> scrollRangeToCenter).
    const text = document.createElement('p')
    text.textContent = 'before needle after'
    row.appendChild(text)
    applySearchHighlights(row, 'needle', false, 0)
    onTestFinished(() => { clearSearchHighlights(row) })
    const rangeRect = vi.spyOn(Range.prototype, 'getBoundingClientRect')
      .mockImplementation(() => rectAt(MATCH_TOP - scroller.scrollTop, MATCH_H))
    onTestFinished(() => { rangeRect.mockRestore() })
    act(() => { result.current.jumpToSearchResult(0) })
    flush()
    return { rowCentre, matchCentre: centreInView(scroller, MATCH_TOP, MATCH_H) }
  }

  it('top-down: the match row, then the match, centre in the band below the dock', () => {
    expect(landSearch(true)).toEqual({ rowCentre: BAND_CENTRE, matchCentre: BAND_CENTRE })
  })
  it('bottom layout: the match row, then the match, keep the full-scroller centre', () => {
    expect(landSearch(false)).toEqual({ rowCentre: FULL_CENTRE, matchCentre: FULL_CENTRE })
  })
})

describe('jump kind: tool focus ("Show in chat", useTranscriptJumps focusToolCallId)', () => {
  function landShowInChat(topDown: boolean): number {
    const flush = installFrames()
    const { scroller, store } = renderJumps(topDown)
    mountTarget(scroller, 4)
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    flush()
    expect(scroller.scrollTop).toBeGreaterThan(0)
    return centreInView(scroller)
  }
  it('top-down: the tool row centres in the band below the dock', () => {
    expect(landShowInChat(true)).toBe(BAND_CENTRE)
  })
  it('bottom layout: the tool row keeps the full-scroller centre', () => {
    expect(landShowInChat(false)).toBe(FULL_CENTRE)
  })
})

describe('jump kind: tool focus (ToolCallLine re-centres the focused pill)', () => {
  const toolMsg = {
    role: 'tool', content: '🔧 Running: echo hello', cls: '', meta: { tool_call_id: 'tc_1', purpose: 'Say hello' },
  } as unknown as ChatMessage

  function focusPill(topDown: boolean) {
    const flush = installFrames()
    const origScrollIntoView = Element.prototype.scrollIntoView
    const native = vi.fn()
    Element.prototype.scrollIntoView = native as unknown as typeof Element.prototype.scrollIntoView
    onTestFinished(() => { Element.prototype.scrollIntoView = origScrollIntoView })
    const { el: scroller, scrollTo } = makeScroller({ topDown })
    const row = document.createElement('div')
    row.setAttribute('data-display-index', '0')
    scroller.appendChild(row)
    const store = createTestStore({
      chat: {
        messages: [toolMsg],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'out', ts: 1 }],
        slotRunning: false,
        focusToolCallId: 'tc_1',
      } as unknown as RootState['chat'],
    })
    renderWithProviders(createElement(ToolCallLine, { message: toolMsg, running: false }), { store, container: row })
    // The pill sits at TARGET_TOP in the content. Its focus effect scrolls on the next frame.
    for (const el of [row, ...Array.from(row.querySelectorAll<HTMLElement>('*'))]) {
      el.getBoundingClientRect = () => rectAt(TARGET_TOP - scroller.scrollTop, TARGET_H)
    }
    flush()
    expect(store.getState().chat.focusToolCallId).toBeNull()
    return { scroller, scrollTo, native }
  }

  it('top-down: the focused pill centres in the band below the dock', () => {
    const { scroller, scrollTo, native } = focusPill(true)
    expect(native).not.toHaveBeenCalled()
    expect(scrollTo).toHaveBeenCalledTimes(1)
    expect(centreInView(scroller)).toBe(BAND_CENTRE)
  })
  it('bottom layout: the focused pill keeps the native full-scroller centre', () => {
    const { scroller, scrollTo, native } = focusPill(false)
    // Main's call, unchanged: scrollIntoView({ block: 'center' }) on the pill.
    expect(native).toHaveBeenCalledTimes(1)
    expect(native).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' })
    expect(scroller.contains(native.mock.contexts[0] as Node)).toBe(true)
    expect(scrollTo).not.toHaveBeenCalled()
  })
})
