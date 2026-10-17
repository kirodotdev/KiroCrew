/**
 * The compaction card's shadow line, and the reader behind it.
 *
 * Two things this file is about, because either one alone would let a wrong number
 * onto the screen: the reader refuses a record whose parts do not add up, and the
 * line draws on every shape the compaction card takes (a success notice, a recycle
 * notice, and the ⚠-led failure that renders through ErrorNotice) — the scoring runs
 * before the compaction picks an arm, so all three are reachable with a record.
 */
import { render, screen } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it } from 'vitest'

import { queryClient } from '../api/queryClient'
import CompactionCard from '../pages/chat/CompactionCard'
import { __resetVerdicts, readCompactionKeepRecord } from '../pages/chat/decisionRecord'

const RECORD = {
  turn_id: 'abc123',
  point: 'compaction.keep',
  total_calls: 61,
  pinned_calls: 7,
  kept_both: 14,
  kept_call: 9,
  dropped: 38,
  chars_all: 1000,
  chars_today_eligible: 100,
  chars_jev_eligible: 410,
  requests: 3,
  fitting_stage: 'inputs_200',
}

const AUTO_COMPACTED = '\u{1F504} Auto-compacted at 85%.'
const RECYCLED = '\u267B\uFE0F Compaction didn\u2019t succeed at 91%, so the session was restarted.'
const FAILED = '\u26A0 Auto-compact failed at 88% \u2014 will retry after cooldown.'
const COMPLETED = '\u2705 Conversation compacted: Goal / Status / Decisions'

function draw(content: string, keepRecord?: unknown) {
  return render(
    <QueryClientProvider client={queryClient}>
      <CompactionCard content={content} keepRecord={keepRecord} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  __resetVerdicts()
})

describe('readCompactionKeepRecord', () => {
  it('reads a complete record and derives the kept count', () => {
    const read = readCompactionKeepRecord(RECORD)
    expect(read).toMatchObject({ turnId: 'abc123', totalCalls: 61, keptCalls: 23 })
    expect(read?.charsShare).toBeCloseTo(0.41)
  })

  it('refuses another point\u2019s record rather than guessing at the older shape', () => {
    expect(readCompactionKeepRecord({ ...RECORD, point: 'skills.select' })).toBeNull()
    expect(readCompactionKeepRecord({ ...RECORD, point: undefined })).toBeNull()
  })

  it('refuses a record with no turn id, because the thumbs post one', () => {
    expect(readCompactionKeepRecord({ ...RECORD, turn_id: '' })).toBeNull()
  })

  it('refuses tallies that do not add up to the total', () => {
    // The line prints "N of M". A record whose parts disagree with M is a number
    // nobody could reconcile with the log, so it draws nothing at all.
    expect(readCompactionKeepRecord({ ...RECORD, dropped: 2 })).toBeNull()
  })

  it('refuses a zero-call record rather than printing "0 of 0"', () => {
    expect(
      readCompactionKeepRecord({ ...RECORD, total_calls: 0, kept_both: 0, kept_call: 0, dropped: 0 }),
    ).toBeNull()
  })

  it('carries the overflow count rather than refusing a truncated record', () => {
    // The tallies describe a PREFIX of the session, which the line states; hiding the
    // record would hide a measurement that is accurate about everything it covers.
    expect(readCompactionKeepRecord({ ...RECORD, calls_truncated: 140 })?.truncatedCalls).toBe(140)
  })

  it('reads an absent overflow count as none, so an older record adds nothing', () => {
    expect(readCompactionKeepRecord(RECORD)?.truncatedCalls).toBe(0)
    expect(readCompactionKeepRecord({ ...RECORD, calls_truncated: -3 })?.truncatedCalls).toBe(0)
  })

  it('prints no share when the numerator exceeds the denominator', () => {
    // Both arms are measured on one character universe, so a share above 100% means
    // they were measured against different things. `null` rather than a clamp to 1:
    // clamping prints a plausible number over a broken measurement.
    const read = readCompactionKeepRecord({ ...RECORD, chars_jev_eligible: 2_000_000 })
    expect(read).not.toBeNull()
    expect(read?.charsShare).toBeNull()
  })

  it('states the share as a BOUND, because the eligible arm is one', () => {
    // The gateway counts conversation rows by ROLE; the replay then applies quotas and
    // a character budget, so the number is an upper bound and the copy has to say so.
    draw(AUTO_COMPACTED, RECORD)
    expect(screen.getByTestId('compaction-keep-line').textContent).toContain('up to')
  })

  it('accepts a share of exactly 100%', () => {
    // The boundary is inclusive: a keep-set that kept everything is a real answer.
    const read = readCompactionKeepRecord({ ...RECORD, chars_jev_eligible: RECORD.chars_all })
    expect(read?.charsShare).toBe(1)
  })

  it('reads a missing chars pair as "did not say" rather than as zero', () => {
    const read = readCompactionKeepRecord({ ...RECORD, chars_jev_eligible: undefined })
    expect(read).not.toBeNull()
    expect(read?.charsShare).toBeNull()
  })

  it('draws nothing for a non-object', () => {
    for (const raw of [null, undefined, 7, 'x', []]) {
      expect(readCompactionKeepRecord(raw)).toBeNull()
    }
  })
})

describe('CompactionCard with a shadow record', () => {
  it('says what Jev WOULD have kept, not what was kept', () => {
    draw(AUTO_COMPACTED, RECORD)
    const line = screen.getByTestId('compaction-keep-line')
    expect(line.textContent).toContain('would keep')
    expect(line.textContent).toContain('23')
    expect(line.textContent).toContain('61')
  })

  it('prints the character share beside it', () => {
    draw(AUTO_COMPACTED, RECORD)
    expect(screen.getByTestId('compaction-keep-line').textContent).toMatch(/41\s*%/)
  })

  it('omits the share when the record does not state both sides', () => {
    draw(AUTO_COMPACTED, { ...RECORD, chars_all: 0 })
    const text = screen.getByTestId('compaction-keep-line').textContent ?? ''
    expect(text).toContain('23')
    expect(text).not.toContain('%')
  })

  it('states the overflow beside the count when the walk was truncated', () => {
    draw(AUTO_COMPACTED, { ...RECORD, calls_truncated: 140 })
    const text = screen.getByTestId('compaction-keep-line').textContent ?? ''
    expect(text).toContain('140')
    expect(text).toContain('not scored')
    // Inside the COUNT's clause, before the character share: the overflow qualifies
    // the count and not the share, which is measured over what the arms did cover.
    expect(text.indexOf('not scored')).toBeLessThan(text.indexOf('%'))
  })

  it('carries the shared jev thumbs, keyed to the record\u2019s turn', () => {
    draw(AUTO_COMPACTED, RECORD)
    expect(screen.getByTestId('compaction-keep-right-jev')).toBeInTheDocument()
    expect(screen.getByTestId('compaction-keep-wrong-jev')).toBeInTheDocument()
  })

  it('draws on the recycle notice too', () => {
    draw(RECYCLED, RECORD)
    expect(screen.getByTestId('compaction-keep-line')).toBeInTheDocument()
  })

  it('draws on the failure shape too, which renders through ErrorNotice', () => {
    // The scoring starts before the compaction picks an arm, so a record outlives a
    // compaction that then failed. A line placed inside one branch would lose it here.
    draw(FAILED, RECORD)
    expect(screen.getByTestId('compaction-card-error')).toBeInTheDocument()
    expect(screen.getByTestId('compaction-keep-line')).toBeInTheDocument()
  })

  it('draws on the folded summary card too, and keeps the fold', () => {
    draw(COMPLETED, RECORD)
    expect(screen.getByTestId('compaction-card')).toBeInTheDocument()
    expect(screen.getByTestId('compaction-card-toggle')).toBeInTheDocument()
    expect(screen.getByTestId('compaction-keep-line')).toBeInTheDocument()
  })
})

describe('CompactionCard without a record', () => {
  it('is unchanged when no record rides the row', () => {
    draw(AUTO_COMPACTED)
    expect(screen.queryByTestId('compaction-keep-line')).toBeNull()
    expect(screen.queryByTestId('compaction-card-host')).toBeNull()
  })

  it('is unchanged when the record fails validation', () => {
    draw(AUTO_COMPACTED, { ...RECORD, kept_both: 999 })
    expect(screen.queryByTestId('compaction-keep-line')).toBeNull()
  })

  it('says nothing about an overflow when there was none', () => {
    // A permanent "(+0 not scored)" is furniture, and furniture is what makes the
    // qualifier unreadable where it matters.
    draw(AUTO_COMPACTED, RECORD)
    expect(screen.getByTestId('compaction-keep-line').textContent).not.toContain('not scored')
  })

  it('draws the count but no share when the two arms disagree', () => {
    draw(AUTO_COMPACTED, { ...RECORD, chars_jev_eligible: 9_000_000 })
    const text = screen.getByTestId('compaction-keep-line').textContent ?? ''
    expect(text).toContain('23')
    expect(text).not.toContain('%')
  })

  it('is unchanged when another point\u2019s record rides the row', () => {
    draw(AUTO_COMPACTED, { turn_id: 't', point: 'message.steer', choice: 'steer' })
    expect(screen.queryByTestId('compaction-keep-line')).toBeNull()
  })
})
