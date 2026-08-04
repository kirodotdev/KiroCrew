/**
 * Spanish style guards.
 *
 * Encodes mechanically checkable rules from `style/es.md`. Runs alongside the
 * shared gates (catalogParity, qa.test) which already check placeholders and
 * plural categories.
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

const en = bundle('en')
const es = bundle('es')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('es punctuation (style/es.md §1)', () => {
  it('questions have inverted question mark ¿', () => {
    // Any value ending with ? that is a real question (not code) should start with ¿
    const bad: string[] = []
    for (const [key, value] of Object.entries(es)) {
      if (!value.endsWith('?')) continue
      // Skip if English doesn't end with ? (might be a placeholder pattern)
      if (!(en[key] ?? '').endsWith('?')) continue
      // Must contain ¿ somewhere
      if (!value.includes('¿')) {
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    expect(bad, report(bad)).toEqual([])
  })

  it('exclamations have inverted exclamation mark ¡', () => {
    const bad: string[] = []
    for (const [key, value] of Object.entries(es)) {
      if (!value.endsWith('!')) continue
      if (!(en[key] ?? '').endsWith('!')) continue
      if (!value.includes('¡')) {
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    expect(bad, report(bad)).toEqual([])
  })
})

describe('es tone (style/es.md §4)', () => {
  it('does not use usted forms', () => {
    // Check for common usted conjugations that would indicate formal register
    const bad = Object.entries(es)
      .filter(([, v]) => /\busted\b/i.test(v))
      .map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })
})
