/**
 * Gap 8 (frontend): the sidebar row surfaces a Kiro Agent workflow that is
 * waiting on the user.
 *
 * (1) a chat whose KAS run reports `controls.needsAttention` shows the
 *     workflow-specific "needs you" line (ranked above the generic ask line);
 * (2) a bare `ask_question` hold (no workflow) falls through to the generic
 *     "Needs your answer" line — the workflow branch does not shadow it.
 *
 * The run's "needs you" signal comes from the client `/state` poll
 * (`useKasSidebarActivity`), which is why this is a render test and not a
 * backend one. Assertions key on a testid, not English, because the new label
 * key is not in the catalog until L6 merges it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

// A live KAS run that needs a person, reporting into chat `k-wf`.
const ATTENTION_RUN = {
  workflowId: 'wf1', slotKey: 'k-wf', status: 'running', name: 'My Run', workflowName: null,
  controls: { needsAttention: true, display: 'running' }, blocking: null,
  activeSteps: [], nodes: [], pauseReason: null, pause: null,
  stepsTotal: null, currentNodeId: null, waitingOnWatch: null,
}

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'workflowEngine') return vi.fn().mockResolvedValue({ engine: 'kas', kas_backend: true, kas_workflows: true })
      if (prop === 'kasState') return vi.fn().mockResolvedValue({ runs: [ATTENTION_RUN], approvals: [], revision: 1 })
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

describe('chat sidebar — a workflow needs you (gap 8)', () => {
  it('shows the workflow needs-you line when the run has attention', async () => {
    const slots: ChatSlot[] = [{ key: 'k-wf', title: 'has-a-workflow', running: true, messages: 2 }]
    const { findByTestId } = renderSidebar(slots)
    expect(await findByTestId('row-workflow-needs-you')).toBeTruthy()
    // Let the shared KAS polls settle so no update lands after the test ends.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  })

  it('falls through to the generic ask line for a bare ask_question (no workflow)', async () => {
    // `k-ask` has no KAS run (the poll only holds `k-wf`), so `wfNeedsYou` is
    // false and the generic needs_input line wins.
    const slots: ChatSlot[] = [{ key: 'k-ask', title: 'asked', running: false, messages: 3, needs_input: true }]
    const { getByText, queryByTestId } = renderSidebar(slots)
    expect(getByText('Needs your answer')).toBeTruthy()
    expect(queryByTestId('row-workflow-needs-you')).toBeNull()
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(queryByTestId('row-workflow-needs-you')).toBeNull()
  })
})
