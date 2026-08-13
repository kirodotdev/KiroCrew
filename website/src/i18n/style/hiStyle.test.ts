/**
 * Hindi style guards.
 *
 * Encodes mechanically checkable rules from `style/hi.md`.
 */

import { describe, it, expect } from 'vitest'
import { CATALOGS as RUNTIME_CATALOGS } from '../index'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const bundle = (code: string) =>
  flatten((RUNTIME_CATALOGS as Record<string, { translation: unknown }>)[code].translation)

const hi = bundle('hi')

const DEVANAGARI = /[\u0900-\u097f]/

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('hi punctuation (style/hi.md §1)', () => {
  it('uses purna viram (।) not Latin period for sentence-final', () => {
    // Values containing Devanagari that end with a Latin period should use ।
    const bad: string[] = []
    for (const [key, value] of Object.entries(hi)) {
      if (!DEVANAGARI.test(value)) continue
      // Ends with period after a Devanagari character
      if (/[\u0900-\u097f]\.$/.test(value)) {
        bad.push(`${key}: ${JSON.stringify(value.slice(-30))}`)
      }
    }
    // Baselined: existing catalog may use periods
    expect(bad.length, report(bad)).toBeLessThanOrEqual(30)
  })
})

describe('hi tone (style/hi.md §4)', () => {
  it('does not use formal आप', () => {
    // Formal आप should be तुम (informal). `अपने-आप` / `अपने आप` is a DIFFERENT
    // word meaning "automatically" and merely contains those two letters, so a
    // substring check conflates them: of the 127 values matching the substring,
    // 8 are `अपने-आप` and have nothing to do with formality, leaving 119 genuine.
    //
    // `आपत्ति` ("objection") is the same class of false positive — a distinct
    // noun that happens to begin with those letters, and the natural Hindi word
    // §4 asks for over a loanword. It is stripped for the same reason, so a value
    // is judged on the pronoun rather than on any word starting आप.
    const bad = Object.entries(hi)
      .filter(([, v]) => v.replace(/अपने[-\s]?आप/g, '').replace(/आपत्ति/g, '').includes('आप'))
      .map(([k]) => k)
    // Baselined: the existing catalog uses आप extensively.
    expect(bad.length, report(bad)).toBeLessThanOrEqual(119)
  })
})
