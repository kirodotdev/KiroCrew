/**
 * A slot refetch must not swallow the optimistic user bubble.
 *
 * Both refetch reducers rebuild `messages` from the fetched page and preserve
 * only an `assistant`/`streaming` tail (`switchSlot`) or `permission` cards
 * (`refreshSlot`), so a refetch landing between the composer's optimistic append
 * and the server's own append used to DROP the just-sent message.
 *
 * The invariant pinned here: retention covers a bubble the server has NOT yet shown
 * back — a positive `pendingServerRow` marker, and nothing else. No clock, no dispatch
 * order and no row count is consulted, because none of them says when the SERVER took
 * its snapshot. It is retired by exactly two things: the row's own identity appearing
 * in a FETCHED PAGE (surviving a merge is a rescue, not a receipt), or an explicit
 * outcome via `clearPendingServerRow`. A refused or queued send retires it early; a
 * transport error does NOT, since a lost response is no proof of non-delivery. A
 * superseded same-slot response is a no-op.
 *
 * Duplicate-safety is pinned per path, so a fix that blindly re-appends fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { retainedSend, switchSlot, refreshSlot, setActiveSlot, appendMessage, appendSlotMessage, warmSlotCache, clearPendingServerRow, confirmOptimisticSend, markDeliveryUnknown, sseChatMessage, appendQueuedMessage } from './chatSlice'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ immutableCheck: false, serializableCheck: false }),
  })
}

const detail = vi.mocked(api.chatSlotDetail)

const SLOT = 'chat-1-1788026016'
const SEND_ID = 's-abc123-def456'
const FRESH_SLOT = 'chat-9-1788026500'

/** A slot-detail response. `messages` is what the SERVER has persisted so far. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- structural stand-in for the api client's response type
const page = (messages: unknown[], running = true): any => ({
  messages, running, stopping: false, has_more: false, total: messages.length, next_before: 0, queue: [],
})

// One switchSlot costs TWO fetches with no retained server total: the bounded page,
// then an unbounded retry, since an unknown prior total cannot prove overlap.
const servePage = (p: unknown) => {
  detail.mockResolvedValueOnce(p as never)
  detail.mockResolvedValue(p as never)
}

/** The row the server appends for a dashboard send: same `sendId`, plus a
 *  server-minted `mid`. This is the shape that must reconcile, not duplicate. */
const serverUserRow = { role: 'user', content: 'hello', ts: '2026-08-29T17:54:01.986Z', meta: retainedSend({ sendId: SEND_ID, mid: 'm-1' }) }

/** Seed a store whose active slot holds exactly one unconfirmed user bubble,
 *  the state the composer leaves behind between send and receipt. */
function storeWithOptimisticBubble() {
  const store = makeStore()
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID }) }))
  const seeded = store.getState().chat.messages
  // `appendMessage` is what sets the flag (from `sendId`), so guard the premise
  // rather than assume it.
  expect(seeded).toHaveLength(1)
  expect(seeded[0].meta?.optimistic).toBe(true)
  return store
}

const userRows = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.filter(m => m.role === 'user')

describe('switchSlot.fulfilled keeps an unconfirmed user bubble', () => {
  beforeEach(() => vi.clearAllMocks())

  it('does not drop the bubble when the fetched page predates the server append', async () => {
    const store = storeWithOptimisticBubble()
    // The server has nothing yet — the exact window the bug lives in.
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('hello')
    expect(rows[0].meta?.sendId).toBe(SEND_ID)
    // The blank-pane symptom is exactly "messages emptied", so pin that too.
    expect(store.getState().chat.messages.length).toBeGreaterThan(0)
  })

  it('does not duplicate the bubble once the page contains the row', async () => {
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([serverUserRow]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(1)
  })
})

describe('refreshSlot.fulfilled keeps an unconfirmed user bubble', () => {
  beforeEach(() => vi.clearAllMocks())

  it('does not drop the bubble when the fetched page predates the server append', async () => {
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([]))

    await store.dispatch(refreshSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('hello')
    expect(rows[0].meta?.sendId).toBe(SEND_ID)
  })

  it('does not duplicate the bubble once the page contains the row', async () => {
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([serverUserRow]))

    await store.dispatch(refreshSlot(SLOT))

    expect(userRows(store)).toHaveLength(1)
  })
})

describe('a re-attached send keeps its position relative to later local rows', () => {
  beforeEach(() => vi.clearAllMocks())

  it('stays ABOVE a thinking row that arrived after it', async () => {
    // The reported ordering bug: a tail append renders reasoning above its prompt.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendMessage({ role: 'thinking', content: 'reasoning...', cls: '', ts: '2026-08-29T17:54:02.100Z' }))
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    const roles = store.getState().chat.messages.map(m => m.role)
    expect(roles).toContain('user')
    expect(roles).toContain('thinking')
    expect(roles.indexOf('user')).toBeLessThan(roles.indexOf('thinking'))
  })

  it('lands after the history it followed, not at the very front', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'earlier', cls: '', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-old' } }))
    store.dispatch(appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID }) }))
    store.dispatch(appendMessage({ role: 'thinking', content: 'reasoning...', cls: '', ts: '2026-08-29T17:54:02.100Z' }))
    // The page carries the history row but not the unconfirmed send.
    detail.mockResolvedValue(page([{ role: 'user', content: 'earlier', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-old' } }]))

    await store.dispatch(switchSlot(SLOT))

    const contents = store.getState().chat.messages.map(m => m.content)
    expect(contents.indexOf('earlier')).toBeLessThan(contents.indexOf('hello'))
  })
})

describe('a failed send stops being re-attachable', () => {
  beforeEach(() => vi.clearAllMocks())

  it('clearPendingServerRow lets a refetch garbage-collect the row', async () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(clearPendingServerRow({ slot: SLOT, sendId: SEND_ID }))
    // Without this the phantom would survive every refetch and the slot cache.
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(0)
  })

  it('leaves the pending flag set — a refusal is not a receipt', () => {
    // Pins the send-confirm contract: `optimistic` is delivery state, so only
    // re-attachment may key on the retention marker.
    const store = storeWithOptimisticBubble()
    store.dispatch(clearPendingServerRow({ slot: SLOT, sendId: SEND_ID }))
    expect(store.getState().chat.messages[0].meta?.optimistic).toBe(true)
    expect(store.getState().chat.messages[0].meta?.pendingServerRow).toBe(false)
  })

  it('leaves an unrelated pending send alone', () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(clearPendingServerRow({ slot: SLOT, sendId: 's-someone-else' }))
    expect(store.getState().chat.messages[0].meta?.pendingServerRow).toBe(true)
    expect(store.getState().chat.messages[0].meta?.optimistic).toBe(true)
  })
})

describe('a queued twin owns the message, even when its content is redacted', () => {
  beforeEach(() => vi.clearAllMocks())

  it('drops the bubble when the queue-side content is REDACTED', async () => {
    // The queue payload redacts content for display, so no content join can pair
    // the twins. The receipt is what says "queued", and it survives redaction.
    const store = storeWithOptimisticBubble()
    store.dispatch(clearPendingServerRow({ slot: SLOT, sendId: SEND_ID }))
    detail.mockResolvedValue({ ...page([]), queue: [{ content: '[REDACTED]', id: 'q-1' }] })

    await store.dispatch(switchSlot(SLOT))

    const msgs = store.getState().chat.messages
    expect(msgs.filter(m => m.role === 'queued')).toHaveLength(1)
    expect(msgs.filter(m => m.role === 'user')).toHaveLength(0)
  })

  it('still re-attaches a send that was never queued', async () => {
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue({ ...page([]), queue: [{ content: '[REDACTED]', id: 'q-2' }] })

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(1)
  })
})

describe('a queued receipt the client could not read still releases the retained row', () => {
  beforeEach(() => vi.clearAllMocks())

  it('releases retention when queue_push names the send, so no refetch re-attaches it', async () => {
    // The POST receipt is the ONLY thing that says "queued", so a 2xx whose body will not
    // parse never fires the release and the row re-attaches for the tab's life.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-9', sendId: SEND_ID }))
    expect(store.getState().chat.messages.some(m => m.role === 'user' && m.meta?.pendingServerRow),
      'the queued twin owns the message, so no retained user row may survive').toBe(false)

    detail.mockResolvedValue({ ...page([]), queue: [{ content: '[REDACTED]', id: 'q-9' }] })
    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store), 'a released row must not come back on refetch').toHaveLength(0)
  })

  it('leaves NO duplicate user bubble beside the queued card', () => {
    // The queued card represents the message while it waits, so keeping the bubble too
    // renders the send twice -- and cancelling the card would strand it.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-12', sendId: SEND_ID }))

    const msgs = store.getState().chat.messages
    expect(msgs.filter(m => m.role === 'user'), 'the optimistic twin must be gone').toHaveLength(0)
    expect(msgs.filter(m => m.role === 'queued'), 'the queued card is what remains').toHaveLength(1)
  })

  it('expands a collapsed PASTE into the recovery text', () => {
    // The bubble holds only a token; the composer sink takes no block channel, so an
    // unexpanded store hands cancel a dead reference instead of the pasted content.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    const body = 'line one\nline two\nline three'
    store.dispatch(appendMessage({
      role: 'user',
      content: 'review this [ Paste #1 · 3 lines ]',
      cls: '',
      ts: '2026-08-29T17:54:01.900Z',
      meta: retainedSend({ sendId: SEND_ID, pastes: [{ id: 'p1', seq: 1, lines: 3, content: body }] }),
    }))
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-15', sendId: SEND_ID }))

    const card = store.getState().chat.messages.find(m => m.role === 'queued')
    const raw = card?.meta?.rawSend as { text?: string } | undefined
    expect(raw?.text, 'the pasted body must be recoverable, not just its token').toContain(body)
  })

  it('leaves the CANONICAL server row intact when the queue event is delayed', () => {
    // HTTP hydration can overtake the WS frames, so the persisted row lands first. It
    // carries the same sendId, and deleting it loses text the server already confirmed.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'ship the release', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: { sendId: SEND_ID, mid: 'm-77' } }))

    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-18', sendId: SEND_ID }))

    const users = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(users, 'the confirmed row must survive a late queue_push').toHaveLength(1)
    expect(users[0].content, 'and keep its real text, not the redacted mask').toBe('ship the release')
  })

  it('still removes the client\'s OWN unconfirmed row', () => {
    // Negative control: the guard must not make the dedup vacuous, or the queued card
    // and the optimistic bubble both render and the send shows twice.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-19', sendId: SEND_ID }))

    expect(userRows(store), 'the optimistic twin is still a duplicate').toHaveLength(0)
  })

  it('keeps queued-card recovery metadata across a REFETCH', async () => {
    // Hydration strips queued rows and rebuilds them from the server `queue`, which
    // carries no `rawSend`: a bare rebuild sends cancel back to the redacted text.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-16', sendId: SEND_ID }))
    const before = store.getState().chat.messages.find(m => m.role === 'queued')
    expect((before?.meta?.rawSend as { text?: string } | undefined)?.text, 'premise: the card carries recovery text').toBe('hello')

    servePage({ ...page([]), queue: [{ content: '[REDACTED]', id: 'q-16' }] })
    await store.dispatch(switchSlot(SLOT))

    const after = store.getState().chat.messages.find(m => m.role === 'queued')
    const raw = after?.meta?.rawSend as { text?: string; files?: string[] } | undefined
    expect(raw?.text, 'the raw draft must survive the rebuild').toBe('hello')
    expect(after?.meta?.sendId, 'and the send id, which releases the retained row').toBe(SEND_ID)
  })

  it('keeps ATTACHMENTS on the recovery metadata across a REFETCH', async () => {
    // Files are the half no parser can reconstruct from the wire text, so losing them
    // on rebuild is silent: the composer comes back with a path-less draft.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    const spaced = '/Users/me/Desktop/My Report.pdf'
    store.dispatch(appendMessage({ role: 'user', content: 'summarize this', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID, files: [spaced] }) }))
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-17', sendId: SEND_ID }))

    servePage({ ...page([]), queue: [{ content: '[REDACTED]', id: 'q-17' }] })
    await store.dispatch(switchSlot(SLOT))

    const raw = store.getState().chat.messages.find(m => m.role === 'queued')?.meta?.rawSend as { text?: string; files?: string[] } | undefined
    expect(raw?.files, 'the staged path must survive the rebuild').toEqual([spaced])
    expect(raw?.text, 'alongside the text it was staged with').toBe('summarize this')
  })

  it('keeps that metadata across a refreshSlot too, not just a switch', async () => {
    // The two above drive `switchSlot`, which hands hydration its own prior list.
    // `refreshSlot` replaces `messages` first, so it must pass the PRE-rebuild order.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    const spaced = '/Users/me/Desktop/Quarterly Report.pdf'
    store.dispatch(appendMessage({ role: 'user', content: 'summarize this too', cls: '', ts: '2026-08-29T17:54:02.900Z', meta: retainedSend({ sendId: SEND_ID, files: [spaced] }) }))
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:01.000Z', queue_id: 'q-18', sendId: SEND_ID }))

    servePage({ ...page([]), queue: [{ content: '[REDACTED]', id: 'q-18' }] })
    await store.dispatch(refreshSlot(SLOT))

    const raw = store.getState().chat.messages.find(m => m.role === 'queued')?.meta?.rawSend as { text?: string; files?: string[] } | undefined
    expect(raw?.text, 'the raw draft must survive a refresh rebuild').toBe('summarize this too')
    expect(raw?.files, 'and so must the staged attachment').toEqual([spaced])
  })

  it('carries the removed bubble\'s RAW text onto the queued card', () => {
    // The push content is redacted for display, so removing the bubble without
    // carrying its text forward makes a later cancel restore only the mask.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-14', sendId: SEND_ID }))

    const card = store.getState().chat.messages.find(m => m.role === 'queued')
    const raw = card?.meta?.rawSend as { text?: string; sent?: string } | undefined
    expect(raw?.text, 'the card must carry what the user actually typed').toBe('hello')
    expect(raw?.sent, 'and the content it was recorded against, as the edit guard').toBe('[REDACTED]')
  })

  it('keeps an optimistic bubble the queued card does NOT own', () => {
    // Negative control: removal must key on the send id, or an unrelated queued
    // message silently deletes a bubble whose own send is still in flight.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-13', sendId: 'a-different-send' }))

    expect(userRows(store), 'a bubble for another send must survive').toHaveLength(1)
  })

  it('does not re-attach a send a MERGED server row already stands for', async () => {
    // The drain folds several queued sends into one row and names them all in
    // `meta.sendIds`; on the scalar alone the earlier send reads as absent and returns.
    const store = storeWithOptimisticBubble()
    const mergedRow = {
      role: 'user',
      content: 'hello\n\nlater one',
      ts: '2026-08-29T17:54:03.986Z',
      meta: retainedSend({ sendId: 'send-later', sendIds: [SEND_ID, 'send-later'], mid: 'm-9' }),
    }
    servePage(page([mergedRow]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store), 'the merged row already represents this send').toHaveLength(1)
  })

  it('still re-attaches a send NO served row names', async () => {
    // Negative control: matching must read the list, not treat any `sendIds` row as
    // covering everything, or a genuinely absent send is silently dropped.
    const store = storeWithOptimisticBubble()
    const otherMerged = {
      role: 'user',
      content: 'two others',
      ts: '2026-08-29T17:54:03.986Z',
      meta: retainedSend({ sendId: 'send-y', sendIds: ['send-x', 'send-y'], mid: 'm-8' }),
    }
    servePage(page([otherMerged]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store), 'an unrepresented send must survive').toHaveLength(2)
  })

  it('confirms EVERY send a merged echo stands for', () => {
    // The drain folds several messages into one row, so its echo names them all;
    // resolving only the scalar leaves earlier sends unconfirmed and they resend.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'first', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: 'send-A' }) }))
    store.dispatch(appendMessage({ role: 'user', content: 'second', cls: '', ts: '2026-08-29T17:54:02.900Z', meta: retainedSend({ sendId: 'send-B' }) }))

    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: '[2 queued messages merged]\n\nfirst\n\nsecond',
      cls: 'msg msg-u',
      ts: '2026-08-29T17:55:00.000Z',
      meta: { mid: 'm-merged', sendId: 'send-B', sendIds: ['send-A', 'send-B'] },
    } as never))

    const stillOptimistic = store.getState().chat.messages.filter(m => m.role === 'user' && m.meta?.optimistic)
    expect(stillOptimistic.map(m => m.content), 'no send the merged row names may stay unconfirmed').toEqual([])
  })

  it('leaves a send the merged echo does NOT name still optimistic', () => {
    // Negative control: membership must be read from the list, not treated as
    // "any merged echo confirms everything pending".
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'first', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: 'send-A' }) }))
    store.dispatch(appendMessage({ role: 'user', content: 'unrelated', cls: '', ts: '2026-08-29T17:54:02.900Z', meta: retainedSend({ sendId: 'send-Z' }) }))

    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: '[2 queued messages merged]\n\nfirst',
      cls: 'msg msg-u',
      ts: '2026-08-29T17:55:00.000Z',
      meta: { mid: 'm-merged2', sendId: 'send-A', sendIds: ['send-A'] },
    } as never))

    const stillOptimistic = store.getState().chat.messages.filter(m => m.role === 'user' && m.meta?.optimistic)
    expect(stillOptimistic.map(m => m.meta?.sendId), 'an unnamed send must stay unconfirmed').toEqual(['send-Z'])
  })

  it('leaves a DIFFERENT send retained', async () => {
    // Negative control: releasing on ANY queue_push would delete a pending send the queue
    // never took, which is the data loss the retention exists to prevent.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-10', sendId: 'some-other-send' }))

    expect(store.getState().chat.messages.find(m => m.role === 'user')?.meta?.pendingServerRow).toBe(true)
  })

  it('carries no marker when the event names no send', async () => {
    // Old-client / plain enqueue shape: absent id must stay absent rather than become a
    // wildcard that matches the first retained row it finds.
    const store = storeWithOptimisticBubble()
    store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:00.000Z', queue_id: 'q-11' }))

    expect(store.getState().chat.messages.find(m => m.role === 'user')?.meta?.pendingServerRow).toBe(true)
    expect(store.getState().chat.messages.find(m => m.role === 'queued')?.meta?.sendId).toBeUndefined()
  })
})

describe('legacy history without identities keeps the prompt at the bottom', () => {
  beforeEach(() => vi.clearAllMocks())

  it('does not move the pending prompt above an identity-less transcript', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    // Legacy rows: no `mid`, so no anchor can ever match them.
    store.dispatch(appendMessage({ role: 'user', content: 'old prompt', cls: '', ts: '2026-08-29T17:00:00.000Z' }))
    store.dispatch(appendMessage({ role: 'assistant', content: 'old answer', cls: '', ts: '2026-08-29T17:00:01.000Z' }))
    store.dispatch(appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID }) }))
    detail.mockResolvedValue(page([
      { role: 'user', content: 'old prompt', ts: '2026-08-29T17:00:00.000Z' },
      { role: 'assistant', content: 'old answer', ts: '2026-08-29T17:00:01.000Z' },
    ]))

    await store.dispatch(switchSlot(SLOT))

    const contents = store.getState().chat.messages.map(m => m.content)
    expect(contents.indexOf('hello')).toBeGreaterThan(contents.indexOf('old answer'))
  })
})

describe('proven delivery retires the unconfirmed marking', () => {
  beforeEach(() => { vi.clearAllMocks(); detail.mockReset() })

  it('confirmOptimisticSend clears deliveryUnknown', () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND_ID }))
    expect(store.getState().chat.messages[0].meta?.deliveryUnknown).toBe(true)

    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))

    expect(store.getState().chat.messages[0].meta?.deliveryUnknown).toBeUndefined()
  })

  it('a WS echo clears deliveryUnknown', () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND_ID }))
    expect(store.getState().chat.messages[0].meta?.deliveryUnknown).toBe(true)

    // The echo carries the same sendId, which is what reconciles the row in place.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:02.500Z', meta: retainedSend({ sendId: SEND_ID, mid: 'm-1' }) }))

    const row = store.getState().chat.messages.find(m => m.role === 'user')
    expect(row?.meta?.deliveryUnknown).toBeUndefined()
  })
})

describe('a send with no preceding anchor lands after the fetched history', () => {
  beforeEach(() => { vi.clearAllMocks(); detail.mockReset() })

  /** Empty cache, so the send is the only prior row and nothing can anchor it; the
   *  client clock is BEHIND, so every page row also looks later. */
  async function skewedSendDuringRefetch(withLaterLocalRow = false) {
    const store = makeStore()
    store.dispatch(setActiveSlot(FRESH_SLOT))
    let release: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { release = res })
    detail.mockReturnValueOnce(withheld as never)
    const switching = store.dispatch(switchSlot(FRESH_SLOT))
    // Client clock is behind: 16:00 while the server's history is at 17:0x.
    store.dispatch(appendMessage({ role: 'user', content: 'skewed send', cls: '', ts: '2026-08-29T16:00:00.000Z', meta: retainedSend({ sendId: SEND_ID }) }))
    if (withLaterLocalRow) store.dispatch(appendMessage({ role: 'thinking', content: 'reasoning...', cls: '', ts: '2026-08-29T16:00:01.000Z' }))
    release(page([
      { role: 'user', content: 'older prompt', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-1' } },
      { role: 'assistant', content: 'older answer', ts: '2026-08-29T17:00:05.000Z', meta: { mid: 'm-2' } },
    ]))
    await switching
    return store
  }

  it('places the prompt AFTER fetched history despite a behind-client clock', async () => {
    const store = await skewedSendDuringRefetch()

    expect(store.getState().chat.messages.map(m => m.content)).toEqual(['older prompt', 'older answer', 'skewed send'])
  })

  it('keeps a later LOCAL row after the send it followed', async () => {
    const store = await skewedSendDuringRefetch(true)

    const contents = store.getState().chat.messages.map(m => m.content)
    expect(contents.indexOf('skewed send')).toBeGreaterThan(contents.indexOf('older answer'))
    expect(contents.indexOf('reasoning...')).toBeGreaterThan(contents.indexOf('skewed send'))
  })
})

describe('a send confirmed mid-flight survives the older refetch resolving', () => {
  beforeEach(() => vi.clearAllMocks())

  /** refetch starts -> send appended -> receipt clears `optimistic` -> that SAME
   *  older response resolves. Its page cannot hold a row that did not exist when it
   *  was dispatched, so the accepted bubble must survive it. Ordering is chosen by
   *  dispatch, never timed. */
  async function confirmDuringRefetch() {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    let release: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { release = res })
    detail.mockReturnValueOnce(withheld as never)
    const older = store.dispatch(switchSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID }) }))
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))
    expect(store.getState().chat.messages[0].meta?.optimistic).toBeUndefined()
    release(page([]))
    await older
    return store
  }

  it('keeps the accepted bubble the stale page could not contain', async () => {
    const store = await confirmDuringRefetch()

    expect(userRows(store).map(r => r.content)).toEqual(['hello'])
  })

})

describe('a refetch that snapshotted before the POST committed cannot delete the send', () => {
  beforeEach(() => vi.clearAllMocks())

  /** The refetch is dispatched AFTER the append, so dispatch order says it should
   *  have seen the row -- but the server snapshotted before the POST committed, so
   *  its page legitimately lacks it. Only page identity or an explicit outcome may
   *  retire the row; dispatch order is not evidence about the server's snapshot. */
  async function refetchAfterAppendSnapshotBefore() {
    const store = storeWithOptimisticBubble()
    let release: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { release = res })
    detail.mockReturnValueOnce(withheld as never)
    detail.mockResolvedValue(page([]) as never)
    const delayed = store.dispatch(switchSlot(SLOT))
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))
    release(page([]))
    await delayed
    return store
  }

  it('keeps the accepted bubble a pre-commit snapshot could not contain', async () => {
    const store = await refetchAfterAppendSnapshotBefore()

    expect(userRows(store).map(r => r.content)).toEqual(['hello'])
  })

  it('still garbage-collects once a page CARRIES the row', async () => {
    // Negative control: retention must end on page identity, or this becomes
    // "retain everything unconditionally" and the row is re-attached forever.
    const store = await refetchAfterAppendSnapshotBefore()
    servePage(page([serverUserRow]))
    await store.dispatch(switchSlot(SLOT))
    expect(userRows(store)).toHaveLength(1)

    servePage(page([]))
    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(0)
  })
})

describe('the warm reconcile is the third rebuild site, and keeps a pending pane send', () => {
  beforeEach(() => { vi.clearAllMocks(); detail.mockReset() })

  it('keeps a pane send whose slot has no identity overlap with the warm page', async () => {
    // An optimistic pane row carries no server identity, so `anchorIdx` is -1 and the
    // warm base is the page alone -- which dropped the send before the user switched in.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendSlotMessage({ slot: FRESH_SLOT, message: { role: 'user', content: 'pane send', cls: '', ts: '2026-08-29T17:54:02.000Z', meta: retainedSend({ sendId: 's-pane-1' }) } }))
    detail.mockResolvedValue(page([{ role: 'assistant', content: 'unrelated', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-warm' } }], false))

    await store.dispatch(warmSlotCache(FRESH_SLOT))

    const rows = store.getState().chat.slotMessages[FRESH_SLOT] ?? []
    expect(rows.filter(m => m.role === 'user').map(m => m.content)).toEqual(['pane send'])
  })

  it('a warm RESCUE is not a server confirmation, so a later stale page cannot drop it', async () => {
    // The warm base can be assembled from the PRIOR CACHE, so surviving the merge is
    // no proof the page carried the row. Longer prior ending earlier takes that branch.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendSlotMessage({ slot: FRESH_SLOT, message: { role: 'assistant', content: 'older reply', cls: '', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-old' } } }))
    store.dispatch(appendSlotMessage({ slot: FRESH_SLOT, message: { role: 'user', content: 'rescued send', cls: '', ts: '2026-08-29T17:54:02.000Z', meta: retainedSend({ sendId: 's-rescue-1' }) } }))

    // One page row, NEWER than the whole prior cache, and it does NOT carry the send.
    detail.mockResolvedValueOnce(page([{ role: 'assistant', content: 'newer reply', ts: '2026-08-29T18:00:00.000Z', meta: { mid: 'm-new' } }], false))
    await store.dispatch(warmSlotCache(FRESH_SLOT))

    const warmed = (store.getState().chat.slotMessages[FRESH_SLOT] ?? []).find(m => m.role === 'user')
    expect(warmed?.content).toBe('rescued send')
    // The mechanism: a rescue must NOT retire retention, because the server never
    // showed the row back.
    expect(warmed?.meta?.pendingServerRow).toBe(true)

    // A later refetch that still lacks the row must therefore keep re-attaching it. Mocked TWICE:
    // this cache shape is a coverage shortfall, so the switch also takes the unbounded read.
    detail.mockResolvedValueOnce(page([{ role: 'assistant', content: 'newer reply', ts: '2026-08-29T18:00:00.000Z', meta: { mid: 'm-new' } }], false))
    detail.mockResolvedValueOnce(page([{ role: 'assistant', content: 'newer reply', ts: '2026-08-29T18:00:00.000Z', meta: { mid: 'm-new' } }], false))
    await store.dispatch(switchSlot(FRESH_SLOT))

    expect(store.getState().chat.messages.filter(m => m.role === 'user').map(m => m.content)).toEqual(['rescued send'])
  })
})

describe('a delayed switch response cannot resurrect a finished turn', () => {
  beforeEach(() => { vi.clearAllMocks(); detail.mockReset() })

  it('leaves the slot idle when chat_done refreshed while the switch was in flight', async () => {
    // The switch snapshotted `running: true`. `chat_done` then refreshes with
    // running: false, so the stale switch response must not restore streaming.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    let releaseSwitch: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseSwitch = res })
    detail.mockReturnValueOnce(withheld as never)
    const switching = store.dispatch(switchSlot(FRESH_SLOT))

    detail.mockResolvedValueOnce(page([], false))
    await store.dispatch(refreshSlot(FRESH_SLOT))
    releaseSwitch(page([], true))
    await switching

    const s = store.getState().chat
    expect(s.slotState).toBe('idle')
    expect(s.slotRunning).toBe(false)
  })
})

describe('a racing refresh must not strand the slot without a paging cursor', () => {
  beforeEach(() => { vi.clearAllMocks(); detail.mockReset() })

  it('installs NO cursor from a superseded switch, even when the slot holds none', async () => {
    // Asserted the OPPOSITE until the fallback was ruled a defect: an older switch landing
    // the anchor skips rows the newer refresh read. No cursor degrades; a wrong one loses.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    let releaseSwitch: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseSwitch = res })
    detail.mockReturnValueOnce(withheld as never)
    const switching = store.dispatch(switchSlot(FRESH_SLOT))

    detail.mockResolvedValueOnce({ ...page([], false), has_more: true, next_before: 40 })
    await store.dispatch(refreshSlot(FRESH_SLOT))
    releaseSwitch({ ...page([], false), has_more: true, next_before: 40 })
    await switching

    const s = store.getState().chat
    expect(s.slotCursorKey).toBeNull()
  })

  it('still applies a refresh when NO switch is in flight', async () => {
    // Negative control: a guard that simply skips every same-slot refresh would pass
    // the test above while breaking the ordinary refresh path.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue({ ...page([serverUserRow]), has_more: true, next_before: 12 })

    await store.dispatch(refreshSlot(SLOT))

    const s = store.getState().chat
    expect(s.slotCursorKey).toBe(SLOT)
    expect(s.slotOldestIndex).toBe(12)
  })

  it('leaves paging OFF after the superseded switch, and a later fetch restores it', async () => {
    // The residual the superseded return now names: with no cursor, `loadOlder`'s own condition
    // (`slotCursorKey === activeSlot`) is false, so paging stays off until a fetch installs one.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    let releaseSwitch: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseSwitch = res })
    detail.mockReturnValueOnce(withheld as never)
    const switching = store.dispatch(switchSlot(FRESH_SLOT))

    detail.mockResolvedValueOnce({ ...page([], false), has_more: true, next_before: 40 })
    await store.dispatch(refreshSlot(FRESH_SLOT))
    releaseSwitch({ ...page([], false), has_more: true, next_before: 40 })
    await switching

    const stranded = store.getState().chat
    expect(stranded.slotCursorKey === stranded.activeSlot, 'paging must be OFF while no cursor exists').toBe(false)

    // A later successful fetch installs the anchor, and paging comes back on its own.
    detail.mockResolvedValueOnce({ ...page([], false), has_more: true, next_before: 40 })
    await store.dispatch(refreshSlot(FRESH_SLOT))
    const healed = store.getState().chat
    expect(healed.slotCursorKey === healed.activeSlot, 'a later fetch must restore paging').toBe(true)
  })
})

describe('a superseded switch still ends its loading state', () => {
  beforeEach(() => vi.clearAllMocks())

  it('clears slotLoading even when the response is discarded', async () => {
    // An UNCACHED target is the only case that raises the spinner, and the racing
    // refresh applies first so the switch's own response is superseded.
    const store = storeWithOptimisticBubble()
    const FRESH = 'chat-2-1788026099'
    let releaseSwitch: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseSwitch = res })
    detail.mockReturnValueOnce(withheld as never)
    const switching = store.dispatch(switchSlot(FRESH))
    expect(store.getState().chat.slotLoading).toBe(true)

    detail.mockResolvedValueOnce(page([]))
    await store.dispatch(refreshSlot(FRESH))
    releaseSwitch(page([]))
    await switching

    expect(store.getState().chat.slotLoading).toBe(false)
  })
})

describe('a confirmed prompt is not resurrected after a remote rewind', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps a confirmed row when the page carries a DIFFERENT prompt', async () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))
    // Another prompt is not this row's identity, so it is no evidence about this row:
    // the page may simply have been read before the POST committed.
    servePage(page([{ role: 'user', content: 'other', ts: '2026-08-29T17:55:10.000Z', meta: { mid: 'm-2' } }]))
    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store).map(r => r.content)).toContain('hello')
  })

  it('keeps an unconfirmed send whose CLIENT clock lags the server page', async () => {
    // A browser clock running behind mints an earlier `ts` than server rows that
    // genuinely predate the send, so no wall-clock comparison may retire a row.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'skewed', cls: '', ts: '2026-08-29T16:00:00.000Z', meta: retainedSend({ sendId: SEND_ID }) }))
    detail.mockResolvedValue(page([{ role: 'user', content: 'earlier server prompt', ts: '2026-08-29T17:50:00.000Z', meta: { mid: 'm-9' } }]))

    await store.dispatch(switchSlot(SLOT))

    // Retention is what matters: a skewed clock may still misplace the row relative
    // to the page, but it must never be the reason the row disappears.
    expect(userRows(store).map(r => r.content)).toContain('skewed')
  })
})

describe('a superseded same-slot response cannot erase a persisted send', () => {
  beforeEach(() => vi.clearAllMocks())

  /** Both thunks are STARTED first, so the older takes the lower sequence; their
   *  responses are then released in reverse. Ordering is chosen, never timed. */
  async function outOfOrder(stalePage: unknown) {
    const store = storeWithOptimisticBubble()
    let releaseOld: (v: unknown) => void = () => {}
    const withheld = new Promise(res => { releaseOld = res })
    detail.mockReturnValueOnce(withheld as never)
    const older = store.dispatch(switchSlot(SLOT))
    // The newer page CARRIES the row, so it retires the retention marker — the
    // precondition the reported race depends on.
    servePage(page([serverUserRow]))
    await store.dispatch(switchSlot(SLOT))
    // Now the older switch's own retry must answer with the stale page, not the newer one.
    detail.mockResolvedValue(stalePage as never)
    releaseOld(stalePage)
    await older
    return store
  }

  it('discards an older refetch that settles after a newer one', async () => {
    const store = await outOfOrder(page([]))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('hello')
  })

  it('does not let a superseded page add rows either', async () => {
    const store = await outOfOrder(page([{ role: 'assistant', content: 'stale answer', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-stale' } }]))

    expect(store.getState().chat.messages.some(m => m.content === 'stale answer')).toBe(false)
  })
})

describe('a rewind that deletes a send is not undone by re-attachment', () => {
  beforeEach(() => vi.clearAllMocks())

  it('drops a row a page once carried and a later page does not', async () => {
    const store = storeWithOptimisticBubble()
    // First refetch CARRIES the row: the server has acknowledged it.
    detail.mockResolvedValue(page([serverUserRow]))
    await store.dispatch(switchSlot(SLOT))
    expect(userRows(store)).toHaveLength(1)

    // Cross-tab rewind deleted it; this page legitimately no longer has it.
    detail.mockResolvedValue(page([]))
    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(0)
  })

  it('stops reattaching a confirmed prompt as soon as a page CARRIES it', async () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))
    const other = { role: 'assistant', content: 'a', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-2' } }
    servePage({ ...page([other], false), total: 4 })
    await store.dispatch(switchSlot(SLOT))
    expect(userRows(store)).toHaveLength(1)

    // The page now carries it, which retires the marker for good.
    servePage(page([serverUserRow]))
    await store.dispatch(switchSlot(SLOT))
    servePage(page([other]))
    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(0)
  })

  it('survives TWO concurrent refetches that both predate the append', async () => {
    // The first response must not retire retention: the second, equally stale, would
    // then find no reason to keep an accepted row and delete it.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))
    expect(userRows(store)).toHaveLength(1)
    await store.dispatch(switchSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('hello')
  })

  it('keeps a still-unconfirmed send even when the server total FALLS', async () => {
    // No count is consulted at all: an unacknowledged send cannot have been in the
    // rewound history, so a falling total is not evidence about it either way.
    const store = storeWithOptimisticBubble()
    const other = { role: 'assistant', content: 'a', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-2' } }
    detail.mockResolvedValue({ ...page([other], false), total: 4 })
    await store.dispatch(switchSlot(SLOT))
    expect(userRows(store)).toHaveLength(1)

    detail.mockResolvedValue({ ...page([other], false), total: 1 })
    await store.dispatch(switchSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('hello')
  })

  it('still retains a send NO page has ever carried', async () => {
    // The other half of the invariant: absence proves nothing until a page has
    // shown the row, so the accepted-but-unseen bubble must survive.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(1)
  })
})

describe('an identity-less row is not overtaken by a retained send', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps a channel row that precedes the send ahead of it', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'old', cls: '', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-old' } }))
    // A channel-replayed row: no `mid`, no `sendId`, so nothing can anchor on it.
    store.dispatch(appendMessage({ role: 'user', content: 'channel msg', cls: '', ts: '2026-08-29T17:30:00.000Z' }))
    store.dispatch(appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID }) }))
    detail.mockResolvedValue(page([
      { role: 'user', content: 'old', ts: '2026-08-29T17:00:00.000Z', meta: { mid: 'm-old' } },
      { role: 'user', content: 'channel msg', ts: '2026-08-29T17:30:00.000Z' },
    ]))

    await store.dispatch(switchSlot(SLOT))

    const contents = store.getState().chat.messages.map(m => m.content)
    expect(contents.indexOf('channel msg')).toBeLessThan(contents.indexOf('hello'))
  })
})

describe('a CONFIRMED send survives an older refetch that resolves after the receipt', () => {
  beforeEach(() => vi.clearAllMocks())

  it('re-attaches the accepted bubble a page has not shown back yet', async () => {
    const store = storeWithOptimisticBubble()
    // A receipt says accepted, NOT visible to the next read: the page may have been
    // snapshotted before the POST committed, so its absence is not proof.
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))
    expect(store.getState().chat.messages[0].meta?.optimistic).toBeUndefined()
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store).map(r => r.content)).toEqual(['hello'])
  })

  it('does not duplicate it once the page carries the confirmed row', async () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: SEND_ID, mid: 'm-1' }))
    detail.mockResolvedValue(page([serverUserRow]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(1)
  })

  it('a FAILED send is still excluded after the flag is cleared', async () => {
    // The one exclusion the widened predicate keeps.
    const store = storeWithOptimisticBubble()
    store.dispatch(clearPendingServerRow({ slot: SLOT, sendId: SEND_ID }))
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    expect(userRows(store)).toHaveLength(0)
  })
})

describe('a page that carries the row confirms delivery, not just retention', () => {
  beforeEach(() => vi.clearAllMocks())

  it('stamps the confirming send identity so a restored draft can be retired', async () => {
    // The refetch is the THIRD confirm path: a lost response leaves the composer
    // holding a payload the server in fact persisted.
    const store = storeWithOptimisticBubble()
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND_ID }))
    detail.mockResolvedValue(page([serverUserRow]))

    await store.dispatch(refreshSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.deliveryConfirmed).toBe(true)
    expect(rows[0].meta?.confirmedSendId).toBe(SEND_ID)
    // Confirmed and unknown are contradictory; the confirmation wins.
    expect(rows[0].meta?.deliveryUnknown).toBeFalsy()
  })

  it('confirms against a REALISTIC server row that carries no client marker', async () => {
    // `pendingServerRow` is client-only, so the page the server returns cannot carry
    // it. This is the shape the real refetch sees.
    const store = storeWithOptimisticBubble()
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: SEND_ID }))
    detail.mockResolvedValue(page([{ role: 'user', content: 'hello', ts: '2026-08-29T17:54:01.986Z', meta: { sendId: SEND_ID, mid: 'm-1' } }]))

    await store.dispatch(refreshSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.deliveryConfirmed).toBe(true)
    expect(rows[0].meta?.confirmedSendId).toBe(SEND_ID)
    expect(rows[0].meta?.deliveryUnknown).toBeFalsy()
  })

  it('leaves a bubble the page does NOT carry unconfirmed', async () => {
    // The negative control: absence of the row is not a receipt, so nothing may
    // be stamped and the staged copy must survive.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([]))

    await store.dispatch(refreshSlot(SLOT))

    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.deliveryConfirmed).toBeFalsy()
    expect(rows[0].meta?.confirmedSendId).toBeUndefined()
  })
})

describe('a fetched receipt must confirm even with no local bubble to key on', () => {
  it('reads the confirmation off the fetched row own sendId', async () => {
    // A stale busy read skips the optimistic append, so NOTHING local carries the
    // sendId -- yet the server accepted it, so the fetched row is itself the receipt.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    expect(store.getState().chat.messages).toHaveLength(0)

    // A real SERVER row: it keeps the client's sendId and gains a `mid`, but not the
    // client-only retention marker.
    detail.mockResolvedValue(page([
      { role: 'user', content: 'hello', ts: '2026-08-29T17:54:01.986Z', meta: { sendId: SEND_ID, mid: 'm-1' } },
    ]))

    await store.dispatch(switchSlot(SLOT))

    const rows = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.deliveryConfirmed).toBe(true)
    expect(rows[0].meta?.confirmedSendId).toBe(SEND_ID)
  })

  it('does NOT confirm a local row the fetched page does not carry', async () => {
    // The guard on the fallback: an unconfirmed LOCAL bubble also carries a sendId, so
    // an ungated fallback would vouch for delivery nothing proved and retire the copy.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([]))

    await store.dispatch(switchSlot(SLOT))

    const rows = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.deliveryConfirmed).toBeUndefined()
  })
})

describe('an accepted detail response records the TOTAL order too', () => {
  it('stamps slotServerTotalSeq even when no total was retained', async () => {
    // `running` makes retainServerTotal return BEFORE it stamps the order, so an OLDER
    // warm could then lower the baseline and the next warm read a shrink that never was.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([serverUserRow], true))

    await store.dispatch(refreshSlot(SLOT))

    const s = store.getState().chat
    // Premise: nothing was retained, which is what leaves the gap.
    expect(s.slotServerTotal?.[SLOT]).toBeUndefined()
    expect(typeof s.slotServerTotalSeq?.[SLOT]).toBe('number')
  })

  it('still records it on the ordinary idle path', async () => {
    // Negative control: a stamp bolted onto the running branch alone would pass the test
    // above while leaving the ordinary path unordered.
    const store = storeWithOptimisticBubble()
    detail.mockResolvedValue(page([serverUserRow], false))

    await store.dispatch(refreshSlot(SLOT))

    const s = store.getState().chat
    expect(typeof s.slotServerTotalSeq?.[SLOT]).toBe('number')
  })
})

describe('a warm confirmation is ordered against a stale refresh', () => {
  it('records the warm in the detail sequence so an older response cannot follow it', async () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(setActiveSlot('other-slot'))
    detail.mockResolvedValueOnce(page([serverUserRow], false))
    await store.dispatch(warmSlotCache(SLOT) as never)

    // Unstamped, the warm leaves no ordering mark, so a refresh snapshot taken BEFORE it
    // still passes its own guard and rebuilds the slot from a page that predates the send.
    const seq = (store.getState().chat.slotDetailSeq ?? {})[SLOT]
    expect(typeof seq).toBe('number')
  })

  it('refuses a warm that is older than the response already applied', async () => {
    const store = storeWithOptimisticBubble()
    store.dispatch(setActiveSlot('other-slot'))
    // A newer response has already been applied for this slot.
    detail.mockResolvedValueOnce(page([serverUserRow], false))
    await store.dispatch(warmSlotCache(SLOT) as never)
    const afterFirst = (store.getState().chat.slotMessages ?? {})[SLOT] ?? []

    // A warm carrying an OLDER sequence must not rewrite the page.
    const state = store.getState().chat as { slotDetailSeq?: Record<string, number> }
    const applied = (state.slotDetailSeq ?? {})[SLOT] as number
    store.dispatch({
      type: 'chat/warmSlotCache/fulfilled',
      payload: { key: SLOT, messages: [], queue: [], hasMore: false, total: 0, running: false, warmSeq: applied - 1 },
    })
    expect((store.getState().chat.slotMessages ?? {})[SLOT]).toEqual(afterFirst)
  })
})

describe('a later confirmed turn retires an earlier unconfirmed caption', () => {
  it('clears deliveryUnknown on the earlier row without claiming it was delivered', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    // A send whose delivery is unknown: never echoed, so nothing else ever retires it.
    store.dispatch(appendMessage({ role: 'user', content: 'never left', cls: '', ts: '2026-08-29T17:00:00.000Z', meta: retainedSend({ sendId: 's-old-1' }) }))
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: 's-old-1' }))
    expect(store.getState().chat.messages[0].meta?.deliveryUnknown).toBe(true)

    // A LATER send is echoed back confirmed.
    store.dispatch(appendMessage({ role: 'user', content: 'this one landed', cls: '', ts: '2026-08-29T17:05:00.000Z', meta: retainedSend({ sendId: 's-new-2' }) }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'this one landed', cls: '', ts: '2026-08-29T17:05:01.000Z', meta: retainedSend({ sendId: 's-new-2', mid: 'm-2' }) }))

    const rows = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(rows[0].meta?.deliveryUnknown).toBeUndefined()
    // The retirement must NOT upgrade the earlier row to confirmed -- it was never delivered
    // as far as anything here knows; only the spent warning goes.
    expect(rows[0].meta?.deliveryConfirmed).toBeUndefined()
  })

  it('keeps a terminal marking so the row is not rendered as an ordinary prompt', () => {
    // Spending the nag must not spend the DOUBT. `deliveryUnknown` drove the dimming, the
    // dashed outline AND the caption, so deleting it alone vouched for a delivery nobody saw.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'never left', cls: '', ts: '2026-08-29T17:00:00.000Z', meta: retainedSend({ sendId: 's-old-1' }) }))
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: 's-old-1' }))

    store.dispatch(appendMessage({ role: 'user', content: 'this one landed', cls: '', ts: '2026-08-29T17:05:00.000Z', meta: retainedSend({ sendId: 's-new-2' }) }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'this one landed', cls: '', ts: '2026-08-29T17:05:01.000Z', meta: retainedSend({ sendId: 's-new-2', mid: 'm-2' }) }))

    const rows = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(rows[0].meta?.deliveryUnresolved).toBe(true)
    // Negative control: the row that WAS echoed carries proof, so marking it unresolved
    // would put a permanent doubt caption on every confirmed send in the transcript.
    expect(rows[1].meta?.deliveryUnresolved).toBeUndefined()
    expect(rows[1].meta?.deliveryConfirmed).toBe(true)
  })

  it('demotes the earlier caption on the RECEIPT path too, not just the WS echo', () => {
    // No `chat_message` echo carries the mid for a dashboard send, so the receipt is the path
    // that fires there; without this loop the earlier twin nags at live urgency for the tab's life.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'never left', cls: '', ts: '2026-08-29T17:00:00.000Z', meta: retainedSend({ sendId: 's-old-1' }) }))
    store.dispatch(markDeliveryUnknown({ slot: SLOT, sendId: 's-old-1' }))
    expect(store.getState().chat.messages[0].meta?.deliveryUnknown).toBe(true)

    // A LATER send is confirmed by its POST receipt rather than by an echo.
    store.dispatch(appendMessage({ role: 'user', content: 'this one landed', cls: '', ts: '2026-08-29T17:05:00.000Z', meta: retainedSend({ sendId: 's-new-2' }) }))
    store.dispatch(confirmOptimisticSend({ slot: SLOT, sendId: 's-new-2', mid: 'm-2' }))

    const rows = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(rows[0].meta?.deliveryUnknown, 'the spent nag must retire on the receipt path too').toBeUndefined()
    expect(rows[0].meta?.deliveryUnresolved, 'the DOUBT outlives the caption').toBe(true)
    // The earlier row must NOT be upgraded to confirmed: nothing here saw it land.
    expect(rows[0].meta?.deliveryConfirmed).toBeUndefined()
    // Negative control: the receipt-carrying row keeps its proof and gains no doubt marking.
    expect(rows[1].meta?.deliveryUnresolved).toBeUndefined()
    expect(rows[1].meta?.deliveryConfirmed).toBe(true)
  })
})

describe('a superseded warm must not write live run state', () => {
  it('leaves a streaming background turn streaming', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('other-slot'))
    // A newer warm lands first, stamping the slot's detail sequence.
    detail.mockResolvedValueOnce(page([serverUserRow], false))
    await store.dispatch(warmSlotCache(SLOT) as never)
    const applied = (store.getState().chat.slotDetailSeq ?? {})[SLOT] as number

    // A live chunk frame then starts a turn in that same background slot.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', cls: '' }))
    // Guard the premise rather than assume the frame armed the run.
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('streaming')

    // An OLDER warm, whose snapshot predates the turn, now resolves. Its page write was
    // already guarded; the run write below it was not, so it idled a live turn.
    store.dispatch({
      type: 'chat/warmSlotCache/fulfilled',
      payload: { key: SLOT, messages: [], queue: [], hasMore: false, total: 0, running: false, warmSeq: applied - 1 },
    })

    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('streaming')
    expect(store.getState().chat.slotRun[SLOT]?.lastChunkSeq).not.toBeNull()
  })

  it('still idles the slot when the warm is NOT superseded', async () => {
    // Negative control: returning unconditionally, or hoisting the guard above the page
    // computation, would pass the test above while leaving a finished pane spinning.
    const store = makeStore()
    store.dispatch(setActiveSlot('other-slot'))
    detail.mockResolvedValueOnce(page([serverUserRow], false))
    await store.dispatch(warmSlotCache(SLOT) as never)
    const applied = (store.getState().chat.slotDetailSeq ?? {})[SLOT] as number

    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', cls: '' }))
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('streaming')

    store.dispatch({
      type: 'chat/warmSlotCache/fulfilled',
      payload: { key: SLOT, messages: [serverUserRow], queue: [], hasMore: false, total: 1, running: false, warmSeq: applied + 1 },
    })

    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
  })
})

/* GPT F1 at ea3041f4f: the retirement scan was bounded to the last 50 rows, so a discard the
 * user delays while an agent turn emits 50+ rows missed its own sendId. Retention is durable
 * across refetch by design and this is its only retirement path, so the phantom was permanent. */
describe('retirement finds its send however far back it has scrolled', () => {
  it('retires a send an agent turn has pushed beyond the old 50-row window', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({ role: 'user', content: 'hello', cls: '', ts: '2026-08-29T17:54:01.900Z', meta: retainedSend({ sendId: SEND_ID }) }))
    // A single agent turn commonly emits this many rows; the discard is still pending.
    for (let i = 0; i < 60; i++) {
      store.dispatch(appendMessage({ role: 'assistant', content: `line ${i}`, cls: '', ts: '2026-08-29T17:55:00.000Z', meta: { mid: `m-fill-${i}` } }))
    }
    store.dispatch(clearPendingServerRow({ slot: SLOT, sendId: SEND_ID }))
    const row = store.getState().chat.messages.find(m => m.role === 'user' && m.meta?.sendId === SEND_ID)
    expect(row, 'the retained send is still in the transcript').toBeTruthy()
    expect(row?.meta?.pendingServerRow,
      'a discard 60 rows back must still retire retention, or refetch reattaches it forever').toBe(false)
  })
})
