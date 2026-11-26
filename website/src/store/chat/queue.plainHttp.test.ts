/**
 * A dashboard opened over plain HTTP from another machine has no
 * `crypto.randomUUID`. A queued row that arrives without an id still needs one
 * on both doors: the live `queue_push` frame and the slot-detail queue a
 * refresh rebuilds from.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage } from '../chatSlice'
import { normalizeQueueEntries } from './wire'
import { stubPlainHttpCrypto } from '../../test/plainHttpCrypto'

const slot = 'plain-http-slot'
const ts = '2026-11-06T09:00:00Z'

function expectTwoDistinctIds(ids: unknown[]) {
  expect(ids).toHaveLength(2)
  expect(ids.every(id => typeof id === 'string' && id.length > 0)).toBe(true)
  expect(new Set(ids).size).toBe(2)
}

describe('queued row id on a plain-HTTP dashboard', () => {
  it('gives a pushed card that carries no queue_id an id of its own', () => {
    stubPlainHttpCrypto()
    let state = reducer(reducer(undefined, { type: '@@INIT' }), appendQueuedMessage({ slot, content: 'first', ts }))
    state = reducer(state, appendQueuedMessage({ slot, content: 'second', ts }))
    expectTwoDistinctIds((state.slotMessages[slot] ?? []).map(row => row.meta?.queueId))
  })

  it('gives a plain-string slot-detail entry an id of its own', () => {
    stubPlainHttpCrypto()
    expectTwoDistinctIds(normalizeQueueEntries(['first', 'second']).map(entry => entry.queueId))
  })
})
