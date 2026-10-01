import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../../api/apiError'

const mockSummary = vi.fn()
const mockOpen = vi.fn()
const mockClose = vi.fn()

vi.mock('../../api/threads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/threads')>()
  return {
    ...actual,
    threadsApi: {
      summary: (...a: unknown[]) => mockSummary(...a),
      detail: vi.fn(),
      open: (...a: unknown[]) => mockOpen(...a),
      close: (...a: unknown[]) => mockClose(...a),
    },
  }
})

import { threadOpenErrorKey, useThreads } from './useThreads'

const LIVE = {
  kind: 'session' as const,
  thread_slot: 'chat-77-1758524400',
  title: 'The other eight',
  opened_by: 'user',
  opened_at: '2026-09-22T07:40:00Z',
  closed_at: null,
  summary_mid: null,
}
const LEGACY = {
  kind: 'legacy' as const,
  count: 3,
  last_reply_ts: '2026-09-22T07:41:40Z',
  participants: ['user', 'assistant'],
}

let qc: QueryClient
const mount = (slot: string | undefined, enabled = true) =>
  renderHook(() => useThreads(slot, { enabled, crewmateName: 'Radar' }), {
    wrapper: ({ children }) => React.createElement(QueryClientProvider, { client: qc }, children),
  })

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mockSummary.mockResolvedValue({ threads: { 'm-1': LIVE, 'm-2': LEGACY } })
  mockOpen.mockResolvedValue({
    thread_slot: 'chat-90-1758525000',
    anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-3' },
    title: 'Thread: something',
    seeded: true,
  })
  mockClose.mockResolvedValue({
    thread_slot: 'chat-77-1758524400',
    anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-1' },
    summary_mid: 'm-card000000000001',
  })
})

describe('useThreads', () => {
  it('opens an existing thread with no request at all', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    expect(result.current.open).toEqual({ mid: 'm-1', threadSlot: 'chat-77-1758524400' })
    expect(mockOpen).not.toHaveBeenCalled()
  })

  it('shows a version 1 thread as a fold, and starting a real one then opens', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-2')).toBeTruthy())
    act(() => result.current.openThread('m-2'))
    // The fold: no slot, because minting a session for those replies would
    // assert a history it never had.
    expect(result.current.open).toEqual({ mid: 'm-2' })
    expect(mockOpen).not.toHaveBeenCalled()
    // The fold's own action comes back through the same call. With the fold
    // already open it falls through and asks for a real thread.
    mockOpen.mockResolvedValueOnce({
      thread_slot: 'chat-91-1758525111',
      anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-2' },
      title: 'Thread',
      seeded: true,
    })
    await act(async () => { result.current.openThread('m-2') })
    await waitFor(() => expect(result.current.open).toEqual({ mid: 'm-2', threadSlot: 'chat-91-1758525111' }))
  })

  it('a streaming row sends the inflight sentinel, not an invented mid', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread(undefined) })
    // A streaming reply has no mid: ids are minted post-turn. The backend
    // resolves the anchor to the message that started the turn and answers with
    // it, which is the mid the drawer then hangs on.
    expect(mockOpen).toHaveBeenCalledWith('member-radar', 'inflight', undefined)
    await waitFor(() => expect(result.current.open).toEqual({ mid: 'm-3', threadSlot: 'chat-90-1758525000' }))
    expect(result.current.openError).toBe('')
  })

  it('opens a thread on a message that has none yet', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-3') })
    expect(mockOpen).toHaveBeenCalledWith('member-radar', 'm-3', undefined)
    await waitFor(() => expect(result.current.open?.threadSlot).toBe('chat-90-1758525000'))
  })

  it('a CLOSED thread on a message mints a replacement rather than reopening the ended one', async () => {
    // Closing releases the message, so **Reply in thread** on it is a click on a
    // free message: it starts a new thread. Reading the ended one is the footer's
    // job, and asks with `read`.
    mockSummary.mockResolvedValue({
      threads: { 'm-1': { ...LIVE, closed_at: '2026-09-22T08:00:00Z', summary_mid: 'm-card' } },
    })
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    await act(async () => { result.current.openThread('m-1') })
    expect(mockOpen).toHaveBeenCalledWith('member-radar', 'm-1', undefined)
    await waitFor(() => expect(result.current.open?.threadSlot).toBe('chat-90-1758525000'))
    expect(result.current.open?.threadSlot).not.toBe(LIVE.thread_slot)
  })

  it('an ENDED thread asked for by READ opens to be read, and mints nothing', async () => {
    // The footer says "Thread -- Ended" and the close card names that thread, so
    // pressing either means "let me read it". Minting a replacement there answers
    // a question nobody asked and hides the conversation the reader pointed at.
    mockSummary.mockResolvedValue({
      threads: { 'm-1': { ...LIVE, closed_at: '2026-09-22T08:00:00Z', summary_mid: 'm-card' } },
    })
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    await act(async () => { result.current.openThread('m-1', { read: true }) })
    expect(mockOpen).not.toHaveBeenCalled()
    expect(result.current.open).toEqual({ mid: 'm-1', threadSlot: LIVE.thread_slot })
  })

  it('the close card opens its thread by SLOT, and says so when no anchor claims it', async () => {
    // The card records the thread's slot, which is what makes it a back-link; the
    // drawer is keyed by the anchored mid. One anchor holds any one slot, so the
    // reverse lookup is exact.
    //
    // A miss is ordinary, not broken: closing frees the message, so a later thread
    // on it takes the anchor over and every older card's slot then matches nothing.
    // The answer is false, which is the host's cue to open the session as a full
    // page -- the thread is still there.
    mockSummary.mockResolvedValue({
      threads: { 'm-1': { ...LIVE, closed_at: '2026-09-22T08:00:00Z', summary_mid: 'm-card' } },
    })
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    let took = true
    act(() => { took = result.current.openThreadSlot('chat-superseded-1758000000') })
    expect(took).toBe(false)
    expect(result.current.open).toBeNull()
    act(() => { took = result.current.openThreadSlot(LIVE.thread_slot) })
    expect(took).toBe(true)
    expect(result.current.open).toEqual({ mid: 'm-1', threadSlot: LIVE.thread_slot })
    expect(mockOpen).not.toHaveBeenCalled()
  })

  it('an OPEN thread on a message is shown with no request at all (control)', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    expect(mockOpen).not.toHaveBeenCalled()
    expect(result.current.open).toEqual({ mid: 'm-1', threadSlot: LIVE.thread_slot })
  })

  it('a slow first open cannot replace the thread a second click already installed', async () => {
    // Both opens are on the SAME chat, so a slot check passes for both and the
    // slower answer would install the older thread while the reader is already
    // typing into the newer one -- their next message into the wrong transcript.
    const settle: Array<(v: unknown) => void> = []
    mockOpen.mockImplementation(() => new Promise((res) => { settle.push(res) }))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-3') })
    await act(async () => { result.current.openThread('m-4') })
    expect(settle).toHaveLength(2)
    const answer = (mid: string, slot: string) => ({
      thread_slot: slot,
      anchor: { surface: 'dashboard', conversation: 'member-radar', mid },
      title: 'Thread: something',
      seeded: true,
    })
    // The SECOND request answers first, then the overtaken first one arrives.
    await act(async () => { settle[1](answer('m-4', 'chat-92-1758525200')) })
    await waitFor(() => expect(result.current.open?.mid).toBe('m-4'))
    await act(async () => { settle[0](answer('m-3', 'chat-90-1758525000')) })
    expect(result.current.open).toEqual({ mid: 'm-4', threadSlot: 'chat-92-1758525200' })
  })

  it('an overtaken open leaves the surface busy while the live mint is still out', async () => {
    // `opening` belongs to the newest request. Cleared by a stale one, the
    // composer would report ready while a mint is still in flight.
    const settle: Array<(v: unknown) => void> = []
    mockOpen.mockImplementation(() => new Promise((res) => { settle.push(res) }))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-3') })
    await act(async () => { result.current.openThread('m-4') })
    await act(async () => { settle[0]({
      thread_slot: 'chat-90-1758525000',
      anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-3' },
      title: 'Thread: something',
      seeded: true,
    }) })
    expect(result.current.opening).toBe(true)
    expect(result.current.open).toBeNull()
  })

  it('already_open is read as the answer it is, not as a failure', async () => {
    // Another tab, or an agent, opened this thread between the anchor read and
    // the click. The refusal names the slot, which is exactly what was asked
    // for -- reporting a problem here would be reporting one the user has none of.
    mockOpen.mockRejectedValue(new ApiError(409, 'x', JSON.stringify({
      error: 'That thread is already open.', code: 'already_open', thread_slot: 'chat-55-1758520000',
    })))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-9') })
    await waitFor(() => expect(result.current.open).toEqual({ mid: 'm-9', threadSlot: 'chat-55-1758520000' }))
    expect(result.current.openError).toBe('')
  })

  it('a refused open says so and opens nothing', async () => {
    mockOpen.mockRejectedValue(new ApiError(404, 'x', JSON.stringify({ error: 'gone', code: 'parent_not_found' })))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-9') })
    await waitFor(() => expect(result.current.openError).toBe('parent_not_found'))
    expect(result.current.open).toBeNull()
    expect(result.current.opening).toBe(false)
  })

  it('reports a failed anchor read without hiding the chat, and retries in place', async () => {
    mockSummary.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryFailed).toBe(true))
    // The hooks stay: a missing footer is a missing footer, not a chat with no
    // way to start a thread.
    expect(result.current.hooks).toBeTruthy()
    await act(async () => { result.current.retrySummary() })
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
  })

  it('offers nothing without a slot, or when the host turns it off', async () => {
    const off = mount('member-radar', false)
    expect(off.result.current.hooks).toBeUndefined()
    const none = mount(undefined)
    expect(none.result.current.hooks).toBeUndefined()
    // Neither reads the anchor index: there is nothing to read it for.
    expect(mockSummary).not.toHaveBeenCalled()
  })

  it('close forgets the thread and the failure with it', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    expect(result.current.open).not.toBeNull()
    act(() => result.current.close())
    expect(result.current.open).toBeNull()
    expect(result.current.openError).toBe('')
  })
})


describe('threadOpenErrorKey', () => {
  it('says "try again" for the refusals that mean not-yet', () => {
    // Read off the route's own admission table (`_ADMISSION_REFUSALS` in
    // chat_threads.py) plus the one code it surfaces verbatim from
    // `create_session`. Measured on a live gateway: opening on the reply being
    // written during a fresh chat's first turn hits `transcript_missing`.
    for (const code of ['transcript_missing', 'thread_open_failed', 'caller_memory_changed']) {
      expect(threadOpenErrorKey(code)).toBe('pages.chat.thread.err_open_not_ready')
    }
  })

  it('says it plainly for the refusals that will not clear on their own', () => {
    for (const code of ['parent_not_found', 'transcript_replaced', 'threads_full', 'invalid_mid', 'surface_unsupported']) {
      expect(threadOpenErrorKey(code)).toBe('pages.chat.thread.err_open_failed')
    }
  })

  it('has no sentence when nothing failed', () => {
    expect(threadOpenErrorKey('')).toBe('')
  })

  it('does not carry a store outcome that never reaches the wire', () => {
    // `unflushed` is one of the two store outcomes the route folds into
    // `transcript_missing`; naming it here would assert a wire code that does
    // not exist, so it is deliberately absent rather than harmlessly included.
    expect(threadOpenErrorKey('unflushed')).toBe('pages.chat.thread.err_open_failed')
  })
})


describe('useThreads endThread', () => {
  it('calls the close route and then dismisses the drawer', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    expect(result.current.open?.threadSlot).toBe('chat-77-1758524400')
    await act(async () => { result.current.endThread('m-1') })
    await waitFor(() => expect(result.current.open).toBeNull())
    // The thread ON SCREEN is named, not just the message: the mid says which
    // message and a message can carry a succession of threads.
    expect(mockClose).toHaveBeenCalledWith('member-radar', 'm-1', LIVE.thread_slot)
    expect(result.current.endError).toBe('')
  })

  it('a late close does not dismiss a drawer that opened after it', async () => {
    // The close is a round trip and the reader can open another thread inside it.
    // Dismissing on the answer alone would dismiss whatever is open when it lands.
    let settle: ((v: unknown) => void) | undefined
    mockClose.mockImplementation(() => new Promise((res) => { settle = res }))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    await waitFor(() => expect(result.current.open?.threadSlot).toBe(LIVE.thread_slot))
    await act(async () => { result.current.endThread('m-1') })
    // A different thread takes the drawer while the close is still out.
    await act(async () => { result.current.openThread('m-3') })
    await waitFor(() => expect(result.current.open?.threadSlot).toBe('chat-90-1758525000'))
    await act(async () => { settle?.({ thread_slot: LIVE.thread_slot, anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-1' }, summary_mid: 'm-card' }) })
    expect(result.current.open?.threadSlot).toBe('chat-90-1758525000')
  })

  it('a refused end keeps the thread open and reports it', async () => {
    mockClose.mockRejectedValue(new ApiError(503, 'unavailable', '{"code":"threads_unavailable"}'))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    await act(async () => { result.current.endThread('m-1') })
    await waitFor(() => expect(result.current.endError).toBe('pages.chat.thread.err_end_failed'))
    // Still on screen: hiding a thread whose close was refused would report an
    // outcome that did not happen.
    expect(result.current.open?.mid).toBe('m-1')
  })

  it('already_closed is the outcome the user asked for, not a failure', async () => {
    mockClose.mockRejectedValue(new ApiError(409, 'conflict', '{"code":"already_closed"}'))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    await act(async () => { result.current.endThread('m-1') })
    await waitFor(() => expect(result.current.open).toBeNull())
    expect(result.current.endError).toBe('')
  })

  it('dismissing the drawer reaches no thread', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    act(() => result.current.close())
    expect(result.current.open).toBeNull()
    expect(mockClose).not.toHaveBeenCalled()
  })
})


describe('useThreads stale opens', () => {
  it('drops an open that resolves after the surface moved to another chat', async () => {
    // A mint is a round trip and the reader can switch chats inside it. Applying
    // the answer blind installs chat A's thread as the controller's open drawer,
    // and the next message typed there goes into A's transcript.
    let settle: ((v: unknown) => void) | undefined
    mockOpen.mockImplementation(() => new Promise((res) => { settle = res }))
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useThreads(slot, { enabled: true, crewmateName: 'Radar' }),
      {
        initialProps: { slot: 'member-radar' },
        wrapper: ({ children }) => React.createElement(QueryClientProvider, { client: qc }, children),
      },
    )
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-3'))
    expect(result.current.opening).toBe(true)
    // The reader leaves for another chat while the mint is still in flight.
    rerender({ slot: 'chat-other' })
    await act(async () => {
      settle?.({
        thread_slot: 'chat-90-1758525000',
        anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-3' },
        title: 'Thread: something',
        seeded: true,
      })
      await Promise.resolve()
    })
    expect(result.current.open).toBeNull()
  })
})

/**
 * Everything this controller holds belongs to ONE parent slot.
 *
 * Both hosts do clear it on a switch -- the chat page through the `?thread=` the
 * session switch deletes, the Crewmates page on `confirmedSlot` -- but that makes
 * the invariant something each host has to remember on the controller's behalf. A
 * host that forgets leaves a drawer open over the wrong conversation, with a
 * composer still writing into the thread it came from. The controller drops it
 * itself, where its own key is what moved.
 */
describe('useThreads across a parent switch', () => {
  const mountWith = (slot: string) =>
    renderHook(({ s }: { s: string }) => useThreads(s, { enabled: true, crewmateName: 'Radar' }), {
      initialProps: { s: slot },
      wrapper: ({ children }) => React.createElement(QueryClientProvider, { client: qc }, children),
    })

  it('drops an open thread when the parent slot changes', async () => {
    const { result, rerender } = mountWith('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    expect(result.current.open).toEqual({ mid: 'm-1', threadSlot: LIVE.thread_slot })

    await act(async () => { rerender({ s: 'member-other' }) })
    expect(result.current.open).toBeNull()
  })

  it('drops a failed open and a failed end with it, so the next parent starts clean', async () => {
    mockOpen.mockRejectedValue(new ApiError(500, 'boom', ''))
    const { result, rerender } = mountWith('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-3') })
    await waitFor(() => expect(result.current.openError).toBeTruthy())

    await act(async () => { rerender({ s: 'member-other' }) })
    expect(result.current.openError).toBe('')
    expect(result.current.opening).toBe(false)
    expect(result.current.endError).toBe('')
    expect(result.current.ending).toBe(false)
  })

  it('a mint still in flight across the switch installs nothing', async () => {
    // The slot check in `live()` already covers this one; the sequence bump covers
    // it as well, so a mint cannot land on a parent the reader has left whichever
    // guard is consulted.
    const settle: Array<(v: unknown) => void> = []
    mockOpen.mockImplementation(() => new Promise((res) => { settle.push(res) }))
    const { result, rerender } = mountWith('member-radar')
    await waitFor(() => expect(result.current.hooks).toBeTruthy())
    await act(async () => { result.current.openThread('m-3') })
    expect(settle).toHaveLength(1)

    await act(async () => { rerender({ s: 'member-other' }) })
    await act(async () => {
      settle[0]({
        thread_slot: 'chat-90-1758525000',
        anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-3' },
        title: 'Thread: something',
        seeded: true,
      })
      await Promise.resolve()
    })
    expect(result.current.open).toBeNull()
  })

  it('leaves state alone on a render where the slot did not move', async () => {
    const { result, rerender } = mountWith('member-radar')
    await waitFor(() => expect(result.current.summaryOf('m-1')).toBeTruthy())
    act(() => result.current.openThread('m-1'))
    await act(async () => { rerender({ s: 'member-radar' }) })
    expect(result.current.open).toEqual({ mid: 'm-1', threadSlot: LIVE.thread_slot })
  })
})

describe('useThreads readiness after a failed anchor read', () => {
  it('does not report ready when the anchor read failed', async () => {
    // A deep link waits on `summaryReady`. Counting a failure as ready acts on the
    // address with no anchors known, so every message reads as anchorless and the
    // deep link mints a replacement thread on a message that already has one --
    // answering a reload of an ENDED thread with a brand-new conversation.
    mockSummary.mockRejectedValue(new ApiError(500, 'boom', ''))
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryFailed).toBe(true))
    expect(result.current.summaryReady).toBe(false)
    // The reader is not stranded: the failure is surfaced with a retry.
    expect(typeof result.current.retrySummary).toBe('function')
  })

  it('reports ready once the read succeeds', async () => {
    const { result } = mount('member-radar')
    await waitFor(() => expect(result.current.summaryReady).toBe(true))
    expect(result.current.summaryFailed).toBe(false)
  })

  it('is ready with no slot, where there is nothing to read', () => {
    const { result } = mount(undefined)
    expect(result.current.summaryReady).toBe(true)
  })
})
