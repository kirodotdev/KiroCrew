/**
 * Chat sidebar — the INHERITED-DEFAULT marker on session and history rows.
 *
 * A record with no stored agent resolves whatever the default is AT RUN TIME. A
 * record pinned to the alias that happens to be the current default resolves that
 * alias forever. Those are different facts with different consequences — change
 * the default and only the first one moves — and the row is where a user reads
 * them.
 *
 * Before this, they were indistinguishable in every value the row holds:
 *
 *   - `agent || defaultAgent` collapses both to the same alias,
 *   - `effective_agent` is "" for both, because the backend reports nothing when
 *     an alias resolves to itself AND nothing when the requested name is empty
 *     (`resolve_effective_agent` returns "" for a falsy argument), so the
 *     divergence marker cannot fire for either,
 *   - the source tint is keyed on the alias, which is equal in both.
 *
 * So no amount of reading the row told the two apart. PR #6515 fixed exactly this
 * for the agents rail and the Schedule page via the shared `agentOrDefaultLabel`;
 * these two rows read the SAME field from the SAME slots slice and were left bare
 * (#6529).
 *
 * The pair in `PAIR_SLOTS` / `PAIR_HISTORY` is the falsifying pair: same resolved
 * alias, opposite stored state. A regression that drops the marker makes the two
 * labels equal again, which `renders the two states differently` catches directly
 * rather than by matching a string.
 *
 * `tints by the stored alias, not the decorated label` guards the other half of
 * the change: the label is DISPLAY-only and the bare alias remains the resolution
 * key. Feeding the decorated string into `installedAgents.find(a => a.name ===
 * ...)` would miss, and the row would silently lose its source tint.
 *
 * Mock scaffolding mirrors ChatSidebar.effectiveAgent.test.tsx (session rows) and
 * ChatSidebar.historyDeepLink.test.tsx (the `?history=1` deep link that opens the
 * Older Sessions pane, which is collapsed by default). i18n is pinned to English
 * by integration/setup.ts, so the marker's visible spelling is assertable.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (the test DOM can't run projection).
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
import type { ChatSlot, ChatHistoryItem } from '../types'

const DEFAULT_AGENT = 'kirocrew'
/** The marker's visible spelling, English catalog. */
const MARKED = `${DEFAULT_AGENT} \u00b7 default`

// RELATIVE, not a literal date: the dormant-session collapse measures age
// against Date.now(), and a hardcoded timestamp turns a passing file into a
// time bomb that starts failing days later with no code change (this happened
// to ChatSidebar.effectiveAgent.test.tsx). The collapse is also pinned off in
// beforeEach; both together keep rows queryable regardless of the file's age.
const LAST_TS = new Date(Date.now() - 60_000).toISOString()

/** Same resolved alias, opposite stored state — the pair the marker separates. */
const PAIR_SLOTS: ChatSlot[] = [
  // Legacy / agent-less: resolves the CURRENT default at run time.
  { key: 'k-inherited', title: 'inherited', running: false, messages: 2, agent: '', tags: [], last_ts: LAST_TS },
  // Pinned to the alias that happens to be today's default.
  { key: 'k-pinned', title: 'pinned', running: false, messages: 2, agent: DEFAULT_AGENT, tags: [], last_ts: LAST_TS },
] as unknown as ChatSlot[]

const PAIR_HISTORY: ChatHistoryItem[] = [
  // An archived session whose JSONL metadata never recorded an agent.
  { key: 'dashboard_h-inherited', title: 'h-inherited', modified: Math.floor(Date.now() / 1000), created: LAST_TS },
  { key: 'dashboard_h-pinned', title: 'h-pinned', agent: DEFAULT_AGENT, modified: Math.floor(Date.now() / 1000), created: LAST_TS },
] as unknown as ChatHistoryItem[]

function renderSidebar(opts: {
  slots?: ChatSlot[]
  history?: ChatHistoryItem[]
  defaultAgent?: string
  installedAgents?: { name: string; source: string }[]
} = {}) {
  const slots = opts.slots ?? PAIR_SLOTS
  const history = opts.history ?? []
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {},
      history, historyHasMore: false, historyOffset: history.length,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['chat-tags'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={history} historyHasMore={false}
              defaultAgent={opts.defaultAgent ?? DEFAULT_AGENT}
              installedAgents={opts.installedAgents ?? []}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The meta line of a SESSION row, found via the title text beneath it. */
function sessionMetaLine(container: HTMLElement, title: string): HTMLElement {
  const titleEl = [...container.querySelectorAll('div')].find(
    el => el.getAttribute('title') === title && el.className.includes('text-[13px]'),
  )
  if (!titleEl) throw new Error(`no session row titled "${title}"`)
  const line = titleEl.previousElementSibling as HTMLElement | null
  if (!line?.className.includes('session-agent-label')) throw new Error(`no meta line above "${title}"`)
  return line
}

/** The meta line of a HISTORY row: the row itself carries the title. */
function historyMetaLine(container: HTMLElement, title: string): HTMLElement {
  const row = [...container.querySelectorAll('div[role="button"]')].find(
    el => el.getAttribute('title') === title,
  )
  if (!row) throw new Error(`no history row titled "${title}"`)
  const line = row.querySelector('.session-agent-label') as HTMLElement | null
  if (!line) throw new Error(`no meta line in history row "${title}"`)
  return line
}

/** The agent-name span's rendered label, whitespace-normalised. */
function label(line: HTMLElement): string {
  const span = line.querySelector('span')
  if (!span) throw new Error('meta line has no label span')
  return (span.textContent ?? '').replace(/\u00A0/g, '').trim()
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  window.history.replaceState({}, '', '/chat')
})
afterEach(() => {
  window.history.replaceState({}, '', '/')
  vi.clearAllMocks()
})

describe('chat sidebar session row — inherited-default marker', () => {
  it('marks a row that resolves the default at run time', () => {
    const { container } = renderSidebar()
    expect(label(sessionMetaLine(container, 'inherited'))).toBe(MARKED)
  })

  it('leaves an explicit pin bare', () => {
    // The pin is a decision the user made; decorating it would claim the row
    // follows the default when it does not.
    const { container } = renderSidebar()
    expect(label(sessionMetaLine(container, 'pinned'))).toBe(DEFAULT_AGENT)
  })

  it('renders the two states differently', () => {
    // The property the whole change exists for, asserted without depending on
    // the marker's spelling: equal labels here means the row is back to being
    // unable to express the difference.
    const { container } = renderSidebar()
    const inherited = label(sessionMetaLine(container, 'inherited'))
    const pinned = label(sessionMetaLine(container, 'pinned'))
    expect(inherited).not.toBe(pinned)
    // Still NAMES the alias — the marker adds a fact, it does not replace one.
    expect(inherited.startsWith(DEFAULT_AGENT)).toBe(true)
  })

  it('is readable on a truncated row — the title repeats the visible label', () => {
    // The span is `truncate` and the marker lengthens it, so the label can be
    // clipped on a narrow sidebar. A `title` that repeats the visible text is
    // the row's existing convention (see the divergence marker) and the form
    // AUTOSDE's session-row rule sanctions for the agent/meta line.
    const { container } = renderSidebar()
    const span = sessionMetaLine(container, 'inherited').querySelector('span')!
    expect(span.getAttribute('title')).toBe(MARKED)
  })

  it('tints by the stored alias, not the decorated label', () => {
    // The label is DISPLAY-only; the bare alias stays the resolution key. Were
    // the decorated string fed to the source lookup it would not match any
    // installed agent and the row would quietly fall back to the muted tone.
    const { container } = renderSidebar({
      installedAgents: [{ name: DEFAULT_AGENT, source: 'package' }],
    })
    expect(sessionMetaLine(container, 'inherited').className).toContain('text-[var(--aim)]')
  })

  it('keeps the blank placeholder when there is no default to inherit', () => {
    // Nothing is bound and nothing is resolved — during the boot window the
    // default has not landed yet. The row holds its line with a blank rather
    // than printing the literal word, which would be a claim, not a label.
    const { container } = renderSidebar({
      slots: [{ key: 'k-bare', title: 'bare', running: false, messages: 1, agent: '', tags: [], last_ts: LAST_TS }] as unknown as ChatSlot[],
      defaultAgent: '',
    })
    const line = sessionMetaLine(container, 'bare')
    expect(label(line)).toBe('')
    expect(line.querySelector('span')!.getAttribute('title')).toBeNull()
  })
})

describe('chat sidebar history row — inherited-default marker', () => {
  it('marks an archived session whose metadata recorded no agent', () => {
    window.history.replaceState({}, '', '/chat?history=1')
    const { container } = renderSidebar({ slots: [], history: PAIR_HISTORY })
    expect(label(historyMetaLine(container, 'h-inherited'))).toBe(MARKED)
  })

  it('leaves an archived pin bare, and the two differ', () => {
    window.history.replaceState({}, '', '/chat?history=1')
    const { container } = renderSidebar({ slots: [], history: PAIR_HISTORY })
    const pinned = label(historyMetaLine(container, 'h-pinned'))
    expect(pinned).toBe(DEFAULT_AGENT)
    expect(label(historyMetaLine(container, 'h-inherited'))).not.toBe(pinned)
  })
})
