/**
 * A live `queue_push` card keeps the entry's `kind` and `appLabel`, as queue
 * hydration does, so a cron or app card is classed the same either way.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage } from '../chatSlice'

const slot = 'kind-slot'
const ts = '2026-11-05T17:00:00Z'
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []

describe('queued card kind', () => {
  it('rides a pushed app card', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'from app', ts, queue_id: 'q1', meta: { kind: 'mcp_app_message', appLabel: 'demo/app' } }))
    expect(rows(state)[0].meta?.kind).toBe('mcp_app_message')
    expect(rows(state)[0].meta?.appLabel).toBe('demo/app')
  })

  it('ignores values that are not text', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'typed', ts, queue_id: 'q2', meta: { kind: 7, appLabel: '' } }))
    expect(rows(state)[0].meta?.kind).toBeUndefined()
    expect(rows(state)[0].meta?.appLabel).toBeUndefined()
  })
})
