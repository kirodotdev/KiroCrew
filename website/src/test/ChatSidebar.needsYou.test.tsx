/**
 * Chat sidebar "needs you" surfacing:
 * the per-row blue dot means "the agent finished its turn and you haven't opened
 * the session" (finished + unread); it does not light mid-stream. A pending tool
 * approval shows a yellow "Needs approval" subtitle instead (and suppresses the
 * blue dot), and running / read / idle rows show nothing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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
// Legacy single-lane list (no tag columns) keeps the rows flat + easy to query.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
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

const SLOTS: ChatSlot[] = [
  { key: 'k-turn', title: 'your-turn', running: false, messages: 2 },        // finished; unread -> blue "your turn" dot
  { key: 'k-appr', title: 'awaiting-approval', running: false, messages: 3, pending_approval: true },
  { key: 'k-run', title: 'is-running', running: true, messages: 1 },          // streaming -> no dot
  { key: 'k-idle', title: 'is-idle', running: false, messages: 5 },           // finished but already opened (read) -> no dot
]
// k-appr is also unread, to prove a pending approval suppresses the blue dot (yellow wins).
const UNREAD = ['k-turn', 'k-appr']

function renderSidebar(slots: ChatSlot[] = SLOTS, unread: string[] = UNREAD) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
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
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — your-turn (repurposed unread) dot', () => {
  it('shows the blue "your turn" dot only for a finished + unopened session', () => {
    const { getAllByTitle } = renderSidebar()
    // k-turn (finished + unread) -> dot. k-run (running), k-idle (read), and
    // k-appr (pending approval -> yellow, dot suppressed) get none.
    expect(getAllByTitle('Agent finished — your turn')).toHaveLength(1)
  })

  it('shows a yellow "Needs approval" label for a pending approval, with no blue dot on that row', () => {
    const { getByText, getAllByTitle } = renderSidebar()
    expect(getByText('Needs approval')).toBeTruthy()
    // k-appr is unread but pending_approval suppresses the blue dot -> total stays 1 (k-turn only)
    expect(getAllByTitle('Agent finished — your turn')).toHaveLength(1)
  })

  it('keeps "Needs approval" ahead of the running spinner, and shows no blue dot while running', () => {
    const slots = [{ key: 'k-run-appr', title: 'approval-while-running', running: true, messages: 2, pending_approval: true }]
    const { getByText, queryByText, queryAllByTitle } = renderSidebar(slots, [])
    expect(getByText('Needs approval')).toBeTruthy()      // approval wins over "Thinking…"
    expect(queryByText('Thinking…')).toBeNull()
    expect(queryAllByTitle('Agent finished — your turn')).toHaveLength(0)  // running -> no blue dot
  })
})
