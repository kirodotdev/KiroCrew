import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, it, expect } from 'vitest'

import { KIND_BADGE } from '../components/library/LibraryTable'

/**
 * The Artifacts library's kind badge, against the kinds the store can actually list.
 *
 * `KIND_BADGE` is a `Record<Artifact['kind'], ...>`, so a tone missing for a kind
 * already in the union is a compile error. What no compiler can see is the other
 * direction: the backend mints a kind this union never heard of, the lookup answers
 * `undefined`, and the badge quietly falls back to its default tone.
 */
describe('the library kind badge', () => {
  it('gives a dashboard package its own tone', () => {
    expect(KIND_BADGE.dashboard).toBe('aim')
  })

  it('covers the kind the backend lists a dashboard package under', () => {
    const src = readFileSync(
      resolve(__dirname, '../../../src/kiro_crew/artifact_store/dashboard_package.py'),
      'utf8',
    )
    const declared = src.match(/^DASHBOARD_KIND = "([a-z][a-z_]*)"/m)?.[1]
    expect(declared, 'the backend declares no single dashboard kind to read').toBeTruthy()
    expect(Object.keys(KIND_BADGE)).toContain(declared)
  })

  it('gives every kind a tone the badge knows', () => {
    const tones = new Set(['ok', 'err', 'warn', 'aim'])
    for (const [kind, tone] of Object.entries(KIND_BADGE)) {
      expect(tones.has(tone), `${kind} carries a tone the badge cannot render`).toBe(true)
    }
  })
})
