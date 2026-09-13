/**
 * The pinned section follows the active sort key until the user reorders it.
 *
 * Membership bookkeeping persists a pinned ORDER from the first pin onwards, so a
 * stored order is present long before anyone drags a row. Consuming rank on that
 * basis froze the pinned section at whatever order a pin toggle captured, and the
 * sidebar's chosen sort then had no effect on any pinned row -- the more sessions a
 * user pins, the less the sort control does. These cases pin the intent boundary:
 * stored-but-not-reordered follows the sort, reordered wins over it.
 *
 * Both assertions read RENDERED row order rather than the comparator in isolation:
 * the defect was in which rank the sidebar handed the comparator, so a unit test on
 * `comparePinnedThenSort` passes either way and proves nothing about the wiring.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import {
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  forgetPinnedSessionOrderManual,
  markPinnedSessionOrderManual,
  movePinnedSession,
  readPinnedSessionOrder,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

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

vi.mock('../utils/pinMutationsInFlight', () => ({
  readPinMutationKeysInFlight: () => pinsInFlight,
  publishPinMutationKeysInFlight: () => {},
  pinMutationsAreInFlight: () => pinsInFlight.length > 0,
}))

vi.mock('../hooks/useSessionActions', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useSessionActions')>()),
}))
let pinsInFlight: string[] = []

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const fixtures: { chatTags: unknown[]; tagColumns: unknown[]; chatFolders: unknown[] } = {
  chatTags: [], tagColumns: [], chatFolders: [],
}

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in fixtures) return vi.fn().mockResolvedValue(fixtures[prop as keyof typeof fixtures])
      return vi.fn().mockResolvedValue([])
    },
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

// Three pinned sessions whose activity order (newest first) is c, b, a -- chosen to
// CONTRADICT the stored order seeded below, so the two orders cannot both pass.
const PINNED: ChatSlot[] = [
  { key: 'a', title: 'alpha', running: false, messages: 1, pinned: true, last_turn_ts: '2026-01-01T00:00:00Z' },
  { key: 'b', title: 'bravo', running: false, messages: 1, pinned: true, last_turn_ts: '2026-01-02T00:00:00Z' },
  { key: 'c', title: 'charlie', running: false, messages: 1, pinned: true, last_turn_ts: '2026-01-03T00:00:00Z' },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[]) {
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
  qc.setQueryData(['tag-columns'], [])
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

/** Rendered list-scope row keys, in DOM order. */
function renderedOrder(): string[] {
  return [...document.querySelectorAll<HTMLElement>('[data-session-row][data-session-scope="list"]')]
    .map(el => el.dataset.sessionRow ?? '')
    .filter(Boolean)
}

beforeEach(() => {
  localStorage.clear()
  pinsInFlight = []
})
afterEach(() => vi.clearAllMocks())

describe('pinned order applies only after an explicit reorder', () => {
  it('follows the sort key when an order was stored without the user reordering', () => {
    // Exactly what a pin toggle leaves behind: an order, and no statement of intent.
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    renderSidebar(PINNED)
    // date-desc, not the stored a/b/c. Before the fix this returned ['a','b','c'].
    expect(renderedOrder()).toEqual(['c', 'b', 'a'])
  })

  it('honours the stored order once the user has reordered', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
    renderSidebar(PINNED)
    expect(renderedOrder()).toEqual(['a', 'b', 'c'])
  })

  it('permutes the VISIBLE order on the first reorder, not the hidden stored one', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    renderSidebar(PINNED)
    expect(renderedOrder()).toEqual(['c', 'b', 'a'])

    const topRow = document.querySelector<HTMLElement>('[data-session-row="c"][data-session-scope="list"]')
    if (!topRow) throw new Error('no list-scope row for c')
    topRow.focus()
    fireEvent.keyDown(topRow, { key: 'ArrowDown', altKey: true })

    expect(readPinnedSessionOrder()).toEqual(['b', 'c', 'a'])
    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('treats a self-drop as no reorder at all', () => {
    // The precondition the sidebar's marker guard keys on: a drop onto the row itself
    // leaves the order identical, so it states no preference and must not latch intent.
    expect(movePinnedSession(['a', 'b', 'c'], 'b', 'b')).toEqual(['a', 'b', 'c'])
  })

  it('leaves the arrangement alone while the pinned set is merely empty on screen', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
    pinsInFlight = []
    renderSidebar([] as unknown as ChatSlot[])
    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('follows the sort key again once the manual arrangement is reset', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
    pinsInFlight = []
    renderSidebar(PINNED)
    expect(renderedOrder()).toEqual(['a', 'b', 'c'])

    act(() => { forgetPinnedSessionOrderManual() })

    expect(readPinnedSessionOrderIsManual()).toBe(false)
    expect(renderedOrder()).toEqual(['c', 'b', 'a'])
  })

  it('restores the same arrangement when the reset is undone before any reorder', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
    pinsInFlight = []
    renderSidebar(PINNED)
    act(() => { forgetPinnedSessionOrderManual() })
    expect(renderedOrder()).toEqual(['c', 'b', 'a'])

    act(() => { markPinnedSessionOrderManual() })

    expect(readPinnedSessionOrderIsManual()).toBe(true)
    expect(readPinnedSessionOrder()).toEqual(['a', 'b', 'c'])
    expect(renderedOrder()).toEqual(['a', 'b', 'c'])
  })

  it('does not apply rank in memory when the marker write is rejected', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    pinsInFlight = []
    const real = Storage.prototype.setItem
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === PINNED_SESSION_ORDER_MANUAL_KEY) {
        const err = new Error('quota') as Error & { name: string }
        err.name = 'QuotaExceededError'
        throw err
      }
      return real.call(this, key, value)
    })
    renderSidebar(PINNED)
    const topRow = document.querySelector<HTMLElement>('[data-session-row="c"][data-session-scope="list"]')
    if (!topRow) throw new Error('no list-scope row for c')
    topRow.focus()
    fireEvent.keyDown(topRow, { key: 'ArrowDown', altKey: true })
    setItem.mockRestore()

    expect(readPinnedSessionOrderIsManual()).toBe(false)
    expect(renderedOrder()).toEqual(['c', 'b', 'a'])
  })

  it('does not set the manual marker when the order write is rejected', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    const real = Storage.prototype.setItem
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === PINNED_SESSION_ORDER_KEY) {
        const err = new Error('quota') as Error & { name: string }
        err.name = 'QuotaExceededError'
        throw err
      }
      return real.call(this, key, value)
    })
    renderSidebar(PINNED)

    const topRow = document.querySelector<HTMLElement>('[data-session-row="c"][data-session-scope="list"]')
    if (!topRow) throw new Error('no list-scope row for c')
    topRow.focus()
    fireEvent.keyDown(topRow, { key: 'ArrowDown', altKey: true })
    setItem.mockRestore()

    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })

  it('does not displace a pending-unpin row from the manual order', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
    pinsInFlight = ['b']
    const optimistic = PINNED.map(s => s.key === 'b' ? { ...s, pinned: false } : s) as unknown as ChatSlot[]
    renderSidebar(optimistic)

    const row = document.querySelector<HTMLElement>('[data-session-row="a"][data-session-scope="list"]')
    if (!row) throw new Error('no list-scope row for a')
    row.focus()
    fireEvent.keyDown(row, { key: 'ArrowDown', altKey: true })

    expect(readPinnedSessionOrder()).toEqual(['b', 'c', 'a'])
    expect(readPinnedSessionOrder().indexOf('b')).toBeLessThan(readPinnedSessionOrder().indexOf('c'))
  })

  it('keeps manual intent when an optimistic unpin is rolled back', () => {
    // The commit path only runs on an ACCEPTED change, so a rejected unpin never reaches it
    // and the ordering survives -- the window a render effect on the pinned set exposed.
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
    pinsInFlight = ['a', 'b', 'c']
    renderSidebar(PINNED.map(s => ({ ...s, pinned: false })) as unknown as ChatSlot[])
    expect(readPinnedSessionOrderIsManual()).toBe(true)
    expect(readPinnedSessionOrder()).toEqual(['a', 'b', 'c'])
  })
})
