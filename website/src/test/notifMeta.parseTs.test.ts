import { describe, it, expect } from 'vitest'
import { parseTs, fmtFull } from '../components/notifications/notifMeta'

// A fixed reference instant: 2026-07-23T05:34:56.123Z
const REF_MS = Date.UTC(2026, 6, 23, 5, 34, 56, 123)

describe('parseTs epoch-unit normalization', () => {
  it('parses ISO 8601 strings', () => {
    expect(parseTs('2026-07-23T05:34:56.123+00:00').getTime()).toBe(REF_MS)
  })

  it('parses epoch seconds (string and number)', () => {
    const sec = Math.floor(REF_MS / 1000)
    expect(parseTs(String(sec)).getFullYear()).toBe(2026)
    expect(parseTs(sec).getFullYear()).toBe(2026)
  })

  it('parses fractional epoch seconds', () => {
    expect(parseTs(String(REF_MS / 1000)).getTime()).toBe(REF_MS)
  })

  // Regression: a millisecond epoch passed as a STRING used to render as
  // year ~58527 (new Date("<13-digit>") is Invalid in V8, then the old
  // fallback multiplied ms by 1000 as if it were seconds).
  it('parses epoch milliseconds as string without inflating the year', () => {
    const d = parseTs(String(REF_MS))
    expect(d.getFullYear()).toBe(2026)
    expect(d.getTime()).toBe(REF_MS)
  })

  it('parses epoch milliseconds as number', () => {
    expect(parseTs(REF_MS).getTime()).toBe(REF_MS)
  })

  // Regression: a microsecond epoch as a number used to render as ~58527.
  it('parses epoch microseconds (string and number)', () => {
    expect(parseTs(String(REF_MS * 1000)).getFullYear()).toBe(2026)
    expect(parseTs(REF_MS * 1000).getFullYear()).toBe(2026)
  })

  it('parses epoch nanoseconds (string and number)', () => {
    expect(parseTs(String(REF_MS * 1e6)).getFullYear()).toBe(2026)
    expect(parseTs(REF_MS * 1e6).getFullYear()).toBe(2026)
  })

  it('rejects garbage and pre-2020 timestamps as invalid', () => {
    expect(isNaN(parseTs('not-a-date').getTime())).toBe(true)
    expect(isNaN(parseTs('2019-01-01T00:00:00Z').getTime())).toBe(true)
  })

  it('fmtFull returns "Unknown date" for unparseable input, never a bogus year', () => {
    expect(fmtFull('not-a-date')).toBe('Unknown date')
    expect(fmtFull(String(REF_MS))).not.toContain('58527')
  })
})
