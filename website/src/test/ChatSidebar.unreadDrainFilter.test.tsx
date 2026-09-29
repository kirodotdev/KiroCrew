/**
 * Chat sidebar — the Unread filter turns itself off when the inbox drains.
 *
 * A persisted Unread filter over an empty inbox is an empty list with no
 * explanation, so the sidebar drops the filter (and persists '0') when:
 *  - the first slot list has loaded and the inbox is empty, or
 *  - a non-empty inbox drains to zero.
 * Nothing at all happens before `dashboard.slotsLoaded` is true: an empty inbox
 * then means "not loaded yet", not "nothing unread", and a pre-load transition
 * must not spend the first-load decision.
 *
 * The decision table itself is pinned without a render in unreadDrain.test.ts;
 * this file pins the effect's wiring to that decision through what renders.
 *
 * Also here: filter counts are taken over the rows the list RENDERS, which
 * includes a connected crew's live sessions, not over local sessions alone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import { PREVIEW_INSTANCE_SESSIONS } from '../utils/previewFlags'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  listInstances: vi.fn(),
  instanceChatSlots: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'

const UNREAD_KEY = 'mc-session-unread-only'

const SLOTS = [
  { key: 'k-1', title: 'first session', messages: 1, running: false, last_ts: '2026-01-02T00:00:00Z' },
  { key: 'k-2', title: 'second session', messages: 1, running: false, last_ts: '2026-01-01T00:00:00Z' },
] as unknown as ChatSlot[]

function renderSidebar(opts: { unreadSlots: string[]; slotsLoaded?: boolean; slots?: ChatSlot[] }) {
  const slots = opts.slots ?? SLOTS
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: opts.unreadSlots, updateProgress: null,
      slotsLoaded: opts.slotsLoaded ?? true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const tree = (unread: string[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={unread}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const view = render(tree(opts.unreadSlots))
  return { ...view, store, setUnread: (unread: string[]) => view.rerender(tree(unread)) }
}

const unreadChip = () => screen.queryByRole('button', { name: 'Clear Unread filter' })

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.chatFolders.mockResolvedValue([])
  mocks.listInstances.mockResolvedValue({ instances: [] })
  mocks.instanceChatSlots.mockResolvedValue([])
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('unread filter auto-drain', () => {
  it('does nothing before the first slot list loads, then drains a persisted filter over an empty inbox', async () => {
    localStorage.setItem(UNREAD_KEY, '1')
    const { store, setUnread } = renderSidebar({ unreadSlots: [], slotsLoaded: false })
    expect(unreadChip()).not.toBeNull()

    // Even a drain seen BEFORE the load is not acted on, and does not use up the
    // decision the first loaded frame has to make.
    setUnread(['k-1'])
    setUnread([])
    expect(localStorage.getItem(UNREAD_KEY)).toBe('1')
    expect(unreadChip()).not.toBeNull()

    act(() => { store.dispatch(sseSlots(SLOTS)) })
    await waitFor(() => expect(localStorage.getItem(UNREAD_KEY)).toBe('0'))
    expect(unreadChip()).toBeNull()
  })

  it('turns the filter off when a non-empty inbox drains to zero', async () => {
    localStorage.setItem(UNREAD_KEY, '1')
    const { setUnread } = renderSidebar({ unreadSlots: ['k-1'] })
    expect(unreadChip()?.textContent).toContain('(1)')
    expect(localStorage.getItem(UNREAD_KEY)).toBe('1')

    setUnread([])
    await waitFor(() => expect(localStorage.getItem(UNREAD_KEY)).toBe('0'))
    expect(unreadChip()).toBeNull()
  })

  it('leaves the filter on while anything is still unread', () => {
    localStorage.setItem(UNREAD_KEY, '1')
    const { setUnread } = renderSidebar({ unreadSlots: ['k-1', 'k-2'] })
    setUnread(['k-1'])
    expect(localStorage.getItem(UNREAD_KEY)).toBe('1')
    expect(unreadChip()?.textContent).toContain('(1)')
  })

  it('writes nothing when the filter was never on', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const { setUnread } = renderSidebar({ unreadSlots: ['k-1'] })
    setUnread([])
    expect(setItem.mock.calls.filter(([k]) => k === UNREAD_KEY)).toEqual([])
    expect(localStorage.getItem(UNREAD_KEY)).toBeNull()
  })
})

describe('filter counts follow the rendered rows', () => {
  it('counts a connected crew\'s live row, and never as a second unread for a colliding key', async () => {
    // The peer row shares the local unread session's key. It is the only RECENT
    // row (the local one is months old), and it can never be unread here.
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    localStorage.setItem('mc-session-recent-only', '1')
    mocks.listInstances.mockResolvedValue({ instances: [{ id: 'inst-a', name: 'astro', status: { state: 'connected' } }] })
    mocks.instanceChatSlots.mockResolvedValue([{
      key: 'k-1',
      row_identity: 'inst-a:k-1',
      title: 'REMOTE recent row',
      last_turn_ts: new Date(Date.now() - 30_000).toISOString(),
      created: new Date(Date.now() - 60_000).toISOString(),
    }])
    renderSidebar({ unreadSlots: ['k-1'] })
    await screen.findByText('REMOTE recent row')

    const recentChip = screen.getByRole('button', { name: 'Clear Recent filter' })
    expect(recentChip.textContent).toContain('(1)')
    expect(document.querySelectorAll('[data-session-row]')).toHaveLength(1)
    // The funnel's badge is the Unread count: the local session only.
    expect(screen.getByRole('button', { name: 'Sort and filter sessions' }).textContent).toBe('1')
  })
})
