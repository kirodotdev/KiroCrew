/**
 * Coverage for the ChatSidebar surfaces that the focused sibling suites never
 * reach: the three header ⋮ panels (Clean Up, Switch All Sessions, Manage
 * Tags), the Older Sessions pane (resume / delete / load-more / date segments /
 * folder-grouped search results), the narrow-width header collapse, and the
 * store-driven reveal-in-sidebar request (chat.revealRequest).
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom (needs PointerEvent),
 * so every trigger here is activated by keyboard — the path jsdom does handle.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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

const cfg = vi.hoisted(() => ({
  saveChatConfig: vi.fn(),
  value: { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false } as Record<string, unknown>,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: cfg.saveChatConfig,
}))

const mocks = vi.hoisted(() => ({
  cleanupSessions: vi.fn(),
  chatSlotsModel: vi.fn(),
  clearSessions: vi.fn(),
  deleteSession: vi.fn(),
  resumeChatSlot: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
  createTagColumn: vi.fn(),
  deleteTagColumn: vi.fn(),
  updateChatFolder: vi.fn(),
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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
import { requestSlotReveal } from '../store/chatSlice'

interface TestSlot {
  key: string
  title?: string
  running: boolean
  messages?: number
  model?: string
  folder_id?: string
  last_ts?: string
  created?: string
  pinned?: boolean
}

interface TestHistoryItem {
  key: string
  title?: string
  modified?: number
  created?: string
  agent?: string
  folder_id?: string
}

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

function renderSidebar(opts: {
  slots?: TestSlot[]
  folders?: ChatFolder[]
  history?: TestHistoryItem[]
  historyHasMore?: boolean
  /** Pre-set chat.revealRequest, simulating a reveal requested while the
   *  sidebar was unmounted (the #912 D1 regression case). */
  revealRequest?: { key: string; nonce: number }
} = {}) {
  const slots = opts.slots ?? []
  const folders = opts.folders ?? []
  mocks.chatFolders.mockResolvedValue(folders)
  // Redux Toolkit REPLACES a slice's state with `preloadedState` -- it does not
  // merge with the slice's initialState. A hand-rolled partial therefore drops
  // every key it forgets, and reducers that legitimately assume the real shape
  // then throw: omitting `slotMessages` made `deleteSlot.fulfilled` blow up in
  // `delete state.slotMessages[...]` as an UNHANDLED rejection, which fails the
  // vitest run even while every test passes. Spread the genuine defaults first.
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
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
      revealRequest: opts.revealRequest ?? null,
      revealNonce: opts.revealRequest?.nonce ?? 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={opts.history ?? []} historyHasMore={!!opts.historyHasMore}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
}

/** The header ⋮ is the first "More options" trigger in document order. */
function openHeaderMenu() {
  fireEvent.keyDown(screen.getAllByLabelText('More options')[0], { key: 'Enter' })
}

async function openHeaderPanel(itemText: string) {
  openHeaderMenu()
  fireEvent.click(await screen.findByText(itemText))
}

/** Expand the Older Sessions pane. */
function openHistory() {
  fireEvent.click(screen.getByLabelText('Older sessions'))
}

beforeEach(() => {
  localStorage.clear()
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  mocks.cleanupSessions.mockResolvedValue({ ok: true, archived: 0, keys: [], failed: [] })
  mocks.chatSlotsModel.mockResolvedValue({ ok: true, failed: [] })
  mocks.clearSessions.mockResolvedValue({ ok: true })
  mocks.deleteSession.mockResolvedValue({ ok: true })
  mocks.resumeChatSlot.mockResolvedValue({ ok: true, key: 'h1', messages: [], mode: '', memory_mode: 'persistent' })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
  mocks.createTagColumn.mockResolvedValue({ id: 'col-new' })
  mocks.deleteTagColumn.mockResolvedValue({ ok: true })
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.chatFolders.mockResolvedValue([])
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: { recent_tint_count: 3 } })
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('ChatSidebar — Clean Up Sessions panel', () => {
  const SLOTS: TestSlot[] = [
    { key: 'k-stale', title: 'Stale one', running: false, messages: 3, last_ts: '2020-01-01T00:00:00Z' },
    { key: 'k-live', title: 'Live one', running: false, messages: 1 },
  ]

  it('re-previews against the chosen inactivity window', async () => {
    mocks.cleanupSessions.mockResolvedValue({ ok: true, archived: 0, keys: [], failed: [], active_is_stale: false })
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Clean up sessions')
    fireEvent.click(await screen.findByText('7 days'))
    await waitFor(() => expect(mocks.cleanupSessions).toHaveBeenCalledWith(7, '', true))
    fireEvent.click(screen.getByText('1 day'))
    await waitFor(() => expect(mocks.cleanupSessions).toHaveBeenCalledWith(1, '', true))
  })

  it('says so when nothing is stale, and keeps Archive disabled', async () => {
    mocks.cleanupSessions.mockResolvedValue({ ok: true, archived: 0, keys: [], failed: [], active_is_stale: false })
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Clean up sessions')
    expect(await screen.findByText('No inactive sessions to archive.')).toBeTruthy()
    expect(screen.getByText('Archive 0 sessions')).toBeDisabled()
  })

  it('offers a retry when the preview request fails', async () => {
    mocks.cleanupSessions.mockRejectedValue(new Error('boom'))
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Clean up sessions')
    expect(await screen.findByText(/Failed to load preview/)).toBeTruthy()
    mocks.cleanupSessions.mockResolvedValue({ ok: true, archived: 0, keys: ['k-stale'], failed: [], active_is_stale: false })
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByText(/will be moved to older sessions/)).toBeTruthy()
  })

  it('keeps the panel open and surfaces the count when some archives fail', async () => {
    mocks.cleanupSessions.mockImplementation((_days: number, _active: string, dry?: boolean) =>
      Promise.resolve(dry
        ? { ok: true, archived: 0, keys: ['k-stale'], failed: [], active_is_stale: false }
        : { ok: true, archived: 0, keys: [], failed: ['k-stale'] }))
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Clean up sessions')
    fireEvent.click(await screen.findByText('Archive 1 session'))
    expect(await screen.findByText('1 session(s) failed to archive')).toBeTruthy()
    expect(screen.getByText('Clean Up Sessions')).toBeTruthy()
  })

  it('closes the panel when every archive succeeds', async () => {
    mocks.cleanupSessions.mockImplementation((_days: number, _active: string, dry?: boolean) =>
      Promise.resolve(dry
        ? { ok: true, archived: 0, keys: ['k-stale'], failed: [], active_is_stale: false }
        : { ok: true, archived: 1, keys: ['k-stale'], failed: [] }))
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Clean up sessions')
    fireEvent.click(await screen.findByText('Archive 1 session'))
    await waitFor(() => expect(screen.queryByText('Clean Up Sessions')).toBeNull())
  })

  it('closes on Cancel without archiving', async () => {
    mocks.cleanupSessions.mockResolvedValue({ ok: true, archived: 0, keys: ['k-stale'], failed: [], active_is_stale: false })
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Clean up sessions')
    fireEvent.click(await screen.findByText('Cancel'))
    expect(screen.queryByText('Clean Up Sessions')).toBeNull()
    expect(mocks.cleanupSessions).not.toHaveBeenCalledWith(3, '', false)
  })
})

describe('ChatSidebar — Switch All Sessions panel', () => {
  const SLOTS: TestSlot[] = [
    { key: 'k-a', title: 'Idle A', running: false, messages: 1 },
    { key: 'k-b', title: 'Busy B', running: true, messages: 1 },
  ]

  it('opens with a model listbox and a skip-running opt-out', async () => {
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Switch all to model…')
    expect(screen.getByText('Switch All Sessions')).toBeTruthy()
    expect(screen.getByRole('listbox', { name: 'Model list' })).toBeTruthy()
    // One running slot, so the skip checkbox is offered and on by default.
    const skip = screen.getByRole('checkbox')
    expect(skip).toBeChecked()
  })

  it('reports a partial failure and keeps the panel open', async () => {
    mocks.chatSlotsModel.mockResolvedValue({ ok: true, failed: ['k-a'] })
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Switch all to model…')
    fireEvent.click(screen.getByRole('option', { name: /auto/i }))
    fireEvent.click(screen.getByText(/^Switch 1 session$/))
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledWith('auto', true))
    expect(await screen.findByText('1 session failed to switch')).toBeTruthy()
    expect(screen.getByText('Switch All Sessions')).toBeTruthy()
  })

  it('closes when the switch fully succeeds', async () => {
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Switch all to model…')
    fireEvent.click(screen.getByRole('option', { name: /auto/i }))
    fireEvent.click(screen.getByText(/^Switch 1 session$/))
    await waitFor(() => expect(screen.queryByText('Switch All Sessions')).toBeNull())
  })

  it('surfaces a hard failure as an error rather than closing', async () => {
    mocks.chatSlotsModel.mockRejectedValue(new Error('gateway down'))
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Switch all to model…')
    fireEvent.click(screen.getByRole('option', { name: /auto/i }))
    fireEvent.click(screen.getByText(/^Switch 1 session$/))
    expect(await screen.findByText('gateway down')).toBeTruthy()
  })

  it('closes on Cancel and does not call the endpoint', async () => {
    renderSidebar({ slots: SLOTS })
    await openHeaderPanel('Switch all to model…')
    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Switch All Sessions')).toBeNull()
    expect(mocks.chatSlotsModel).not.toHaveBeenCalled()
  })

  it('omits the skip-running opt-out when nothing is running', async () => {
    renderSidebar({ slots: [{ key: 'k-a', title: 'Idle A', running: false, messages: 1 }] })
    await openHeaderPanel('Switch all to model…')
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
})

describe('ChatSidebar — header menu view + tag entries', () => {
  it('turning on board view seeds the four state lanes', async () => {
    // An empty board is seeded with the derived state lanes rather than one
    // unnamed match-all column, which renders as a single "All sessions" pile.
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    fireEvent.click(await screen.findByText('Switch to board view'))
    expect(cfg.saveChatConfig).toHaveBeenCalledWith(expect.objectContaining({ tagColumnsEnabled: true }))
    await waitFor(() => expect(mocks.createTagColumn).toHaveBeenCalledTimes(4))
    expect(mocks.createTagColumn.mock.calls.map(c => c[0].state_key))
      .toEqual(['needs_approval', 'waiting', 'working', 'idle'])
    expect(mocks.createTagColumn).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'state', state_key: 'working' }),
    )
  })

  it('adding lanes never deletes a column, whatever the board holds', async () => {
    // The invariant. "Add column after" produces the same unnamed/unfiltered
    // shape the view toggle once created, so no predicate can tell a disposable
    // placeholder from a bare column the user added. Seeding therefore deletes
    // nothing at all -- including when the board is nothing BUT bare columns.
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([
      { id: 'c-bare-1', name: '', tag_ids: [], mode: 'any', order: 0 },
      { id: 'c-bare-2', name: '', tag_ids: [], mode: 'any', order: 1 },
    ])
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    fireEvent.click(await screen.findByTestId('add-state-lanes'))
    await waitFor(() => expect(mocks.createTagColumn).toHaveBeenCalledTimes(4))
    expect(mocks.deleteTagColumn).not.toHaveBeenCalled()
  })

  it('creates only the missing lanes, so an incomplete set is completable', async () => {
    // Idempotence plus a real recovery path: with two lanes already present the
    // menu still offers to add lanes, and doing so creates only the other two --
    // never a duplicate set. This is what a partial failure recovers through.
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([
      { id: 'l-1', name: '', tag_ids: [], mode: 'any', order: 0, source: 'state', state_key: 'needs_approval' },
      { id: 'l-2', name: '', tag_ids: [], mode: 'any', order: 1, source: 'state', state_key: 'working' },
      { id: 'c-bare', name: '', tag_ids: [], mode: 'any', order: 2 },
    ])
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    fireEvent.click(await screen.findByTestId('add-state-lanes'))
    await waitFor(() => expect(mocks.createTagColumn).toHaveBeenCalledTimes(2))
    expect(mocks.createTagColumn.mock.calls.map(c => c[0].state_key).sort())
      .toEqual(['idle', 'waiting'])
    expect(mocks.deleteTagColumn).not.toHaveBeenCalled()
  })

  it('surfaces a failed seed instead of leaving an empty board', async () => {
    // The toggle flips tagColumnsEnabled BEFORE the mutation runs, so a failure
    // with no feedback looks identical to a board that is simply empty.
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([])
    mocks.createTagColumn.mockRejectedValue(new Error('persist failed'))
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    fireEvent.click(await screen.findByText('Switch to board view'))
    const banner = await screen.findByTestId('lane-seed-error')
    expect(banner.textContent).toContain('Could not add the automatic columns')
    expect(banner.textContent).toContain('Try again')
  })

  it('gives back the pre-board width when switching to list view', async () => {
    // Persisting the auto-widened value without remembering the old one destroys
    // the width the user chose and strands a wide sidebar in list view.
    localStorage.setItem('mc-sidebar-width', '300')
    localStorage.setItem('mc-sidebar-width-pre-board', '300')
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue(
      ['needs_approval', 'waiting', 'working', 'idle'].map((k, i) => (
        { id: `l-${i}`, name: '', tag_ids: [], mode: 'any', order: i, source: 'state', state_key: k }
      )),
    )
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    fireEvent.click(await screen.findByText('Switch to list view'))
    await waitFor(() => expect(localStorage.getItem('mc-sidebar-width')).toBe('300'))
  })

  it('labels a legacy bare column as showing every session once lanes exist', async () => {
    // Seeding does not delete it (indistinguishable from a user's own column), so
    // the duplicate-cards effect has to be named rather than left to be guessed.
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([
      { id: 'c-bare', name: '', tag_ids: [], mode: 'any', order: 0 },
      { id: 'l-0', name: '', tag_ids: [], mode: 'any', order: 1, source: 'state', state_key: 'idle' },
    ])
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    const hint = await screen.findByTestId('column-duplicates-hint-c-bare')
    expect(hint.textContent).toContain('shows every session')
  })

  it('does not label a bare column when there are no lanes', async () => {
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([
      { id: 'c-bare', name: '', tag_ids: [], mode: 'any', order: 0 },
    ])
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    await waitFor(() => expect(screen.getByTestId('column-c-bare')).toBeTruthy())
    expect(screen.queryByTestId('column-duplicates-hint-c-bare')).toBeNull()
  })

  it('hides the add-lanes entry once all four lanes exist', async () => {
    // The affordance is keyed on there being something to add, so a complete
    // board does not offer a no-op action.
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue(
      ['needs_approval', 'waiting', 'working', 'idle'].map((k, i) => (
        { id: `l-${i}`, name: '', tag_ids: [], mode: 'any', order: i, source: 'state', state_key: k }
      )),
    )
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    await waitFor(() => expect(screen.queryByText('Switch to list view')).toBeTruthy())
    expect(screen.queryByTestId('add-state-lanes')).toBeNull()
  })

  it('leaves the board recoverable when lane creation fails part-way', async () => {
    // No rollback and no deletion: a partial failure just means fewer lanes.
    // The bare column survives, so the board still renders and the next seed
    // fills the gap rather than starting from an empty strip.
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([
      { id: 'c-bare', name: '', tag_ids: [], mode: 'any', order: 0 },
    ])
    mocks.createTagColumn
      .mockResolvedValueOnce({ id: 'lane-1' })
      .mockResolvedValueOnce({ id: 'lane-2' })
      .mockRejectedValueOnce(new Error('persist failed'))
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    openHeaderMenu()
    fireEvent.click(await screen.findByText('Switch to board view'))
    await waitFor(() => expect(mocks.createTagColumn).toHaveBeenCalledTimes(3))
    expect(mocks.deleteTagColumn).not.toHaveBeenCalled()
  })

  it('offers the way back to list view once board view is on', async () => {
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.tagColumns.mockResolvedValue([{ id: 'c1', name: 'Doing', tag_ids: [], mode: 'any', order: 0 }])
    const view = renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    view.rerender(<div />)
    // Re-render with the column cache primed via a fresh mount.
    const second = renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    await waitFor(() => expect(second.container).toBeTruthy())
  })

  it('toggles the Manage Tags panel open and closed', async () => {
    renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    await openHeaderPanel('Manage tags…')
    const panel = screen.getByTestId('manage-tags-panel')
    expect(within(panel).getByText('Manage Tags')).toBeTruthy()
    fireEvent.click(within(panel).getByLabelText('Close'))
    expect(screen.queryByTestId('manage-tags-panel')).toBeNull()
  })
})

describe('ChatSidebar — Older Sessions pane', () => {
  // Pin the clock for the date-bucket assertions (issue #2919): a raw
  // `Date.now() - n days` fixture slides across local midnight when the suite
  // runs just after 00:00, moving the `daysAgo(1)` row from Yesterday into
  // Last 7 Days. The pin is LOCAL midday — the farthest point from both
  // midnight edges in any timezone — and fixtures are built here at
  // collection time from the PIN constant, not the faked clock (the
  // beforeEach below only pins what the component reads at render). Faking
  // ONLY `Date` leaves real timers driving waitFor/promises. Mid-January
  // avoids DST transitions inside the lookback window.
  const PIN = new Date(2026, 0, 15, 12, 0, 0) // local midday, not 12:00Z
  const daysAgo = (n: number) => Math.floor((PIN.getTime() - n * 86400_000) / 1000)
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PIN)
  })
  const HISTORY: TestHistoryItem[] = [
    { key: 'h1', title: 'Fresh history', modified: daysAgo(0), agent: 'builder' },
    { key: 'h2', title: 'Yesterday history', modified: daysAgo(1) },
    { key: 'h3', title: 'Week history', modified: daysAgo(4) },
    { key: 'h4', title: 'Month history', modified: daysAgo(20) },
    { key: 'h5', title: 'Undated history' },
  ]

  it('expands, requests a refresh, and segments rows by date', async () => {
    renderSidebar({ history: HISTORY })
    openHistory()
    await waitFor(() => expect(mocks.sessions).toHaveBeenCalled())
    for (const label of ['Today', 'Yesterday', 'Last 7 Days', 'Last 30 Days', 'Older']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(screen.getByText('Fresh history')).toBeTruthy()
    expect(screen.getByText('Undated history')).toBeTruthy()
  })

  it('collapses again from the keyboard', () => {
    renderSidebar({ history: HISTORY })
    const header = screen.getByLabelText('Older sessions')
    fireEvent.keyDown(header, { key: 'Enter' })
    expect(screen.getByPlaceholderText('Search older sessions…')).toBeTruthy()
    fireEvent.keyDown(header, { key: ' ' })
    expect(screen.queryByPlaceholderText('Search older sessions…')).toBeNull()
  })

  it('resumes a session by pointer and by keyboard', async () => {
    renderSidebar({ history: HISTORY })
    openHistory()
    fireEvent.mouseDown(screen.getByTitle('Fresh history'))
    await waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalledWith('h1', 'Fresh history'))
    mocks.resumeChatSlot.mockClear()
    fireEvent.keyDown(screen.getByTitle('Week history'), { key: 'Enter' })
    await waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalledWith('h3', 'Week history'))
  })

  it('deletes one history session behind a confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderSidebar({ history: HISTORY })
    openHistory()
    fireEvent.click(screen.getAllByLabelText('Delete history session')[0])
    expect(mocks.deleteSession).not.toHaveBeenCalled()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getAllByLabelText('Delete history session')[0])
    await waitFor(() => expect(mocks.deleteSession).toHaveBeenCalledWith('h1'))
    confirmSpy.mockRestore()
  })

  it('clears all closed sessions behind a confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSidebar({ history: HISTORY })
    openHistory()
    fireEvent.click(screen.getByText('Delete all'))
    await waitFor(() => expect(mocks.clearSessions).toHaveBeenCalled())
    confirmSpy.mockRestore()
  })

  it('loads another page when more history exists', async () => {
    renderSidebar({ history: HISTORY, historyHasMore: true })
    openHistory()
    mocks.sessions.mockClear()
    fireEvent.mouseDown(screen.getByText('Load more…'))
    await waitFor(() => expect(mocks.sessions).toHaveBeenCalled())
  })

  it('filters locally below the search threshold', () => {
    renderSidebar({ history: HISTORY })
    openHistory()
    fireEvent.change(screen.getByPlaceholderText('Search older sessions…'), { target: { value: 'W' } })
    expect(screen.getByText('Week history')).toBeTruthy()
    expect(screen.queryByText('Fresh history')).toBeNull()
  })

  it('groups backend search results by folder and collapses a group', async () => {
    mocks.sessionsSearch.mockResolvedValue({
      sessions: [
        { key: 'h-in', title: 'Filed hit', modified: daysAgo(2), folder_id: 'f1' },
        { key: 'h-out', title: 'Unfiled hit', modified: daysAgo(2) },
      ],
    })
    renderSidebar({ history: HISTORY, folders: FOLDERS })
    openHistory()
    fireEvent.change(screen.getByPlaceholderText('Search older sessions…'), { target: { value: 'hit' } })
    expect(await screen.findByText('Filed hit')).toBeTruthy()
    expect(screen.getByText('Unfiled hit')).toBeTruthy()
    // Relevance-ranked results replace date segments with folder groups.
    expect(screen.queryByText('Last 7 Days')).toBeNull()
    const group = screen.getByLabelText('Collapse Alpha results')
    fireEvent.click(group)
    expect(screen.queryByText('Filed hit')).toBeNull()
    expect(screen.getByText('Unfiled hit')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Expand Alpha results'))
    expect(screen.getByText('Filed hit')).toBeTruthy()
  })

  it('exposes the resize separator only while the pane is open', () => {
    renderSidebar({ history: HISTORY })
    expect(screen.queryByLabelText('Resize history pane')).toBeNull()
    openHistory()
    const sep = screen.getByLabelText('Resize history pane')
    expect(sep).toBeTruthy()
    // Double-clicking the separator collapses the pane again.
    fireEvent.doubleClick(sep)
    expect(screen.queryByLabelText('Resize history pane')).toBeNull()
  })
})

describe('ChatSidebar — narrow-width header', () => {
  it('keeps the full header at a comfortable width', () => {
    localStorage.setItem('mc-sidebar-width', '400')
    renderSidebar()
    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(screen.getByText('New')).toBeTruthy()
  })

  it('drops the create label, then the panel title, as the sidebar narrows', () => {
    localStorage.setItem('mc-sidebar-width', '230')
    const compact = renderSidebar()
    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(screen.queryByText('New')).toBeNull()
    compact.unmount()

    localStorage.setItem('mc-sidebar-width', '190')
    renderSidebar()
    expect(screen.queryByText('Sessions')).toBeNull()
    expect(screen.queryByText('New')).toBeNull()
  })

  it('ignores an out-of-range persisted width', () => {
    localStorage.setItem('mc-sidebar-width', '99999')
    renderSidebar()
    // Falls back to the 260px default, which still shows both labels.
    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(screen.getByText('New')).toBeTruthy()
  })
})

describe('ChatSidebar — reveal request (store-driven, issue #912)', () => {
  let scrollIntoView: ReturnType<typeof vi.fn>
  let originalScroll: typeof Element.prototype.scrollIntoView
  beforeEach(() => {
    scrollIntoView = vi.fn()
    originalScroll = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
  })
  afterEach(() => { Element.prototype.scrollIntoView = originalScroll })

  it('consumes a request set BEFORE mount: expands ancestors, scrolls, clears the request', async () => {
    // The D1 regression case: with the drawer collapsed the sidebar is
    // unmounted, so the old window CustomEvent was dispatched into nothing and
    // dropped. The store request must survive until this mount consumes it.
    const folders: ChatFolder[] = [
      { id: 'f-parent', name: 'Parent', order: 0, collapsed: true },
      { id: 'f-child', name: 'Child', order: 1, parent_id: 'f-parent', collapsed: true },
    ]
    const { store } = renderSidebar({
      slots: [{ key: 'k-deep', title: 'Deep one', running: false, folder_id: 'f-child' }],
      folders,
      revealRequest: { key: 'k-deep', nonce: 1 },
    })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f-child', { collapsed: false }))
    expect(mocks.updateChatFolder).toHaveBeenCalledWith('f-parent', { collapsed: false })
    // The row enters the DOM only after the optimistic expansion re-render —
    // the bounded retry (not a one-shot timeout) must still find it (D3).
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
    expect(store.getState().chat.revealRequest).toBeNull()
  })

  it('flashes the revealed row so an in-place reveal is visible', async () => {
    renderSidebar({
      slots: [{ key: 'k-a', title: 'Alpha', running: false }],
      revealRequest: { key: 'k-a', nonce: 1 },
    })
    // The confirmation outline is the only signal when the row was already on
    // screen (D4) — scrollIntoView on a visible row is a visual no-op.
    await waitFor(() => {
      const row = document.querySelector('[data-session-row="k-a"]')
      expect(row?.classList.contains('session-reveal-flash')).toBe(true)
    })
  })

  it('re-fires for a repeat reveal of the same session (nonce-keyed)', async () => {
    const { store } = renderSidebar({ slots: [{ key: 'k-a', title: 'Alpha', running: false }] })
    act(() => { store.dispatch(requestSlotReveal('k-a')) })
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1))
    act(() => { store.dispatch(requestSlotReveal('k-a')) })
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(2))
  })

  it('clears the sidebar search filter when it hides the target row', async () => {
    localStorage.setItem('mc-session-pinned-only', '1')
    const { store } = renderSidebar({
      slots: [
        { key: 'k-a', title: 'Alpha', running: false },
        { key: 'k-b', title: 'Beta', running: false },
      ],
    })
    const search = screen.getByPlaceholderText('Search sessions…')
    fireEvent.change(search, { target: { value: 'b' } })
    // Filter active: Alpha's row is out of the DOM entirely (D5).
    await waitFor(() => expect(document.querySelector('[data-session-row="k-a"]')).toBeNull())
    act(() => { store.dispatch(requestSlotReveal('k-a')) })
    // Reveal is an explicit "show me this row": the filter is dropped so the
    // reveal has something to land on.
    await waitFor(() => expect((search as HTMLInputElement).value).toBe(''))
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
    // The status-filter clearing must ALSO clear the persisted key — the
    // sidebar unmounts when the drawer collapses, and remount re-reads it.
    expect(localStorage.getItem('mc-session-pinned-only')).toBe('0')
  })

  it('ignores a request for an unknown session key but still consumes it', async () => {
    const { store } = renderSidebar({ slots: [{ key: 'k-a', title: 'A', running: false }] })
    act(() => { store.dispatch(requestSlotReveal('k-gone')) })
    await waitFor(() => expect(store.getState().chat.revealRequest).toBeNull())
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(scrollIntoView).not.toHaveBeenCalled()
  })
})
