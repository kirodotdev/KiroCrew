/**
 * Board view: global session filters (Unread / Running / Pinned / Recent)
 * must narrow the rendered set of sessions in tag-column (board) mode.
 *
 * Regression test for GitHub issue #739.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn } from '../types'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children)
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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, { get: () => vi.fn().mockResolvedValue([]) }),
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

// --- Test data ---
const TAG_BUG = 'tag-bug'
const TAG_FEAT = 'tag-feat'
const COL_BUG = 'col-bug'
const COL_FEAT = 'col-feat'

const tags: ChatTag[] = [
  { id: TAG_BUG, name: 'Bug', color: '#e11', order: 0, status: false },
  { id: TAG_FEAT, name: 'Feature', color: '#1a1', order: 1, status: false },
]

const columns: TagColumn[] = [
  { id: COL_BUG, name: 'Bugs', tag_ids: [TAG_BUG], mode: 'any', order: 0 },
  { id: COL_FEAT, name: 'Features', tag_ids: [TAG_FEAT], mode: 'any', order: 1 },
]

const NOW = new Date(Date.now() - 5 * 60 * 1000).toISOString()  // 5 minutes ago (within 1h default window)
const OLD = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString()  // 3 hours ago (outside 1h default window)

const allSlots = [
  { key: 's-1', title: 'Fix auth bug', running: true, pinned: true, tags: [TAG_BUG], created: NOW, last_ts: NOW },
  { key: 's-2', title: 'Fix CSS bug', running: false, pinned: false, tags: [TAG_BUG], created: OLD, last_ts: OLD },
  { key: 's-3', title: 'Add feature X', running: false, pinned: true, tags: [TAG_FEAT], created: NOW, last_ts: NOW },
  { key: 's-4', title: 'Add feature Y', running: true, pinned: false, tags: [TAG_FEAT], created: NOW, last_ts: NOW },
  { key: 's-5', title: 'Add feature Z', running: false, pinned: false, tags: [TAG_FEAT], created: OLD, last_ts: OLD },
]

function renderBoard(opts: { unreadSlots?: string[]; filterKey?: string } = {}) {
  const { unreadSlots = [], filterKey } = opts
  // Seed the localStorage filter state BEFORE render
  if (filterKey) {
    const storageKeys: Record<string, string> = {
      unread: 'mc-session-unread-only',
      running: 'mc-session-running-only',
      pinned: 'mc-session-pinned-only',
      recent: 'mc-session-recent-only',
    }
    localStorage.setItem(storageKeys[filterKey], '1')
  }
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: allSlots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, goalLoops: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={allSlots} activeSlot={null} unreadSlots={unreadSlots}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function slotsInColumn(container: HTMLElement, columnId: string): string[] {
  const col = container.querySelector(`[data-testid="column-${columnId}"]`)
  if (!col) return []
  return Array.from(col.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key')!)
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('Board view: global session filters', () => {
  it('without filters, all tagged sessions appear in their columns', () => {
    const { container } = renderBoard()
    expect(slotsInColumn(container, COL_BUG)).toEqual(['s-1', 's-2'])
    expect(slotsInColumn(container, COL_FEAT)).toEqual(['s-3', 's-4', 's-5'])
  })

  it('Unread filter: only unread sessions appear', () => {
    const { container } = renderBoard({ unreadSlots: ['s-1', 's-4'], filterKey: 'unread' })
    expect(slotsInColumn(container, COL_BUG)).toEqual(['s-1'])
    expect(slotsInColumn(container, COL_FEAT)).toEqual(['s-4'])
  })

  it('Running filter: only running sessions appear', () => {
    const { container } = renderBoard({ filterKey: 'running' })
    expect(slotsInColumn(container, COL_BUG)).toEqual(['s-1'])
    expect(slotsInColumn(container, COL_FEAT)).toEqual(['s-4'])
  })

  it('Pinned filter: only pinned sessions appear', () => {
    const { container } = renderBoard({ filterKey: 'pinned' })
    expect(slotsInColumn(container, COL_BUG)).toEqual(['s-1'])
    expect(slotsInColumn(container, COL_FEAT)).toEqual(['s-3'])
  })

  it('Recent filter: only sessions within the recent window appear', () => {
    // s-1, s-3, s-4 have last_ts = NOW (within default 24h window)
    // s-2, s-5 have last_ts = OLD (outside window)
    const { container } = renderBoard({ filterKey: 'recent' })
    expect(slotsInColumn(container, COL_BUG)).toEqual(['s-1'])
    expect(slotsInColumn(container, COL_FEAT)).toEqual(['s-3', 's-4'])
  })

  it('combination: Pinned + Running shows only sessions matching either', () => {
    // Pinned: s-1, s-3. Running: s-1, s-4. Union: s-1, s-3, s-4
    localStorage.setItem('mc-session-pinned-only', '1')
    localStorage.setItem('mc-session-running-only', '1')
    const store = createTestStore({
      dashboard: {
        status: {}, connected: false, slots: allSlots, approvalMode: 'normal',
        channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      } as RootState['dashboard'],
      chat: { activeSlot: null, slotStatusDetail: {}, goalLoops: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['chat-tags'], tags)
    qc.setQueryData(['tag-columns'], columns)
    qc.setQueryData(['chat-folders'], [])
    const { container } = render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={allSlots} activeSlot={null} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    // s-1 is both pinned and running (Bug column)
    // s-3 is pinned (Feature column)
    // s-4 is running (Feature column)
    expect(slotsInColumn(container, COL_BUG)).toEqual(['s-1'])
    expect(slotsInColumn(container, COL_FEAT)).toEqual(['s-3', 's-4'])
  })

  it('filters apply even to columns with empty tag_ids (no column-level filter)', () => {
    // Columns with tag_ids: [] match all slots, so the global filter is the sole gate
    const COL_ALL_A = 'col-all-a'
    const COL_ALL_B = 'col-all-b'
    const unfilteredColumns: TagColumn[] = [
      { id: COL_ALL_A, name: 'Everything A', tag_ids: [], mode: 'any', order: 0 },
      { id: COL_ALL_B, name: 'Everything B', tag_ids: [], mode: 'any', order: 1 },
    ]
    localStorage.setItem('mc-session-running-only', '1')
    const store = createTestStore({
      dashboard: {
        status: {}, connected: false, slots: allSlots, approvalMode: 'normal',
        channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      } as RootState['dashboard'],
      chat: { activeSlot: null, slotStatusDetail: {}, goalLoops: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['chat-tags'], tags)
    qc.setQueryData(['tag-columns'], unfilteredColumns)
    qc.setQueryData(['chat-folders'], [])
    const { container } = render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={allSlots} activeSlot={null} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    // Only running sessions (s-1, s-4) should show in both unfiltered columns
    expect(slotsInColumn(container, COL_ALL_A)).toEqual(['s-1', 's-4'])
    expect(slotsInColumn(container, COL_ALL_B)).toEqual(['s-1', 's-4'])
  })
})
