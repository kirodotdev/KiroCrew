/** The browser mirror of the exfil base64 heuristic treats `=` as trailing
 *  padding, not a joiner, in step with the backend `_EXFIL_PATTERNS`
 *  (test/test_exfil_base64_padding.py pins the Python side).
 *
 *  A `name=<32-char id>` link now renders; padded base64, AWS key ids, heavy
 *  percent encoding, a 40-hex value and a payload in the parameter name are all
 *  still redacted. URLs are built from parts on purpose.
 */
import { describe, it, expect } from 'vitest'
import { sanitizeExfiltrationUrls } from '../utils/sanitize'

const COURSE_ID = 'COURSE2026041420115089b6a4269f01' // invented, 32 chars

function url(query: string): string {
  return 'https://learn.example.com/t/view?' + query
}

function isRedacted(u: string): boolean {
  return !sanitizeExfiltrationUrls(`see ${u} for details`).includes(u)
}

describe('sanitizeExfiltrationUrls: `=` is base64 padding, not a joiner', () => {
  it('keeps a name=<32-char id> link the old class fused into one run', () => {
    const query = 'trainingId=' + COURSE_ID + '&lms=LEARN'
    expect(/[A-Za-z0-9+/=]{40,}/.test(query)).toBe(true)
    const text = `see ${url(query)} for details`
    expect(sanitizeExfiltrationUrls(text)).toBe(text)
  })

  it('redacts a 40-char base64 value', () => {
    expect(isRedacted(url('lms=LEARN&id=' + 'Ab3/'.repeat(10)))).toBe(true)
  })

  it('redacts a double-padded base64 secret', () => {
    const blob = btoa('S'.repeat(34))
    expect(blob.endsWith('==')).toBe(true)
    expect(isRedacted(url('d=' + blob + '&lms=LEARN'))).toBe(true)
  })

  it('redacts a minimum-length padded payload (38 + `==`, 39 + `=`)', () => {
    for (const size of [28, 29]) {
      const blob = btoa('S'.repeat(size))
      expect(blob.length).toBe(40)
      expect(isRedacted(url('d=' + blob + '&lms=LEARN'))).toBe(true)
    }
  })

  it('redacts an AWS-key-shaped value', () => {
    expect(isRedacted(url('k=' + 'AKIA' + 'IOSFODNN7EXAMPLE'))).toBe(true)
  })

  it('redacts heavy percent encoding', () => {
    expect(isRedacted(url('q=' + '%41'.repeat(25)))).toBe(true)
  })

  it('redacts a 40-hex value', () => {
    expect(isRedacted(url('sha=' + '0123456789abcdef'.repeat(2) + '01234567'))).toBe(true)
  })

  it('redacts a payload carried in the parameter name', () => {
    expect(isRedacted(url('Ab3x'.repeat(10) + '=1'))).toBe(true)
  })

  it('treats an `=` split like the `&` split that already passed, bounded by length', () => {
    const half = 'Ab3x'.repeat(5)
    expect(isRedacted(url(`a=${half}&b=${half}`))).toBe(false)
    expect(isRedacted(url(`a=${half}=${half}`))).toBe(false)
    const longSplit = Array(7).fill('Ab3x'.repeat(7) + 'Ab').join('=')
    expect(isRedacted(url('a=' + longSplit))).toBe(true)
  })
})
