/**
 * The session sidebar's small icon controls carry the `mc-touch-hit*` classes,
 * which grow their tap target to 44px under `(pointer: coarse)` without changing
 * their box (the CSS and its geometry are pinned by touchHitArea.test.ts).
 *
 * The split New button is the case that needs care: its main segment and caret
 * sit side by side behind a 1px divider, so the main segment grows height only
 * (`-y`) and the caret grows away from it (`-end`). The wrapper clips its
 * children with `overflow-hidden` for the rounded corners, and a clipped area is
 * not hit-testable, so on a coarse pointer it lifts the clip and the segments
 * round their own outer corners instead.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
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
import { PREVIEW_DASHBOARD, setPreviewFlag } from '../utils/previewFlags'

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


beforeEach(() => { localStorage.clear(); mobile.value = true })
afterEach(() => vi.clearAllMocks())

describe('sidebar icon controls: 44px touch hit area', () => {
  it('marks the header kebab and both halves of the split New button', () => {
    renderSidebar()
    const main = screen.getByLabelText('New chat session')
    const caret = screen.getByLabelText('More create options')
    const wrapper = main.closest('[data-create-menu]') as HTMLElement
    // The header kebab is the wrapper's preceding control in the same group.
    const kebab = within(wrapper.parentElement as HTMLElement).getByRole('button', { name: 'More options' })
    expect(kebab).toHaveClass('mc-touch-hit')
    expect(main).toHaveClass('mc-touch-hit-y', 'rounded-s-md')
    expect(main).not.toHaveClass('mc-touch-hit')
    expect(caret).toHaveClass('mc-touch-hit-end', 'rounded-e-md')
    expect(caret).not.toHaveClass('mc-touch-hit')
    // Clip kept for a mouse, lifted on touch so the hit areas can reach past it.
    expect(wrapper).toHaveClass('overflow-hidden', '[@media(pointer:coarse)]:overflow-visible')
  })

  it("marks a phone session row's kebab", () => {
    renderSidebar()
    const row = document.querySelector('[data-slot-key="k1"]') as HTMLElement
    expect(row, 'session row not rendered').not.toBeNull()
    expect(within(row).getByRole('button', { name: 'More options' })).toHaveClass('mc-touch-hit')
  })
})

describe('sidebar header menu: All Dashboards behind the Dynamic Dashboard preview', () => {
  const openHeaderMenu = () => act(() => { fireEvent.keyDown(screen.getAllByLabelText('More options')[0], { key: 'Enter' }) })

  it('offers no All Dashboards item while the preview is off, and does once it is on', async () => {
    mobile.value = false
    renderSidebar()
    openHeaderMenu()
    const menu = await screen.findByRole('menu')
    expect(within(menu).queryByRole('menuitem', { name: /All Dashboards/ })).toBeNull()
    // The rest of the menu is untouched: only the preview's door is withheld.
    expect(within(menu).getByRole('menuitem', { name: /board view/ })).toBeTruthy()
    act(() => { fireEvent.keyDown(menu, { key: 'Escape' }) })
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, true) })
    openHeaderMenu()
    const again = await screen.findByRole('menu')
    expect(within(again).getByRole('menuitem', { name: /All Dashboards/ })).toBeTruthy()
  })
})
