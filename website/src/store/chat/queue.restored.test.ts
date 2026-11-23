/**
 * A crewmate send's queued row keeps its place across a refresh (it is pinned),
 * and still takes the listing's restored and held marks: an entry the session reloaded
 * from disk is never drained, so the chat must know to draw it as a card.
 */
import { describe, expect, it } from 'vitest'
import { hydrateQueuedBubbles } from './queue'
import { normalizeQueueEntries } from './wire'
import { isNonInteractiveQueued } from '../../components/QueueStack'

const ts = '2026-11-06T09:00:00Z'
const row = (id: string, restored?: true) => ({ role: 'queued' as const, content: id, ts, meta: { queueId: id, responsiveSend: true, ...(restored ? { restored, held: true } : {}) } })

describe('pinned crewmate queued row restored and held marks', () => {
  it('takes the listing restored and held marks in place', () => {
    const list = [row('a'), { role: 'assistant' as const, content: 'reply', ts }, row('b')]
    const queue = normalizeQueueEntries([{ id: 'a', content: 'a', meta: { restored: true, held: true } }, { id: 'b', content: 'b', meta: { restored: true } }])
    const out = hydrateQueuedBubbles(list, queue)
    expect(out.map(r => r.content)).toEqual(['a', 'reply', 'b'])
    expect(out.map(r => r.meta?.restored)).toEqual([true, undefined, true])
    expect(out.map(r => r.meta?.held)).toEqual([true, undefined, undefined])
    expect(out[0].meta?.responsiveSend).toBe(true)
  })

  it('keeps a restored entry in the interactive stack the chat page draws', () => {
    const rows = hydrateQueuedBubbles([], normalizeQueueEntries([{ id: 'a', content: 'a', meta: { restored: true } }]))
    expect(rows.filter(m => m.role === 'queued' && !isNonInteractiveQueued(m)).map(m => m.meta?.queueId)).toEqual(['a'])
  })

  it('drops restored and held marks the listing no longer carries', () => {
    const queue = normalizeQueueEntries([{ id: 'a', content: 'a' }])
    const out = hydrateQueuedBubbles([row('a', true)], queue)[0]
    expect(out.meta?.restored).toBeUndefined()
    expect(out.meta?.held).toBeUndefined()
  })
})

