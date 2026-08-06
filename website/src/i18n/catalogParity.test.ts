/**
 * Catalog integrity guards.
 *
 * These are the tests that make a large i18n conversion safe to review:
 * instead of hand-auditing hundreds of converted call sites, CI proves the
 * catalogs are structurally sound and that English rendering is unchanged.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, isSupportedLanguage } from './languages'
import pluralKeys from './pluralKeys.json'

/**
 * Catalogs exactly as the runtime composes them — including English's
 * generated + manual merge. Reading `CATALOGS` from `./index` rather than
 * re-importing the JSON means this suite can never disagree with what actually
 * ships, and adding a language needs no edit here.
 */
const CATALOGS: Record<string, unknown> = Object.fromEntries(
  Object.entries(RUNTIME_CATALOGS).map(([code, bundle]) => [
    code,
    (bundle as { translation: unknown }).translation,
  ]),
)

const en = CATALOGS[DEFAULT_LANGUAGE]

/** Flatten a nested catalog to dotted leaf paths → string values. */
function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      Object.assign(out, flatten(value, path))
    } else {
      out[path] = String(value)
    }
  }
  return out
}

const EN_KEYS = Object.keys(flatten(en)).sort()

/**
 * i18next plural suffixes. A pluralized key does NOT have the same key set in
 * every language: English needs `_one`/`_other`, Russian `_one`/`_few`/`_many`/
 * `_other`, Chinese only `_other`. So exact key-set equality — correct for every
 * other key — would report Russian's extra forms as strays and Chinese's absent
 * ones as missing.
 *
 * Plural keys are therefore compared per-language against that language's OWN
 * `Intl.PluralRules` categories, which is exactly what i18next uses at runtime
 * to choose a form.
 */
const PLURAL_SUFFIXES = ['zero', 'one', 'two', 'few', 'many', 'other'] as const

/**
 * Which keys are pluralized comes from an EXPLICIT registry, not from sniffing
 * a `_one`/`_other` suffix. Suffix detection is wrong here because real English
 * copy ends in those words: `panel_to_add_one` = "panel to add one.",
 * `click_new_to_create_one` = "Click + New to create one.",
 * `add_column_after_this_one` = "Add column after this one". Treating those as
 * plural forms invented demands for `_few`/`_many` siblings that must not exist.
 *
 * The registry is written by `scripts/i18n-plural-codemod.mjs`, which is the
 * only thing that pluralizes a key — so it cannot drift from the call sites.
 */
const PLURAL_BASES: ReadonlySet<string> = new Set(pluralKeys as readonly string[])

/**
 * Split `a.b.c_one` into `['a.b.c', 'one']` — but ONLY when the base is a
 * registered plural key. Returns null otherwise, so naturally-suffixed copy is
 * left alone.
 */
function splitPlural(key: string): [string, string] | null {
  for (const suffix of PLURAL_SUFFIXES) {
    if (!key.endsWith(`_${suffix}`)) continue
    const base = key.slice(0, -(suffix.length + 1))
    if (PLURAL_BASES.has(base)) return [base, suffix]
  }
  return null
}

/** Non-plural English keys — these must match exactly in every language. */
const EN_SINGULAR_KEYS = EN_KEYS.filter(k => {
  const parts = splitPlural(k)
  return !(parts && PLURAL_BASES.has(parts[0]))
})

function pluralCategories(code: string): readonly string[] {
  return new Intl.PluralRules(code).resolvedOptions().pluralCategories
}

describe('language registry', () => {
  it('ships a catalog for every supported language', () => {
    for (const { code } of SUPPORTED_LANGUAGES) {
      expect(CATALOGS[code], `no catalog registered for '${code}'`).toBeDefined()
    }
  })

  it('does not register a catalog for an unlisted language', () => {
    // Guards the reverse drift: a catalog added to `resources` but never listed
    // in SUPPORTED_LANGUAGES would ship in the bundle yet be unreachable from
    // the picker.
    for (const code of Object.keys(CATALOGS)) {
      expect(isSupportedLanguage(code), `'${code}' has a catalog but is not listed`).toBe(true)
    }
  })

  it('includes the fallback language', () => {
    expect(isSupportedLanguage(DEFAULT_LANGUAGE)).toBe(true)
  })

  it('does not add a thirteenth statically bundled catalog', () => {
    // The lazy-loading ratchet, as a GATE rather than a sentence.
    //
    // `index.ts` and `docs/system-specs/modules/config.md` both say catalog #13
    // belongs behind the `i18next-http-backend` seam, because every catalog ships
    // to every user whatever language they read (~173 KB gzip each, ~2.0 MB gzip
    // for the twelve). A doc cannot hold that line: the PR that crosses it is
    // also the PR that can rewrite the doc, which is exactly how #12 landed in
    // front of a seam #12 was supposed to trigger.
    //
    // Raising this number is legitimate ONLY together with the seam, or with a
    // re-measured figure in `config.md` and a reviewer who accepted the deferral.
    // The pseudolocale is excluded: it is DEV-only and Rollup drops it from a
    // production build.
    const authored = SUPPORTED_LANGUAGES.filter(l => !l.devOnly)
    expect(
      authored.length,
      'Adding a catalog puts its full weight in every user\'s first load. Land the '
      + 'lazy-loading seam in i18n/index.ts instead, or re-measure and say why here.',
    ).toBeLessThanOrEqual(12)
  })
})

describe('catalog parity', () => {
  it('en catalog is non-empty', () => {
    expect(EN_KEYS.length).toBeGreaterThan(0)
  })

  for (const { code } of SUPPORTED_LANGUAGES.filter(l => l.code !== DEFAULT_LANGUAGE)) {
    describe(`${code}`, () => {
      const keys = Object.keys(flatten(CATALOGS[code])).sort()

      it('has no key missing relative to en', () => {
        // A missing key silently renders English. That is a fine RUNTIME
        // degradation but a bad shipping state, so it fails here instead.
        const missing = EN_SINGULAR_KEYS.filter(k => !keys.includes(k))
        expect(missing, `missing ${missing.length} key(s), e.g. ${missing.slice(0, 5).join(', ')}`)
          .toEqual([])
      })

      it('has no key absent from en', () => {
        // An extra key is dead weight — usually a typo'd path or a leftover
        // from a renamed key, which would render as its raw key in that
        // language while looking fine in English.
        const enKeySet = new Set(EN_SINGULAR_KEYS)
        const extra = keys.filter(k => {
          if (enKeySet.has(k)) return false
          // A plural form of an English-pluralized base is legitimate as long
          // as this language actually has that category.
          const parts = splitPlural(k)
          if (parts && PLURAL_BASES.has(parts[0])) return false
          return true
        })
        expect(extra, `${extra.length} stray key(s), e.g. ${extra.slice(0, 5).join(', ')}`)
          .toEqual([])
      })

      it('provides every plural form its own language requires', () => {
        // i18next picks the form via Intl.PluralRules for THIS language. A
        // missing category falls back and renders the wrong grammatical number
        // — e.g. Russian showing the `_one` form for 5 items.
        const present = new Set(keys)
        const gaps: string[] = []
        for (const base of PLURAL_BASES) {
          for (const category of pluralCategories(code)) {
            if (!present.has(`${base}_${category}`)) gaps.push(`${base}_${category}`)
          }
        }
        expect(gaps, `missing ${gaps.length} plural form(s) for categories `
          + `[${pluralCategories(code).join(', ')}], e.g. ${gaps.slice(0, 5).join(', ')}`)
          .toEqual([])
      })

      it('does not define a plural form its language never selects', () => {
        // A category outside this language's rule set is unreachable dead
        // weight, and usually means the forms were copied from another locale.
        const allowed = new Set(pluralCategories(code))
        const dead = keys.filter(k => {
          const parts = splitPlural(k)
          return parts !== null && PLURAL_BASES.has(parts[0]) && !allowed.has(parts[1])
        })
        expect(dead, `${dead.length} unreachable plural form(s), e.g. ${dead.slice(0, 5).join(', ')}`)
          .toEqual([])
      })

      it('has no empty translation', () => {
        const flat = flatten(CATALOGS[code])
        const empty = Object.keys(flat).filter(k => flat[k].trim() === '')
        expect(empty, `empty value(s): ${empty.slice(0, 5).join(', ')}`).toEqual([])
      })

      it('preserves every interpolation placeholder from en', () => {
        // `{{count}}` dropped in translation renders a sentence missing its
        // number; a placeholder renamed in translation renders a literal
        // "{{cnt}}". Both are invisible without this check.
        const enFlat = flatten(en)
        const flat = flatten(CATALOGS[code])
        const placeholders = (s: string) => (s.match(/\{\{[^}]+\}\}/g) ?? []).sort()
        const mismatched: string[] = []
        for (const key of EN_SINGULAR_KEYS) {
          if (flat[key] === undefined) continue
          const want = placeholders(enFlat[key])
          const got = placeholders(flat[key])
          if (want.join(',') !== got.join(',')) {
            mismatched.push(`${key}: expected [${want}] got [${got}]`)
          }
        }
        // Plural forms are checked against the English `_other` form rather than
        // a same-suffix key, which may not exist in this language. Comparing by
        // suffix would silently SKIP most forms (Russian's `_few`/`_many` have no
        // English counterpart) — exactly the keys where a dropped `{{count}}`
        // renders a countless sentence.
        for (const base of PLURAL_BASES) {
          const want = placeholders(enFlat[`${base}_other`] ?? '')
          for (const category of pluralCategories(code)) {
            const value = flat[`${base}_${category}`]
            if (value === undefined) continue
            const got = placeholders(value)
            if (want.join(',') !== got.join(',')) {
              mismatched.push(`${base}_${category}: expected [${want}] got [${got}]`)
            }
          }
        }
        expect(mismatched, mismatched.slice(0, 5).join(' | ')).toEqual([])
      })
    })
  }
})
