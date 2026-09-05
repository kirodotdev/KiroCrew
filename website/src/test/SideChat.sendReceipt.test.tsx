/**
 * Receipt-policy tests for SideChat's send path under the chat-core transport.
 *
 * The side panel's `/side/open` + `/side/turn` calls have the OPPOSITE receipt
 * semantics from `POST /api/chat?ws=1` (they resolve JSON on 2xx and reject
 * with an `ApiError` on non-2xx). `sideTurnWire` re-expresses them in the fetch
 * seam's shape so `sendTurn` classifies them by the shared rule; these tests
 * pin how SideChat REACTS to each status:
 *
 * - refused (an `ApiError`)      -> optimistic bubble rolled back, text handed
 *                                  back merged, the server's own reason shown.
 * - transport-error              -> same recovery, connection-framed copy.
 * - response-late, no bubble     -> text handed back under an "unconfirmed"
 *   (steer / queue)                 notice (the composer already cleared and no
 *                                  bubble holds a copy).
 * - response-late, with bubble   -> nothing: the bubble is the visible copy;
 *                                  restoring would invite a duplicate.
 * - unknown (unreadable 2xx)     -> nothing.
 * - dispatched / queued          -> unchanged acceptance handling (covered by
 *                                  SideChat.steerQueue.test.tsx, which passes
 *                                  unmodified -- the compatibility evidence).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'
import { ApiError, AcceptedBodyUnreadable } from '../api/apiError'
import { SEND_ABORT_MS } from '../chat-core/transport/sendTurn'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideQueueCancel: vi.fn().mockResolvedValue({ ok: true, content: '', depth: 0 }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'

const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }
const SLOT = 'test-slot-1'
const initial = reducer(undefined, { type: '@@INIT' })

function sideState(streaming: boolean) {
  return createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: streaming ? 'partial' : 'done', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
        },
      },
    },
  })
}

const box = () => screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
const sideMessages = (store: ReturnType<typeof createTestStore>) => store.getState().chat.slotSide[SLOT]?.messages ?? []

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SideChat send receipt policy', () => {
  it("a refused send (ApiError) rolls the bubble back, hands the text back, and shows the server's reason", async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideTurn).mockRejectedValueOnce(new ApiError(429, 'side queue is full (max 20)', ''))
    const store = sideState(false)
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await user.type(box(), 'too many')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(box()).toHaveValue('too many'))
    // Framed as a send failure, rendered through the shared ErrorNotice (role=alert),
    // not a bare red div: a raw reason reads as the agent erroring mid-work.
    expect(screen.getByRole('alert')).toHaveTextContent('Send failed: side queue is full (max 20)')
    // The optimistic bubble (idle side) is gone again.
    expect(sideMessages(store).filter(m => m.content === 'too many')).toHaveLength(0)
  })

  it('a transport failure hands the text back with connection-framed copy', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideTurn).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    renderWithProviders(<SideChat slot={SLOT} />, { store: sideState(false) })
    await user.type(box(), 'offline')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(box()).toHaveValue('offline'))
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't send — check your connection and try again.")
  })

  it('a refusal from /side/open is a refusal too (the two-call sequence is one send)', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideOpen).mockRejectedValueOnce(new ApiError(404, 'not found', ''))
    renderWithProviders(<SideChat slot={SLOT} />, { store: sideState(false) })
    await user.type(box(), 'hello')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(box()).toHaveValue('hello'))
    expect(screen.getByRole('alert')).toHaveTextContent('Send failed: not found')
    expect(api.sideTurn).not.toHaveBeenCalled()
  })

  it('a late response on a STEER (no bubble) hands the text back under an unconfirmed notice', async () => {
    vi.useFakeTimers()
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideTurn>)
    renderWithProviders(<SideChat slot={SLOT} />, { store: sideState(true) })
    await act(async () => { fireEvent.change(box(), { target: { value: 'steer me' } }) })
    await act(async () => { fireEvent.click(screen.getByTestId('busy-send-button')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'steer me', { steer: true })
    expect(box()).toHaveValue('')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(box()).toHaveValue('steer me')
    expect(screen.getByText(/^Delivery not confirmed/)).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't send/)).toBeNull()
    // A STANDING notice: it describes restored text, so it must outlive the
    // transient-notice TTL and hold until the next submit.
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(screen.getByText(/^Delivery not confirmed/)).toBeInTheDocument()
  })

  /** A settled answer with chips while a steer is still pending. */
  const chipState = () => createTestStore({
      dashboard: dashInitial,
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: {
            messages: [
              { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
              { role: 'assistant' as const, content: 'Next? [OPTIONS: Run tests | Skip]', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
            ],
            // pending (a steer awaiting consumption) but not streaming: the
            // answer has settled so the chips render, and the side is busy so
            // a chip send is NOT optimistic -- the one shape that reaches the
            // override branch of the response-late policy.
            lastRunId: 'r1', pending: true, streaming: false, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z',
          },
        },
      },
    })

  it('a late response on a CHIP send says to re-pick the option and leaves the draft alone', async () => {
    vi.useFakeTimers()
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideTurn>)
    renderWithProviders(<SideChat slot={SLOT} />, { store: chipState() })
    await act(async () => { fireEvent.change(box(), { target: { value: 'my own draft' } }) })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send now: Run tests')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'Run tests', undefined)
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(box()).toHaveValue('my own draft')
    expect(screen.getByText(/re-pick the option/)).toBeInTheDocument()
    expect(screen.queryByText(/back in the composer/)).toBeNull()
  })

  it('a late CHIP send with an EMPTY composer keeps its notice (the empty-draft retirement is for restored text only)', async () => {
    // The common chip case: nothing typed. The notice restored nothing, so an
    // empty composer says nothing about whether the user has seen it.
    vi.useFakeTimers()
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideTurn>)
    renderWithProviders(<SideChat slot={SLOT} />, { store: chipState() })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send now: Run tests')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 100) })
    expect(box()).toHaveValue('')
    expect(screen.getByText(/re-pick the option/)).toBeInTheDocument()
  })

  it('a deadline that fires during /side/open is a FAILURE (nothing accepted): restore + error, never dispatch the turn', async () => {
    // Not `response-late`: the turn request was never sent, so nothing is
    // indeterminate. Classifying it as unconfirmed would strand an idle send's
    // optimistic bubble for a turn that will never run.
    vi.useFakeTimers()
    vi.mocked(api.sideOpen).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideOpen>)
    const store = sideState(false)
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await act(async () => { fireEvent.change(box(), { target: { value: 'stalled open' } }) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(sideMessages(store).some(m => m.content === 'stalled open')).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(box()).toHaveValue('stalled open')
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't send")
    expect(screen.queryByText(/Delivery not confirmed/)).toBeNull()
    expect(sideMessages(store).some(m => m.content === 'stalled open')).toBe(false)
    expect(api.sideTurn).not.toHaveBeenCalled()
  })

  it('the standing unconfirmed notice retires when the user empties the restored draft', async () => {
    vi.useFakeTimers()
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideTurn>)
    renderWithProviders(<SideChat slot={SLOT} />, { store: sideState(true) })
    await act(async () => { fireEvent.change(box(), { target: { value: 'steer me' } }) })
    await act(async () => { fireEvent.click(screen.getByTestId('busy-send-button')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 100) })
    expect(screen.getByText(/^Delivery not confirmed/)).toBeInTheDocument()
    await act(async () => { fireEvent.change(box(), { target: { value: '' } }) })
    expect(screen.queryByText(/Delivery not confirmed/)).toBeNull()
  })

  it('an accepted /side/turn whose body is unreadable is UNKNOWN: no error, no restore, no duplicate invitation', async () => {
    const user = userEvent.setup()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    // A stream cut mid-body is a `TypeError` -- the same class `fetch` throws
    // for a request that never left. The client method tags it by phase.
    vi.mocked(api.sideTurn).mockRejectedValueOnce(new AcceptedBodyUnreadable(new TypeError('network error')))
    const store = sideState(false)
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await user.type(box(), 'accepted but mangled')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalled())
    await act(async () => { await new Promise(r => setTimeout(r, 30)) })
    expect(box()).toHaveValue('')
    expect(screen.queryByRole('alert')).toBeNull()
    // The optimistic bubble stays: the server took the question.
    expect(sideMessages(store).some(m => m.content === 'accepted but mangled')).toBe(true)
  })

  it('a receipt that lands after the user switched slots is parked for the ORIGINATING slot, not merged into the other slot\'s draft', async () => {
    // The panel is one instance re-propped across slots. Slot A's refused
    // question must not land in the draft being written for slot B -- and must
    // not be lost either: it is handed back when the user returns to A.
    vi.useFakeTimers()
    const OTHER = 'other-slot'
    let rejectTurn: (e: unknown) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise((_, rej) => { rejectTurn = rej }) as ReturnType<typeof api.sideTurn>)
    const store = createTestStore({
      dashboard: dashInitial,
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: { messages: [], lastRunId: null, pending: false, streaming: false, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z' },
          [OTHER]: { messages: [], lastRunId: null, pending: false, streaming: false, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z' },
        },
      },
    })
    const { rerender } = renderWithProviders(<SideChat slot={SLOT} />, { store })
    await act(async () => { fireEvent.change(box(), { target: { value: 'question for A' } }) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'question for A', undefined)
    // Switch to B and start a draft there; then A's send is refused.
    rerender(<SideChat slot={OTHER} />)
    expect(screen.queryByRole('status')).toBeNull()
    await act(async () => { fireEvent.change(box(), { target: { value: 'draft for B' } }) })
    await act(async () => { rejectTurn(new ApiError(409, 'side turn already in flight')); await vi.advanceTimersByTimeAsync(20) })
    expect(box()).toHaveValue('draft for B')
    expect(screen.queryByRole('alert')).toBeNull()
    // Back to A: the question and its error are handed back there.
    rerender(<SideChat slot={SLOT} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(box().value).toContain('question for A')
    expect(screen.getByRole('alert')).toHaveTextContent('side turn already in flight')
  })

  it('a slot\'s send status does not follow the user to another slot, and is back on return', async () => {
    vi.useFakeTimers()
    const OTHER = 'other-slot'
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideTurn>)
    const store = createTestStore({
      dashboard: dashInitial,
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: { messages: [], lastRunId: 'r1', pending: true, streaming: true, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z' },
          [OTHER]: { messages: [], lastRunId: null, pending: false, streaming: false, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z' },
        },
      },
    })
    const { rerender } = renderWithProviders(<SideChat slot={SLOT} />, { store })
    await act(async () => { fireEvent.change(box(), { target: { value: 'steer A' } }) })
    await act(async () => { fireEvent.click(screen.getByTestId('busy-send-button')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 100) })
    expect(screen.getByText(/^Delivery not confirmed/)).toBeInTheDocument()
    rerender(<SideChat slot={OTHER} />)
    expect(screen.queryByText(/Delivery not confirmed/)).toBeNull()
    rerender(<SideChat slot={SLOT} />)
    expect(screen.getByText(/^Delivery not confirmed/)).toBeInTheDocument()
  })

  it('a refusal that lands after the panel was UNMOUNTED still hands the text back when the slot is next shown', async () => {
    // The text rides the store (`sideHandBackText`), not a component ref, so
    // closing the side panel between the send and its receipt loses nothing.
    vi.useFakeTimers()
    let rejectTurn: (e: unknown) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise((_, rej) => { rejectTurn = rej }) as ReturnType<typeof api.sideTurn>)
    const store = sideState(false)
    const { unmount } = renderWithProviders(<SideChat slot={SLOT} />, { store })
    await act(async () => { fireEvent.change(box(), { target: { value: 'orphaned question' } }) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    unmount()
    await act(async () => { rejectTurn(new ApiError(409, 'side turn already in flight')); await vi.advanceTimersByTimeAsync(20) })
    // The store holds the text for SLOT -- and the status that explains it, so
    // the next panel showing SLOT drains the text into its composer WITH the
    // failure line rather than an unexplained restored question.
    expect(store.getState().chat.slotSide[SLOT]?.releasedText).toBe('orphaned question')
    expect(store.getState().chat.slotSide[SLOT]?.sendStatus?.error).toContain('side turn already in flight')
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(box().value).toContain('orphaned question')
    expect(screen.getByRole('alert')).toHaveTextContent('side turn already in flight')
    expect(store.getState().chat.slotSide[SLOT]?.messages.some(m => m.content === 'orphaned question')).toBe(false)
  })

  it('a late response on an IDLE send (bubble on screen) leaves the bubble and the composer alone', async () => {
    vi.useFakeTimers()
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise(() => {}) as ReturnType<typeof api.sideTurn>)
    const store = sideState(false)
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await act(async () => { fireEvent.change(box(), { target: { value: 'slow one' } }) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(api.sideTurn).toHaveBeenCalled()
    expect(sideMessages(store).some(m => m.content === 'slow one')).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(box()).toHaveValue('')
    expect(sideMessages(store).some(m => m.content === 'slow one')).toBe(true)
    expect(screen.queryByText(/Delivery not confirmed/)).toBeNull()
  })
})
