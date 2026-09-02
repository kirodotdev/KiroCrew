import { describe, it, expect } from 'vitest'
import { friendlyErrText } from '../api/client'

describe('friendlyErrText', () => {
  it('unwraps {"error": …} to the human message with real newlines', () => {
    const body = JSON.stringify({ error: 'line one\n  sudo do-thing\nline two' })
    const out = friendlyErrText(500, body)
    expect(out).toBe('line one\n  sudo do-thing\nline two')
    // No raw JSON envelope / escaped sequences leak through.
    expect(out).not.toContain('{"error"')
    expect(out).not.toContain('\\n')
  })

  it('falls back to detail/message fields', () => {
    expect(friendlyErrText(500, JSON.stringify({ detail: 'boom' }))).toBe('boom')
    expect(friendlyErrText(500, JSON.stringify({ message: 'kaboom' }))).toBe('kaboom')
  })

  it('returns the raw body when it is not JSON', () => {
    expect(friendlyErrText(500, 'plain text error')).toBe('plain text error')
  })

  it('returns the raw body when JSON has no known message field', () => {
    const body = JSON.stringify({ code: 42 })
    expect(friendlyErrText(500, body)).toBe(body)
  })

  it('keeps the 429 friendly message', () => {
    expect(friendlyErrText(429, '{"error":"x"}')).toContain('Rate limited')
  })
})
