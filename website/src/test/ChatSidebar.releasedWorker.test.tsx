/**
 * Chat sidebar — the released marker on a crewmate's worker row.
 *
 * The idle sweep stops a worker's process and keeps its conversation; the next
 * message resumes it. The slot frame carries `process: live | released` for a
 * worker tab, and the row shows a released one with a quiet marker at rest, so
 * a stopped worker reads differently from a running one.
 *
 * Scaffolding mirrors ChatSidebar.defaultAgentMarker.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
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

// One pinned clock: the rows are a minute old against it, so the dormant-session
// collapse never hides them however old this file gets.
const NOW = new Date('2026-10-23T12:00:00Z')
const LAST_TS = new Date(NOW.getTime() - 60_000).toISOString()

function renderSidebar(slots: ChatSlot[]) {
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
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="kirocrew" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The meta line of the session row titled *title* (the markers live there). */
function row(container: HTMLElement, title: string): HTMLElement {
  const titleEl = [...container.querySelectorAll('div')].find(
    el => el.getAttribute('title') === title && el.className.includes('text-[13px]'),
  )
  if (!titleEl) throw new Error(`no session row titled "${title}"`)
  const meta = titleEl.previousElementSibling as HTMLElement | null
  if (!meta) throw new Error(`no meta line above "${title}"`)
  return meta
}

beforeEach(() => {
  vi.setSystemTime(NOW)
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  window.history.replaceState({}, '', '/chat')
})
afterEach(() => {
  window.history.replaceState({}, '', '/')
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('chat sidebar session row — released worker marker', () => {
  const SLOTS = [
    { key: 'w-asleep', title: 'asleep', running: false, messages: 2, agent: 'kirocrew', created_by: 'dashboard_member-mate', process: 'released', tags: [], last_ts: LAST_TS },
    { key: 'w-awake', title: 'awake', running: false, messages: 2, agent: 'kirocrew', created_by: 'dashboard_member-mate', process: 'live', tags: [], last_ts: LAST_TS },
    { key: 'k-ordinary', title: 'ordinary', running: false, messages: 2, agent: 'kirocrew', tags: [], last_ts: LAST_TS },
  ] as unknown as ChatSlot[]

  it('marks a released worker, named for screen readers', () => {
    const { container } = renderSidebar(SLOTS)
    const marker = row(container, 'asleep').querySelector('[data-testid="session-row-released"]')
    expect(marker).not.toBeNull()
    expect(marker!.getAttribute('aria-label')).toBe('Asleep: wakes on the next message')
  })

  it('leaves a live worker and an ordinary session unmarked', () => {
    const { container } = renderSidebar(SLOTS)
    expect(row(container, 'awake').querySelector('[data-testid="session-row-released"]')).toBeNull()
    expect(row(container, 'ordinary').querySelector('[data-testid="session-row-released"]')).toBeNull()
  })

  it('shows the marker even when the row has no activity time', () => {
    const { container } = renderSidebar([
      { key: 'w-quiet', title: 'quiet', running: false, messages: 0, agent: 'kirocrew', created_by: 'dashboard_member-mate', process: 'released', tags: [] },
    ] as unknown as ChatSlot[])
    expect(row(container, 'quiet').querySelector('[data-testid="session-row-released"]')).not.toBeNull()
  })
})
