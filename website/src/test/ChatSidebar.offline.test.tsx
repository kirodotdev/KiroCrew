/**
 * Test: ChatSidebar offline session-row + history click blocking.
 * Covers the offline-UX session-row / history click-blocking guards:
 *   - L760-761: active-list onClick early-returns when `connected=false && !isActive`
 *     and skips dispatching `switchSlot`.
 *   - L1392, L1395: history-list onMouseDown early-returns when `connected=false`
 *     and skips dispatching `resumeFromHistory`.
 *
 * No existing ChatSidebar tests in src/test/, so this file owns the mock setup
 * for the component. Mocks the chat slice's switchSlot / resumeFromHistory thunks
 * so we can assert they were NOT called when offline. Renders ChatSidebar
 * directly (no router needed beyond MemoryRouter) and simulates user clicks.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Mock the chat slice thunks BEFORE importing the component so we can
// observe whether they are dispatched. switchSlot + resumeFromHistory are
// the two functions that the offline guards block in production.
// vi.hoisted() ensures these vi.fn() instances exist by the time the hoisted
// vi.mock() factory runs (factories execute before top-level statements).
const { switchSlotMock, resumeFromHistoryMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
  // Real dispatch of a real createAsyncThunk() call is augmented with
  // .unwrap() by RTK; this mock returns a plain action object instead, so it
  // needs its own `unwrap` to match that shape now that ChatSidebar's resume
  // handler chains off it (#3624). Never-resolving is fine — these tests only
  // assert the dispatch call happened, not what resume does after it resolves.
  resumeFromHistoryMock: vi.fn(() => ({ type: 'chat/resumeFromHistory/pending', meta: {}, unwrap: () => new Promise(() => {}) })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return {
    ...actual,
    switchSlot: (...args: unknown[]) => switchSlotMock(...args),
    resumeFromHistory: (...args: unknown[]) => resumeFromHistoryMock(...args),
  }
})

vi.mock('../hooks/useTheme', async () => {
  const actual = await vi.importActual<typeof import('../hooks/useTheme')>('../hooks/useTheme')
  return actual
})

// Mock API client (sidebar fires fetch calls on mount). Use importOriginal so
// that non-`api` exports (e.g. SEARCH_MIN_CHARS constants used by sidebar
// search debounce) keep their real values.
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: Object.fromEntries(
      [
        'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
        'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
        'renameSlot', 'forkSession',
      ].map(k => [k, vi.fn().mockResolvedValue({})]),
    ),
  }
})

// Browser API stubs
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import { api } from '../api/client'
import { sseSlotTitle, sseSlots, sseDisconnected, sseConnected } from '../store/dashboardSlice'
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'
import { __resetRenameSlotStateForTests } from '../hooks/useRenameSlot'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

function renderSidebar(connected: boolean, opts: { withHistory?: boolean } = {}) {
  const slots = [slot('s1', 'Session 1'), slot('s2', 'Session 2')]
  const history = opts.withHistory ? [histItem('h1', 'History 1')] : []
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'s1'}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

beforeEach(() => {
  __resetRenameSlotStateForTests()
})

describe('ChatSidebar – the offline notice does not outlive the outage', () => {
  const titleOf = (label: string) =>
    screen.getByText(label).closest('[data-session-title]') as HTMLElement

  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
  })

  it('reconnecting retires a refusal that only applied while offline', async () => {
    const store = renderSidebar(false)
    fireEvent.doubleClick(titleOf('Session 2'))
    // Positive control: the refusal really is on screen before we reconnect, so
    // its later absence is the clear firing rather than it never having posted.
    expect(screen.getByTestId('title-action-error')).toBeInTheDocument()
    store.dispatch(sseConnected())
    await waitFor(() => expect(screen.queryByTestId('title-action-error')).not.toBeInTheDocument())
  })

  it('a refusal already on screen survives when the connection does not change', async () => {
    const store = renderSidebar(false)
    fireEvent.doubleClick(titleOf('Session 2'))
    expect(screen.getByTestId('title-action-error')).toBeInTheDocument()
    // Still offline after this dispatch, so nothing may clear it behind the user.
    store.dispatch(sseDisconnected())
    await waitFor(() => expect(screen.getByTestId('title-action-error')).toBeInTheDocument())
  })

  it('reconnecting does NOT retire a rejected rename\u2019s revert explanation', async () => {
    // Reconnecting neither re-sends the rename nor restores the typed name, so
    // the revert is still unexplained to the reader.
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('Request failed with status 500'))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = document.querySelector('[data-slot-key="s2"] textarea') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Rejected name' } })
    fireEvent.blur(box)
    const notice = await waitFor(() => screen.getByTestId('title-action-error'))
    expect(notice.textContent).toContain('The previous title was restored')
    // Each leg must COMMIT separately: batched, connected goes true->true, the
    // effect never re-runs, and this test passes whatever the clear does.
    store.dispatch(sseDisconnected())
    await waitFor(() => expect(store.getState().dashboard.connected).toBe(false))
    store.dispatch(sseConnected())
    await waitFor(() => expect(store.getState().dashboard.connected).toBe(true))
    expect(screen.getByTestId('title-action-error')).toBeInTheDocument()
    expect(screen.getByTestId('title-action-error').textContent).toContain('The previous title was restored')
  })

  it('claims no failed rename when the refusal came before any attempt', () => {
    renderSidebar(false)
    fireEvent.doubleClick(titleOf('Session 2'))
    const notice = screen.getByTestId('title-action-error')
    // Matches the folder refusal: no PATCH was sent, so naming a rename that
    // failed would be false, and the offline line is already a full sentence.
    expect(notice.textContent).toContain('Gateway offline')
    expect(notice.textContent).not.toContain("Couldn't rename")
  })

  it('still names the rename when the gateway took it and rejected it', async () => {
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('Request failed with status 500'))
    renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = document.querySelector('[data-slot-key="s2"] textarea') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Rejected name' } })
    fireEvent.blur(box)
    const notice = await waitFor(() => screen.getByTestId('title-action-error'))
    expect(notice.textContent).toContain("Couldn't rename")
  })
})

describe('ChatSidebar – offline guards', () => {
  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    switchSlotMock.mockClear()
    resumeFromHistoryMock.mockClear()
  })

  it('clicking a non-active session row when offline does NOT dispatch switchSlot', () => {
    renderSidebar(false)
    // The clickable element is the inner div with class "session-row" — the
    // outer motion.div with data-slot-key just wraps for animation/layout.
    const wrapper = screen.getByText('Session 2').closest('[data-slot-key]') as HTMLElement | null
    expect(wrapper).not.toBeNull()
    const row = wrapper!.querySelector('.session-row') as HTMLElement | null
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('clicking the ACTIVE session row when offline does NOT dispatch switchSlot', () => {
    // Without the guard, re-clicking the active row while offline dispatches
    // switchSlot, fetchSlotDetail fails with the gateway down, switchSlot.rejected
    // clears messages to [], and the ChatPage WelcomeView fallback kicks in
    // (activeSlot truthy + messages empty → "What can I do for you?"). The
    // "if (!connected && !isActive) return" guard covers the active row explicitly.
    renderSidebar(false)
    const wrapper = screen.getByText('Session 1').closest('[data-slot-key]') as HTMLElement | null
    expect(wrapper).not.toBeNull()
    const row = wrapper!.querySelector('.session-row') as HTMLElement | null
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('clicking a non-active session row when connected DOES dispatch switchSlot', () => {
    renderSidebar(true)
    const wrapper = screen.getByText('Session 2').closest('[data-slot-key]') as HTMLElement | null
    expect(wrapper).not.toBeNull()
    const row = wrapper!.querySelector('.session-row') as HTMLElement | null
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(switchSlotMock).toHaveBeenCalled()
  })

  it('mousedown on a history row when offline does NOT dispatch resumeFromHistory', () => {
    renderSidebar(false, { withHistory: true })
    // History section is collapsed by default — expand it first
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    const histRow = screen.getByText('History 1').closest('div') as HTMLElement | null
    expect(histRow).not.toBeNull()
    fireEvent.mouseDown(histRow!)
    expect(resumeFromHistoryMock).not.toHaveBeenCalled()
  })

  it('mousedown on a history row when connected DOES dispatch resumeFromHistory', () => {
    renderSidebar(true, { withHistory: true })
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    const histRow = screen.getByText('History 1').closest('div') as HTMLElement | null
    expect(histRow).not.toBeNull()
    fireEvent.mouseDown(histRow!)
    expect(resumeFromHistoryMock).toHaveBeenCalled()
  })

  it('offline session rows expose aria-disabled=true for screen readers', () => {
    // Beyond visual cursor-not-allowed + opacity-50, screen-reader users
    // need an explicit aria-disabled to know the rows aren't actionable
    // while the gateway is offline. Without it the row is announced as a
    // plain interactive element (no semantic disabled state).
    renderSidebar(false)
    // Active row
    const activeWrapper = screen.getByText('Session 1').closest('[data-slot-key]') as HTMLElement
    const activeRow = activeWrapper.querySelector('.session-row') as HTMLElement
    expect(activeRow.getAttribute('aria-disabled')).toBe('true')
    // Non-active row
    const otherWrapper = screen.getByText('Session 2').closest('[data-slot-key]') as HTMLElement
    const otherRow = otherWrapper.querySelector('.session-row') as HTMLElement
    expect(otherRow.getAttribute('aria-disabled')).toBe('true')
  })

  it('connected session rows expose aria-disabled=false', () => {
    renderSidebar(true)
    const activeWrapper = screen.getByText('Session 1').closest('[data-slot-key]') as HTMLElement
    const activeRow = activeWrapper.querySelector('.session-row') as HTMLElement
    expect(activeRow.getAttribute('aria-disabled')).toBe('false')
  })

  it('offline history rows expose aria-disabled=true for screen readers', () => {
    renderSidebar(false, { withHistory: true })
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    // The history row is the outermost flex container with the offline title.
    // closest('[aria-disabled]') walks up to the element that owns the attr.
    const histRow = screen.getByText('History 1').closest('[aria-disabled]') as HTMLElement
    expect(histRow).not.toBeNull()
    expect(histRow.getAttribute('aria-disabled')).toBe('true')
  })
})

/**
 * Rename is a separate sink from the row click and stayed reachable while the row
 * was aria-disabled. Each guard is paired with a connected control, so the
 * assertion is shown to be able to fail for the intended reason.
 */
describe('ChatSidebar – offline rename guards', () => {
  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    vi.mocked(api.renameSlot).mockClear()
  })

  const titleOf = (label: string) =>
    screen.getByText(label).closest('[data-session-title]') as HTMLElement

  // Scoped to the ROW: the sidebar's own search field is also a role=textbox, so
  // a bare getByRole('textbox') would pass the offline case for the wrong reason.
  const renameBoxOf = (key: string) =>
    document.querySelector(`[data-slot-key="${key}"] textarea`) as HTMLTextAreaElement | null

  it('double-clicking a session title when offline does NOT open the rename editor', () => {
    renderSidebar(false)
    const title = titleOf('Session 2')
    expect(title).not.toBeNull()
    fireEvent.doubleClick(title)
    expect(renameBoxOf('s2')).toBeNull()
  })

  it('double-clicking a session title when offline SAYS SO instead of doing nothing', () => {
    // A sighted mouse user never hovers for the aria-label, so the refusal has to
    // reach the same notice the commit half uses.
    renderSidebar(false)
    fireEvent.doubleClick(titleOf('Session 2'))
    expect(renameBoxOf('s2')).toBeNull()
    expect(screen.getByTestId('title-action-error')).toBeInTheDocument()
  })

  it('double-clicking a session title when connected DOES open the rename editor', () => {
    // Control: proves doubleClick on this element is what arms the editor.
    renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    expect(renameBoxOf('s2')).toBeInstanceOf(HTMLTextAreaElement)
  })

  it('a failed rename reverts the optimistic title and reports it', async () => {
    // Covers a CONNECTED rejection too, which a connectivity check never sees:
    // without a local revert the store keeps a title the gateway refused.
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('nope'))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    expect(box).not.toBeNull()
    fireEvent.change(box, { target: { value: 'Renamed but rejected' } })
    fireEvent.blur(box)
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Session 2')
    })
    expect(screen.getByTestId('title-action-error')).toBeInTheDocument()
  })

  it('shows translated text and never splices the attempted title into it', async () => {
    // Endpoint, status and code now travel as ErrorNotice's `report` prop, whose
    // doc says it skips the message-match lookup — so the message can be readable.
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('Request failed with status 500'))
    renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Rejected name' } })
    fireEvent.blur(box)
    const notice = await waitFor(() => screen.getByTestId('title-action-error'))
    expect(notice.textContent).not.toContain('Request failed with status 500')
    expect(notice.textContent).toContain('The previous title was restored')
  })

  it('a server ack that arrives before a lost response is NOT rolled back', async () => {
    // The endpoint pushes slot_title BEFORE it responds, so a dropped response
    // must not undo a title the server already acknowledged.
    let reject: (e: Error) => void = () => {}
    vi.mocked(api.renameSlot).mockImplementationOnce(() => new Promise((_r, rej) => { reject = rej }))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Acked name' } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalled())
    // The targeted ack carries the SAME title we painted, so a value comparison
    // cannot tell it apart — only the generation can.
    store.dispatch(sseSlotTitle({ key: 's2', title: 'Acked name' }))
    reject(new Error('response lost'))
    await waitFor(() => expect(screen.getByTestId('title-action-error')).toBeInTheDocument())
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Acked name')
  })

  it('an authoritative slots frame outranks a later rollback', async () => {
    // The lost-response case: the PATCH persisted, so once the server's own frame
    // lands its title wins and the rollback must stand down.
    let reject: (e: Error) => void = () => {}
    vi.mocked(api.renameSlot).mockImplementationOnce(() => new Promise((_r, rej) => { reject = rej }))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Persisted name' } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalled())
    store.dispatch(sseSlots(store.getState().dashboard.slots.map(s => (
      s.key === 's2' ? { ...s, title: 'Persisted name' } : s
    ))))
    reject(new Error('response lost'))
    await waitFor(() => expect(screen.getByTestId('title-action-error')).toBeInTheDocument())
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Persisted name')
  })

  it('a rollback after a SETTLED rename does not resurrect a pre-update title', async () => {
    // The cache must not outlive its batch: a title that arrives after the first
    // rename settles is the live one the next failure has to fall back to.
    vi.mocked(api.renameSlot).mockResolvedValueOnce({} as never)
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const first = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(first, { target: { value: 'Accepted name' } })
    fireEvent.blur(first)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledTimes(1))
    store.dispatch(sseSlotTitle({ key: 's2', title: 'Authoritative later' }))
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('nope'))
    fireEvent.doubleClick(titleOf('Session 2'))
    const second = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(second, { target: { value: 'Rejected name' } })
    fireEvent.blur(second)
    await waitFor(() => expect(screen.getByTestId('title-action-error')).toBeInTheDocument())
    const title = store.getState().dashboard.slots.find(s => s.key === 's2')?.title
    expect(title).not.toBe('Accepted name')
    expect(title).toBe('Authoritative later')
  })

  it('an offline session title keeps its name and does NOT repeat the offline suffix', () => {
    // The row carries aria-disabled, so a suffix here would repeat once per
    // session — and on a node that is not interactive.
    renderSidebar(false)
    const title = titleOf('Session 2')
    expect(title.getAttribute('aria-disabled')).toBe('true')
    expect(title.getAttribute('title')).toBe('Session 2')
    expect(title.getAttribute('aria-label')).toBeNull()
  })

  it('a connected session title keeps its own tooltip and is not disabled', () => {
    renderSidebar(true)
    const title = titleOf('Session 2')
    expect(title.getAttribute('aria-disabled')).toBe('false')
    expect(title.getAttribute('title')).toBe('Session 2')
    expect(title.getAttribute('aria-label')).toBeNull()
  })

  it('a successful rename shows the SERVER-normalized title, not the one typed', async () => {
    // The endpoint truncates to 200 chars, so a lost title event would otherwise
    // leave an over-length value on screen that was never stored.
    const typed = 'x'.repeat(250)
    const stored = typed.slice(0, 200)
    vi.mocked(api.renameSlot).mockResolvedValueOnce({ ok: true, title: stored } as never)
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: typed } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledWith('s2', typed, expect.any(AbortSignal)))
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe(stored)
    })
  })

  it('a rename the server did NOT normalize is left exactly as typed', async () => {
    vi.mocked(api.renameSlot).mockResolvedValueOnce({ ok: true, title: 'Short name' } as never)
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Short name' } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalled())
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Short name')
    })
  })

  it('a delayed ack does NOT overwrite a newer authoritative slots frame', async () => {
    // A full frame advances slotsGeneration alone, so a guard watching only the
    // title generation lets the delayed response repaint over that frame.
    let resolve: (v: unknown) => void = () => {}
    vi.mocked(api.renameSlot).mockImplementationOnce(() => new Promise(res => { resolve = res }))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Typed name' } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalled())
    store.dispatch(sseSlots(store.getState().dashboard.slots.map(s => (
      s.key === 's2' ? { ...s, title: 'Frame name' } : s
    ))))
    resolve({ ok: true, title: 'Typed name' })
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledTimes(1))
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Frame name')
  })

  it('a late response does NOT overwrite an authoritative frame carrying the prior title', async () => {
    // A second client can rename this slot BACK to the title we replaced, so a
    // frame carrying it may be newer than our response rather than stale.
    let resolve: (v: unknown) => void = () => {}
    vi.mocked(api.renameSlot).mockImplementationOnce(() => new Promise(res => { resolve = res }))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Typed name' } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalled())
    // Positive control: the optimistic paint really did land, so the assertion
    // below is the frame winning rather than the rename never having painted.
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Typed name')
    store.dispatch(sseSlots(store.getState().dashboard.slots.map(s => (
      s.key === 's2' ? { ...s, title: 'Session 2' } : s
    ))))
    resolve({ ok: true, title: 'Typed name' })
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledTimes(1))
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Session 2')
  })

  it('a second rename of one slot is refused while the first is still in flight', async () => {
    // Two PATCHes of the same field can be applied by the server in either order,
    // so the second never reaches the wire while the first is open.
    vi.mocked(api.renameSlot).mockImplementation(() => new Promise(() => {}))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const first = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(first, { target: { value: 'Name A' } })
    fireEvent.blur(first)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledTimes(1))

    const row = document.querySelector('[data-slot-key="s2"] [data-session-title]') as HTMLElement
    fireEvent.doubleClick(row)
    const second = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(second, { target: { value: 'Name B' } })
    fireEvent.blur(second)

    await waitFor(() => expect(screen.getByTestId('title-action-error')).toBeInTheDocument())
    expect(api.renameSlot).toHaveBeenCalledTimes(1)
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Name A')
  })

  it('a refused concurrent rename KEEPS the editor open with the draft intact', async () => {
    // Refusing is only safe if the caller keeps the draft: closing the editor on a
    // refusal discards the typed title, which is the loss this PR exists to stop.
    vi.mocked(api.renameSlot).mockImplementation(() => new Promise(() => {}))
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const first = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(first, { target: { value: 'Name A' } })
    fireEvent.blur(first)
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledTimes(1))

    const row = document.querySelector('[data-slot-key="s2"] [data-session-title]') as HTMLElement
    fireEvent.doubleClick(row)
    const second = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(second, { target: { value: 'Name B' } })
    fireEvent.blur(second)

    await waitFor(() => expect(screen.getByTestId('title-action-error')).toBeInTheDocument())
    const surviving = renameBoxOf('s2') as HTMLTextAreaElement | null
    expect(surviving).not.toBeNull()
    expect(surviving?.value).toBe('Name B')
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Name A')
  })

  it('committing while disconnected KEEPS the editor open with the draft intact', () => {
    // A mid-edit drop must not cost the user their typed title: the refusal is
    // reported and the editor is left standing so the draft can be retried.
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Typed while dropping' } })
    store.dispatch(sseDisconnected())
    fireEvent.blur(box)
    expect(api.renameSlot).not.toHaveBeenCalled()
    const still = renameBoxOf('s2') as HTMLTextAreaElement
    expect(still).not.toBeNull()
    expect(still.value).toBe('Typed while dropping')
  })

  it('committing while disconnected does not attempt the write, and says so', () => {
    // The commit half of the guard the double-click entry already applies.
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Typed while dropping' } })
    store.dispatch(sseDisconnected())
    fireEvent.blur(box)
    expect(api.renameSlot).not.toHaveBeenCalled()
    expect(screen.getByTestId('title-action-error')).toBeInTheDocument()
  })

  it('paints the new title optimistically and reaches renameSlot', async () => {
    // Control for the revert above: the paint has to happen for a revert to mean
    // anything.
    const store = renderSidebar(true)
    fireEvent.doubleClick(titleOf('Session 2'))
    const box = renameBoxOf('s2') as HTMLTextAreaElement
    expect(box).not.toBeNull()
    fireEvent.change(box, { target: { value: 'Renamed while online' } })
    fireEvent.blur(box)
    expect(store.getState().dashboard.slots.find(s => s.key === 's2')?.title).toBe('Renamed while online')
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledWith('s2', 'Renamed while online', expect.any(AbortSignal)))
  })
})
