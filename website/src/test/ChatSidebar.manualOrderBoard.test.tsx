/**
 * The tag board under the Manual sort. A column draws the order the list
 * draws (pinned rows on top, new sessions first, then the saved places), and
 * its cards keep the board's own drag: no card wears the manual drag handle,
 * no card offers the Alt+Arrow move, and no keystroke on a card writes the
 * manual order. The list's own Manual rules are pinned in
 * ChatSidebar.manualOrder.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { safeSetItem } from '../utils/safeStorage'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children)
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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

// One bare column (empty tag_ids: every session matches it), and the sort
// the gateway keeps set to Manual.
const mocks = vi.hoisted(() => ({
  columns: [{ id: 'col-all', name: 'Everything', tag_ids: [] as string[], mode: 'any' as const, order: 0 }],
  config: { dashboard: { session_sort: 'manual' } },
  setManualOrder: vi.fn(),
}))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: new Proxy({} as Record<string, unknown>, {
      get: (_t, p: string) => {
        if (p === 'chatFolders') return vi.fn().mockResolvedValue([])
        if (p === 'tagColumns') return vi.fn().mockResolvedValue(mocks.columns)
        if (p === 'kirocrewConfig') return vi.fn(async () => structuredClone(mocks.config))
        if (p === 'setManualOrder') return mocks.setManualOrder
        return vi.fn().mockResolvedValue([])
      },
    }),
  }
})
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const slot = (key: string, ts: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: `${key} title`, messages: 1, running: false, mode: '', created: ts, last_ts: ts, ...extra,
} as ChatSlot)

// Newest first would draw p, n, a, b. The saved places put b before a, and n,
// which the saved order does not hold yet, is new: it draws first.
const SLOTS = [
  slot('p', '2026-01-05T00:00:00Z', { pinned: true }),
  slot('a', '2026-03-01T00:00:00Z', { manual_rank: 1, manual_rev: 100 }),
  slot('b', '2026-02-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
  slot('n', '2026-04-01T00:00:00Z', { manual_rank: null, manual_rev: 100 }),
]

function renderBoard() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['tag-columns'], mocks.columns)
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['kirocrewConfig'], structuredClone(mocks.config))
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const column = (container: HTMLElement) => {
  const col = container.querySelector('[data-testid="column-col-all"]')
  expect(col, 'column col-all missing').toBeTruthy()
  return col as HTMLElement
}
const cardOf = (container: HTMLElement, key: string) => {
  const row = column(container).querySelector(`[data-slot-key="${key}"] [data-session-row]`)
  expect(row, `card ${key} missing`).toBeTruthy()
  return row as HTMLElement
}
/** Runs every pending promise chain inside act: the providers' mount-time
 *  requests, and any write a keystroke queued (several hops deep). */
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  // Flat inside the board: the column holds every matching session as a row.
  safeSetItem('mc-sidebar-flat-view', '1')
})
afterEach(() => vi.clearAllMocks())

describe('the tag board under Manual', () => {
  it('draws the manual order: pinned on top, new sessions first, then the saved places', async () => {
    const { container } = renderBoard()
    await settle()
    const keys = Array.from(column(container).querySelectorAll('[data-slot-key]'))
      .map(el => el.getAttribute('data-slot-key'))
    expect(keys).toEqual(['p', 'n', 'b', 'a'])
  })

  it('keeps the board\'s own drag: no manual handle, no Alt+Arrow move, no manual write', async () => {
    const { container } = renderBoard()
    await settle()
    expect(column(container).querySelectorAll('[data-testid="session-row-drag-handle"]')).toHaveLength(0)
    // b has a row below it (n, b, a), so a list row here would move.
    const card = cardOf(container, 'b')
    expect(card.getAttribute('aria-keyshortcuts')).toBeNull()
    card.focus()
    // fireEvent returns false when a handler called preventDefault.
    expect(fireEvent.keyDown(card, { key: 'ArrowDown', altKey: true })).toBe(true)
    await settle()
    expect(mocks.setManualOrder).not.toHaveBeenCalled()
  })
})
