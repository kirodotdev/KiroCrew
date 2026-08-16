/**
 * A mid-turn steer must not be read as a turn boundary by the reasoning paths.
 *
 * A steered message is injected INTO the running turn and is stored as a
 * `user` row carrying `meta.steer`. Two reasoning paths scan for a turn
 * boundary by looking for a `user` row, and both must skip a steered one:
 *
 *  - `sseThinkingChunk` scans BACK for the turn's existing block. Reading the
 *    steer as a boundary mints a second block for one turn.
 *  - `mergePreservedThinking` (the refresh fired on `chat_done`) scans FORWARD
 *    from a block for the assistant row that anchors it. Reading the steer as a
 *    boundary leaves the anchor null, and an unanchored block is appended at
 *    the tail — below the answer — from where the forward scan can never reach
 *    an assistant row again, so it sticks there and re-appends on every
 *    refresh.
 *
 * Both are asserted through the real reducer, so the guard cannot be satisfied
 * by a test double that skips the scan.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseThinkingChunk, sseChatMessage, refreshSlot } from '../store/chatSlice'

type State = ReturnType<typeof reducer>

const SLOT = 'default'

/** A store with one committed user turn, ready to receive reasoning. */
function withOpenTurn(): State {
  let s = reducer(undefined, { type: '@@INIT' })
  s = { ...s, activeSlot: SLOT }
  s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'do the thing', ts: '100' }))
  return s
}

const thinkingRows = (s: State) => s.messages.filter(m => m.role === 'thinking')

describe('sseThinkingChunk turn-boundary scan', () => {
  it('keeps one block when a steer lands mid-turn', () => {
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'first thought ' }))
    // The steer arrives while the turn is still running.
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'also check X', ts: '101', meta: { steer: true } }))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'second thought' }))

    const rows = thinkingRows(s)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('first thought second thought')
  })

  it('still starts a new block on a real new turn', () => {
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'turn one' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'answer one', ts: '102' }))
    // A genuine user message — not a steer — IS a boundary.
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'next question', ts: '200' }))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'turn two' }))

    const rows = thinkingRows(s)
    expect(rows).toHaveLength(2)
    expect(rows.map(r => r.content)).toEqual(['turn one', 'turn two'])
  })
  it('treats an UNCONFIRMED steer as a boundary so a new turn cannot corrupt the old block', () => {
    // The idle race: the turn finished but `chat_done` has not been handled, so
    // the composer still reads busy and the text is sent as a steer. The backend
    // gates its steer branch on `slot.running`, so it starts a REAL turn instead
    // and no `steer_push` echo ever clears `meta.optimistic`. That row must count
    // as a boundary — exempting it would splice this new turn's reasoning onto
    // the finished turn's block.
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'turn one reasoning' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'answer one', ts: '102' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'raced text', ts: '103', meta: { steer: true, optimistic: true } }))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'turn two reasoning' }))

    const rows = thinkingRows(s)
    expect(rows).toHaveLength(2)
    // The finished turn's reasoning is untouched — no splice.
    expect(rows[0].content).toBe('turn one reasoning')
    expect(rows[1].content).toBe('turn two reasoning')
  })

  it('still merges once the server confirms the steer', () => {
    // Same shape, but the echo has reconciled the bubble (optimistic cleared),
    // so it is a real mid-turn steer and reasoning continues one block.
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'first ' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'steered', ts: '101', meta: { steer: true } }))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'second' }))

    expect(thinkingRows(s)).toHaveLength(1)
    expect(thinkingRows(s)[0].content).toBe('first second')
  })
})

describe('mergePreservedThinking anchoring across a steer', () => {
  /** Drive the refresh that fires on chat_done, returning the merged rows. */
  function refreshWith(s: State, serverMessages: Array<Record<string, unknown>>): State {
    return reducer(s, {
      type: refreshSlot.fulfilled.type,
      payload: { key: SLOT, messages: serverMessages, running: false, hasMore: false, queue: [], nextBefore: 0 },
      meta: { arg: SLOT },
    })
  }

  it('re-inserts the block above its answer when a steer sits between them', () => {
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'reasoning' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'also check X', ts: '101', meta: { steer: true } }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'the answer', ts: '102' }))

    // The server replays the turn without reasoning (it is never persisted).
    s = refreshWith(s, [
      { role: 'user', content: 'do the thing', ts: '100' },
      { role: 'user', content: 'also check X', ts: '101', meta: { steer: true } },
      { role: 'assistant', content: 'the answer', ts: '102' },
    ])

    const roles = s.messages.map(m => m.role)
    const thinkingIdx = roles.indexOf('thinking')
    const assistantIdx = roles.indexOf('assistant')
    expect(thinkingIdx).toBeGreaterThanOrEqual(0)
    // Anchored above its answer, NOT parked at the tail.
    expect(thinkingIdx).toBeLessThan(assistantIdx)
    expect(thinkingIdx).not.toBe(s.messages.length - 1)
    expect(thinkingRows(s)).toHaveLength(1)
  })

  it('reproduces the reported shape: one row above the answer, none at the tail', () => {
    // The screenshot's exact sequence: reasoning, a mid-turn steer, more
    // reasoning, then the answer — followed by the refresh chat_done fires.
    // Unfixed this yields TWO thinking rows sitting below the answer.
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'first thought ' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'steered', ts: '101', meta: { steer: true } }))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'second thought' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'the answer', ts: '102' }))

    s = refreshWith(s, [
      { role: 'user', content: 'do the thing', ts: '100' },
      { role: 'user', content: 'steered', ts: '101', meta: { steer: true } },
      { role: 'assistant', content: 'the answer', ts: '102' },
    ])

    const rows = thinkingRows(s)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('first thought second thought')
    const roles = s.messages.map(m => m.role)
    expect(roles.indexOf('thinking')).toBeLessThan(roles.indexOf('assistant'))
    expect(s.messages[s.messages.length - 1].role).not.toBe('thinking')
  })
})
