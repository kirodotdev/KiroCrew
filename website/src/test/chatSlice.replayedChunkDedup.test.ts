import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage } from '../store/chatSlice'
import './mockApiClient'

/**
 * Replayed-chunk idempotency guard.
 *
 * WS delivery is at-least-once: a reconnect replay or a retry re-stream can
 * redeliver a streaming chunk the client already applied. missedChunkMarker
 * only flags FORWARD gaps (curSeq - prevSeq - 1 > 0), so a repeated/backward
 * seq produced NO marker and its content was appended a second time — the
 * silent mid-stream "stutter" (e.g. "So opus-4.8 ISSo opus-4.8 IS...").
 *
 * Both reducer chunk paths (active `sseChatMessage` and background
 * `applyNonActiveFrame`) drop a chunk whose seq <= the last-seen seq on the
 * non-batched path. Batched frames are pre-deduped by the WS flush buffer
 * (useWebSocket), so the guard is gated with `!batched` — mirroring the
 * missedChunkMarker gating so the two cannot drift.
 */

const SLOT = 'active-slot'
const OTHER = 'focused-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

describe('replayed-chunk dedup (active path)', () => {
  it('does not double-append a chunk redelivered with the same seq', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))
    // Same seq redelivered (reconnect replay / retry re-stream).
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))

    const text = s.messages.map(m => m.content).join('')
    expect(text.match(/So opus-4.8 IS/g)?.length).toBe(1)
  })

  it('drops a backward-seq replay but keeps the forward chunk that followed', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A', seq: 5 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'B', seq: 6 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A', seq: 5 })) // stale replay

    expect(s.messages.map(m => m.content).join('')).toBe('AB')
  })

  it('still appends normal forward-progress chunks (guard does not over-drop)', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'a', seq: 1 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'b', seq: 2 }))

    expect(s.messages.map(m => m.content).join('')).toBe('ab')
  })
})

describe('replayed-chunk dedup (background / applyNonActiveFrame path)', () => {
  it('does not double-append a redelivered chunk for a non-active slot', () => {
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))

    const streamed = (s.slotMessages[SLOT] ?? []).map(m => m.content).join('')
    expect(streamed.match(/So opus-4.8 IS/g)?.length).toBe(1)
  })
})
