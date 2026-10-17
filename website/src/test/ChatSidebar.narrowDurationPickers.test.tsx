/**
 * The sessions filter menu's duration pickers at PHONE width.
 *
 * Both pickers (the Recent window, the dormant-collapse threshold) are nested
 * flyouts on a wide viewport. A flyout cannot work on a phone: Radix pins a
 * submenu to `side="right"` — the side is hardcoded in `@radix-ui/react-menu`,
 * not a prop — and its popper only shifts on the cross axis, so it never moves
 * along the axis that is short. Measured at a 390px viewport the Recent flyout
 * came out 249px wide with 192px of it past the screen edge
 * (`--radix-popper-available-width: 57px`), and `flip`'s bestFit fallback can
 * just as well land it off the LEFT edge instead — which is what the bug report
 * showed. Neither side fits beside a menu that already spans most of the screen.
 *
 * So on a phone the options render INLINE inside the one open menu. These tests
 * pin that switch from both directions: the inline controls must be present at
 * phone width, and the row must still be a submenu trigger (with the controls
 * NOT inline) on a wide viewport. Asserting only the mobile half would stay
 * green if the branch were made unconditional.
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

/* `useIsMobile` resolves its media query at MODULE LOAD, so a matchMedia stub
   installed in this file's body would land after the hoisted import. Mocking the
   hook itself is both deterministic and flippable per test. */
const mobile = { value: true }
vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsMobile: () => mobile.value,
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
const RECENT_FILTER_LS_KEY = 'mc-session-recent-only'
const STALE_COLLAPSE_LS_KEY = 'mc-session-stale-collapse-ms'
const DAY_MS = 24 * 60 * 60 * 1000

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

/** Radix's trigger opens on pointerdown for a mouse; keyboard is what jsdom
 *  drives reliably (same approach as ChatSidebar.recencyUnit). */
async function openFilterMenu() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'Sort and filter sessions' }), { key: 'Enter' })
  return screen.findByRole('menuitem', { name: /Recent/ })
}

/** The filter menu is still mounted (its label row survives). */
const filterMenuOpen = () => screen.queryAllByText('Filter').length > 0

beforeEach(() => { localStorage.clear(); mobile.value = true })
afterEach(() => vi.clearAllMocks())

describe('sessions filter menu — duration pickers at phone width', () => {
  it('renders the Recent window picker inline, not behind a submenu trigger', async () => {
    renderSidebar()
    const recentRow = await openFilterMenu()

    // Inline: no flyout to open, so the row must not advertise one.
    expect(recentRow.getAttribute('aria-haspopup')).toBeNull()

    // The whole picker is reachable in the one open menu.
    expect(screen.getByText('Within')).toBeInTheDocument()
    expect(screen.getByLabelText('Custom recency amount')).toBeInTheDocument()
    expect(screen.getByLabelText('Custom recency unit')).toBeInTheDocument()
    const presets = ['1 hour', '6 hours', '1 day', '1 week']
    for (const label of presets) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    }
  })

  it('shows the window picker even while the Recent filter is off', async () => {
    renderSidebar()
    await openFilterMenu()

    // It was gated on the filter being active, because picking a window did not
    // enable it and a visible picker on an inactive filter reported an effect it
    // was not having. Picking now enables it, so the gate's reason is gone — and
    // a gate on one viewport was the per-modality thinking that caused the bug.
    expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBeNull()
    expect(screen.getByText('Within')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '1 week' })).toBeInTheDocument()
  })

  it('commits a preset from the inline chip without dismissing the menu', async () => {
    renderSidebar()
    await openFilterMenu()

    fireEvent.click(screen.getByRole('button', { name: '1 week' }))

    await waitFor(() => expect(localStorage.getItem(RECENT_WINDOW_LS_KEY)).toBe(String(7 * DAY_MS)))
    // A chip is a plain button, not a menu item, so the menu survives the pick
    // and stays available for the next one.
    expect(filterMenuOpen()).toBe(true)
    expect(screen.getByRole('button', { name: '1 week' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('renders the dormant-collapse thresholds inline, and commits one', async () => {
    renderSidebar()
    await openFilterMenu()

    const row = screen.getByTestId('stale-collapse-menu')
    expect(row.getAttribute('aria-haspopup')).toBeNull()
    // A caption, not a menu row: nothing here is tappable (the chips carry the
    // action), so it must not be styled or exposed as an item people can press.
    expect(row.getAttribute('role')).not.toBe('menuitem')
    // "Off" plus the day presets, as chips rather than more menu rows.
    for (const label of ['Off', '1d', '2d', '7d', '14d']) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    }

    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await waitFor(() => expect(localStorage.getItem(STALE_COLLAPSE_LS_KEY)).toBe(String(7 * DAY_MS)))
    expect(filterMenuOpen()).toBe(true)
  })

  it('caps the menu at the viewport width, so an inline caption cannot widen it past the screen', async () => {
    renderSidebar()
    const recentRow = await openFilterMenu()
    const menu = recentRow.closest('[role="menu"]') as HTMLElement
    // Radix sizes the popper wrapper to `max-content`; without this cap the
    // dormant-collapse caption sentence alone took the menu to 510px inside a
    // 390px viewport (measured), 120px of it off-screen.
    expect(menu.className).toContain('max-w-[calc(100vw-1rem)]')
  })

  it('keeps the flyout on a wide viewport — the inline controls are NOT rendered there', async () => {
    mobile.value = false
    renderSidebar()
    const recentRow = await openFilterMenu()

    expect(recentRow).toHaveAttribute('aria-haspopup', 'menu')
    expect(screen.getByTestId('stale-collapse-menu')).toHaveAttribute('aria-haspopup', 'menu')
    // The picker lives in the closed submenu, so nothing of it is in the DOM yet.
    expect(screen.queryByText('Within')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Custom recency amount')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '1 week' })).not.toBeInTheDocument()
  })

  it('turns the Recent filter on when a window is chosen, on BOTH viewports', async () => {
    // Neither leg may enable the filter on its way to the chip, or the final
    // assertion passes without the fix. The mobile leg used to tap the row first
    // (to reveal a gated picker), which wrote the flag before the pick and made
    // that leg true by construction; the picker is no longer gated, so the tap is
    // gone and both legs now discriminate.
    for (const isMobile of [true, false]) {
      localStorage.clear()
      mobile.value = isMobile
      const view = renderSidebar()
      const row = await openFilterMenu()
      expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBeNull()

      if (!isMobile) {
        // ArrowRight is the one key ChatSidebar lets fall through to Radix's
        // submenu-open handler (Enter/Space toggle the filter instead).
        fireEvent.keyDown(row, { key: 'ArrowRight' })
      }
      await screen.findByText('Within')
      // Reaching the picker must not have enabled anything by itself.
      expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBeNull()

      fireEvent.click(screen.getByRole('button', { name: '1 week' }))
      await waitFor(() => expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBe('1'))
      view.unmount()
    }
  })

  it('enables the filter even when the clicked preset is the one already stored', async () => {
    // DEFAULT_RECENT_WINDOW_MS equals the "1 hour" preset, so this is the chip a
    // fresh user is most likely to click — and a value-equality gate on the shared
    // commit seam would swallow exactly this case, keeping the defect alive for
    // the most common pick.
    renderSidebar()
    await openFilterMenu()
    const chip = screen.getByRole('button', { name: '1 hour' })
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBeNull()

    fireEvent.click(chip)

    await waitFor(() => expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBe('1'))
    expect(localStorage.getItem(RECENT_WINDOW_LS_KEY)).toBe(String(60 * 60 * 1000))
  })

  it('does not toggle the filter when a commit re-writes the same window', async () => {
    mobile.value = false
    renderSidebar()
    const row = await openFilterMenu()
    fireEvent.keyDown(row, { key: 'ArrowRight' })
    const amount = await screen.findByLabelText('Custom recency amount')

    // Default is 1 hour and the draft already reads "1", so a bare blur commits
    // the identical window. That must not read as "the user chose recency".
    fireEvent.blur(amount)
    await waitFor(() => expect(localStorage.getItem(RECENT_WINDOW_LS_KEY)).toBe(String(60 * 60 * 1000)))
    expect(localStorage.getItem(RECENT_FILTER_LS_KEY)).toBeNull()
  })
})
