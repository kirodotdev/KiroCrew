/**
 * Chat sidebar — WHEN the Crew Mode filter is offered.
 *
 * The filter menu gains a Crew Mode row and a one-line hint once a chat runs on
 * the conductor (a row the server stamps `crew_mode_root`). With no such chat —
 * a list with none yet, or a gateway that predates Crew Mode and stamps nothing —
 * the menu reads exactly as it did before: no row, no hint. A stored Crew Mode
 * filter keeps its row while it is on, so it can always be turned off.
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
import { crewModePreviewOn } from './crewModePreview'
import { PREVIEW_CREW_MODE } from '../utils/previewFlags'

const CREW_ONLY = 'mc-session-crew-only'

/** Rows as a gateway without Crew Mode sends them: no `crew_mode_root`, no `crew_root`. */
const PLAIN_SLOTS = [
  { key: 'k-a', title: 'plain a', running: true, messages: 3 },
  { key: 'k-b', title: 'plain b', running: false, messages: 3 },
]
const WITH_ROOT = [
  ...PLAIN_SLOTS,
  { key: 'k-root', title: 'the crew chat', running: true, messages: 3, crew_mode_root: true },
]

function renderSidebar(slots: object[]) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
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
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

async function openFilterMenu() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'Sort and filter sessions' }), { key: 'Enter' })
  await screen.findByRole('menu')
}

const crewRow = () => screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('Crew Mode'))

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

const WITH_CREW_MEMBER = [
  ...WITH_ROOT,
  { key: 'k-worker', title: 'the crew member', running: true, messages: 3, crew_root: 'k-root' },
]

describe('a Crew Mode root\'s crew members', () => {
  it('stay out of the list and out of the Broadcast dialog', async () => {
    renderSidebar(WITH_CREW_MEMBER)
    await waitFor(() => expect(screen.queryByText('the crew chat')).not.toBeNull())
    // The root is listed, the row the root dispatched is not.
    expect(screen.queryByText('the crew member')).toBeNull()
    expect(screen.queryByText('plain a')).not.toBeNull()
    // The Broadcast dialog reads the same rows: the crew member is no target.
    // The list menu's trigger (the rows have one of their own each).
    const listMenu = screen.getAllByRole('button', { name: 'More options' }).find(el => el.title === 'More options')
    fireEvent.keyDown(listMenu!, { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('broadcast-menu-item'))
    const panel = await screen.findByTestId('broadcast-panel')
    await waitFor(() => expect(panel.textContent).toContain('the crew chat'))
    expect(panel.textContent).not.toContain('the crew member')
  })
})

describe('the Crew Mode filter offer', () => {
  it('is absent, hint included, while no chat runs on the conductor', async () => {
    renderSidebar(PLAIN_SLOTS)
    await waitFor(() => expect(screen.queryByText('plain a')).not.toBeNull())
    await openFilterMenu()
    // The menu is the usual one: its other rows are there.
    expect(screen.getAllByRole('menuitem').some(el => el.textContent?.startsWith('In progress'))).toBe(true)
    expect(crewRow()).toBeUndefined()
    expect(screen.queryByTestId('filter-crew-hint')).toBeNull()
    expect(screen.queryByTestId('filter-chip-crew')).toBeNull()
  })

  it('appears with its hint once a Crew Mode chat is in the list', async () => {
    renderSidebar(WITH_ROOT)
    await waitFor(() => expect(screen.queryByText('the crew chat')).not.toBeNull())
    await openFilterMenu()
    const row = crewRow()
    expect(row).toBeDefined()
    expect(row!.textContent).toContain('(1)')
    expect(screen.getByTestId('filter-crew-hint')).toBeInTheDocument()
    // Turning it on narrows the list to the Crew Mode chat.
    fireEvent.click(row!)
    await waitFor(() => expect(screen.queryByText('plain a')).toBeNull())
    expect(screen.queryByText('the crew chat')).not.toBeNull()
    expect(localStorage.getItem(CREW_ONLY)).toBe('1')
  })

  it('draws its chip in a row of its own, never in the four status chips\' row', async () => {
    localStorage.setItem(CREW_ONLY, '1')
    localStorage.setItem('mc-session-running-only', '1')
    renderSidebar(WITH_ROOT)
    const crewChip = await screen.findByTestId('filter-chip-crew')
    const runningChip = await screen.findByTestId('filter-chip-running')
    // Two visual groups: the status row keeps its base-branch button count
    // (AUTOSDE max-two-buttons-per-row), the Crew Mode chip is not one of its children.
    expect(crewChip.parentElement).not.toBe(runningChip.parentElement)
    expect(runningChip.parentElement!.querySelectorAll('button')).toHaveLength(1)
    expect(crewChip.parentElement!.querySelectorAll('button')).toHaveLength(1)
    expect(crewChip.className).toContain('crew-grad')
  })

  it('keeps the row while the filter is stored on, so it can be turned off with no Crew Mode chat left', async () => {
    localStorage.setItem(CREW_ONLY, '1')
    renderSidebar(PLAIN_SLOTS)
    // The filter chip is drawn for the active filter, as for any other.
    await screen.findByTestId('filter-chip-crew')
    await openFilterMenu()
    const row = crewRow()
    expect(row).toBeDefined()
    fireEvent.click(row!)
    await waitFor(() => expect(localStorage.getItem(CREW_ONLY)).toBe('0'))
    await waitFor(() => expect(screen.queryByText('plain a')).not.toBeNull())
  })
})

describe('with the Crew Mode preview off', () => {
  // The file turns the preview on for every test; these turn it off again.
  beforeEach(() => localStorage.removeItem(PREVIEW_CREW_MODE))

  it('lists a crew member as an ordinary row and draws no crew card', async () => {
    renderSidebar(WITH_CREW_MEMBER)
    await waitFor(() => expect(screen.queryByText('the crew member')).not.toBeNull())
    expect(screen.queryByText('the crew chat')).not.toBeNull()
    expect(screen.queryByTestId('crew-eyebrow')).toBeNull()
    expect(screen.queryByTestId('crew-ghost-badge')).toBeNull()
  })

  it('offers no Crew Mode filter, and a stored one narrows nothing', async () => {
    localStorage.setItem(CREW_ONLY, '1')
    renderSidebar(WITH_ROOT)
    await waitFor(() => expect(screen.queryByText('plain a')).not.toBeNull())
    expect(screen.queryByTestId('filter-chip-crew')).toBeNull()
    await openFilterMenu()
    expect(crewRow()).toBeUndefined()
    expect(screen.queryByTestId('filter-crew-hint')).toBeNull()
    // The stored value is kept, so turning the preview on brings the filter back.
    expect(localStorage.getItem(CREW_ONLY)).toBe('1')
  })

  it('a stored paused Crew Mode filter holds no pause: another filter narrows, the stored value stays', async () => {
    localStorage.setItem(CREW_ONLY, '2')
    renderSidebar(WITH_ROOT)
    await waitFor(() => expect(screen.queryByText('plain b')).not.toBeNull())
    await openFilterMenu()
    fireEvent.click(screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith('In progress'))!)
    // Not joined to a pause: stored on, and the idle row drops out of the list.
    await waitFor(() => expect(localStorage.getItem('mc-session-running-only')).toBe('1'))
    await waitFor(() => expect(screen.queryByText('plain b')).toBeNull())
    expect(screen.queryByText('plain a')).not.toBeNull()
    expect(localStorage.getItem(CREW_ONLY)).toBe('2')
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
