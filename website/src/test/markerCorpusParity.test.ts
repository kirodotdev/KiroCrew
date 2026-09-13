/**
 * Frontend half of the cross-language marker-grammar parity pin.
 *
 * The grammar is hand-mirrored: `constants.py` and `optionMarker.ts` each re-derive the
 * tempered body, the sibling lookahead and the tail. Nothing made them agree, and the
 * pair has already drifted once in a way that shipped — the `[OPTIONS:]` head is
 * case-insensitive here and case-SENSITIVE on the backend, which is why per-head casing
 * machinery had to be added rather than inherited.
 *
 * Parallel hand-written suites structurally cannot catch that: each side only ever asks
 * its own implementation what it thinks. Both halves read the SAME corpus, so a
 * disagreement between the two implementations fails on whichever side diverged instead
 * of being invisible. The pytest half is `test/test_marker_corpus_parity.py`.
 */
import { describe, it, expect } from 'vitest'

import corpus from '../app-sdk/protocol/markerCorpus.json'
import { parseOptions } from '../app-sdk/protocol/options'

interface Expectation {
  options: string[]
  hasAction: boolean
  stripped: string
}

interface Case extends Partial<Expectation> {
  name: string
  text: string
  /** Present only where the two implementations genuinely disagree. */
  divergent?: { backend: Expectation, frontend: Expectation }
}

const cases = corpus.cases as Case[]

/** This side's required trio, honouring a recorded divergence. */
function expected(c: Case, side: 'backend' | 'frontend'): Expectation {
  return c.divergent ? c.divergent[side] : (c as Expectation)
}

describe('the shared marker corpus', () => {
  it('is readable and populated', () => {
    // Positive control. An empty or moved corpus would make every assertion below
    // register nothing, and a suite that asserts nothing reports green — the same
    // false all-clear this pin exists to prevent, one level up.
    expect(cases.length).toBeGreaterThanOrEqual(8)
    for (const c of cases) {
      expect(c.name, 'case is missing name').toBeDefined()
      expect(c.text, `case ${c.name} is missing text`).toBeDefined()
      for (const side of ['backend', 'frontend'] as const) {
        for (const field of ['options', 'hasAction', 'stripped'] as const) {
          expect(expected(c, side)[field], `case ${c.name} is missing ${field} for ${side}`).toBeDefined()
        }
      }
    }
    expect(new Set(cases.map(c => c.name)).size, 'duplicate case names').toBe(cases.length)
    // The divergence this pin exists for must actually be represented.
    expect(cases.some(c => c.text.includes('[option-actions:')), 'no lowercase action head').toBe(true)
    expect(cases.some(c => c.text.includes('[options:')), 'no lowercase content head').toBe(true)
  })

  it('still genuinely disagrees on every case marked divergent', () => {
    // A resolved divergence must fail here rather than leaving the corpus asserting a
    // disagreement that no longer exists — a stale claim reads as a live finding.
    const divergent = cases.filter(c => c.divergent)
    expect(divergent.length, 'the casing drift this pin was built for is unrepresented')
      .toBeGreaterThan(0)
    for (const c of divergent) {
      expect(c.divergent!.backend, `${c.name} is marked divergent but both sides agree`)
        .not.toEqual(c.divergent!.frontend)
    }
  })
})

describe('the frontend matches the shared corpus', () => {
  for (const c of cases) {
    it(c.name, () => {
      const want = expected(c, 'frontend')
      const parsed = parseOptions(c.text)
      expect(parsed.options, 'options').toEqual(want.options)
      expect(parsed.action !== null, 'hasAction').toBe(want.hasAction)
      expect(parsed.text, 'stripped').toBe(want.stripped)
    })
  }
})
