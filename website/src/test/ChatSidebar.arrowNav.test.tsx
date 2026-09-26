/**
 * Arrow-key roving over the sidebar session list.
 *
 * Covers the two halves separately: the scope-bounded/clamped index maths as a
 * plain-DOM unit test, and the wiring (bare arrows only, focus-only rove, Enter
 * still activates) against a rendered ChatSidebar.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
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

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'setPinnedOrder') return setPinnedOrder
      if (prop === 'chatSlots') return chatSlots
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
  return { ...utils, store, rerenderSlots: (nextSlots: ChatSlot[]) => utils.rerender(view(nextSlots)) }
}

const THREE = [
  { key: 'k1', title: 'first', running: false, messages: 1 },
  { key: 'k2', title: 'second', running: false, messages: 1 },
  { key: 'k3', title: 'third', running: false, messages: 1 },
]

/** The focusable row element for a slot key, in list scope. */
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

  it('stores no order before the person reorders', async () => {
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, last_ts: '2026-01-01T00:00:00Z' },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, last_ts: '2026-02-01T00:00:00Z' },
    ]
    const { findByText } = renderSidebar(pins)
    await findByText('newer')
    expect(setPinnedOrder).not.toHaveBeenCalled()
  })

  it('hands the legacy browser order to an unranked gateway once, then clears it', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'gone', 'k2']))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, last_ts: '2026-01-01T00:00:00Z' },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, last_ts: '2026-02-01T00:00:00Z' },
    ]
    renderSidebar(pins)
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k1', 'k2'], true))
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
  })

  it('keeps the legacy order while the gateway still reports no pinned session', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    const { rerenderSlots } = renderSidebar([])
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(setPinnedOrder).not.toHaveBeenCalled()
    expect(localStorage.getItem('mc-pinned-session-order')).not.toBeNull()
    rerenderSlots([
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true },
    ])
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledWith(['k1', 'k2'], true))
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
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

  it('keeps the legacy order for the next load when the hand-off fails, without retrying', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    setPinnedOrder.mockRejectedValue(Object.assign(new Error('write failed'), { status: 500 }))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true },
    ]
    const { rerenderSlots } = renderSidebar(pins)
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(1))
    rerenderSlots(pins.map(p => ({ ...p })))
    rerenderSlots(pins.map(p => ({ ...p })))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(setPinnedOrder).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem('mc-pinned-session-order')).not.toBeNull()
  })

  it('drops the legacy browser order when the gateway already has one', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['k1', 'k2']))
    const pins = [
      { key: 'k1', title: 'older', running: false, messages: 1, pinned: true, pin_rank: 1 },
      { key: 'k2', title: 'newer', running: false, messages: 1, pinned: true, pin_rank: 0 },
    ]
    renderSidebar(pins)
    await waitFor(() => expect(localStorage.getItem('mc-pinned-session-order')).toBeNull())
    expect(setPinnedOrder).not.toHaveBeenCalled()
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
    const { findByText, store } = renderSidebar(pins)
    await findByText('second pin')
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    expect(await findByText(/gateway unavailable/)).toBeInTheDocument()
    // The ranks come from the gateway's answer, not from a client-held copy.
    await waitFor(() => expect(chatSlots).toHaveBeenCalled())
    await waitFor(() => {
      const ranks = Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
      expect(ranks).toEqual({ k1: 1, k2: 0 })
    })
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
    fireEvent.keyDown(row('k2'), { key: 'ArrowUp', altKey: true })
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(1))
    rejectFirst(new Error('gateway unavailable'))
    await waitFor(() => expect(setPinnedOrder).toHaveBeenCalledTimes(2))
    // A later gesture is queued, so the failed write does not re-read: the
    // queued write's answer is newer than anything a re-read would return.
    expect(chatSlots).not.toHaveBeenCalled()
    resolveSecond({ ok: true, order: ['k2', 'k1'] })
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'k2')?.pin_rank).toBe(0))
    answerReread(pins)
    await new Promise(resolve => setTimeout(resolve, 20))
    const ranks = Object.fromEntries(store.getState().dashboard.slots.map(s => [s.key, s.pin_rank]))
    expect(ranks).toEqual({ k2: 0, k1: 1 })
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
    // The props rows stay at the old ranks, so both keys issue a request.
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    fireEvent.keyDown(row('k2'), { key: 'ArrowUp', altKey: true })
    await waitFor(() => expect(answers).toHaveLength(1))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(answers).toHaveLength(1)
    answers[0]({ ok: true, order: ['k2', 'k1'] })
    await waitFor(() => expect(answers).toHaveLength(2))
    expect(setPinnedOrder.mock.calls.map(call => call[0])).toEqual([['k2', 'k1'], ['k2', 'k1']])
    answers[1]({ ok: true, order: ['k2', 'k1'] })
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

    expect(setPinnedOrder).not.toHaveBeenCalled()
  })
})
