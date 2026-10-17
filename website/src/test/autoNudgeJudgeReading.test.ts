/**
 * The judge readout the monitor popover draws: which loops get a line at all, and
 * what the line is allowed to say.
 *
 * Kept on the pure helpers rather than on the rendered popover because the two
 * decisions worth pinning are both here -- whether a loop has a judge, and whether
 * that judge has answered yet -- while the render is a conditional and three
 * interpolations that `tsc` already checks.
 */
import { describe, it, expect } from 'vitest'

import { judgeReading, judgeVerdictTime, type AutoNudgeLoop } from '../components/autoNudgeLoop'
import { fmtTimeNumeric } from '../i18n/format'

/** A loop with only the fields the readout reads. */
const loopOf = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop =>
  ({
    id: 'l1',
    slot_key: 'chat-1-1',
    message: 'patrol chat-2-2',
    idle_secs: 2700,
    max_cycles: 24,
    cycle_count: 3,
    active: true,
    last_fire_ts: 0,
    next_due_ts: 0,
    ...over,
  }) as AutoNudgeLoop

describe('judgeReading', () => {
  it('reports no judge for an ordinary timer loop', () => {
    expect(judgeReading(loopOf())).toEqual({ kind: 'none' })
  })

  it('reports no judge for an empty brief', () => {
    // `{}` is what the arm path stores when a judge is CLEARED, so reading it as
    // armed would draw a judge line on a plain timer.
    expect(judgeReading(loopOf({ judge: {} })).kind).toBe('none')
    expect(judgeReading(loopOf({ judge: { wake_when: '   ' } })).kind).toBe('none')
  })

  it('reports no judge for a null or undefined loop', () => {
    expect(judgeReading(null).kind).toBe('none')
    expect(judgeReading(undefined).kind).toBe('none')
  })

  it('carries the wake criterion for an armed loop', () => {
    const got = judgeReading(
      loopOf({ judge: { wake_when: 'a worker line starts with RULING', quiet_when: 'still WORKING' } }),
    )
    expect(got).toEqual({
      kind: 'armed',
      sense: 'wake',
      criterion: 'a worker line starts with RULING',
    })
  })

  it('reports the quiet SENSE when only the quiet criterion was given', () => {
    // The sense, not just the text. Both sentences reach the same field, and the
    // popover puts that field inside a label -- so a reading that does not say which
    // sentence it holds prints a quiet condition as a wake condition, which is the
    // exact inverse of what the owner armed.
    const got = judgeReading(loopOf({ judge: { quiet_when: 'checks are still running' } }))
    expect(got).toEqual({
      kind: 'armed',
      sense: 'quiet',
      criterion: 'checks are still running',
    })
  })

  it('distinguishes a judge that has not answered from one that answered quiet', () => {
    // The distinction the line depends on. Reading an unanswered judge as quiet
    // would tell the owner a tick was skipped that never happened.
    const fresh = judgeReading(loopOf({ judge: { wake_when: 'x' } }))
    expect(fresh.kind === 'armed' && fresh.verdict).toBeUndefined()

    const answered = judgeReading(
      loopOf({
        judge: { wake_when: 'x' },
        judge_last_verdict: { outcome: 'quiet', evidence_items: 3, at: 1_764_600_600 },
      }),
    )
    expect(answered.kind === 'armed' && answered.verdict).toEqual({
      outcome: 'quiet',
      items: 3,
      at: 1_764_600_600,
    })
  })

  it('keeps a zero-item answer, which is a real verdict on no evidence', () => {
    const got = judgeReading(
      loopOf({ judge: { wake_when: 'x' }, judge_last_verdict: { outcome: 'quiet', evidence_items: 0 } }),
    )
    expect(got.kind === 'armed' && got.verdict).toEqual({ outcome: 'quiet', items: 0, at: 0 })
  })

  it('treats a verdict with no outcome as no answer', () => {
    const got = judgeReading(
      loopOf({ judge: { wake_when: 'x' }, judge_last_verdict: { evidence_items: 4 } }),
    )
    expect(got.kind === 'armed' && got.verdict).toBeUndefined()
  })

  it('never surfaces a field the record does not carry', () => {
    // The record is text-free and carries no probability. A readout that invented
    // either would put a number on screen the gateway never measured.
    const got = judgeReading(
      loopOf({
        judge: { wake_when: 'x' },
        judge_last_verdict: { outcome: 'quiet', evidence_items: 2, at: 1 },
      }),
    )
    const verdict = got.kind === 'armed' ? got.verdict : undefined
    expect(Object.keys(verdict ?? {}).sort()).toEqual(['at', 'items', 'outcome'])
  })
})

describe('judgeVerdictTime', () => {
  /** Built with `Date.UTC` rather than a hand-computed epoch: a literal here is a
   *  second thing that can be wrong, and when it is, the test blames the helper. */
  const at = (h: number, m: number) => Date.UTC(2025, 11, 1, h, m, 0) / 1000

  it('renders with the same formatter as the last-fire line above it', () => {
    // Asserted against the formatter rather than a fixed string, because the
    // answer is the reader's own locale and zone. Two times in one block reading
    // on different clocks is the defect this closes; a literal expectation here
    // would only pass in whichever zone the test happened to run in.
    expect(judgeVerdictTime(at(12, 30))).toBe(fmtTimeNumeric(at(12, 30)))
    expect(judgeVerdictTime(at(9, 5))).toBe(fmtTimeNumeric(at(9, 5)))
  })

  it('does not spell a zone the reader has to already know', () => {
    expect(judgeVerdictTime(at(12, 30))).not.toContain('Z')
  })

  it('gives nothing for an absent time rather than the epoch', () => {
    expect(judgeVerdictTime(0)).toBe('')
    expect(judgeVerdictTime(-1)).toBe('')
  })
})
