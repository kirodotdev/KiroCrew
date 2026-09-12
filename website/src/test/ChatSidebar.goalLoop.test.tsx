/**
 * Chat sidebar goal-loop ("Loop N/M") subtitle:
 * a session in an active auto-nudge loop shows its cycle progress composed with
 * whatever the row would otherwise have said, and gives up the blue "your turn"
 * dot — a loop appends a turn every cycle, so that dot would light permanently
 * and stop meaning anything.
 *
 * Progress comes from chat.goalLoops, keyed by the BARE slot key (autonudge.py
 * `binding_key_for` strips the `dashboard:` prefix). Presence in the map IS
 * "looping": inactive loops are dropped on the way into the store, so a loop
 * that hit max_cycles leaves no residue here. An actively-looping slot also
 * counts as "In progress" for the session filter, like a live workflow run.
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
import type { ChatSlot, SubagentActivity } from '../types'

const UNREAD_DOT_TITLE = 'Agent finished — your turn'

/** Minimal SubagentActivity — subagentCounts only reads `.status`. */
const sa = (status: string) => ({ id: `id-${status}-${Math.random()}`, status } as unknown as SubagentActivity)

function renderSidebar(
  slots: ChatSlot[],
  chat: Record<string, unknown>,
  { activeSlotProp = null, unreadSlots = [] }: { activeSlotProp?: string | null; unreadSlots?: string[] } = {},
) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, goalLoops: {}, ...chat } as unknown as RootState['chat'],
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

describe('chat sidebar — goal-loop progress subtitle', () => {
  it('shows bounded cycle progress composed with the last message', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, last_message: 'pulled 24/24 halves' }]
    const { getByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } })
    expect(getByText('Loop 7/24')).toBeTruthy()
    // Composed, not replaced — the last message survives beside the progress.
    expect(getByText(/pulled 24\/24 halves/)).toBeTruthy()
  })

  it('drops the denominator for an unlimited loop (max_cycles === 0)', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5 }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 31, max_cycles: 0 } } })
    expect(getByText('Loop · 31')).toBeTruthy()
    expect(queryByText(/Loop 31\/0/)).toBeNull()
  })

  it('is NOT gated on running — a mid-turn loop still shows progress, carrying the live tool status', () => {
    // The case the feature exists for: a looping session is mid-turn most of the
    // time, so gating on !running would hide the indicator almost always.
    const slots = [{ key: 'k', title: 'loop', running: true, messages: 5, last_message: 'stale' }]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { activeSlot: 'k', goalLoops: { k: { cycle_count: 3, max_cycles: 24 } }, slotStatusDetail: { k: { text: 'Reading gateway.log' } } },
      { activeSlotProp: 'k' },
    )
    expect(getByText('Loop 3/24')).toBeTruthy()
    expect(getByText(/Reading gateway\.log/)).toBeTruthy()
    expect(queryByText(/stale/)).toBeNull() // live detail wins over last_message
  })

  it('carries the subagent label as its detail when a wave is running', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5 }]
    const { getByText } = renderSidebar(slots, {
      goalLoops: { k: { cycle_count: 9, max_cycles: 24 } },
      slotActivity: { k: { toolLog: [], subagents: { a: sa('running'), b: sa('running') } } },
    })
    expect(getByText('Loop 9/24')).toBeTruthy()
    expect(getByText(/2 agents running/)).toBeTruthy()
  })

  it('is outranked by a pending approval — an owed decision must not read as unattended progress', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, pending_approval: true }]
    const { getByText, queryByText } = renderSidebar(slots, { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } })
    expect(getByText('Needs approval')).toBeTruthy()
    expect(queryByText('Loop 7/24')).toBeNull()
  })

  it('suppresses the blue "your turn" dot while looping', () => {
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, last_message: 'cycle output' }]
    const { queryByTitle } = renderSidebar(
      slots,
      { goalLoops: { k: { cycle_count: 7, max_cycles: 24 } } },
      { unreadSlots: ['k'] },
    )
    expect(queryByTitle(UNREAD_DOT_TITLE)).toBeNull()
  })

  it('keeps the dot and the plain last message when no loop is active', () => {
    // Guards the suppression against over-reach: an unread, idle, loop-free row
    // is exactly the case the dot was built for.
    const slots = [{ key: 'k', title: 'plain', running: false, messages: 5, last_message: 'final answer' }]
    const { getByText, queryByTitle, queryByText } = renderSidebar(slots, {}, { unreadSlots: ['k'] })
    expect(queryByTitle(UNREAD_DOT_TITLE)).toBeTruthy()
    expect(getByText('final answer')).toBeTruthy()
    expect(queryByText(/^Loop/)).toBeNull()
  })

  it('a looping slot passes the "In progress" session filter despite running=false', () => {
    // A loop spends its idle gaps with running=false (turn ended, waiting for
    // the next nudge). The row still says "Loop N/M", so the filter — and its
    // count — must keep surfacing it, mirroring the dynamic-workflow rule.
    // Pre-activate the filter via its persisted toggle (read at mount).
    localStorage.setItem('mc-session-running-only', '1')
    const slots = [
      { key: 'k-loop', title: 'loop session', running: false, messages: 5 },
      { key: 'k-idle', title: 'idle session', running: false, messages: 2 },
    ]
    const { getByText, queryByText } = renderSidebar(
      slots,
      { goalLoops: { 'k-loop': { cycle_count: 10, max_cycles: 40 } } },
    )
    expect(getByText('loop session')).toBeTruthy() // kept: active loop counts as in-progress
    expect(queryByText('idle session')).toBeNull() // filtered out: genuinely idle
  })
})

describe('chat sidebar — interrupted goal loop', () => {
  const STALLED_TITLE = 'Goal loop armed — last turn was interrupted; resume the chat or wait for the next cycle'

  it('flashes the whole card danger-red and reads "interrupted" when the loop session sits behind Resume', () => {
    // The reported bug: the turn died on a transient model error (trailing
    // error row → summary interrupted=true) and the pill kept pulsing as if a
    // cycle were executing — for up to idle_secs, until the next fire.
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, interrupted: true, last_message: 'cycle output' }]
    const { getByText, getByTitle, container } = renderSidebar(
      slots,
      { goalLoops: { k: { cycle_count: 47, max_cycles: 72 } } },
    )
    expect(getByText(/Loop 47\/72 — interrupted/)).toBeTruthy()
    const pill = getByTitle(STALLED_TITLE)
    expect(pill).toBeTruthy()
    // The pulse MEANS "work is happening" — the stalled glyph is STATIC danger
    // ink; the flashing lives on the row container (`session-loop-stalled`,
    // index.css), which pulses the WHOLE card red until the user resumes.
    const glyph = container.querySelector('.lucide-goal')
    expect(glyph).toBeTruthy()
    expect(glyph!.classList.contains('animate-pulse')).toBe(false)
    expect(glyph!.classList.contains('text-danger')).toBe(true)
    const row = container.querySelector('[data-session-row="k"]')
    expect(row).toBeTruthy()
    expect(row!.classList.contains('session-loop-stalled')).toBe(true)
    expect(container.querySelector('[title="Goal loop · cycle 47 of 72"]')).toBeNull()
  })

  it('keeps the pulse mid-turn — a trailing error row is superseded once a new turn runs', () => {
    const slots = [{ key: 'k', title: 'loop', running: true, messages: 5, interrupted: false }]
    const { getByText, getByTitle, container } = renderSidebar(
      slots,
      { activeSlot: 'k', goalLoops: { k: { cycle_count: 47, max_cycles: 72 } }, slotStatusDetail: { k: { text: 'Reading gateway.log' } } },
      { activeSlotProp: 'k' },
    )
    expect(getByText('Loop 47/72')).toBeTruthy()
    expect(getByTitle('Goal loop · cycle 47 of 72')).toBeTruthy()
    // Pulse lives on the gutter Goal glyph now (moved off the inline subtitle dot).
    expect(container.querySelector('.lucide-goal.animate-pulse')).toBeTruthy()
  })

  it('keeps the pulse while a subagent wave is executing on the loop\'s behalf', () => {
    // interrupted only describes the parent turn; children still working means
    // the loop IS working.
    const slots = [{ key: 'k', title: 'loop', running: false, messages: 5, interrupted: true }]
    const { getByText, container } = renderSidebar(slots, {
      goalLoops: { k: { cycle_count: 9, max_cycles: 24 } },
      slotActivity: { k: { toolLog: [], subagents: { a: sa('running') } } },
    })
    expect(getByText(/1 agent running/)).toBeTruthy()
    // The loop outranks the sub-agent count in the gutter, and a running child
    // means the loop is working — so the gutter Goal glyph pulses.
    expect(container.querySelector('.lucide-goal.animate-pulse')).toBeTruthy()
  })
})

describe('chat sidebar — interrupted ordinary session', () => {
  it('flags an idle interrupted turn as needing manual attention', () => {
    const slots = [{
      key: 'k', title: 'deployment follow-up', running: false, messages: 5,
      interrupted: true, last_message: 'Checking the deployment',
    }]
    const { getByTitle, queryByTitle, container } = renderSidebar(
      slots,
      {},
      { unreadSlots: ['k'] },
    )

    const row = container.querySelector('[data-session-row="k"]')
    expect(row?.textContent).toContain('Turn interrupted · Resume')
    expect(getByTitle('Turn interrupted · Resume')).toBeTruthy()
    expect(container.querySelector('.lucide-triangle-alert.text-danger')).toBeTruthy()
    // The specific attention state replaces the generic unread marker.
    expect(queryByTitle(UNREAD_DOT_TITLE)).toBeNull()
  })

  it('keeps live subagent activity above stale parent-turn interruption', () => {
    const slots = [{ key: 'k', title: 'delegated work', running: false, messages: 5, interrupted: true }]
    const { getByText, queryByText } = renderSidebar(slots, {
      slotActivity: { k: { toolLog: [], subagents: { a: sa('running') } } },
    })

    expect(getByText(/1 agent running/)).toBeTruthy()
    expect(queryByText('Turn interrupted')).toBeNull()
  })

  it('trusts snapshot child activity before reconnect details hydrate', () => {
    const slots = [
      {
        key: 'plain', title: 'delegated work', running: false, messages: 5,
        interrupted: true, subagents_running: true, last_message: 'Delegating',
      },
      {
        key: 'loop', title: 'loop work', running: false, messages: 5,
        interrupted: true, subagents_running: true, last_message: 'Delegating',
      },
      {
        key: 'orchestrating', title: 'planning', running: false, messages: 5,
        interrupted: true, orchestrating: true, last_message: 'Planning',
      },
      {
        key: 'queued', title: 'queued turn', running: false, messages: 5,
        interrupted: true, queue_depth: 1, last_message: 'Queued',
      },
    ]
    const { container } = renderSidebar(slots, {
      goalLoops: { loop: { cycle_count: 9, max_cycles: 24 } },
    })

    expect(container.querySelector('[data-session-row="plain"]')?.textContent).not.toContain('Turn interrupted')
    expect(container.querySelector('[data-session-row="plain"]')?.textContent).toContain('In progress')
    expect(container.querySelector('[data-session-row="loop"]')?.textContent).not.toContain('interrupted')
    expect(container.querySelector('[data-session-row="loop"]')?.textContent).toContain('In progress')
    expect(container.querySelector('[data-session-row="orchestrating"]')?.textContent).not.toContain('Turn interrupted')
    expect(container.querySelector('[data-session-row="orchestrating"]')?.textContent).toContain('In progress')
    expect(container.querySelector('[data-session-row="queued"]')?.textContent).not.toContain('Turn interrupted')
    expect(container.querySelector('[data-session-row="queued"]')?.textContent).toContain('In progress')
  })

  it('leaves completed ordinary sessions unflagged', () => {
    const slots = [{ key: 'k', title: 'complete', running: false, messages: 5, interrupted: false, last_message: 'Done' }]
    const { getByText, queryByText } = renderSidebar(slots, {})

    expect(getByText('Done')).toBeTruthy()
    expect(queryByText('Turn interrupted')).toBeNull()
  })
})
