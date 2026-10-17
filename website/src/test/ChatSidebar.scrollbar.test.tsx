/**
 * The chat sidebar's session lanes must not paint a vertical scrollbar.
 *
 * Why this is a test and not just a class in the JSX: the lane is permanently
 * scrollable once a user has more than a screenful of sessions, so the 6px
 * ::-webkit-scrollbar track from index.css reads as a fixed stripe down the
 * sidebar rather than a transient hint. It is easy to lose the hiding again
 * while editing that long className, and nothing else would fail.
 *
 * Both halves of the mechanism are asserted, because they cover different
 * engines and only one of them is visible to jsdom:
 *   - `scrollbar-none` (class)      -> ::-webkit-scrollbar{display:none}, WebKit
 *   - inline `scrollbarWidth:none`  -> Firefox + Safari <16
 * The class is checked by NAME rather than by computed style on purpose: jsdom
 * does not load index.css, so a computed-style assertion here would be vacuous.
 * Real rendering is verified separately by scripts/capture-sidebar-scrollbar.mjs.
 *
 * Lanes are located structurally — from a rendered session row up to its scroll
 * parent — so the test cannot be satisfied by a scrollbar-none that landed on
 * some unrelated container.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
// `style` must survive the strip — it carries the assertion under test.
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

const FOLDERS: ChatFolder[] = [{ id: 'f1', name: 'Alpha', order: 0 }]

const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
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

const SLOTS: ChatSlot[] = [
  { key: 'k-alpha', title: 'In Alpha', messages: 1, running: false, folder_id: 'f1', modified: 1000 },
  { key: 'k-root', title: 'Unfoldered', messages: 1, running: false, modified: 2000 },
] as unknown as ChatSlot[]

function renderSidebar() {
  mocks.folders = FOLDERS
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
  qc.setQueryData(['chat-folders'], FOLDERS)
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

/** Walk up from a session row to the lane that owns the vertical scroll. */
function laneFor(row: Element | null): HTMLElement {
  expect(row).toBeTruthy()
  let el = row!.parentElement
  while (el && !el.className.includes('overflow-y-auto')) el = el.parentElement
  expect(el).toBeTruthy()
  return el as HTMLElement
}

function expectScrollbarHidden(lane: HTMLElement) {
  expect(lane.className).toContain('scrollbar-none')
  expect(lane.style.scrollbarWidth).toBe('none')
  // The lane must still SCROLL — hiding the bar is cosmetic, and removing the
  // overflow would also "hide" it while breaking access to the list.
  expect(lane.className).toContain('overflow-y-auto')
}

// Fixtures here carry fixed old timestamps; keep the stale-session collapse
// off so every row stays queryable (its own behavior is pinned in
// ChatSidebar.staleCollapse.test.tsx).
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — vertical scrollbar is hidden', () => {
  it('hides it on the default folder-tree lane', () => {
    const { container } = renderSidebar()
    expectScrollbarHidden(laneFor(container.querySelector('[data-slot-key="k-root"]')))
  })

  it('hides it on the flat view lane', () => {
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('flat-view-toggle'))
    const lane = getByTestId('flat-view-lane')
    expectScrollbarHidden(lane)
    // Confirm this really is the lane holding the rows, not an empty shell.
    expect(lane.querySelectorAll('[data-slot-key]').length).toBeGreaterThan(0)
  })
})
