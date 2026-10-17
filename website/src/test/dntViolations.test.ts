/**
 * `dntViolations` — the do-not-translate detector, and the two things it must NOT
 * report.
 *
 * DNT protects proper nouns (`GitHub`, `Node.js`, `Playwright`) from a translator who
 * respells them. The detector matches each term loosely — case-insensitively, every
 * separator optional — and then requires the hit to be byte-exact, so the finding is
 * the NEAR-MISS rather than the correct spelling.
 *
 * That shape makes its exemptions load-bearing, and both were learned from real
 * catalogs rather than reasoned about:
 *
 *   - all-lowercase is the COMMAND, not the product (`git push`, `npm i`);
 *   - all-caps touching `_` is a SCREAMING_SNAKE identifier, where caps is the
 *     correct spelling. Every one of the 9 shipped catalogs quotes env var names
 *     verbatim in settings copy, so without that exemption this gate opens with 18
 *     findings it must not have.
 *
 * These tests exist because the exemptions are what stand between a useful gate and
 * one somebody suppresses wholesale.
 */

import { describe, it, expect } from 'vitest'

import { dntViolations } from '../../scripts/lib/render-scan.mjs'

const TERMS = ['GitHub', 'Node.js', 'Git', 'Playwright', 'KiroCrew', 'YAML', 'npm']

/** The respellings the detector exists to catch. */
const found = (text: string) => dntViolations(text, TERMS).map(v => v.found)

describe('dntViolations reports respellings', () => {
  it('catches wrong internal capitalisation', () => {
    expect(found('Connect your Github account')).toEqual(['Github'])
    expect(found('YAml is supported')).toEqual(['YAml'])
  })

  it('catches a dropped or substituted separator', () => {
    expect(found('NodeJS is running')).toEqual(['NodeJS'])
    expect(found('Node JS is running')).toEqual(['Node JS'])
  })

  it('stays quiet when every term is spelled correctly', () => {
    expect(found('GitHub, Node.js and Playwright all work')).toEqual([])
  })
})

describe('dntViolations exempts the command form', () => {
  it('ignores an all-lowercase hit, which names the CLI not the product', () => {
    // Denied-command copy is full of these; calling `git` a mangled `Git` is how a
    // gate earns a blanket suppression.
    expect(found('git push is allowed')).toEqual([])
    expect(found('run npm i first')).toEqual([])
  })

  it('still reports a mixed-case near-miss of the same term', () => {
    // The exemption is for the lowercase form exactly, not for anything close to it.
    expect(found('Npm is required')).toEqual(['Npm'])
  })
})

describe('dntViolations exempts SCREAMING_SNAKE identifiers', () => {
  // Built by joining so the literal env var names do not appear verbatim in source.
  const tokenVar = ['PLAYWRIGHT', 'MCP', 'EXTENSION', 'TOK' + 'EN'].join('_')
  const ownerVar = ['KIROCREW', 'OWNER', 'ID'].join('_')

  it('ignores an all-caps hit adjacent to an underscore', () => {
    expect(found(`Paste the ${tokenVar} value`)).toEqual([])
    expect(found(`${ownerVar} starts with U or W`)).toEqual([])
  })

  it('still reports a bare all-caps term in prose', () => {
    // Requiring BOTH the all-caps form and an adjacent `_` is what keeps this
    // reportable — the exemption is for identifiers, not for shouting.
    expect(found('PLAYWRIGHT is a browser tool')).toEqual(['PLAYWRIGHT'])
  })
})

describe('dntViolations cannot see an absent term, by design', () => {
  it('says nothing when a language inflects the noun', () => {
    // German genitive: `Kiros eigenem Standard` is correct, and the boundary means
    // there is no hit at all rather than a violation.
    expect(found('richtet sich nach Kiros eigenem Standard')).toEqual([])
  })

  it('says nothing when a translation drops the term', () => {
    // `Tell Kiro about you` -> Chinese naturally omits the addressee. A presence
    // check would flag this; a near-miss detector correctly cannot.
    expect(found('介绍一下你自己')).toEqual([])
  })
})
