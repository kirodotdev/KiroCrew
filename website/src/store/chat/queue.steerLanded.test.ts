import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, cancelQueuedMessage, removeQueuedMessage } from '../chatSlice'
import type { ChatMessage } from '../../types'

const slot = 'steer-landed'
const ts = '2026-11-05T00:00:00Z'
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []

/** A slot that already holds `seed`, then a queued entry for `text` that drains. */
/** The card the steer requeue pushes says so (`meta.requeuedSteer`); pass
 *  `cardMeta: {}` for an ordinary queued send. */
function drain(seed: ChatMessage[], text: string, popMeta?: Record<string, unknown>, cardMeta: Record<string, unknown> = { requeuedSteer: true }) {
  const seeded = structuredClone(cold())
  seeded.slotMessages[slot] = structuredClone(seed)
  let state = reducer(seeded, appendQueuedMessage({ slot, content: text, ts: '2026-11-05T00:01:00Z', queue_id: 'q-1', meta: cardMeta }))
  state = reducer(state, removeQueuedMessage({ slot, content: text, queue_id: 'q-1', ...(popMeta ? { meta: popMeta } : {}) }))
  const rebuilt = rows(state).at(-1)
  expect(rebuilt?.role).toBe('user')
  expect(rebuilt?.content).toBe(text)
  return rebuilt!
}

const steerRow = (content: string, steerState: string): ChatMessage => ({
  role: 'user', content, ts, cls: 'msg msg-u', meta: { mid: `m-${steerState}`, steer: true, steerState },
})

describe('queue_pop of a requeued steer marks the rebuilt row', () => {
  it('stamps landedFromSteer when the nearest earlier row with the text is a requeued steer', () => {
    const row = drain([steerRow('go north', 'requeued')], 'go north')
    // The mark is the drained card's own id, the one-shot key UserMessage uses.
    expect(row.meta?.landedFromSteer).toBe('q-1')
    // The card's flag says which card drained; it never reaches the row.
    expect(row.meta?.requeuedSteer).toBeUndefined()
  })

  it('stamps it when the drain already marked the steer row superseded', () => {
    const row = drain([steerRow('go north', 'superseded')], 'go north')
    expect(row.meta?.landedFromSteer).toBe('q-1')
  })

  it('keeps the steer row itself unmarked', () => {
    const seeded = structuredClone(cold())
    seeded.slotMessages[slot] = [steerRow('go north', 'requeued')]
    let state = reducer(seeded, appendQueuedMessage({ slot, content: 'go north', ts: '2026-11-05T00:01:00Z', queue_id: 'q-1', meta: { requeuedSteer: true } }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go north', queue_id: 'q-1' }))
    expect(rows(state)[0].meta?.landedFromSteer).toBeUndefined()
  })
})

describe('queue_pop with no requeued steer to answer leaves the row unmarked', () => {
  it('when the steer text differs', () => {
    expect(drain([steerRow('go north', 'requeued')], 'go south').meta?.landedFromSteer).toBeUndefined()
  })

  it('when the steer was consumed by the turn', () => {
    expect(drain([steerRow('go north', 'consumed')], 'go north').meta?.landedFromSteer).toBeUndefined()
  })

  it('when the steer is still written, or taken back', () => {
    expect(drain([steerRow('go north', 'written')], 'go north').meta?.landedFromSteer).toBeUndefined()
    expect(drain([steerRow('go north', 'retracted')], 'go north').meta?.landedFromSteer).toBeUndefined()
  })

  it('when the slot holds no steer row', () => {
    const plain: ChatMessage = { role: 'user', content: 'go north', ts, cls: 'msg msg-u' }
    expect(drain([], 'go north').meta?.landedFromSteer).toBeUndefined()
    expect(drain([plain], 'go north').meta?.landedFromSteer).toBeUndefined()
  })

  it('when the requeued steer already has its own row, so a later send of the same text is not tied to it', () => {
    const successor: ChatMessage = { role: 'user', content: 'go north', ts: '2026-11-05T00:00:30Z', cls: 'msg msg-u' }
    expect(drain([steerRow('go north', 'superseded'), successor], 'go north').meta?.landedFromSteer).toBeUndefined()
  })

  it('when only the pop frame claims it: the mark is client-minted and a frame cannot set it', () => {
    expect(drain([], 'go north', { landedFromSteer: true }).meta?.landedFromSteer).toBeUndefined()
  })
})

describe('queue_pop of a requeued steer row that still carries its sendId', () => {
  // A crewmate steer: the requeue copies the `sendId` captured for the
  // persisted steer row onto the entry (`steer_queue.py`), and when the drain
  // cannot reuse that row the pop carries the same id.
  const composerSteer: ChatMessage = {
    role: 'user', content: 'check the logs', ts, cls: 'msg msg-u',
    meta: { mid: 'm-steer', steer: true, steerState: 'requeued', sendId: 's-1' },
  }

  it('still rebuilds the row and marks it, keeping the steer row', () => {
    const rebuilt = drain([composerSteer], 'check the logs', { sendId: 's-1' }, { requeuedSteer: true, sendId: 's-1' })
    expect(rebuilt.meta?.landedFromSteer).toBe('q-1')
    expect(rebuilt.meta?.steer).toBeUndefined()
  })
})

describe('queue_pop of a card the steer requeue did not push leaves the row unmarked', () => {
  it('when the card is an ordinary queued send of the same words', () => {
    expect(drain([steerRow('go north', 'requeued')], 'go north', undefined, {}).meta?.landedFromSteer).toBeUndefined()
  })

  it('when the requeued card was cancelled and the same words are sent again', () => {
    // A cancel removes the requeue's card but leaves the steer row `requeued`.
    const seeded = structuredClone(cold())
    seeded.slotMessages[slot] = [steerRow('go north', 'requeued')]
    let state = reducer(seeded, appendQueuedMessage({ slot, content: 'go north', ts: '2026-11-05T00:01:00Z', queue_id: 'q-steer', meta: { requeuedSteer: true } }))
    state = reducer(state, cancelQueuedMessage({ slot, queue_id: 'q-steer' }))
    state = reducer(state, appendQueuedMessage({ slot, content: 'go north', ts: '2026-11-05T00:02:00Z', queue_id: 'q-later' }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go north', queue_id: 'q-later' }))
    const rebuilt = rows(state).at(-1)
    expect(rebuilt?.role).toBe('user')
    expect(rebuilt?.meta?.landedFromSteer).toBeUndefined()
  })
})

describe('queue_pop of two steers requeued in the same tick', () => {
  it('marks each rebuilt row with its own card id, so each one flashes', () => {
    // Both cards carry the same `ts`, the one-shot key a ts would give.
    const sameTs = '2026-11-05T00:01:00Z'
    const seeded = structuredClone(cold())
    seeded.slotMessages[slot] = [steerRow('go north', 'requeued'), { ...steerRow('go south', 'requeued'), meta: { mid: 'm-2', steer: true, steerState: 'requeued' } }]
    let state = reducer(seeded, appendQueuedMessage({ slot, content: 'go north', ts: sameTs, queue_id: 'q-a', meta: { requeuedSteer: true } }))
    state = reducer(state, appendQueuedMessage({ slot, content: 'go south', ts: sameTs, queue_id: 'q-b', meta: { requeuedSteer: true } }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go north', queue_id: 'q-a' }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go south', queue_id: 'q-b' }))
    const marks = rows(state).filter(m => m.role === 'user' && !m.meta?.steer).map(m => m.meta?.landedFromSteer)
    expect(marks).toEqual(['q-a', 'q-b'])
  })
})
