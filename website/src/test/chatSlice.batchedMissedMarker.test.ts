import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage } from '../store/chatSlice'
import './mockApiClient'

/**
 * Background-pane (applyNonActiveFrame) batched-chunk gap detection.
 *
 * The live WS flush buffer (useWebSocket) buffers streaming chunks per slot,
 * owns cross-chunk gap detection itself (inlining the missedChunkMarker into the
 * merged content), and dispatches ONE batched frame per slot carrying only the
 * batch's LAST seq with `batched: true`. Both the active-slot and non-active
 * reducer paths guard their marker branch with `!batched` so they do not
 * double-count: comparing consecutive batches' last-seqs (whose natural
 * difference is the batch size) would otherwise fabricate a false
 * "[N chunk(s) missed]" marker on every multi-chunk background batch.
 */

const SLOT = 'grid-slot'
const OTHER = 'focused-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

describe('applyNonActiveFrame batched missed-chunk marker', () => {
  it('does NOT fabricate a marker across consecutive batched frames', () => {
    // Background pane: OTHER is focused, so SLOT frames route through
    // applyNonActiveFrame. Two batched flushes, contiguous seqs (batch A = 1..3
    // -> lastSeq 3; batch B = 4..6 -> lastSeq 6). No real gap; the buffer would
    // have inlined nothing.
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Hello ', seq: 3, batched: true }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'world', seq: 6, batched: true }))

    const streamed = (s.slotMessages[SLOT] ?? []).map(m => m.content).join('')
    expect(streamed).toBe('Hello world')
    expect(streamed).not.toMatch(/chunk\(s\) missed/)
  })

  it('matches the active path for the same batched sequence (parity)', () => {
    const frames = [
      { slot: SLOT, role: 'chunk', content: 'Hello ', seq: 3, batched: true },
      { slot: SLOT, role: 'chunk', content: 'world', seq: 6, batched: true },
    ]
    let active = { ...init(), activeSlot: SLOT }
    for (const f of frames) active = reducer(active, sseChatMessage(f))
    let bg = { ...init(), activeSlot: OTHER }
    for (const f of frames) bg = reducer(bg, sseChatMessage(f))

    const activeText = active.messages.map(m => m.content).join('')
    const bgText = (bg.slotMessages[SLOT] ?? []).map(m => m.content).join('')
    expect(bgText).toBe(activeText)
    expect(bgText).toBe('Hello world')
  })

  it('still detects a real gap on the non-batched (legacy/test) path', () => {
    // Without `batched`, the reducer owns gap detection. A jump from seq 1 to 3
    // means seq 2 was dropped -> exactly one missed-chunk marker.
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'a', seq: 1 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'b', seq: 3 }))

    const streamed = (s.slotMessages[SLOT] ?? []).map(m => m.content).join('')
    expect(streamed).toContain('[1 chunk(s) missed]')
  })
})
