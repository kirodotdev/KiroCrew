import { describe, it, expect, vi, beforeEach } from 'vitest'
import { StrictMode } from 'react'
import { render, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import { editQueuedMessage, applyQueueEdit } from '../store/chatSlice'
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

import { useQueuedMessageActions, queuedSendStash, preSendStash, adoptPreSendStash, stashPreSend, stashQueuedSend, retirePreSendStash, type QueuedMessageActions } from '../hooks/useQueuedMessageActions'

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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const withClient = (r: ChatMessage[]) => (
    <QueryClientProvider client={qc}>{wrap(r)}</QueryClientProvider>
  )
  const view = render(withClient(rows))
  return {
    store,
    get: () => actions!,
    slot,
    /** Re-render as the host would once a server frame changed the queue. */
    setQueue: (next: ChatMessage[]) => view.rerender(withClient(next)),
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

  /* When the HTTP receipt beat `queue_push`, the receipt
   * wrote `sent` as the sender's UN-redacted text and adoption then skipped the existing record, so
   * the cancel guard could not match a server-redacted card and the attachments were dropped. */
  it('restores attachments when the receipt beat `queue_push` on a redacted card', () => {
    const spaced = '/Users/me/Desktop/My Report.pdf'
    const llm = 'look at this\n[attached_file 1] /Users/me/Desktop/My Report.pdf'
    const redacted = 'look at this\n[attached_file 1] [image]'
    const restoreDraft = vi.fn()

    stashPreSend('s-1', { raw: 'look at this', files: [spaced], sent: llm })
    // The receipt lands FIRST and writes the queue-id record with its own un-redacted copy.
    stashQueuedSend('q1', { raw: 'look at this', files: [spaced], sent: llm })
    // `queue_push` lands second, carrying what the server actually shows on the card.
    adoptPreSendStash('s-1', 'q1', redacted)

    const { get } = renderActions({ rows: [queued('q1', redacted)], restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('look at this', [spaced])
  })

  it('hands back only text and files, the whole restore surface', () => {
    const restoreDraft = vi.fn()
    queuedSendStash.clear()
    const { get } = renderActions({ rows: [queued('q1', 'plain words')], restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('plain words', [])
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

  it('recovers the RAW text from card meta when no stash record was written', () => {
    // The unreadable-receipt path: the client never saw a queue_id, so nothing was
    // stashed, and the card carries the REDACTED text -- meta is the only raw source.
    const restoreDraft = vi.fn()
    const redacted = 'deploy with token [REDACTED: credential]'
    const rows = [{
      ...queued('q1', redacted),
      meta: { queueId: 'q1', rawSend: { text: 'deploy with token hunter2', files: ['/tmp/keys.txt'], sent: redacted } },
    } as ChatMessage]
    const { get } = renderActions({ rows, restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('deploy with token hunter2', ['/tmp/keys.txt'])
  })

  it('ignores card meta once the entry was EDITED after send', () => {
    // Negative control: the same guard the stash uses. Restoring the pre-edit raw
    // text here would silently discard the edit the user just made.
    const restoreDraft = vi.fn()
    const rows = [{
      ...queued('q1', 'actually, roll back'),
      meta: { queueId: 'q1', rawSend: { text: 'deploy with token hunter2', files: [], sent: 'deploy with token [REDACTED: credential]' } },
    } as ChatMessage]
    const { get } = renderActions({ rows, restoreDraft })
    act(() => { get().onCancel('q1') })
    expect(restoreDraft).toHaveBeenCalledWith('actually, roll back', [])
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
    await waitFor(() => expect(apiMocks.editQueuedMessage)
      .toHaveBeenCalledWith('chat-1', 'q1', 'run the tests twice', expect.any(String)))
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
    const qc2 = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(
      <StrictMode>
        <QueryClientProvider client={qc2}>
          <Provider store={store}><Probe /></Provider>
        </QueryClientProvider>
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


describe('adoptPreSendStash — the wire key the server actually broadcasts', () => {
  it('adopts on the snake_case queue_id a queue_push carries', () => {
    preSendStash.clear()
    queuedSendStash.clear()
    preSendStash.set('s-1', { raw: 'typed words', files: ['/tmp/a.pdf'], sent: 'typed words' })
    // Exactly the frame the backend broadcasts: `queue_id`, not `queueId`. Reading the camelCase
    // form yields undefined and the helper silently adopts nothing.
    const frame = { slot: 'chat-1', content: '[redacted]', ts: '1', queue_id: 'q-9', sendId: 's-1' }
    adoptPreSendStash((frame as { sendId?: string }).sendId, (frame as { queue_id?: string }).queue_id)
    expect(queuedSendStash.get('q-9')?.raw, 'the raw payload must reach the queue id').toBe('typed words')
    expect(queuedSendStash.get('q-9')?.files).toEqual(['/tmp/a.pdf'])
    expect(preSendStash.has('s-1'), 'the pre-send record is consumed').toBe(false)
  })

  it('does not overwrite a receipt-path record already keyed on that queue id', () => {
    preSendStash.clear()
    queuedSendStash.clear()
    queuedSendStash.set('q-9', { raw: 'better copy', sent: 'better copy' })
    preSendStash.set('s-1', { raw: 'fallback copy', sent: 'fallback copy' })
    adoptPreSendStash('s-1', 'q-9')
    expect(queuedSendStash.get('q-9')?.raw).toBe('better copy')
  })

  it('is inert without both identifiers', () => {
    preSendStash.clear()
    queuedSendStash.clear()
    preSendStash.set('s-1', { raw: 'x', sent: 'x' })
    adoptPreSendStash('s-1', undefined)
    adoptPreSendStash(undefined, 'q-9')
    expect(queuedSendStash.size).toBe(0)
    expect(preSendStash.has('s-1')).toBe(true)
  })
})


/* the stash was written on every send and deleted only on the `queue_push` path, so an
 * ordinary immediately-running send left its full prompt referenced for the tab's life. */
describe('preSendStash — retired on a definitive outcome, and bounded when unresolved', () => {
  it('retires the record so a non-queued send does not retain its prompt', () => {
    preSendStash.clear()
    stashPreSend('s-dispatched', { raw: 'a very long prompt', sent: 'a very long prompt' })
    expect(preSendStash.has('s-dispatched')).toBe(true)
    // What the queue_push path already did, now also done when no queue_push can follow.
    retirePreSendStash('s-dispatched')
    expect(preSendStash.has('s-dispatched'),
      'a send that never queues must not keep its prompt referenced').toBe(false)
  })

  it('drops no record a queue_push has not resolved yet', () => {
    preSendStash.clear()
    // Each of these is a send whose receipt was unreadable, so nothing has retired it -- every one
    // is still live and a late queue_push may still name any of them.
    for (let i = 0; i < 60; i++) {
      stashPreSend(`s-${i}`, { raw: `prompt ${i}`, sent: `prompt ${i}` })
    }
    expect(preSendStash.has('s-59'), 'the newest record is kept').toBe(true)
    expect(preSendStash.has('s-0'),
      'evicting the oldest LIVE record loses attachments a later cancel cannot rebuild').toBe(true)
  })

  it('replaces a rewritten record rather than keeping both', () => {
    preSendStash.clear()
    stashPreSend('s-first', { raw: 'original', sent: 'original' })
    for (let i = 0; i < 19; i++) stashPreSend(`s-pad-${i}`, { raw: 'x', sent: 'x' })
    stashPreSend('s-first', { raw: 'rewritten', sent: 'rewritten' })
    stashPreSend('s-overflow', { raw: 'y', sent: 'y' })
    expect(preSendStash.get('s-first')?.raw).toBe('rewritten')
    expect(preSendStash.has('s-first'), 'a rewrite must not be evicted next').toBe(true)
  })

  it('is inert on a retire with no sendId', () => {
    preSendStash.clear()
    stashPreSend('s-keep', { raw: 'keep me', sent: 'keep me' })
    retirePreSendStash(undefined)
    expect(preSendStash.has('s-keep')).toBe(true)
  })
})


/* `editQueuedMessage` left `meta.rawSend` in place, so the card carried the
 * payload it was SENT with after the user had edited it -- and `keep.rawSend` outlives rebuilds. */
describe('editQueuedMessage drops the pre-edit raw payload', () => {
  it('removes rawSend from the card it edits', () => {
    const redacted = 'deploy with token [REDACTED: credential]'
    const rows = [{
      ...queued('q1', redacted),
      meta: { queueId: 'q1', rawSend: { text: 'deploy with token hunter2', files: ['/tmp/keys.txt'], sent: redacted } },
    } as ChatMessage]
    const { store } = renderActions({ rows })
    expect((store.getState() as RootState).chat.messages[0].meta?.rawSend).toBeTruthy()

    act(() => {
      store.dispatch(editQueuedMessage({ slot: 'chat-1', queue_id: 'q1', content: 'roll back instead' }))
    })

    const row = (store.getState() as RootState).chat.messages[0]
    expect(row.content).toBe('roll back instead')
    expect(row.meta?.rawSend,
      'the payload the card was sent with is stale once the card is edited').toBeUndefined()
    // The rest of meta is untouched -- only the stale record leaves.
    expect(row.meta?.queueId).toBe('q1')
  })

  it('cancel after an edit restores the EDITED text, even when the edit matches the redacted form', () => {
    // The data-loss path the equality guard alone cannot close: edit the card back to exactly the
    // redacted wire text and `carried.sent === msg.content` holds again, restoring the secret.
    const restoreDraft = vi.fn()
    const redacted = 'deploy with token [REDACTED: credential]'
    const seed = [{
      ...queued('q1', 'some other text'),
      meta: { queueId: 'q1', rawSend: { text: 'deploy with token hunter2', files: ['/tmp/keys.txt'], sent: redacted } },
    } as ChatMessage]

    // Run the REAL reducer, then hand the hook the row it produced -- the harness feeds the hook
    // a static prop, so a dispatch alone would be invisible to it.
    const seedStore = makeStore('chat-1', seed)
    seedStore.dispatch(editQueuedMessage({ slot: 'chat-1', queue_id: 'q1', content: redacted }))
    const edited = (seedStore.getState() as RootState).chat.messages.filter(m => m.role === 'queued')

    const { get } = renderActions({ rows: edited, restoreDraft })
    act(() => { get().onCancel('q1') })

    expect(restoreDraft).toHaveBeenCalledWith(redacted, [])
    expect(restoreDraft, 'the pre-edit secret must not come back')
      .not.toHaveBeenCalledWith('deploy with token hunter2', ['/tmp/keys.txt'])
  })
})


/* an adopted stash kept the SENDER's raw text in `sent`, while the cancel
 * guard compares `sent` against the card's own content — which the server redacts. The guard
 * missed, cancel fell to the parser, and the masked text came back WITHOUT the attachments. */
describe('an adopted stash survives a redacted queue push', () => {
  beforeEach(() => { queuedSendStash.clear(); preSendStash.clear() })

  it('restores the raw text and files when the broadcast content was redacted', async () => {
    // The LLM-facing text the server broadcasts has the image @-token erased, so it differs
    // from what the sender held. That difference is the whole failure.
    const RAW = 'caption for @image.png'
    const REDACTED = 'caption for'
    stashPreSend('s-redact-1', { raw: RAW, files: ['image.png'], sent: RAW })
    adoptPreSendStash('s-redact-1', 'q-redact', REDACTED)

    const restoreDraft = vi.fn()
    const { get } = renderActions({ rows: [queued('q-redact', REDACTED)], restoreDraft })
    await act(async () => { get().onCancel('q-redact') })

    await waitFor(() => expect(restoreDraft).toHaveBeenCalled())
    expect(restoreDraft.mock.calls[0][0],
      'the raw text must come back, not the redacted broadcast').toBe(RAW)
    expect(restoreDraft.mock.calls[0][1],
      'the attachments are what the parser fallback cannot recover').toEqual(['image.png'])
  })

  it('does not restore pre-edit state after the entry was edited', async () => {
    const RAW = 'original with @image.png'
    stashPreSend('s-redact-2', { raw: RAW, files: ['image.png'], sent: RAW })
    adoptPreSendStash('s-redact-2', 'q-edited', 'original with')

    const restoreDraft = vi.fn()
    const { get } = renderActions({ rows: [queued('q-edited', 'original with')], restoreDraft })
    await act(async () => { get().onEdit('q-edited', 'a different instruction') })
    await act(async () => { get().onCancel('q-edited') })

    await waitFor(() => expect(restoreDraft).toHaveBeenCalled())
    expect(restoreDraft.mock.calls[0][0],
      'an edited card must not be clobbered with the pre-edit payload').not.toBe(RAW)
  })
})


/* `queue_push` can win the race against its own HTTP receipt. The adopted
 * record's `sent` is the server's BROADCAST content, which is what the cancel guard compares
 * against the card; the receipt's is the sender's un-redacted copy. The receipt used to overwrite
 * unconditionally, so on a redacted entry cancel fell to the parser and the attachments were
 * gone for good. */
describe('a receipt does not clobber a queue_push already adopted', () => {
  beforeEach(() => { queuedSendStash.clear(); preSendStash.clear() })

  it('keeps the adopted record, so a redacted cancel still restores the attachments', async () => {
    const RAW = 'ship it @plan.pdf'
    const REDACTED = 'ship it'            // the server erases the image/file @-token
    stashPreSend('s-race-1', { raw: RAW, files: ['plan.pdf'], sent: RAW })

    // queue_push lands FIRST and adopts, binding `sent` to the broadcast content.
    adoptPreSendStash('s-race-1', 'q-race', REDACTED)
    // ...then the slower HTTP receipt tries to write the sender's un-redacted copy.
    stashQueuedSend('q-race', { raw: RAW, files: ['plan.pdf'], sent: RAW })

    const restoreDraft = vi.fn()
    const { get } = renderActions({ rows: [queued('q-race', REDACTED)], restoreDraft })
    await act(async () => { get().onCancel('q-race') })

    await waitFor(() => expect(restoreDraft).toHaveBeenCalled())
    expect(restoreDraft.mock.calls[0][0],
      'the adopted record matches the card, so the raw text comes back').toBe(RAW)
    expect(restoreDraft.mock.calls[0][1],
      'the attachments are what the parser fallback cannot recover').toEqual(['plan.pdf'])
  })

  it('still writes the receipt record when no queue_push adopted first', async () => {
    // The ordinary ordering must be unchanged: with nothing adopted, the receipt is the record.
    stashQueuedSend('q-normal', { raw: 'plain text', files: ['a.txt'], sent: 'plain text' })
    const restoreDraft = vi.fn()
    const { get } = renderActions({ rows: [queued('q-normal', 'plain text')], restoreDraft })
    await act(async () => { get().onCancel('q-normal') })
    await waitFor(() => expect(restoreDraft).toHaveBeenCalled())
    expect(restoreDraft.mock.calls[0][1]).toEqual(['a.txt'])
  })
})

describe('a REMOTE queue_edit must retire the queue-id stash', () => {
  beforeEach(() => {
    queuedSendStash.clear()
    preSendStash.clear()
    vi.clearAllMocks()
  })

  it('restores the EDITED content after a remote edit, not the stashed pre-edit payload', async () => {
    // The stash holds what the user actually sent; the card's content is the server's redacted form,
    // so the cancel guard `stashed.sent === msg.content` passes on an UNEDITED entry.
    stashQueuedSend('q1', { raw: 'token=SECRET-ORIGINAL', files: ['/secret.pem'], sent: 'token=[redacted]' })

    const restoreDraft = vi.fn()
    const rows = [queued('q1', 'token=[redacted]')]
    const { get, store } = renderActions({ rows, restoreDraft })

    // A DIFFERENT client edited the entry. This is exactly what the WS `queue_edit` frame does --
    // useWebSocket dispatches this same owner, so the test drives the remote path.
    await act(async () => {
      await store.dispatch(applyQueueEdit({ slot: 'slot-1', queue_id: 'q1', content: 'token=[redacted]' }) as never)
    })

    act(() => { get().onCancel('q1') })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    const [text, files] = restoreDraft.mock.calls[0]
    expect(text, 'a remote edit makes the stash stale -- restoring it leaks the pre-edit secret')
      .not.toBe('token=SECRET-ORIGINAL')
    expect(files, 'the pre-edit attachments must not come back either').toEqual([])
  })

  it('still restores the stashed payload when NO edit happened', async () => {
    // Positive control: an invalidation fired unconditionally would satisfy the assertion above
    // while destroying the recovery this stash exists for.
    stashQueuedSend('q2', { raw: 'token=SECRET-ORIGINAL', files: ['/secret.pem'], sent: 'token=[redacted]' })
    const restoreDraft = vi.fn()
    const rows = [queued('q2', 'token=[redacted]')]
    const { get } = renderActions({ rows, restoreDraft })

    act(() => { get().onCancel('q2') })

    expect(restoreDraft).toHaveBeenCalledWith('token=SECRET-ORIGINAL', ['/secret.pem'])
  })
})

describe('a FAILED edit must leave the attachment stash recoverable', () => {
  const QID = 'q-failed-edit'
  const CARD = 'token=[redacted]'
  const RAW = 'token=SECRET-ORIGINAL'
  const FILES = ['/invoice.pdf']

  beforeEach(() => { queuedSendStash.clear(); vi.clearAllMocks() })

  it('still restores the queued attachments after the edit request rejects', async () => {
    // The stash is the ONLY copy of what a redacted card was sent with, so retiring it before the
    // server has taken the edit throws it away on exactly the path that still needs it.
    stashQueuedSend(QID, { raw: RAW, files: FILES, sent: CARD })
    const restoreDraft = vi.fn()
    const rows = [queued(QID, CARD)]
    const { get, setQueue } = renderActions({ rows, restoreDraft })

    apiMocks.editQueuedMessage.mockRejectedValueOnce(new Error('rejected by the server'))
    await act(async () => { get().onEdit(QID, 'token=[redacted] and more') })
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })

    // The server never took the edit, so a refetch puts the ORIGINAL entry back on screen.
    await act(async () => { setQueue([queued(QID, CARD)]) })
    act(() => { get().onCancel(QID) })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    const [text, files] = restoreDraft.mock.calls[0]
    expect(text, 'a failed edit changed nothing, so cancel must still recover the payload').toBe(RAW)
    expect(files, 'the attachments are the half no parser can reconstruct').toEqual(FILES)
  })

  it('retires the stash once the edit SUCCEEDS, so a later cancel cannot restore pre-edit text', async () => {
    // Positive control: keeping the stash unconditionally would satisfy the assertion above and
    // reinstate the leak the invalidation exists to close.
    stashQueuedSend(QID, { raw: RAW, files: FILES, sent: CARD })
    const restoreDraft = vi.fn()
    const rows = [queued(QID, CARD)]
    const { get, setQueue } = renderActions({ rows, restoreDraft })

    apiMocks.editQueuedMessage.mockResolvedValueOnce({ ok: true })
    await act(async () => { get().onEdit(QID, 'token=[redacted] edited') })
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })

    await act(async () => { setQueue([queued(QID, CARD)]) })
    act(() => { get().onCancel(QID) })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(restoreDraft.mock.calls[0][0],
      'the edit landed, so the pre-edit payload is stale and must not come back').not.toBe(RAW)
  })
})

describe('the queue edit runs inside the mutation lifecycle', () => {
  it('drives the server write through useMutation, not a hand-rolled promise chain', () => {
    // A `.then` on the request handled the server write outside React Query, so its success had no
    // lifecycle hook to hang the confirmation on and nothing observed its pending or error state.
    const src = readFileSync(resolve(process.cwd(), 'src/hooks/useQueuedMessageActions.ts'), 'utf8')
    expect(src, 'the edit is a mutation').toMatch(/useMutation\(/)
    expect(/editQueuedMessage\([^)]*\)\s*\.then/.test(src),
      'no hand-rolled continuation on the edit request').toBe(false)
    expect(src, 'and the confirmation is dispatched from its success hook')
      .toMatch(/onSuccess:[\s\S]{0,200}applyQueueEdit/)
  })
})

describe('a rejected queue edit is rolled back and reported', () => {
  it('restores the original text and surfaces the failure', async () => {
    // The entry still holds its ORIGINAL text server-side and will RUN that text, so a card left
    // showing the edit has the agent execute a prompt the user believes they replaced.
    apiMocks.editQueuedMessage.mockRejectedValueOnce(new Error('queue entry already running'))
    const { get, store } = renderActions({})
    const card = () => ((store.getState() as RootState).chat.messages
      .find(m => m.meta?.queueId === 'q1'))
    expect(card()?.content, 'premise: the card starts on its original text').toBe('run the tests')

    act(() => { get().onEdit('q1', 'run the tests twice') })

    await waitFor(() => expect(get().editError, 'the failure is reported, not swallowed').toBeTruthy())
    expect(get().editError).toContain('queue entry already running')
    expect(card()?.content, 'and the optimistic edit is rolled back').toBe('run the tests')
    expect(get().pendingIds.has('q1'), 'the latch still releases').toBe(false)

    act(() => { get().dismissEditError() })
    expect(get().editError).toBeNull()
  })

  it('reports nothing when the server takes the edit', async () => {
    // Positive control: an error surfaced unconditionally would satisfy the case above while
    // reporting a failure on every successful edit.
    const { get, store } = renderActions({})
    act(() => { get().onEdit('q1', 'run the tests twice') })
    await waitFor(() => expect(apiMocks.editQueuedMessage).toHaveBeenCalled())
    await waitFor(() => expect(((store.getState() as RootState).chat.messages
      .find(m => m.meta?.queueId === 'q1'))?.content).toBe('run the tests twice'))
    expect(get().editError, 'a taken edit reports nothing').toBeNull()
  })
})

describe('a lost edit response must not undo an edit the server already took', () => {
  it('leaves the confirmed text alone when the WS echo lands BEFORE the failure', async () => {
    // The commit is broadcast on a separate channel, so both orderings happen. With the echo first,
    // undoing the edit leaves text the agent is not running, and nothing re-syncs it.
    const d = deferred()
    apiMocks.editQueuedMessage.mockReturnValueOnce(d.promise)
    const { get, store } = renderActions({})
    const card = () => ((store.getState() as RootState).chat.messages
      .find(m => m.meta?.queueId === 'q1'))

    act(() => { get().onEdit('q1', 'the edited prompt') })
    await waitFor(() => expect(card()?.content).toBe('the edited prompt'))

    // The server took it and said so on the other channel, exactly as `useWebSocket` does.
    await act(async () => {
      store.dispatch(applyQueueEdit({ slot: 'chat-1', queue_id: 'q1', content: 'the edited prompt' }))
    })
    await act(async () => { d.reject(new Error('connection reset')); await Promise.resolve() })
    await waitFor(() => expect(get().pendingIds.has('q1')).toBe(false))

    expect(card()?.content,
      'the edit the agent will run must stay on the card').toBe('the edited prompt')
  })

  it('still rolls back when the failure is the only outcome', async () => {
    // Positive control: skipping the rollback whenever anything raced would leave the card showing an
    // edit the server never took -- the defect the rollback exists for.
    apiMocks.editQueuedMessage.mockRejectedValueOnce(new Error('rejected outright'))
    const { get, store } = renderActions({})
    const card = () => ((store.getState() as RootState).chat.messages
      .find(m => m.meta?.queueId === 'q1'))

    act(() => { get().onEdit('q1', 'never accepted') })

    await waitFor(() => expect(get().editError).toBeTruthy())
    expect(card()?.content, 'an unconfirmed edit is undone').toBe('run the tests')
  })
})

/* `onSuccess` reapplied this mutation's text unconditionally, so a delayed
 * local success landing AFTER another client's newer edit reinstated text the server is not running
 * -- silent until the next slot rebuild happened to refetch. The response and the broadcast are
 * different channels, so this ordering is ordinary, not exotic. */
describe('useQueuedMessageActions — a late edit response must not overwrite a newer edit', () => {
  const contentOf = (store: ReturnType<typeof makeStore>, id: string) =>
    (store.getState() as RootState).chat.messages.find(m => m.meta?.queueId === id)?.content

  it('leaves another client\u2019s later edit in place when this success arrives after it', async () => {
    const d = deferred()
    apiMocks.editQueuedMessage.mockReturnValue(d.promise)
    const h = renderActions({ rows: [queued('q1', 'run the tests')] })

    await act(async () => { h.get().onEdit('q1', 'run the tests twice') })
    expect(contentOf(h.store, 'q1'),
      'premise: the optimistic edit is on the card').toBe('run the tests twice')

    // A DIFFERENT client's later edit arrives on the broadcast channel, and IS the server's truth.
    await act(async () => {
      h.store.dispatch(applyQueueEdit({ slot: 'chat-1', queue_id: 'q1', content: 'deploy instead' }))
    })
    expect(contentOf(h.store, 'q1'), 'premise: the remote edit landed').toBe('deploy instead')

    // Only now does this client's own success land, answering an edit that is no longer current.
    await act(async () => { d.resolve({ ok: true, content: 'run the tests twice' }) })

    await waitFor(() => expect(contentOf(h.store, 'q1'),
      'a stale success must not reinstate text the server is not running').toBe('deploy instead'))
  })

  it('still confirms an edit that no newer edit superseded', async () => {
    // Mutation control: refusing every success would satisfy the assertion above while leaving the
    // card permanently unconfirmed, so the ordinary path must still apply the server's own text.
    const d = deferred()
    apiMocks.editQueuedMessage.mockReturnValue(d.promise)
    const h = renderActions({ rows: [queued('q1', 'run the tests')] })

    await act(async () => { h.get().onEdit('q1', 'run the tests twice') })
    await act(async () => { d.resolve({ ok: true, content: 'run the tests twice [attached_file 1]' }) })

    await waitFor(() => expect(contentOf(h.store, 'q1'),
      'the server\u2019s normalized text is what the card must show').toBe('run the tests twice [attached_file 1]'))
  })
})

/* a successful edit DELETED the queue-keyed record, so a later cancel fell
 * to the strict parser -- which cannot claim a path containing a space back out of the card's text,
 * destroying the attachment. Editing then changing your mind is an ordinary flow, not an exotic one. */
describe('useQueuedMessageActions — a cancel AFTER a successful edit still recovers attachments', () => {
  const metaOf = (store: ReturnType<typeof makeStore>, id: string) =>
    (store.getState() as RootState).chat.messages.find(m => m.meta?.queueId === id)?.meta

  it('restores the EDITED text together with the spaced attachment path', async () => {
    const restoreDraft = vi.fn()
    // What the send receipt wrote. The space is the whole point: the parser refuses to claim it,
    // and a real entry carries the marker line, which is how the path travels at all.
    const ATT = '/tmp/Q3 report.pdf'
    const before = `read the draft\n[attached_file 1] ${ATT}`
    const after = `read the final draft\n[attached_file 1] ${ATT}`
    const h = renderActions({ rows: [queued('q1', before)], restoreDraft })
    stashQueuedSend('q1', { raw: before, files: [ATT], sent: before })

    apiMocks.editQueuedMessage.mockResolvedValue({ ok: true, content: after })
    await act(async () => { h.get().onEdit('q1', after) })
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the server has taken the edit').toBeUndefined())
    // The host re-renders the card with the text the server confirmed.
    h.setQueue([queued('q1', after)])

    await act(async () => { h.get().onCancel('q1') })

    expect(restoreDraft,
      'the attachment must survive an edit, and the text restored must be the EDITED one')
      .toHaveBeenCalledWith(after, [ATT])
  })

  it('survives this tab receiving its OWN queue_edit echo, which the server does not suppress', async () => {
    // `_send_ws_all` excludes no sender, so the author's socket gets the broadcast for its own
    // edit. Retiring the record on any echo therefore destroyed one this tab had just re-pointed.
    const restoreDraft = vi.fn()
    const ATT = '/tmp/Q3 report.pdf'
    const before = `read the draft\n[attached_file 1] ${ATT}`
    const after = `read the final draft\n[attached_file 1] ${ATT}`
    const h = renderActions({ rows: [queued('q1', before)], restoreDraft })
    stashQueuedSend('q1', { raw: before, files: [ATT], sent: before })

    apiMocks.editQueuedMessage.mockResolvedValue({ ok: true, content: after })
    await act(async () => { h.get().onEdit('q1', after) })
    // The id the request was sent under: only the echo naming it is ours.
    const echoId = apiMocks.editQueuedMessage.mock.calls[0][3] as string
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the server has taken the edit').toBeUndefined())
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: after, editId: echoId }) as never)
    })
    h.setQueue([queued('q1', after)])

    await act(async () => { h.get().onCancel('q1') })

    expect(restoreDraft, 'the author\u2019s own echo must not retire its own record')
      .toHaveBeenCalledWith(after, [ATT])
  })

  it('drops an attachment the edit REMOVED, so a blind retry cannot resend it', async () => {
    // Deleting a marker prunes that attachment server-side. Carrying `files` through unchanged put
    // the removed file back in the composer, where a retry would send it again.
    const KEPT = '/tmp/Q3 report.pdf'
    const GONE = '/tmp/secret notes.pdf'
    const before = `read these\n[attached_file 1] ${KEPT}\n[attached_file 2] ${GONE}`
    const after = `read these\n[attached_file 1] ${KEPT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', before)], restoreDraft })
    stashQueuedSend('q1', { raw: before, files: [KEPT, GONE], sent: before })

    apiMocks.editQueuedMessage.mockResolvedValue({ ok: true, content: after })
    await act(async () => { h.get().onEdit('q1', after) })
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the server has taken the edit').toBeUndefined())
    h.setQueue([queued('q1', after)])

    await act(async () => { h.get().onCancel('q1') })

    const [, files] = restoreDraft.mock.calls[0]
    expect(files, 'the removed attachment must not come back').not.toContain(GONE)
    // Control: filtering everything would satisfy the line above while destroying the recovery.
    expect(files, 'the surviving spaced path must still be recovered losslessly').toContain(KEPT)
  })

  it('restores the text the user SUBMITTED, not the redacted form the card shows', async () => {
    // The server returns and broadcasts a REDACTED display form. Writing that into `raw` handed the
    // composer back the redaction, so the credential the user actually typed was gone for good.
    const ATT = '/tmp/Q3 report.pdf'
    const submitted = `token=SECRET-EDITED\n[attached_file 1] ${ATT}`
    const redacted = `token=[redacted]\n[attached_file 1] ${ATT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', 'token=old')], restoreDraft })
    stashQueuedSend('q1', { raw: 'token=old', files: [ATT], sent: 'token=old' })

    apiMocks.editQueuedMessage.mockResolvedValue({ ok: true, content: redacted })
    await act(async () => { h.get().onEdit('q1', submitted) })
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the server has taken the edit').toBeUndefined())
    h.setQueue([queued('q1', redacted)])

    await act(async () => { h.get().onCancel('q1') })

    const [text, files] = restoreDraft.mock.calls[0]
    expect(text, 'the composer must get back what the user submitted').toBe(submitted)
    // Control: the record must still be USED, so `sent` has to match the card's redacted text.
    expect(files, 'and the attachment must still come back with it').toContain(ATT)
  })

  it('settles the record from its OWN echo when that echo beats the HTTP success', async () => {
    // A credential-only edit redacts to the SAME display text, so the cancel guard still passes on
    // the stale record. The echo's own frame clears `editPending`, so the success cannot restash.
    const ATT = '/tmp/Q3 report.pdf'
    const display = `token=[redacted]\n[attached_file 1] ${ATT}`
    const oldRaw = `token=OLD-SECRET\n[attached_file 1] ${ATT}`
    const submitted = `token=NEW-SECRET\n[attached_file 1] ${ATT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', display)], restoreDraft })
    stashQueuedSend('q1', { raw: oldRaw, files: [ATT], sent: display })

    const d = deferred()
    apiMocks.editQueuedMessage.mockReturnValue(d.promise)
    await act(async () => { h.get().onEdit('q1', submitted) })
    // The id the request was sent under: only the echo naming it is ours.
    const echoId = apiMocks.editQueuedMessage.mock.calls[0][3] as string

    // The server broadcasts to every client, so this tab's own echo can land before its response.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: display, editId: echoId }) as never)
    })
    await act(async () => { d.resolve({ ok: true, content: display }) })
    h.setQueue([queued('q1', display)])

    await act(async () => { h.get().onCancel('q1') })

    const [text, files] = restoreDraft.mock.calls[0]
    expect(text, 'the echo must settle the record, or the pre-edit secret comes back').toBe(submitted)
    // Control: the record must still be USED, so `sent` has to match the card's redacted text.
    expect(files, 'and the attachment must come back with it').toContain(ATT)
  })

  it('lets a LATER remote edit retire the record even when the success beat the echo', async () => {
    // HTTP-first leaves the authorship mark set, and nothing else cleared it -- so a genuine remote
    // edit could not retire the record, and a cancel handed this tab's own text back for it.
    const ATT = '/tmp/Q3 report.pdf'
    const mineText = `token=MINE-SECRET\n[attached_file 1] ${ATT}`
    // Both edits redact to the SAME display form, so the cancel guard passes on a stale record.
    const display = `token=[redacted]\n[attached_file 1] ${ATT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', display)], restoreDraft })
    stashQueuedSend('q1', { raw: 'token=OLD-SECRET', files: [ATT], sent: display })

    apiMocks.editQueuedMessage.mockResolvedValue({ ok: true, content: display })
    await act(async () => { h.get().onEdit('q1', mineText) })
    // The id the request was sent under: only the echo naming it is ours.
    const echoId = apiMocks.editQueuedMessage.mock.calls[0][3] as string
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the success landed BEFORE any echo').toBeUndefined())

    // This tab's own echo, which the mark exists to survive.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: display, editId: echoId }) as never)
    })
    // Then a DIFFERENT client edits the entry: this frame must be able to retire the record.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: display }) as never)
    })
    h.setQueue([queued('q1', display)])

    await act(async () => { h.get().onCancel('q1') })

    const [text] = restoreDraft.mock.calls[0]
    expect(text, 'a remote edit must retire this tab\u2019s record').not.toBe(mineText)
  })

  it('settles a DELAYED echo after a rollback, so a lost response cannot cost the spaced path', async () => {
    // The server commits the edit but the response is lost, so the mutation rejects and rolls back --
    // which clears the ROW's `editPending`. The identity kept on the record is what settles the echo.
    const ATT = '/tmp/Q3 report.pdf'
    const before = `read the draft\n[attached_file 1] ${ATT}`
    const display = `read the FINAL draft\n[attached_file 1] ${ATT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', before)], restoreDraft })
    stashQueuedSend('q1', { raw: before, files: [ATT], sent: before })

    apiMocks.editQueuedMessage.mockRejectedValue(new Error('connection lost'))
    await act(async () => { h.get().onEdit('q1', display) })
    // The id the request was sent under: only the echo naming it is ours.
    const echoId = apiMocks.editQueuedMessage.mock.calls[0][3] as string
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the failure rolled the optimistic edit back').toBeUndefined())

    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: display, editId: echoId }) as never)
    })
    h.setQueue([queued('q1', display)])

    await act(async () => { h.get().onCancel('q1') })

    const [text, files] = restoreDraft.mock.calls[0]
    expect(files, 'the parser cannot claim a path holding a space, so the record must answer')
      .toContain(ATT)
    expect(text, 'and the text restored is the committed edit').toBe(display)
  })

  it('ignores a CONCURRENT editor\u2019s frame and settles only the echo naming our own edit', async () => {
    // Every `queue_edit` frame arrives as `{queue_id, content}`, so a foreign tab's edit was settled
    // as though it were ours -- rebinding the record and spending the identity our own echo needed.
    const ATT = '/tmp/Q3 report.pdf'
    const before = `read the draft\n[attached_file 1] ${ATT}`
    const mineText = `read MY draft\n[attached_file 1] ${ATT}`
    const theirs = `read THEIR draft\n[attached_file 1] ${ATT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', before)], restoreDraft })
    stashQueuedSend('q1', { raw: before, files: [ATT], sent: before })

    const d = deferred()
    apiMocks.editQueuedMessage.mockReturnValue(d.promise)
    await act(async () => { h.get().onEdit('q1', mineText) })
    // The id the request was actually sent under, read back rather than assumed. Falls back so the
    // BEHAVIOURAL assertions below are what fail on an uncorrelated tree, not this read.
    const editId = (apiMocks.editQueuedMessage.mock.calls[0][3] as string | undefined) ?? 'absent'

    // ANOTHER tab's edit lands first, naming no edit of ours.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: theirs }) as never)
    })
    expect(queuedSendStash.get('q1')?.pendingEdit,
      'a concurrent editor must not spend our edit identity').toBe(mineText)

    // Now OUR echo, which names our id.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: mineText, editId }) as never)
    })
    await act(async () => { d.resolve({ ok: true, content: mineText }) })
    h.setQueue([queued('q1', mineText)])

    await act(async () => { h.get().onCancel('q1') })

    const [text, files] = restoreDraft.mock.calls[0]
    expect(text, 'our own edit is what comes back').toBe(mineText)
    expect(files, 'with the spaced path the parser cannot claim').toContain(ATT)
    expect(editId, 'and the request carried a real correlation id, not the fallback').not.toBe('absent')
  })

  it('retires on a later remote edit even when our own echo NEVER arrives', async () => {
    // The response settles the record but the echo is lost, so nothing spends the pending text --
    // and while it was retained the record could never retire, answering a cancel with stale data.
    const ATT = '/tmp/Q3 report.pdf'
    const mineText = `token=MINE-SECRET\n[attached_file 1] ${ATT}`
    const display = `token=[redacted]\n[attached_file 1] ${ATT}`
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', display)], restoreDraft })
    stashQueuedSend('q1', { raw: 'token=OLD-SECRET', files: [ATT], sent: display })

    apiMocks.editQueuedMessage.mockResolvedValue({ ok: true, content: display })
    await act(async () => { h.get().onEdit('q1', mineText) })
    await waitFor(() => expect(metaOf(h.store, 'q1')?.editPending,
      'premise: the response landed').toBeUndefined())

    // No echo for OUR edit ever arrives. A DIFFERENT client then edits the entry.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: display }) as never)
    })
    h.setQueue([queued('q1', display)])

    await act(async () => { h.get().onCancel('q1') })

    const [text] = restoreDraft.mock.calls[0]
    expect(text, 'a remote edit must be able to retire our settled record').not.toBe(mineText)
  })

  it('still refuses a record whose text is NOT what the card shows', async () => {
    // Mutation control: re-pointing the record unconditionally would satisfy the test above while
    // letting a record from another edit clobber the composer, so the guard must still reject it.
    const restoreDraft = vi.fn()
    const h = renderActions({ rows: [queued('q1', 'read the draft')], restoreDraft })
    stashQueuedSend('q1', { raw: 'not this text', files: ['/tmp/Q3 report.pdf'], sent: 'a different send' })

    await act(async () => { h.get().onCancel('q1') })

    expect(restoreDraft).toHaveBeenCalled()
    expect(restoreDraft.mock.calls[0][0],
      'a record that does not match the card must not be restored verbatim').not.toBe('not this text')
  })

  it('lets a concurrent remote edit retire the record after OUR edit failed', async () => {
    // `rollbackQueueEdit` clears the ROW's editPending but not the stash, so the record kept a
    // `pendingEdit` string forever and `retireRemoteQueueEdit` was never allowed to delete it.
    const shown = 'the ORIGINAL text\n[attached_file 1] /tmp/Q3 report.pdf'
    const rows = [queued('q1', shown)]
    queuedSendStash.set('q1', { raw: 'the ORIGINAL text', files: ['/tmp/Q3 report.pdf'], sent: shown })
    const restoreDraft = vi.fn()
    apiMocks.editQueuedMessage.mockRejectedValueOnce(new Error('connection lost'))
    const h = renderActions({ rows, restoreDraft })

    await act(async () => { await h.get().onEdit('q1', 'text I typed that never landed') })
    await waitFor(() => expect(apiMocks.editQueuedMessage).toHaveBeenCalled())

    // A DIFFERENT client edits the same entry: its frame carries no id of ours, so the echo settler
    // declines it and the retire path runs.
    await act(async () => {
      await h.store.dispatch(applyQueueEdit(
        { slot: 'chat-1', queue_id: 'q1', content: 'text SOMEONE ELSE typed' } as never) as never)
    })

    expect(queuedSendStash.get('q1'),
      'a record kept past our failed edit answers a later cancel with pre-edit text').toBeUndefined()

    act(() => { h.get().onCancel('q1') })
    expect(restoreDraft.mock.calls[0]?.[0] ?? '',
      'cancel must not hand back the text the other client replaced')
      .not.toBe('the ORIGINAL text')
  })

  it('still settles a DELAYED echo of the failed request, keeping the spaced path', async () => {
    // Positive control, and the reason the fix does not simply clear `pendingEdit`: the echo of the
    // failed request still arrives, and settling it is what recovers a path no parser can rebuild.
    const shown = 'the ORIGINAL text\n[attached_file 1] /tmp/Q3 report.pdf'
    const rows = [queued('q1', shown)]
    queuedSendStash.set('q1', { raw: 'the ORIGINAL text', files: ['/tmp/Q3 report.pdf'], sent: shown })
    apiMocks.editQueuedMessage.mockRejectedValueOnce(new Error('connection lost'))
    const h = renderActions({ rows })

    await act(async () => { await h.get().onEdit('q1', 'edited, still attached' + '\n[attached_file 1] /tmp/Q3 report.pdf') })
    const ourId = apiMocks.editQueuedMessage.mock.calls[0][3] as string

    await act(async () => {
      await h.store.dispatch(applyQueueEdit({
        slot: 'chat-1', queue_id: 'q1', content: 'edited, still attached' + '\n[attached_file 1] /tmp/Q3 report.pdf', editId: ourId,
      } as never) as never)
    })

    expect(queuedSendStash.get('q1')?.files,
      'our own delayed echo must re-point the record, not lose its attachments')
      .toEqual(['/tmp/Q3 report.pdf'])
  })

  it('refuses the stash while THIS tab\u2019s edit is still outstanding, even on matching text', () => {
    // `sent` matches the card exactly, so a content-only guard passes. The row's own flag is no help:
    // a remote frame clears it while this tab's edit is still in flight, which is the reachable gap.
    const shown = 'review this\n[attached_file 1] /tmp/plan.pdf'
    const rows = [queued('q1', shown)]
    queuedSendStash.set('q1', {
      raw: 'review this @/tmp/plan.pdf', files: ['/tmp/plan.pdf'], sent: shown,
      pendingEdit: 'text we submitted and never saw settle', pendingEditId: 'edit-outstanding',
    })
    const restoreDraft = vi.fn()
    const { get } = renderActions({ rows, restoreDraft })

    act(() => { get().onCancel('q1') })

    expect(restoreDraft).toHaveBeenCalled()
    expect(restoreDraft.mock.calls[0][0],
      'an outstanding edit means this record cannot be vouched for by its text alone')
      .not.toBe('review this @/tmp/plan.pdf')
  })

  it('still uses the stash when no edit identity is outstanding', () => {
    // Control: a settle spends the pending text, so the ordinary recovery -- including a spaced path
    // the parser cannot rebuild -- must keep working, or the guard bought correctness by disabling it.
    const spaced = '/tmp/Q3 report.pdf'
    const shown = 'summarize this\n[attached_file 1] /tmp/Q3 report.pdf'
    const rows = [queued('q1', shown)]
    queuedSendStash.set('q1', { raw: 'summarize this', files: [spaced], sent: shown })
    const restoreDraft = vi.fn()
    const { get } = renderActions({ rows, restoreDraft })

    act(() => { get().onCancel('q1') })

    expect(restoreDraft).toHaveBeenCalledWith('summarize this', [spaced])
  })
})
