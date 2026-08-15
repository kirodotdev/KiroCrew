/**
 * Chat sidebar session-row STATUS GUTTER — the fixed-width slot left of the
 * headline that holds the row's single status glyph.
 *
 * What is pinned here, and why each would regress silently:
 *   1. exactly ONE glyph per row, drawn from the subtitle chain's own precedence,
 *      so glyph and secondary line can never name different states,
 *   2. an owed decision (approval, question) outranks every "working" signal — a
 *      decision rendered as work in progress is how an owed approval is missed,
 *   3. `running` draws a SPINNER, not a dot,
 *   4. the unread "your turn" dot lives in the gutter, NOT absolutely positioned
 *      at the row's right edge, and yields to any more specific state,
 *   5. the gutter reserves its width even with no glyph, so every row's meta
 *      line, headline, secondary line and chips share one left edge,
 *   6. the glyph carries an accessible name — it is the gutter's only content.
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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
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

function renderSidebar(slots: ChatSlot[], unread: string[] = [], chat: Record<string, unknown> = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, ...chat } as unknown as RootState['chat'],
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

/** The gutter is the row's FIRST child; the content column is its sibling. */
function gutterOf(container: HTMLElement): HTMLElement {
  const row = container.querySelector('.session-row')
  if (!row) throw new Error('no .session-row rendered')
  return row.firstElementChild as HTMLElement
}

const slot = (over: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: 'k1', title: 'a-session', running: false, messages: 2, agent: 'kirocrew', ...over }) as ChatSlot

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — status gutter', () => {
  it('draws a spinner, not a dot, while the agent is working', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const g = gutterOf(container)
    expect(g.querySelector('.animate-spin')).toBeTruthy()
    // A pulsing dot is what the spinner replaces; it must not come back.
    expect(g.querySelector('.rounded-full')).toBeNull()
  })

  it('puts the unread "your turn" dot in the gutter, not at the row\'s right edge', () => {
    const { container } = renderSidebar([slot()], ['k1'])
    expect(gutterOf(container).querySelector('.rounded-full')).toBeTruthy()
    // The old treatment was an absolutely-positioned dot pinned right.
    expect(container.querySelector('.session-row .absolute.right-1\\.5.rounded-full')).toBeNull()
  })

  it('reserves the gutter width for an idle, read row so text stays aligned', () => {
    const { container } = renderSidebar([slot({ last_message: 'done' })])
    const g = gutterOf(container)
    expect(g).toBeTruthy()
    expect(g.className).toMatch(/\bw-3\b/)        // width still declared
    expect(g.querySelector('svg, .rounded-full')).toBeNull()   // but nothing drawn
    expect(g.getAttribute('aria-hidden')).toBe('true')
  })

  it('renders exactly one status glyph, never two', () => {
    // Running AND unread: the old right-edge dot coexisted with the running
    // subtitle glyph. One slot cannot show both.
    const { container } = renderSidebar([slot({ running: true })], ['k1'])
    const row = container.querySelector('.session-row')!
    expect(row.querySelectorAll('.animate-spin, .rounded-full')).toHaveLength(1)
  })

  it('ranks an owed approval above every working signal', () => {
    const { container } = renderSidebar([slot({ running: true, pending_approval: true })], ['k1'])
    const g = gutterOf(container)
    expect(g.getAttribute('aria-label')).toBe('Needs approval')
    expect(g.querySelector('.animate-spin')).toBeNull()   // not "working"
    // A shield, not a bare dot: an owed decision earns a shape of its own, and
    // the accent "your turn" dot is now the only bare dot in the gutter. A shield
    // also reads as "permission" rather than "message", which is what an approval
    // actually is — it gates a tool call, it is not something to reply to.
    expect(g.querySelector('.lucide-shield-check')).toBeTruthy()
    expect(g.querySelector('.rounded-full')).toBeNull()
  })

  it('ranks an unanswered question above working, and below an approval', () => {
    const { container: q } = renderSidebar([slot({ running: true, needs_input: true })])
    expect(gutterOf(q).getAttribute('aria-label')).toBe('Needs your answer')
    // A question mark, not the plain speech bubble the channel-origin glyph uses
    // elsewhere in this row — an owed answer must not look like provenance.
    expect(gutterOf(q).querySelector('.lucide-message-circle-question-mark')).toBeTruthy()

    const { container: both } = renderSidebar([slot({ needs_input: true, pending_approval: true })])
    expect(gutterOf(both).getAttribute('aria-label')).toBe('Needs approval')
  })

  it('marks a goal loop with the Goal icon, not a bare dot', () => {
    // A loop is a distinct MODE, so it earns a distinct mark; the pulsing dot it
    // replaced was indistinguishable from the unread dot at a glance.
    const { container } = renderSidebar(
      [slot()],
      [],
      { goalLoops: { k1: { active: true, cycle_count: 7, max_cycles: 24 } } },
    )
    const g = gutterOf(container)
    expect(g.getAttribute('aria-label')).toBe('Loop 7/24')
    expect(g.querySelector('.lucide-goal')).toBeTruthy()
    expect(g.querySelector('.rounded-full')).toBeNull()
  })

  it('gives the glyph an accessible name, since it is the gutter\'s only content', () => {
    const { container } = renderSidebar([slot()], ['k1'])
    const g = gutterOf(container)
    expect(g.getAttribute('role')).toBe('img')
    expect(g.getAttribute('aria-label')).toBe('Agent finished — your turn')
  })

  it('keeps the coloured text label in the secondary line', () => {
    // The glyph moving to the gutter must not take its label with it — "Needs
    // approval" has to stay readable as a phrase.
    const { getByText } = renderSidebar([slot({ pending_approval: true })])
    expect(getByText('Needs approval')).toBeTruthy()
  })

  it('leaves no inline glyph beside the secondary line', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const col = gutterOf(container).nextElementSibling as HTMLElement
    // children: [meta line, headline, secondary line]
    const sub = col.children[2]
    expect(sub?.textContent).toContain('Thinking')
    expect(sub?.querySelector('svg, .rounded-full')).toBeNull()
  })
})
