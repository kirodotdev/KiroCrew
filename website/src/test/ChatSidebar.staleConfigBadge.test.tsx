/**
 * Chat sidebar session row -- where the stale-config mark sits.
 *
 * A session row has a fixed set of stacked lines, and new per-session metadata
 * goes on the agent/meta line directly AFTER the agent name (website
 * AUTOSDE.yaml, session-row-fixed-height). These tests pin that placement: the
 * mark is the agent name's next sibling on the meta line, ahead of the
 * incognito/temporary glyphs, and a current session renders no mark at all.
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
import type { ChatSlot } from '../types'

// RELATIVE, not a literal date: the sidebar's dormant-session collapse
// (staleCollapse.ts, default threshold 2 days) hides any row whose last
// activity is older than the threshold, and these tests address rows by
// title, so a hardcoded date turns into a time bomb — the fixture aged past
// the threshold two days after it was written and all ten tests started
// failing on every PR at once. A minute ago is always fresh.
const LAST_TS = new Date(Date.now() - 60_000).toISOString()

const SLOTS: ChatSlot[] = [
  { key: 'k-stale', title: 'stale', running: false, messages: 2, agent: 'mochi', tags: [], last_ts: LAST_TS, config_stale: true, config_stale_inputs: '~/.kiro/agents/mochi.json', memory_mode: 'incognito' },
  { key: 'k-current', title: 'current', running: false, messages: 2, agent: 'mochi', tags: [], last_ts: LAST_TS },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
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
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The meta line for a row, found via the title text that sits beneath it. */
function metaLineFor(container: HTMLElement, title: string): HTMLElement {
  const titleEl = [...container.querySelectorAll('div')].find(
    el => el.getAttribute('title') === title && el.className.includes('text-[13px]'),
  )
  if (!titleEl) throw new Error(`no row titled "${title}"`)
  const line = titleEl.previousElementSibling as HTMLElement | null
  if (!line?.className.includes('session-agent-label')) throw new Error(`no meta line above "${title}"`)
  return line
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar -- stale-config mark placement', () => {
  it('sits on the meta line directly after the agent name', () => {
    const { container } = renderSidebar()
    const line = metaLineFor(container, 'stale')
    const badge = line.querySelector('[data-testid="stale-config-badge"]') as HTMLElement | null
    expect(badge).not.toBeNull()
    // A direct child of the meta line: no stacked line of its own.
    expect(badge!.parentElement).toBe(line)
    expect(badge!.previousElementSibling?.textContent).toBe('mochi')
  })

  it('comes ahead of the memory-mode glyphs', () => {
    const { container } = renderSidebar()
    const line = metaLineFor(container, 'stale')
    const children = [...line.children]
    const badge = line.querySelector('[data-testid="stale-config-badge"]')!
    const incognito = line.querySelector('.lucide-eye-off')!.closest('span')!
    expect(children.indexOf(badge)).toBeLessThan(children.indexOf(incognito))
  })

  it('renders no mark on a session whose config is current', () => {
    const { container } = renderSidebar()
    expect(metaLineFor(container, 'current').querySelector('[data-testid="stale-config-badge"]')).toBeNull()
  })
})
