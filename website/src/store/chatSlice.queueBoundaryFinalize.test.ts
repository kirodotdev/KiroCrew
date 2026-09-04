import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage } from './chatSlice'
import { parseOptions } from '../app-sdk/protocol/options'
import '../test/mockApiClient'

/**
 * Reducer contract behind the queue-boundary finalize fix (backend
 * `_start_next_queued_turn` / `_run_pending_synthesis` broadcasting
 * `chat_segment` before dispatching a successor turn).
 *
 * The chunk reducer deliberately appends into the LAST live `streaming` row,
 * so the frame between two turns is what separates them. Two frames can
 * finalize that row: the flush's `chat_message{role:'assistant'}` (which is
 * CONDITIONAL server-side -- suppressed while an HTTP SSE reader drains the
 * slot, absent when the final segment is empty, droppable by the mid-keyed
 * redelivery guard) and `chat_segment` (unconditional, idempotent -- what the
 * backend fix emits at the boundary). These tests drive the REAL reducer and
 * the REAL `OPTION_MARKER_RE` (via `parseOptions`) and pin:
 *
 *  - the fixed frame sequence yields TWO rows with a parseable first-row
 *    `[OPTIONS: ...]` marker;
 *  - either finalizing frame alone separates the turns, and applying both is
 *    idempotent (the fix cannot double-finalize a client that also got the
 *    assistant frame);
 *  - WITHOUT any finalizing frame the turns merge and the marker degrades --
 *    the defect mechanism the unconditional frame exists to close.
 */

const SLOT = 'chat-boundary'
const init = () => ({ ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT })

const TURN1_CHUNKS = ['Pick a path.\n', '[OPTIONS: Alpha | Bravo]']
const TURN1_TEXT = TURN1_CHUNKS.join('')
const TURN2_CHUNK = 'Second turn reply.'

const apply = (frames: Array<Parameters<typeof sseChatMessage>[0]>) => {
  let state = init()
  for (const f of frames) state = reducer(state, sseChatMessage(f))
  return state
}

const textRows = (state: ReturnType<typeof apply>) =>
  state.messages.filter(m => m.role === 'assistant' || m.role === 'streaming')

describe('queue-boundary finalize frame (defect mechanism + fixed contract)', () => {
  it('with the chat_segment finalize: two rows, and the first row\'s [OPTIONS:] parses', () => {
    const state = apply([
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[0], seq: 1 },
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[1], seq: 2 },
      { slot: SLOT, role: '_segment', content: '' },
      { slot: SLOT, role: 'chunk', content: TURN2_CHUNK, seq: 3 },
      { slot: SLOT, role: '_done', content: '' },
    ])

    const rows = textRows(state)
    expect(rows.map(m => ({ role: m.role, content: m.content }))).toEqual([
      { role: 'assistant', content: TURN1_TEXT },
      { role: 'assistant', content: TURN2_CHUNK },
    ])
    expect(parseOptions(rows[0].content).options).toEqual(['Alpha', 'Bravo'])
  })

  it('the assistant chat_message frame alone also separates the turns (when it arrives)', () => {
    // The end-of-turn flush's `slot.append` emits this frame on paths where it
    // is not suppressed; a client that receives it never merges. It is the
    // conditional half of the boundary's frame pair.
    const state = apply([
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[0], seq: 1 },
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[1], seq: 2 },
      { slot: SLOT, role: 'assistant', content: TURN1_TEXT, meta: { mid: 'm-t1' } },
      { slot: SLOT, role: 'chunk', content: TURN2_CHUNK, seq: 3 },
      { slot: SLOT, role: '_done', content: '' },
    ])

    const rows = textRows(state)
    expect(rows.map(m => ({ role: m.role, content: m.content }))).toEqual([
      { role: 'assistant', content: TURN1_TEXT },
      { role: 'assistant', content: TURN2_CHUNK },
    ])
    expect(parseOptions(rows[0].content).options).toEqual(['Alpha', 'Bravo'])
  })

  it('both frames together (the fixed backend boundary) do not double-finalize', () => {
    // The fixed backend emits chat_segment right after the flush already sent
    // the assistant frame: the second finalize must find no streaming row and
    // be a no-op, never mint an extra row or clobber the finalized one.
    const state = apply([
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[0], seq: 1 },
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[1], seq: 2 },
      { slot: SLOT, role: 'assistant', content: TURN1_TEXT, meta: { mid: 'm-t1' } },
      { slot: SLOT, role: '_segment', content: '' },
      { slot: SLOT, role: 'chunk', content: TURN2_CHUNK, seq: 3 },
      { slot: SLOT, role: '_done', content: '' },
    ])

    const rows = textRows(state)
    expect(rows.map(m => ({ role: m.role, content: m.content }))).toEqual([
      { role: 'assistant', content: TURN1_TEXT },
      { role: 'assistant', content: TURN2_CHUNK },
    ])
  })

  it('without any finalizing frame the turns merge and the marker degrades (the defect)', () => {
    const state = apply([
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[0], seq: 1 },
      { slot: SLOT, role: 'chunk', content: TURN1_CHUNKS[1], seq: 2 },
      // Neither `_segment` nor an assistant frame between the turns -- the
      // shape a client sees when the conditional assistant frame is suppressed
      // and no boundary finalize is emitted (the pre-fix backend).
      { slot: SLOT, role: 'chunk', content: TURN2_CHUNK, seq: 3 },
      { slot: SLOT, role: '_done', content: '' },
    ])

    const rows = textRows(state)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe(TURN1_TEXT + TURN2_CHUNK)

    // The marker no longer ends its line, so the real regex declines it:
    // the pills degrade to literal prose. This is what makes the backend's
    // unconditional boundary finalize load-bearing rather than cosmetic.
    expect(parseOptions(rows[0].content).options).toEqual([])
  })
})
