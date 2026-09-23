/**
 * Chat sidebar loop-waiting subtitle ("Waiting on you"):
 * an ARMED loop (goal loop or structured monitor) whose NEWEST assistant reply
 * ends with an `[OPTIONS:]` ask renders an owed-decision row instead of the
 * pulsing "Loop N/M" progress row — the user cannot otherwise tell a loop that
 * is holding for their answer apart from one that is working.
 *
 * The state is derived from the newest reply only (payload `has_options`), so
 * a later cycle that talks over the marker clears it. That is the supersede
 * semantics that reverted the buried-[OPTIONS:] backward scan (#10615): an old
 * ask can never be resurrected, and a session WITHOUT an armed loop never gets
 * the badge — its composer chips are the whole surface.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { StructuredMonitor } from '../monitoring/automation'

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
    useReducedMotion: () => false,
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

const WAITING_LABEL = 'Waiting on you'

function renderSidebar(
  slots: ChatSlot[],
  chat: Record<string, unknown>,
  { unreadSlots = [] }: { unreadSlots?: string[] } = {},
) {
  const legacyFixtures = chat.goalLoops as Record<string, { cycle_count: number; max_cycles: number }> | undefined
  const { goalLoops: _legacyFixtures, ...chatState } = chat
  const migratedLegacy = Object.fromEntries(Object.entries(legacyFixtures ?? {}).map(([slotKey, loop]) => [
    slotKey,
    {
      kind: 'legacy_goal_loop', id: `loop-${slotKey}`, slotKey, message: '', idleSecs: 60,
      maxCycles: loop.max_cycles, cycleCount: loop.cycle_count, active: true,
      lastFireAt: 0, stoppedReason: '',
    },
  ]))
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      ...chatState,
      automations: { ...migratedLegacy, ...((chatState.automations as object) ?? {}) },
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={unreadSlots}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const structuredMonitor = (overrides: Partial<StructuredMonitor> = {}): StructuredMonitor => ({
  kind: 'structured_monitor', id: 'monitor-1', slotKey: 'k', active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', cadenceSecs: 300,
  nextProbeAt: 1_800_000_300, wakeInstructions: '',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'unchanged', reasonCode: '', observedAt: 1_800_000_000, decision: 'stay_quiet' },
  usage: { probes: 5, wakes: 2, agentTurns: 1, inputTokens: 100, outputTokens: 20, providerErrors: 0, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
  ...overrides,
})

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — loop waiting-on-you subtitle', () => {
  it('an idle loop whose newest reply carries [OPTIONS:] shows the waiting row, keeping the cycle detail', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, has_options: true }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } })
    expect(getByText(WAITING_LABEL)).toBeTruthy()
    // The loop identity survives as trailing detail, but PREFIXED with
    // "Paused at" — reusing the working row's "Loop N/M" verbatim made the
    // two rows read as the same state (UX span 03f52eaca7f7).
    expect(getByText(/Paused at 7\/24/)).toBeTruthy()
    expect(queryByText(/Loop 7\/24/)).toBeNull()
    // The tooltip glosses the fraction: the UX blind-reader could not tell the
    // trailing "Loop 7/24" was a cycle counter, so the title names it outright.
    expect(getByText(WAITING_LABEL).closest('[title]')?.getAttribute('title'))
      .toBe('Paused at cycle 7 of 24 — the last reply asked you to choose. Open the session to answer.')
    // The plain pulsing progress reading did not ALSO render.
    expect(queryByText(/interrupted/)).toBeNull()
  })

  it('a loop with no ask on its newest reply keeps the ordinary progress row — the supersede path', () => {
    // has_options is derived from the NEWEST reply, so a later cycle that talks
    // over an old [OPTIONS:] marker arrives here as has_options=false: the badge
    // clears by construction, never by a dismiss endpoint (#10615).
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, has_options: false }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 8, max_cycles: 24 } } })
    expect(getByText('Loop 8/24')).toBeTruthy()
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })

  it('a RUNNING loop turn is the loop working, not waiting — even with options still on the newest reply', () => {
    const slots = [{ key: 'k', title: 'loop', running: true, messages: 5, has_options: true }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 3, max_cycles: 24 } } })
    expect(getByText('Loop 3/24')).toBeTruthy()
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })

  it('a session WITHOUT an armed loop never gets the badge — composer chips are its whole surface', () => {
    // The #10615 invariant: options on an ordinary session render as composer
    // chips in the open tab, and the sidebar must not re-grow a waiting state.
    const slots = [{ key: 'k', title: 'plain', running: false, messages: 5, has_options: true, last_message: 'pick one' }]
    const { getByText, queryByText } = renderSidebar(slots, {})
    expect(getByText('pick one')).toBeTruthy()
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })

  it('an owed approval outranks the waiting row', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, has_options: true, pending_approval: true }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } })
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })

  it('a parked question card outranks the waiting row — the card is the stronger ask', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, has_options: true, needs_input: true }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } })
    expect(getByText('Needs your answer')).toBeTruthy()
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })

  it('a stalled loop keeps its danger reading — a dead turn cannot collect the answer', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, has_options: true, interrupted: true }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } })
    expect(getByText(/interrupted/)).toBeTruthy()
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })

  it('an ACTIVE structured monitor with an ask on the newest reply also shows the waiting row', () => {
    const slots = [{ key: 'k', title: 'watch', running: false, messages: 5, has_options: true }]
    const { getByText, queryByText } = renderSidebar(slots, { automations: { k: structuredMonitor() } })
    expect(getByText(WAITING_LABEL)).toBeTruthy()
    // The visible detail is a paused gloss, NOT the live status string:
    // "Waiting on you · active" read as a contradiction (UX span 4a221cc48433).
    expect(getByText(/Monitor paused/)).toBeTruthy()
    expect(queryByText(/·\s*active/)).toBeNull()
    // Monitors carry no cycle fraction, so the tooltip stays generic — the
    // count-naming variant is the goal-loop path's alone.
    expect(getByText(WAITING_LABEL).closest('[title]')?.getAttribute('title'))
      .toBe('The last reply asked you to choose. Open the session to answer.')
  })

  it('a TERMINAL structured monitor never claims to be waiting — the loop is over', () => {
    const slots = [{ key: 'k', title: 'watch', running: false, messages: 5, has_options: true, last_message: 'done' }]
    const { queryByText } = renderSidebar(slots, {
      automations: {
        k: structuredMonitor({
          active: false,
          terminal: { outcome: 'success', reason: 'objective_met', at: 1_800_000_500 } as unknown as StructuredMonitor['terminal'],
        }),
      },
    })
    expect(queryByText(WAITING_LABEL)).toBeNull()
  })
})
