/**
 * The badge's reader, held against the record the spec pins.
 *
 * `toolRiskRecord.ts` fails safe by drawing nothing, and that state is
 * byte-identical to a healthy release which stamps no record at all. So if the
 * gateway half renames a field, respells a tier or changes the feedback
 * vocabulary, the badge would simply stop appearing — no exception, no failing
 * test, and the feature would look like it had never been wired.
 *
 * This suite closes that hole from the side it can reach, the same way
 * `decisionStripContract.test.ts` does for the strip. It parses the record
 * fixture out of `docs/system-specs/modules/decisions.md` § 9 — the document both
 * halves are written against — and asserts the reader accepts it field for field.
 *
 * It reads the spec rather than restating it on purpose: a copy of the fixture in
 * this file would be a second contract, free to drift from the one the backend
 * author reads.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { readToolRiskRecord, TOOL_RISK_TIERS } from '../pages/chat/toolRiskRecord'

const SPEC = join(__dirname, '../../../docs/system-specs/modules/decisions.md')
const SECTION = '## 9. The `tool.risk` annotation on the tool card'

function sectionText(): string {
  const text = readFileSync(SPEC, 'utf-8')
  const start = text.indexOf(SECTION)
  if (start < 0) throw new Error(`${SECTION} is gone from ${SPEC} — the contract moved or was deleted`)
  return text.slice(start)
}

/** The first fenced JSON block inside § 9. */
function specFixture(): Record<string, unknown> {
  const fence = /```json\n([\s\S]*?)```/.exec(sectionText())
  if (!fence) throw new Error(`no fenced json record under ${SECTION}`)
  return JSON.parse(fence[1]) as Record<string, unknown>
}

describe('the record fixture in the decisions spec', () => {
  const fixture = specFixture()

  it('is a record, so the fence really held the payload', () => {
    expect(Object.keys(fixture).length).toBeGreaterThan(5)
  })

  it('is accepted by the reader, field for field', () => {
    // Every value the badge prints comes from here. A rename on either side
    // breaks this rather than silently hiding the badge.
    expect(readToolRiskRecord(fixture)).toEqual({
      turnId: 'tr-9d41c7',
      tool: 'bash',
      tier: 'risky',
      p: 0.88,
    })
  })

  it('names the point the gateway writes the row under', () => {
    expect(fixture.point).toBe('tool.risk')
  })

  it('carries the fields the reader ignores, so an extra key is proven additive', () => {
    // The row is stamped as the LOG wrote it, so it always carries more than the
    // badge prints. A reader that refused an unknown key would draw nothing for
    // every real record.
    expect(fixture).toHaveProperty('policy')
    expect(fixture).toHaveProperty('flagged')
    expect(fixture).toHaveProperty('latency_ms')
    expect(readToolRiskRecord(fixture)).not.toBeNull()
  })
})

describe('the vocabulary the spec spells', () => {
  it('names every tier the reader accepts, and marks safe as not one of them', () => {
    const text = sectionText()
    for (const tier of TOOL_RISK_TIERS) expect(text).toContain(`\`${tier}\``)
    // The asymmetry is the design, so the spec has to say it where the reader is
    // specified rather than only where the producer is.
    expect(text).toContain('`safe` is not a readable tier')
  })

  it('names the feedback route and the one side this surface rates', () => {
    const text = sectionText()
    expect(text).toContain('`POST /api/decisions/feedback`')
    expect(text).toContain('"side": "jev"')
  })

  it('states that the annotation changes nothing about permission', () => {
    // The feature's whole licence to exist. A spec that stopped saying it would
    // be a spec a later author could read as permission to gate on the answer.
    expect(sectionText()).toContain('It decides NOTHING.')
  })
})
