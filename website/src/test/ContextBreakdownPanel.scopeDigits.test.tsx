/**
 * The trend heading counts the turns the chart draws ("Last 12 turns"), and the
 * fold line counts the session-start turn listed above it. Both numbers sit
 * beside the chart's own axis labels, which `fmtNumber` writes in the active
 * language's digits. A raw `{{count}}` writes Latin digits whatever the
 * language, so under Bengali the heading read "12" next to axis labels in "১২".
 */

import { describe, it, expect, afterAll } from 'vitest'
import { act, render, screen } from '@testing-library/react'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'
import { fmtNumber } from '../i18n/format'
import { ContextBreakdownPanel, type ContextTrace, type TrendRow } from '../pages/ContextBreakdownPanel'
import type { LatestTurn } from '../pages/ContextEngineBreakdown'

const latestOf = (over: Partial<LatestTurn> = {}): LatestTurn => ({
  turn: 6,
  phase: 'per_turn',
  ours: { message: 80, memory: 1200, rules: 500, skills: 240, other: 140 },
  loop_chars: 6000,
  loop_estimated: true,
  engine: null,
  ttft_ms: null,
  context_used: 2000,
  context_window: 200000,
  ...over,
})

const rowOf = (turn: number): TrendRow => {
  const ours = { message: 80, memory: 100 * turn, rules: 500, skills: 240, other: 140 }
  return {
    turn,
    ours,
    loop: 1000,
    detail: latestOf({ turn, ours, context_used: 1000 * turn }),
  }
}

const traceOf = (over: Partial<ContextTrace>): ContextTrace => ({
  slot: '578c537a',
  turns: [],
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
  ...over,
})

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('ContextBreakdownPanel — turn counts use the active language digits', () => {
  it('writes the heading and the fold count in Bengali digits under Bengali', async () => {
    await act(async () => {
      await i18next.changeLanguage('bn')
    })
    const trend = Array.from({ length: 12 }, (_, i) => rowOf(i + 3))
    render(
      <ContextBreakdownPanel
        trace={traceOf({ latest: latestOf({ turn: 14 }), trend, start: rowOf(1), earlier: 2, turn_count: 14 })}
        chartWidth={600}
      />,
    )
    const heading = screen.getByTestId('trend').textContent ?? ''
    expect(heading).toContain(fmtNumber(12))
    expect(heading).not.toMatch(/\b12\b/)
    const earlier = screen.getByTestId('earlier-turns').textContent ?? ''
    expect(earlier).toContain(`${fmtNumber(1)}টি`)
    expect(earlier).not.toMatch(/\b1টি/)
  })
})
