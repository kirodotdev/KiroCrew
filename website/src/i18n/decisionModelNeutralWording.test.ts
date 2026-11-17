/**
 * The decision feature is named by its role ("the decision model"), not by a
 * product. "Jev" is one provider among several, so the product name may appear
 * only in the strings that describe that provider's own entry: the provider
 * option, its API key, its accuracy and benchmark notes, and the judge option
 * that routes to it. Any other catalog string that names it is the feature
 * leaking a provider into neutral wording.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const TAB = 'pages.developer.featurePreviewsTab.'

/** The strings about the Jev provider itself. Adding a key here is a decision, not a fix. */
const PROVIDER_ENTRY_KEYS = new Set([
  `${TAB}decisions_api_key_label`,
  `${TAB}decisions_custom_loopback_no_key`,
  `${TAB}decisions_judge_provider_desc`,
  `${TAB}decisions_judge_provider_jev`,
  `${TAB}decisions_provider_jev`,
  `${TAB}decisions_provider_measured`,
  `${TAB}decisions_provider_quality`,
])

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

function namingJev(code: string): string[] {
  const bundle = (CATALOGS as Record<string, { translation: unknown }>)[code]
  return Object.entries(flatten(bundle.translation))
    .filter(([, text]) => text.toLowerCase().includes('jev'))
    .map(([key]) => key)
    .sort()
}

describe('decision model wording is neutral outside the provider entry', () => {
  for (const { code } of SUPPORTED_LANGUAGES) {
    if (code === 'en-XA') continue
    it(`${code}: only the provider-entry strings name Jev`, () => {
      const leaks = namingJev(code).filter(key => !PROVIDER_ENTRY_KEYS.has(key))
      expect(leaks, `strings that name the provider outside its own entry:\n${leaks.join('\n')}`).toEqual([])
    })
  }

  it('the provider-entry allowlist names no key that has gone', () => {
    const present = new Set(namingJev('en'))
    const stale = [...PROVIDER_ENTRY_KEYS].filter(key => !present.has(key))
    expect(stale, 'allowlisted keys that no longer name Jev: remove them').toEqual([])
  })
})
