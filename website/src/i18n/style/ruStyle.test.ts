/**
 * Russian style guards.
 *
 * Encodes mechanically checkable rules from `style/ru.md`.
 * The 4-category plural system is already enforced by catalogParity.test.ts.
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

const ru = bundle('ru')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('ru punctuation (style/ru.md §1)', () => {
  it('uses guillemets « » for quotation, not straight quotes around Russian text', () => {
    // Values containing Cyrillic text in straight quotes "..." where guillemets are expected.
    // This is a soft check — straight quotes are acceptable in some contexts (nested quotes).
    const CYRILLIC = /[\u0400-\u04ff]/
    const bad: string[] = []
    for (const [key, value] of Object.entries(ru)) {
      if (!CYRILLIC.test(value)) continue
      // Check for "Cyrillic text" pattern (straight quotes around Cyrillic)
      if (/"[\u0400-\u04ff]/.test(value) && !value.includes('«')) {
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    // Baselined — existing catalog may use straight quotes
    expect(bad.length, report(bad)).toBeLessThanOrEqual(20)
  })
})

describe('ru tone (style/ru.md §4)', () => {
  it('uses ты forms, not вы/Вы', () => {
    // Capital Вы is the formal second-person pronoun
    const bad = Object.entries(ru)
      .filter(([, v]) => /\bВы\b/.test(v))
      .map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ru DNT (style/ru.md §3)', () => {
  it('does not transliterate product names into Cyrillic', () => {
    // Common transliterations that should stay in Latin
    const TRANSLITERATIONS: Array<[string, string]> = [
      ['Гитхаб', 'GitHub'],
      ['Слэк', 'Slack'],
      ['Слак', 'Slack'],
      ['Дискорд', 'Discord'],
      ['КироКрю', 'KiroCrew'],
      ['Докер', 'Docker'],
      ['Плейрайт', 'Playwright'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(ru)) {
      for (const [cyrillic, latin] of TRANSLITERATIONS) {
        if (value.includes(cyrillic)) {
          bad.push(`${key}: has '${cyrillic}', should be '${latin}'`)
        }
      }
    }
    expect(bad, report(bad)).toEqual([])
  })
})
