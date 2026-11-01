/**
 * Shared Crew copy that has to match what the gateway does.
 *
 * 1. **Tailscale decides who gets in.** Shared-access copy names Tailscale
 *    access rules and promises no in-Crew approval step.
 * 2. **A hard-limit refusal names its cause and a next step.** The gateway
 *    refuses `containment_unavailable` until the host's resource-limit setup is
 *    fixed or hard limits are turned off, so "try again later" never helps.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

const LOCALES = Object.entries(CATALOGS).map(
  ([lang, bundle]) => [lang, (bundle as { translation: Record<string, unknown> }).translation] as const,
)

function value(catalog: Record<string, unknown>, key: string): string {
  const v = resolve(catalog, key)
  if (typeof v !== 'string' || v.trim() === '') throw new Error(`missing ${key}`)
  return v
}

describe('Shared Crew copy matches gateway behavior', () => {
  it('scans every shipped catalog', () => {
    expect(LOCALES.length).toBeGreaterThanOrEqual(13)
  })

  it('lets Tailscale access rules decide, with no in-Crew approval', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const keys = ['direct_link_note', 'invite_revoke_body']
    for (const key of keys) {
      const copy = value(en, `sharedCrew.${key}`)
      expect(copy).toMatch(/Tailscale (access )?rules/)
      expect(copy.toLowerCase()).not.toMatch(/local authorization|approve/)
    }
  })

  it('gives the hard-limit refusal a cause and a next step, not a retry', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const copy = value(en, 'sharedCrew.refused_containment').toLowerCase()
    expect(copy).not.toContain('try again')
    expect(copy).toContain('hard limits')
    expect(copy).toContain('whoever runs this crew')
  })
})
