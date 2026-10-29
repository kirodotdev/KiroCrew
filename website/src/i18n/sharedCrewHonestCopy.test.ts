/**
 * Shared Crew copy that has to match what the gateway does.
 *
 * 1. **Session Manager work is not Unattributed.** A port-forwarded Session
 *    Manager connection arrives on loopback with no forwarding header, which
 *    `shared_crew/identity.py` `derive_principal` cannot tell apart from the
 *    operator's own browser, so with a dashboard token it is the local operator.
 *    The People adapter text must not promise the Unattributed label for it, in
 *    any language: checked against each catalog's own `sharedCrew.unattributed`.
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

  it('never promises Unattributed for Session Manager connections', () => {
    const offenders: string[] = []
    for (const [lang, catalog] of LOCALES) {
      const label = value(catalog, 'sharedCrew.unattributed')
      if (value(catalog, 'sharedCrew.adapter_ssm_body').includes(label)) offenders.push(lang)
    }
    expect(offenders, 'adapter_ssm_body claims the Unattributed label').toEqual([])
  })

  it('describes network trust and qualifies local authorization in shared access copy', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const direct = value(en, 'sharedCrew.direct_link_note')
    expect(direct).toContain('By default, anyone allowed by Tailscale access rules')
    expect(direct).toContain('With local authorization on')
    expect(value(en, 'sharedCrew.invite_manual_body')).toContain('If local authorization is on')
    const revoke = value(en, 'sharedCrew.invite_revoke_body')
    expect(revoke).toContain('With local authorization on')
    expect(revoke).toContain('Crew does not stop their work')
  })

  it('gives the hard-limit refusal a cause and a next step, not a retry', () => {
    const en = LOCALES.find(([lang]) => lang === 'en')?.[1] as Record<string, unknown>
    const copy = value(en, 'sharedCrew.refused_containment').toLowerCase()
    expect(copy).not.toContain('try again')
    expect(copy).toContain('hard limits')
    expect(copy).toContain('whoever runs this crew')
  })
})
