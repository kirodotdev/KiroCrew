/**
 * Sidebar flat view ("explode chats out of folders") — a view-only toggle
 * that renders every visible session (foldered + unfoldered) in one lane
 * sorted by last activity, for temporal work across many folders.
 *
 * Locks the contract:
 *  (1) The toggle button flips between folder tree and the flat lane.
 *  (2) The flat lane contains foldered AND unfoldered sessions, ordered by
 *      the user's active sort (default: recency desc), respecting pin
 *      priority, filters, and search — same as the tree view.
 *  (3) Foldered rows carry a folder-name annotation.
 *  (4) The choice persists to localStorage and is restored on mount.
 *  (5) Toggling back restores the folder tree (view-only — no data change).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { safeSetItem } from '../utils/safeStorage'
import type { ChatFolder } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
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
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Legacy single-lane list (no tag columns) keeps the rows flat + easy to query.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

// Mutable so individual tests can render with zero folders.
const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
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

// Sessions spread across folders + one unfoldered, with distinct recency.
// modified is epoch seconds; higher = more recent.
const SLOTS = [
  { key: 'k-old-alpha', title: 'Old in Alpha', messages: 1, running: false, folder_id: 'f1', modified: 1000 },
  { key: 'k-new-beta', title: 'Newest in Beta', messages: 1, running: false, folder_id: 'f2', modified: 3000 },
  { key: 'k-mid-root', title: 'Middle unfoldered', messages: 1, running: false, modified: 2000, pinned: true },
]

function renderSidebar(slots: any[] = SLOTS, folders: ChatFolder[] = FOLDERS) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  return render(
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
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

/** Pin the clock for date-bucket tests (issue #2919). Fakes ONLY `Date` so
 *  real timers keep driving RTL and promises, and pins the instant to LOCAL
 *  midday — the farthest point from both midnight edges in any timezone — so
 *  a `daysAgo(n)` fixture lands in its intended calendar bucket regardless of
 *  timezone or time of day (a raw `now - offset` slides across local midnight
 *  when the suite runs just after 00:00, which put the 60s-ago row under a
 *  "Yesterday" header). Mid-January avoids DST transitions in the lookback. */
function pinClock() {
  vi.useFakeTimers({ toFake: ['Date'] })
  const pin = new Date(2026, 0, 15, 12, 0, 0) // local midday, not 12:00Z
  vi.setSystemTime(pin)
  return (daysAgo: number) => new Date(pin.getTime() - daysAgo * 86400_000).toISOString()
}

describe('chat sidebar — flat view (explode chats out of folders)', () => {
  it('is off by default: folder tree renders, no flat lane', () => {
    const { queryByTestId, getByText } = renderSidebar()
    expect(queryByTestId('flat-view-lane')).toBeNull()
    // Folder headers from the tree
    expect(getByText('Alpha')).toBeTruthy()
    expect(getByText('Beta')).toBeTruthy()
  })

  it('toggling on shows one lane with ALL sessions (foldered + unfoldered) in the tree\'s sort order', () => {
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('flat-view-toggle'))
    const lane = getByTestId('flat-view-lane')
    const rows = Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
    // The flat view removes ONLY the folder hierarchy — ordering is exactly
    // what the tree uses: pinned first, then the active sort (default
    // date-desc). The pinned unfoldered session leads despite being older.
    expect(rows).toEqual(['k-mid-root', 'k-new-beta', 'k-old-alpha'])
  })

  it('does not annotate rows with their folder name in flat view', () => {
    // The folder-name chip was removed from the row. Flat view therefore carries
    // no folder annotation; ordering (asserted above) is the only thing the
    // folder data still drives. (Untagged rows lose on-row location context in
    // flat view as a result — a known trade-off, tracked separately.)
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('flat-view-toggle'))
    const lane = getByTestId('flat-view-lane')
    const rowOf = (key: string) => lane.querySelector(`[data-slot-key="${key}"]`) as HTMLElement
    for (const key of ['k-new-beta', 'k-old-alpha', 'k-mid-root']) {
      expect(within(rowOf(key)).queryByTitle(/In folder:/)).toBeNull()
    }
  })

  it('respects active session filters inside the flat lane', () => {
    // Pre-arm the "In progress" filter (read from localStorage at mount).
    safeSetItem('mc-session-running-only', '1')
    safeSetItem('mc-sidebar-flat-view', '1')
    const slots = [
      { key: 'k-running', title: 'Running one', messages: 1, running: true, folder_id: 'f1', modified: 1000 },
      { key: 'k-idle', title: 'Idle one', messages: 1, running: false, folder_id: 'f2', modified: 3000 },
    ]
    const { getByTestId } = renderSidebar(slots)
    const lane = getByTestId('flat-view-lane')
    const rows = Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
    expect(rows).toEqual(['k-running'])
  })

  it('hides the toggle when there are no folders (list is already flat)', () => {
    const slots = [{ key: 'k-a', title: 'A', messages: 1, running: false, modified: 1000 }]
    const { queryByTestId } = renderSidebar(slots, [])
    expect(queryByTestId('flat-view-toggle')).toBeNull()
  })

  it('falls back to the tree when flat view is persisted but all folders are gone', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const slots = [{ key: 'k-a', title: 'A', messages: 1, running: false, modified: 1000 }]
    const { queryByTestId, getByText } = renderSidebar(slots, [])
    expect(queryByTestId('flat-view-lane')).toBeNull()
    expect(getByText('A')).toBeTruthy()  // normal tree/ungrouped rendering
  })

  it('renders date segment headers on date sorts; pinned rows stay above without a header', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const iso = pinClock()
    const slots = [
      { key: 'k-pin', title: 'Pinned old', messages: 1, running: false, last_ts: iso(40), pinned: true },
      { key: 'k-today', title: 'Fresh one', messages: 1, running: false, folder_id: 'f1', last_ts: iso(0) },
      { key: 'k-week', title: 'Three days ago', messages: 1, running: false, folder_id: 'f2', last_ts: iso(3) },
    ]
    const { getByTestId } = renderSidebar(slots)
    const lane = getByTestId('flat-view-lane')
    // Document-order walk over headers + rows: pinned row leads with NO
    // header above it, then Today / Last 7 Days buckets.
    const seq = Array.from(lane.querySelectorAll('[data-testid="date-segment-header"], [data-slot-key]'))
      .map(el => el.getAttribute('data-slot-key') ?? `H:${el.textContent}`)
    expect(seq).toEqual(['k-pin', 'H:Today', 'k-today', 'H:Last 7 Days', 'k-week'])
  })

  it('hides date segment headers on non-date sorts', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    safeSetItem('mc-session-sort', 'name-asc')
    const iso = pinClock()
    const slots = [
      { key: 'k-b', title: 'Bravo', messages: 1, running: false, folder_id: 'f1', last_ts: iso(0) },
      { key: 'k-a', title: 'Alpha row', messages: 1, running: false, folder_id: 'f2', last_ts: iso(3) },
    ]
    const { getByTestId } = renderSidebar(slots)
    const lane = getByTestId('flat-view-lane')
    // A→Z order applies and no segment headers render.
    const rows = Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
    expect(rows).toEqual(['k-a', 'k-b'])
    expect(lane.querySelectorAll('[data-testid="date-segment-header"]').length).toBe(0)
  })

  it('persists the choice to localStorage and restores it on mount', () => {
    const first = renderSidebar()
    fireEvent.click(first.getByTestId('flat-view-toggle'))
    expect(localStorage.getItem('mc-sidebar-flat-view')).toBe('1')
    first.unmount()
    // Fresh mount picks the persisted flat view straight away.
    const second = renderSidebar()
    expect(second.getByTestId('flat-view-lane')).toBeTruthy()
  })

  it('toggling back off restores the folder tree', () => {
    const { getByTestId, queryByTestId, getByText } = renderSidebar()
    const toggle = getByTestId('flat-view-toggle')
    fireEvent.click(toggle)
    expect(getByTestId('flat-view-lane')).toBeTruthy()
    fireEvent.click(toggle)
    expect(queryByTestId('flat-view-lane')).toBeNull()
    expect(getByText('Alpha')).toBeTruthy()
    expect(localStorage.getItem('mc-sidebar-flat-view')).toBe('0')
  })
})
