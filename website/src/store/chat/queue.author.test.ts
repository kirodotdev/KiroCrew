/**
 * A waiting send keeps its server-stamped author on the live queue path: the
 * `queue_push` row and cold hydration of the queue. A crewmate's chat draws a
 * waiting send as a message bubble (`withQueuedAsBubbles`), so without the
 * author a teammate's send behind a running turn reads as the reader's own
 * until the turn drains it.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, hydrateSlotMessages } from '../chatSlice'
import { messageAuthor } from '../../api/sharedCrewUi'
import { withQueuedAsBubbles } from '../../components/chat/crewmateBubbles'

const slot = 'author-slot'
const ts = '2026-11-24T19:00:00Z'
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []
const author = { id: 'p_sam', kind: 'human', display: { name: 'Sam Avila', login: 'sam', captured_at: 1 } }

describe('queued author', () => {
  it('rides the queue_push row and the bubble a crewmate chat draws for it', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'second thought', ts, queue_id: 'q1', meta: { sendId: 's1', author } }))
    const [queued] = rows(state)
    expect(messageAuthor(queued.meta)?.id).toBe('p_sam')

    const [bubble] = withQueuedAsBubbles(rows(state), [queued])
    expect(bubble.role).toBe('user')
    expect(messageAuthor(bubble.meta)?.display.name).toBe('Sam Avila')
  })

  it('comes back from cold queue hydration', () => {
    const state = reducer(cold(), {
      type: hydrateSlotMessages.type,
      payload: { slot, messages: [], queue: [{ id: 'q2', content: 'second thought', meta: { author } }], bounded: false },
    })
    expect(messageAuthor(rows(state).find(m => m.role === 'queued')?.meta)?.id).toBe('p_sam')
  })

  it('is absent on an entry the server stamped no author on, and a malformed one is dropped', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: 'plain', ts, queue_id: 'q3', meta: { sendId: 's3' } }))
    state = reducer(state, appendQueuedMessage({ slot, content: 'odd', ts, queue_id: 'q4', meta: { author: { id: '' } } }))
    state = reducer(state, appendQueuedMessage({ slot, content: 'odder', ts, queue_id: 'q5', meta: { author: 'p_sam' } }))
    expect(rows(state).map(m => 'author' in (m.meta ?? {}))).toEqual([false, false, false])
  })
})
