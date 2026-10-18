/**
 * Chat sidebar session-row LIVE DOT — the small pulsing accent disc in front of
 * the session name while the session is working.
 *
 * What is pinned here, and why each would regress silently:
 *   1. the dot lights for exactly the working branches of the row's state
 *      resolver (a running turn, started sub-agents, reconnect live work), so it
 *      can never disagree with the secondary line under it,
 *   2. every stronger state that outranks those keeps the title bare: an owed
 *      approval or answer, an interrupted turn, a stalled loop — and a
 *      queued-only row, where nothing has started,
 *   3. it sits INSIDE the title line, ahead of the name, and leaves the row's
 *      own chrome alone: selection keeps its tint and the dot shows on top of it,
 *   4. it carries its state on its own, so it has a real accessible name,
 *   5. it pulses with the shared `animate-pulse` utility, which the global
 *      reduced-motion reset leaves on its solid frame.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { readIndexCss } from './indexCssSource'
import { sseSubagentQueued, sseSubagentSpawn } from '../store/chatSlice'
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

interface RenderOpts {
  unread?: string[]
  activeSlot?: string | null
  goalLoops?: Record<string, { cycle_count: number; max_cycles: number }>
  prepare?: (store: ReturnType<typeof createTestStore>) => void
}

function renderSidebar(slots: ChatSlot[], opts: RenderOpts = {}) {
  const { unread = [], activeSlot = null, goalLoops = {}, prepare } = opts
  const automations = Object.fromEntries(Object.entries(goalLoops).map(([slotKey, loop]) => [
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
      channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot, slotStatusDetail: {}, workflowRuns: {}, subagents: {}, slotActivity: {}, automations,
    } as unknown as RootState['chat'],
  })
  prepare?.(store)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={activeSlot} unreadSlots={unread}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const slot = (over: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: 'k1', title: 'a-session', running: false, messages: 2, agent: 'kirocrew', ...over }) as ChatSlot

function rowOf(container: HTMLElement): HTMLElement {
  const row = container.querySelector('.session-row')
  if (!row) throw new Error('no .session-row rendered')
  return row as HTMLElement
}

function titleOf(container: HTMLElement): HTMLElement {
  const title = rowOf(container).querySelector('[data-session-title]')
  if (!title) throw new Error('no session title rendered')
  return title as HTMLElement
}

function dotOf(container: HTMLElement): HTMLElement | null {
  return rowOf(container).querySelector('[data-testid="session-live-dot"]')
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — live dot on the session name', () => {
  it('leads the session name while a turn is running', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const title = titleOf(container)
    const dot = dotOf(container)
    expect(dot).toBeTruthy()
    // Inside the title line, ahead of the name: the eye scans names, not the
    // secondary line, in a long list.
    expect(title.contains(dot)).toBe(true)
    expect(title.firstChild).toBe(dot)
    expect(title.textContent).toBe('a-session')
    expect(dot!.className).toMatch(/\bsession-live-dot\b/)
  })

  it('names its state for assistive tech, since it leads no label of its own', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const dot = dotOf(container)!
    expect(dot.getAttribute('role')).toBe('img')
    expect(dot.getAttribute('aria-label')).toBe('Running')
    expect(dot.getAttribute('title')).toBe('Running')
    expect(dot.getAttribute('aria-hidden')).toBeNull()
  })

  it('lights for started sub-agents after the parent turn ended', () => {
    const { container } = renderSidebar([slot()], {
      prepare: store => { store.dispatch(sseSubagentSpawn({ slot: 'k1', id: 'a1', task: 't', agent: 'kirocrew' })) },
    })
    expect(dotOf(container)).toBeTruthy()
  })

  it('lights for live work a reconnect snapshot reports before its details arrive', () => {
    const { container } = renderSidebar([slot({ subagents_running: true })])
    expect(dotOf(container)).toBeTruthy()
  })

  it.each([
    ['an idle, read row', { last_message: 'done' }, {}],
    ['a finished row that is still unread', { last_message: 'done' }, { unread: ['k1'] }],
    ['a pending approval over a running turn', { running: true, pending_approval: true }, {}],
    ['an unanswered question over a running turn', { running: true, needs_input: true }, {}],
    ['an interrupted turn', { interrupted: true }, {}],
    ['a stalled goal loop', { interrupted: true }, { goalLoops: { k1: { cycle_count: 3, max_cycles: 10 } } }],
  ] as const)('stays off for %s', (_name, over, opts) => {
    const { container } = renderSidebar([slot(over as Partial<ChatSlot>)], opts as RenderOpts)
    expect(dotOf(container)).toBeNull()
  })

  it('keeps the unread dot as the only marker on a finished, unread row', () => {
    // The two dots name different states: "working now" and "finished, not yet
    // viewed". A finished row carries only the second.
    const { container } = renderSidebar([slot({ last_message: 'done' })], { unread: ['k1'] })
    const unread = rowOf(container).querySelector('span[role="img"][aria-label="Agent finished — your turn"]')
    expect(unread).toBeTruthy()
    expect(dotOf(container)).toBeNull()
  })

  it('stays off while every sub-agent is still queued, since nothing has started', () => {
    const { container } = renderSidebar([slot()], {
      prepare: store => { store.dispatch(sseSubagentQueued({ slot: 'k1', queued: 2 })) },
    })
    expect(container.querySelector('[data-testid="session-subagents-waiting"]')).toBeTruthy()
    expect(dotOf(container)).toBeNull()
  })

  it('shows on a selected row without replacing the selection tint', () => {
    const { container } = renderSidebar([slot({ running: true })], { activeSlot: 'k1' })
    expect(rowOf(container).className).toMatch(/\bsession-active\b/)
    expect(dotOf(container)).toBeTruthy()
  })

  it('agrees with the secondary line: the dot and the working spinner appear together', () => {
    const running = renderSidebar([slot({ running: true })])
    expect(rowOf(running.container).querySelector('.animate-spin')).toBeTruthy()
    expect(dotOf(running.container)).toBeTruthy()
    running.unmount()

    const approval = renderSidebar([slot({ running: true, pending_approval: true })])
    expect(rowOf(approval.container).querySelector('.animate-spin')).toBeNull()
    expect(dotOf(approval.container)).toBeNull()
  })
})

describe('chat sidebar — live dot styling', () => {
  const css = readIndexCss()

  it('is an accent disc that claims none of the row chrome', () => {
    const rule = /\.session-live-dot\{([^}]*)\}/.exec(css)
    expect(rule, 'no .session-live-dot rule').toBeTruthy()
    const body = rule![1]
    expect(body).toMatch(/background:var\(--accent\)/)
    expect(body).toMatch(/border-radius:50%/)
    // No position offsets: the left edge belongs to the session colour bar and
    // the recency tint, so the dot stays in the title's text flow.
    expect(body).not.toMatch(/position:/)
  })

  it('pulses with the shared animate-pulse utility, not a keyframe of its own', () => {
    const { container } = renderSidebar([slot({ running: true })])
    expect(dotOf(container)!.className).toMatch(/\banimate-pulse\b/)
    const rule = /\.session-live-dot\{([^}]*)\}/.exec(css)![1]
    expect(rule).not.toMatch(/animation/)
  })

  it('stays solid under reduced motion through the global motion reset', () => {
    // The global rule cuts every animation to one near-instant run that ends on
    // the pulse's full-opacity frame. It skips the transcript's virtualised
    // scroller, so the dot must not sit inside one.
    expect(css).toMatch(/@media\(prefers-reduced-motion:reduce\)\{\*:not\(\[data-virtuoso-scroller\] \*\),\*::before,\*::after\{animation-duration:0\.01ms !important;animation-iteration-count:1 !important\}\}/)
    const { container } = renderSidebar([slot({ running: true })])
    expect(dotOf(container)!.closest('[data-virtuoso-scroller]')).toBeNull()
  })
})
