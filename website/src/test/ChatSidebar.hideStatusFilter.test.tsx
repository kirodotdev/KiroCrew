/**
 * Chat sidebar — a status chip's HIDE state.
 *
 * Unread, In progress and Pinned each carry a second control beside their menu
 * row that drops matching sessions instead of keeping only them. The include
 * chips OR together, so a hide is its own filter dimension that ANDs over the
 * result: "Unread + hide In progress" is unread sessions that are NOT running.
 * A chip is either showing-only or hiding, never both — the hide control is
 * offered only while show-only is off — and the active hides always show as one
 * aggregate chip under the header so no row vanishes silently.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { requestSlotReveal } from '../store/chatSlice'

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
const UNREAD_HIDDEN = 'mc-session-unread-hidden'
const PINNED_HIDDEN = 'mc-session-pinned-hidden'

const SLOTS = [
  { key: 'k-run-unread', title: 'running unread', running: true, messages: 3 },
  { key: 'k-idle-unread', title: 'idle unread', running: false, messages: 3 },
  { key: 'k-idle-read', title: 'idle read', running: false, messages: 3 },
  { key: 'k-run-pinned', title: 'running pinned', running: true, messages: 3, pinned: true },
]
const UNREAD = ['k-run-unread', 'k-idle-unread']

function renderSidebar(slots: object[] = SLOTS) {
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
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
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={UNREAD}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
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
    expect(screen.getByTestId('hidden-filter-chip').textContent).toContain('Hiding In progress (2)')
    expect(screen.getByRole('button', { name: 'Stop hiding In progress sessions' })).toBeInTheDocument()
  })

  it('several hides share ONE aggregate chip in its own row, never a chip each in the filter-chip row', async () => {
    localStorage.setItem(RUNNING_HIDDEN, '1')
    localStorage.setItem(PINNED_HIDDEN, '1')
    localStorage.setItem(UNREAD_ONLY, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle unread')).toBe(true))
    expect(shown('running unread')).toBe(false)
    expect(shown('running pinned')).toBe(false)
    const chip = screen.getByTestId('hidden-filter-chip')
    expect(chip.textContent).toContain('Hiding In progress (2), Pinned (1)')
    expect(chip).toHaveAttribute('aria-label', 'Stop hiding In progress and Pinned sessions')
    // The include chips' row keeps its base-branch button count
    // (AUTOSDE max-two-buttons-per-row): the hide chip is not one of its children.
    const includeChip = screen.getByRole('button', { name: 'Clear Unread filter' })
    expect(includeChip.parentElement).not.toBe(chip.parentElement)
    expect(includeChip.parentElement!.querySelectorAll('button')).toHaveLength(1)
    // One click drops every hide and leaves the include chip alone.
    fireEvent.click(chip)
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    expect(localStorage.getItem(PINNED_HIDDEN)).toBe('0')
    expect(localStorage.getItem(UNREAD_ONLY)).toBe('1')
    expect(screen.queryByTestId('hidden-filter-chip')).toBeNull()
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

  it('"Pause all filters" lifts the show-only chips but leaves a hide hiding, like the folder and tag filters', async () => {
    localStorage.setItem(UNREAD_ONLY, '1')
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle unread')).toBe(true))
    expect(shown('idle read')).toBe(false)

    openFilterMenu()
    fireEvent.click(await screen.findByTestId('filter-pause-all'))

    // Unread is lifted, so the read session comes back...
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(localStorage.getItem(UNREAD_ONLY)).toBe('2')
    // ...but running sessions stay hidden, and the hide chip stays.
    expect(shown('running unread')).toBe(false)
    expect(shown('running pinned')).toBe(false)
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('1')
    expect(screen.getByTestId('hidden-filter-chip').textContent).toContain('Hiding In progress (2)')
  })

  it('a hide alone offers no pause row: the pause only lifts show-only chips', async () => {
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle read')).toBe(true))
    openFilterMenu()
    await screen.findByRole('menu')
    expect(screen.queryByTestId('filter-pause-all')).toBeNull()
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

  it('show-only and hide are exclusive: turning show-only on drops the hide, and the hide is only offered while show-only is off', async () => {
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(shown('running unread')).toBe(false)
    openFilterMenu()
    await screen.findByTestId('filter-hide-running')
    // Clicking the row goes to show-only and drops the hide.
    const row = screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('In progress'))!
    fireEvent.click(row)
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    expect(localStorage.getItem(RUNNING_ONLY)).toBe('1')
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    // From show-only there is no one-click path to a hide: the row holds its
    // check and the control is gone, so the next click only turns show-only off.
    expect(screen.queryByTestId('filter-hide-running')).toBeNull()
    fireEvent.click(screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('In progress'))!)
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(shown('running unread')).toBe(true)
    expect(localStorage.getItem(RUNNING_ONLY)).toBe('0')
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    // Now the control is back, and hiding keeps show-only off.
    fireEvent.click(await screen.findByTestId('filter-hide-running'))
    await waitFor(() => expect(shown('running unread')).toBe(false))
    expect(shown('idle read')).toBe(true)
    expect(localStorage.getItem(RUNNING_ONLY)).toBe('0')
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('1')
  })

  it('clearing the hide chip brings the rows back', async () => {
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Stop hiding In progress sessions' }))
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    expect(screen.queryByTestId('hidden-filter-chip')).toBeNull()
  })

  it('hides pinned sessions the same way', async () => {
    localStorage.setItem(PINNED_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('running pinned')).toBe(false)
  })

  it('while show-only is on the row carries its check and offers no hide control: a click on it only turns show-only off', async () => {
    localStorage.setItem(UNREAD_ONLY, '1')
    renderSidebar()
    await waitFor(() => expect(shown('idle unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    openFilterMenu()
    // Other hideable rows do offer the control, so the menu is open and the
    // omission below is this row's, not a missing menu.
    await screen.findByTestId('filter-hide-running')
    expect(screen.queryByTestId('filter-hide-unread')).toBeNull()
    const row = screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('Unread'))!
    const check = row.querySelector('.lucide-check')
    expect(check).not.toBeNull()
    // The check is part of the row: clicking it turns show-only off and hides nothing.
    fireEvent.click(check!)
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(shown('idle unread')).toBe(true)
    expect(localStorage.getItem(UNREAD_ONLY)).toBe('0')
    expect(localStorage.getItem(UNREAD_HIDDEN)).not.toBe('1')
    expect(screen.queryByTestId('hidden-filter-chip')).toBeNull()
    // With show-only off the hide control is back, and lights up when used.
    const slot = await screen.findByTestId('filter-hide-unread')
    expect(slot.querySelector('.lucide-check')).toBeNull()
    fireEvent.click(slot)
    await waitFor(() => expect(screen.getByTestId('filter-hide-unread')).toHaveAttribute('data-filter-hidden', 'true'))
    expect(shown('idle unread')).toBe(false)
  })

  it('a stored show-only AND hide for one chip resolves to show-only', async () => {
    localStorage.setItem(RUNNING_ONLY, '1')
    localStorage.setItem(RUNNING_HIDDEN, '1')
    renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(shown('idle read')).toBe(false)
    expect(screen.queryByTestId('hidden-filter-chip')).toBeNull()
  })

  it('the losing hide of a stored show-only AND hide pair is written back, so it cannot resurface on the next mount', async () => {
    localStorage.setItem(RUNNING_ONLY, '1')
    localStorage.setItem(RUNNING_HIDDEN, '1')
    const first = renderSidebar()
    await waitFor(() => expect(shown('running unread')).toBe(true))
    expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    // Turn show-only off: nothing is hidden, so the hide's clear has nothing
    // to persist — the mount above is what had to drop the stale flag.
    openFilterMenu()
    const row = screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('In progress'))!
    fireEvent.click(row)
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(localStorage.getItem(RUNNING_ONLY)).toBe('0')
    first.unmount()
    renderSidebar()
    await waitFor(() => expect(shown('idle read')).toBe(true))
    expect(shown('running unread')).toBe(true)
    expect(screen.queryByTestId('hidden-filter-chip')).toBeNull()
  })

  it('Recent has no hide control', async () => {
    renderSidebar()
    await waitFor(() => expect(shown('idle read')).toBe(true))
    openFilterMenu()
    await screen.findByTestId('filter-hide-running')
    expect(screen.queryByTestId('filter-hide-recent')).toBeNull()
  })

  // A reveal clears only the filters actually hiding its target. scrollIntoView
  // is stubbed because the reveal scrolls the row into view and jsdom has none.
  describe('reveal', () => {
    const original = Element.prototype.scrollIntoView
    beforeEach(() => { Element.prototype.scrollIntoView = vi.fn() })
    afterEach(() => { Element.prototype.scrollIntoView = original })

    it('a row only search dropped clears the search and keeps every persisted hide', async () => {
      localStorage.setItem(RUNNING_HIDDEN, '1')
      const utils = renderSidebar()
      fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: 'idle read' } })
      await waitFor(() => expect(shown('idle unread')).toBe(false))
      utils.store.dispatch(requestSlotReveal('k-idle-unread'))
      await waitFor(() => expect(shown('idle unread')).toBe(true))
      expect(screen.getByPlaceholderText(/search/i)).toHaveValue('')
      expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('1')
      expect(shown('running unread')).toBe(false)
      expect(screen.getByTestId('hidden-filter-chip').textContent).toContain('Hiding In progress (2)')
    })

    it('a row a hide matches clears that hide', async () => {
      localStorage.setItem(RUNNING_HIDDEN, '1')
      const utils = renderSidebar()
      await waitFor(() => expect(shown('running unread')).toBe(false))
      utils.store.dispatch(requestSlotReveal('k-run-unread'))
      await waitFor(() => expect(shown('running unread')).toBe(true))
      expect(localStorage.getItem(RUNNING_HIDDEN)).toBe('0')
    })
  })

  // The conductor lane nests a child under the session that started it, and puts a
  // parent the filters dropped back on screen as a dimmed anchor when a child under it
  // matches. A hide must not come back that way.
  describe('conductor lane', () => {
    const CREW = [
      { key: 'k-run-conductor', title: 'running conductor', running: true, messages: 3 },
      { key: 'k-idle-child', title: 'idle child', running: false, messages: 3, parent: { slot: 'k-run-conductor', key: 'k-run-conductor' } },
    ]
    beforeEach(() => {
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      // Settled rows and closed crews would fold away for reasons unrelated to the hide.
      localStorage.setItem('mc-session-stale-collapse-ms', '0')
      localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-run-conductor']))
    })

    it('a hidden running conductor does not come back as an anchor over its idle child', async () => {
      localStorage.setItem(RUNNING_HIDDEN, '1')
      renderSidebar(CREW)
      await waitFor(() => expect(shown('idle child')).toBe(true))
      expect(shown('running conductor')).toBe(false)
    })

    it('shows the conductor over its child when nothing is hidden', async () => {
      // Control: proves this lane renders the parent at all, so its absence above is
      // the hide doing work.
      renderSidebar(CREW)
      await waitFor(() => expect(shown('idle child')).toBe(true))
      expect(shown('running conductor')).toBe(true)
    })
  })
})
