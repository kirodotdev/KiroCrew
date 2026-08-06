import { describe, it, expect, beforeAll } from 'vitest'
import { readdirSync, readFileSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import i18next from 'i18next'
import {
  APP_MANIFEST_KEY, appDisplayName, appDescription, appPageLabel, appHighlights,
} from '../components/appstore/appManifest'
import en from '../i18n/locales/en.json'
import zh from '../i18n/locales/zh-CN.json'
import '../i18n'

/**
 * `APP_MANIFEST_KEY` localises built-in app metadata that the PYTHON side owns, without
 * replacing the English in `app.json`. These tests pin the three properties that design
 * depends on and that no other gate covers:
 *
 *   - an unknown app (every third-party install) falls through to the manifest's own
 *     value rather than rendering a raw key or an empty string
 *   - a prototype-shaped app id cannot reach `Object.prototype`
 *   - a highlights length mismatch degrades to the FULL English list, never a
 *     truncated translated one
 *
 * Key existence and manifest agreement are enforced by
 * `scripts/check-app-manifest-sync.mjs` and `catalogParity.test.ts`; they are not
 * re-litigated here.
 */
const lookup = (cat: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((o, p) => (o == null ? undefined : (o as Record<string, unknown>)[p]), cat)

describe('APP_MANIFEST_KEY', () => {
  /**
   * Coverage lives HERE and not in `scripts/check-app-manifest-sync.mjs` on purpose.
   * That script cannot read a TS module without a regex, and a regex over source text
   * also matches source inside a comment — so an entry wrapped in a block comment left
   * the gate reading a table the runtime no longer had, exiting 0 while that app
   * rendered untranslated. This test imports the real module, so a commented-out entry
   * simply is not in the object and fails here.
   */
  it('has an entry for every built-in that ships an app.json', () => {
    const dir = resolve(__dirname, '../../../src/kiro_crew/apps/builtins')
    const shipped = readdirSync(dir, { withFileTypes: true })
      .filter(e => e.isDirectory() && !e.name.startsWith('.') && !e.name.startsWith('_'))
      .map(e => join(dir, e.name, 'app.json'))
      .filter(f => existsSync(f))
      .map(f => JSON.parse(readFileSync(f, 'utf8')))
    expect(shipped.length).toBeGreaterThan(10)
    for (const m of shipped) {
      expect(APP_MANIFEST_KEY, `no APP_MANIFEST_KEY entry for '${m.name}'`)
        .toHaveProperty(m.name)
      // One key per bullet. A short table makes appHighlights() fall back to English
      // for the WHOLE list, which is a silent loss rather than a visible break.
      expect(APP_MANIFEST_KEY[m.name].highlights.length, `highlight count for '${m.name}'`)
        .toBe((m.highlights || []).length)
    }
    // And nothing survives for an app that no longer exists.
    const names = new Set(shipped.map(m => m.name))
    for (const name of Object.keys(APP_MANIFEST_KEY)) {
      expect(names.has(name), `APP_MANIFEST_KEY has a stale entry for '${name}'`).toBe(true)
    }
  })

  it('carries full literal keys, which is the only form check-i18n-keys resolves', () => {
    for (const [name, keys] of Object.entries(APP_MANIFEST_KEY)) {
      for (const key of [keys.displayName, keys.description, keys.pageLabel, ...keys.highlights]) {
        expect(key, `${name} key is not a dotted literal`).toMatch(/^apps\.[A-Za-z]+\.manifest\.[a-z0-9_]+$/)
        expect(lookup(en, key), `${key} missing from en.json`).toBeTypeOf('string')
      }
    }
  })

  it('is actually translated in zh-CN, not copied', () => {
    // Proper nouns legitimately match (Papyrus stays Papyrus), so this asserts the
    // POPULATION moved rather than every string: a table wired to untranslated keys
    // would show ~0 differences.
    const all: string[] = []
    for (const keys of Object.values(APP_MANIFEST_KEY)) {
      all.push(keys.description, ...keys.highlights)
    }
    const differing = all.filter(k => lookup(zh, k) !== lookup(en, k))
    expect(differing.length).toBeGreaterThan(all.length * 0.9)
  })
})

describe('resolvers', () => {
  beforeAll(async () => { await i18next.changeLanguage('en') })

  it('translates a known built-in', () => {
    const name = Object.keys(APP_MANIFEST_KEY)[0]
    expect(appDisplayName({ name, displayName: 'IGNORED' }))
      .toBe(lookup(en, APP_MANIFEST_KEY[name].displayName))
  })

  it('returns a third-party app verbatim rather than dressing up a key', () => {
    const app = { name: 'some-vendor-app', displayName: 'Vendor App', description: 'Their copy.' }
    expect(appDisplayName(app)).toBe('Vendor App')
    expect(appDescription(app)).toBe('Their copy.')
    expect(appHighlights({ name: app.name, highlights: ['a', 'b'] })).toEqual(['a', 'b'])
  })

  it('falls back to the id when a third-party app has no displayName', () => {
    expect(appDisplayName({ name: 'no-name-app' })).toBe('no-name-app')
    expect(appDisplayName({})).toBe('')
  })

  it('does not resolve prototype members for a prototype-shaped id', () => {
    // `in` would find these on Object.prototype and hand a function to i18next.
    for (const id of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(appDisplayName({ name: id, displayName: 'Literal' })).toBe('Literal')
      expect(appHighlights({ name: id, highlights: ['x'] })).toEqual(['x'])
    }
  })

  it('refuses to lend first-party copy to a registry row that reuses a built-in id', () => {
    // An id is not provenance. `_registry` is attached server-side by
    // `_load_external_registries` and cannot be forged by index content, whereas
    // `origin` is copied verbatim from that content — so this must key off `_registry`,
    // exactly as `isVerified()` in ../components/appstore/types.ts does. Without the
    // guard, an external registry publishing an entry named `projects` would be
    // rendered with the built-in's localised name and description, next to an Install
    // button that runs setup code with gateway privileges.
    const name = Object.keys(APP_MANIFEST_KEY)[0]
    const impostor = {
      name,
      _registry: 'some-external-registry',
      origin: 'builtin',            // self-declared, and deliberately not trusted
      displayName: 'Impostor',
      description: 'Impostor copy.',
      highlights: ['impostor bullet'],
    }
    expect(appDisplayName(impostor)).toBe('Impostor')
    expect(appDescription(impostor)).toBe('Impostor copy.')
    expect(appHighlights(impostor)).toEqual(['impostor bullet'])
    // A genuine built-in is merged client-side from the installed list and carries no
    // `_registry`, so it still resolves.
    expect(appDisplayName({ name, displayName: 'IGNORED' }))
      .toBe(lookup(en, APP_MANIFEST_KEY[name].displayName))
  })

  it('keeps every bullet in English when the table and manifest disagree in length', () => {
    const name = Object.keys(APP_MANIFEST_KEY)[0]
    const real = APP_MANIFEST_KEY[name].highlights.length
    const tooMany = Array.from({ length: real + 1 }, (_, i) => `extra ${i}`)
    // Complete but untranslated beats translated but truncated: a dropped bullet is
    // invisible, whereas English on app-detail is reported by the en-XA render gate.
    expect(appHighlights({ name, highlights: tooMany })).toEqual(tooMany)
    expect(appHighlights({ name, highlights: [] })).toEqual([])
  })

  it('resolves translated bullets when the lengths match', () => {
    const name = Object.keys(APP_MANIFEST_KEY)[0]
    const keys = APP_MANIFEST_KEY[name].highlights
    const same = keys.map((_, i) => `manifest ${i}`)
    expect(appHighlights({ name, highlights: same }))
      .toEqual(keys.map(k => lookup(en, k)))
  })

  it('prefers the page label key, then the passed label, then the id', () => {
    const name = Object.keys(APP_MANIFEST_KEY)[0]
    expect(appPageLabel(name, 'Raw Label', 'Raw Display'))
      .toBe(lookup(en, APP_MANIFEST_KEY[name].pageLabel))
    expect(appPageLabel('vendor-app', 'Raw Label', 'Raw Display')).toBe('Raw Label')
    expect(appPageLabel('vendor-app', '', 'Raw Display')).toBe('Raw Display')
    expect(appPageLabel('vendor-app')).toBe('vendor-app')
  })
})
