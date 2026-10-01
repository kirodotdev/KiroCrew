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

import { CATALOGS as RUNTIME_CATALOGS } from './catalogs'
import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES } from './languages'
import glossary from './glossary.json'

// 1 genuine drop remains (zh-CN onboarding).
const DNT_BASELINE = 0

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

/**
 * Word-boundary match for a do-not-translate term.
 *
 * A dot continues an identifier only when a word character follows it, so a term
 * appearing only inside an identifier — `Kiro.dev`, `kiro.json` — is not *demanded*
 * in the translation, while a term at the END of a sentence still matches. That end
 * position matters: Romance and Slavic word order moves the noun modifier last, so
 * `its own MCP backends.` becomes `sus propios backends MCP.` — the term is present
 * and the trailing full stop must not read as a drop.
 */
const boundary = (term: string) => {
  const t = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(?<!\\w)(?<!\\w\\.)${t}(?!\\w)(?!\\.\\w)`)
}

/**
 * The DNT patterns, compiled ONCE, paired with the term for the report.
 *
 * `boundary()` used to be called in the innermost loop, so the same 41 patterns
 * were recompiled for every key in every catalog: 41 terms x 6884 keys x 11
 * catalogs = **3.1 MILLION** `new RegExp` constructions per test, twice over
 * (both tests below run the identical scan). That is what put this file at
 * 29-41s of test time against the 15s per-test budget — it failed on load, from
 * a cost that is entirely avoidable rather than inherent to what it checks.
 */
const DNT_PATTERNS: ReadonlyArray<{ term: string; re: RegExp }> =
  glossary.dnt.map((term) => ({ term, re: boundary(term) }))

/**
 * Every DNT violation in the catalogs, computed ONCE and shared by both tests.
 *
 * They ask two different questions of the same scan — "at most the baseline"
 * and "exactly the baseline, so tighten it" — so running it twice doubled the
 * cost for no additional coverage.
 */
const DROPPED: ReadonlyArray<string> = (() => {
  const out: string[] = []
  for (const [code, catalog] of Object.entries(catalogs)) {
    if (code === DEFAULT_LANGUAGE) continue
    for (const [key, value] of Object.entries(catalog)) {
      const source = en[key]
      if (source === undefined) continue
      for (const { term, re } of DNT_PATTERNS) {
        if (re.test(source) && !re.test(value)) out.push(`${code}:${key} [${term}]`)
      }
    }
  }
  return out
})()

describe('glossary', () => {
  it('glossary.json is well formed', () => {
    expect(Array.isArray(glossary.dnt)).toBe(true)
    expect(glossary.dnt.length).toBeGreaterThan(10)
  })

  it('do-not-translate terms are not dropped in translation', () => {
    expect(
      DROPPED.length,
      `${DROPPED.length} translations dropped a product name (baseline ${DNT_BASELINE}).\n`
        + `${DROPPED.slice(0, 8).map((d) => `  ${d}`).join('\n')}\n`
        + 'Lower the baseline when violations are fixed.',
    ).toBeLessThanOrEqual(DNT_BASELINE)
  })

  it('ratchet: report exact DNT count for tightening', () => {
    expect(
      DROPPED.length,
      `only ${DROPPED.length} now — lower DNT_BASELINE to ${DROPPED.length}`,
    ).toBe(DNT_BASELINE)
  })
})
