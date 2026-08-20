/**
 * Drag-to-folder arms an undo offer, and that offer sits where it can be seen.
 *
 * A drag is the one folder move that never names its destination, so the
 * sidebar has to say where the session went. Two things are locked here:
 *
 *  1. **Wiring** — a drop performs the move AND records its inverse, undo posts
 *     the ORIGINAL folder back, and a drop onto the folder the session already
 *     lives in arms nothing (there would be nothing to undo).
 *  2. **Placement** — the bar renders after the session lanes and BEFORE the
 *     "Older sessions" footer, and outside every scroll container. That is the
 *     whole point of putting it in the flow: it must not cover the persistent
 *     footer control, and must not scroll away with the list.
 *
 * The board-view folder header is the drop target used here because it is a
 * native HTML5 drop zone; the list-view path is dnd-kit, whose pointer sensor
 * jsdom cannot drive (same constraint as ChatSidebar.boardNewChatInFolder).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { sseSlots, updateSlotFolder } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder, Slot } from '../types'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({ setSlotFolder: vi.fn() }))

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
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
import { MOVE_UNDO_MS } from '../components/SessionMoveUndoBar'

const TAG = '11111111-1111-1111-1111-111111111111'
const COL = 'col-aaaa'
const SLOT_KEY = 'chat-undo-1'
const ARCHIVE = 'folder-archive'
const OTHER = 'folder-later'

const tags: ChatTag[] = [{ id: TAG, name: 'Blocked', color: '#e11', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Planned', tag_ids: [TAG], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [
  { id: ARCHIVE, name: 'Archive', order: 0 },
  { id: OTHER, name: 'Later', order: 1 },
]

function renderSidebar(folderId = '') {
  const slot = {
    key: SLOT_KEY, title: 'Session drag lands in the wrong folder', messages: 0,
    running: false, tags: [TAG], created: '', last_ts: '', folder_id: folderId,
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
  // `slots` is read from the store rather than pinned to a literal, because
  // that is what ChatPage does (`filteredSlots` derives from
  // `dashboard.slots`). The move is OPTIMISTIC — it dispatches the new
  // folder_id into the store — so a harness that froze the prop would never
  // show the sidebar the move it just made.
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
          <ThemeProvider>
            <MemoryRouter>
              <Harness />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    ),
    store,
    qc,
  }
}

/** The undo bar inside THIS render. Scoped to `container` rather than
 *  `screen`, because `screen` searches all of document.body and this DOM
 *  implementation happily returns a DETACHED bar left behind by an earlier
 *  test's render — which then compares as "disconnected" against every live
 *  node and silently passes any ordering assertion. */
const barIn = (c: HTMLElement) => c.querySelector('[data-testid="session-move-undo"]') as HTMLElement | null
const undoButtonIn = (c: HTMLElement) => c.querySelector('[data-testid="session-move-undo-button"]') as HTMLElement

/** Fire a native session drop on the board-view folder header. */
function dropSessionOnArchive(container: HTMLElement, slotKey = SLOT_KEY) {
  const target = container.querySelector(`[data-testid="col-${COL}-folder-${ARCHIVE}"]`)
  expect(target).toBeTruthy()
  const dataTransfer = { getData: (t: string) => (t === 'text/plain' ? slotKey : ''), types: ['text/plain'] }
  fireEvent.dragOver(target as HTMLElement, { dataTransfer })
  fireEvent.drop(target as HTMLElement, { dataTransfer })
}

beforeEach(() => { localStorage.clear(); mocks.setSlotFolder.mockResolvedValue({}) })
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers() })

describe('drag-to-folder undo', () => {
  it('performs the move and offers it back, naming the destination', async () => {
    const { container } = renderSidebar()
    expect(container.querySelector('[data-testid="session-move-undo"]')).toBeNull()
    dropSessionOnArchive(container)
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, ARCHIVE))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    expect(barIn(container)!.textContent).toContain('Archive')
  })

  it('undo posts the ORIGINAL folder back, then retires the offer', async () => {
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    mocks.setSlotFolder.mockClear()
    fireEvent.click(undoButtonIn(container))
    // null, not '' — the move hook's contract for "no folder".
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, null))
    expect(barIn(container)).toBeNull()
  })

  it('offers nothing until the server has acknowledged the move', async () => {
    // The move is optimistic: the store shows Archive at once. Going live on
    // that would let the user undo while the original PATCH is still in flight —
    // undo's compare-and-set would be refused and the original write would then
    // land, silently reversing it. So the bar waits for the acknowledgement.
    let ack: (v: unknown) => void = () => {}
    mocks.setSlotFolder.mockImplementationOnce(() => new Promise(res => { ack = res }))
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, ARCHIVE))
    expect(barIn(container)).toBeNull()
    await act(async () => { ack({}) })
    await waitFor(() => expect(barIn(container)).toBeTruthy())
  })

  it('offers nothing at all when the move itself failed', async () => {
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('boom'))
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(barIn(container)).toBeNull()
  })

  it('never arms an offer that another client superseded while it was pending', async () => {
    // The nastier half of the ack race, and the one no later check can catch:
    // another client moves the session AWAY AND BACK inside the pending window.
    // By the time our ack lands, live state matches the destination again, so a
    // check that only judges armed offers sees nothing wrong and arms an inverse
    // that would overwrite that newer, intentional placement. The divergence is
    // only ever visible WHILE pending, so it has to be latched there.
    let ack: (v: unknown) => void = () => {}
    mocks.setSlotFolder.mockImplementationOnce(() => new Promise(res => { ack = res }))
    const { container, store } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, ARCHIVE))
    act(() => { store.dispatch(updateSlotFolder({ key: SLOT_KEY, folderId: OTHER })) })
    await act(async () => { await Promise.resolve() })
    act(() => { store.dispatch(updateSlotFolder({ key: SLOT_KEY, folderId: ARCHIVE })) })
    await act(async () => { await Promise.resolve() })
    // Our own move is acknowledged — but it is no longer the last word.
    await act(async () => { ack({}) })
    await act(async () => { await Promise.resolve() })
    expect(barIn(container)).toBeNull()
  })

  it('arms nothing when the session is dropped on the folder it already lives in', async () => {    const { container } = renderSidebar(ARCHIVE)
    dropSessionOnArchive(container)
    // Nothing to await — assert on a settled tick so a late call would still be seen.
    await Promise.resolve()
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
    expect(container.querySelector('[data-testid="session-move-undo"]')).toBeNull()
  })

  it('retires the offer when the moved session is closed', async () => {
    const { container, store } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    // The session goes away (closed elsewhere): undo has nothing to put back,
    // so the offer must not linger as a button that silently does nothing.
    act(() => { store.dispatch(sseSlots([])) })
    await waitFor(() => expect(barIn(container)).toBeNull())
  })

  it('expires the offer on its own clock', async () => {
    // Fake timers from the start: installing them AFTER the drop would leave the
    // already-scheduled deadline on the real clock, where advanceTimersByTime
    // cannot reach it — and the test would "pass" for the wrong reason.
    vi.useFakeTimers()
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    // The move is optimistic: let its microtasks land (they are not faked) so
    // the offer goes live and the bar mounts.
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeTruthy()
    // The clock is the SIDEBAR's, not the bar's: an offer whose move never
    // became visible has no bar to run a timer and must still die.
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 50) })
    expect(barIn(container)).toBeNull()
  })

  it('never revives an offer once the session has been moved again', async () => {
    const { container, store } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    // A later, INTENTIONAL move from another surface (a row menu) sends the
    // session elsewhere — the recorded inverse is dead from here on.
    act(() => { store.dispatch(updateSlotFolder({ key: SLOT_KEY, folderId: OTHER })) })
    await waitFor(() => expect(barIn(container)).toBeNull())
    // …and moving it BACK into Archive must not resurrect it. If the bar's
    // visibility were derived from live state, this offer would match again and
    // its Undo would overwrite the newer move by dragging the session to root.
    act(() => { store.dispatch(updateSlotFolder({ key: SLOT_KEY, folderId: ARCHIVE })) })
    await Promise.resolve()
    expect(barIn(container)).toBeNull()
  })

  it('degrades a deleted origin folder to unfiled instead of replaying a dead id', async () => {
    // Session starts in Later, is dragged into Archive, and Later is deleted
    // inside the undo window. Replaying `folder-later` would be rejected by the
    // endpoint as an unknown folder (400) and Undo would do nothing at all, so
    // the origin is degraded to unfiled — which is also what the sidebar already
    // renders for a folder id it does not know.
    const { container, qc } = renderSidebar(OTHER)
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    act(() => { qc.setQueryData(['chat-folders'], [folders[0]]) })
    mocks.setSlotFolder.mockClear()
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, null))
  })

  it('ignores an undo fired by an offer that has already been retired', async () => {
    // AnimatePresence keeps the retired bar mounted for its exit animation, and
    // that instance still holds the props it had while live — so a click or ⌘Z in
    // that window would fire a stale undo and overwrite the newer placement.
    const { container, store } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    const button = undoButtonIn(container)
    // Retire the offer by moving the session on from another surface…
    act(() => { store.dispatch(updateSlotFolder({ key: SLOT_KEY, folderId: OTHER })) })
    await act(async () => { await Promise.resolve() })
    mocks.setSlotFolder.mockClear()
    // …then fire the button the exiting instance still owns.
    fireEvent.click(button)
    await act(async () => { await Promise.resolve() })
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('suspends the expiry deadline while the bar is held', async () => {
    vi.useFakeTimers()
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeTruthy()
    // Pointer over the bar: the deadline must not fire under a hand that is
    // already reaching for Undo.
    fireEvent.mouseEnter(barIn(container)!)
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 2) })
    expect(barIn(container)).toBeTruthy()
    // Released: the clock resumes and the offer expires.
    fireEvent.mouseLeave(barIn(container)!)
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 50) })
    expect(barIn(container)).toBeNull()
  })

  it('resumes the held clock from its remainder instead of granting a fresh window', async () => {
    vi.useFakeTimers()
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeTruthy()
    // Most of the window is already spent when the user notices the bar…
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1000) })
    expect(barIn(container)).toBeTruthy()
    // …and reaches for Undo. The hold freezes the clock where it stands.
    fireEvent.mouseEnter(barIn(container)!)
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 2) })
    expect(barIn(container)).toBeTruthy()
    // On release only the REMAINDER may be left. Re-arming the full window here
    // would keep the bar up for another 8s — and since the countdown draws this
    // same number, it would sit empty for all of it and read as expired.
    fireEvent.mouseLeave(barIn(container)!)
    act(() => { vi.advanceTimersByTime(500) })
    expect(barIn(container)).toBeTruthy()
    act(() => { vi.advanceTimersByTime(600) })
    expect(barIn(container)).toBeNull()
  })

  it('sits above the Older sessions footer, not over it', async () => {
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    const bar = barIn(container)!
    const footer = container.querySelector('[aria-label="Older sessions"]')
    expect(footer).toBeTruthy()
    // Same parent + earlier in document order = the bar pushes the footer down
    // instead of overlaying it. Compared by index in `querySelectorAll('*')`
    // (document order) rather than `compareDocumentPosition`, which this DOM
    // implementation answers with the DISCONNECTED bit set either way — it
    // reported "footer follows" even when the footer had been moved in FRONT.
    expect(footer!.parentElement).toBe(bar.parentElement)
    const order = Array.from(container.querySelectorAll('*'))
    expect(order.indexOf(bar)).toBeGreaterThanOrEqual(0)
    expect(order.indexOf(bar)).toBeLessThan(order.indexOf(footer!))
  })

  it('renders outside every scroll container, so the list cannot scroll it away', async () => {
    const { container } = renderSidebar()
    dropSessionOnArchive(container)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    const bar = barIn(container)!
    // Guard against a vacuous pass: a session row DOES have a scrolling
    // ancestor in this tree, so the walk below is looking at real classes.
    const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)!
    let rowHasScrollAncestor = false
    for (let el = row.parentElement; el && el !== container; el = el.parentElement) {
      if (/overflow-y-auto|overflow-auto/.test(el.className)) rowHasScrollAncestor = true
    }
    expect(rowHasScrollAncestor).toBe(true)
    for (let el = bar.parentElement; el && el !== container; el = el.parentElement) {
      expect(el.className).not.toMatch(/overflow-y-auto|overflow-auto/)
    }
  })
})


