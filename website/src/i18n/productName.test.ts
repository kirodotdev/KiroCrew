/**
 * The `{{productName}}` contract.
 *
 * Catalog values interpolate the product name instead of hardcoding it, so a
 * downstream edition can rebrand by overriding one variable from its
 * composition root instead of forking 13 locale files. These tests pin the
 * three properties the whole arrangement rests on: the stock default renders
 * the exact same English a hardcoded literal would, the variable is wired as
 * an i18next `defaultVariables` (so it survives the planned lazy-catalog
 * migration untouched), and a call-time variable still wins.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS, initI18n, i18next, setProductName } from './index'

// No-op — the vitest setup file already initialized i18n. Explicit so this
// file also works standalone, and so the late-override test below is
// self-evidently running against an initialized instance.
initI18n()

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

describe('productName interpolation variable', () => {
  it('defaults to the stock product name', () => {
    expect(i18next.options.interpolation?.defaultVariables).toMatchObject({
      productName: 'Kiro Crew',
    })
  })

  it('renders a rewritten catalog value identically to the old literal', () => {
    // Any key whose value carries {{productName}} works; this one is stable.
    expect(i18next.t('app.updating_kirocrew')).toBe('Updating Kiro Crew…')
  })

  it('lets a call-time variable win over the default', () => {
    expect(i18next.t('app.updating_kirocrew', { productName: 'Acme' })).toBe('Updating Acme…')
  })

  it('refuses a late override rather than half-applying it', () => {
    // After init the variable has been handed to i18next; silently accepting
    // the call would leave the UI unchanged while the caller believes it
    // rebranded. Vitest runs with import.meta.env.DEV true, so this throws.
    expect(() => setProductName('Acme')).toThrow(/before initI18n/)
  })

  it('no non-manifest catalog value hardcodes the product name literal', () => {
    // The regression this catches: an upstream-authored key lands with the
    // literal instead of the placeholder, and an edition's rebranding is
    // silently incomplete — nothing else fails, because parity only compares
    // placeholders that exist in en. The apps.*.manifest.* keys are the one
    // documented exception (manifest-sync pins them to the app.json prose).
    const isManifestKey = (k: string) => /^apps\.[^.]+\.manifest\./.test(k)
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      const offenders = Object.entries(flatten(catalog.translation))
        .filter(([k, v]) => !isManifestKey(k) && v.includes('Kiro Crew'))
        .map(([k]) => `${lang}:${k}`)
      expect(offenders, offenders.slice(0, 5).join(', ')).toEqual([])
    }
  })
})
