/**
 * Chat sidebar: the incognito and temporary markers on session rows and on
 * Older Sessions rows carry an accessible name.
 *
 * Both markers say a memory-privacy fact about the session (no memory writes,
 * or no memory reads or writes). A `title` alone is not read by a screen reader,
 * so they take `role="img"` and an `aria-label`, like the muted and released
 * markers beside them.
 *
 * Scaffolding mirrors ChatSidebar.releasedWorker.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
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
  // One component per tag: a fresh type on every read would remount the subtree.
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
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
import type { ChatHistoryItem, ChatSlot } from '../types'

// One pinned clock: the rows are a minute old against it, so the dormant-session
// collapse never hides them however old this file gets.
const NOW = new Date('2026-10-23T12:00:00Z')
const LAST_TS = new Date(NOW.getTime() - 60_000).toISOString()

function renderSidebar(slots: ChatSlot[], history: ChatHistoryItem[] = []) {
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
              defaultAgent="kirocrew" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The meta line of the row titled *title* (the markers live there). */
function row(container: HTMLElement, title: string): HTMLElement {
  const titleEl = [...container.querySelectorAll('div')].find(
    el => (el.getAttribute('title') === title || el.textContent === title) && el.className.includes('text-[13px]'),
  )
  if (!titleEl) throw new Error(`no row titled "${title}"`)
  const meta = titleEl.previousElementSibling as HTMLElement | null
  if (!meta) throw new Error(`no meta line above "${title}"`)
  return meta
}

const INCOGNITO = 'Incognito — no memory writes'
const TEMPORARY = 'Temporary — no memory reads or writes'

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

describe('chat sidebar memory-mode markers are named for screen readers', () => {
  it('names the incognito and temporary markers on a session row', () => {
    const { container } = renderSidebar([
      { key: 'k-inc', title: 'private', running: false, messages: 2, agent: 'kirocrew', memory_mode: 'incognito', tags: [], last_ts: LAST_TS },
      { key: 'k-tmp', title: 'scratch', running: false, messages: 2, agent: 'kirocrew', memory_mode: 'temporary', tags: [], last_ts: LAST_TS },
    ] as unknown as ChatSlot[])
    expect(within(row(container, 'private')).getByRole('img', { name: INCOGNITO })).toBeTruthy()
    expect(within(row(container, 'scratch')).getByRole('img', { name: TEMPORARY })).toBeTruthy()
  })

  it('names the incognito and temporary markers on an older-session row', () => {
    const { container } = renderSidebar(
      [{ key: 'k-live', title: 'live', running: false, messages: 2, agent: 'kirocrew', tags: [], last_ts: LAST_TS }] as unknown as ChatSlot[],
      [
        { key: 'h-inc', title: 'old private', last_ts: LAST_TS, memory_mode: 'incognito' },
        { key: 'h-tmp', title: 'old scratch', last_ts: LAST_TS, memory_mode: 'temporary' },
      ] as unknown as ChatHistoryItem[],
    )
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    expect(within(row(container, 'old private')).getByRole('img', { name: INCOGNITO })).toBeTruthy()
    expect(within(row(container, 'old scratch')).getByRole('img', { name: TEMPORARY })).toBeTruthy()
  })
})
