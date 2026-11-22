import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage } from '../store/chatSlice'
import './mockApiClient'

/**
 * A reply must not open with "[N chunk(s) missed]" because of chunks that
 * belong to OTHER turns.
 *
 * The chunk seq is a per-slot counter that continues across turns, and the
 * replay floor is kept past `_done` so a redelivered chunk of the ended turn is
 * still dropped. Turns this tab never streamed (it was not watching the slot,
 * or the turn ran while another slot was open) advance the counter without
 * moving the floor. A turn that starts with no user echo -- a work-ledger wake,
 * a cron or a monitor cycle -- leaves the stale floor in place, so the first
 * chunk of its reply arrived N seqs above it and the reply streamed with a
 * leading marker for N chunks it never contained.
 *
 * The marker reports a hole inside the text on screen. A chunk that opens a new
 * streaming row has no text before it to have a hole in, so it gets none; a gap
 * between two chunks of the same row still does.
 */

const SLOT = 'bot-slot'
const OTHER = 'other-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

const batched = (slot: string, parts: { seq: number; text: string }[]) =>
  sseChatMessage({
    slot,
    role: 'chunk',
    content: parts.map(p => p.text).join(''),
    seq: parts[parts.length - 1].seq,
    batched: true,
    parts,
  })

const streamedText = (messages: { role: string; content: string }[]) =>
  messages.filter(m => m.role === 'streaming').map(m => m.content).join('')

describe('a turn that starts without a user echo', () => {
  it('opens its reply without a marker for chunks of earlier turns', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, batched(SLOT, [{ seq: 1, text: 'first ' }, { seq: 2, text: 'turn' }]))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    // Seqs 3..17 streamed in turns this tab never saw. The next turn is a wake:
    // its first frame is a tool call, not a user echo.
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'tool', content: 'work_brief', meta: { tool_call_id: 't1' } }))
    s = reducer(s, batched(SLOT, [{ seq: 18, text: 'Both PRs ' }, { seq: 19, text: 'are open.' }]))

    const text = streamedText(s.messages)
    expect(text).toBe('Both PRs are open.')
    expect(text).not.toMatch(/chunk\(s\) missed/)
  })

  it('still drops a redelivered chunk of the ended turn', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, batched(SLOT, [{ seq: 1, text: 'first ' }, { seq: 2, text: 'turn' }]))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    s = reducer(s, batched(SLOT, [{ seq: 2, text: 'turn' }]))

    expect(streamedText(s.messages)).toBe('')
  })

  it('still marks a gap inside the reply on screen', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, batched(SLOT, [{ seq: 1, text: 'a' }]))
    s = reducer(s, batched(SLOT, [{ seq: 4, text: 'b' }]))

    expect(streamedText(s.messages)).toBe('a\n[2 chunk(s) missed]\nb')
  })

  it('does the same on a background pane', () => {
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, batched(SLOT, [{ seq: 1, text: 'first' }]))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'tool', content: 'work_brief', meta: { tool_call_id: 't1' } }))
    s = reducer(s, batched(SLOT, [{ seq: 16, text: 'reply' }]))

    const text = streamedText(s.slotMessages[SLOT] ?? [])
    expect(text).toBe('reply')
    expect(text).not.toMatch(/chunk\(s\) missed/)
  })
})
