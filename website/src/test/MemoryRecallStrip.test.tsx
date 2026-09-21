/**
 * The transcript's receipt for one recalled-memory decision.
 *
 * Four things are pinned, each a way the strip could lie:
 *
 *  - the READER. Every field arrives over the wire, and `point` is REQUIRED here:
 *    an absent one is the oldest producer's shape and belongs to the skill
 *    reader, so inferring this record from the fields present would claim a
 *    decision about memory over a record that never named one.
 *  - the two records DECLINING each other. They share one `meta.decisions_strip`
 *    field, so each must refuse the other's shape rather than render it — a
 *    memory record read as a skill one would print memory ids as skill keys.
 *  - the COLLAPSED line being counts, not ids. A memory id is a store handle a
 *    reader cannot read anything off, so six of them on one line would be six
 *    opaque words where the only actionable fact is how many were dropped.
 *  - the ASSISTANT ROW. An ordinary turn carries no record, so the absent-field
 *    path is the common path and must render exactly as it does today.
 */
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { queryClient } from '../api/queryClient'
import AssistantMessage from '../pages/chat/AssistantMessage'
import {
  __resetVerdicts,
  decisionStripFieldOf,
  readDecisionStrip,
  readMemoryRecallRecord,
  readSteerRecord,
} from '../pages/chat/decisionRecord'
import MemoryRecallStrip from '../pages/chat/MemoryRecallStrip'
import type { ChatMessage } from '../types'

/** A full record as the gateway sends it, with two memories dropped. */
const WIRE = {
  turn_id: 't-9',
  ts: '2026-09-21T07:00:00Z',
  point: 'memory.recall',
  baseline_keys: ['mem-a', 'mem-b', 'mem-c', 'mem-d', 'mem-e', 'mem-f'],
  jev_keys: ['mem-a', 'mem-c', 'mem-f'],
  agree: false,
  p: 0.81,
  chars_saved: 2100,
  candidates: 6,
  message_chars: 96,
  latency_ms: 210,
  error: null,
}

/** The skill record, for the mutual-refusal pair. */
const SKILL_WIRE = {
  turn_id: 't-1',
  point: 'skills.select',
  baseline: ['brazil'],
  jev: [],
  p: 0.7,
  latency_ms: 100,
}

beforeEach(() => {
  __resetVerdicts()
  queryClient.clear()
})

afterEach(() => {
  cleanup()
  queryClient.clear()
  vi.restoreAllMocks()
})

describe('readMemoryRecallRecord', () => {
  it('reads a full record and keeps every measurement', () => {
    expect(readMemoryRecallRecord(WIRE)).toEqual({
      turnId: 't-9',
      point: 'memory.recall',
      baselineKeys: ['mem-a', 'mem-b', 'mem-c', 'mem-d', 'mem-e', 'mem-f'],
      jevKeys: ['mem-a', 'mem-c', 'mem-f'],
      agree: false,
      p: 0.81,
      charsSaved: 2100,
      candidates: 6,
      messageChars: 96,
      latencyMs: 210,
      error: null,
    })
  })

  it('draws nothing for anything that is not a record with a turn id', () => {
    for (const bad of [undefined, null, 'x', 7, [], {}, { point: 'memory.recall' }]) {
      expect(readMemoryRecallRecord(bad)).toBeNull()
    }
  })

  it('requires the point, so an absent one is left to the skill reader', () => {
    // An absent `point` is what the only earlier producer stamped. Reading it as
    // this record would put memory copy over a skill selection.
    const { point: _p, ...withoutPoint } = WIRE
    expect(readMemoryRecallRecord(withoutPoint)).toBeNull()
    expect(readMemoryRecallRecord({ ...WIRE, point: 'skills.select' })).toBeNull()
  })

  it('needs both lists whole, so a broken producer draws nothing', () => {
    expect(readMemoryRecallRecord({ ...WIRE, jev_keys: 'oops' })).toBeNull()
    expect(readMemoryRecallRecord({ ...WIRE, baseline_keys: ['a', 42] })).toBeNull()
  })

  it('derives agreement from the two lists rather than the wire flag', () => {
    // A flag that disagreed with the lists beside it could only ever hide a real
    // divergence, so it is recomputed.
    expect(readMemoryRecallRecord({ ...WIRE, agree: true })!.agree).toBe(false)
    const same = { ...WIRE, jev_keys: [...WIRE.baseline_keys].reverse(), agree: false }
    expect(readMemoryRecallRecord(same)!.agree).toBe(true)
  })

  it('accepts an empty kept list, because keeping nothing is a real answer', () => {
    expect(readMemoryRecallRecord({ ...WIRE, jev_keys: [] })!.jevKeys).toEqual([])
  })

  it('prints no score for a probability that is not one', () => {
    for (const bad of [-0.1, 1.2, 'x', null, undefined, NaN]) {
      expect(readMemoryRecallRecord({ ...WIRE, p: bad })!.p).toBeNull()
    }
  })

  it('floors the counts and reads an unstated excerpt as not stated', () => {
    const floored = readMemoryRecallRecord({
      ...WIRE,
      chars_saved: -5,
      candidates: 'x',
      latency_ms: -1,
      message_chars: undefined,
    })!
    expect(floored.charsSaved).toBe(0)
    expect(floored.candidates).toBe(0)
    expect(floored.latencyMs).toBe(0)
    expect(floored.messageChars).toBeNull()
  })

  it('treats a blank error as no error', () => {
    expect(readMemoryRecallRecord({ ...WIRE, error: '   ' })!.error).toBeNull()
    expect(readMemoryRecallRecord({ ...WIRE, error: 'timeout' })!.error).toBe('timeout')
  })

  it('ignores a key it does not know, so the contract is a floor', () => {
    const extras = { ...WIRE, future_field: { a: 1 }, agree: true }
    expect(readMemoryRecallRecord(extras)).toEqual(readMemoryRecallRecord(WIRE))
  })
})

describe('the three records on one field', () => {
  it('each reader declines the other two shapes', () => {
    // One field, three producers. A record read by the wrong reader would print a
    // receipt for a decision nobody made.
    expect(readDecisionStrip(WIRE)).toBeNull()
    expect(readSteerRecord(WIRE)).toBeNull()
    expect(readMemoryRecallRecord(SKILL_WIRE)).toBeNull()
    expect(readMemoryRecallRecord({ turn_id: 't', point: 'message.steer', choice: 'queue' })).toBeNull()
  })

  it('is why the memory record names its lists for what they hold', () => {
    // `baseline`/`jev` are what the skill reader requires. Spelling them here
    // would make the skill strip accept this record and draw memory ids as skill
    // keys — so the refusal above is a property of the FIELD NAMES, not a guess.
    expect(Object.keys(WIRE)).not.toContain('baseline')
    expect(Object.keys(WIRE)).not.toContain('jev')
    const respelled = { ...WIRE, baseline: WIRE.baseline_keys, jev: WIRE.jev_keys }
    expect(readDecisionStrip(respelled)).not.toBeNull()
  })
})

describe('the collapsed line', () => {
  const record = readMemoryRecallRecord(WIRE)!

  it('prints counts, the score, the latency and the saving', () => {
    render(<MemoryRecallStrip record={record} />)
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    expect(text).toContain('memory')
    expect(text).toContain('recalled')
    expect(text).toContain('6')
    expect(text).toContain('3')
    // Each number carries its own word on the line itself. A bare `0.81` beside a
    // duration reads as a second duration, and `chars` alone does not say chars of what.
    expect(screen.getByTestId('memory-recall-strip-scores').textContent).toContain('confidence 0.81')
    expect(screen.getByTestId('memory-recall-strip-scores').textContent).toContain('210 ms')
    expect(screen.getByTestId('memory-recall-strip-saved').textContent).toContain('prompt chars')
  })

  it('names no memory id until it is expanded', () => {
    // Counts, not handles: the ids are in the body for a reader who wants one.
    render(<MemoryRecallStrip record={record} />)
    expect(screen.getByTestId('memory-recall-strip').textContent).not.toContain('mem-a')
  })

  it('prints both counts even when the two sides agreed', () => {
    // "similarity 6 · Jev kept 6" already says they agreed, so there is no second
    // way to say it and no agreement branch to get wrong.
    const agreed = readMemoryRecallRecord({ ...WIRE, jev_keys: WIRE.baseline_keys })!
    render(<MemoryRecallStrip record={agreed} />)
    const strip = screen.getByTestId('memory-recall-strip')
    expect(strip.dataset.agree).toBe('true')
    expect(strip.textContent).toContain('6')
  })

  it('credits the FALLBACK, not Jev, when the decision failed', () => {
    // `error` set means the decision did not land and the shipped recall was
    // injected. "Jev kept: 6" beside "Decision failed" credits an actor that chose
    // nothing, which is the one claim this line must not make.
    render(<MemoryRecallStrip record={readMemoryRecallRecord({ ...WIRE, error: 'timeout' })!} />)
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    expect(text).toContain('kept after fallback')
    expect(text).not.toContain('Jev kept')
  })

  it('credits Jev on a healthy record', () => {
    render(<MemoryRecallStrip record={record} />)
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    expect(text).toContain('Jev kept')
    expect(text).not.toContain('kept after fallback')
  })

  it('says which characters the saving counts, on the line and not only in the title', () => {
    // The visible text carries the meaning, because a hover legend is unreachable on
    // touch and silent to a screen reader. The title stays, with the fuller sentence.
    render(<MemoryRecallStrip record={record} />)
    expect(screen.getByTestId('memory-recall-strip-saved').textContent).toContain('prompt chars')
    expect(screen.getByTestId('memory-recall-strip-saved')).toHaveAttribute(
      'title',
      'Prompt characters the narrower memory block saved.',
    )
  })

  it('names the confidence on the line and not only in the title', () => {
    render(<MemoryRecallStrip record={record} />)
    const scores = screen.getByTestId('memory-recall-strip-scores')
    expect(scores.textContent).toContain('confidence 0.81')
    // Still legended, for what the number is an average OF.
    expect(scores).toHaveAttribute(
      'title',
      "Jev's average confidence that one memory was worth the prompt, from 0 to 1, and how long the answer took",
    )
  })

  it('draws no saving row when nothing was saved', () => {
    render(<MemoryRecallStrip record={readMemoryRecallRecord({ ...WIRE, chars_saved: 0 })!} />)
    expect(screen.queryByTestId('memory-recall-strip-saved')).toBeNull()
  })

  it('draws no parenthetical when the record carried neither number', () => {
    const bare = readMemoryRecallRecord({ ...WIRE, p: null, latency_ms: 0 })!
    render(<MemoryRecallStrip record={bare} />)
    expect(screen.queryByTestId('memory-recall-strip-scores')).toBeNull()
  })
})

describe('the expanded body', () => {
  const record = readMemoryRecallRecord(WIRE)!

  it('names both sets, so "three were dropped" is checkable', () => {
    render(<MemoryRecallStrip record={record} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    for (const key of WIRE.baseline_keys) expect(text).toContain(key)
  })

  it('names the empty kept set in words rather than as a blank', () => {
    render(<MemoryRecallStrip record={readMemoryRecallRecord({ ...WIRE, jev_keys: [] })!} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    expect(screen.getByTestId('memory-recall-strip').textContent).toContain('no memories')
  })

  it('draws no egress row for a record that did not measure the excerpt', () => {
    const unstated = readMemoryRecallRecord({ ...WIRE, message_chars: undefined })!
    render(<MemoryRecallStrip record={unstated} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    expect(screen.getByTestId('memory-recall-strip').textContent).not.toContain('Sent to Jev')
  })

  it('retitles the kept list on a failure rather than crediting Jev', () => {
    // Retitled and not hidden: the list is still what the prompt carried, which is
    // worth seeing. It is the ATTRIBUTION that was wrong, not the content.
    render(<MemoryRecallStrip record={readMemoryRecallRecord({ ...WIRE, error: 'timeout' })!} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    expect(text).toContain('Kept (fallback)')
    expect(text).not.toContain('Kept by Jev')
    // And the list itself is still there.
    for (const key of WIRE.baseline_keys) expect(text).toContain(key)
  })

  it('titles the kept list for Jev on a healthy record', () => {
    render(<MemoryRecallStrip record={record} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    expect(text).toContain('Kept by Jev')
    expect(text).not.toContain('Kept (fallback)')
  })

  it('carries a thumbs pair for each side', () => {
    // A reader must be able to say the similarity ranker was the better judge,
    // not only rate Jev.
    render(<MemoryRecallStrip record={record} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    expect(screen.getByLabelText('Jev kept the right memories')).toBeInTheDocument()
    expect(screen.getByLabelText('Similarity recalled the right memories')).toBeInTheDocument()
  })

  it('names both visible rate labels as ratings, not as actors', () => {
    // The two pairs sit on one strip. Labelled with the actor alone -- "Jev" beside
    // "Similarity" -- neither said it was a thumbs pair for a MEMORY pick, and the
    // jev one reused the skills strip's shared key.
    render(<MemoryRecallStrip record={record} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    const text = screen.getByTestId('memory-recall-strip').textContent ?? ''
    expect(text).toContain("Rate Jev's pick")
    expect(text).toContain('Rate the similarity pick')
  })

  it('surfaces a failed decision through the shared error surface', () => {
    render(<MemoryRecallStrip record={readMemoryRecallRecord({ ...WIRE, error: 'timeout' })!} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    expect(screen.getByTestId('memory-recall-strip-error')).toBeInTheDocument()
  })

  it('offers the agent hand-off on that failure', () => {
    // The strip holds no draft input and the failure names the judge rather than
    // the reply, so there is something for an agent to pick up and nothing to lose.
    render(<MemoryRecallStrip record={readMemoryRecallRecord({ ...WIRE, error: 'timeout' })!} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('draws no hand-off when the decision did not fail', () => {
    // The notice renders nothing without a message, so the hand-off must not be a
    // button sitting under every healthy strip.
    render(<MemoryRecallStrip record={record} />)
    fireEvent.click(screen.getByTestId('memory-recall-strip-toggle'))
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('exposes the toggle state to assistive technology', () => {
    render(<MemoryRecallStrip record={record} />)
    const toggle = screen.getByTestId('memory-recall-strip-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('the assistant row', () => {
  const row = (over: Partial<ChatMessage> = {}): ChatMessage =>
    ({ role: 'assistant', content: 'done', cls: '', ts: '07:00', ...over }) as ChatMessage

  it('renders exactly as today when the row carries no record', () => {
    render(
      <AssistantMessage content="done" isStreaming={false} decisionsStrip={decisionStripFieldOf(row())} />,
    )
    expect(screen.queryByTestId('memory-recall-strip')).toBeNull()
    expect(screen.getByText('done')).toBeInTheDocument()
  })

  it('renders the memory strip once the row carries one', () => {
    render(
      <AssistantMessage
        content="done"
        isStreaming={false}
        decisionsStrip={decisionStripFieldOf(row({ meta: { decisions_strip: WIRE } }))}
      />,
    )
    expect(screen.getByTestId('memory-recall-strip')).toBeInTheDocument()
    // And never both: a skill record and a memory record cannot be one turn's.
    expect(screen.queryByTestId('decision-strip')).toBeNull()
  })

  it('renders the skill strip and no memory strip for a skill record', () => {
    render(
      <AssistantMessage
        content="done"
        isStreaming={false}
        decisionsStrip={decisionStripFieldOf(row({ meta: { decisions_strip: SKILL_WIRE } }))}
      />,
    )
    expect(screen.getByTestId('decision-strip')).toBeInTheDocument()
    expect(screen.queryByTestId('memory-recall-strip')).toBeNull()
  })
})
