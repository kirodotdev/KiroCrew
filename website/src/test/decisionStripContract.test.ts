/**
 * The strip's reader, held against the record the spec pins.
 *
 * `decisionRecord.ts` fails safe by drawing nothing, and that state is
 * byte-identical to a healthy release which stamps no record at all. So if the
 * gateway half renames a field, respells a key or changes the feedback
 * vocabulary, the strip would simply stop appearing — no exception, no failing
 * test, and the feature would look like it had never been wired.
 *
 * This suite closes that hole from the side it can reach. It parses the record
 * fixture out of `docs/system-specs/modules/decisions.md` § 8 — the document
 * both halves are written against — and asserts the reader accepts it field for
 * field. A change to the spec without the matching reader change is now a red
 * test here, and a reader change that drops a field the spec still promises is
 * red too.
 *
 * It reads the spec rather than restating it on purpose: a copy of the fixture
 * in this file would be a second contract, free to drift from the one the
 * backend author reads.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { readDecisionStrip } from '../pages/chat/decisionRecord'

const SPEC = join(__dirname, '../../../docs/system-specs/modules/decisions.md')
const SECTION = "## 8. The decision strip's record and feedback"

/** The first fenced JSON block inside § 8. */
function specFixture(): Record<string, unknown> {
  const text = readFileSync(SPEC, 'utf-8')
  const start = text.indexOf(SECTION)
  if (start < 0) throw new Error(`${SECTION} is gone from ${SPEC} — the contract moved or was deleted`)
  const fence = /```json\n([\s\S]*?)```/.exec(text.slice(start))
  if (!fence) throw new Error(`no fenced json record under ${SECTION}`)
  return JSON.parse(fence[1]) as Record<string, unknown>
}

/** The feedback vocabulary the spec spells, as the strings the client sends.
 *
 * Read from inside the section above, not from the first mention in the file:
 * the route is also described where the gateway's half is specified, and the
 * body this client sends is the sentence under the record it sends it about.
 */
function specFeedbackVocabulary(): string {
  const text = readFileSync(SPEC, 'utf-8')
  const section = text.indexOf(SECTION)
  if (section < 0) throw new Error(`${SECTION} is gone from ${SPEC} — the contract moved or was deleted`)
  const start = text.indexOf('`POST /api/decisions/feedback`', section)
  if (start < 0) throw new Error(`${SECTION} no longer names POST /api/decisions/feedback`)
  return text.slice(start, start + 600)
}

describe('the record fixture in the decisions spec', () => {
  const fixture = specFixture()

  it('is a record, so the fence really held the payload', () => {
    expect(Object.keys(fixture).length).toBeGreaterThan(10)
  })

  it('is accepted by the reader, field for field', () => {
    // Every value the strip prints comes from here. A rename on either side
    // breaks this rather than silently hiding the strip.
    expect(readDecisionStrip(fixture)).toEqual({
      turnId: 'turn-4f2a9c',
      point: 'skills.select',
      baseline: ['brazil', 'crux-code-reviews'],
      jev: ['brazil'],
      agree: false,
      p: 0.81,
      tokensSaved: 3240,
      candidates: 42,
      batches: 3,
      historyChars: 1840,
      truncated: 2,
      dropped: [{ key: 'tst', p: 0.12 }],
      error: null,
    })
  })

  it('names every key the reader needs, so a dropped promise is visible here', () => {
    // Listed explicitly: `toEqual` above proves the reader's OUTPUT, and this
    // proves the spec still promises each INPUT it reads.
    for (const key of [
      'turn_id', 'point', 'baseline', 'jev', 'p', 'tokens_saved',
      'candidates', 'batches', 'history_chars', 'truncated', 'dropped', 'error',
    ]) {
      expect(Object.keys(fixture), `the spec fixture no longer carries ${key}`).toContain(key)
    }
  })

  it('promises neither `agree` nor `ts`, because nothing reads them', () => {
    // Agreement is recomputed from the two lists, so a promised flag could only
    // ever be used to contradict the names beside it. The row carries its own
    // timestamp. Both were dropped before any producer could stamp them.
    expect(Object.keys(fixture)).not.toContain('agree')
    expect(Object.keys(fixture)).not.toContain('ts')
  })

  it('needs both skill lists whole, so a broken producer draws nothing', () => {
    // The fixture is the shape a producer must send. Breaking either list is not
    // a partial record the strip renders less of — it is a record it refuses,
    // because the claim it exists to make is about those two lists.
    expect(readDecisionStrip({ ...fixture, jev: 'oops' })).toBeNull()
    expect(readDecisionStrip({ ...fixture, baseline: ['brazil', 42] })).toBeNull()
  })

  it('ignores a key the reader does not know, so the contract is a floor', () => {
    // A producer may add fields. The strip renders from the keys above and drops
    // the rest rather than refusing the record — including the two just removed,
    // so a gateway still stamping them is not broken by their removal.
    const withExtras = { ...fixture, ts: '2026-09-19T07:04:11Z', agree: true, future_field: { a: 1 } }
    expect(readDecisionStrip(withExtras)).toEqual(readDecisionStrip(fixture))
  })

  it('carries no message text, description or key — the bound the log section sets', () => {
    const serialized = JSON.stringify(fixture).toLowerCase()
    for (const forbidden of ['api_key', 'secret', 'prompt', 'message', 'description', 'content']) {
      expect(serialized, `the fixture leaks ${forbidden}`).not.toContain(forbidden)
    }
  })
})

describe('the feedback vocabulary in the decisions spec', () => {
  const prose = specFeedbackVocabulary()

  it('spells the two sides and the three verdicts the client sends', () => {
    // These are the literals in `api.sendDecisionsFeedback`'s body. A respelling
    // on the server side lands here before it reaches a user.
    for (const literal of ['"jev"', '"baseline"', '"right"', '"wrong"', 'null', 'turn_id', 'side', 'verdict']) {
      expect(prose, `the spec no longer spells ${literal}`).toContain(literal)
    }
  })

  it('keeps the retraction in the contract, not as an accident', () => {
    // `verdict: null` is the only way a reader takes an answer back; if the spec
    // stops promising it, the second press becomes an undefined request.
    expect(prose.toLowerCase()).toContain('retract')
  })
})
