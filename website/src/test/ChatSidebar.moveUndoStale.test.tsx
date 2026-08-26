/**
 * A retired undo offer must not still be actionable.
 *
 * `AnimatePresence` keeps the bar mounted for its 150ms exit animation, and that
 * exiting instance still holds the props it had while live — including the
 * `onUndo` it was given. A click or ⌘Z inside that window would therefore fire an
 * undo for an offer that has already been retired, overwriting whatever newer
 * placement retired it. The sidebar closes this by re-checking the offer's
 * identity against CURRENT state at invocation time rather than trusting the
 * closure it was created in.
 *
 * This is tested by capturing the `onUndo` prop and calling it AFTER retirement —
 * which is exactly what the exiting instance does, and is testable without
 * depending on animation timing (the sibling suite renders framer-motion as plain
 * DOM, so there is no exit window there to exercise at all).
 *
 * The guard has two parts and only one is pinned here. Reading CURRENT state
 * (rather than the closure) is what closes the reported defect, and the first case
 * below fails without it. The id comparison on top of it covers a narrower case —
 * a NEW offer armed inside the exit window, where current state is non-null and a
 * bare "is there an offer?" check would undo the wrong one. That case could not be
 * constructed here: retiring the first offer moves the session out of the target
 * folder, whose board drop zone then unmounts, so a second drag has nothing to
 * aim at. The comparison is kept as defence in depth, deliberately untested.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { updateSlotFolder } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder, Slot } from '../types'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({
  setSlotFolder: vi.fn(),
  /** The last `onUndo` the sidebar handed the bar — the closure the exiting
   *  instance would still be holding. */
  lastOnUndo: { current: null as null | (() => void) },
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

// Stub the bar: this suite is about the OWNER's guard, not the bar's rendering,
// and a stub is what lets the retained callback be invoked after retirement.
vi.mock('../components/SessionMoveUndoBar', () => ({
  MOVE_UNDO_MS: 8000,
  default: ({ onUndo }: { onUndo: () => void }) => {
    mocks.lastOnUndo.current = onUndo
    return null
  },
}))

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
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

const TAG = '11111111-1111-1111-1111-111111111111'
const COL = 'col-aaaa'
const SLOT_KEY = 'chat-stale-1'
const ARCHIVE = 'folder-archive'
const OTHER = 'folder-later'

const tags: ChatTag[] = [{ id: TAG, name: 'Blocked', color: '#e11', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Planned', tag_ids: [TAG], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [
  { id: ARCHIVE, name: 'Archive', order: 0 },
  { id: OTHER, name: 'Later', order: 1 },
]

function renderSidebar() {
  const slot = {
    key: SLOT_KEY, title: 'Session drag lands in the wrong folder', messages: 0,
    running: false, tags: [TAG], created: '', last_ts: '', folder_id: '',
  } as Slot
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [slot], slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  const Harness = () => {
    const slots = useAppSelector(s => s.dashboard.slots)
    return (
      <ChatSidebar
        slots={slots} activeSlot={null} unreadSlots={[]}
        history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
      />
    )
  }
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider><MemoryRouter><Harness /></MemoryRouter></ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    ),
    store,
  }
}

function dropSessionOnArchive(container: HTMLElement) {
  const target = container.querySelector(`[data-testid="col-${COL}-folder-${ARCHIVE}"]`)
  expect(target).toBeTruthy()
  const dataTransfer = { getData: (t: string) => (t === 'text/plain' ? SLOT_KEY : ''), types: ['text/plain'] }
  fireEvent.dragOver(target as HTMLElement, { dataTransfer })
  fireEvent.drop(target as HTMLElement, { dataTransfer })
}

beforeEach(() => {
  localStorage.clear()
  mocks.setSlotFolder.mockResolvedValue({})
  mocks.lastOnUndo.current = null
})
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers() })

describe('a retired undo offer is inert', () => {
  it('does not act when its retained callback fires after retirement', async () => {
    const { container, store } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(mocks.lastOnUndo.current).toBeTruthy())
    const staleUndo = mocks.lastOnUndo.current!
    // Retire the offer: the session moves on from another surface.
    act(() => { store.dispatch(updateSlotFolder({ key: SLOT_KEY, folderId: OTHER })) })
    await act(async () => { await Promise.resolve() })
    mocks.setSlotFolder.mockClear()
    // The exiting instance's button/chord would call exactly this.
    act(() => { staleUndo() })
    await act(async () => { await Promise.resolve() })
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('still acts while the offer is the live one', async () => {
    // The guard must reject only STALE invocations, not undo itself.
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(mocks.lastOnUndo.current).toBeTruthy())
    mocks.setSlotFolder.mockClear()
    act(() => { mocks.lastOnUndo.current!() })
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, null))
  })
})
