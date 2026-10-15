/**
 * Chat sidebar — a session row names which of a same-name pair it runs (#19472).
 *
 * A crewmate and a custom agent of one name read alike in the session list, so a
 * row whose agent has such a pair in the picker's rows says which memory the
 * session runs on. Rows for every other agent keep their label. Scaffolding
 * mirrors ChatSidebar.defaultAgentMarker.test.tsx.
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
import type { ChatSlot } from '../types'
import type { MemorySourceRow } from '../lib/agentMemorySource'

const DEFAULT_AGENT = 'kirocrew'
// The clock is pinned to NOW in beforeEach, so the rows' age is a fixed minute
// whatever day the file runs. The dormant-session collapse is also pinned off.
const NOW = new Date('2026-10-16T12:00:00Z').getTime()
const LAST_TS = new Date(NOW - 60_000).toISOString()

const CHOICES: MemorySourceRow[] = [
  { name: 'reviewer', selection_kind: 'member', memory_store: 'member-reviewer' },
  { name: 'reviewer', selection_kind: 'template' },
  { name: DEFAULT_AGENT, selection_kind: 'template' },
]

const ROWS: ChatSlot[] = [
  { key: 'k-member', title: 'on-member', running: false, messages: 2, agent: 'reviewer', agent_kind: 'member', tags: [], last_ts: LAST_TS },
  { key: 'k-template', title: 'on-template', running: false, messages: 2, agent: 'reviewer', agent_kind: 'template', tags: [], last_ts: LAST_TS },
  { key: 'k-kindless', title: 'kindless', running: false, messages: 2, agent: 'reviewer', tags: [], last_ts: LAST_TS },
  { key: 'k-plain', title: 'plain', running: false, messages: 2, agent: DEFAULT_AGENT, tags: [], last_ts: LAST_TS },
] as unknown as ChatSlot[]

function renderSidebar(rowChoices?: MemorySourceRow[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: ROWS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {},
      history: [], historyHasMore: false, historyOffset: 0,
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
              slots={ROWS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent={DEFAULT_AGENT}
              installedAgents={[]}
              rowChoices={rowChoices}
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

/** The agent-name span's rendered label, whitespace-normalised. */
function label(line: HTMLElement): string {
  const span = line.querySelector('span')
  if (!span) throw new Error('meta line has no label span')
  return (span.textContent ?? '').replace(/\u00A0/g, '').trim()
}

beforeEach(() => {
  vi.setSystemTime(NOW)
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  window.history.replaceState({}, '', '/chat')
})
afterEach(() => {
  vi.useRealTimers()
  window.history.replaceState({}, '', '/')
  vi.clearAllMocks()
})

describe('chat sidebar session row — same-name memory mark (#19472)', () => {
  it('says own memory on a mate session and shared memory on a custom-agent one', () => {
    const { container } = renderSidebar(CHOICES)
    expect(label(sessionMetaLine(container, 'on-member'))).toBe('reviewer · own memory')
    expect(label(sessionMetaLine(container, 'on-template'))).toBe('reviewer · shared memory')
  })

  it('reads a kind-less session as the mate, as name-first resolution does', () => {
    const { container } = renderSidebar(CHOICES)
    expect(label(sessionMetaLine(container, 'kindless'))).toBe('reviewer · own memory')
  })

  it('leaves an agent without a same-name pair unmarked', () => {
    const { container } = renderSidebar(CHOICES)
    expect(label(sessionMetaLine(container, 'plain'))).toBe(DEFAULT_AGENT)
  })

  it('repeats the marked label in the title for a truncated row', () => {
    const { container } = renderSidebar(CHOICES)
    const span = sessionMetaLine(container, 'on-member').querySelector('span')!
    expect(span.getAttribute('title')).toBe('reviewer · own memory')
  })

  it('marks nothing when the host passes no picker rows', () => {
    const { container } = renderSidebar()
    expect(label(sessionMetaLine(container, 'on-member'))).toBe('reviewer')
  })
})
