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
        const missing = EN_KEYS.filter(k => !keys.includes(k))
        expect(missing, `missing ${missing.length} key(s), e.g. ${missing.slice(0, 5).join(', ')}`)
          .toEqual([])
      })

      it('has no key absent from en', () => {
        // An extra key is dead weight — usually a typo'd path or a leftover
        // from a renamed key, which would render as its raw key in that
        // language while looking fine in English.
        const extra = keys.filter(k => !EN_KEYS.includes(k))
        expect(extra, `${extra.length} stray key(s), e.g. ${extra.slice(0, 5).join(', ')}`)
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
        for (const key of EN_KEYS) {
          if (flat[key] === undefined) continue
          const want = placeholders(enFlat[key])
          const got = placeholders(flat[key])
          if (want.join(',') !== got.join(',')) {
            mismatched.push(`${key}: expected [${want}] got [${got}]`)
          }
        }
        expect(mismatched, mismatched.slice(0, 5).join(' | ')).toEqual([])
      })
    })
  }
})
