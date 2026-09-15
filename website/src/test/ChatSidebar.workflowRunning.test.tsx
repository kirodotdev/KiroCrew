/**
 * Chat sidebar "workflow running" subtitle:
 * a session row surfaces its live dynamic-workflow runs — read from
 * chat.workflowRuns (fed by globally-subscribed workflow_run_event WS
 * broadcasts) and scoped to the row via runBelongsToSlot, so a run tagged
 * `dashboard:<slot>` pins to its own chat only. The subtitle shows even when
 * the parent turn has ended (running === false) — the case the feature exists
 * for — outranks the subagent count and the stale last message, but not
 * "Needs approval". Terminal runs (finished/failed/cancelled) are not shown,
 * and a wf-active slot counts as "In progress" for the session filter.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
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

/** Minimal WorkflowRunProgress for the sidebar (reads name/phase/status/sessionKey). */
const wf = (over: Record<string, unknown> = {}) => ({
  run_id: 'wf_000001', name: 'bugfix-16-comments', phase: 'implement-fixes',
  lastLog: '', status: 'running', sessionKey: 'dashboard:k-bg', ...over,
})

function renderSidebar(slots: ChatSlot[], chat: Record<string, unknown>, activeSlotProp: string | null = null) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {}, ...chat } as unknown as RootState['chat'],
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

describe('chat sidebar — "workflow running" subtitle', () => {
  it('shows name · phase for a BACKGROUND slot whose turn has ended, over the last message', () => {
    // The core case: parent turn is done but the launched workflow still runs.
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3, last_message: 'stale last message' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { workflowRuns: { wf_000001: wf() } },
    )
    expect(getByText('bugfix-16-comments · implement-fixes')).toBeTruthy()
    expect(queryByText('stale last message')).toBeNull() // workflow activity outranks last_message
  })

  it('scopes runs to their own slot via session_key — other rows stay untouched', () => {
    const slots = [
      { key: 'k-bg', title: 'bg', running: false, messages: 3 },
      { key: 'k-other', title: 'other', running: false, messages: 2, last_message: 'other last' },
    ]
    const { getByText, queryAllByText } = renderSidebar(
      slots,
      { workflowRuns: { wf_000001: wf() } },
    )
    expect(getByText('bugfix-16-comments · implement-fixes')).toBeTruthy()
    expect(queryAllByText(/implement-fixes/)).toHaveLength(1) // not on k-other
    expect(getByText('other last')).toBeTruthy()
  })

  it('does NOT show terminal runs (finished/failed/cancelled) or runs without a session_key', () => {
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3, last_message: 'the last message' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      {
        workflowRuns: {
          wf_1: wf({ run_id: 'wf_1', status: 'finished' }),
          wf_2: wf({ run_id: 'wf_2', status: 'failed' }),
          wf_3: wf({ run_id: 'wf_3', status: 'cancelled' }),
          // UI-launched run with no chat link: belongs to no slot.
          wf_4: wf({ run_id: 'wf_4', name: 'orphan', sessionKey: undefined }),
        },
      },
    )
    expect(queryByText(/implement-fixes/)).toBeNull()
    expect(queryByText(/orphan/)).toBeNull()
    expect(getByText('the last message')).toBeTruthy()
  })

  it('collapses multiple active runs into "N workflows running" and outranks the subagent count', () => {
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3 }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      {
        workflowRuns: {
          wf_1: wf({ run_id: 'wf_1' }),
          wf_2: wf({ run_id: 'wf_2', name: 'second' }),
        },
        // A live subagent on the same slot: the workflow line wins.
        slotActivity: { 'k-bg': { toolLog: [], subagents: { s1: { id: 's1', status: 'running' } } } },
      },
    )
    expect(getByText('2 workflows running')).toBeTruthy()
    expect(queryByText('1 agent running')).toBeNull()
  })

  it('"Needs approval" still outranks the workflow line', () => {
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3, pending_approval: true }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { workflowRuns: { wf_000001: wf() } },
    )
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText(/implement-fixes/)).toBeNull()
  })

  it('falls back to run_id when the run has no name yet (mid-run reload)', () => {
    // After a page reload the store entry is recreated from the next WS event,
    // which may not carry the name — the run_id is still a usable label.
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3 }]
    const { getByText } = renderSidebar(
      slots,
      { workflowRuns: { wf_000001: wf({ name: '', phase: '' }) } },
    )
    expect(getByText('wf_000001')).toBeTruthy()
  })

  it('a wf-active slot passes the "In progress" session filter despite running=false', () => {
    // Pre-activate the filter via its persisted toggle (read at mount).
    localStorage.setItem('mc-session-running-only', '1')
    const slots = [
      { key: 'k-bg', title: 'wf session', running: false, messages: 3 },
      { key: 'k-idle', title: 'idle session', running: false, messages: 2 },
    ]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { workflowRuns: { wf_000001: wf() } },
    )
    expect(getByText('wf session')).toBeTruthy()   // kept: live workflow counts as in-progress
    expect(queryByText('idle session')).toBeNull() // filtered out: genuinely idle
  })
})
