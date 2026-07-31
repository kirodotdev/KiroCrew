/**
 * Do-not-translate (DNT) gate.
 *
 * Product names must survive translation verbatim. This test reads the `dnt` array from
 * `glossary.json` and fails when a translation drops a proper noun that is present in
 * the English source.
 *
 * The list is proper nouns only: abbreviations like PR, CR, API, CLI, URL are deliberately
 * absent because they behave as common nouns and inflect — Russian declines PR to
 * "пул-реквеста", which is correct translation, not a dropped term.
 *
 * 36 existing violations are baselined. They are the same six English keys repeated across
 * five languages (systematic cause: codemod sentence splitting placed the name at a
 * fragment boundary). Whether dropping a name is acceptable is a per-language judgement
 * for a reader, so it is ratcheted rather than fixed blind.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES } from './languages'
import glossary from './glossary.json'

const DNT_BASELINE = 36

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code))

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

const catalogs = Object.fromEntries(
  Object.entries(RUNTIME_CATALOGS)
    .filter(([code]) => !GENERATED.has(code))
    .map(([code, bundle]) => [code, flatten((bundle as { translation: unknown }).translation)]),
)
const en = catalogs[DEFAULT_LANGUAGE]

/** Word-boundary match that also refuses to fire inside a dotted identifier (`Node.js`). */
const boundary = (term: string) =>
  new RegExp(`(?<![\\w.])${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w.])`)

describe('glossary', () => {
  it('glossary.json is well formed', () => {
    expect(Array.isArray(glossary.dnt)).toBe(true)
    expect(glossary.dnt.length).toBeGreaterThan(10)
  })

  it('do-not-translate terms are not dropped in translation', () => {
    const dropped: string[] = []
    for (const [code, catalog] of Object.entries(catalogs)) {
      if (code === DEFAULT_LANGUAGE) continue
      for (const [key, value] of Object.entries(catalog)) {
        const source = en[key]
        if (source === undefined) continue
        for (const term of glossary.dnt) {
          const re = boundary(term)
          if (re.test(source) && !re.test(value)) dropped.push(`${code}:${key} [${term}]`)
        }
      }
    }
    expect(
      dropped.length,
      `${dropped.length} translations dropped a product name (baseline ${DNT_BASELINE}).\n`
        + `${dropped.slice(0, 8).map((d) => `  ${d}`).join('\n')}\n`
        + 'Lower the baseline when violations are fixed.',
    ).toBeLessThanOrEqual(DNT_BASELINE)
  })

  it('ratchet: report exact DNT count for tightening', () => {
    const dropped: string[] = []
    for (const [code, catalog] of Object.entries(catalogs)) {
      if (code === DEFAULT_LANGUAGE) continue
      for (const [key, value] of Object.entries(catalog)) {
        const source = en[key]
        if (source === undefined) continue
        for (const term of glossary.dnt) {
          const re = boundary(term)
          if (re.test(source) && !re.test(value)) dropped.push(`${code}:${key}`)
        }
      }
    }
    expect(
      dropped.length,
      `only ${dropped.length} now — lower DNT_BASELINE to ${dropped.length}`,
    ).toBe(DNT_BASELINE)
  })
})
