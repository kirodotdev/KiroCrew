/**
 * The connector line down an open folder's left edge is a collapse target.
 *
 * `FolderRail` lays an out-of-flow hit strip over the body's `border-l`, so a
 * click on the thin line folds the folder it groups. What this file pins:
 *
 *   - an open folder's body carries the rail, and clicking it writes
 *     `collapsed: true` for THAT folder through the same mutation the header
 *     toggle uses;
 *   - the rail takes no tab stop and is aria-hidden: the header button stays
 *     the one keyboard and screen-reader control, so the tab order and the
 *     accessibility tree are unchanged;
 *   - the rail carries the out-of-flow `.folder-rail` rule inside a `relative`
 *     body, which is what keeps the sidebar's alignment guides where they were.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, waitFor } from '@testing-library/react'
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
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<HTMLElement>) => {
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
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const FOLDER = 'f-autofix'
// Folders must come back from the api mock, not only the seeded query cache:
// the seeded entry is stale on mount so react-query refetches it.
const fixtures: { chatFolders: unknown[] } = { chatFolders: [] }
const updateChatFolder = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'updateChatFolder') return updateChatFolder
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
import { FolderRail } from '../components/FolderRail'

const SLOTS = [
  { key: 'in-folder', title: 'autofix session', running: false, messages: 4, folder_id: FOLDER },
  { key: 'ungrouped', title: 'Cron: gh-issue-triage', running: false, messages: 4 },
]

/** A second, open folder with no sessions filed in it. */
const EMPTY_FOLDER = 'f-empty'

async function renderSidebar(collapsed: boolean) {
  fixtures.chatFolders = [
    { id: FOLDER, name: 'kirocrew-github-autofix', order: 0, collapsed },
    { id: EMPTY_FOLDER, name: 'empty folder', order: 1, collapsed: false },
  ]
  // Cast through the factory's own parameter type rather than `any`: the
  // preloaded slices are partial on purpose (this suite only needs the sidebar's
  // inputs), and naming the type keeps the cast checked against the real shape.
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint',
      sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: { activeSlot: null, slotStatusDetail: {} },
  } as Parameters<typeof createTestStore>[0])
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as React.ComponentProps<typeof ChatSidebar>['slots']}
              activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  await utils.findByText('kirocrew-github-autofix')
  return utils
}


afterEach(() => { cleanup(); updateChatFolder.mockClear() })

describe('folder rail collapses its folder', () => {
  it('renders a rail on an open folder body and collapses that folder on click', async () => {
    const { getByTestId, getAllByRole } = await renderSidebar(false)
    const rail = getByTestId(`folder-rail-${FOLDER}`)
    expect(rail.getAttribute('aria-hidden')).toBe('true')
    expect(rail.getAttribute('aria-label')).toBe('Collapse folder kirocrew-github-autofix')
    expect(rail.getAttribute('title')).toBe('Collapse folder kirocrew-github-autofix')
    // The rail is hidden from assistive technology, so the header toggle is the
    // only collapse control a screen reader sees.
    expect(getAllByRole('button', { name: 'Collapse folder kirocrew-github-autofix' })).toHaveLength(1)
    fireEvent.click(rail)
    await waitFor(() => expect(updateChatFolder).toHaveBeenCalledWith(FOLDER, { collapsed: true }))
  })

  it('sits out of flow inside a positioned body, with no tab stop', async () => {
    const { getByTestId } = await renderSidebar(false)
    const rail = getByTestId(`folder-rail-${FOLDER}`)
    expect(rail.tabIndex).toBe(-1)
    // Positioned by the `.folder-rail` rule in index.css (absolute, out of flow).
    expect(rail.className).toBe('folder-rail')
    expect((rail.parentElement as HTMLElement).className.split(/\s+/)).toContain('relative')
  })

  it('draws no rail beside an empty folder\'s lone new-chat row', async () => {
    // The empty body is one row directly under its own header, so a second
    // collapse target there adds surface without shortening any reach.
    const { getByTestId, queryByTestId } = await renderSidebar(false)
    expect(getByTestId(`folder-empty-new-chat-${EMPTY_FOLDER}`)).toBeTruthy()
    expect(queryByTestId(`folder-rail-${EMPTY_FOLDER}`)).toBeNull()
  })
})

describe('FolderRail', () => {
  it('toggles without letting the click reach the enclosing folder', () => {
    const onToggle = vi.fn()
    const outer = vi.fn()
    // A native listener on document.body stands in for the enclosing folder. It
    // sits above React's root container, so it sees a click only if the rail let
    // it bubble past the root.
    // The rail is aria-hidden, so it is found by test id rather than by role.
    const { getByTestId } = render(<FolderRail name="X" id="x" onToggle={onToggle} />)
    document.body.addEventListener('click', outer)
    fireEvent.click(getByTestId('folder-rail-x'))
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()
    document.body.removeEventListener('click', outer)
  })
})
