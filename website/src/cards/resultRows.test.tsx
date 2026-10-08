/**
 * The finished rows of a chat, read by a novice: a recurring schedule always
 * says it recurs, and an applied card says what now exists instead of echoing
 * the gateway's imperative title.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import '../api/client'
import type { Card } from '../api/cards'
import { fmtCron } from '../utils/cronUtils'
import { fmtCardCron } from './scheduleText'
import { resultSummary } from './CardResultLine'
import { CardChanges } from './CardChanges'

const NOW = Date.parse('2026-10-03T15:05:00Z') // Sat Oct 3
const SLOT = 'slot-R'

const card = (over: Partial<Card> = {}): Card => ({
  id: 'c1',
  slot_key: SLOT,
  kind: 'setting.change',
  revision: 1,
  status: 'applied',
  risk: 'normal',
  title: 'Change Response verbosity',
  changes: [{ label: 'Response verbosity', before: 'standard', after: 'brief' }],
  editable: [],
  params: { path: 'chat.verbosity', value: 'brief' },
  plan: { apply: [], undo: null },
  result: { summary: 'Done: Change Response verbosity' },
  created_at: 1_790_000_000,
  expires_at: NOW + 3_600_000,
  ...over,
})

describe('a recurring schedule always states its recurrence', () => {
  it('adds the recurrence to every clock the Schedule page shows bare', () => {
    expect(fmtCardCron('0 9 * * *')).toBe('9:00 AM · Every day')
    expect(fmtCardCron('0 9 * * 0-6')).toBe('9:00 AM · Every day')
    expect(fmtCardCron('0 9 * * 1-5')).toBe('9:00 AM · Mon-Fri')
    expect(fmtCardCron('0 9 * * 1,3,5')).toBe('9:00 AM · Mon,Wed,Fri')
    expect(fmtCardCron('5 6 * * 1')).toBe('6:05 AM · Weekly on Mon')
    expect(fmtCardCron('0 9 1 * *')).toBe('9:00 AM · Monthly on day 1')
    expect(fmtCardCron('0 0 30 2 *')).toBe('12:00 AM · Every year on Feb 30')
    expect(fmtCardCron('15 6 * 7 *')).toBe('6:15 AM · Every day in Jul')
  })

  it('names intervals, which have no clock', () => {
    expect(fmtCardCron('*/15 * * * *')).toBe('Every 15 minutes')
    expect(fmtCardCron('* * * * *')).toBe('Every minute')
    expect(fmtCardCron('0 * * * *')).toBe('Every hour')
    expect(fmtCardCron('0 */2 * * *')).toBe('Every 2 hours')
  })

  it('keeps an expression neither humanizer knows exact', () => {
    expect(fmtCardCron('0 0 * * 1,2,4,6')).toBe('0 0 * * 1,2,4,6')
    expect(fmtCardCron('30 * * * *')).toBe('30 * * * *')
    expect(fmtCardCron('0 9 1,15 * *')).toBe('0 9 1,15 * *')
    // The Schedule page's own column is untouched.
    expect(fmtCron('0 9 * * *')).toBe('9:00 AM')
  })

  it('shows the Schedule row of a daily card as recurring', () => {
    render(<CardChanges changes={[{ field: 'cron_expr', label: 'When', after: '0 9 * * *' }]} />)
    expect(screen.getByTestId('change-card-value').textContent).toBe('9:00 AM · Every day')
  })
})

describe('an applied card says what now exists', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(NOW) })
  afterEach(() => { vi.useRealTimers() })

  it('a one-time reminder: set, and when it runs', () => {
    const reminder = card({
      kind: 'schedule.create', title: 'Create one-time reminder “Call the dentist”', once: true, timezone: 'UTC',
      next_run_at: Date.parse('2026-10-04T09:00:00Z') / 1000,
      params: { name: 'Call the dentist', message: 'Call', at: 'tomorrow 9am', timezone: 'UTC' },
    })
    expect(resultSummary(reminder)).toBe('Reminder set: Call the dentist · runs once, tomorrow, Oct 4 · 9:00 AM UTC')
  })

  it('a recurring schedule: created, and how often', () => {
    const sched = card({
      kind: 'schedule.create', title: 'Create schedule “Morning GitHub PR check”',
      params: { name: 'Morning GitHub PR check', message: 'Check PRs', cron_expr: '0 9 * * *' },
    })
    expect(resultSummary(sched)).toBe('Schedule created: Morning GitHub PR check · 9:00 AM · Every day')
  })

  it('a setting, a crewmate, and the generic fallback', () => {
    expect(resultSummary(card())).toBe('Setting changed: Response verbosity')
    expect(resultSummary(card({ kind: 'crewmate.create', title: 'Create crewmate “Scout”', params: { name: 'Scout', goal: 'g' } })))
      .toBe('Crewmate created: Scout')
    expect(resultSummary(card({ kind: 'mcp.install', title: 'Install MCP server “fetch”', params: { id: 'fetch' } })))
      .toBe('Applied: Install MCP server “fetch”')
  })

  it('leaves undone, cancelled and a partial summary as they were', () => {
    expect(resultSummary(card({ status: 'undone' }))).toBe('Restored: Change Response verbosity')
    expect(resultSummary(card({ status: 'cancelled' }))).toBe('Cancelled: Change Response verbosity')
    expect(resultSummary(card({ status: 'partial', result: { summary: 'Created crewmate “Scout”, but its schedule was not saved' } })))
      .toBe('Created crewmate “Scout”, but its schedule was not saved')
  })
})
