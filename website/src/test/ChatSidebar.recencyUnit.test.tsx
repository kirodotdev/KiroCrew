/**
 * The Recent-filter custom picker's unit dropdown, after the native `<select>`
 * was replaced by `SimpleSelect` (Radix Select).
 *
 * This control is the most nested dropdown in the app: a Radix Select living
 * inside a Radix DropdownMenu SUBmenu, inside a wrapper div that deliberately
 * `stopPropagation()`s click / mousedown / keydown so choosing a window doesn't
 * dismiss the menu. What these tests pin down:
 *
 *  1. Picking an option commits the window WITHOUT tearing down the host menu —
 *     the wrapper's stopPropagation keeps the dismiss off Radix's menu layer.
 *     Radix's Select popup portals to `document.body`, i.e. OUTSIDE that
 *     wrapper, so the popup's own events are unaffected by it.
 *  2. Escape collapses the WHOLE menu tree, which the native `<select>` did not.
 *     See the last test for the measured mechanism — it lives in
 *     `@radix-ui/react-menu`, not in this call site.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    React.forwardRef((props: any, ref: any) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
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

const RECENT_WINDOW_LS_KEY = 'mc-session-recent-window-ms'

function renderSidebar() {
  const slots = [{ key: 'k1', title: 'a session', running: false, messages: 2 }]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    chat: { activeSlot: null, slotStatusDetail: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
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

/** Open the filter menu, then its Recent submenu, and return the unit trigger. */
async function openUnitPicker() {
  // Radix's DropdownMenuTrigger opens on pointerdown for a mouse; the keyboard
  // path is what jsdom drives reliably (same approach as ChatSidebar.createMenu).
  fireEvent.keyDown(screen.getByRole('button', { name: 'Sort and filter sessions' }), { key: 'Enter' })
  const recentRow = await screen.findByRole('menuitem', { name: /Recent/ })
  // ArrowRight is the one key ChatSidebar's own onKeyDown lets fall through to
  // Radix's submenu-open handler (Enter/Space are preventDefaulted to toggle
  // the filter instead).
  fireEvent.keyDown(recentRow, { key: 'ArrowRight' })
  return screen.findByLabelText('Custom recency unit')
}

/** The filter menu is still mounted (its label row survives). */
const filterMenuOpen = () => screen.queryAllByText('Filter').length > 0

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — recency unit picker (SimpleSelect inside a Radix submenu)', () => {
  it('shows the current unit on the theme-drawn trigger', async () => {
    renderSidebar()
    const trigger = await openUnitPicker()
    // Default window is 1 hour, so the draft unit decomposes to "hours".
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger).toHaveTextContent('hours')
  })

  it('commits the window when an option is picked, and keeps the host menu open', async () => {
    renderSidebar()
    const trigger = await openUnitPicker()

    // Radix Select: a `change` event on the trigger does nothing — open it,
    // then click the option.
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'days' }))

    // 1 (amount draft) × days
    await waitFor(() => expect(localStorage.getItem(RECENT_WINDOW_LS_KEY)).toBe(String(24 * 60 * 60 * 1000)))
    // The outer filter menu survived the pick — the wrapper's stopPropagation
    // keeps the dismiss from reaching Radix's menu layer.
    expect(filterMenuOpen()).toBe(true)
    expect(await screen.findByLabelText('Custom recency unit')).toHaveTextContent('days')
  })

  it('offers exactly the three units once opened', async () => {
    renderSidebar()
    fireEvent.click(await openUnitPicker())
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3))
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['min', 'hours', 'days'])
  })

  it('dismisses on a pointer-down outside, taking the menu with it', async () => {
    renderSidebar()
    const trigger = await openUnitPicker()
    fireEvent.click(trigger)
    await screen.findByRole('option', { name: 'days' })

    fireEvent.pointerDown(document.body, { button: 0, ctrlKey: false })

    // Both surfaces go: a click on the page outside every layer is a dismiss of
    // the whole stack, which is the behaviour the native select had too.
    await waitFor(() => expect(screen.queryByRole('option', { name: 'days' })).toBeNull())
    await waitFor(() => expect(filterMenuOpen()).toBe(false))
  })

  it('collapses the whole menu tree on Escape — a Radix submenu behaviour, not ours', async () => {
    renderSidebar()
    const trigger = await openUnitPicker()
    fireEvent.click(trigger)
    const option = await screen.findByRole('option', { name: 'days' })

    fireEvent.keyDown(option, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('option', { name: 'days' })).toBeNull())
    // Documented, not desired: Escape dismisses the filter menu too.
    //
    // Measured with `onEscapeKeyDown` spies on both menu layers: the handler
    // that fires is the SUBMENU's, never the Select's. `@radix-ui/react-menu`
    // hard-wires `MenuSubContent.onEscapeKeyDown -> rootContext.onClose()`, so
    // the whole tree closes imperatively and the Select just loses its host.
    // `SelectContent`'s propagation fix in `ui/select.tsx` is never reached on
    // this keystroke — it guards window-level listeners, not a sibling Radix
    // layer. This is unrelated to the surrounding stopPropagation wrapper: a
    // bare Select in a bare DropdownMenu submenu behaves identically.
    //
    // Pre-migration the native select swallowed Escape in its OS popup, so this
    // is a small regression. Fixing it means intercepting Escape in
    // `ui/select.tsx` or `ui/dropdown-menu.tsx` — both shared, both out of this
    // change's scope. Flip these assertions when that lands.
    expect(filterMenuOpen()).toBe(false)
  })
})
