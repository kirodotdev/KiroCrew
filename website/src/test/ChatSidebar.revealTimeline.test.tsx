/**
 * Chat sidebar — the reveal pipeline, on its clock.
 *
 * A reveal is a store-held request (`chat.revealRequest`: kind, target, nonce)
 * that the sidebar consumes, clears whatever hides the target, then scrolls the
 * row into view and flashes it, retrying on a bounded budget until the row can
 * be seen. What is pinned here is the TIMELINE of that pipeline:
 *
 *  - a request present at mount is consumed, and a filter hiding its target is
 *    already cleared when the scroll lands;
 *  - the flash holds for 1600 ms, fades, and is gone 500 ms later;
 *  - a row that never becomes visible stops the loop after 20 retries of 100 ms
 *    and leaves a console.debug trace naming the kind and key;
 *  - the scroll is smooth and centred, and not smooth under reduced motion;
 *  - a folder reveal from the flat lane switches to the tree for THIS visit only,
 *    without writing either lane key.
 *
 * Fake timers go on only after the render has settled on real timers, and come
 * off in afterEach whether or not the test threw.
 *
 * Already pinned elsewhere: mount replay of either kind and nonce re-fire
 * (ChatSidebarCoverage, ChatSidebar.folderSearch), the filter registry the
 * session reveal clears (ChatSidebar.revealFilterDimensions), dormant-section
 * pre-expansion (ChatSidebar.revealStaleExpand), inert rows never scrolled
 * (ChatSidebarCoverage).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { requestFolderReveal, requestSlotReveal } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder, TagColumn } from '../types'

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

/** Mutable so one case can put the sidebar in board view. */
const cfg = vi.hoisted(() => ({
  value: { tagColumnsEnabled: false, confirmCloseSession: false } as Record<string, unknown>,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  updateChatFolder: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

/** Flipped per test: the reveal reads the reduced-motion preference at scroll time. */
const motionPref = vi.hoisted(() => ({ reduce: false }))
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: motionPref.reduce && q.includes('prefers-reduced-motion'), media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

type TestSlot = Record<string, unknown>

const FLASH = 'session-reveal-flash'
const FADE = 'session-reveal-flash-fade'
const NEVER_VISIBLE = 'reveal-in-sidebar: row never became visible for'

function renderSidebar(opts: {
  slots: TestSlot[]
  folders?: ChatFolder[]
  revealRequest?: { kind: 'session' | 'folder'; target: string; nonce: number }
  columns?: TagColumn[]
  tags?: Array<Record<string, unknown>>
}) {
  const folders = opts.folders ?? []
  mocks.chatFolders.mockResolvedValue(folders)
  if (opts.columns) {
    cfg.value = { ...cfg.value, tagColumnsEnabled: true }
    mocks.tagColumns.mockResolvedValue(opts.columns)
    mocks.chatTags.mockResolvedValue(opts.tags ?? [])
  }
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: opts.slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
      revealRequest: opts.revealRequest ?? null,
      revealNonce: opts.revealRequest?.nonce ?? 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], opts.columns ?? [])
  qc.setQueryData(['chat-tags'], opts.tags ?? [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={opts.slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
}

const sessionRow = (key: string) => document.querySelector<HTMLElement>(`[data-session-row="${key}"]`)
const neverVisibleCalls = (spy: ReturnType<typeof vi.spyOn>) =>
  spy.mock.calls.filter(args => args[0] === NEVER_VISIBLE)

let scrollIntoView: ReturnType<typeof vi.fn>
let originalScroll: typeof Element.prototype.scrollIntoView

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  motionPref.reduce = false
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false }
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.chatFolders.mockResolvedValue([])
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
  scrollIntoView = vi.fn()
  originalScroll = Element.prototype.scrollIntoView
  Element.prototype.scrollIntoView = scrollIntoView as unknown as typeof Element.prototype.scrollIntoView
})
afterEach(() => {
  vi.useRealTimers()
  Element.prototype.scrollIntoView = originalScroll
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('reveal — consumption and filters', () => {
  it('consumes a request present at mount, and has cleared the filter hiding its target before the scroll lands', async () => {
    // The pinned-only filter hides the target; the search box is empty so the
    // filter is the only thing between the request and the row.
    localStorage.setItem('mc-session-pinned-only', '1')
    const atScroll: Array<{ key: string | null; pinnedOnly: string | null }> = []
    scrollIntoView.mockImplementation(function (this: Element) {
      atScroll.push({ key: this.getAttribute('data-session-row'), pinnedOnly: localStorage.getItem('mc-session-pinned-only') })
    })
    vi.spyOn(console, 'debug').mockImplementation(() => {})
    const { store } = renderSidebar({
      slots: [
        { key: 'k-pinned', title: 'Pinned one', messages: 1, running: false, pinned: true },
        { key: 'k-target', title: 'Unpinned target', messages: 1, running: false },
      ],
      revealRequest: { kind: 'session', target: 'k-target', nonce: 1 },
    })
    expect(store.getState().chat.revealRequest).toBeNull()
    await waitFor(() => expect(atScroll.length).toBeGreaterThan(0))
    expect(atScroll[0]).toEqual({ key: 'k-target', pinnedOnly: '0' })
    // The cleared filter stays cleared: the unpinned target is still listed.
    expect(document.querySelector('[data-session-row="k-target"]')).not.toBeNull()
  })
})

describe('reveal — the scroll', () => {
  it('scrolls smoothly to the centre, and without smoothing under reduced motion', async () => {
    const first = renderSidebar({ slots: [{ key: 'k-a', title: 'Alpha', messages: 1, running: false }] })
    await screen.findByText('Alpha')
    act(() => { first.store.dispatch(requestSlotReveal('k-a')) })
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1))
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' })
    first.unmount()

    scrollIntoView.mockClear()
    motionPref.reduce = true
    const reduced = renderSidebar({ slots: [{ key: 'k-a', title: 'Alpha', messages: 1, running: false }] })
    await screen.findByText('Alpha')
    act(() => { reduced.store.dispatch(requestSlotReveal('k-a')) })
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1))
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'center' })
  })
})

describe('reveal — the flash', () => {
  it('holds the flash for 1600 ms, fades it, and removes it 500 ms later', async () => {
    const { store } = renderSidebar({ slots: [{ key: 'k-a', title: 'Alpha', messages: 1, running: false }] })
    await screen.findByText('Alpha')
    vi.useFakeTimers()

    act(() => { store.dispatch(requestSlotReveal('k-a')) })
    // The row was already on screen, so the first attempt lands at once.
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(sessionRow('k-a')!.classList.contains(FLASH)).toBe(true)
    expect(sessionRow('k-a')!.classList.contains(FADE)).toBe(false)

    act(() => { vi.advanceTimersByTime(1599) })
    expect(sessionRow('k-a')!.classList.contains(FLASH)).toBe(true)
    expect(sessionRow('k-a')!.classList.contains(FADE)).toBe(false)

    act(() => { vi.advanceTimersByTime(1) })
    expect(sessionRow('k-a')!.classList.contains(FLASH)).toBe(true)
    expect(sessionRow('k-a')!.classList.contains(FADE)).toBe(true)

    act(() => { vi.advanceTimersByTime(499) })
    expect(sessionRow('k-a')!.classList.contains(FADE)).toBe(true)

    act(() => { vi.advanceTimersByTime(1) })
    expect(sessionRow('k-a')!.classList.contains(FLASH)).toBe(false)
    expect(sessionRow('k-a')!.classList.contains(FADE)).toBe(false)
  })
})

describe('reveal — the bounded retry', () => {
  it('stops after 20 retries of 100 ms and leaves a trace naming the kind and key', async () => {
    // A board whose only column wants a tag the target does not carry: the target
    // is a real session, so the request is honoured, but no lane ever draws it.
    const tags = [{ id: 't1', name: 'Blocked', color: '#ff0000', order: 0, status: true }]
    const columns = [{ id: 'col-1', name: 'Blocked lane', tag_ids: ['t1'], mode: 'any', order: 0 }] as TagColumn[]
    const { store } = renderSidebar({
      slots: [
        { key: 'k-tagged', title: 'Tagged one', messages: 1, running: false, tags: ['t1'] },
        { key: 'k-untagged', title: 'Untagged one', messages: 1, running: false },
      ],
      columns,
      tags,
    })
    await waitFor(() => expect(sessionRow('k-tagged')).not.toBeNull())
    expect(sessionRow('k-untagged')).toBeNull()
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    vi.useFakeTimers()

    act(() => { store.dispatch(requestSlotReveal('k-untagged')) })
    expect(store.getState().chat.revealRequest).toBeNull()

    act(() => { vi.advanceTimersByTime(20 * 100 - 1) })
    expect(neverVisibleCalls(debug)).toEqual([])

    act(() => { vi.advanceTimersByTime(1) })
    expect(neverVisibleCalls(debug)).toEqual([[NEVER_VISIBLE, 'session', 'k-untagged']])

    // The loop is over: no further attempt, no second trace, never a scroll.
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(neverVisibleCalls(debug)).toHaveLength(1)
    expect(scrollIntoView).not.toHaveBeenCalled()
  })
})

describe('reveal — a folder from the flat lane', () => {
  const FOLDERS: ChatFolder[] = [
    { id: 'fA', name: 'Alpha folder', order: 0 },
    { id: 'fB', name: 'Beta folder', order: 1 },
  ]
  const SLOTS: TestSlot[] = [
    { key: 'k-a', title: 'alpha work', messages: 1, running: false, folder_id: 'fA' },
    { key: 'k-b', title: 'beta work', messages: 1, running: false, folder_id: 'fB' },
  ]

  it('shows the folder in the tree for this visit without writing either lane key', async () => {
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const first = renderSidebar({ slots: SLOTS, folders: FOLDERS })
    expect(screen.getByTestId('flat-view-lane')).toBeInTheDocument()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')

    act(() => { first.store.dispatch(requestFolderReveal('fB')) })
    await waitFor(() => expect(document.querySelector('[data-folder-row="fB"]')).not.toBeNull())
    expect(screen.queryByTestId('flat-view-lane')).toBeNull()
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
    expect(document.querySelector('[data-folder-row="fB"]')!.className).toContain(FLASH)

    const laneWrites = setItem.mock.calls.filter(([k]) => k === 'mc-sidebar-lane' || k === 'mc-sidebar-flat-view')
    expect(laneWrites).toEqual([])
    expect(localStorage.getItem('mc-sidebar-lane')).toBe('flat')
    expect(localStorage.getItem('mc-sidebar-flat-view')).toBeNull()
    first.unmount()

    // The preference was never touched, so the next visit opens flat again.
    renderSidebar({ slots: SLOTS, folders: FOLDERS })
    expect(screen.getByTestId('flat-view-lane')).toBeInTheDocument()
  })
})
