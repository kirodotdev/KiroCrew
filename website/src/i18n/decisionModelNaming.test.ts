/**
 * The feature is "decision model"; Jev is one decision-model provider among the
 * others, so a catalog value may name it only where it is that provider's own text.
 *
 * Every shipped catalog is walked, every value checked: a value outside the hosted
 * provider's own keys that says "Jev" is the feature named after one provider again,
 * which reads wrong the moment a local model or a SageMaker endpoint answers. The
 * provider's own keys share one prefix, so a new string for that entry belongs under
 * it and any other new key cannot carry the name.
 *
 * The KEY names are not checked: `auto_jev`, `rate_jev` and the like are stored
 * identifiers (a saved slot model is `auto:jev`), renamed by nobody, and never shown.
 */

import { describe, expect, it } from 'vitest'

import { CATALOGS } from './catalogs'

/** The hosted provider's own catalog keys: its name, and the detail line under it. */
const HOSTED_PROVIDER_KEY_PREFIX = 'pages.developer.featurePreviewsTab.decisions_provider_jev'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj && typeof obj === 'object') {
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      const key = prefix ? `${prefix}.${k}` : k
      if (typeof v === 'string') out[key] = v
      else Object.assign(out, flatten(v, key))
    }
  }
  return out
}

/** Every catalog key whose value names Jev outside the provider's own keys. */
function featureLevelProviderNames(catalog: Record<string, unknown>): string[] {
  return Object.entries(flatten(catalog))
    .filter(([key, value]) => !key.startsWith(HOSTED_PROVIDER_KEY_PREFIX) && value.includes('Jev'))
    .map(([key]) => key)
}

describe('decision-model naming in every catalog', () => {
  const codes = Object.keys(CATALOGS)

  it('covers every shipped catalog', () => {
    expect(codes).toContain('en')
    expect(codes.length).toBeGreaterThan(10)
  })

  for (const code of codes) {
    it(`${code}: names Jev only inside the hosted provider's own keys`, () => {
      expect(featureLevelProviderNames(CATALOGS[code].translation)).toEqual([])
    })
  }

  it('still names the provider in its own entry', () => {
    const en = flatten(CATALOGS.en.translation)
    expect(en[HOSTED_PROVIDER_KEY_PREFIX]).toContain('Jev')
  })

  it('catches a feature-level value that names the provider', () => {
    // The guard against a check that can never fail: a value of the shape this
    // change removed is reported, under its own key.
    const sample = {
      pages: {
        developer: {
          featurePreviewsTab: {
            decisions_provider_jev: 'Jev, hosted by TypeSafe',
            decisions_bucket_label: 'Share of sessions Jev answers for',
          },
        },
      },
    }
    expect(featureLevelProviderNames(sample)).toEqual([
      'pages.developer.featurePreviewsTab.decisions_bucket_label',
    ])
  })
})
