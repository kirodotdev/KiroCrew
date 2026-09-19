/**
 * The transcript's decision strip — its reader and its two states.
 *
 * Three things are pinned here, and each is a way the strip could lie:
 *
 *  - the READER. Every field arrives over the wire, so the record the strip
 *    prints is the record it validated. `agree` is the sharp case: it is derived
 *    from the two name lists rather than read, so a wire flag that contradicts
 *    them cannot put a check mark over a real divergence.
 *  - the ABSENCE of a consent gate, which is deliberate and is the thing most
 *    likely to be "fixed" back into a bug. A stamped record is history that
 *    already sits on this machine, so drawing it sends nothing, and the Settings
 *    switch governs whether a FUTURE turn may ask Jev. Gating the receipt on the
 *    current switch would cost an owner who tried the preview and turned it off
 *    the record of which past replies were Jev-picked.
 *  - the ASSISTANT ROW. An ordinary turn carries no record — the field is
 *    absent on every turn the seam did not decide, which is most of them — so
 *    the absent-field path is the common path: the row must render exactly as it
 *    does without a record, and the strip must appear when one arrives.
 */
import { QueryClient } from '@tanstack/react-query'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { api } from '../api/client'
import { queryClient } from '../api/queryClient'
import AssistantMessage from '../pages/chat/AssistantMessage'
import DecisionStrip from '../pages/chat/DecisionStrip'
import {
  __resetVerdicts,
  decisionStripFieldOf,
  nextVerdict,
  readDecisionStrip,
  recordedVerdict,
} from '../pages/chat/decisionRecord'
import type { ChatMessage } from '../types'

/** A full record as the gateway sends it, with the two sides disagreeing. */
const WIRE = {
  turn_id: 't-1',
  ts: '2026-09-19T07:00:00Z',
  point: 'skills.select',
  baseline: ['brazil', 'crux-code-reviews'],
  jev: ['brazil'],
  agree: false,
  p: 0.81,
  tokens_saved: 3200,
  candidates: 42,
  batches: 3,
  history_chars: 1800,
  truncated: 2,
  dropped: [{ key: 'tst', p: 0.12 }],
  error: null,
}

const ON = { enabled: true, endpoint: 'https://api.example/v1', configured_endpoint: 'https://api.example/v1', permits: true }
const OFF = { enabled: false, endpoint: '', configured_endpoint: 'https://api.example/v1', permits: false }

/** Seed the consent answer the strip reads, without a network round trip. */
function consent(body: unknown) {
  queryClient.setQueryData(['decisionsConsent'], body)
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

describe('readDecisionStrip', () => {
  it('reads a full record and keeps every measurement', () => {
    expect(readDecisionStrip(WIRE)).toEqual({
      turnId: 't-1',
      point: 'skills.select',
      baseline: ['brazil', 'crux-code-reviews'],
      jev: ['brazil'],
      agree: false,
      p: 0.81,
      tokensSaved: 3200,
      candidates: 42,
      batches: 3,
      historyChars: 1800,
      truncated: 2,
      dropped: [{ key: 'tst', p: 0.12 }],
      error: null,
    })
  })

  it('draws nothing for anything that is not a record with a turn id', () => {
    // The absent field is the normal path until the backend ships; the rest are
    // shapes a hand-edited history or a mismatched gateway could hand over.
    for (const bad of [undefined, null, 'x', 7, [], {}, { turn_id: '' }, { turn_id: 3 }]) {
      expect(readDecisionStrip(bad)).toBeNull()
    }
  })

  it('never prints agreement for a record it could not read, whatever the flag says', () => {
    // The two halves of the same rule: a readable pair is compared, an unreadable
    // pair draws nothing at all rather than comparing two empty remnants.
    expect(readDecisionStrip({ ...WIRE, baseline: [], jev: [], agree: false })!.agree).toBe(true)
    expect(readDecisionStrip({ ...WIRE, baseline: {}, jev: {}, agree: true })).toBeNull()
  })

  it('derives agreement from the two lists, ignoring a wire flag that disagrees', () => {
    // A record claiming the sides agreed while naming different skills would
    // otherwise print one list under a check mark and hide the divergence.
    expect(readDecisionStrip({ ...WIRE, agree: true }).agree).toBe(false)
    // Order is not disagreement.
    expect(readDecisionStrip({ ...WIRE, baseline: ['a', 'b'], jev: ['b', 'a'], agree: false }).agree).toBe(true)
    // Two empty picks agree: neither side wanted a skill.
    expect(readDecisionStrip({ ...WIRE, baseline: [], jev: [], agree: false }).agree).toBe(true)
  })

  it('keeps a probability only when it is one', () => {
    expect(readDecisionStrip({ ...WIRE, p: 0 }).p).toBe(0)
    expect(readDecisionStrip({ ...WIRE, p: 1 }).p).toBe(1)
    for (const bad of [-0.1, 1.2, Number.NaN, Number.POSITIVE_INFINITY, '0.5', null]) {
      expect(readDecisionStrip({ ...WIRE, p: bad }).p).toBeNull()
    }
  })

  it('floors every count and refuses a negative or non-numeric one', () => {
    const r = readDecisionStrip({
      ...WIRE, tokens_saved: 12.7, candidates: -3, batches: '4', history_chars: Number.NaN, truncated: 1.9,
    })
    expect(r).toMatchObject({ tokensSaved: 12, candidates: 0, batches: 0, historyChars: 0, truncated: 1 })
  })

  it('refuses a record whose skill lists it cannot read whole', () => {
    // NOT a filtered list. Dropping unreadable entries is the shape that invents
    // a false claim: two lists each broken in a DIFFERENT way both filter to
    // `[]`, and `[]` equals `[]`, so the strip would print "same pick · no
    // skills" over a record whose two sides it never read.
    expect(readDecisionStrip({ ...WIRE, baseline: 'not-a-list', jev: 'also-not' })).toBeNull()
    expect(readDecisionStrip({ ...WIRE, baseline: ['a', 7], jev: ['a'] })).toBeNull()
    expect(readDecisionStrip({ ...WIRE, baseline: ['a'], jev: ['a', ''] })).toBeNull()
    expect(readDecisionStrip({ ...WIRE, jev: null })).toBeNull()
  })

  it('accepts an empty list, because "no skill applies" is a real answer', () => {
    // The no-skill path produces `[]` on purpose, so strictness must not read an
    // empty answer as a broken one.
    const r = readDecisionStrip({ ...WIRE, baseline: [], jev: [] })!
    expect(r.baseline).toEqual([])
    expect(r.jev).toEqual([])
    expect(r.agree).toBe(true)
  })

  it('keeps the dropped entries it can read, since none of them makes a claim', () => {
    // `dropped` stays lenient, deliberately: it is a diagnostic list and nothing
    // is derived from it, so an unreadable entry costs one line of detail rather
    // than making the strip assert something false.
    const r = readDecisionStrip({
      ...WIRE,
      dropped: [{ key: 'ok', p: 'x' }, { key: '' }, 'junk', { p: 0.5 }],
    })!
    // A readable key with an unreadable score keeps the key and scores it 0 —
    // the entry names a refusal that happened, which is the fact worth printing.
    expect(r.dropped).toEqual([{ key: 'ok', p: 0 }])
  })

  it('treats a blank error as no error, and an absent point as the live one', () => {
    expect(readDecisionStrip({ ...WIRE, error: '   ' }).error).toBeNull()
    expect(readDecisionStrip({ ...WIRE, error: 'jev timed out' }).error).toBe('jev timed out')
    expect(readDecisionStrip({ ...WIRE, point: '' }).point).toBe('skills.select')
  })
})

describe('decisionStripFieldOf', () => {
  it('reads the live row and the reloaded row alike', () => {
    expect(decisionStripFieldOf({ decisions_strip: WIRE })).toBe(WIRE)
    expect(decisionStripFieldOf({ meta: { decisions_strip: WIRE } })).toBe(WIRE)
    expect(decisionStripFieldOf({})).toBeUndefined()
  })

  it('returns the same reference every call, so a memoised row is not busted', () => {
    const msg = { meta: { decisions_strip: WIRE } }
    expect(decisionStripFieldOf(msg)).toBe(decisionStripFieldOf(msg))
  })
})

describe('nextVerdict', () => {
  it('takes the answer back when the lit thumb is pressed again', () => {
    expect(nextVerdict(null, 'right')).toBe('right')
    expect(nextVerdict('right', 'right')).toBeNull()
    expect(nextVerdict('right', 'wrong')).toBe('wrong')
  })
})

describe('DecisionStrip', () => {
  const record = readDecisionStrip(WIRE)!

  it('draws the receipt whatever the switch says, because the record is history', () => {
    // The record is already on this machine, so drawing it sends nothing; the
    // switch governs whether a FUTURE turn may ask Jev. Gating on it would mean
    // an owner who tried the preview and turned it off loses the record of which
    // past replies were Jev-picked.
    for (const state of [ON, OFF, undefined]) {
      consent(state)
      render(<DecisionStrip record={record} />)
      expect(screen.getByTestId('decision-strip')).toBeInTheDocument()
      cleanup()
      queryClient.clear()
    }
  })

  it('reads no consent endpoint at all', async () => {
    // Not "reads it and ignores the answer": the request is gone. A strip that
    // still fetched would put one request per visible receipt on the gateway for
    // an answer it does not use.
    const read = vi.spyOn(api, 'getDecisionsConsent')
    render(<DecisionStrip record={record} />)
    expect(screen.getByTestId('decision-strip')).toBeInTheDocument()
    await Promise.resolve()
    expect(read).not.toHaveBeenCalled()
  })

  it('names both sides, the score and the saving on one collapsed line', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    const strip = screen.getByTestId('decision-strip')
    expect(strip).toHaveAttribute('data-agree', 'false')
    expect(strip).toHaveAttribute('data-expanded', 'false')
    expect(strip.textContent).toContain('word match: brazil, crux-code-reviews')
    expect(strip.textContent).toContain('Jev: brazil')
    expect(screen.getByTestId('decision-strip-confidence').textContent).toBe('(0.81)')
    expect(screen.getByTestId('decision-strip-saved').textContent).toContain('3.2K')
    // Collapsed means collapsed: the question's own shape is not on the line.
    expect(strip.textContent).not.toContain('42')
  })

  it('prints the set once when the two sides agreed, naming both and saying so', () => {
    consent(ON)
    const agreed = readDecisionStrip({ ...WIRE, baseline: ['brazil'], jev: ['brazil'] })!
    render(<DecisionStrip record={agreed} />)
    const strip = screen.getByTestId('decision-strip')
    expect(strip).toHaveAttribute('data-agree', 'true')
    // A visible word, not only a glyph: the check alone read as "brazil succeeded".
    expect(screen.getByTestId('decision-strip-agreed-word').textContent).toBe('same pick')
    // Both sides named, so the line says Jev was involved in this turn at all.
    expect(strip.textContent).toContain('Jev and word match: brazil')
    // Still ONE list, not two separately labelled ones. Checked by the DIVERGED
    // line's own shape — its Jev segment — because now that both sides share one
    // term, "Jev and word match: brazil" legitimately contains "word match:".
    expect(strip.textContent).not.toContain('\u00B7 Jev: ')
  })

  it('says so rather than showing a blank when a side picked nothing', () => {
    consent(ON)
    render(<DecisionStrip record={readDecisionStrip({ ...WIRE, jev: [] })!} />)
    expect(screen.getByTestId('decision-strip').textContent).toContain('Jev: no skills')
  })

  it('omits the score and the saving when the record carries neither', () => {
    consent(ON)
    render(<DecisionStrip record={readDecisionStrip({ ...WIRE, p: null, tokens_saved: 0 })!} />)
    expect(screen.queryByTestId('decision-strip-confidence')).toBeNull()
    expect(screen.queryByTestId('decision-strip-saved')).toBeNull()
  })

  it('opens the question’s own shape, the refusals and the second thumbs pair', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    const toggle = screen.getByTestId('decision-strip-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // Collapsed: only the Jev pair is reachable.
    expect(screen.queryByTestId('decision-strip-right-baseline')).toBeNull()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const strip = screen.getByTestId('decision-strip')
    expect(strip.textContent).toContain('42')
    expect(strip.textContent).toContain('1,800')
    expect(strip.textContent).toContain('tst (0.12)')
    // Both sets are named, so "the same list" is checkable and not just claimed.
    // One term for this side everywhere on the card, not three spellings.
    expect(strip.textContent).toContain('Word match')
    expect(strip.textContent).not.toContain('Word matching')
    expect(screen.getByTestId('decision-strip-right-baseline')).toBeInTheDocument()
    expect(screen.getByTestId('decision-strip-wrong-baseline')).toBeInTheDocument()

    // The toggle is a real button, so the keyboard reaches it natively.
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('surfaces a failed decision through the shared error surface', () => {
    consent(ON)
    render(<DecisionStrip record={readDecisionStrip({ ...WIRE, error: 'jev timed out' })!} />)
    fireEvent.click(screen.getByTestId('decision-strip-toggle'))
    const notice = screen.getByTestId('decision-strip-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('jev timed out')
  })

  it('has no error surface when the decision did not fail', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    fireEvent.click(screen.getByTestId('decision-strip-toggle'))
    expect(screen.queryByTestId('decision-strip-error')).toBeNull()
  })

  it('posts the verdict for the side that was rated, and takes it back on a second press', async () => {
    consent(ON)
    const send = vi.spyOn(api, 'sendDecisionsFeedback').mockResolvedValue({})
    render(<DecisionStrip record={record} />)

    fireEvent.click(screen.getByTestId('decision-strip-right-jev'))
    await waitFor(() => expect(screen.getByTestId('decision-strip-right-jev')).toHaveAttribute('aria-pressed', 'true'))
    expect(send).toHaveBeenLastCalledWith('t-1', 'right', 'jev')

    fireEvent.click(screen.getByTestId('decision-strip-right-jev'))
    await waitFor(() => expect(send).toHaveBeenLastCalledWith('t-1', null, 'jev'))
    expect(screen.getByTestId('decision-strip-right-jev')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByTestId('decision-strip-wrong-jev'))
    await waitFor(() => expect(send).toHaveBeenLastCalledWith('t-1', 'wrong', 'jev'))
  })

  it('rates the baseline as its own side', async () => {
    consent(ON)
    const send = vi.spyOn(api, 'sendDecisionsFeedback').mockResolvedValue({})
    render(<DecisionStrip record={record} />)
    fireEvent.click(screen.getByTestId('decision-strip-toggle'))
    fireEvent.click(screen.getByTestId('decision-strip-wrong-baseline'))
    await waitFor(() => expect(send).toHaveBeenLastCalledWith('t-1', 'wrong', 'baseline'))
  })

  it('keeps an accepted verdict when the virtualised row is recycled', async () => {
    consent(ON)
    vi.spyOn(api, 'sendDecisionsFeedback').mockResolvedValue({})
    const first = render(<DecisionStrip record={record} />)
    fireEvent.click(screen.getByTestId('decision-strip-right-jev'))
    await waitFor(() => expect(recordedVerdict('t-1', 'jev')).toBe('right'))
    first.unmount()

    render(<DecisionStrip record={record} />)
    expect(screen.getByTestId('decision-strip-right-jev')).toHaveAttribute('aria-pressed', 'true')
  })

  it('reports a refused verdict instead of pretending it landed', async () => {
    consent(ON)
    vi.spyOn(api, 'sendDecisionsFeedback').mockRejectedValue(new Error('offline'))
    render(<DecisionStrip record={record} />)
    fireEvent.click(screen.getByTestId('decision-strip-right-jev'))
    await waitFor(() =>
      expect(screen.getByTestId('decision-strip-feedback-error-jev').textContent).toContain('Could not save that'),
    )
    // Nothing was accepted, so nothing is lit and nothing is remembered.
    expect(screen.getByTestId('decision-strip-right-jev')).toHaveAttribute('aria-pressed', 'false')
    expect(recordedVerdict('t-1', 'jev')).toBeNull()
  })

  it('gives both thumbs a label, since neither carries text', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    expect(screen.getByLabelText('Jev picked the right skills')).toBeInTheDocument()
    expect(screen.getByLabelText('Jev picked the wrong skills')).toBeInTheDocument()
  })

  it('says what each thumbs pair rates, in text a sighted reader can see', () => {
    // A bare thumb beside a line of text does not say what it rates, and an
    // aria-label answers that for a screen reader only. Both pairs carry the
    // same kind of visible label, so neither reads as the odd one out.
    consent(ON)
    render(<DecisionStrip record={record} />)
    // Short SIDE names, not two near-identical sentences: with both pairs open a
    // reader could not tell which one a thumb counted for.
    expect(screen.getByTestId('decision-strip-rate-label-jev').textContent).toBe('Jev')
    fireEvent.click(screen.getByTestId('decision-strip-toggle'))
    expect(screen.getByTestId('decision-strip-rate-label-baseline').textContent).toBe('Word match')
    expect(screen.getByTestId('decision-strip-rate-label-jev').textContent)
      .not.toBe(screen.getByTestId('decision-strip-rate-label-baseline').textContent)
  })

  it('carries each thumb’s meaning as a tooltip too, since the button renders no text', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    expect(screen.getByTestId('decision-strip-right-jev')).toHaveAttribute('title', 'Jev picked the right skills')
    expect(screen.getByTestId('decision-strip-wrong-jev')).toHaveAttribute('title', 'Jev picked the wrong skills')
  })

  it('puts the agreement check’s meaning where a sighted reader can reach it', () => {
    // The glyph alone reads as "something succeeded". The meaning is on a title
    // as well as in sr-only text, because a title is not reliably announced.
    consent(ON)
    const agreed = readDecisionStrip({ ...WIRE, baseline: ['brazil'], jev: ['brazil'] })!
    const { container } = render(<DecisionStrip record={agreed} />)
    expect(container.querySelector('[title="Both picked the same skills"]')).not.toBeNull()
  })

  it('names the counts in words a reader can act on, not mechanism words', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    fireEvent.click(screen.getByTestId('decision-strip-toggle'))
    const strip = screen.getByTestId('decision-strip')
    // "Batches" and "Left out" drew blanks in a read-aloud: neither says what
    // the number beside it counts.
    expect(strip.textContent).not.toContain('Batches')
    expect(strip.textContent).not.toContain('Left out')
    expect(strip.textContent).toContain('Selection rounds')
    expect(strip.textContent).toContain('Skills cut from the offer')
    // "History characters" drew "no idea, honestly" in a read-aloud. The rename
    // has to keep the UNIT the old label carried, or the number becomes
    // "1,840 of what?" — which is how the first attempt at this regressed.
    expect(strip.textContent).not.toContain('History characters')
    expect(strip.textContent).toContain('Context sent (characters)')
  })

  it('spells the saving in a word the product uses elsewhere', () => {
    consent(ON)
    render(<DecisionStrip record={record} />)
    const saved = screen.getByTestId('decision-strip-saved').textContent ?? ''
    expect(saved).toContain('tokens')
    expect(saved).not.toMatch(/\btok\b/)
  })
})

describe('the assistant row', () => {
  const row = (over: Partial<ChatMessage> = {}): ChatMessage =>
    ({ role: 'assistant', content: 'done', cls: '', ts: '07:00', ...over }) as ChatMessage

  it('renders exactly as today when the row carries no decision record', () => {
    // This is the shipping state: the gateway on `main` stamps no such field, so
    // the strip must be invisible rather than an empty box under every reply.
    consent(ON)
    render(
      <AssistantMessage
        content="done"
        isStreaming={false}
        decisionsStrip={decisionStripFieldOf(row())}
      />,
    )
    expect(screen.queryByTestId('decision-strip')).toBeNull()
    expect(screen.getByText('done')).toBeInTheDocument()
  })

  it('renders the strip under the bubble once the row carries one', () => {
    consent(ON)
    render(
      <AssistantMessage
        content="done"
        isStreaming={false}
        decisionsStrip={decisionStripFieldOf(row({ meta: { decisions_strip: WIRE } }))}
      />,
    )
    expect(screen.getByTestId('decision-strip')).toBeInTheDocument()
  })

  it('draws no strip for a record it cannot validate', () => {
    consent(ON)
    render(
      <AssistantMessage
        content="done"
        isStreaming={false}
        decisionsStrip={decisionStripFieldOf(row({ meta: { decisions_strip: { point: 'skills.select' } } }))}
      />,
    )
    expect(screen.queryByTestId('decision-strip')).toBeNull()
  })
})

describe('the query client the strip reads', () => {
  it('is the shared instance, so the Settings card and this row make one request', () => {
    // A second client would double the consent read and let the two surfaces
    // disagree about whether the seam is on.
    expect(queryClient).toBeInstanceOf(QueryClient)
    consent(ON)
    expect(queryClient.getQueryData(['decisionsConsent'])).toEqual(ON)
  })
})
