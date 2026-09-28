/**
 * Session status surfacing for "the agent asked you something" (`needs_input`).
 *
 * The backend raises the flag for an unanswered question CARD only; these tests
 * pin what the sidebar DOES with it, which no backend test can see:
 *  (1) the row shows an info-coloured label instead of a bare unread dot, and
 *      shows it even while the slot reports running (a blocking card parks the
 *      turn, so "Thinking…" would be wrong);
 *  (2) a pending tool approval still outranks it — an owed allow/deny is the more
 *      specific state and keeps its own warn treatment.
 *
 * A turn that ended offering `[OPTIONS:]` is deliberately NOT this: it never
 * raises the flag, so its row keeps its message preview, its unread dot, and —
 * once the next turn starts — its live turn status. `test_slot_needs_input_status
 * .py` pins that half.
 *
 * The session-grid picker's own sort and dot are pinned in
 * `SessionGridViewCoverage.test.tsx`, which owns that view's stubs.
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

function renderSidebar(slots: ChatSlot[], unread: string[] = []) {
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

describe('chat sidebar — the agent is waiting on your answer', () => {
  it('labels an unanswered question card', () => {
    const slots: ChatSlot[] = [
      { key: 'k-q', title: 'asked-a-question', running: false, messages: 3, needs_input: true },
    ]
    const { getByText } = renderSidebar(slots, ['k-q'])
    expect(getByText('Needs your answer')).toBeTruthy()
  })

  it('does not trail the last message after the ask', () => {
    // A card has no transcript row, so the message is whatever the agent said
    // BEFORE asking — printed after "Needs your answer ·" it reads as the
    // question itself.
    const slots: ChatSlot[] = [
      {
        key: 'k-q', title: 'asked', running: false, messages: 3, needs_input: true,
        last_message: 'Both policies fit the read pattern.',
      },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Needs your answer')).toBeTruthy()
    expect(queryByText(/Both policies fit the read pattern/)).toBeNull()
  })

  it('leaves a plain [OPTIONS:] turn alone — the backend never flags one', () => {
    // Pinned here as well as in the backend suite because this row is what the
    // regression looked like: a constant label where the message should be.
    const slots: ChatSlot[] = [
      {
        key: 'k-o', title: 'offered-options', running: false, messages: 3,
        last_message: 'CI is green except the shelf button-count rule.',
      },
    ]
    const { getByText, queryByText, getAllByTitle } = renderSidebar(slots, ['k-o'])
    expect(getByText('CI is green except the shelf button-count rule.')).toBeTruthy()
    expect(queryByText('Needs your answer')).toBeNull()
    // And it keeps the dot: it is an ordinary finished turn.
    expect(getAllByTitle('Agent finished — your turn')).toHaveLength(1)
  })

  it('suppresses the blue "your turn" dot on that row', () => {
    // Two markers for one state read as two things to do; the label says more.
    const slots: ChatSlot[] = [
      { key: 'k-q', title: 'asked', running: false, messages: 3, needs_input: true },
      { key: 'k-turn', title: 'plain-finish', running: false, messages: 2 },
    ]
    const { getAllByTitle } = renderSidebar(slots, ['k-q', 'k-turn'])
    // Both rows are unread; only the one WITHOUT an ask keeps the dot.
    expect(getAllByTitle('Agent finished — your turn')).toHaveLength(1)
  })

  it('shows the ask even while the slot reports running', () => {
    // A blocking ask_question parks the turn, so `running` stays true — a
    // "Thinking…" row would hide the only thing that can unblock it.
    const slots: ChatSlot[] = [
      { key: 'k-q', title: 'blocked-on-you', running: true, messages: 3, needs_input: true },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Needs your answer')).toBeTruthy()
    expect(queryByText('Thinking…')).toBeNull()
  })

  it('keeps showing live turn status on a running session that is not asking', () => {
    // The ask branch outranks `running`, so anything that raises the flag on an
    // ordinary turn takes the row's live status with it.
    const slots: ChatSlot[] = [
      { key: 'k-run', title: 'working', running: true, messages: 3, last_message: 'earlier reply' },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Thinking…')).toBeTruthy()
    expect(queryByText('Needs your answer')).toBeNull()
  })

  it('keeps a pending tool approval ahead of it', () => {
    const slots: ChatSlot[] = [
      { key: 'k-both', title: 'approval-and-question', running: false, messages: 4, pending_approval: true, needs_input: true },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText('Needs your answer')).toBeNull()
  })
})
