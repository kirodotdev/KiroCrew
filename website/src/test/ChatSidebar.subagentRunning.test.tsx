/**
 * Chat sidebar "N agents running" subtitle:
 * a session row surfaces its live subagents — counted from chat.subagents (the
 * store's active slot) + slotActivity[slot].subagents (background slots),
 * matching only non-terminal statuses (running/tool/pending). The subtitle
 * outranks the generic "Thinking…" running line but not "Needs approval", and
 * shows even when the parent turn has ended (running === false) — the case the
 * feature exists for. Terminal (done/error) subagents are not counted.
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

/** Minimal SubagentActivity — subagentCounts only reads `.status`. */
const sa = (status: string) => ({ id: `id-${status}-${Math.random()}`, status } as any)

function renderSidebar(slots: any[], chat: Record<string, unknown>, activeSlotProp: string | null = null) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, ...chat } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={activeSlotProp} unreadSlots={[]}
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

describe('chat sidebar — "N agents running" subtitle', () => {
  it('shows the indicator for a BACKGROUND slot whose turn has ended (running=false), over the last message', () => {
    // The core case: parent turn is done but a spawned subagent is still working.
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3, last_message: 'stale last message' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: null, slotActivity: { 'k-bg': { toolLog: [], subagents: { s1: sa('running') } } } },
    )
    expect(getByText('1 agent running')).toBeTruthy()
    expect(queryByText('stale last message')).toBeNull() // subagent activity outranks last_message
  })

  it('pluralizes and counts running + tool + pending as active for the ACTIVE slot', () => {
    const slots = [{ key: 'k-act', title: 'act', running: false, messages: 2 }]
    const { getByText } = renderSidebar(
      slots,
      { activeSlot: 'k-act', subagents: { a: sa('running'), b: sa('tool'), c: sa('pending') } },
      'k-act',
    )
    expect(getByText('3 agents running')).toBeTruthy()
  })

  it('outranks the generic running "Thinking…" line', () => {
    const slots = [{ key: 'k', title: 'r', running: true, messages: 1 }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: 'k', subagents: { a: sa('running') }, slotStatusDetail: { k: { text: 'Thinking…' } } },
      'k',
    )
    expect(getByText('1 agent running')).toBeTruthy()
    expect(queryByText('Thinking…')).toBeNull()
  })

  it('is outranked by a pending approval', () => {
    const slots = [{ key: 'k', title: 'a', running: false, messages: 2, pending_approval: true }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: null, slotActivity: { k: { toolLog: [], subagents: { a: sa('running') } } } },
    )
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText(/agents? running/)).toBeNull()
  })

  it('does not count terminal (done/error) subagents', () => {
    const slots = [{ key: 'k', title: 'd', running: false, messages: 2, last_message: 'final answer' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: null, slotActivity: { k: { toolLog: [], subagents: { a: sa('done'), b: sa('error') } } } },
    )
    expect(getByText('final answer')).toBeTruthy() // falls through to last_message
    expect(queryByText(/agents? running/)).toBeNull()
  })
})

/**
 * Queued waves. An accepted-but-not-started wave has NO entry in the per-slot
 * subagents map (subagent_spawn hasn't fired), so counting the map alone left
 * the row silent — showing a stale last message — for the whole ramp.
 */
describe('chat sidebar — queued subagents', () => {
  it('surfaces a wave that is entirely queued, and says queued (not running)', () => {
    const slots = [{ key: 'k-q', title: 'q', running: false, messages: 2, last_message: 'stale last message' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: null, subagentQueued: { 'k-q': 3 } },
    )
    expect(getByText('3 agents queued')).toBeTruthy()
    expect(queryByText('stale last message')).toBeNull()
  })

  it('splits the label when a staggered ramp has both started and queued agents', () => {
    const slots = [{ key: 'k-mix', title: 'mix', running: false, messages: 2 }]
    const { getByText } = renderSidebar(
      slots,
      { activeSlot: 'k-mix', subagents: { a: sa('running') }, subagentQueued: { 'k-mix': 2 } },
      'k-mix',
    )
    expect(getByText('1 running · 2 queued')).toBeTruthy()
  })

  it('a zero queue depth changes nothing', () => {
    const slots = [{ key: 'k-z', title: 'z', running: false, messages: 2, last_message: 'final answer' }]
    const { getByText } = renderSidebar(slots, { activeSlot: null, subagentQueued: { 'k-z': 0 } })
    expect(getByText('final answer')).toBeTruthy()
  })
})
