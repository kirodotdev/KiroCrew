// How a critic reply becomes a report — the two steps between a finished slot and
// a rendered critique, and the shapes that used to fall between them.
//
// `DesignCritiquePage` polls a slot and, once it stops running, turns the turn's
// messages into a `Report`. When that fails the run ends on "The critic replied
// but not in a readable format." — an honest message for a reply that carries no
// report, and a wrong one for a reply that carries a report the reader could not
// find. Everything below is the second case, plus the first case still failing.
import { describe, expect, it } from 'vitest'

import { extractJson, jsonFromMessages, looksLikeReport } from '../apps/design-critique/utils'
import type { SlotData } from '../apps/design-critique/types'

/** A contract-shaped report, as the schema in `prompts.ts` asks for it. */
const REPORT = {
  overallRead: 'The two blue buttons fight each other.',
  health: '1 major',
  tally: { catastrophe: 0, major: 1, minor: 0, cosmetic: 0 },
  screens: [{ step: 1, label: 'Cart', path: '/tmp/cart.png' }],
  findings: [{
    severity: 'major',
    title: 'Two primary buttons compete',
    category: 'Hierarchy',
    scope: 'screen',
    steps: [1],
    location: 'Footer action row',
    evidence: 'Save and Publish are both filled in the same blue.',
    fix: 'Consider demoting one to a quiet button.',
    rules: ['Nielsen: consistency'],
    box: { x: 0.1, y: 0.8, w: 0.8, h: 0.1 },
  }],
  keep: ['Generous spacing between the fields.'],
  couldNotSee: ['Hover states.'],
}
const JSON_REPORT = JSON.stringify(REPORT)

const assistant = (content: string) => ({ role: 'assistant', content })
const msgs = (...rows: Array<{ role: string; content: string }>): SlotData['messages'] => [
  { role: 'user', content: 'critique this' }, ...rows,
]

describe('extractJson', () => {
  it('reads a reply that honours the contract and sends JSON alone', () => {
    expect(extractJson(JSON_REPORT)).toEqual(REPORT)
  })

  it('reads a reply wrapped in a json code fence', () => {
    expect(extractJson('```json\n' + JSON_REPORT + '\n```')).toEqual(REPORT)
  })

  it('reads a reply introduced by a line of prose', () => {
    expect(extractJson('Here is the critique.\n\n' + JSON_REPORT)).toEqual(REPORT)
  })

  // The three shapes a first-`{`-to-last-`}` slice mangled: each one hands
  // `JSON.parse` a span that starts or ends outside the report.
  it('reads a report followed by a remark that contains a brace', () => {
    expect(extractJson(JSON_REPORT + '\n\nTweak the `{primary}` token and re-run.'))
      .toEqual(REPORT)
  })

  it('reads a report preceded by a remark that contains a brace', () => {
    expect(extractJson('I filled the {schema} you gave me.\n\n' + JSON_REPORT))
      .toEqual(REPORT)
  })

  it('prefers the report over a smaller object beside it', () => {
    expect(extractJson('{"note":"draft"}\n\n' + JSON_REPORT)).toEqual(REPORT)
  })

  // Size decides only among candidates the caller will accept. A reply carrying a
  // bigger unrelated object must not cost the report: rejecting the largest span
  // has to continue the search, not end it.
  it('keeps searching past a rejected span to find the report', () => {
    const bulky = { debug: 'x'.repeat(JSON_REPORT.length * 2) }
    const lean = { overallRead: 'Tidy, but the labels crowd each other.', findings: [] }
    const reply = JSON.stringify(bulky) + '\n\n' + JSON.stringify(lean)

    // Without a filter the bigger object wins, which is why the filter belongs
    // inside the span search rather than over its result.
    expect(extractJson(reply)).toEqual(bulky)
    expect(extractJson(reply, looksLikeReport)).toEqual(lean)
  })

  // A brace inside a string value must not be read as structure, or the span it
  // opens swallows the rest of the report.
  it('ignores braces inside string values', () => {
    const withBraces = { ...REPORT, overallRead: 'The {primary} and {danger} tokens collide.' }
    expect(extractJson(JSON.stringify(withBraces))).toEqual(withBraces)
  })

  // The mirror of that: prose is not JSON, so its quotes do not pair. One
  // unmatched quote ahead of the report would put a quote-tracking scanner in
  // string mode for the rest of the reply and hide every brace it contains.
  it('reads a report behind prose carrying an unmatched quote', () => {
    expect(extractJson('Here is "the report:\n' + JSON_REPORT)).toEqual(REPORT)
    expect(extractJson('One screen, one " finding.\n\n' + JSON_REPORT)).toEqual(REPORT)
  })

  it('reads a report whose own strings contain escaped quotes', () => {
    const quoted = { ...REPORT, overallRead: 'The button reads "Save" and "Publish".' }
    expect(extractJson(JSON.stringify(quoted))).toEqual(quoted)
  })

  it('still returns null for a reply that carries no JSON at all', () => {
    expect(extractJson('I had a look and it seems fine.')).toBeNull()
    expect(extractJson('')).toBeNull()
    expect(extractJson(undefined)).toBeNull()
  })

  it('still returns null for a report cut off mid-object', () => {
    expect(extractJson(JSON_REPORT.slice(0, 120))).toBeNull()
  })
})

describe('jsonFromMessages', () => {
  it('reads a turn that finished as one assistant message', () => {
    expect(jsonFromMessages(msgs(assistant(JSON_REPORT)))).toEqual(REPORT)
  })

  // Text either side of a tool group arrives as two assistant rows
  // (`_flush_segment`), so the report is not always the newest one.
  it('reads a report left behind an earlier segment of the same turn', () => {
    expect(jsonFromMessages(msgs(
      assistant(JSON_REPORT),
      { role: 'tool', content: 'artifact_save' },
      assistant('Saved the critique for you.'),
    ))).toEqual(REPORT)
  })

  it('reads the newest report when a turn produced two', () => {
    const stale = { ...REPORT, overallRead: 'A first pass.' }
    expect(jsonFromMessages(msgs(
      assistant(JSON.stringify(stale)),
      assistant(JSON_REPORT),
    ))).toEqual(REPORT)
  })

  // A stream interrupted by a transient backend error is persisted as a partial
  // plus a continuation, and the model is told to carry on where it stopped —
  // which for this prompt means resuming mid-JSON. Neither half parses alone.
  it('rejoins a report split across a partial and its continuation', () => {
    expect(jsonFromMessages(msgs(
      assistant(JSON_REPORT.slice(0, 300)),
      { role: 'error', content: 'The previous response was interrupted.' },
      assistant(JSON_REPORT.slice(300)),
    ), looksLikeReport)).toEqual(REPORT)
  })

  // The split that makes "it parsed" insufficient: cut immediately before a
  // nested object and the continuation is a complete, valid finding object on its
  // own. Taken for the report it would be coerced into a blank critique, written
  // to history, and the slot holding the real report deleted. The newest-first
  // pass has to skip it and reach the join.
  it('does not mistake a continuation that starts at a nested object for the report', () => {
    const cut = JSON_REPORT.indexOf('{"severity"')
    expect(cut).toBeGreaterThan(0)
    const suffix = JSON_REPORT.slice(cut)
    // The trap is real: that suffix parses, and on its own looks like a finding.
    expect(extractJson(suffix)).toMatchObject({ title: 'Two primary buttons compete' })

    expect(jsonFromMessages(msgs(
      assistant(JSON_REPORT.slice(0, cut)),
      assistant(suffix),
    ), looksLikeReport)).toEqual(REPORT)
  })

  it('reads a row carrying the legacy cls role', () => {
    expect(jsonFromMessages([{ role: 'msg msg-a', content: JSON_REPORT }])).toEqual(REPORT)
  })

  it('ignores the user prompt, which quotes the schema back at the model', () => {
    expect(jsonFromMessages([
      { role: 'user', content: 'Return ONLY JSON matching {"overallRead":"…"}' },
    ])).toBeNull()
  })

  it('returns null when no message in the turn carries a report', () => {
    expect(jsonFromMessages(msgs(assistant('I had a look and it seems fine.')))).toBeNull()
    expect(jsonFromMessages([])).toBeNull()
    expect(jsonFromMessages(undefined)).toBeNull()
  })

  it('returns null when the only JSON in the turn is not a report', () => {
    expect(jsonFromMessages(msgs(assistant('{"severity":"major","title":"orphan"}')), looksLikeReport))
      .toBeNull()
  })

  // Same hazard one layer out: the row holding the report also holds a bigger
  // object, so the row must not be abandoned on the first rejected span.
  it('reads a report sharing its row with a bigger unrelated object', () => {
    const bulky = JSON.stringify({ debug: 'x'.repeat(JSON_REPORT.length * 2) })
    expect(jsonFromMessages(msgs(assistant(bulky + '\n\n' + JSON_REPORT)), looksLikeReport))
      .toEqual(REPORT)
  })

  // And the harder version of it: the bigger object borrows a report FIELD NAME,
  // so only its type tells the two apart.
  it('reads the report past a bigger object that only borrows a report key', () => {
    const impostor = JSON.stringify({ findings: 'draft '.repeat(JSON_REPORT.length / 3) })
    expect(impostor.length).toBeGreaterThan(JSON_REPORT.length)
    expect(jsonFromMessages(msgs(assistant(impostor + '\n\n' + JSON_REPORT)), looksLikeReport))
      .toEqual(REPORT)
  })
})

describe('looksLikeReport', () => {
  it('accepts a report, and one carrying only a single report-level field', () => {
    expect(looksLikeReport(REPORT)).toBe(true)
    expect(looksLikeReport({ overallRead: 'Tidy.' })).toBe(true)
    expect(looksLikeReport({ findings: [] })).toBe(true)
    expect(looksLikeReport({ tally: { major: 1 } })).toBe(true)
  })

  it('rejects a finding object, which shares no field with a report', () => {
    expect(looksLikeReport(REPORT.findings[0])).toBe(false)
    expect(looksLikeReport({ box: { x: 0, y: 0, w: 1, h: 1 } })).toBe(false)
  })

  // A key name alone is cheap for unrelated JSON to satisfy, so each field has to
  // arrive as the type the UI renders it as.
  it('rejects a report-shaped key holding the wrong type', () => {
    expect(looksLikeReport({ findings: 'draft notes' })).toBe(false)
    expect(looksLikeReport({ overallRead: { text: 'Tidy.' } })).toBe(false)
    expect(looksLikeReport({ keep: 'spacing' })).toBe(false)
    expect(looksLikeReport({ tally: '1 major' })).toBe(false)
  })

  it('rejects anything that is not a plain object', () => {
    expect(looksLikeReport(null)).toBe(false)
    expect(looksLikeReport([REPORT])).toBe(false)
    expect(looksLikeReport('overallRead')).toBe(false)
  })
})
