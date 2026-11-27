/**
 * Shared Crew copy that has to match what the gateway does.
 *
 * 1. **Tailscale decides who gets in.** Shared-access copy names Tailscale
 *    access rules and promises no in-Crew approval step.
 * 2. **A hard-limit refusal names its cause and a next step.** The gateway
 *    refuses `containment_unavailable` until the host's resource-limit setup is
 *    fixed or hard limits are turned off, so "try again later" never helps.
 * 3. **One name for a Crew several people use.** English says Shared Crew, never
 *    Team Crew, and a new Crew's note names the switcher heading each catalog shows.
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
    const keys = ['direct_link_note']
    for (const key of keys) {
      const copy = value(en, `sharedCrew.${key}`)
      expect(copy).toMatch(/Tailscale (access )?rules/)
      expect(copy.toLowerCase()).not.toMatch(/local authorization|approve/)
    }
  })

  it('says plainly what Remove does to a person, without promising an approval step', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const copy = value(en, 'sharedCrew.invite_revoke_body')
    expect(copy).toContain('{{name}}')
    expect(copy.toLowerCase()).toMatch(/loses access through this invite/)
    // Removing a guest also pauses their schedules, so it must not promise their work goes on.
    expect(copy.toLowerCase()).not.toMatch(/keeps running|continues|already started/)
    expect(copy.toLowerCase()).not.toMatch(/local authorization|approve/)
  })

  it('gives the hard-limit refusal a cause and a next step, not a retry', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const copy = value(en, 'sharedCrew.refused_containment').toLowerCase()
    expect(copy).not.toContain('try again')
    expect(copy).toContain('hard limits')
    expect(copy).toContain('whoever runs this crew')
  })

  it('never calls a Crew a Team Crew in English', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const said: string[] = []
    const walk = (node: unknown, path: string): void => {
      if (typeof node === 'string') {
        if (node.toLowerCase().includes('team crew')) said.push(path)
      } else if (typeof node === 'object' && node !== null) {
        for (const [key, child] of Object.entries(node)) walk(child, path ? `${path}.${key}` : key)
      }
    }
    walk(en, '')
    expect(said).toEqual([])
  })

  it("names the Crew switcher's heading in a new Crew's note, in every catalog", () => {
    for (const [lang, catalog] of LOCALES) {
      // en-XA is generated from English, so English's own check covers it.
      if (lang === 'en-XA') continue
      const heading = value(catalog, 'components.relayCrews.group_team_crews')
      for (const key of ['relay_crew_added_body', 'relay_crews_available_body']) {
        expect(value(catalog, `components.notifications.systemNote.${key}`), `${lang} ${key}`).toContain(heading)
      }
    }
  })
})
