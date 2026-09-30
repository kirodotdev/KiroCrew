/**
 * Chat sidebar — a status chip's HIDE state.
 *
 * Unread, In progress and Pinned each carry a second control beside their menu
 * row that drops matching sessions instead of keeping only them. The include
 * chips OR together, so a hide is its own filter dimension that ANDs over the
 * result: "Unread + hide In progress" is unread sessions that are NOT running.
 * A chip is either showing-only or hiding, never both, and an active hide always
 * shows as a chip in the strip under the header so no row vanishes silently.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
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

const RUNNING_ONLY = 'mc-session-running-only'
const RUNNING_HIDDEN = 'mc-session-running-hidden'
const UNREAD_ONLY = 'mc-session-unread-only'
const PINNED_HIDDEN = 'mc-session-pinned-hidden'

const SLOTS = [
  { key: 'k-run-unread', title: 'running unread', running: true, messages: 3 },
  { key: 'k-idle-unread', title: 'idle unread', running: false, messages: 3 },
  { key: 'k-idle-read', title: 'idle read', running: false, messages: 3 },
  { key: 'k-run-pinned', title: 'running pinned', running: true, messages: 3, pinned: true },
]
const UNREAD = ['k-run-unread', 'k-idle-unread']

function renderSidebar() {
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: UNREAD, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
      revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as never} activeSlot={null} unreadSlots={UNREAD}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function openFilterMenu() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'Sort and filter sessions' }), { key: 'Enter' })
}

const shown = (title: string) => screen.queryByText(title) !== null

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('status chip hide state', () => {
  it('a stored hide drops running sessions and names itself in the chip strip', async () => {
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(shown('idle unread')).toBe(true)
    expect(shown('running unread')).toBe(false)
    expect(shown('running pinned')).toBe(false)
    expect(screen.getByTestId('filter-chip-hide-running').textContent).toContain('Hiding In progress (2)')
    expect(screen.getByRole('button', { name: 'Stop hiding In progress sessions' })).toBeInTheDocument()
  })

  it('ANDs with the include chips: Unread + hide In progress keeps unread sessions that are not running', async () => {
    localStorage.setItem(UNREAD_ONLY, '1')
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle unread')).toBe(true))
    expect(shown('running unread')).toBe(false)
    expect(shown('idle read')).toBe(false)
    expect(shown('running pinned')).toBe(false)
  })

  it('the menu hide button turns the hide on and off, persisted', async () => {
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    openFilterMenu()
    const hideButton = await screen.findByTestId('filter-hide-running')
    expect(hideButton).toHaveAttribute('aria-label', 'Hide sessions matching In progress')
    fireEvent.click(hideButton)
    await waitFor(() => expect(shown('running unread')).toBe(false))
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('1')
    expect(screen.getByTestId('filter-hide-running')).toHaveAttribute('aria-label', 'Stop hiding In progress sessions')
    fireEvent.click(screen.getByTestId('filter-hide-running'))
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
  })

  it('show-only and hide are exclusive: turning one on turns the other off', async () => {
    localStorage.setItem(RUNNING_ONLY, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    openFilterMenu()
    fireEvent.click(await screen.findByTestId('filter-hide-running'))
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(shown('running unread')).toBe(false)
    expect(localStorage.getItem(RUNNING_ONLY)).toBe('0')
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('1')
    // Clicking the row itself goes back to show-only and drops the hide.
    const row = screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('In progress'))!
    fireEvent.click(row)
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    expect(localStorage.getItem(RUNNING_ONLY)).toBe('1')
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
  })

  it('clearing the hide chip brings the rows back', async () => {
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Stop hiding In progress sessions' }))
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    expect(screen.queryByTestId('filter-chip-hide-running')).toBeNull()
  })

  it('hides pinned sessions the same way', async () => {
    localStorage.setItem(PINNED_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('running pinned')).toBe(false)
  })

  it('the check mark sits in the hide slot and gives way to the lit hide icon', async () => {
    localStorage.setItem(UNREAD_ONLY, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle unread')).toBe(true))
    openFilterMenu()
    const slot = await screen.findByTestId('filter-hide-unread')
    expect(slot.querySelector('.lucide-check')).not.toBeNull()
    const row = screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('Unread'))!
    expect(row.querySelector('.lucide-check')).toBeNull()
    fireEvent.click(slot)
    await waitFor(() => expect(screen.getByTestId('filter-hide-unread')).toHaveAttribute('data-filter-hidden', 'true'))
    expect(screen.getByTestId('filter-hide-unread').querySelector('.lucide-check')).toBeNull()
  })

  it('a stored show-only AND hide for one chip resolves to show-only', async () => {
    localStorage.setItem(RUNNING_ONLY, '1')
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    expect(screen.queryByTestId('filter-chip-hide-running')).toBeNull()
  })

  it('Recent has no hide control', async () => {
    renderSidebar()
    await waitFor(() => expect(shown('idle read')).toBe(true))
    openFilterMenu()
    await screen.findByTestId('filter-hide-running')
    expect(screen.queryByTestId('filter-hide-recent')).toBeNull()
  })
})
