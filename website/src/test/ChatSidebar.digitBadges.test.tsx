/**
 * Sidebar digit-jump integration:
 *  (1) The sidebar publishes its DISPLAYED session order to
 *      `dashboard.sidebarOrder` (recency-desc by default), which is what the
 *      Ctrl/Alt+digit chat-jump shortcuts index — so Ctrl+1 hits the top row
 *      even under "recent" sort where store order (backend insertion order)
 *      disagrees.
 *  (2) While the jump modifier is held (Alt on non-Mac), the first nine rows
 *      show a digit badge revealing which key picks them; released → gone.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
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

// Store order (backend insertion order) is OLDEST-first on purpose: the
// display order under the default date-desc sort is the exact reverse, which
// is what makes these assertions meaningful.
const SLOTS = [
  { key: 'k-oldest', title: 'Oldest', messages: 1, running: false, modified: 1000 },
  { key: 'k-middle', title: 'Middle', messages: 1, running: false, modified: 2000 },
  { key: 'k-newest', title: 'Newest', messages: 1, running: false, modified: 3000 },
]

function renderSidebar(slots: any[] = SLOTS, folders: ChatFolder[] = []) {
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
  const utils = render(
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
  return { store, ...utils }
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — published shortcut order', () => {
  it('publishes the DISPLAYED order (recency-desc), not store order', () => {
    const { store } = renderSidebar()
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-newest', 'k-middle', 'k-oldest'])
  })

  it('republishes when the sort flips to oldest-first', () => {
    localStorage.setItem('mc-session-sort', 'date-asc')
    const { store } = renderSidebar()
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-oldest', 'k-middle', 'k-newest'])
  })
})

describe('chat sidebar — held-modifier digit badges', () => {
  it('shows digits 1..N on rows in display order while Alt is held, hides on release', () => {
    const { queryAllByTestId, getAllByTestId } = renderSidebar()
    expect(queryAllByTestId('digit-jump-badge')).toHaveLength(0)

    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    const badges = getAllByTestId('digit-jump-badge')
    expect(badges).toHaveLength(3)
    // Badge digit ↔ row mapping: 1 = top displayed row (newest).
    const byRow = badges.map(b => [b.closest('[data-session-row]')?.getAttribute('data-session-row'), b.textContent])
    expect(byRow).toContainEqual(['k-newest', '1'])
    expect(byRow).toContainEqual(['k-middle', '2'])
    expect(byRow).toContainEqual(['k-oldest', '3'])

    act(() => { fireEvent.keyUp(window, { altKey: false }) })
    expect(queryAllByTestId('digit-jump-badge')).toHaveLength(0)
  })

  it('badges only the first nine rows', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      key: `k-${i}`, title: `S${i}`, messages: 1, running: false, modified: 10_000 - i,
    }))
    const { getAllByTestId } = renderSidebar(many)
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    expect(getAllByTestId('digit-jump-badge')).toHaveLength(9)
  })
})
