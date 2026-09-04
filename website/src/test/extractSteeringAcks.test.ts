import { describe, it, expect } from 'vitest'
import { extractSteeringAcks } from '../app-sdk/protocol'

// extractSteeringAcks pulls kiro-cli's `[STEERING steer-<hex>: <summary>]`
// acknowledgment markers out of assistant prose so the raw tag never renders;
// the caller shows a chip per returned summary instead.
describe('extractSteeringAcks', () => {
  it('strips a single marker and returns its summary', () => {
    const input = 'Stopping early.\n\n[STEERING steer-abc123: Stopped at phase 4 as requested.]'
    const { cleaned, acks } = extractSteeringAcks(input)
    expect(acks).toEqual(['Stopped at phase 4 as requested.'])
    expect(cleaned).toBe('Stopping early.')
    expect(cleaned).not.toContain('[STEERING')
  })

  it('extracts multiple markers in order', () => {
    const input = 'a [STEERING steer-1: first] b [STEERING steer-2: second] c'
    const { cleaned, acks } = extractSteeringAcks(input)
    expect(acks).toEqual(['first', 'second'])
    expect(cleaned).not.toContain('STEERING')
    expect(cleaned).toContain('a')
    expect(cleaned).toContain('c')
  })

  it('returns prose unchanged (trimmed) when there is no marker', () => {
    const input = 'Just a normal reply with no steering.'
    const { cleaned, acks } = extractSteeringAcks(input)
    expect(acks).toEqual([])
    expect(cleaned).toBe('Just a normal reply with no steering.')
  })

  it('collapses the blank gap the removed marker leaves behind', () => {
    const input = 'line one\n\n[STEERING steer-deadbeef: did the thing]\n\n'
    const { cleaned } = extractSteeringAcks(input)
    expect(cleaned).toBe('line one')
    expect(cleaned).not.toMatch(/\n{3,}/)
  })

  it('ignores a marker with an empty summary (no ack pushed)', () => {
    const input = 'text [STEERING steer-x: ] more'
    const { acks } = extractSteeringAcks(input)
    expect(acks).toEqual([])
  })

  it('matches the real kiro-cli marker shape', () => {
    const input =
      '[STEERING steer-fc22b5295fd641fbaa2bc669cdee1249: Stopped executing remaining commands (phases 5 and 6) and provided a summary of the 4 completed phases as requested.]'
    const { cleaned, acks } = extractSteeringAcks(input)
    expect(acks).toHaveLength(1)
    expect(acks[0]).toContain('Stopped executing remaining commands')
    expect(cleaned).toBe('')
  })
})
