/**
 * Arrow-key roving over the sidebar session list.
 *
 * Covers the two halves separately: the scope-bounded/clamped index maths as a
 * plain-DOM unit test, and the wiring (bare arrows only, focus-only rove, Enter
 * still activates) against a rendered ChatSidebar.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act, fireEvent, waitFor } from '@testing-library/react'
import type React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sessionRowsInScope, siblingSessionRow, focusSiblingSessionRow } from '../pages/chat/sessionRowNav'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Board view is per-describe: the list-view suites need it off, the board suite
// needs it on, and loadChatConfig is called at render time so a mutable flag is
// enough (a vi.mock factory only runs once per module).
const chatConfig = { tagColumnsEnabled: false, confirmCloseSession: false }
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => chatConfig,
  saveChatConfig: vi.fn(),
}))

// Board fixtures are per-describe too. They have to come back from the api mock,
// not just be seeded into the query cache: the seeded entry is stale on mount, so
// react-query refetches and a blanket `[]` mock would erase it.
const fixtures: { chatTags: unknown[]; tagColumns: unknown[]; chatFolders: unknown[] } = {
  chatTags: [], tagColumns: [], chatFolders: [],
}

// The pinned-order write and the slots re-read are stable spies so a case can
// assert what was sent and what the gateway answered.
const setPinnedOrder = vi.hoisted(() => vi.fn())
const chatSlots = vi.hoisted(() => vi.fn())
const setSlotPin = vi.hoisted(() => vi.fn())

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'setPinnedOrder') return setPinnedOrder
      if (prop === 'chatSlots') return chatSlots
      if (prop === 'setSlotPin') return setSlotPin
      if (prop in fixtures) return vi.fn().mockResolvedValue(fixtures[prop as keyof typeof fixtures])
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import { useSessionActions } from '../hooks/useSessionActions'
import { setPinRanks, sseSlotPatch, sseSlots } from '../store/dashboardSlice'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

function renderSidebar(slots: ChatSlot[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const view = (nextSlots: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={nextSlots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(view(slots))
  return {
    ...utils, store, qc,
    rerenderSlots: (nextSlots: ChatSlot[]) => utils.rerender(view(nextSlots)),
    // A collapse-and-reopen: a fresh sidebar mount on the same store.
    remount: (nextSlots: ChatSlot[]) => { utils.unmount(); return render(view(nextSlots)) },
  }
}

const THREE = [
  { key: 'k1', title: 'first', running: false, messages: 1 },
  { key: 'k2', title: 'second', running: false, messages: 1 },
  { key: 'k3', title: 'third', running: false, messages: 1 },
]

/** The focusable row element for a slot key, in list scope. */
// Drains pending promise callbacks and the React updates they schedule, with
// no wall-clock wait. A test that asserts something did NOT happen calls this
// first: every mock here settles through microtasks, so anything that was
// going to fire has fired by the time it returns.
async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
  })
}

function row(key: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-session-row="${key}"][data-session-scope="list"]`)
  if (!el) throw new Error(`no list-scope row for ${key}`)
  return el
}

beforeEach(() => {
  localStorage.clear()
  chatConfig.tagColumnsEnabled = false
  setPinnedOrder.mockReset().mockImplementation(async (keys: string[]) => ({ ok: true, order: keys }))
  chatSlots.mockReset().mockResolvedValue([])
  setSlotPin.mockReset().mockImplementation(async (_key: string, pinned: boolean) => ({ ok: true, pinned }))
})
afterEach(() => vi.clearAllMocks())

describe('sessionRowNav', () => {
  /**
   * Built with createElement rather than an HTML-string template: the blocking
   * `frontend-security` AUTOSDE rule forbids assigning to the innerHTML property
   * anywhere under `src/**`, with no test exemption.
   */
  function mkRow(key: string, scope: string): HTMLElement {
    const el = document.createElement('div')
    el.dataset.sessionRow = key
    el.dataset.sessionScope = scope
    el.tabIndex = 0
    return el
  }

  function scopedDom(): HTMLElement {
    const host = document.createElement('div')
    host.append(mkRow('a', 'list'), mkRow('b', 'board-1'), mkRow('c', 'list'))
    document.body.appendChild(host)
    return host
  }

  afterEach(() => { document.body.replaceChildren() })

  it('collects only the rows sharing the scope, in DOM order', () => {
    const host = scopedDom()
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(sessionRowsInScope(first).map(el => el.dataset.sessionRow)).toEqual(['a', 'c'])
  })

  it('steps past a foreign-scope row rather than into it', () => {
    const host = scopedDom()
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(siblingSessionRow(first, 1)?.dataset.sessionRow).toBe('c')
  })

  it('clamps at both ends instead of wrapping', () => {
    const host = scopedDom()
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    const last = host.querySelector<HTMLElement>('[data-session-row="c"]')!
    expect(siblingSessionRow(first, -1)).toBeNull()
    expect(siblingSessionRow(last, 1)).toBeNull()
    expect(focusSiblingSessionRow(last, 1)).toBe(false)
  })

  it('skips rows hidden inside an inert subtree (a collapsed folder)', () => {
    // A collapsed folder keeps its rows mounted under an [inert] wrapper, so
    // without the filter ArrowDown would stall on an unfocusable row.
    const host = document.createElement('div')
    const folder = document.createElement('div')
    folder.setAttribute('inert', '')
    folder.append(mkRow('hidden', 'list'))
    host.append(mkRow('a', 'list'), folder, mkRow('c', 'list'))
    document.body.appendChild(host)
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(sessionRowsInScope(first).map(el => el.dataset.sessionRow)).toEqual(['a', 'c'])
    expect(siblingSessionRow(first, 1)?.dataset.sessionRow).toBe('c')
  })

  /**
   * A row in a folder carries `scroll-margin-top` for the pinned header stack
   * above it. jsdom has no layout, so the row and lane rects are stubbed: the
   * lane's top edge is at 100 and the row either sits under the headers (17px
   * into a 56px margin) or clear of them (80px in).
   */
  function pinnedLaneDom(rowOffset: number, margin: string, scrollPadding = '') {
    const lane = document.createElement('div')
    lane.style.overflowY = 'auto'
    if (scrollPadding) lane.style.scrollPaddingTop = scrollPadding
    const from = mkRow('a', 'list')
    const to = mkRow('b', 'list')
    to.style.scrollMarginTop = margin
    lane.append(to, from)
    document.body.appendChild(lane)
    lane.getBoundingClientRect = () => ({ top: 100 } as DOMRect)
    to.getBoundingClientRect = () => ({ top: 100 + rowOffset } as DOMRect)
    const scrolls: Array<ScrollIntoViewOptions | boolean | undefined> = []
    to.scrollIntoView = (arg?: ScrollIntoViewOptions | boolean) => { scrolls.push(arg) }
    return { from, scrolls }
  }

  it('re-aligns a row the pinned folder headers cover', () => {
    const { from, scrolls } = pinnedLaneDom(17, '56px')
    expect(focusSiblingSessionRow(from, -1)).toBe(true)
    expect(scrolls).toEqual([{ block: 'nearest' }, { block: 'start' }])
  })

  it('counts the pinned band from the lane\'s scroll-padding, where the floating search dock pushes the headers', () => {
    // Dock 44px tall: the headers pin just above the padding edge, so a row 60px
    // in is still under a 56px stack (band ends at 44 + 56 = 100).
    const covered = pinnedLaneDom(60, '56px', '44px')
    expect(focusSiblingSessionRow(covered.from, -1)).toBe(true)
    expect(covered.scrolls).toEqual([{ block: 'nearest' }, { block: 'start' }])
    document.body.replaceChildren()
    const clear = pinnedLaneDom(104, '56px', '44px')
    expect(focusSiblingSessionRow(clear.from, -1)).toBe(true)
    expect(clear.scrolls).toEqual([{ block: 'nearest' }])
  })

  it('leaves a row clear of the pinned headers, or with no margin, where nearest put it', () => {
    const clear = pinnedLaneDom(80, '56px')
    expect(focusSiblingSessionRow(clear.from, -1)).toBe(true)
    expect(clear.scrolls).toEqual([{ block: 'nearest' }])
    document.body.replaceChildren()
    const unfiled = pinnedLaneDom(0, '')
    expect(focusSiblingSessionRow(unfiled.from, -1)).toBe(true)
    expect(unfiled.scrolls).toEqual([{ block: 'nearest' }])
  })
})

describe('chat sidebar — session list arrow navigation', () => {
  it('ArrowDown moves focus to the next row', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k1').focus()
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(row('k2'))
  })

  it('ArrowUp moves focus to the previous row', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k2').focus()
    fireEvent.keyDown(row('k2'), { key: 'ArrowUp' })
    expect(document.activeElement).toBe(row('k1'))
  })

  it('roving does not switch the session — only Enter does', async () => {
    const { findByText, store } = renderSidebar(THREE)
    await findByText('third')
    row('k1').focus()
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown' })
    fireEvent.keyDown(row('k2'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(row('k3'))
    expect(store.getState().chat.activeSlot).toBeNull()
    // Enter is still claimed by the row (the activation path), unchanged.
    expect(fireEvent.keyDown(row('k3'), { key: 'Enter' })).toBe(false)
  })

  it('leaves the arrow alone at the last row so the list can still scroll', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k3').focus()
    // fireEvent returns false when the handler called preventDefault.
    expect(fireEvent.keyDown(row('k3'), { key: 'ArrowDown' })).toBe(true)
    expect(document.activeElement).toBe(row('k3'))
  })

  it('ignores a modified arrow so Alt+arrow session cycling still reaches its handler', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k1').focus()
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    expect(document.activeElement).toBe(row('k1'))
  })

  it('stores the natural pinned order once after authoritative slots load', async () => {
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, last_ts: '2026-01-01T00:00:00Z' },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, last_ts: '2026-02-01T00:00:00Z' },
    ]
    const { findByText, rerenderSlots } = renderSidebar(pins)
    await findByText('newer')
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k2', 'k1'], true))
    rerenderSlots(pins.map(p => ({ ...p })))
    await settle()
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
  })

  it('seeds the stored order when pins arrive after an initially empty roster', async () => {
    const { rerenderSlots } = renderSidebar([])
    await settle()
    expect(setPinnedOrder).not.toHaveBeenCalled()
    rerenderSlots([
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, last_ts: '2026-01-01T00:00:00Z' },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, last_ts: '2026-02-01T00:00:00Z' },
    ])
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k2', 'k1'], true))
  })

  it('hands the legacy browser order to an unranked gateway once, then clears it', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'gone', 'k2']))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, last_ts: '2026-01-01T00:00:00Z' },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, last_ts: '2026-02-01T00:00:00Z' },
    ]
    renderSidebar(pins)
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k1', 'gone', 'k2'], true))
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
  })

  it('hands over the whole legacy order even when the gateway has not restored those sessions yet', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    // A gateway still restoring its sessions answers a partial list: here
    // only k2 is back. The order must reach the gateway with k1 in place.
    renderSidebar([{ key: 'k2', title: 'newer', running: false, messages: 1, pinned: true }])
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k1', 'k2'], true))
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
  })

  it('drops the legacy browser order when the conditional hand-off finds an order', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    setPinnedOrder.mockRejectedValue(Object.assign(new Error('exists'), { status: 409 }))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true },
    ]
    renderSidebar(pins)
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
  })

  it('reports a failed hand-off and keeps the legacy order for the next load, without retrying', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    setPinnedOrder.mockRejectedValue(Object.assign(new Error('write failed'), { status: 500 }))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true },
    ]
    const { rerenderSlots, findByText, queryByText } = renderSidebar(pins)
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(1))
    // The person's words, not the server's log line.
    expect(await findByText(/Your pinned order wasn't saved yet/)).toBeInTheDocument()
    expect(queryByText(/write failed/)).toBeNull()
    rerenderSlots(pins.map(p => ({ ...p })))
    rerenderSlots(pins.map(p => ({ ...p })))
    await settle()
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem('mc-pinned-session-order')).not.toBeNull()
  })

  it('drops the legacy browser order once the gateway confirms it already has one', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    setPinnedOrder.mockRejectedValue(Object.assign(new Error('exists'), { status: 409 }))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, pin_rank: 0 },
    ]
    renderSidebar(pins)
    // Ranked rows are not proof of a stored order (an optimistic drag paints
    // them too), so the copy goes through the conditional hand-off.
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k1', 'k2'], true))
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
  })

  it('keeps the legacy browser order when ranked rows were never stored and the hand-off fails', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    setPinnedOrder.mockRejectedValue(Object.assign(new Error('write failed'), { status: 500 }))
    // Optimistic ranks from a drag whose POST failed look like this too.
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, pin_rank: 0 },
    ]
    const { findByText } = renderSidebar(pins)
    expect(await findByText(/Your pinned order wasn't saved yet/)).toBeInTheDocument()
    await settle()
    expect(localStorage.getItem('mc-pinned-session-order')).not.toBeNull()
  })

  it('says why a reorder failed and re-reads the gateway ranks instead of restoring its own', async () => {
    setPinnedOrder.mockRejectedValue(new Error('gateway unavailable'))
    const gatewayRows = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
    ]
    chatSlots.mockResolvedValue(gatewayRows)
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    const { findByText, queryByText, store } = renderSidebar(pins)
    await findByText('second pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    // It says the rows went back, in the person's words, not the server's.
    expect(await findByText(/The move wasn't saved, so the last saved order is back/)).toBeInTheDocument()
    expect(queryByText(/gateway unavailable/)).toBeNull()
    // The ranks come from the gateway's answer, not from a client-held copy.
    await waitFor(() => expect(chatSlots).toHaveBeenCalled())
    await waitFor(() => {
      const ranks = Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
      expect(ranks).toEqual({ k1: 1, k2: 0 })
    })
  })

  it('says the pinned set changed when a reorder meets a 409', async () => {
    setPinnedOrder.mockRejectedValue(Object.assign(new Error('a session in the reorder is gone or no longer pinned'), { status: 409 }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    chatSlots.mockResolvedValue(pins)
    const { findByText, queryByText } = renderSidebar(pins)
    await findByText('second pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    expect(await findByText(/Your pinned sessions changed while you were moving one/)).toBeInTheDocument()
    expect(queryByText(/no longer pinned/)).toBeNull()
  })

  it('paints only the last queued write’s answer when two reorders succeed back to back', async () => {
    const answers: Array<(value: { ok: boolean; order: string[] }) => void> = []
    setPinnedOrder.mockImplementation(() => new Promise(resolve => { answers.push(resolve) }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k3', title: 'third pin', running: false, messages: 1, pinned: true, pin_rank: 2 },
    ]
    const { findByText, store } = renderSidebar(pins)
    await findByText('third pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    fireEvent.keyDown(row('k3'), { key: 'ArrowUp', altKey: true })
    await waitFor(() => expect(answers).toHaveLength(1))
    // The first write's answer arrives while the second gesture is queued: it
    // is older than what the sidebar shows, so it must not repaint.
    const painted = Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
    answers[0]({ ok: true, order: ['k2', 'k1', 'k3'] })
    await waitFor(() => expect(answers).toHaveLength(2))
    expect(Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))).toEqual(painted)
    answers[1]({ ok: true, order: ['k1', 'k3', 'k2'] })
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'k3')?.pin_rank).toBe(1))
  })

  it('lets a queued reorder, not a re-read, settle the ranks after an earlier write fails', async () => {
    let rejectFirst: (reason: unknown) => void = () => undefined
    let resolveSecond: (value: { ok: boolean; order: string[] }) => void = () => undefined
    setPinnedOrder
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveSecond = resolve }))
    let answerReread: (rows: unknown[]) => void = () => undefined
    chatSlots.mockImplementation(() => new Promise(resolve => { answerReread = resolve }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    const { findByText, store } = renderSidebar(pins)
    await findByText('second pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    fireEvent.keyDown(row('k1'), { key: 'ArrowUp', altKey: true })
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(1))
    rejectFirst(new Error('gateway unavailable'))
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(2))
    // A later gesture is queued, so the failed write does not re-read: the
    // queued write's answer is newer than anything a re-read would return.
    expect(chatSlots).not.toHaveBeenCalled()
    resolveSecond({ ok: true, order: ['k1', 'k2'] })
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'k2')?.pin_rank).toBe(1))
    answerReread(pins)
    await settle()
    const ranks = Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
    expect(ranks).toEqual({ k1: 0, k2: 1 })
  })

  it('sends reorders one at a time, in gesture order', async () => {
    const answers: Array<(value: { ok: boolean; order: string[] }) => void> = []
    setPinnedOrder.mockImplementation(() => new Promise(resolve => { answers.push(resolve) }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    const { findByText } = renderSidebar(pins)
    await findByText('second pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    fireEvent.keyDown(row('k1'), { key: 'ArrowUp', altKey: true })
    await waitFor(() => expect(answers).toHaveLength(1))
    await settle()
    expect(answers).toHaveLength(1)
    answers[0]({ ok: true, order: ['k2', 'k1'] })
    await waitFor(() => expect(answers).toHaveLength(2))
    expect(setPinnedOrder.mock.calls.map(call => call[0])).toEqual([['k2', 'k1'], ['k1', 'k2']])
    answers[1]({ ok: true, order: ['k1', 'k2'] })
  })

  it('derives the next gesture from the queued order when a stale slots frame lands mid-write', async () => {
    const answers: Array<(value: { ok: boolean; order: string[] }) => void> = []
    setPinnedOrder.mockImplementation(() => new Promise(resolve => { answers.push(resolve) }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k3', title: 'third pin', running: false, messages: 1, pinned: true, pin_rank: 2 },
    ]
    const { findByText, rerenderSlots } = renderSidebar(pins)
    await findByText('third pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    await waitFor(() => expect(answers).toHaveLength(1))
    // A slots frame serialized before the write landed still carries the old
    // ranks. The next gesture must build on the queued order, not on these.
    rerenderSlots(pins.map(p => ({ ...p })))
    fireEvent.keyDown(row('k3'), { key: 'ArrowUp', altKey: true })
    answers[0]({ ok: true, order: ['k2', 'k1', 'k3'] })
    await waitFor(() => expect(answers).toHaveLength(2))
    expect(setPinnedOrder.mock.calls.map(call => call[0])).toEqual([['k2', 'k1', 'k3'], ['k2', 'k3', 'k1']])
    answers[1]({ ok: true, order: ['k2', 'k3', 'k1'] })
  })

  it('keeps a newer slots frame over a slower answer to its own reorder', async () => {
    const pins = [
      { key: 'k1', title: 'first', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    const { findByText, store } = renderSidebar(pins)
    await findByText('second')
    let answer: (value: unknown) => void = () => {}
    setPinnedOrder.mockImplementation(() => new Promise(resolve => { answer = resolve }))
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k2', 'k1'], false))

    // Another browser's reorder lands after this one and its frame arrives
    // first; this write's own answer, built before that, arrives last.
    act(() => { store.dispatch(sseSlots([{ ...pins[0], pin_rank: 0, pin_rev: 6 }, { ...pins[1], pin_rank: 1, pin_rev: 6 }])) })
    await act(async () => { answer({ ok: true, order: ['k2', 'k1'], rev: 5 }) })

    const ranks = Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
    expect(ranks).toEqual({ k1: 0, k2: 1 })
  })

  it('keeps an answered reorder over a slots frame serialized before it', () => {
    const pins = [
      { key: 'k1', title: 'first', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    const { store } = renderSidebar(pins)
    const ranks = () => Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
    act(() => { store.dispatch(sseSlots(pins.map(p => ({ ...p, pin_rev: 1 })))) })
    // A frame from another session's creation, serialized before the write,
    // lands between the send and the answer; the answer still applies.
    act(() => { store.dispatch(setPinRanks({ order: ['k2', 'k1'], rev: 2 })) })
    expect(ranks()).toEqual({ k1: 1, k2: 0 })
    // That old frame, or a one-row patch of the same age, arriving after the
    // answer cannot bring the old ranks back.
    act(() => { store.dispatch(sseSlots(pins.map(p => ({ ...p, pin_rev: 1 })))) })
    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: 'k1', pin_rank: 0, pin_rev: 1 }] } as never)) })
    expect(ranks()).toEqual({ k1: 1, k2: 0 })
    expect(store.getState().dashboard.slots.every(s => !('pin_rev' in s))).toBe(true)
    // A newer frame is the gateway's later order and replaces it.
    act(() => { store.dispatch(sseSlots(pins.map(p => ({ ...p, pin_rev: 3 })))) })
    expect(ranks()).toEqual({ k1: 0, k2: 1 })
  })

  it('shows a reorder failure in a sidebar reopened while the write was in flight', async () => {
    let fail: (reason: unknown) => void = () => {}
    setPinnedOrder.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
    ]
    const { findByText, remount } = renderSidebar(pins)
    await findByText('second pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(1))
    const reopened = remount(pins)
    await reopened.findByText('second pin')
    await act(async () => { fail(new Error('order write failed')) })
    expect(await reopened.findByText(/The move wasn't saved, so the last saved order is back/)).toBeInTheDocument()
  })

  it('builds on the queued gesture after a remount even when an earlier answer lands', async () => {
    const answers: Array<(value: unknown) => void> = []
    setPinnedOrder.mockImplementation(() => new Promise(resolve => { answers.push(resolve) }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k3', title: 'third pin', running: false, messages: 1, pinned: true, pin_rank: 2 },
    ]
    const { findByText, remount, store } = renderSidebar(pins)
    await findByText('third pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    await waitFor(() => expect(answers).toHaveLength(1))
    fireEvent.keyDown(row('k3'), { key: 'ArrowUp', altKey: true })
    const reopened = remount(pins)
    await reopened.findByText('third pin')
    // The first write's answer lands while the second is still queued.
    await act(async () => { answers[0]({ ok: true, order: ['k2', 'k1', 'k3'], rev: 2 }) })
    await waitFor(() => expect(answers).toHaveLength(2))
    expect(store.getState().dashboard.pendingPinOrder).toEqual(['k2', 'k3', 'k1'])
    // The next gesture moves k2 down from the queued [k2, k3, k1].
    fireEvent.keyDown(row('k2'), { key: 'ArrowDown', altKey: true })
    answers[1]({ ok: true, order: ['k2', 'k3', 'k1'], rev: 3 })
    await waitFor(() => expect(answers).toHaveLength(3))
    expect(setPinnedOrder.mock.calls[2][0]).toEqual(['k3', 'k2', 'k1'])
    answers[2]({ ok: true, order: ['k3', 'k2', 'k1'], rev: 4 })
  })

  it('keeps gesture order across a sidebar remount while writes are queued', async () => {
    const answers: Array<(value: unknown) => void> = []
    setPinnedOrder.mockImplementation(() => new Promise(resolve => { answers.push(resolve) }))
    const pins = [
      { key: 'k1', title: 'first pin', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second pin', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k3', title: 'third pin', running: false, messages: 1, pinned: true, pin_rank: 2 },
    ]
    const { findByText, remount } = renderSidebar(pins)
    await findByText('third pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    await waitFor(() => expect(answers).toHaveLength(1))
    fireEvent.keyDown(row('k3'), { key: 'ArrowUp', altKey: true })
    const reopened = remount(pins)
    await reopened.findByText('third pin')
    fireEvent.keyDown(row('k2'), { key: 'ArrowDown', altKey: true })
    await settle()
    // The reopened sidebar's gesture waits behind the closed one's queue.
    expect(answers).toHaveLength(1)
    answers[0]({ ok: true, order: ['k2', 'k1', 'k3'], rev: 2 })
    await waitFor(() => expect(answers).toHaveLength(2))
    answers[1]({ ok: true, order: ['k2', 'k3', 'k1'], rev: 3 })
    await waitFor(() => expect(answers).toHaveLength(3))
    const sent = setPinnedOrder.mock.calls.map(call => call[0])
    expect(sent.slice(0, 2)).toEqual([['k2', 'k1', 'k3'], ['k2', 'k3', 'k1']])
    answers[2]({ ok: true, order: sent[2], rev: 4 })
  })

  it('holds a reorder until a pin the person made first has reached the gateway', async () => {
    const pins = [
      { key: 'k1', title: 'first', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'second', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k3', title: 'third', running: false, messages: 1 },
    ]
    const { findByText, store, qc } = renderSidebar(pins)
    await findByText('third')
    let releasePin: (value: unknown) => void = () => {}
    setSlotPin.mockImplementation(() => new Promise(resolve => { releasePin = resolve }))
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}><Provider store={store}>{children}</Provider></QueryClientProvider>
    )
    const { result } = renderHook(() => useSessionActions(), { wrapper })
    act(() => result.current.togglePin('k3'))
    await waitFor(() => expect(setSlotPin).toHaveBeenCalledWith('k3', true))

    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    // The reorder names k1 and k2 only, but it still waits: the pin PATCH is
    // older and must land first.
    await settle()
    expect(setPinnedOrder).not.toHaveBeenCalled()

    await act(async () => { releasePin({ ok: true, pinned: true }) })
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(1))
  })

  it('sends each pinned row\'s created token with the reorder', async () => {
    const pins = [
      { key: 'k1', title: 'first', running: false, messages: 1, pinned: true, pin_rank: 0, created: '2026-09-01T00:00:00+00:00' },
      { key: 'k2', title: 'second', running: false, messages: 1, pinned: true, pin_rank: 1, created: '2026-09-02T00:00:00+00:00' },
    ]
    const { findByText } = renderSidebar(pins)
    await findByText('second')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k2', 'k1'], false, {
      k1: '2026-09-01T00:00:00+00:00', k2: '2026-09-02T00:00:00+00:00',
    }))
  })

  it('reorders against the next rendered pin instead of a filtered-out peer', async () => {
    const pins = [
      { key: 'k1', title: 'keep first', running: false, messages: 1, pinned: true, pin_rank: 0 },
      { key: 'k2', title: 'drop second', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k3', title: 'keep third', running: false, messages: 1, pinned: true, pin_rank: 2 },
    ]
    const { findByText, getByPlaceholderText, queryByText } = renderSidebar(pins)
    await findByText('drop second')

    fireEvent.change(getByPlaceholderText(/search/i), { target: { value: 'keep' } })
    await waitFor(() => expect(queryByText('drop second')).toBeNull())
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })

    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k2', 'k3', 'k1'], false))
  })
})

/**
 * A board column's foldered and ungrouped rows are ONE visible list, so the rove
 * has to cross the folder boundary. The row's `scope` stays per-folder (Framer
 * layoutId + rename target uniqueness), so the nav scope is threaded separately.
 */
describe('chat sidebar — board column arrow navigation', () => {
  const TAG = '11111111-1111-1111-1111-111111111111'
  const COL = 'col-aaaa'
  const FOLDER = 'folder-zzzz'
  const boardTags = [{ id: TAG, name: 'Blocked', color: '#e11', order: 0, status: true }]
  const boardColumns = [{ id: COL, name: 'Blocked', tag_ids: [TAG], mode: 'any', order: 0 }]
  const boardFolders = [{ id: FOLDER, name: 'CDF', order: 0, collapsed: false }]
  const boardSlots = [
    { key: 'b1', title: 'in folder', running: false, messages: 1, tags: [TAG], folder_id: FOLDER, pinned: true },
    { key: 'b2', title: 'at column root', running: false, messages: 1, tags: [TAG], pinned: true },
  ]

  function renderBoard() {
    const store = createTestStore({
      dashboard: {
        status: {}, connected: true, slots: boardSlots, approvalMode: 'normal',
        channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
        slotsLoaded: true,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      } as unknown as RootState['dashboard'],
      chat: { activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    qc.setQueryData(['chat-tags'], boardTags)
    qc.setQueryData(['tag-columns'], boardColumns)
    qc.setQueryData(['chat-folders'], boardFolders)
    const view = (slots: typeof boardSlots) => (
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={slots} activeSlot={null} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>
    )
    const utils = render(view(boardSlots))
    return { ...utils, rerenderSlots: (slots: typeof boardSlots) => utils.rerender(view(slots)) }
  }

  beforeEach(() => {
    chatConfig.tagColumnsEnabled = true
    fixtures.chatTags = boardTags
    fixtures.tagColumns = boardColumns
    fixtures.chatFolders = boardFolders
  })
  afterEach(() => {
    chatConfig.tagColumnsEnabled = false
    fixtures.chatTags = []
    fixtures.tagColumns = []
    fixtures.chatFolders = []
  })

  it('scopes a foldered board row to its column, not its folder', async () => {
    const { findByText } = renderBoard()
    await findByText('in folder')
    const foldered = document.querySelector<HTMLElement>('[data-session-row="b1"]')!
    const rooted = document.querySelector<HTMLElement>('[data-session-row="b2"]')!
    expect(foldered.dataset.sessionScope).toBe(COL)
    expect(rooted.dataset.sessionScope).toBe(COL)
    // …and the rove therefore reaches across the boundary in one step.
    expect(siblingSessionRow(foldered, 1)).toBe(rooted)
  })

  it('does not advertise or execute pinned ordering in a board projection', async () => {
    const { findByText } = renderBoard()
    await findByText('in folder')
    const foldered = document.querySelector<HTMLElement>('[data-session-row="b1"]')!
    expect(foldered.getAttribute('aria-keyshortcuts')).toBeNull()

    fireEvent.keyDown(foldered, { key: 'ArrowDown', altKey: true })

    // A board view neither reorders nor seeds the natural order, as on main.
    await settle()
    expect(setPinnedOrder).not.toHaveBeenCalled()
  })
})
