/** A queue echo carries no `expect`, so the reducer cannot correlate it by text either: the server
 *  rewrites content for display. Applying an echo the stash did not recognise as the pending edit's
 *  own restores the older text and drops the newer edit's pending state.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, appendMessage, editQueuedMessage, applyQueueEdit } from './chatSlice'
import { queuedSendStash, stashQueuedSend, noteAppliedEditRev } from '../utils/queuedSendStash'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ immutableCheck: false, serializableCheck: false }),
  })
}

const SLOT = 'echo-vs-newer-edit'

describe('a delayed lower-revision remote frame does not overwrite the newer row', () => {
  beforeEach(() => { queuedSendStash.clear() })

  it('refuses the stale frame at the thunk, before the reducer writes the row', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'queued', content: 'revision two text', cls: '', ts: '2026-09-11T10:00:00.000Z',
      meta: { queueId: 'q-rev-race' },
    } as never))
    stashQueuedSend('q-rev-race', { raw: 'revision two text', files: [], sent: 'revision two text' })
    // Revision 2 is what the row displays: a refetch or a newer remote edit was already applied.
    noteAppliedEditRev('q-rev-race', 2)

    // The delayed REMOTE frame, revision 1. Carrying no `expect` it takes the echo branch, which
    // stamped the revision but never refused, so the reducer's row write replaced newer text.
    await store.dispatch(applyQueueEdit({
      slot: SLOT, queue_id: 'q-rev-race', content: 'revision one text', editRev: 1,
    }) as never)

    const row = store.getState().chat.messages.find(m => m.meta?.queueId === 'q-rev-race')
    expect(row?.content,
      'a lower-revision frame must not overwrite the revision the row already shows')
      .toBe('revision two text')
  })

  it('still applies a remote frame whose revision is NEWER than the row', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'queued', content: 'revision two text', cls: '', ts: '2026-09-11T10:00:00.000Z',
      meta: { queueId: 'q-rev-fwd' },
    } as never))
    stashQueuedSend('q-rev-fwd', { raw: 'revision two text', files: [], sent: 'revision two text' })
    noteAppliedEditRev('q-rev-fwd', 2)

    await store.dispatch(applyQueueEdit({
      slot: SLOT, queue_id: 'q-rev-fwd', content: 'revision three text', editRev: 3,
    }) as never)

    const row = store.getState().chat.messages.find(m => m.meta?.queueId === 'q-rev-fwd')
    expect(row?.content,
      'the guard must refuse only what is stale, or every remote edit stops landing')
      .toBe('revision three text')
  })
})

describe('a stale queue echo does not overwrite a newer edit', () => {
  beforeEach(() => { queuedSendStash.clear() })

  const seed = () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'queued', content: 'first text', cls: '', ts: '2026-09-11T10:00:00.000Z',
      meta: { queueId: 'q-e' },
    } as never))
    // The user edits again; the card now shows the newer text with its edit still unconfirmed.
    store.dispatch(editQueuedMessage({
      slot: SLOT, queue_id: 'q-e', content: 'second text', confirmed: false, editId: 'edit-2',
    }))
    return store
  }

  it('keeps the newer pending edit when the echo is not its confirmation', () => {
    const store = seed()
    const before = store.getState().chat.messages.find(m => m.meta?.queueId === 'q-e')
    expect(before?.content, 'premise: the newer edit is on the card').toBe('second text')
    expect(before?.meta?.editPending, 'premise: it is still unconfirmed').toBe('second text')

    // A delayed echo of the FIRST edit arrives. The stash does not recognise it, so it cannot be
    // the pending edit's confirmation.
    store.dispatch(editQueuedMessage({
      slot: SLOT, queue_id: 'q-e', content: 'first text', echoConfirms: false,
    }))

    const row = store.getState().chat.messages.find(m => m.meta?.queueId === 'q-e')
    expect(row?.content,
      'a stale echo restored the old text over a newer edit').toBe('second text')
    expect(row?.meta?.editPending,
      'clearing the newer edit pending state drops its own response').toBe('second text')
  })

  it('applies an echo that DOES confirm the pending edit, so the guard is not refusing everything', () => {
    const store = seed()
    store.dispatch(editQueuedMessage({
      slot: SLOT, queue_id: 'q-e', content: 'second text redacted', echoConfirms: true,
    }))

    const row = store.getState().chat.messages.find(m => m.meta?.queueId === 'q-e')
    expect(row?.content, 'the confirming echo is the display form and must land')
      .toBe('second text redacted')
    expect(row?.meta?.editPending, 'a confirmed edit is no longer pending').toBeUndefined()
  })

  it('applies a remote echo when no edit of ours is pending', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'queued', content: 'first text', cls: '', ts: '2026-09-11T10:00:00.000Z',
      meta: { queueId: 'q-r' },
    } as never))

    store.dispatch(editQueuedMessage({
      slot: SLOT, queue_id: 'q-r', content: 'their text', echoConfirms: false,
    }))

    expect(store.getState().chat.messages.find(m => m.meta?.queueId === 'q-r')?.content,
      'another client edit must still show when nothing local is pending').toBe('their text')
  })
})
