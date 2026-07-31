/**
 * Italian style guards.
 *
 * Encodes mechanically checkable rules from `style/it.md`.
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

const it_ = bundle('it')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('it tone (style/it.md §4)', () => {
  it('uses tu (informal), not Lei (formal)', () => {
    // "Lei" as formal pronoun — capitalized at sentence start is ambiguous,
    // but "Lei" mid-sentence is the formal form
    const bad = Object.entries(it_)
      .filter(([, v]) => /\bLei\b/.test(v) && !/[.!?]\s*Lei/.test(v))
      .map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })
})

describe('it accents (style/it.md §7)', () => {
  it('final vowels have their required accents', () => {
    // Common violations: "perche" should be "perché", "piu" should be "più"
    const MUST_ACCENT: Array<[RegExp, string]> = [
      [/\bperche\b/i, 'perché'],
      [/\bpiu\b/i, 'più'],
      [/\bgia\b/i, 'già'],
      [/\bcioe\b/i, 'cioè'],
      [/\bpuo\b/i, 'può'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(it_)) {
      for (const [wrong, correct] of MUST_ACCENT) {
        if (wrong.test(value)) {
          bad.push(`${key}: has unaccented form, should be '${correct}'`)
        }
      }
    }
    expect(bad, report(bad)).toEqual([])
  })
})
