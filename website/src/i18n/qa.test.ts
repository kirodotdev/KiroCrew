/**
 * Catalog QA guards — the checks a translation management system would run, for a
 * project that keeps its catalogs in git and has no TMS.
 *
 * `catalogParity.test.ts` proves the catalogs are structurally *aligned* with each
 * other: same keys, same placeholders, right plural categories. It says nothing
 * about whether an individual value is well formed. This file covers that half.
 *
 * ## Why these specific checks
 *
 * They are the intersection of what every major TMS ships as a stock check
 * (Lokalise, Weblate, Crowdin, POEditor all include bracket balance, edge
 * whitespace and doubled spaces) and what a JSON catalog can decide on its own.
 * They matter more here than in a TMS-backed project, not less: translations are
 * machine-generated and then edited piecemeal by contributors who cannot read the
 * other nine languages, so CI is the only reviewer these strings get.
 *
 * ## Why an allowlist rather than a threshold
 *
 * The existing violations are a codemod artifact, not a translation problem — the
 * extractor split sentences at JSX boundaries, so `'Skills ('` and `')'` became
 * separate keys. Fixing those is a large, separate piece of work. Until then this
 * suite is a **ratchet**: it fails on anything new and ignores the frozen set. The
 * allowlist is also the worklist for that later cleanup.
 *
 * The allowlist carries a **staleness guard** — an entry that no longer matches a
 * real violation fails the suite. Without it the file silently accumulates dead
 * exemptions and stops meaning anything.
 *
 * ## Regenerating
 *
 *     I18N_QA_UPDATE_ALLOWLIST=1 npx vitest run src/i18n/qa.test.ts
 *
 * The check logic lives here and nowhere else, so the generator cannot drift from
 * the assertions the way a separate script would.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES } from './languages'

/**
 * Generated catalogs are excluded. The pseudolocale is a mechanical transform of
 * `en.json`, so every defect it has is inherited — checking it would report the same
 * English violation twice and let a generator change silently move the baseline.
 */
const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

const ALLOWLIST_PATH = join(__dirname, 'qa-allowlist.json')
const UPDATING = process.env.I18N_QA_UPDATE_ALLOWLIST === '1'

/** Catalogs exactly as the runtime composes them, including English's two-file merge. */
const CATALOGS: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(RUNTIME_CATALOGS)
    .filter(([code]) => !GENERATED.has(code))
    .map(([code, bundle]) => [code, flatten((bundle as { translation: unknown }).translation)]),
)

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

/**
 * Interpolation placeholders are removed before any punctuation check. `{{count}}`
 * contains braces that would otherwise register as an unbalanced pair.
 */
const stripInterpolation = (v: string) => v.replace(/\{\{[^}]*\}\}/g, '')

const DELIMITER_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ['(', ')'],
  ['[', ']'],
  ['（', '）'],
  ['【', '】'],
  ['「', '」'],
]

/**
 * Values that are a single connector or morpheme. These cannot be translated in
 * isolation — `'s'` is an English plural suffix and `'repl'` is the stem of
 * "replies", so every language ships them verbatim and the UI renders English.
 */
const CONNECTORS = new Set([
  'and', 'or', 'of', 'to', 'in', 'on', 'for', 'with', 'by', 'at', 'from',
  'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
  's', 'es', 'y', 'ies', 'repl',
])

/** Fullwidth digits and Latin letters. Fullwidth *punctuation* is correct in CJK; these are not. */
const FULLWIDTH_ALPHANUMERIC = /[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/

/**
 * The curly double-quote pair a locale actually uses, keyed by language.
 *
 * Pairing curly quotes is locale-specific, and getting it wrong in either direction is a
 * bug. Most locales open with U+201C `“` and close with U+201D `”`. The low-high locales
 * — German here, plus Polish, Czech, Croatian, Hungarian and others if they are ever
 * shipped — open with U+201E `„` and close with U+201C `“`. So German's correct
 * `„Weiter“` has one `“` and no `”`, which an English-shaped rule reports as unbalanced.
 *
 * Guillemets (`«` `»`, used by French and Russian) are deliberately NOT checked: they
 * are a separate pair with their own spacing rules, and no shipped catalog has shown a
 * defect in them. That is a stated false-negative class, not an oversight.
 */
const QUOTE_PAIRS: Record<string, readonly [string, string]> = {
  de: ['\u201E', '\u201C'],
}
const DEFAULT_QUOTE_PAIR: readonly [string, string] = ['\u201C', '\u201D']

type Check = {
  id: string
  describe: string
  violates: (value: string, lang: string) => boolean
}

const CHECKS: Check[] = [
  {
    id: 'unbalanced-delimiter',
    describe: 'brackets and parentheses must be balanced within a single value',
    violates: (v) => {
      const t = stripInterpolation(v)
      return DELIMITER_PAIRS.some(([open, close]) => count(t, open) !== count(t, close))
    },
  },
  {
    id: 'odd-quote-count',
    describe: 'quotation marks must pair within a single value',
    violates: (v, lang) => {
      const t = stripInterpolation(v)
      // Curly quotes are DIRECTIONAL, so parity over their sum is the wrong test: the
      // previous `(count(“) + count(”)) % 2` passed on any even total, so `“click “here`
      // — two openers, no closer — was reported as balanced. Compare the locale's opener
      // against its closer instead, which catches an odd total AND an even-but-mismatched
      // one. Straight `"` is non-directional, so parity is all that can be checked there.
      const [open, close] = QUOTE_PAIRS[lang] ?? DEFAULT_QUOTE_PAIR
      return count(t, open) !== count(t, close) || count(t, '"') % 2 === 1
    },
  },
  {
    id: 'edge-whitespace',
    describe: 'no leading or trailing space or tab',
    // U+00A0 is excluded deliberately: a non-breaking space is a glyph the copy
    // asked for, not accidental padding.
    violates: (v) => v !== v.replace(/^[ \t\n\r]+/, '').replace(/[ \t\n\r]+$/, ''),
  },
  {
    id: 'doubled-space',
    describe: 'no run of two or more spaces',
    // A whitespace run containing a newline is indentation carried over from a
    // multi-line JSX literal; it collapses to one space when rendered and is not
    // a defect. Only newline-free runs are accidental.
    violates: (v) =>
      [...v.matchAll(/[ \t\n\r]{2,}/g)].some((m) => !m[0].includes('\n') && !m[0].includes('\r')),
  },
  {
    id: 'bare-connector',
    describe: 'a value must not be a lone connector word or morpheme',
    violates: (v) => CONNECTORS.has(v.trim().toLowerCase().replace(/[.,;:!?]+$/, '')),
  },
  {
    id: 'fullwidth-alphanumeric',
    describe: 'CJK catalogs must not store fullwidth Latin letters or digits',
    // W3C CLReq: "when storing text, avoid the fullwidth alphabetic and numeric
    // characters of that block; leave it to the layout engine."
    violates: (v) => FULLWIDTH_ALPHANUMERIC.test(v),
  },
]

const count = (haystack: string, needle: string) => haystack.split(needle).length - 1

/** `lang:key` — the unit an allowlist entry addresses. */
const site = (lang: string, key: string) => `${lang}:${key}`

function findViolations(check: Check): string[] {
  const out: string[] = []
  for (const [lang, catalog] of Object.entries(CATALOGS)) {
    for (const [key, value] of Object.entries(catalog)) {
      if (check.violates(value, lang)) out.push(site(lang, key))
    }
  }
  return out.sort()
}

const live: Record<string, string[]> = Object.fromEntries(
  CHECKS.map((c) => [c.id, findViolations(c)]),
)

if (UPDATING) {
  writeFileSync(
    ALLOWLIST_PATH,
    `${JSON.stringify({ _generated: 'I18N_QA_UPDATE_ALLOWLIST=1 npx vitest run src/i18n/qa.test.ts', ...live }, null, 2)}\n`,
  )
}

const allowlist: Record<string, string[]> = JSON.parse(readFileSync(ALLOWLIST_PATH, 'utf-8'))

describe('catalog QA', () => {
  it.each(CHECKS.map((c) => [c.id, c] as const))('%s — no NEW violations', (id, check) => {
    const allowed = new Set(allowlist[id] ?? [])
    const added = live[id].filter((s) => !allowed.has(s))
    expect(
      added,
      `${check.describe}\n\n${added.length} new violation(s). Fix them, or if they are ` +
        `deliberate, regenerate the allowlist with I18N_QA_UPDATE_ALLOWLIST=1.`,
    ).toEqual([])
  })

  it.each(CHECKS.map((c) => [c.id, c] as const))('%s — allowlist has no stale entries', (id) => {
    const found = new Set(live[id])
    const stale = (allowlist[id] ?? []).filter((s) => !found.has(s))
    expect(
      stale,
      `These allowlist entries no longer match a real violation — the strings were ` +
        `fixed or the keys renamed. Regenerate the allowlist so it keeps meaning something.`,
    ).toEqual([])
  })

  it('every check is represented in the allowlist file', () => {
    // Guards against a check being added here and silently never gated because
    // the allowlist file has no key for it.
    const missing = CHECKS.map((c) => c.id).filter((id) => !(id in allowlist))
    expect(missing, 'regenerate the allowlist after adding a check').toEqual([])
  })

  it('English is the reference catalog and is present', () => {
    expect(Object.keys(CATALOGS)).toContain(DEFAULT_LANGUAGE)
    expect(Object.keys(CATALOGS[DEFAULT_LANGUAGE]).length).toBeGreaterThan(3000)
  })
})

/**
 * The detectors themselves, tested on synthetic values.
 *
 * A per-file ratchet over real catalogs cannot tell "this check is correct" from "this
 * check never fires": both look green. `odd-quote-count` shipped as a parity test over
 * the SUM of both curly directions, which passes on any even total — so `“click “here`,
 * two opening quotes and no closing one, was reported as balanced. That is a false
 * negative a catalog-driven test can never surface, because the catalog does not happen
 * to contain the case.
 */
describe('catalog QA detectors', () => {
  const check = (id: string) => {
    const found = CHECKS.find((c) => c.id === id)
    expect(found, `no check with id ${id}`).toBeDefined()
    return found!.violates
  }

  describe('odd-quote-count', () => {
    const raw = check('odd-quote-count')
    const violates = (v: string, lang = DEFAULT_LANGUAGE) => raw(v, lang)

    it('accepts correctly paired curly quotes', () => {
      expect(violates('press “Save” to continue')).toBe(false)
      expect(violates('no quotes at all')).toBe(false)
      expect(violates('“one” and “two”')).toBe(false)
    })

    it('rejects an odd curly-quote count', () => {
      expect(violates('press “Save to continue')).toBe(true)
      expect(violates('press Save” to continue')).toBe(true)
    })

    it('rejects an EVEN count whose directions do not match', () => {
      // The regression: even total, still broken. Parity over the sum passes all of these.
      expect(violates('“click “here')).toBe(true)
      expect(violates('click” here”')).toBe(true)
      expect(violates('“a” “b')).toBe(true)
    })

    it('applies the low-high pair for German rather than the English one', () => {
      // Correct German typography. An English-shaped rule counts one `“` and no `”` and
      // reports six real de-catalog strings as unbalanced.
      expect(violates('drücken Sie „Weiter“, um fortzufahren', 'de')).toBe(false)
      expect(violates('„Mehrzeilig“ und „Einzeilig“', 'de')).toBe(false)
      // Still catches genuinely unbalanced German.
      expect(violates('drücken Sie „Weiter, um fortzufahren', 'de')).toBe(true)
      // And the English pair is not silently accepted for German.
      expect(violates('drücken Sie “Weiter” hier', 'de')).toBe(true)
    })

    it('still parity-checks non-directional straight quotes', () => {
      expect(violates('press "Save" to continue')).toBe(false)
      expect(violates('press "Save to continue')).toBe(true)
    })
  })
})
