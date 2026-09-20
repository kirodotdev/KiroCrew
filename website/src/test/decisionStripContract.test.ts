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
 * fixtures out of `docs/system-specs/modules/decisions.md` — § 8 for the skill
 * selection, § 9 for the model routing, the document both halves are written
 * against — and asserts the reader accepts each field for field. A change to the
 * spec without the matching reader change is now a red test here, and a reader
 * change that drops a field the spec still promises is red too.
 *
 * It reads the spec rather than restating it on purpose: a copy of the fixture
 * in this file would be a second contract, free to drift from the one the
 * backend author reads.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import {
  readDecisionRecord,
  readDecisionStrip,
  readModelRecord,
  readSteerRecord,
} from '../pages/chat/decisionRecord'

const SPEC = join(__dirname, '../../../docs/system-specs/modules/decisions.md')
const SECTION = "## 8. The decision strip's record and feedback"
const STEER_SECTION = '## 10. Mid-turn handling (`message.steer`)'
const MODEL_SECTION = '## 11. Model routing (`model.route`)'

/** The first fenced JSON block inside *section*. */
function specFixtureIn(section: string): Record<string, unknown> {
  const text = readFileSync(SPEC, 'utf-8')
  const start = text.indexOf(section)
  if (start < 0) throw new Error(`${section} is gone from ${SPEC} — the contract moved or was deleted`)
  const fence = /```json\n([\s\S]*?)```/.exec(text.slice(start))
  if (!fence) throw new Error(`no fenced json record under ${section}`)
  return JSON.parse(fence[1]) as Record<string, unknown>
}

function specFixture(): Record<string, unknown> {
  return specFixtureIn(SECTION)
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
      messageChars: 96,
      historyChars: 1840,
      latencyMs: 197,
      dropped: [{ key: 'tst', p: 0.12 }],
      error: null,
    })
  })

  it('names every key the reader needs, so a dropped promise is visible here', () => {
    // Listed explicitly: `toEqual` above proves the reader's OUTPUT, and this
    // proves the spec still promises each INPUT it reads.
    for (const key of [
      'turn_id', 'point', 'baseline', 'jev', 'p', 'tokens_saved',
      'candidates', 'message_chars', 'history_chars', 'latency_ms', 'dropped', 'error',
    ]) {
      expect(Object.keys(fixture), `the spec fixture no longer carries ${key}`).toContain(key)
    }
  })

  it('promises no count that is the same on every turn', () => {
    // A row printing 0 forever is furniture, not a measurement. The menu is one
    // question, so there is no batch count; a history clip is a fact about the
    // transcript read and lives on the call row, not on the receipt.
    expect(Object.keys(fixture)).not.toContain('batches')
    expect(Object.keys(fixture)).not.toContain('truncated')
  })

  it('names the egress as two halves, so the strip can print what actually left', () => {
    // `message_chars` is the excerpt the request carried, not the length of what
    // the owner typed; together with `history_chars` it is the whole egress of
    // one question. A spec that promised only the history would leave the strip
    // printing 0 at the shipped budget while a message did leave.
    expect(readDecisionStrip(fixture)!.messageChars).toBe(96)
    expect(readDecisionStrip(fixture)!.historyChars).toBe(1840)
    // Absent reads as `null`, not 0: the spec's floor-to-zero rule covers the
    // counts a reader may take at face value, and an unstated egress is not one.
    expect(readDecisionStrip({ ...fixture, message_chars: undefined })!.messageChars).toBeNull()
  })

  it('carries the latency under the core log field name, since the record IS the row', () => {
    // The point does not stamp a latency of its own: `log.build_row` writes
    // `latency_ms` among the six core fields and the whole row is what gets
    // published. A reader keying on any other spelling would print nothing.
    expect(readDecisionStrip(fixture)!.latencyMs).toBe(197)
    expect(readDecisionStrip({ ...fixture, latency_ms: -5 })!.latencyMs).toBe(0)
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
    // `message_chars` is a LENGTH, so it is dropped by exact key before the
    // check. The forbidden list itself stays broad — narrowing `message` to
    // `"message"` so the new field fits would admit a `message_text` carrying the
    // conversation, which is the leak this asserts against.
    const { message_chars: _length, ...withoutLength } = fixture
    const serialized = JSON.stringify(withoutLength).toLowerCase()
    for (const forbidden of ['api_key', 'secret', 'prompt', 'message', 'description', 'content']) {
      expect(serialized, `the fixture leaks ${forbidden}`).not.toContain(forbidden)
    }
    // The teeth: masking is by key, so anything else naming a message still fails.
    expect(JSON.stringify({ ...withoutLength, message_text: 'hi' })).toContain('message')
    expect(Object.keys(fixture)).toContain('message_chars')
    expect(typeof fixture.message_chars).toBe('number')
  })
})

describe('the mid-turn handling fixture in the decisions spec', () => {
  // Same hazard as § 8's, from the same direction: `readSteerRecord` fails safe by
  // drawing nothing, which is byte-identical to a send nobody decided for. A field
  // respelled on one side would make the line vanish rather than fail.
  const fixture = specFixtureIn(STEER_SECTION)

  it('is accepted by the reader, field for field', () => {
    expect(readSteerRecord(fixture)).toEqual({
      turnId: 'turn-91c3ab',
      point: 'message.steer',
      choice: 'queue',
      p: 0.83,
      latencyMs: 190,
    })
  })

  it('names every key the reader needs, so a dropped promise is visible here', () => {
    for (const key of ['turn_id', 'point', 'choice', 'p', 'latency_ms']) {
      expect(Object.keys(fixture), `the spec fixture no longer carries ${key}`).toContain(key)
    }
  })

  it('promises the baseline arm the LOG folds on, which the reader ignores', () => {
    // It rides the record because the record IS the row, and the day-file fold
    // reads it; the line does not, because it is `steer` on every row.
    expect(Object.keys(fixture)).toContain('baseline')
    expect(readSteerRecord(fixture)).not.toHaveProperty('baseline')
  })

  it('promises no error field, because a failed decision stamps no record', () => {
    // The producer's own contract: a refusal takes the shipped steer path and
    // writes nothing, so a receipt exists only for a decision that was made.
    expect(Object.keys(fixture)).not.toContain('error')
  })

  it('is refused by the skill reader, and refuses the skill record in turn', () => {
    // Two records on one field. Each must decline the other's shape rather than
    // render it: a steer record read as a skill one would print two empty skill
    // lists under a check mark saying the sides agreed.
    expect(readDecisionStrip(fixture)).toBeNull()
    expect(readSteerRecord(specFixture())).toBeNull()
  })

  it('carries no message text, description or activity — the bound the log sets', () => {
    // `point` is dropped by exact key first: its VALUE is the identifier
    // `message.steer`, so it would match the broad `message` needle below. The
    // needle stays broad rather than narrowing to `"message"`, because a narrow one
    // would admit a `message_text` carrying the conversation — the leak this
    // asserts against.
    const { point: _id, ...withoutPoint } = fixture
    const serialized = JSON.stringify(withoutPoint).toLowerCase()
    for (const forbidden of ['api_key', 'secret', 'prompt', 'message', 'description', 'content', 'activity']) {
      expect(serialized, `the fixture leaks ${forbidden}`).not.toContain(forbidden)
    }
    // The teeth: masking is by key, so anything else naming a message still fails.
    expect(JSON.stringify({ ...withoutPoint, message_text: 'hi' })).toContain('message')
    expect(fixture.point).toBe('message.steer')
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

describe('the model-routing fixture in the decisions spec', () => {
  const fixture = specFixtureIn(MODEL_SECTION)

  it('is accepted by the model reader, field for field', () => {
    expect(readModelRecord(fixture)).toEqual({
      turnId: 'turn-7b1c40',
      point: 'model.route',
      tier: 'complex',
      modelChosen: 'model-c',
      baselineModel: 'model-b',
      p: 0.91,
      latencyMs: 180,
      historyChars: 0,
      truncated: 0,
      applied: true,
      modelUsed: 'model-c',
      error: null,
    })
  })

  it('names every key the reader needs, so a dropped promise is visible here', () => {
    for (const key of [
      'turn_id', 'point', 'tier', 'model_chosen', 'baseline_model',
      'p', 'latency_ms', 'history_chars', 'truncated', 'error',
      // What the turn RAN on, which is not always what was chosen.
      'applied', 'model_used',
    ]) {
      expect(Object.keys(fixture), `the spec fixture no longer carries ${key}`).toContain(key)
    }
  })

  it('carries the point, because that is what tells the two records apart', () => {
    // Dispatched on `point`, never on which fields are present: the whole reason a
    // model record cannot be rendered as a skill one.
    expect(fixture.point).toBe('model.route')
    expect(readDecisionRecord(fixture)).toEqual(readModelRecord(fixture))
    expect(readDecisionStrip(fixture)).toBeNull()
  })

  it('needs the tier whole, so a broken producer draws nothing', () => {
    // The tier IS the answer; a record that cannot name it has no claim to print.
    expect(readModelRecord({ ...fixture, tier: '' })).toBeNull()
  })

  it('accepts an empty model, which is the shipped unpinned state', () => {
    // Every tier of `decisions.model_route` is unpinned until an owner pins one,
    // because no model id may be a hardcoded default. Refusing the record would
    // hide the feature on every install that has not been configured yet.
    expect(readModelRecord({ ...fixture, model_chosen: '' })!.modelChosen).toBe('')
    expect(readModelRecord({ ...fixture, model_chosen: 42 })!.modelChosen).toBe('')
  })

  it('carries no message text, description or key — the bound the log section sets', () => {
    const serialized = JSON.stringify(fixture).toLowerCase()
    for (const forbidden of ['api_key', 'secret', 'prompt', 'message', 'description', 'content']) {
      expect(serialized, `the fixture leaks ${forbidden}`).not.toContain(forbidden)
    }
  })
})
