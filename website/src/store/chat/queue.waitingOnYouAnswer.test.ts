/** A validated queued waiting-on-you answer claim survives both queue wire paths. */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, editQueuedMessage, hydrateQueuedBubbles, normalizeQueueEntries } from '../chatSlice'

const slot = 'waiting-answer-slot'
const ts = '2026-11-20T04:00:00Z'
const claim = { key: 'deploy', ask_id: 'wy-0000000000000001' }
const cold = () => reducer(undefined, { type: '@@INIT' })

describe('queued waiting-on-you answer claim', () => {
  it('survives a live queue_push frame', () => {
    const state = reducer(cold(), appendQueuedMessage({
      slot,
      content: 'Done',
      ts,
      queue_id: 'q-live',
      waiting_on_you_answer: claim,
    }))
    expect(state.slotMessages[slot]?.[0].meta?.waitingOnYouAnswer).toEqual(claim)
  })

  it('is dropped when an edit changes the text, as the server drops it', () => {
    const queued = reducer(cold(), appendQueuedMessage({
      slot,
      content: 'Done',
      ts,
      queue_id: 'q-edit',
      waiting_on_you_answer: claim,
    }))
    const sameText = reducer(queued, editQueuedMessage({ slot, queue_id: 'q-edit', content: 'Done' }))
    expect(sameText.slotMessages[slot]?.[0].meta?.waitingOnYouAnswer).toEqual(claim)
    const edited = reducer(queued, editQueuedMessage({ slot, queue_id: 'q-edit', content: 'Done, and rolled back' }))
    expect(edited.slotMessages[slot]?.[0].meta?.waitingOnYouAnswer).toBeUndefined()
  })

  it('survives slot-detail hydration and rejects malformed claims', () => {
    const queue = normalizeQueueEntries([
      { id: 'q-good', content: 'Done', waiting_on_you_answer: claim },
      { id: 'q-extra', content: 'No', waiting_on_you_answer: { ...claim, extra: 'private' } },
      { id: 'q-bad', content: 'No', waiting_on_you_answer: { key: 'deploy', ask_id: 7 } },
    ])
    const rows = hydrateQueuedBubbles([], queue)
    expect(rows[0].meta?.waitingOnYouAnswer).toEqual(claim)
    expect(rows[1].meta?.waitingOnYouAnswer).toBeUndefined()
    expect(rows[2].meta?.waitingOnYouAnswer).toBeUndefined()
  })
})
