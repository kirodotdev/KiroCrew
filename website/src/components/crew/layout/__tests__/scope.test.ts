/**
 * The pure-data scope value shipped in PR 1: only the `Subject` type and the
 * fixed key it is read under. The runtime scope bus is PR 3. This test exists to
 * exercise the one runtime statement (`SUBJECT_KEY`) — the `Subject` interface is
 * compile-time only — so the file is not reported as 0% covered.
 */
import { describe, it, expect } from 'vitest'
import { SUBJECT_KEY, type Subject } from '../scope'

describe('scope (pure-data subject)', () => {
  it('publishes the fixed subject key', () => {
    expect(SUBJECT_KEY).toBe('subject')
  })

  it('a Subject carries just a slot', () => {
    const s: Subject = { slot: 'chat-123' }
    expect(s.slot).toBe('chat-123')
  })
})
