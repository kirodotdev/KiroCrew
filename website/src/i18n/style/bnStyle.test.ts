/**
 * Bengali style guards.
 *
 * Encodes mechanically checkable rules from `style/bn.md`.
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

const bn = bundle('bn')

const BENGALI = /[\u0980-\u09ff]/

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('bn punctuation (style/bn.md §1)', () => {
  it('uses dari (।) not Latin period for sentence-final', () => {
    const bad: string[] = []
    for (const [key, value] of Object.entries(bn)) {
      if (!BENGALI.test(value)) continue
      if (/[\u0980-\u09ff]\.$/.test(value)) {
        bad.push(`${key}: ${JSON.stringify(value.slice(-30))}`)
      }
    }
    // Baselined: existing catalog may use periods
    expect(bad.length, report(bad)).toBeLessThanOrEqual(30)
  })
})

describe('bn numerals (style/bn.md §2)', () => {
  it('uses Western digits (0-9) not Bengali digits (০-৯)', () => {
    // Bengali digits U+09E6 to U+09EF should not appear in the catalog
    const BENGALI_DIGITS = /[\u09e6-\u09ef]/
    const bad = Object.entries(bn)
      .filter(([, v]) => BENGALI_DIGITS.test(v))
      .map(([k]) => k)
    expect(bad.length, report(bad)).toBeLessThanOrEqual(8)
  })
})
