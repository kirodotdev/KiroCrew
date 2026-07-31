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
    // Check for आप (formal) — should use तुम (informal)
    const bad = Object.entries(hi)
      .filter(([, v]) => v.includes('आप'))
      .map(([k]) => k)
    // Baselined: existing catalog likely uses आप extensively
    expect(bad.length, report(bad)).toBeLessThanOrEqual(127)
  })
})
