import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import { api } from '../api/client'
import { abortPendingSends, sendTurn, type SendWire } from '../chat-core/transport/sendTurn'
import chatReducer, { requestStop, setActiveSlot, setSlotRunning, startLocalTurn } from '../store/chatSlice'

// Stop on a slot must cancel that slot's main-chat send that is still waiting
// on the network, so the message cannot start the turn the user just stopped.
// A follow-up sent while a turn runs is the gateway's to queue, and Stop keeps it.

/** A POST that never answers until its signal aborts. */
function hang(signals: AbortSignal[], signal: AbortSignal | undefined): Promise<never> {
  if (signal) signals.push(signal)
  return new Promise((_resolve, reject) => {
    signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  })
}

/** The dashboard wire's `api.sendChat`, held open. */
function hangingSendChat() {
  const signals: AbortSignal[] = []
  vi.spyOn(api, 'sendChat').mockImplementation(((_m: string, _s?: string, _t?: string, signal?: AbortSignal) => hang(signals, signal)) as never)
  return signals
}

afterEach(() => { vi.restoreAllMocks() })

describe('Stop aborts a pending send', () => {
  it('aborts only the stopped slot and settles it as response-late', async () => {
    const signals = hangingSendChat()
    const stopped = sendTurn({ message: 'hi', slot: 'slot-a', cancelOnStop: true })
    const other = sendTurn({ message: 'hi', slot: 'slot-b', cancelOnStop: true })
    abortPendingSends('slot-a')
    await expect(stopped).resolves.toMatchObject({ status: 'response-late' })
    expect(signals[0].aborted).toBe(true)
    expect(signals[1].aborted).toBe(false)
    abortPendingSends('slot-b')
    await other
  })

  it('leaves an answered send alone', async () => {
    let seen: AbortSignal | undefined
    vi.spyOn(api, 'sendChat').mockImplementation(((_m: string, _s?: string, _t?: string, signal?: AbortSignal) => {
      seen = signal
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }))
    }) as never)
    const receipt = await sendTurn({ message: 'hi', slot: 'slot-c', cancelOnStop: true })
    abortPendingSends('slot-c')
    expect(receipt.status).toBe('dispatched')
    expect(seen?.aborted).toBe(false)
  })

  it('leaves a send on another wire (Side Chat, an app embed) alone', async () => {
    const signals: AbortSignal[] = []
    const wire: SendWire = (_payload, signal) => hang(signals, signal)
    const side = sendTurn({ message: 'hi', slot: 'slot-e', wire, cancelOnStop: true })
    abortPendingSends('slot-e')
    expect(signals[0].aborted).toBe(false)
    void side
  })

  it('a Stop that cancelled the send idles the composer on not running', async () => {
    vi.spyOn(api, 'stopChatSlot').mockResolvedValue({ ok: true, info: 'not running' } as never)
    hangingSendChat()
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('slot-f'))
    store.dispatch(startLocalTurn('slot-f'))
    const pending = sendTurn({ message: 'hi', slot: 'slot-f', cancelOnStop: true })
    await store.dispatch(requestStop({ slotId: 'slot-f', force: false }))
    expect(store.getState().chat.slotRunning).toBe(false)
    expect(store.getState().chat.pendingTurnSlot).toBeNull()
    await pending
  })

  it('a Stop that cancelled the send clears its pending mark after a switch away', async () => {
    let answer: (v: unknown) => void = () => {}
    vi.spyOn(api, 'stopChatSlot').mockReturnValue(new Promise((resolve) => { answer = resolve }) as never)
    hangingSendChat()
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('slot-h'))
    store.dispatch(startLocalTurn('slot-h'))
    const pending = sendTurn({ message: 'hi', slot: 'slot-h', cancelOnStop: true })
    const stopping = store.dispatch(requestStop({ slotId: 'slot-h', force: false }))
    store.dispatch(setActiveSlot('slot-i'))
    answer({ ok: true, info: 'not running' })
    await stopping
    await pending
    // Re-entering the chat must not bring the cancelled send's busy mark back.
    store.dispatch(setActiveSlot('slot-h'))
    expect(store.getState().chat.pendingTurnSlot).toBeNull()
  })

  it('leaves a follow-up sent while the slot runs alone, so the gateway keeps it queued', async () => {
    vi.spyOn(api, 'stopChatSlot').mockResolvedValue({ ok: true } as never)
    const signals = hangingSendChat()
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('slot-j'))
    store.dispatch(setSlotRunning(true))
    const followUp = sendTurn({ message: 'and then this', slot: 'slot-j' })
    await store.dispatch(requestStop({ slotId: 'slot-j', force: false }))
    expect(signals[0].aborted).toBe(false)
    void followUp
  })

  it('a Stop retried after a failed request still settles the send the first press cancelled', async () => {
    vi.spyOn(api, 'stopChatSlot')
      .mockRejectedValueOnce(new TypeError('connection reset'))
      .mockResolvedValueOnce({ ok: true, info: 'not running' } as never)
    hangingSendChat()
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('slot-k'))
    store.dispatch(startLocalTurn('slot-k'))
    const pending = sendTurn({ message: 'hi', slot: 'slot-k', cancelOnStop: true })
    const failed = await store.dispatch(requestStop({ slotId: 'slot-k', force: false }))
    expect(failed.payload).toMatchObject({ error: 'connection reset' })
    await store.dispatch(requestStop({ slotId: 'slot-k', force: false }))
    expect(store.getState().chat.pendingTurnSlot).toBeNull()
    expect(store.getState().chat.slotRunning).toBe(false)
    await pending
  })

  it('a Stop with no send to cancel keeps a pending turn busy on not running', async () => {
    vi.spyOn(api, 'stopChatSlot').mockResolvedValue({ ok: true, info: 'not running' } as never)
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('slot-g'))
    store.dispatch(startLocalTurn('slot-g'))
    await store.dispatch(requestStop({ slotId: 'slot-g', force: false }))
    expect(store.getState().chat.slotRunning).toBe(true)
  })

  it('requestStop aborts the slot\'s pending send', async () => {
    vi.spyOn(api, 'stopChatSlot').mockResolvedValue({ ok: true } as never)
    const signals = hangingSendChat()
    const store = configureStore({ reducer: { chat: chatReducer } })
    const pending = sendTurn({ message: 'hi', slot: 'slot-d', cancelOnStop: true })
    await store.dispatch(requestStop({ slotId: 'slot-d', force: false }))
    expect(signals[0].aborted).toBe(true)
    await expect(pending).resolves.toMatchObject({ status: 'response-late' })
  })
})
