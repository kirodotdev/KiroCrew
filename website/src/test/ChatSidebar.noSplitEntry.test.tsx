/**
 * Sidebar carries NO Split View entry.
 *
 * Split View (the session grid) used to pin a full-width "Split View" button
 * between the New row and the search box, gated on a `splitEnabled` prop. It
 * cost a row of session list on every screen for an opt-in secondary layout,
 * and it was the third copy of a control the chat header already owns: the
 * header's Columns2 button (⌘D) enters the grid, and the header's "in split"
 * badge is the way back into a live split.
 *
 * Locks the contract:
 *  (1) No split-view control renders in the sidebar, feature on or off.
 *  (2) The prop surface is gone — a caller that still passes the old
 *      `splitEnabled` / `splitActive` / `onOpenSplit` props gets no button and
 *      no click target, so a revival has to be a deliberate re-add here.
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
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
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
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
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

const SLOTS = [
  { key: 'k-a', title: 'Alpha session', messages: 1, running: false, modified: 2000 },
]

function renderSidebar(extraProps: Record<string, unknown> = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as any} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              {...(extraProps as any)}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — no Split View entry', () => {
  it('renders no split-view control', () => {
    const { queryByText, container } = renderSidebar()
    expect(queryByText(/split view/i)).toBeNull()
    const labelled = Array.from(container.querySelectorAll('[aria-label], [title]'))
      .filter(el => /split/i.test(`${el.getAttribute('aria-label') ?? ''} ${el.getAttribute('title') ?? ''}`))
    expect(labelled).toEqual([])
  })

  it('ignores the removed splitEnabled / splitActive / onOpenSplit props', () => {
    const onOpenSplit = vi.fn()
    const { queryByText, container } = renderSidebar({ splitEnabled: true, splitActive: true, onOpenSplit })
    expect(queryByText(/split view/i)).toBeNull()
    // aria-pressed was the pinned entry's toggle marker — nothing in the
    // sidebar header claims that state now.
    expect(container.querySelectorAll('[aria-pressed="true"]').length).toBe(0)
    expect(onOpenSplit).not.toHaveBeenCalled()
  })
})
