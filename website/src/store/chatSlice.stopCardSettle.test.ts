// Regression (#20728): the live chat view kept a stop card at `stopping` after
// the gateway settled it. The stop-card upsert-by-`meta.id` runs only in the
// live WS frame appliers, never in the switch/refresh MERGE: that path rebuilds
// the transcript from the server snapshot and dedups only by `meta.mid`, and a
// snapshot's stop_event row carries no `mid` (`_prepare_messages` keeps `mid`
// for permission rows only). So a snapshot captured while the stop was still
// `stopping`, landing after the tab already settled the card, overwrote the
// settled state — leaving the card stuck pulsing at `stopping` until a reload.
// `preserveSettledStopState` restores the settled state the merge dropped.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatMessage } from '../types'
import { preserveSettledStopState } from './chat/transcript'

const SLOT = 'slot-stop'
const STOP_ID = 'stop-abc123'
const STOP_TS = new Date(Date.UTC(2026, 0, 1, 0, 0, 10)).toISOString()

const cls = (state: string) =>
  JSON.stringify({ kind: 'stop_event', id: STOP_ID, state, outcome: state === 'stopping' ? null : 'hard', ts_start: 'tstart', ...(state === 'stopping' ? {} : { ts_end: 'tend' }) })

// A SERVER snapshot stop row: cls carries kind/id/state; a non-permission row's
// `mid` is NOT on the wire (per `_prepare_messages`).
const snapshotStopRow = (state: string) =>
  ({ role: 'system', content: cls(state), cls: cls(state), ts: STOP_TS, meta: { kind: 'stop_event', id: STOP_ID, state } })

// The LIVE settled card the tab already holds (id + mid from the WS frame).
const liveSettledRow = () =>
  ({ role: 'system', content: cls('stop_failed_reset'), cls: cls('stop_failed_reset'), ts: STOP_TS, meta: { kind: 'stop_event', id: STOP_ID, state: 'stop_failed_reset', mid: 'row-mid-xyz' } })

let SNAPSHOT: Array<Record<string, unknown>> = []
vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn(() => Promise.resolve({
      messages: SNAPSHOT, has_more: false, total: SNAPSHOT.length, next_before: 0, running: false,
    })),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, { refreshSlot, switchSlot } from './chatSlice'

function makeStore(extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: SLOT, ...extra } },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

const stopCards = (store: ReturnType<typeof makeStore>) => {
  const cards = store.getState().chat.messages.filter(m => (m.meta as { kind?: string } | undefined)?.kind === 'stop_event')
  return { count: cards.length, states: cards.map(c => (c.meta as { state?: string }).state) }
}

beforeEach(() => { SNAPSHOT = [] })

describe('#20728 stop card settle survives a snapshot merge', () => {
  it('refreshSlot: a late stopping snapshot does not resurrect a settled card', async () => {
    const store = makeStore({ messages: [liveSettledRow()] })
    expect(stopCards(store)).toEqual({ count: 1, states: ['stop_failed_reset'] })

    SNAPSHOT = [snapshotStopRow('stopping')]
    await store.dispatch(refreshSlot(SLOT))

    expect(stopCards(store)).toEqual({ count: 1, states: ['stop_failed_reset'] })
  })

  it('switchSlot: reopening the slot does not regress the settled card', async () => {
    const store = makeStore({ messages: [liveSettledRow()], activeSlot: 'other' })
    SNAPSHOT = [snapshotStopRow('stopping')]
    // Seed the live settled row as the slot's held transcript, then reopen it.
    store.dispatch({ type: 'chat/hydrateSlotMessages', payload: { slot: SLOT, messages: [liveSettledRow()] } })
    await store.dispatch(switchSlot(SLOT))

    const after = stopCards(store)
    expect(after.count).toBe(1)
    expect(after.states).toEqual(['stop_failed_reset'])
  })
})

describe('preserveSettledStopState', () => {
  const row = (state: string, mid?: string): ChatMessage =>
    ({ role: 'system', content: cls(state), cls: cls(state), ts: STOP_TS, meta: { kind: 'stop_event', id: STOP_ID, state, ...(mid ? { mid } : {}) } } as unknown as ChatMessage)

  it('carries a settled prior state onto a merged stopping row of the same id', () => {
    const out = preserveSettledStopState([row('stop_failed_reset', 'm1')], [row('stopping')])
    expect(out).toHaveLength(1)
    expect((out[0].meta as { state?: string }).state).toBe('stop_failed_reset')
  })

  it('upgrades a stale stopping copy of a settled id rather than leaving it pulsing', () => {
    // Scope is the stale-snapshot regression: carry the settled state onto a
    // stopping row of the same id. Collapsing two SEPARATE backend rows for one
    // id is a backend-writer concern and deliberately not done here.
    const out = preserveSettledStopState([row('stop_failed_reset', 'm1')], [row('stop_failed_reset', 'm1'), row('stopping')])
    const cards = out.filter(m => (m.meta as { kind?: string }).kind === 'stop_event')
    expect(cards.map(c => (c.meta as { state?: string }).state)).toEqual(['stop_failed_reset', 'stop_failed_reset'])
  })

  it('leaves an id that is still only stopping untouched', () => {
    const stopping = [row('stopping')]
    expect(preserveSettledStopState([], stopping)).toBe(stopping)
    // No settled prior copy -> no change.
    expect(preserveSettledStopState([row('stopping')], stopping)).toBe(stopping)
  })

  it('does not touch non-stop rows', () => {
    const msgs = [{ role: 'assistant', content: 'hi', cls: 'msg', ts: STOP_TS } as unknown as ChatMessage]
    expect(preserveSettledStopState([row('stopped', 'm1')], msgs)).toBe(msgs)
  })
})
