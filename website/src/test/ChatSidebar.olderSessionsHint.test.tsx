/**
 * The sessions sidebar carries an in-flow "Show all older sessions" row after
 * the LAST session of a lane (list-view root lane and flat view). It triggers
 * the same action as the persistent Older Sessions footer but serves a
 * different purpose: a user who scans the list to its end without finding a
 * chat is exactly the user whose session was evicted into Older Sessions, and
 * this row is the only cue in the reading flow that tells them where it went.
 *
 * Pins: the row is the lane's last element (after the dormant expander), it
 * opens the pane and fetches history on click, and it is hidden while the
 * pane is open — the pane is the continuation then. The footer itself is
 * pinned in ChatSidebarCoverage.test.tsx.
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
    React.forwardRef((props: Record<string, unknown> & { children?: unknown }, ref: unknown) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as never)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
    LayoutGroup: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

// `sessions` is what fetchHistory calls; asserting on it proves the row does
// the footer's work rather than merely flipping local state.
const apiMocks = vi.hoisted(() => ({
  chatFolders: vi.fn().mockResolvedValue([]),
  sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in apiMocks) return apiMocks[prop as keyof typeof apiMocks]
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

const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString()

const slot = (key: string, title: string, ageHours: number) => ({
  key, title, running: false, messages: 2,
  created: hoursAgo(ageHours + 1), last_turn_ts: hoursAgo(ageHours),
})

const FRESH = [slot('a', 'alpha chat', 1), slot('b', 'beta chat', 2)]
// Older than every dormant-collapse preset, so it folds behind the expander
// whenever the collapse is on (the default).
const DORMANT = slot('old', 'old chat', 30 * 24)

function renderSidebar(slots: Record<string, unknown>[], folders: Record<string, unknown>[] = []) {
  // The folders query refetches on mount, so the mock must serve the same
  // folders the cache is seeded with or the refetch wipes them.
  apiMocks.chatFolders.mockResolvedValue(folders)
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
      revealRequest: null, revealNonce: 0,
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The footer is the pane's persistent toggle; its aria-label is stable. */
const footer = () => screen.getByLabelText('Older sessions')

beforeEach(() => {
  localStorage.clear()
  apiMocks.sessions.mockClear()
})
afterEach(() => vi.clearAllMocks())

describe('sidebar in-flow "Show all older sessions" row', () => {
  it('follows the last row of the list-view root lane, after the dormant expander', () => {
    renderSidebar([...FRESH, DORMANT])
    const hint = screen.getByTestId('older-sessions-hint-root')
    expect(hint).toHaveTextContent('Show all older sessions')
    // Last element of its bucket: nothing renders below it, and the dormant
    // expander (present, since `old chat` is folded) sits above it.
    const expander = screen.getByTestId('stale-expander-root')
    expect(expander.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(hint.parentElement?.lastElementChild).toBe(hint)
    // A single row, not one per session.
    expect(screen.getAllByText('Show all older sessions')).toHaveLength(1)
  })

  it('opens the Older Sessions pane and fetches history on click, then hides itself', async () => {
    renderSidebar(FRESH)
    expect(footer).not.toThrow()
    expect(footer().getAttribute('aria-expanded')).toBe('false')
    expect(apiMocks.sessions).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('older-sessions-hint-root'))

    expect(footer().getAttribute('aria-expanded')).toBe('true')
    await waitFor(() => expect(apiMocks.sessions).toHaveBeenCalled())
    // The pane is now the continuation of the list; the cue would be noise.
    expect(screen.queryByTestId('older-sessions-hint-root')).toBeNull()
  })

  it('is hidden while the pane is open via the footer, and returns when it closes', () => {
    renderSidebar(FRESH)
    fireEvent.click(footer())
    expect(screen.queryByTestId('older-sessions-hint-root')).toBeNull()
    fireEvent.click(footer())
    expect(screen.getByTestId('older-sessions-hint-root')).toBeInTheDocument()
  })

  it('also closes the flat-view lane, after the hidden-folders reveal row', async () => {
    // Flat view only takes over when there is a folder to flatten.
    localStorage.setItem('mc-sidebar-flat-view', '1')
    renderSidebar(FRESH, [{ id: 'f1', name: 'Alpha', order: 0 }])
    const hint = await screen.findByTestId('older-sessions-hint-flat')
    // Judge placement from the row's own parent: the folders refetch can
    // remount the lane, so a lane node grabbed earlier may be stale.
    expect(hint.parentElement).toHaveAttribute('data-testid', 'flat-view-lane')
    expect(hint.parentElement?.lastElementChild).toBe(hint)
    expect(screen.queryByTestId('older-sessions-hint-root')).toBeNull()

    fireEvent.click(screen.getByTestId('older-sessions-hint-flat'))
    await waitFor(() => expect(footer().getAttribute('aria-expanded')).toBe('true'))
    expect(screen.queryByTestId('older-sessions-hint-flat')).toBeNull()
  })
})
