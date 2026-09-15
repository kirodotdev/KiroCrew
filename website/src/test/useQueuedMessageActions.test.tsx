import { describe, it, expect, vi, beforeEach } from 'vitest'
import { StrictMode } from 'react'
import { render, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

/* Equivalence pins for the shared queue-action recipe extracted in #5891.
 *
 * These assert the recipe ITSELF, at the seam both hosts now call, so a later
 * change to one host cannot quietly re-fork it. The per-host wiring is pinned
 * separately: ChatPage in ChatPageW3Coverage.test.tsx / ChatPageCoverage.test.tsx,
 * ChatPane in ChatPane.queueEdit.test.tsx and ChatPane.queueActions.test.tsx.
 *
 * Mutation checks (each makes a test below RED):
 *  - drop the `if (!trimmed) return` guard        -> "refuses a blank edit"
 *  - drop the `if (!slot) return` guard           -> "does nothing without a slot"
 *  - drop the optimistic dispatch from onCancel   -> "removes the card optimistically"
 *  - build reorder from visibleQueued not allQueued -> "submits the FULL order"
 *  - never add to pendingIds                      -> "latches the card while in flight"
 *  - release the latch only on success            -> "releases the latch on failure"
 */

const deferred = () => {
  let resolve!: (v?: unknown) => void
  let reject!: (e?: unknown) => void
  const promise = new Promise<unknown>((res, rej) => { resolve = res as typeof resolve; reject = rej })
  return { promise, resolve, reject }
}

const apiMocks = vi.hoisted(() => ({
  cancelQueuedMessage: vi.fn(),
  editQueuedMessage: vi.fn(),
  interruptSlot: vi.fn(),
  reorderQueuedMessages: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: apiMocks }))

import { useQueuedMessageActions, queuedSendStash, type QueuedMessageActions } from '../hooks/useQueuedMessageActions'

const queued = (queueId: string, content: string): ChatMessage =>
  ({ role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId } }) as ChatMessage

function makeStore(slot: string, rows: ChatMessage[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      chat: { activeSlot: slot, messages: rows, slotMessages: {} },
    } as unknown as Partial<RootState>,
  })
}

/** Render the hook with the host-supplied inputs and expose its result. */
function renderActions(opts: {
  slot?: string | null
  rows?: ChatMessage[]
  /** Rows QueueStack would draw. Defaults to every row (all interactive). */
  visible?: ChatMessage[]
  restoreDraft?: (text: string, files: string[]) => void
}) {
  const rows = opts.rows ?? [queued('q1', 'run the tests'), queued('q2', 'then deploy')]
  const slot = opts.slot === undefined ? 'chat-1' : opts.slot
  const store = makeStore(slot ?? 'chat-1', rows)
  let actions: QueuedMessageActions | null = null

  function Probe({ queue }: { queue: ChatMessage[] }) {
    actions = useQueuedMessageActions({
      slot,
      allQueued: queue,
      visibleQueued: opts.visible ?? queue,
      restoreDraft: opts.restoreDraft,
    })
    return null
  }
  const wrap = (queue: ChatMessage[]) => <Provider store={store}><Probe queue={queue} /></Provider>
  const view = render(wrap(rows))
  return {
    store,
    get: () => actions!,
    slot,
    /** Re-render as the host would once a server frame changed the queue. */
    setQueue: (next: ChatMessage[]) => view.rerender(wrap(next)),
  }
}

const queueIdsIn = (store: ReturnType<typeof makeStore>) =>
  (store.getState() as RootState).chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)

beforeEach(() => {
  vi.clearAllMocks()
  // Module-level store: entries would otherwise leak across tests (and across
  // reused queue ids like 'q1'), making the suite order-dependent.
  queuedSendStash.clear()
  for (const fn of Object.values(apiMocks)) fn.mockResolvedValue({ ok: true })
})

describe('useQueuedMessageActions — cancel', () => {
  it('hands the card text to the host composer, removes the card optimistically, and tells the server', async () => {
    const restoreDraft = vi.fn()
    const { get, store } = renderActions({ restoreDraft })
    act(() => { get().onCancel('q1') })
    // Plain text round-trips the parser unchanged, with nothing to re-stage.
    expect(restoreDraft).toHaveBeenCalledWith('run the tests', [])
    expect(apiMocks.cancelQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1')
    // Optimistic: the card is gone without waiting for the WS echo.
    expect(queueIdsIn(store)).toEqual(['q2'])
  })

  it('restores the pre-send composer state from the queue-id stash — typed text AND files', () => {
    // The card content is the LLM-facing serialization; the stash record is
    // what the user actually composed. A hit restores the raw text and
    // re-stages the files — lossless even for a spaced path no parser could
    // reconstruct from the wire text.
    const spaced = '/Users/me/Desktop/My Report.pdf'
    const sent = 'summarize this\n[attached_file 1] /Users/me/Desktop/My Report.pdf'
    const restoreDraft = vi.fn()
    const rows = [queued('q1', sent)]
    queuedSendStash.set('q1', { raw: 'summarize this', files: [spaced], sent })
    const { get } = renderActions({ rows, restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('summarize this', [spaced])
    // Consumed: a record restores exactly once.
    expect(queuedSendStash.has('q1')).toBe(false)
  })

  it('an entry edited after send fails the `sent` guard and falls to the parser', () => {
    // Same queue id, different content: restoring the pre-edit stash would
    // silently discard the edit, so the edited text must win.
    const restoreDraft = vi.fn()
    const rows = [queued('q1', 'actually, deploy instead')]
    queuedSendStash.set('q1', { raw: 'summarize this', files: ['/tmp/a.pdf'], sent: 'summarize this\n[attached_file 1] /tmp/a.pdf' })
    const { get } = renderActions({ rows, restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('actually, deploy instead', [])
  })

  it('a foreign card (no stash record) decomposes producer markers via the parser', () => {
    // Reload/another tab: no record exists, but a provably-lossless own-line
    // marker still comes back as typed text + a re-staged file.
    const restoreDraft = vi.fn()
    const rows = [queued('q1', 'summarize the report\n[attached_file 1] /tmp/report.docx')]
    const { get } = renderActions({ rows, restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('summarize the report', ['/tmp/report.docx'])
  })

  it('restores nothing when the host supplies no composer sink', () => {
    const { get, store } = renderActions({})
    act(() => { get().onCancel('q1') })
    expect(apiMocks.cancelQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1')
    expect(queueIdsIn(store)).toEqual(['q2'])
  })

  it('cancels a card the host draws no button for, without restoring an empty body', () => {
    const restoreDraft = vi.fn()
    const rows = [queued('q1', ''), queued('q2', 'then deploy')]
    const { get } = renderActions({ rows, restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(apiMocks.cancelQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1')
  })
})

describe('useQueuedMessageActions — edit', () => {
  it('trims, updates the card optimistically, and PATCHes the trimmed text', async () => {
    const { get, store } = renderActions({})
    act(() => { get().onEdit('q1', '  run the tests twice  ') })
    expect(apiMocks.editQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1', 'run the tests twice')
    const card = (store.getState() as RootState).chat.messages.find(m => m.meta?.queueId === 'q1')
    expect(card?.content).toBe('run the tests twice')
  })

  it('refuses a blank edit without touching the store or the server', () => {
    const { get, store } = renderActions({})
    act(() => { get().onEdit('q1', '   ') })
    expect(apiMocks.editQueuedMessage).not.toHaveBeenCalled()
    const card = (store.getState() as RootState).chat.messages.find(m => m.meta?.queueId === 'q1')
    expect(card?.content).toBe('run the tests')
  })
})

describe('useQueuedMessageActions — interrupt', () => {
  it('asks the server to interrupt that entry only, with no optimistic store change', () => {
    const { get, store } = renderActions({})
    act(() => { get().onInterrupt('q2') })
    expect(apiMocks.interruptSlot).toHaveBeenCalledWith('chat-1', 'q2')
    expect(apiMocks.cancelQueuedMessage).not.toHaveBeenCalled()
    expect(queueIdsIn(store)).toEqual(['q1', 'q2'])
  })
})

describe('useQueuedMessageActions — reorder', () => {
  it('submits the FULL order so a hidden system delivery is not demoted', () => {
    // The delivery sits between the two cards and is never drawn. Submitting only
    // the visible ids would let the backend re-append it at the tail.
    const sys = queued('sys1', '[Subagent completion event] Agent X completed ✅')
    const rows = [queued('q1', 'run the tests'), sys, queued('q2', 'then deploy')]
    const { get } = renderActions({ rows, visible: [rows[0], rows[2]] })
    act(() => { get().onReorder('q1', 'later') })
    expect(apiMocks.reorderQueuedMessages).toHaveBeenCalledWith('chat-1', ['q2', 'sys1', 'q1'])
  })

  it('ignores a reorder that would run off either end of the visible stack, or names no card', () => {
    const { get } = renderActions({})
    act(() => { get().onReorder('q1', 'next') })
    act(() => { get().onReorder('q2', 'later') })
    act(() => { get().onReorder('nope', 'later') })
    expect(apiMocks.reorderQueuedMessages).not.toHaveBeenCalled()
  })

  it('makes no optimistic store change — the server broadcast is authoritative', () => {
    const { get, store } = renderActions({})
    act(() => { get().onReorder('q2', 'next') })
    expect(apiMocks.reorderQueuedMessages).toHaveBeenCalledWith('chat-1', ['q2', 'q1'])
    expect(queueIdsIn(store)).toEqual(['q1', 'q2'])
  })
})

describe('useQueuedMessageActions — in-flight latch (#5891 item 2)', () => {
  it('latches the card while an interrupt is in flight and holds it until the row is retired', async () => {
    // An accepted interrupt is not finished when its response lands: the entry is
    // dequeued and started, and the card only goes away with the queue_pop frame.
    // Releasing on the response would re-enable the button inside that gap, and
    // the next click would interrupt the turn the first click just promoted.
    const d = deferred()
    apiMocks.interruptSlot.mockReturnValue(d.promise)
    const rows = [queued('q1', 'run the tests'), queued('q2', 'then deploy')]
    const { get, setQueue } = renderActions({ rows })
    act(() => { get().onInterrupt('q2') })
    await waitFor(() => expect(get().pendingIds.has('q2')).toBe(true))

    await act(async () => { d.resolve({ ok: true }) })
    // Still latched: the response arrived, the card has not gone yet.
    expect(get().pendingIds.has('q2')).toBe(true)

    // The frame lands and the row disappears.
    await act(async () => { setQueue([rows[0]]) })
    await waitFor(() => expect(get().pendingIds.has('q2')).toBe(false))
  })

  it('releases an interrupt immediately on rejection so the user can retry', async () => {
    // Nothing was promoted and the card is the same card, so holding it would
    // strand a control over an entry that is still queued.
    const d = deferred()
    apiMocks.interruptSlot.mockReturnValue(d.promise)
    const { get } = renderActions({})
    act(() => { get().onInterrupt('q2') })
    await waitFor(() => expect(get().pendingIds.has('q2')).toBe(true))
    await act(async () => { d.reject(new Error('offline')); await d.promise.catch(() => undefined) })
    await waitFor(() => expect(get().pendingIds.has('q2')).toBe(false))
  })

  it('latches cancel and edit only for their request, since their dispatch settles the card', async () => {
    const d = deferred()
    apiMocks.editQueuedMessage.mockReturnValue(d.promise)
    const { get } = renderActions({})
    act(() => { get().onEdit('q1', 'changed') })
    await waitFor(() => expect(get().pendingIds.has('q1')).toBe(true))
    await act(async () => { d.resolve({ ok: true }) })
    // No retirement to wait for: edit rewrote the card in place.
    await waitFor(() => expect(get().pendingIds.has('q1')).toBe(false))
  })

  it('latches each card independently', async () => {
    const first = deferred()
    const second = deferred()
    apiMocks.interruptSlot.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const rows = [queued('q1', 'run the tests'), queued('q2', 'then deploy')]
    const { get, setQueue } = renderActions({ rows })
    act(() => { get().onInterrupt('q1') })
    act(() => { get().onInterrupt('q2') })
    await waitFor(() => expect(get().pendingIds.has('q1')).toBe(true))
    expect(get().pendingIds.has('q2')).toBe(true)
    await act(async () => { first.resolve({ ok: true }) })
    await act(async () => { setQueue([rows[1]]) })
    await waitFor(() => expect(get().pendingIds.has('q1')).toBe(false))
    // The other card stays latched — one settled request must not unlock the rest.
    expect(get().pendingIds.has('q2')).toBe(true)
  })

  it('leaves the reorder arrows unlatched — QueueStack does not gate them on pendingIds', () => {
    const d = deferred()
    apiMocks.reorderQueuedMessages.mockReturnValue(d.promise)
    const { get } = renderActions({})
    act(() => { get().onReorder('q2', 'next') })
    expect(get().pendingIds.size).toBe(0)
  })

  it('releases the latch after a StrictMode mount/unmount/remount of its effects', async () => {
    // A request can outlive its host, which invites a `mounted` ref around the
    // release. Under the StrictMode this app renders in, the obvious form of that
    // guard latches false on the simulated unmount and never recovers, leaving
    // every card's controls disabled for the rest of the session after one click.
    // This is the test that catches it. Cancel is the action used here because its
    // latch settles on the response alone, so a failure to release can only be the
    // guard rather than a row that has not been retired yet.
    const d = deferred()
    apiMocks.cancelQueuedMessage.mockReturnValue(d.promise)
    const rows = [queued('q1', 'run the tests')]
    const store = makeStore('chat-1', rows)
    let actions: QueuedMessageActions | null = null
    function Probe() {
      actions = useQueuedMessageActions({ slot: 'chat-1', allQueued: rows, visibleQueued: rows })
      return null
    }
    render(
      <StrictMode>
        <Provider store={store}><Probe /></Provider>
      </StrictMode>,
    )
    act(() => { actions!.onCancel('q1') })
    await waitFor(() => expect(actions!.pendingIds.has('q1')).toBe(true))
    await act(async () => { d.resolve({ ok: true }) })
    await waitFor(() => expect(actions!.pendingIds.has('q1')).toBe(false))
  })
})

describe('useQueuedMessageActions — no active slot', () => {
  it('does nothing without a slot', () => {
    const { get, store } = renderActions({ slot: null })
    act(() => {
      get().onCancel('q1')
      get().onInterrupt('q1')
      get().onEdit('q1', 'changed')
      get().onReorder('q1', 'later')
    })
    for (const fn of Object.values(apiMocks)) expect(fn).not.toHaveBeenCalled()
    expect(queueIdsIn(store)).toEqual(['q1', 'q2'])
  })
})

describe('useQueuedMessageActions — callback identity', () => {
  it('keeps the four callbacks stable across a queue mutation so QueueStack does not repaint', async () => {
    const { get } = renderActions({})
    const before = get()
    act(() => { get().onEdit('q1', 'changed') })
    await waitFor(() => expect(apiMocks.editQueuedMessage).toHaveBeenCalled())
    const after = get()
    // The queue contents changed; the callbacks must not have. QueueStack is
    // memo-compared on these, and a fresh identity repaints the stack mid-animation.
    expect(after.onCancel).toBe(before.onCancel)
    expect(after.onInterrupt).toBe(before.onInterrupt)
    expect(after.onEdit).toBe(before.onEdit)
    expect(after.onReorder).toBe(before.onReorder)
  })
})
