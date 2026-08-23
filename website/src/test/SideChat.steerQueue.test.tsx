import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer, {
  sseSideResult, sseSideQueue, sideReleaseConsumed, sideOptimisticAppend, sideOptimisticRollback,
  sideClose as sideCloseAction,
} from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideQueueCancel: vi.fn().mockResolvedValue({ ok: true, content: 'queued text', depth: 0 }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'

// The composer blocks sends while the gateway reads as offline, so every
// scene runs against a connected dashboard unless it tests the offline path.
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }

const SLOT = 'test-slot-1'
const initial = reducer(undefined, { type: '@@INIT' })

/** A side that is mid-turn: the answer is streaming, so a submit can only
 *  steer or queue. */
function busyState(extra: Record<string, unknown> = {}) {
  return createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'partial', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming: true,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
          ...extra,
        },
      },
    },
  })
}

describe('SideChat busy-send: steer vs queue', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('shows the split send button while a turn is in flight and steers by default', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    await user.type(screen.getByLabelText('Ask a side question'), 'actually use QUIC')
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'actually use QUIC', { steer: true }))
  })

  it('Queue mode submits without the steer flag', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.type(screen.getByLabelText('Ask a side question'), 'later please')
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'later please', undefined))
  })

  it('an idle side keeps the plain send button and never sends a steer flag', async () => {
    const user = userEvent.setup()
    const store = createTestStore({ dashboard: dashInitial, chat: { ...initial, activeSlot: SLOT } })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.type(screen.getByLabelText('Ask a side question'), 'fresh question')
    expect(screen.queryByTestId('busy-send-button')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('Send'))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'fresh question', undefined))
  })

  it('the composer stays usable while a turn runs', () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })
    expect(screen.getByLabelText('Ask a side question')).not.toBeDisabled()
  })

  it('a rejected submit keeps BOTH its text and whatever was typed since', async () => {
    const user = userEvent.setup()
    // The queue-full 429 makes rejection a reachable path, and the composer is
    // live during the request, so the user can be mid-draft when it lands. The
    // test settles the request itself rather than racing a timer, so "typed in
    // flight" is a fact of the schedule and not of how fast typing happens.
    let failRequest!: (err: Error) => void
    vi.mocked(api.sideTurn).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { failRequest = reject })
    )
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    const box = screen.getByLabelText('Ask a side question')
    await user.type(box, 'rejected one')
    await user.click(screen.getByTestId('busy-send-button'))
    // onMutate cleared the draft; the user starts a new thought while it is in flight.
    await waitFor(() => expect(box).toHaveValue(''))
    await user.type(box, 'a new thought')

    failRequest(new Error('side queue is full (max 20)'))

    await waitFor(() => expect(box).toHaveValue('a new thought\n\nrejected one'))
  })
})

describe('SideChat queue cards', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('cancels through the server and only then hands the text back', async () => {
    const user = userEvent.setup()
    const store = busyState({
      queue: [{ id: 'q-1', content: 'queued text', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    expect(screen.getByText('queued text')).toBeInTheDocument()
    await user.click(screen.getByLabelText('Cancel queued message'))

    await waitFor(() => expect(api.sideQueueCancel).toHaveBeenCalledWith(SLOT, 'q-1'))
    // The HTTP response is one of TWO convergence paths (the other is the
    // `chat.side_queue` frame), so the card retires without any WebSocket
    // delivery — a dropped socket cannot leave it stale forever.
    await waitFor(() => expect(store.getState().chat.slotSide[SLOT].queue).toEqual([]))
    await waitFor(() => expect(screen.getByLabelText('Ask a side question')).toHaveValue('queued text'))

    // And the frame arriving afterwards is a no-op rather than a double-apply.
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-1' }))
    expect(store.getState().chat.slotSide[SLOT].queue).toEqual([])
  })

  it('a cancel keeps BOTH the queued text and an in-progress draft', async () => {
    const user = userEvent.setup()
    const store = busyState({
      queue: [{ id: 'q-1', content: 'queued text', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    // Typing while something is queued is the intended flow now that the composer
    // stays live, so this is the common case — not an edge one.
    await user.type(screen.getByLabelText('Ask a side question'), 'half-typed')
    await user.click(screen.getByLabelText('Cancel queued message'))

    // Both are typed work and the released text has no other home, so neither
    // may be discarded — they are merged for the user to edit.
    await waitFor(() =>
      expect(screen.getByLabelText('Ask a side question')).toHaveValue('half-typed\n\nqueued text')
    )
  })

  it('a demoted steer says so instead of only showing a card', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideTurn).mockResolvedValueOnce({ ok: true, queued: true, demoted: true, queue_id: 'q-9', depth: 1 })
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    await user.type(screen.getByLabelText('Ask a side question'), 'too late')
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() =>
      expect(screen.getByText('The turn ended — queued instead')).toBeInTheDocument()
    )
  })

  it('a second cancel click cannot fire while the first is in flight', async () => {
    const user = userEvent.setup()
    // The card is only retired when the server's frame lands, so it stays on
    // screen through the request. A duplicate would race the first and 404 —
    // reporting a failure for a cancel that worked.
    let release!: () => void
    vi.mocked(api.sideQueueCancel).mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ ok: true, content: 'queued text', depth: 0 }) })
    )
    const store = busyState({
      queue: [{ id: 'q-1', content: 'queued text', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const cancelBtn = screen.getByLabelText('Cancel queued message')
    await user.click(cancelBtn)
    await waitFor(() => expect(cancelBtn).toBeDisabled())
    await user.click(cancelBtn)

    expect(api.sideQueueCancel).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Could not cancel that queued question')).not.toBeInTheDocument()

    release()
    // Once the server confirms, the card is retired outright — so "pending
    // cleared" is no longer observable on the button; the card's absence is the
    // post-success state to assert.
    await waitFor(() => expect(screen.queryByLabelText('Cancel queued message')).not.toBeInTheDocument())
  })

  it('a cancel frame with no HTTP response still releases the text', async () => {
    // Mirror case of the WS-loss test: the DELETE succeeds server-side but its
    // response never arrives, so only the frame lands. It must still hand the
    // text back, or a confirmed cancel silently destroys the question.
    const store = busyState({
      queue: [{ id: 'q-1', content: 'released by frame', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-1', content: 'released by frame' }))

    await waitFor(() => expect(screen.getByLabelText('Ask a side question')).toHaveValue('released by frame'))
    // Released exactly once — the stash is cleared, so a redelivered frame or the
    // late HTTP response cannot append it a second time.
    expect(store.getState().chat.slotSide[SLOT].releasedText).toBeUndefined()
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-1', content: 'released by frame' }))
    await waitFor(() => expect(screen.getByLabelText('Ask a side question')).toHaveValue('released by frame'))
  })

  it('a cancel the server refuses leaves the card standing and reports it', async () => {
    const user = userEvent.setup()
    // A drain can dequeue the entry between render and click — the server then
    // 404s, and the card must NOT disappear as though the text were cancelled.
    vi.mocked(api.sideQueueCancel).mockRejectedValueOnce(new Error('queue entry not found'))
    const store = busyState({
      queue: [{ id: 'q-1', content: 'already running', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.click(screen.getByLabelText('Cancel queued message'))

    await waitFor(() => expect(screen.getByText('Could not cancel that queued question')).toBeInTheDocument())
    expect(store.getState().chat.slotSide[SLOT].queue).toHaveLength(1)
    expect(screen.getByLabelText('Ask a side question')).toHaveValue('')
  })

  it('edits through the server and takes the content from its frame', async () => {
    const user = userEvent.setup()
    const store = busyState({
      queue: [{ id: 'q-1', content: 'old', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByLabelText('Edit queued message')
    await user.clear(editor)
    await user.type(editor, 'new{Enter}')

    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-1', 'new'))
    // Converges from the HTTP response, without needing the WS frame.
    await waitFor(() => expect(store.getState().chat.slotSide[SLOT].queue?.[0].content).toBe('new'))

    // The frame arriving afterwards is idempotent.
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'q-1', content: 'new' }))
    expect(store.getState().chat.slotSide[SLOT].queue?.[0].content).toBe('new')
  })

  it('an edit the server refuses leaves the old content and reports it', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideQueueEdit).mockRejectedValueOnce(new Error('queue entry not found'))
    const store = busyState({
      queue: [{ id: 'q-1', content: 'old', ts: '2026-05-20T00:00:02Z', raw: true }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByLabelText('Edit queued message')
    await user.clear(editor)
    await user.type(editor, 'new{Enter}')

    await waitFor(() => expect(screen.getByText('Could not update that queued question')).toBeInTheDocument())
    expect(store.getState().chat.slotSide[SLOT].queue?.[0].content).toBe('old')
  })
})

describe('chatSlice side queue reducer', () => {
  it('push appends, edit rewrites, cancel and drain remove', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'first' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'b', content: 'second' }))
    expect(state.slotSide[SLOT].queue?.map(e => e.content)).toEqual(['first', 'second'])

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'a', content: 'first edited' }))
    expect(state.slotSide[SLOT].queue?.map(e => e.content)).toEqual(['first edited', 'second'])

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'a' }))
    expect(state.slotSide[SLOT].queue?.map(e => e.id)).toEqual(['b'])

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'b' }))
    expect(state.slotSide[SLOT].queue).toEqual([])
  })

  it('a redelivered push updates in place instead of doubling the card', () => {
    let state = reducer(initial, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'x' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'x' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(1)
  })

  it('a head-insert push PREPENDS, matching the order the backend will run', () => {
    // A requeued steer and a failed drain's entry go to the HEAD server-side.
    // Appending them would show a different next question than the backend runs.
    let state = reducer(initial, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'existing', content: 'already queued' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'steer-1', content: 'first steer', front: true }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'steer-2', content: 'second steer', front: true }))

    // The backend inserts each at the head in reverse order, so the resulting
    // order is [second, first, existing] on both sides.
    expect(state.slotSide[SLOT].queue?.map(e => e.content)).toEqual([
      'second steer',
      'first steer',
      'already queued',
    ])
  })

  it('a queue frame never resurrects a closed side', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, { type: 'chat/sideClose', payload: SLOT })
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'late' }))
    expect(state.slotSide[SLOT]).toBeUndefined()
  })
})

describe('chatSlice steer frame placement', () => {
  it('lands the steer bubble ABOVE the streaming answer so the terminal frame still replaces it', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'steer me', steer: true }))

    const rows = state.slotSide[SLOT].messages
    expect(rows.map(m => [m.role, m.content])).toEqual([
      ['user', 'q1'],
      ['user', 'steer me'],
      ['assistant', 'partial'],
    ])
    expect(rows[1].steer).toBe(true)

    // Terminal frame carries the WHOLE turn: it must replace the assistant row,
    // not append a fourth row or concatenate onto the partial text.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial and the rest', final: true }))
    const after = state.slotSide[SLOT].messages
    expect(after).toHaveLength(3)
    expect(after[2].content).toBe('partial and the rest')
    expect(state.slotSide[SLOT].streaming).toBe(false)
  })

  it('repairs the card even when it arrives BEFORE the steer response resolves', async () => {
    // The other order. `submittedRaw` is a ref, so writing it triggers no render and the
    // queue effect will not re-run — the repair has to happen in `onSuccess` too. A
    // deferred response reproduces the race deterministically.
    const raw = 'rotate AKIAIOSFODNN7EXAMPLE now'
    const redacted = 'rotate [REDACTED: credential] now'

    let resolveTurn: (v: unknown) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(
      () => new Promise(res => { resolveTurn = res }) as ReturnType<typeof api.sideTurn>,
    )

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalled())

    // The requeued card lands FIRST, while the response is still in flight.
    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-early', content: redacted,
      front: true, steer_id: 'steer-early',
    }))
    expect(
      store.getState().chat.slotSide[SLOT]?.queue?.find(e => e.id === 'q-early')?.content,
    ).toBe(redacted)

    // Only now does the steer response arrive with the correlation id.
    resolveTurn({ ok: true, steered: false, pending: true, run_id: 'r1', steer_id: 'steer-early' })

    await waitFor(() => {
      const entry = store.getState().chat.slotSide[SLOT]?.queue?.find(e => e.id === 'q-early')
      expect(entry?.content).toBe(raw)
    })
  })

  it('cancelling a requeued steer via the WS frame releases the RAW text', async () => {
    // The reducer's cancel path reads the CARD, and cannot reach the raw-text map in the
    // component. So the card itself has to be repaired; otherwise a credential-bearing
    // steer comes back scrubbed and the user sends `[REDACTED: credential]`.
    const raw = 'redeploy with AKIAIOSFODNN7EXAMPLE please'
    const redacted = 'redeploy with [REDACTED: credential] please'

    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true, steered: false, pending: true, run_id: 'r1', steer_id: 'steer-xyz',
    })

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalled())

    // The turn ends without consuming it: the backend requeues it as a card whose
    // broadcast content is REDACTED, naming the steer it came from.
    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-requeued', content: redacted,
      front: true, steer_id: 'steer-xyz',
    }))

    // The card must be rewritten to the raw text the client still holds.
    await waitFor(() => {
      const entry = store.getState().chat.slotSide[SLOT]?.queue?.find(e => e.id === 'q-requeued')
      expect(entry?.content).toBe(raw)
    })

    // And a cancel arriving as a WS frame — no HTTP response involved — releases raw.
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-requeued' }))
    await waitFor(() => {
      const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
      expect(box.value).toContain('AKIAIOSFODNN7EXAMPLE')
    })
    const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(box.value).not.toContain('[REDACTED')
  })

  it('a requeued steer card keeps the steer id it came from', () => {
    // The card's queue id is brand new to this client and its broadcast content is
    // redacted, so the steer id is the only handle for matching the raw text the
    // submitter still holds. Losing it means a cancel restores the scrubbed copy.
    const state = reducer(
      initial,
      sseSideQueue({
        slot: SLOT,
        action: 'push',
        queue_id: 'q-from-steer',
        content: 'deploy with [REDACTED: credential]',
        front: true,
        steer_id: 'steer-abc',
      }),
    )

    const entry = state.slotSide[SLOT].queue?.[0]
    expect(entry?.id).toBe('q-from-steer')
    expect(entry?.steerId).toBe('steer-abc')
  })

  it('an ordinary queue card carries no steer id', () => {
    const state = reducer(
      initial,
      sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-plain', content: 'just asking' }),
    )
    expect(state.slotSide[SLOT].queue?.[0].steerId).toBeUndefined()
  })

  it('an assistant frame between the bubble and its user frame does not duplicate it', () => {
    // The reconcile used to take "the last message" — so an in-flight turn's
    // assistant text landing on top of the optimistic bubble made the server's user
    // frame push a SECOND bubble for the same question.
    const q = 'what does this flag do'
    let state = reducer(
      initial,
      sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: q, ts: '2026-05-20T00:00:00Z' } }),
    )
    // An assistant frame for the turn already running arrives in between.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r-old', role: 'assistant', content: 'earlier answer' }))
    // Then the server's own user frame for the question.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r-new', role: 'user', content: q }))

    const mine = state.slotSide[SLOT].messages.filter(m => m.role === 'user' && m.content === q)
    expect(mine).toHaveLength(1)
    expect(mine[0].run_id).toBe('r-new')
    expect(mine[0].optimistic).toBeUndefined()
  })

  it('a rollback removes the bubble, not whatever happens to be last', () => {
    const q = 'this submit will fail'
    let state = reducer(
      initial,
      sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: q, ts: '2026-05-20T00:00:00Z' } }),
    )
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r-old', role: 'assistant', content: 'a real answer' }))
    state = reducer(state, sideOptimisticRollback(SLOT))

    const contents = state.slotSide[SLOT].messages.map(m => m.content)
    expect(contents).toContain('a real answer')
    expect(contents).not.toContain(q)
  })

  it('StrictMode does not restore a cancelled question twice', async () => {
    // StrictMode runs effect / cleanup / effect against the SAME render, so the
    // consume dispatch has not landed between the two invocations. Appending to the
    // draft has to be idempotent on its own.
    const store = busyState({ releasedText: 'the cancelled question' })
    renderWithProviders(
      <StrictMode>
        <SideChat slot={SLOT} />
      </StrictMode>,
      { store },
    )

    const box = await screen.findByLabelText<HTMLTextAreaElement>('Ask a side question')
    await waitFor(() => expect(box.value).toContain('the cancelled question'))
    const hits = box.value.split('the cancelled question').length - 1
    expect(hits).toBe(1)
  })

  it('a second release of the same text is still drained', async () => {
    // The idempotence guard must key on a release still in flight, never on the text
    // alone: cancelling the same question twice has to hand it back both times.
    const store = busyState({ releasedText: 'same words' })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const box = await screen.findByLabelText<HTMLTextAreaElement>('Ask a side question')
    await waitFor(() => expect(box.value).toContain('same words'))
    // The component consumed it, so the store is clear again.
    await waitFor(() => expect(store.getState().chat.slotSide[SLOT]?.releasedText ?? '').toBe(''))

    // Clear the composer as the user would, then release the identical text again.
    await userEvent.clear(box)
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-again', content: 'same words' }))
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-again' }))

    await waitFor(() => expect(box.value).toContain('same words'))
  })

  it('the submit itself repairs a WS-first redacted card to the raw text', async () => {
    // Component-level on purpose. The reducer test below proves the dispatch PAIR
    // behaves; only driving the real component proves the component actually sends
    // the repair edit. (A mutation that deleted the edit left the reducer test
    // green, which is exactly the blind spot this covers.)
    const raw = 'deploy using AKIAIOSFODNN7EXAMPLE now'
    const redacted = 'deploy using [REDACTED: credential] now'

    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true,
      queued: true,
      queue_id: 'q-ws',
      still_queued: true,
      depth: 1,
    })

    // The redacted WS frame already created the card before the response lands.
    const store = busyState({
      queue: [{ id: 'q-ws', content: redacted, ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() => {
      const entry = store.getState().chat.slotSide[SLOT]?.queue?.[0]
      expect(entry?.content).toBe(raw)
    })
  })

  it('cancelling an edited credential-bearing card restores the EDITED raw text', async () => {
    // Full reachable chain: edit a credential-bearing queued question, let the server's
    // own edit broadcast land (ws.py scrubs it, and the reducer's edit sets content
    // unconditionally, so the card becomes redacted), then cancel. The only surviving
    // copy of the edited raw text is this client's cache, so it has to be current AND
    // has to outrank the card.
    const original = 'deploy with AKIAIOSFODNN7EXAMPLE now'
    const editedRaw = 'deploy with AKIAIOSFODNN7EXAMPLE tomorrow'
    const editedRedacted = 'deploy with [REDACTED: credential] tomorrow'

    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true, queued: true, queue_id: 'q-edit', still_queued: true, depth: 1,
    })
    vi.mocked(api.sideQueueEdit).mockResolvedValueOnce({ ok: true, depth: 1 })
    vi.mocked(api.sideQueueCancel).mockResolvedValueOnce({
      ok: true, content: editedRedacted, depth: 0,
    })

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), original)
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe(original)
    })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByDisplayValue(original)
    await user.clear(editor)
    await user.type(editor, editedRaw)
    await user.click(screen.getByLabelText('Save edit'))
    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalled())

    // The server's edit broadcast arrives scrubbed. It carries no vouch, so the ratchet
    // holds and the card keeps the text the user typed.
    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'edit', queue_id: 'q-edit', content: editedRedacted,
    }))
    expect(
      store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content,
    ).toBe(editedRaw)

    await user.click(screen.getByLabelText('Cancel queued message'))

    await waitFor(() => {
      const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
      expect(box.value).toContain('AKIAIOSFODNN7EXAMPLE')
    })
    const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(box.value).toContain('tomorrow')
    expect(box.value).not.toContain('[REDACTED')
    expect(box.value).not.toContain('now')
  })

  it('a requeued steer card whose id is not yet known cannot be cancelled', async () => {
    // The raw text is keyed on the steer id, which arrives with the HTTP response. If the
    // requeue frame wins the race, cancelling would release the scrubbed copy — so the
    // card's actions stay shut until the response names the steer.
    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    // Requeue frame arrives first: a card with a steer handle and scrubbed content, and no
    // HTTP response yet, so this client has never seen `steer-1`.
    store.dispatch(sseSideQueue({
      slot: SLOT,
      action: 'push',
      queue_id: 'q-requeued',
      content: 'ask about [REDACTED: credential]',
      steer_id: 'steer-1',
      front: true,
    }))

    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.steerId).toBe('steer-1')
    })

    await userEvent.setup().click(screen.getByLabelText('Cancel queued message'))
    // Named explicitly rather than by total call count: mocks are shared across this file.
    expect(api.sideQueueCancel).not.toHaveBeenCalledWith(SLOT, 'q-requeued')
  })

  it('a late scrubbed edit frame cannot overwrite raw card content', () => {
    // The reducer cannot tell a genuine edit from the scrubbed echo of one, so raw content
    // is a one-way ratchet: only a dispatch that vouches for typed text may change it.
    const store = busyState()
    const rawText = 'deploy with AKIAIOSFODNN7EXAMPLE tomorrow'

    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: rawText, raw: true }))
    expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe(rawText)

    // The server's echo of that edit, scrubbed on the wire and carrying no vouch.
    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'edit', queue_id: 'q1', content: 'deploy with [REDACTED: credential] tomorrow',
    }))
    expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe(rawText)
  })

  it('a scrubbed edit still applies to a card this client never typed', () => {
    // The ratchet must not freeze cards the client has no raw copy of — those are the only
    // text it will ever have, so a broadcast edit is an upgrade, not a downgrade.
    const store = busyState()
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'original' }))
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'q2', content: 'edited elsewhere' }))
    expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe('edited elsewhere')
  })

  it('a vouched edit may still change raw content', () => {
    // The user editing their own card must not be blocked by the ratchet.
    const store = busyState()
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q3', content: 'first', raw: true }))
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'q3', content: 'second', raw: true }))
    expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe('second')
  })

  it('a failed edit returns the new text to the composer when the entry has drained', async () => {
    // The turn finishes mid-PATCH, the entry drains, and the request 404s. The editor closed
    // on save and the card is gone, so the composer is the only place the text can land.
    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true, queued: true, queue_id: 'q-drain', still_queued: true, depth: 1,
    })
    vi.mocked(api.sideQueueEdit).mockRejectedValueOnce(new Error('not found'))

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'original question')
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe('original question')
    })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByDisplayValue('original question')
    await user.clear(editor)
    await user.type(editor, 'the edited question')
    await user.click(screen.getByLabelText('Save edit'))

    await waitFor(() => {
      const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
      expect(box.value).toContain('the edited question')
    })
  })

  it('a failed edit MERGES into the composer rather than replacing what is there', async () => {
    // The user may have started a new question while the PATCH was in flight; clobbering it
    // would trade one lost message for another.
    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true, queued: true, queue_id: 'q-merge', still_queued: true, depth: 1,
    })
    vi.mocked(api.sideQueueEdit).mockRejectedValueOnce(new Error('boom'))

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const box = screen.getByLabelText('Ask a side question')
    await user.type(box, 'queued one')
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalled())

    // A new question already sitting in the composer when the edit fails. Seeded before the
    // editor opens because typing into the composer closes the inline editor.
    await user.type(screen.getByLabelText('Ask a side question'), 'meanwhile a new thought')

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByDisplayValue('queued one')
    await user.clear(editor)
    await user.type(editor, 'queued one edited')
    await user.click(screen.getByLabelText('Save edit'))

    await waitFor(() => {
      const composer = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
      expect(composer.value).toContain('queued one edited')
    })
    const composer = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(composer.value).toContain('meanwhile a new thought')
  })

  it('a steer echo arriving after close does not resurrect the conversation', () => {
    // A refresh tombstones the slot. A steer echo carries role 'user', so without the guard
    // it would clear the tombstone and file the old conversation's bubble into the next one.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'old', role: 'user', content: 'q' }))
    state = reducer(state, sideCloseAction(SLOT))

    state = reducer(state, sseSideResult({
      slot: SLOT, run_id: 'old', role: 'user', content: 'a steer from the closed turn',
      steer: true,
    }))

    expect(state.slotSide[SLOT]).toBeUndefined()
    expect(state.slotSideClosed[SLOT]).toBe(true)
  })

  it('a real new question still re-opens a closed side conversation', () => {
    // The guard must not freeze the slot shut — a deliberate re-open is the whole reason the
    // re-open branch exists.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'old', role: 'user', content: 'q' }))
    state = reducer(state, sideCloseAction(SLOT))

    state = reducer(state, sseSideResult({
      slot: SLOT, run_id: 'new', role: 'user', content: 'a brand new question',
    }))

    expect(state.slotSideClosed[SLOT]).toBeUndefined()
    expect(state.slotSide[SLOT]?.messages?.[0]?.content).toBe('a brand new question')
  })

  it('another tab\'s cancel removes the card without pasting the question here', () => {
    // The queue broadcast reaches every owner tab. Without suppression each one merged the
    // question into its own composer, so the user saw it N times and could send it twice.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r', role: 'user', content: 'q' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'a queued question' }))

    state = reducer(state, sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q1', content: 'a queued question', suppressRelease: true,
    }))

    expect(state.slotSide[SLOT].queue?.length ?? 0).toBe(0)
    expect(state.slotSide[SLOT].releasedText).toBeUndefined()
  })

  it('this tab\'s own cancel still hands the question back', () => {
    // The suppression must be scoped to a FOREIGN cancel — the initiating tab is the whole
    // reason releasedText exists.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r', role: 'user', content: 'q' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'a queued question' }))

    state = reducer(state, sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q1', content: 'a queued question',
    }))

    expect(state.slotSide[SLOT].queue?.length ?? 0).toBe(0)
    expect(state.slotSide[SLOT].releasedText).toContain('a queued question')
  })

  it('a card created by the push cannot be cancelled while its submit is still in flight', async () => {
    // The scrubbed push can beat the POST response, and the raw text is only cached when that
    // response lands. Cancelling inside the window would release the scrubbed text AND retire
    // the id, so the late response could not repair the card either.
    let releaseResponse: ((v: unknown) => void) | undefined
    vi.mocked(api.sideTurn).mockImplementationOnce(
      () => new Promise(resolve => { releaseResponse = resolve }) as never,
    )

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'deploy with AKIAIOSFODNN7EXAMPLE')
    await user.click(screen.getByTestId('busy-send-button'))

    // The broadcast lands first, carrying the scrubbed rendering.
    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-inflight', content: 'deploy with [REDACTED: credential]',
    }))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.length).toBe(1)
    })

    await user.click(screen.getByLabelText('Cancel queued message'))
    expect(api.sideQueueCancel).not.toHaveBeenCalledWith(SLOT, 'q-inflight')

    // Once the response settles the card is correlated and actionable again.
    releaseResponse?.({ ok: true, queued: true, queue_id: 'q-inflight', still_queued: true, depth: 1 })
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toContain('AKIAIOSFODNN7EXAMPLE')
    })
    await user.click(screen.getByLabelText('Cancel queued message'))
    await waitFor(() => expect(api.sideQueueCancel).toHaveBeenCalledWith(SLOT, 'q-inflight'))
  })

  it('an already-correlated card stays cancellable while a LATER submit is in flight', async () => {
    // The gate is scoped to cards this client cannot name yet. A card from the user's own
    // earlier submit is correlated, so queueing a second question must not freeze the first.
    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true, queued: true, queue_id: 'q-first', still_queued: true, depth: 1,
    })
    let releaseSecond: ((v: unknown) => void) | undefined
    vi.mocked(api.sideTurn).mockImplementationOnce(
      () => new Promise(resolve => { releaseSecond = resolve }) as never,
    )

    // Mocks are shared across this file, so count relative to a baseline.
    const turnsBefore = vi.mocked(api.sideTurn).mock.calls.length

    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'first question')
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe('first question')
    })

    // A second submit is now in flight and never settles during this test.
    await user.type(screen.getByLabelText('Ask a side question'), 'second question')
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => {
      expect(vi.mocked(api.sideTurn).mock.calls.length).toBe(turnsBefore + 2)
    })

    // The FIRST card is correlated, so it must still be cancellable.
    await user.click(screen.getAllByLabelText('Cancel queued message')[0])
    await waitFor(() => expect(api.sideQueueCancel).toHaveBeenCalledWith(SLOT, 'q-first'))

    releaseSecond?.({ ok: true, queued: true, queue_id: 'q-second', still_queued: true, depth: 1 })
  })

  it('the tab holding the raw copy releases it even when ANOTHER tab cancels', () => {
    // Suppressing a foreign cancel keeps one cancellation from pasting the question into every
    // tab — but the tab that OWNS the unredacted text must still hand it back, or the only good
    // copy is dropped and the cancelling tab is left with the scrubbed one.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r', role: 'user', content: 'q' }))
    state = reducer(state, sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q1', content: 'deploy AKIAIOSFODNN7EXAMPLE', raw: true,
    }))

    state = reducer(state, sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q1',
      content: 'deploy [REDACTED: credential]', suppressRelease: true,
    }))

    expect(state.slotSide[SLOT].queue?.length ?? 0).toBe(0)
    expect(state.slotSide[SLOT].releasedText).toContain('AKIAIOSFODNN7EXAMPLE')
  })

  it('a tab with no raw copy still stays quiet on a foreign cancel', () => {
    // The suppression must survive: a tab that neither cancelled nor holds the text would
    // otherwise paste a scrubbed duplicate into its own composer.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r', role: 'user', content: 'q' }))
    state = reducer(state, sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q1', content: 'deploy [REDACTED: credential]',
    }))

    state = reducer(state, sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q1',
      content: 'deploy [REDACTED: credential]', suppressRelease: true,
    }))

    expect(state.slotSide[SLOT].queue?.length ?? 0).toBe(0)
    expect(state.slotSide[SLOT].releasedText).toBeUndefined()
  })

  it('an edit whose response is lost is treated as applied, not returned to the composer', async () => {
    // The server applied the PATCH and broadcast it; only the response was lost. Restoring the
    // text would leave the question queued AND in the composer, so it would be asked twice.
    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-lost', content: 'original', ts: 1, raw: true,
    }))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.length).toBe(1)
    })

    const user = userEvent.setup()
    vi.mocked(api.sideQueueEdit).mockImplementation(async () => {
      // The broadcast lands while the request is in flight, exactly as it would on the wire.
      store.dispatch(sseSideQueue({
        slot: SLOT, action: 'edit', queue_id: 'q-lost', content: 'ask about [REDACTED: credential]',
      }))
      throw new Error('network dropped')
    })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByDisplayValue('original')
    await user.clear(editor)
    await user.type(editor, 'edited question')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalled())
    // Not merged back into the composer, and no failure claimed — the edit did land.
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/ask/i)).toHaveValue('')
    })
    expect(screen.queryByText(/queue_edit_failed|failed/i)).toBeNull()
  })

  it('an edit that genuinely failed still returns its text to the composer', async () => {
    // The other half of the discriminator: with no broadcast the request really did fail, so
    // the typed text has nowhere to live but the composer.
    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-fail', content: 'original', ts: 1, raw: true,
    }))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.length).toBe(1)
    })

    const user = userEvent.setup()
    vi.mocked(api.sideQueueEdit).mockRejectedValueOnce(new Error('gone'))

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByDisplayValue('original')
    await user.clear(editor)
    await user.type(editor, 'edited question')
    await user.keyboard('{Enter}')

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/ask/i)).toHaveValue('edited question')
    })
  })

  it('refresh is blocked while questions are queued, so closing cannot discard them', async () => {
    // `api_side_close` clears the sidecar's queue. With the button live, a queued question is
    // destroyed without running or coming back to a composer.
    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-keep', content: 'still waiting', ts: 1, raw: true,
    }))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.length).toBe(1)
    })

    const refresh = screen.getByRole('button', { name: /refresh/i })
    expect(refresh).toBeDisabled()
    await userEvent.setup().click(refresh)
    expect(api.sideClose).not.toHaveBeenCalled()
  })

  it('refresh stays available on an idle panel with nothing queued', async () => {
    // The guard must stay scoped: an idle side panel with nothing waiting still needs its
    // context reset, which is the whole purpose of this control.
    const store = busyState({ streaming: false, pending: false })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    expect(screen.getByRole('button', { name: /refresh/i })).toBeEnabled()
  })

  it('refresh is blocked during a running turn, which can hold an unconsumed steer', async () => {
    // An accepted steer is not in `queue` and has no card, so the empty-queue check cannot see
    // it. Closing clears the ledger before the cleanup can requeue it, losing the question.
    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const refresh = screen.getByRole('button', { name: /refresh/i })
    expect(refresh).toBeDisabled()
    await userEvent.setup().click(refresh)
    expect(api.sideClose).not.toHaveBeenCalled()
  })

  it('an answer offering choices renders them as buttons, not as the raw marker', async () => {
    // `parseOptions` strips the marker out of the answer text, and the bar that turns the
    // choices into buttons lives in the composer — so stripping without the bar would DELETE
    // the choices, which is worse than the raw marker the user could at least read and type.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick one\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    expect(screen.queryByText(/\[OPTIONS:/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Rebase first' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Skip the rebase' })).toBeInTheDocument()
  })

  it('picking a choice fills the composer instead of sending it', async () => {
    // The draft is the source of truth for what gets submitted, so a pick stays amendable.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick one\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    // `api.sideTurn` is shared across this file, so the baseline is what makes "did not send"
    // meaningful — an absolute count would carry earlier tests' calls.
    const turnsBefore = vi.mocked(api.sideTurn).mock.calls.length
    // A single click on the chip body is debounced (the chip reserves double-click for its
    // own send gesture), so the fill lands after the timer rather than on the click.
    await userEvent.setup().click(screen.getByRole('button', { name: 'Rebase first' }))
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/ask/i)).toHaveValue('Rebase first')
    })
    expect(vi.mocked(api.sideTurn).mock.calls.length).toBe(turnsBefore)
  })

  it("a choice's send arrow submits that choice, not the unrelated draft", async () => {
    // The chip hands the option to `onSend` precisely because it has not been folded into the
    // draft yet. A callback that ignored the argument would submit the draft alone and drop the
    // answer the user just clicked.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick one\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText(/ask/i), 'some unrelated note')
    await user.click(screen.getByRole('button', { name: /send now.*Rebase first/i }))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalled())
    const sent = vi.mocked(api.sideTurn).mock.calls.at(-1)
    expect(JSON.stringify(sent)).toContain('Rebase first')
    // The draft belongs to the composer, not to this send: wiping it would destroy text the
    // user typed and never sent. The main chat guards the same way (`if (!optionText)`).
    expect(screen.getByPlaceholderText(/ask/i)).toHaveValue('some unrelated note')
  })

  it('un-picking keeps the identical word the user typed themselves', async () => {
    // The tail is `bar, bar`: only the second one is the pick. Peeling both would delete the
    // user's own word along with it.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick\n\n[OPTIONS: bar]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)
    const chip = screen.getByRole('button', { name: 'bar' })

    await user.type(composer, 'bar, bar')
    // Only the LAST one is treated as the pick, so the chip offers to take one back.
    expect(chip).toHaveAttribute('title', 'Click to remove from input (double-click to send)')

    await user.click(chip)
    await waitFor(() => expect(composer).toHaveValue('bar'))
  })

  it('a pick made after editing still reaches the draft and gets sent', async () => {
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)
    const before = (api.sideTurn as unknown as Mock).mock.calls.length

    await user.click(screen.getByRole('button', { name: 'Rebase first' }))
    await waitFor(() => expect(composer).toHaveValue('Rebase first'))

    await user.type(composer, ' if you can')
    await user.click(screen.getByRole('button', { name: 'Skip the rebase' }))
    // A highlighted chip whose text never landed in the composer would be dropped silently here.
    await waitFor(() => {
      expect(composer).toHaveValue('Rebase first if you can, Skip the rebase')
    })

    // Exactly 'Send': the chips carry their own 'Send now: <option>' segments.
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => {
      const calls = (api.sideTurn as unknown as Mock).mock.calls.slice(before)
      expect(calls.length).toBe(1)
      expect(calls[0][1]).toContain('Skip the rebase')
    })
  })

  it('editing elsewhere in the draft is not undone by a later un-pick', async () => {
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)
    const chip = screen.getByRole('button', { name: 'Rebase first' })

    await user.type(composer, 'hi')
    await user.click(chip)
    await waitFor(() => expect(composer).toHaveValue('hi, Rebase first'))

    // Typed at the front, so the option is still the tail and the block is still found — but what
    // precedes it is no longer what was recorded.
    await user.type(composer, 'X ', { initialSelectionStart: 0, initialSelectionEnd: 0 })
    expect(composer).toHaveValue('X hi, Rebase first')

    await user.click(chip)
    await waitFor(() => expect(composer).toHaveValue('X hi'))
  })

  it('a draft already ending in a comma does not get a second one', async () => {
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)
    const chip = screen.getByRole('button', { name: 'Rebase first' })

    await user.type(composer, 'hello,')
    await user.click(chip)
    await waitFor(() => expect(composer).toHaveValue('hello, Rebase first'))

    // And un-picking puts the user's comma back exactly, even though `hello,` + ` ` and
    // `hello` + `, ` are the same string.
    await user.click(chip)
    await waitFor(() => expect(composer).toHaveValue('hello,'))
  })

  it('editing past the option de-selects it, and clicking again appends', async () => {
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)
    const chip = screen.getByRole('button', { name: 'Rebase first' })

    await user.click(chip)
    await waitFor(() => expect(composer).toHaveValue('Rebase first'))
    await waitFor(() => expect(chip).toHaveAttribute('title', 'Click to remove from input (double-click to send)'))

    // Typing past the option makes it part of the user's own sentence, so it stops being a block
    // this can take back out — and the chip must stop claiming otherwise.
    await user.type(composer, ' but only if CI is green')
    expect(composer).toHaveValue('Rebase first but only if CI is green')
    expect(chip).toHaveAttribute('title', 'Click to add to input (double-click to select and send)')

    // So the next click is a fresh pick: it appends rather than being swallowed.
    await user.click(chip)
    await waitFor(() => {
      expect(composer).toHaveValue('Rebase first but only if CI is green, Rebase first')
    })
    expect(chip).toHaveAttribute('title', 'Click to remove from input (double-click to send)')
  })

  it('un-picking one option leaves another whose text contains it intact', async () => {
    // `foo, bar` contains `bar` plus the separator, so any substring search for `, bar` cuts into
    // the second option instead of removing the first. The block is rebuilt from the picked list.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick\n\n[OPTIONS: bar | foo, bar]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)

    await user.click(screen.getByRole('button', { name: 'bar' }))
    await waitFor(() => expect(composer).toHaveValue('bar'))

    await user.click(screen.getByRole('button', { name: 'foo, bar' }))
    await waitFor(() => expect(composer).toHaveValue('bar, foo, bar'))

    // Removing the FIRST option must leave the second one whole.
    await user.click(screen.getByRole('button', { name: 'bar' }))
    await waitFor(() => expect(composer).toHaveValue('foo, bar'))
  })

  it('un-picking removes the appended copy, not the same words the user typed', async () => {
    // A draft may legitimately already contain the option's words. Searching from the left
    // splices the user's own text and leaves the appended copy behind.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'pick one\n\n[OPTIONS: Rebase first | Skip the rebase]',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    const composer = screen.getByPlaceholderText(/ask/i)
    await user.type(composer, 'a, Rebase first and b')

    const chip = screen.getByRole('button', { name: 'Rebase first' })
    await user.click(chip)
    await waitFor(() => {
      expect(composer).toHaveValue('a, Rebase first and b, Rebase first')
    })

    await user.click(chip)
    await waitFor(() => {
      expect(composer).toHaveValue('a, Rebase first and b')
    })
  })

  it('a steer acknowledgement renders as a chip, not as the raw marker', async () => {
    // kiro-cli emits the acknowledgement inline as `[STEERING steer-<id>: <summary>]` and the
    // backend deliberately does NOT strip it — it only holds back a half-streamed fragment,
    // because a frontend consumer is expected to extract it. Rendering the side transcript
    // through the shared list is what runs that extraction.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'partial [STEERING steer-abc123: switched to the new question] rest',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    expect(screen.queryByText(/\[STEERING/)).toBeNull()
    expect(screen.getByText('Steered')).toBeInTheDocument()
    expect(screen.getByText('switched to the new question')).toBeInTheDocument()
  })

  it('an error answer renders through the shared error treatment', async () => {
    // The side buffer flags an error on an `assistant` row rather than carrying an `error`
    // role, so the mapping is what gets it the same treatment as the main transcript.
    const store = busyState({
      messages: [
        { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        {
          role: 'assistant' as const,
          content: 'the backend refused',
          ts: '2026-05-20T00:00:01Z',
          run_id: 'r1',
          is_error: true,
        },
      ],
      streaming: false,
      pending: false,
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    // The danger treatment is the point: the same text in an assistant row proves nothing.
    expect(screen.getByText('the backend refused')).toHaveClass('text-danger')
  })

  it('a card whose text this client never had can be neither edited nor cancelled', async () => {
    // Editing would save the scrubbed rendering over the real question. Cancelling is no safer:
    // it deletes the raw entry server-side while the response returns only `redact(content)`,
    // so with the tab that typed it closed the question would survive nowhere. The entry is
    // left to drain on the next turn instead.
    const store = busyState()
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    store.dispatch(sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-foreign', content: 'ask about [REDACTED: credential]',
    }))
    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.length).toBe(1)
    })

    expect(screen.getByLabelText('Edit queued message')).toBeDisabled()
    expect(screen.getByLabelText('Cancel queued message')).toBeDisabled()

    await userEvent.setup().click(screen.getByLabelText('Cancel queued message'))
    expect(api.sideQueueCancel).not.toHaveBeenCalledWith(SLOT, 'q-foreign')
  })

  it('a response lands in the slot the question was asked in, not the one now shown', async () => {
    // The panel is not keyed by slot, so switching slots re-renders the SAME instance with a
    // new prop and react-query resolves callbacks from the current options. A response that
    // read the rendered slot would file this card under the slot the user moved to.
    const OTHER = 'chat-other'
    let releaseTurn: ((v: unknown) => void) | undefined
    vi.mocked(api.sideTurn).mockImplementationOnce(
      () => new Promise(resolve => { releaseTurn = resolve }) as never,
    )

    const store = busyState()
    const { rerender } = renderWithProviders(<SideChat slot={SLOT} />, { store })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'asked in the first slot')
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalled())

    // The user moves to another slot before the response arrives.
    rerender(<SideChat slot={OTHER} />)

    releaseTurn?.({ ok: true, queued: true, queue_id: 'q-first-slot', still_queued: true, depth: 1 })

    await waitFor(() => {
      expect(store.getState().chat.slotSide[SLOT]?.queue?.[0]?.content).toBe('asked in the first slot')
    })
    // The slot now on screen must not have acquired a card for a question asked elsewhere.
    expect(store.getState().chat.slotSide[OTHER]?.queue ?? []).toEqual([])
  })

  it('a WS-first card still ends up holding the raw text', () => {
    // The redacted WS frame can create the card before the HTTP response lands.
    // The raw push is then ignored as a duplicate (it must be, or a LATE redacted
    // push would clobber raw text), so the submit follows it with an `edit` — the
    // one action allowed to change content — and the card ends up raw either way.
    const raw = 'deploy using AKIAIOSFODNN7EXAMPLE now'
    const redacted = 'deploy using [REDACTED: credential] now'

    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    // WS frame wins the race and creates the card, redacted.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: redacted }))
    expect(state.slotSide[SLOT].queue?.[0].content).toBe(redacted)

    // The submit's own dispatch pair then repairs it.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: raw }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'q1', content: raw }))

    expect(state.slotSide[SLOT].queue).toHaveLength(1)
    expect(state.slotSide[SLOT].queue?.[0].content).toBe(raw)

    // And a cancel therefore releases the raw text, not the scrubbed rendering.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q1' }))
    const released = state.slotSide[SLOT].releasedText ?? ''
    expect(released).toContain('AKIAIOSFODNN7EXAMPLE')
    expect(released).not.toContain('[REDACTED')
  })

  it('a replayed redacted push cannot overwrite the raw text already stored', () => {
    // Broadcasts are scrubbed on the wire, so a late duplicate push carries a
    // redacted rendering of text already stored raw from the HTTP response.
    // Overwriting it corrupts whatever a later cancel restores.
    const raw = 'deploy using AKIAIOSFODNN7EXAMPLE now'
    const redacted = 'deploy using [REDACTED: credential] now'

    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: raw }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: redacted }))

    expect(state.slotSide[SLOT].queue).toHaveLength(1)
    expect(state.slotSide[SLOT].queue?.[0].content).toBe(raw)
  })

  it('an explicit edit still changes the content', () => {
    // The guard above must not freeze the card: real content changes arrive as
    // `edit`, and those still apply.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'before' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'q1', content: 'after' }))
    expect(state.slotSide[SLOT].queue?.[0].content).toBe('after')
  })

  it('consuming a release keeps text a second cancel appended after the snapshot', () => {
    // releasedText accumulates, and the consumer captures it at render then clears
    // it. A cancel landing between those two moments had its text appended and then
    // deleted with the rest — the same loss round 8 fixed, one layer up.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'first' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'second' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q1' }))

    // What the consumer's render saw.
    const snapshot = state.slotSide[SLOT].releasedText ?? ''
    expect(snapshot).toContain('first')

    // A second cancel appends BEFORE the consumer's effect runs.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q2' }))
    // Now the effect reports only what it drained.
    state = reducer(state, sideReleaseConsumed({ slot: SLOT, consumed: snapshot }))

    const left = state.slotSide[SLOT].releasedText ?? ''
    expect(left).toContain('second')
    expect(left).not.toContain('first')
  })

  it('consuming the whole buffer clears it', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'only' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q1' }))
    const snapshot = state.slotSide[SLOT].releasedText ?? ''
    state = reducer(state, sideReleaseConsumed({ slot: SLOT, consumed: snapshot }))
    expect(state.slotSide[SLOT].releasedText).toBeUndefined()
  })

  it('a submit response landing after the drain frame cannot resurrect the card', () => {
    // The HTTP callback and the WS frame are independent, so the frame that
    // retires an entry can land first. Re-pushing then shows a card the server no
    // longer has, and cancelling that card 404s. A server-side "is it still
    // queued" answer cannot close this — it is already stale by the time the
    // callback runs — so the client remembers retired ids.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'later please' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(1)

    // The queue drains: the entry is gone from the server.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'q1' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(0)

    // The submit's HTTP callback finally runs and re-pushes the same id.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'later please' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(0)
  })

  it('a cancelled entry also stays retired against a late push', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'drop me' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q2' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'drop me' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(0)
  })

  it('cancel releases the card\u2019s raw text, not the redacted copy from the frame', () => {
    // Broadcast payloads are scrubbed on the wire, so the cancel frame carries a
    // redacted rendering of the question. Releasing THAT into the composer hands
    // the user a permanently corrupted prompt — they would have to retype the
    // secret, or not notice and send the redaction marker as their question.
    const raw = 'deploy using AKIAIOSFODNN7EXAMPLE now'
    const redacted = 'deploy using [REDACTED: credential] now'

    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    // The card is populated from the HTTP response, which carries the raw text.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: raw }))
    // The cancel frame arrives redacted.
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q1', content: redacted }))

    const released = state.slotSide[SLOT].releasedText ?? ''
    expect(released).toContain('AKIAIOSFODNN7EXAMPLE')
    expect(released).not.toContain('[REDACTED')
  })

  it('two cancellations settling before the panel reads keep both texts', () => {
    // releasedText is a handoff buffer: the panel merges it into the composer and
    // clears it. If two cancels settle inside that window, assigning would drop
    // the first question permanently — the exact loss this feature prevents.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q1', content: 'first cancelled' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q2', content: 'second cancelled' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(2)

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q2' }))

    const released = state.slotSide[SLOT].releasedText ?? ''
    expect(released).toContain('first cancelled')
    expect(released).toContain('second cancelled')
  })

  it('a steer settling after the next turn started lands at its own run, not the tail', () => {
    // The steer RPC and the stream are separate paths, so a consumed steer can
    // surface after the next queued turn has already begun. Appending it then
    // files an older steer below a newer turn and regresses run identity.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'a1', final: true }))
    // The queue drains: a second turn starts and streams.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'q2' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'a2 partial' }))
    expect(state.slotSide[SLOT].lastRunId).toBe('r2')

    // Now r1's steer finally surfaces.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'late steer for r1', steer: true }))

    const msgs = state.slotSide[SLOT].messages
    const steerIdx = msgs.findIndex(m => m.steer)
    const r1Answer = msgs.findIndex(m => m.role === 'assistant' && m.run_id === 'r1')
    const r2User = msgs.findIndex(m => m.role === 'user' && m.run_id === 'r2' && !m.steer)

    expect(steerIdx).toBeGreaterThanOrEqual(0)
    expect(steerIdx).toBe(r1Answer - 1)      // sits immediately above r1's answer
    expect(steerIdx).toBeLessThan(r2User)    // and stays above the newer turn
    // Run identity must not regress to the turn that already ended.
    expect(state.slotSide[SLOT].lastRunId).toBe('r2')
  })

  it('a steer frame arriving after the terminal frame does not revive busy state', () => {
    // The steer RPC and the stream are separate paths, so the chip can land after
    // the turn has already finished. Reviving pending/streaming there strands the
    // panel: nothing later would clear it, and the composer keeps offering Steer
    // for a turn that ended.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'the whole answer', final: true }))
    expect(state.slotSide[SLOT].streaming).toBe(false)
    expect(state.slotSide[SLOT].pending).toBe(false)

    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'late steer', steer: true }))

    expect(state.slotSide[SLOT].streaming).toBe(false)
    expect(state.slotSide[SLOT].pending).toBe(false)
    // The chip is still recorded — it just does not claim the turn is live.
    expect(state.slotSide[SLOT].messages.some(m => m.steer)).toBe(true)
  })

  it('a steer arriving before any delta simply appends', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'early steer', steer: true }))
    expect(state.slotSide[SLOT].messages.map(m => m.content)).toEqual(['q1', 'early steer'])
  })
})
