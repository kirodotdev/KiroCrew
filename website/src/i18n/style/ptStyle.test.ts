/**
 * Portuguese style guards.
 *
 * Encodes mechanically checkable rules from `style/pt.md`.
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

const pt = bundle('pt')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('pt regional variant (style/pt.md §1)', () => {
  it('uses Brazilian Portuguese vocabulary, not European', () => {
    // Common European Portuguese words that should be Brazilian
    const EU_WORDS: Array<[RegExp, string]> = [
      [/\bficheiro/i, 'arquivo (BR)'],
      [/\becr[ãa]\b/i, 'tela (BR)'],
      [/\bdescarregar\b/i, 'baixar (BR)'],
      [/\btelemovel\b/i, 'celular (BR)'],
      [/\bordenador\b/i, 'computador (BR)'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(pt)) {
      for (const [pattern, preferred] of EU_WORDS) {
        if (pattern.test(value)) {
          bad.push(`${key}: European PT detected, prefer ${preferred}`)
        }
      }
    }
    expect(bad, report(bad)).toEqual([])
  })
})

describe('pt punctuation (style/pt.md §2)', () => {
  it('does not use guillemets (European Portuguese convention)', () => {
    const bad = Object.entries(pt)
      .filter(([, v]) => v.includes('«') || v.includes('»'))
      .map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })
})
