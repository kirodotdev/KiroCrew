/**
 * The model-routing row of the decision strip, and the reader that feeds it.
 *
 * Three things are pinned, and each is a way the row could lie:
 *
 *  - the READER. A record that cannot name the tier or the model it routed to has
 *    no claim left to make, so it draws nothing rather than a row with a blank in
 *    it. And a record is dispatched on its OWN `point`, never on which fields
 *    happen to be present: a model row read through the skill reader would print
 *    two empty skill lists and a check mark saying the sides agreed.
 *  - the LINE. The tier alone says nothing a reader can disagree with, so the row
 *    names the model the turn ran on AND the model it would otherwise have used.
 *  - the LIST. Two points can decide one reply, so the row carries a list of
 *    records and renders one strip per record — while a bare object, which is what
 *    an earlier release stamped, still renders as the one record it is.
 */
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { describe, it, expect, afterEach } from 'vitest'

import { queryClient } from '../api/queryClient'
import AssistantMessage from '../pages/chat/AssistantMessage'
import DecisionStrip from '../pages/chat/DecisionStrip'
import {
  __resetVerdicts,
  isModelRecord,
  readDecisionRecord,
  readDecisionRecords,
  readDecisionStrip,
  readModelRecord,
} from '../pages/chat/decisionRecord'

/** A model-routing record as the gateway stamps it. */
const MODEL_WIRE = {
  turn_id: 'tm-1',
  ts: '2026-09-20T07:00:00Z',
  point: 'model.route',
  session: 'abc123def456',
  tier: 'complex',
  model_chosen: 'model-c',
  baseline_model: 'model-b',
  p: 0.91,
  latency_ms: 180,
  history_chars: 0,
  truncated: 0,
  scrubbed: false,
  answers: null,
  error: null,
}

/** A skill-selection record, for the two-records-on-one-reply case. */
const SKILL_WIRE = {
  turn_id: 'ts-1',
  point: 'skills.select',
  baseline: ['brazil', 'tst'],
  jev: ['brazil'],
  p: 0.8,
  tokens_saved: 900,
  candidates: 12,
  batches: 1,
  history_chars: 0,
  truncated: 0,
  error: null,
}

afterEach(() => {
  cleanup()
  queryClient.clear()
  __resetVerdicts()
})

describe('readModelRecord', () => {
  it('reads a full record and keeps every measurement', () => {
    expect(readModelRecord(MODEL_WIRE)).toEqual({
      turnId: 'tm-1',
      point: 'model.route',
      tier: 'complex',
      modelChosen: 'model-c',
      baselineModel: 'model-b',
      p: 0.91,
      latencyMs: 180,
      historyChars: 0,
      truncated: 0,
      applied: true,
      modelUsed: '',
      error: null,
    })
  })

  it('reads a row that predates the applied field as applied', () => {
    // Only an observed failure writes `false`. An older row carries no field at all,
    // and reading its absence as a failed switch would invent one on every archived
    // reply.
    const { applied, ...rest } = MODEL_WIRE as Record<string, unknown> & { applied?: boolean }
    expect(applied).toBeUndefined()
    expect(readModelRecord(rest)!.applied).toBe(true)
    expect(readModelRecord({ ...MODEL_WIRE, applied: false })!.applied).toBe(false)
  })

  it('draws nothing without a turn id or a tier', () => {
    // The turn it is about, and the answer. A row missing either has no claim.
    expect(readModelRecord({ ...MODEL_WIRE, turn_id: '' })).toBeNull()
    expect(readModelRecord({ ...MODEL_WIRE, tier: '' })).toBeNull()
    expect(readModelRecord({ ...MODEL_WIRE, tier: 42 })).toBeNull()
    expect(readModelRecord(undefined)).toBeNull()
  })

  it('accepts an empty model, because that is the SHIPPED state', () => {
    // Every tier is unpinned until an owner pins one, since no model id may be a
    // hardcoded default. Refusing this record would hide the feature on every
    // install that has not been configured yet.
    for (const empty of ['', '   ', undefined, 42]) {
      expect(readModelRecord({ ...MODEL_WIRE, model_chosen: empty })!.modelChosen).toBe('')
    }
  })

  it('accepts a tier this build does not know, so a shipped tier is shown', () => {
    // The tiers are the backend's own question domain. Refusing a fourth would
    // hide it instead of showing it, and the gate already held the answer against
    // the options it offered.
    expect(readModelRecord({ ...MODEL_WIRE, tier: 'trivial' })!.tier).toBe('trivial')
  })

  it("keeps an empty baseline model, because it is the backend's own default", () => {
    expect(readModelRecord({ ...MODEL_WIRE, baseline_model: '' })!.baselineModel).toBe('')
  })

  it('prints no score for a number that is not a probability', () => {
    for (const bad of [-0.1, 1.2, Number.NaN, '0.9', null]) {
      expect(readModelRecord({ ...MODEL_WIRE, p: bad })!.p).toBeNull()
    }
  })

  it('reads a negative or unparsable count as zero', () => {
    const read = readModelRecord({ ...MODEL_WIRE, latency_ms: -5, history_chars: 'x' })!
    expect(read.latencyMs).toBe(0)
    expect(read.historyChars).toBe(0)
  })
})

describe('readDecisionRecord dispatches on the point', () => {
  it('sends a model record to the model reader', () => {
    const read = readDecisionRecord(MODEL_WIRE)!
    expect(isModelRecord(read)).toBe(true)
  })

  it('sends a skill record, and one naming no point, to the skill reader', () => {
    // An absent point is what the earlier producer stamped, and the skill reader
    // already defaults to it.
    expect(isModelRecord(readDecisionRecord(SKILL_WIRE)!)).toBe(false)
    const { point, ...noPoint } = SKILL_WIRE
    expect(point).toBe('skills.select')
    expect(readDecisionRecord(noPoint)).toEqual(readDecisionStrip(SKILL_WIRE))
  })

  it('draws nothing for a point it does not know', () => {
    // Rendering an unknown record through a known reader is how a row comes to
    // print a claim nobody made.
    expect(readDecisionRecord({ ...MODEL_WIRE, point: 'cron.novelty' })).toBeNull()
  })

  it('never reads a model record as a skill one', () => {
    // The sharp case: the skill reader needs both name lists, so it refuses this
    // record outright rather than rendering two empty lists as "same pick".
    expect(readDecisionStrip(MODEL_WIRE)).toBeNull()
  })
})

describe('readDecisionRecords', () => {
  it('reads the list the gateway stamps, in publish order', () => {
    const records = readDecisionRecords([SKILL_WIRE, MODEL_WIRE])
    expect(records.map(r => r.point)).toEqual(['skills.select', 'model.route'])
  })

  it('reads a bare object as a list of one, so an older row still renders', () => {
    expect(readDecisionRecords(SKILL_WIRE)).toEqual([readDecisionStrip(SKILL_WIRE)])
  })

  it('drops an unreadable member instead of failing the whole row', () => {
    // The records are independent decisions; losing one receipt is not a reason to
    // lose the other's.
    const records = readDecisionRecords([{ turn_id: '' }, MODEL_WIRE])
    expect(records.map(r => r.point)).toEqual(['model.route'])
  })

  it('reads nothing at all as an empty list', () => {
    for (const nothing of [undefined, null, [], 'x', 7]) {
      expect(readDecisionRecords(nothing)).toEqual([])
    }
  })
})

describe('the model row', () => {
  const record = readModelRecord(MODEL_WIRE)!

  it('names the tier and the model it ran on, and keeps the baseline one click in', () => {
    render(<DecisionStrip record={record} />)
    const strip = screen.getByTestId('decision-strip-model')
    expect(strip).toHaveAttribute('data-tier', 'complex')
    expect(strip).toHaveAttribute('data-expanded', 'false')
    expect(screen.getByTestId('decision-strip-model-pick').textContent).toContain('Jev: complex')
    expect(screen.getByTestId('decision-strip-model-pick').textContent).toContain('model-c')
    expect(screen.getByTestId('decision-strip-model-confidence').textContent).toBe('(0.91)')
    expect(screen.getByTestId('decision-strip-model-latency').textContent).toContain('180')
    // The collapsed line carries the DECISION and nothing else: on a routed
    // session the baseline is the previous turn's tier, so a caption there read as
    // a "default" that changed every turn. It is still on the record, labelled for
    // what it is, one click in.
    expect(screen.queryByTestId('decision-strip-model-baseline')).toBeNull()
    fireEvent.click(screen.getByTestId('decision-strip-model-toggle'))
    expect(screen.getByText('Model without routing').closest('div')!)
      .toHaveTextContent('model-b')
  })

  it('says the tier is unpinned rather than leaving a gap where a model goes', () => {
    const unpinned = readModelRecord({ ...MODEL_WIRE, model_chosen: '' })!
    render(<DecisionStrip record={unpinned} />)
    const strip = screen.getByTestId('decision-strip-model')
    expect(strip).toHaveAttribute('data-pinned', 'false')
    expect(screen.getByTestId('decision-strip-model-pick').textContent).toContain('Jev: complex')
    expect(screen.getByTestId('decision-strip-model-pick').textContent).toContain('(unpinned)')
    // The tier and the score are still shown: that is the evidence to pin from.
    expect(screen.getByTestId('decision-strip-model-confidence').textContent).toBe('(0.91)')
  })

  it('says a switch did not take, and names the model the turn ran on', () => {
    // A backend that judges the model VALUE can accept the call and stay put, so the
    // durable row records `applied: false`. The row an owner reads has to say that, or
    // it credits a model the turn never ran on.
    const notApplied = readModelRecord({ ...MODEL_WIRE, applied: false, model_used: 'model-b' })!
    render(<DecisionStrip record={notApplied} />)

    expect(screen.getByTestId('decision-strip-model')).toHaveAttribute('data-applied', 'false')
    expect(screen.getByTestId('decision-strip-model-not-applied')).toHaveTextContent('model-b')
    // And the expanded panel agrees with the collapsed line: "Model used" names what
    // ran, or the receipt an owner pins from contradicts itself one click apart.
    fireEvent.click(screen.getByTestId('decision-strip-model-toggle'))
    const used = screen.getByText('Model used').closest('div')!
    expect(used).toHaveTextContent('model-b')
    expect(used).not.toHaveTextContent('model-c')
    // The asked-for model is still on the row: what was decided and what happened are
    // two facts, and dropping the first would hide the failure rather than report it.
    expect(screen.getByTestId('decision-strip-model-pick')).toHaveTextContent('model-c')
  })

  it('says nothing about applying when the switch did take', () => {
    render(<DecisionStrip record={readModelRecord(MODEL_WIRE)!} />)

    expect(screen.getByTestId('decision-strip-model')).toHaveAttribute('data-applied', 'true')
    expect(screen.queryByTestId('decision-strip-model-not-applied')).toBeNull()
  })

  it('marks a pinned row as pinned', () => {
    render(<DecisionStrip record={readModelRecord(MODEL_WIRE)!} />)
    expect(screen.getByTestId('decision-strip-model')).toHaveAttribute('data-pinned', 'true')
  })

  it("names the built-in default rather than an empty gap", () => {
    const inherited = readModelRecord({ ...MODEL_WIRE, baseline_model: '' })!
    render(<DecisionStrip record={inherited} />)
    fireEvent.click(screen.getByTestId('decision-strip-model-toggle'))
    expect(screen.getByText('Model without routing').closest('div')!)
      .toHaveTextContent('Built-in default')
  })

  it('carries ONE thumbs pair, on the Jev side', () => {
    // There is no second judgement to rate: the alternative is the absence of one,
    // so a `baseline` pair would ask the reader to rate "whatever the session was
    // on", which is not a decision anybody made about this turn.
    render(<DecisionStrip record={record} />)
    expect(screen.getByTestId('decision-strip-right-jev')).toBeInTheDocument()
    expect(screen.queryByTestId('decision-strip-right-baseline')).toBeNull()
  })

  it('prints no score when the answer carried none', () => {
    const scoreless = readModelRecord({ ...MODEL_WIRE, p: null })!
    render(<DecisionStrip record={scoreless} />)
    expect(screen.queryByTestId('decision-strip-model-confidence')).toBeNull()
  })

  it('is not the skill row, and the skill row is not it', () => {
    render(<DecisionStrip record={record} />)
    expect(screen.queryByTestId('decision-strip')).toBeNull()
    cleanup()
    render(<DecisionStrip record={readDecisionStrip(SKILL_WIRE)!} />)
    expect(screen.queryByTestId('decision-strip-model')).toBeNull()
  })
})

describe('a reply decided by two points', () => {
  it('draws one row per decision', () => {
    render(
      <AssistantMessage
        content="an answer"
        messageTs="2026-09-20T07:00:00Z"
        decisionsStrip={[SKILL_WIRE, MODEL_WIRE]}
      />,
    )
    expect(screen.getByTestId('decision-strip')).toBeInTheDocument()
    expect(screen.getByTestId('decision-strip-model')).toBeInTheDocument()
  })

  it('draws nothing for an ordinary turn, which is most of them', () => {
    render(<AssistantMessage content="an answer" messageTs="2026-09-20T07:00:00Z" />)
    expect(screen.queryByTestId('decision-strip')).toBeNull()
    expect(screen.queryByTestId('decision-strip-model')).toBeNull()
  })
})
