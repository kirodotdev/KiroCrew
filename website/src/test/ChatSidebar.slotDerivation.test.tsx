/**
 * Sidebar slot derivation — the values that are NOT payload fields. They used to
 * be attached by copying every slot per render; they are now resolved from
 * membership lookups at the point of use.
 *
 * Each is pinned SEPARATELY, because the failure mode of that swap is a silently
 * dropped field: the list still renders and the row count is unchanged, so only
 * a per-field assertion can tell one piece of state has gone `undefined`.
 *
 *  (1) `recent`  — a timestamp inside the window OR a running turn. Both halves.
 *  (2) `running` — wider than `s.running`: a workflow run or goal loop counts.
 *  (3) `midTurn` — the RAW turn flag, deliberately NOT the widened one.
 *  (4) `unread`  — reaches the row's secondary-line status marker.
 * Plus `pinned`, a payload field read through the same lookup.
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

const UNREAD_DOT_TITLE = 'Agent finished — your turn'
// Default recency window is 1h, so these sit either side of it.
const FRESH = new Date(Date.now() - 60 * 1000).toISOString()
const STALE = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString()

/** Minimal WorkflowRunProgress for the sidebar (reads name/phase/status/sessionKey). */
const wf = (over: Record<string, unknown> = {}) => ({
  run_id: 'wf_000001', name: 'nightly-sweep', phase: 'gather',
  lastLog: '', status: 'running', sessionKey: 'dashboard:k-wf', ...over,
})

function renderSidebar(
  slots: any[],
  chat: Record<string, unknown> = {},
  { activeSlotProp = null, unreadSlots = [] }: { activeSlotProp?: string | null; unreadSlots?: string[] } = {},
) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {}, goalLoops: {}, ...chat } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={activeSlotProp} unreadSlots={unreadSlots}
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

describe('sidebar slot derivation — every derived value still reaches the row', () => {
  it('POSITIVE CONTROL: with no filter active, every row renders', () => {
    const slots = [
      { key: 'a', title: 'alpha', running: false, messages: 1, last_turn_ts: STALE },
      { key: 'b', title: 'bravo', running: true, messages: 2, last_turn_ts: FRESH },
      { key: 'c', title: 'charlie', running: false, messages: 3, last_turn_ts: FRESH, pinned: true },
    ]
    const { getByText } = renderSidebar(slots)
    expect(getByText('alpha')).toBeTruthy()
    expect(getByText('bravo')).toBeTruthy()
    expect(getByText('charlie')).toBeTruthy()
  })

  it('`recent` counts a RUNNING turn whose timestamp has already aged out', () => {
    // The ordering key stops advancing mid-turn, so a turn outliving the window
    // must not age out of Recent. A timestamp-only lookup drops this half.
    localStorage.setItem('mc-session-recent-only', '1')
    const slots = [
      { key: 'run', title: 'long running turn', running: true, messages: 5, last_turn_ts: STALE },
      { key: 'idle', title: 'stale and idle', running: false, messages: 2, last_turn_ts: STALE },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('long running turn')).toBeTruthy()
    expect(queryByText('stale and idle')).toBeNull()
  })

  it('`recent` still counts a fresh timestamp on an idle slot', () => {
    // The other half of the same union: recency must not collapse into "running".
    localStorage.setItem('mc-session-recent-only', '1')
    const slots = [
      { key: 'fresh', title: 'recently active', running: false, messages: 2, last_turn_ts: FRESH },
      { key: 'old', title: 'long forgotten', running: false, messages: 2, last_turn_ts: STALE },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('recently active')).toBeTruthy()
    expect(queryByText('long forgotten')).toBeNull()
  })

  it('`running` is widened by an active GOAL LOOP, not just the payload flag', () => {
    localStorage.setItem('mc-session-running-only', '1')
    const slots = [
      { key: 'k-loop', title: 'looping session', running: false, messages: 5, last_turn_ts: STALE },
      { key: 'k-idle', title: 'idle session', running: false, messages: 2, last_turn_ts: STALE },
    ]
    const { getByText, queryByText } = renderSidebar(slots, {
      goalLoops: { 'k-loop': { cycle_count: 7, max_cycles: 24 } },
    })
    expect(getByText('looping session')).toBeTruthy()
    expect(queryByText('idle session')).toBeNull()
  })

  it('`running` is widened by a live WORKFLOW run, not just the payload flag', () => {
    localStorage.setItem('mc-session-running-only', '1')
    const slots = [
      { key: 'k-wf', title: 'workflow session', running: false, messages: 5, last_turn_ts: STALE },
      { key: 'k-idle', title: 'idle session', running: false, messages: 2, last_turn_ts: STALE },
    ]
    const { getByText, queryByText } = renderSidebar(slots, { workflowRuns: { wf_000001: wf() } })
    expect(getByText('workflow session')).toBeTruthy()
    expect(queryByText('idle session')).toBeNull()
  })

  it('`midTurn` is the RAW turn flag: an idle-between-cycles loop shows its last message', () => {
    // The widened flag is TRUE here (the goal loop widens it), so resolving
    // this from the widened value would print "Thinking…" for an idle row.
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, last_message: 'cycle 7 output' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } }, slotStatusDetail: { k: { text: 'Reading gateway.log' } } },
    )
    expect(getByText('Loop 7/24')).toBeTruthy()
    expect(getByText(/cycle 7 output/)).toBeTruthy()
    expect(queryByText(/Reading gateway\.log/)).toBeNull()
  })

  it('`midTurn` true mid-turn: the same loop row carries the live tool status instead', () => {
    const slots = [{ key: 'k', title: 'loop', running: true, messages: 5, last_message: 'cycle 7 output' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: 'k', goalLoops: { k: { cycle_count: 7, max_cycles: 24 } }, slotStatusDetail: { k: { text: 'Reading gateway.log' } } },
      { activeSlotProp: 'k' },
    )
    expect(getByText('Loop 7/24')).toBeTruthy()
    expect(getByText(/Reading gateway\.log/)).toBeTruthy()
    expect(queryByText(/cycle 7 output/)).toBeNull()
  })

  it('`unread` reaches the row gutter as the "your turn" dot', () => {
    const slots = [{ key: 'k', title: 'answered', running: false, messages: 5, last_message: 'final answer' }]
    const { queryByTitle } = renderSidebar(slots, {}, { unreadSlots: ['k'] })
    expect(queryByTitle(UNREAD_DOT_TITLE)).toBeTruthy()
  })

  it('`unread` drives its own filter, and a read row is excluded', () => {
    localStorage.setItem('mc-session-unread-only', '1')
    const slots = [
      { key: 'u', title: 'unread session', running: false, messages: 2, last_turn_ts: FRESH },
      { key: 'r', title: 'read session', running: false, messages: 2, last_turn_ts: FRESH },
    ]
    const { getByText, queryByText } = renderSidebar(slots, {}, { unreadSlots: ['u'] })
    expect(getByText('unread session')).toBeTruthy()
    expect(queryByText('read session')).toBeNull()
  })

  it('`pinned` still resolves through the same lookup as the derived keys', () => {
    localStorage.setItem('mc-session-pinned-only', '1')
    const slots = [
      { key: 'p', title: 'pinned session', running: false, messages: 2, last_turn_ts: FRESH, pinned: true },
      { key: 'q', title: 'loose session', running: false, messages: 2, last_turn_ts: FRESH },
    ]
    const { getByText, queryByText } = renderSidebar(slots)
    expect(getByText('pinned session')).toBeTruthy()
    expect(queryByText('loose session')).toBeNull()
  })
})
