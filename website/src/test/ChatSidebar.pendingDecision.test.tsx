/**
 * Session status surfacing for a buried [OPTIONS:] decision (`pending_decision`).
 *
 * The backend raises the payload only when an options-bearing turn was talked
 * over by a later option-less reply with no human row in between
 * (test_slot_pending_decision.py). These tests pin what the sidebar DOES with
 * it, which no backend test can see:
 *  (1) the row shows a warn-coloured "Pending your response" label instead of a
 *      bare unread dot, and shows it even while the slot reports running — the
 *      loop keeps cycling, but a click is owed;
 *  (2) precedence: an unanswered question card (`needs_input`) and a pending
 *      tool approval both outrank it — the same ordering the composer band
 *      uses, where the question card owns the band.
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

const DECISION = { options: ['Retry', 'Stop'], excerpt: 'How should I proceed?', ts: '2026-09-11T10:00:00' }

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

describe('chat sidebar — a loop buried its own question', () => {
  it('labels the row, warn-coloured, instead of the message preview', () => {
    const slots: ChatSlot[] = [
      {
        key: 'k-d', title: 'loop-asked', running: false, messages: 8, pending_decision: DECISION,
        last_message: 'Cycle 12: board unchanged.',
      },
    ]
    const { getByText, queryByText } = renderSidebar(slots, ['k-d'])
    const label = getByText('Pending your response')
    expect(label).toBeTruthy()
    // The yellow is the theme's warn token, not a hardcoded color.
    expect(label.getAttribute('style') || '').toContain('var(--warn)')
    // The loop's own chatter is not the question — the label stands alone.
    expect(queryByText(/Cycle 12: board unchanged/)).toBeNull()
  })

  it('suppresses the "your turn" dot on that row', () => {
    const slots: ChatSlot[] = [
      { key: 'k-d', title: 'loop-asked', running: false, messages: 8, pending_decision: DECISION },
      { key: 'k-turn', title: 'plain-finish', running: false, messages: 2 },
    ]
    const { getAllByTitle } = renderSidebar(slots, ['k-d', 'k-turn'])
    // Both rows are unread; only the one WITHOUT a buried decision keeps the dot.
    expect(getAllByTitle('Agent finished — your turn')).toHaveLength(1)
  })

  it('shows the label even while the loop keeps the slot running', () => {
    // The next cycle is executing, but nothing it does can answer the buried
    // ask — "Thinking…" would file an owed click under work in progress.
    const slots: ChatSlot[] = [
      { key: 'k-d', title: 'cycling', running: true, messages: 8, pending_decision: DECISION },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Pending your response')).toBeTruthy()
    expect(queryByText('Thinking…')).toBeNull()
  })

  it('yields to an unanswered question card', () => {
    // Same precedence as the composer band, where the question card owns the
    // band: an explicit card outranks a marker.
    const slots: ChatSlot[] = [
      { key: 'k-both', title: 'card-and-marker', running: false, messages: 4, needs_input: true, pending_decision: DECISION },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Needs your answer')).toBeTruthy()
    expect(queryByText('Pending your response')).toBeNull()
  })

  it('yields to a pending tool approval', () => {
    const slots: ChatSlot[] = [
      { key: 'k-both', title: 'approval-and-marker', running: false, messages: 4, pending_approval: true, pending_decision: DECISION },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText('Pending your response')).toBeNull()
  })

  it('leaves a session without the payload alone', () => {
    const slots: ChatSlot[] = [
      {
        key: 'k-plain', title: 'ordinary', running: false, messages: 3,
        last_message: 'CI is green.', pending_decision: null,
      },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('CI is green.')).toBeTruthy()
    expect(queryByText('Pending your response')).toBeNull()
  })
})
