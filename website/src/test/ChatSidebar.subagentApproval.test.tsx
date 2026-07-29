/**
 * Chat sidebar "N sub-agents need approval" subtitle.
 *
 * A spawn approval is broadcast as a WS `approval` event with id
 * `spawn:<agent_id>` and lands in the per-slot subagents map as
 * status 'pending' + approval_id. It has no inline chat prompt and no
 * notification, so before this the session row showed "N agents running" —
 * an owed decision rendered as work in progress, and invisible entirely for a
 * background chat the user was not viewing.
 *
 * Covered here: the label and its pluralization, precedence against the
 * running/workflow/Thinking lines, exclusion of blocked agents from the
 * running count, the slot's own approval still outranking it, and the
 * "your turn" dot staying suppressed while an approval is owed.
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

/** Sub-agent held at the spawn gate: pending + an approval_id. */
const awaiting = (id: string) => ({ id, status: 'pending', approval_id: `spawn:${id}` } as any)
/** Sub-agent actually working. */
const running = (id: string) => ({ id, status: 'running' } as any)

function renderSidebar(
  slots: any[],
  chat: Record<string, unknown>,
  activeSlotProp: string | null = null,
  unreadSlots: string[] = [],
) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
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

describe('chat sidebar — sub-agents awaiting spawn approval', () => {
  it('labels a single blocked sub-agent on a BACKGROUND slot, over the last message', () => {
    // The case the feature exists for: no inline prompt, no notification, and
    // the user is not looking at this chat.
    const slots = [{ key: 'k-bg', title: 'bg', running: false, messages: 3, last_message: 'stale last message' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: null, slotActivity: { 'k-bg': { toolLog: [], subagents: { a1: awaiting('a1') } } } },
    )
    expect(getByText('1 sub-agent needs approval')).toBeTruthy()
    expect(queryByText('stale last message')).toBeNull()
  })

  it('pluralizes for several blocked sub-agents on the ACTIVE slot', () => {
    const slots = [{ key: 'k', title: 'act', running: false, messages: 2 }]
    const { getByText } = renderSidebar(
      slots,
      { activeSlot: 'k', subagents: { a1: awaiting('a1'), a2: awaiting('a2') } },
      'k',
    )
    expect(getByText('2 sub-agents need approval')).toBeTruthy()
  })

  it('outranks the running-agents line and drops blocked agents from its count', () => {
    // The reported bug: 4 started + 2 blocked read as "4 agents running" with
    // no hint that anything was owed. Approval wins, and the count it would
    // have shown never includes the blocked pair.
    const slots = [{ key: 'k', title: 'mix', running: false, messages: 2 }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      {
        activeSlot: 'k',
        subagents: {
          r1: running('r1'), r2: running('r2'), r3: running('r3'), r4: running('r4'),
          a1: awaiting('a1'), a2: awaiting('a2'),
        },
      },
      'k',
    )
    expect(getByText('2 sub-agents need approval')).toBeTruthy()
    expect(queryByText(/agents? running/)).toBeNull()
  })

  it('outranks the generic "Thinking…" line while the parent turn is still running', () => {
    const slots = [{ key: 'k', title: 'r', running: true, messages: 1 }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: 'k', subagents: { a1: awaiting('a1') }, slotStatusDetail: { k: { text: 'Thinking…' } } },
      'k',
    )
    expect(getByText('1 sub-agent needs approval')).toBeTruthy()
    expect(queryByText('Thinking…')).toBeNull()
  })

  it('is itself outranked by the slot\'s own pending approval', () => {
    // Only one approval line per row; the slot's own tool approval is the more
    // immediate one and already carries the last-message tail.
    const slots = [{ key: 'k', title: 'a', running: false, messages: 2, pending_approval: true }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: null, slotActivity: { k: { toolLog: [], subagents: { a1: awaiting('a1') } } } },
    )
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText(/sub-agents? needs? approval/)).toBeNull()
  })

  it('a pending sub-agent with no approval_id is still counted as running, not blocked', () => {
    // 'pending' alone is the pre-spawn state of an approved agent. Only the
    // approval_id marks it as waiting on the user.
    const slots = [{ key: 'k', title: 'p', running: false, messages: 2 }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: 'k', subagents: { p1: { id: 'p1', status: 'pending' } as any } },
      'k',
    )
    expect(getByText('1 agent running')).toBeTruthy()
    expect(queryByText(/needs? approval/)).toBeNull()
  })

  it('suppresses the blue "your turn" dot while a spawn approval is owed', () => {
    const slots = [{ key: 'k', title: 'u', running: false, messages: 2, unread: true }]
    const { container, getByText } = renderSidebar(
      slots,
      { activeSlot: null, slotActivity: { k: { toolLog: [], subagents: { a1: awaiting('a1') } } } },
      null,
      ['k'],
    )
    expect(getByText('1 sub-agent needs approval')).toBeTruthy()
    expect(container.querySelector('[title="Agent finished — your turn"]')).toBeNull()
  })
})
