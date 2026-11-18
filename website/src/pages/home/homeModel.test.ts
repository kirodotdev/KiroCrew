import { describe, expect, it } from 'vitest'
import type { RunNode } from '../chat/command-center/model'
import type { CronJob } from '../../types'
import type { MemberRosterRow } from '../../api/client/agents'
import type { AttentionItem } from '../chat/command-center/model'
import { activeCounts, botCounts, fleetRowExtras, orderFleet, scheduleSummary, spendByModel, splitAttention, upcomingJobs } from './homeModel'

const node = (id: string, kind: RunNode['kind'], state: RunNode['state']): RunNode =>
  ({ id, kind, state, ref: id, slot: 's', title: id, ordinal: 1 })

const job = (over: Partial<CronJob>): CronJob =>
  ({ id: 'j', name: 'Job', message: '', enabled: true, schedule: 'daily', last_status: 'ok', ...over })

describe('activeCounts', () => {
  it('counts only running nodes, by kind', () => {
    const counts = activeCounts([
      node('a', 'session', 'running'), node('b', 'session', 'idle'), node('c', 'session', 'needs_input'),
      node('d', 'subagent', 'running'), node('e', 'subagent', 'done'),
      node('f', 'workflow', 'running'), node('g', 'workflow', 'waiting'),
    ])
    expect(counts).toEqual({ sessions: 1, subagents: 1, workflows: 1, total: 3 })
  })
  it('is all zeros for an empty fleet', () => {
    expect(activeCounts([])).toEqual({ sessions: 0, subagents: 0, workflows: 0, total: 0 })
  })
})

describe('orderFleet', () => {
  it('lifts what needs a person first, then blocked, running and waiting; rests the remainder', () => {
    const { active, resting } = orderFleet([
      node('idle', 'session', 'idle'), node('run', 'session', 'running'), node('ask', 'session', 'needs_input'),
      node('done', 'subagent', 'done'), node('wait', 'subagent', 'waiting'), node('block', 'subagent', 'blocked'),
      node('stop', 'workflow', 'stopped'),
    ])
    expect(active.map(n => n.id)).toEqual(['ask', 'block', 'run', 'wait'])
    expect(resting.map(n => n.id)).toEqual(['idle', 'done', 'stop'])
  })
  it('rests a paused workflow, as the command center does', () => {
    const { active, resting } = orderFleet([node('wf', 'workflow', 'waiting'), node('sub', 'subagent', 'waiting')])
    expect(active.map(n => n.id)).toEqual(['sub'])
    expect(resting.map(n => n.id)).toEqual(['wf'])
  })
  it('lifts a session with an inbox item to needs_input, and only a session', () => {
    const { active } = orderFleet([node('s', 'session', 'idle'), node('w', 'subagent', 'idle')], new Set(['s']))
    expect(active.map(n => [n.id, n.state])).toEqual([['s', 'needs_input']])
  })
  it('keeps the model order within one state (a stable sort)', () => {
    const { active } = orderFleet([node('r1', 'session', 'running'), node('r2', 'subagent', 'running'), node('r3', 'workflow', 'running')])
    expect(active.map(n => n.id)).toEqual(['r1', 'r2', 'r3'])
  })
})

describe('scheduleSummary and upcomingJobs', () => {
  const jobs = [
    job({ id: 'late', name: 'Late', next_run_ts: 3_000 }),
    job({ id: 'paused', name: 'Paused', enabled: false, next_run_ts: 1 }),
    job({ id: 'soon', name: 'Soon', next_run_ts: 1_000, last_status: 'error' }),
    job({ id: 'oneshot', name: 'Fired', next_run_ts: null, is_running: true }),
  ]
  it('counts enabled, paused, failing and running jobs and names the soonest forecast run', () => {
    expect(scheduleSummary(jobs)).toEqual({ enabled: 3, paused: 1, failing: 1, running: 1, next: jobs[2] })
  })
  it('lists enabled jobs with a forecast soonest first, up to the limit', () => {
    expect(upcomingJobs(jobs, 5).map(j => j.id)).toEqual(['soon', 'late'])
    expect(upcomingJobs(jobs, 1).map(j => j.id)).toEqual(['soon'])
  })
  it('has no next job when nothing enabled carries a forecast', () => {
    expect(scheduleSummary([job({ next_run_ts: null }), job({ enabled: false, next_run_ts: 5 })]).next).toBeNull()
  })
})

describe('botCounts', () => {
  const row = (name: string, running: boolean): MemberRosterRow => ({ name, slug: name, slot_key: `member-${name}`, running })
  it('counts the crewmates mid-turn over the roster', () => {
    expect(botCounts([row('a', true), row('b', false), row('c', true)])).toEqual({ running: 2, total: 3 })
    expect(botCounts([])).toEqual({ running: 0, total: 0 })
  })
})

describe('splitAttention', () => {
  const ask = (slot: string, followUp?: boolean): AttentionItem =>
    ({ id: `q:${slot}`, slot, kind: 'question', question: { slot, followUp, questions: [{ question: 'Q', options: [{ label: 'A' }, { label: 'B' }] }] } })
  it('keeps questions, approvals and needs-input sessions as blockers and folds follow-up offers away', () => {
    const split = splitAttention([
      ask('s1'), ask('s2', true), { id: 'ap', slot: 's3', kind: 'approval' }, { id: 'open', slot: 's4', kind: 'open_session' }, ask('s5', true),
    ])
    expect(split.blockers.map(i => i.slot)).toEqual(['s1', 's3', 's4'])
    expect(split.suggestions.map(i => i.slot)).toEqual(['s2', 's5'])
    expect(split.questions).toBe(2)
    expect(split.approvals).toBe(1)
  })
})

describe('fleetRowExtras', () => {
  it('reads the run start from last_turn_ts only while the session runs', () => {
    const slot = { key: 's', messages: 1, running: true, last_turn_ts: '2026-11-17T12:00:00Z', last_message: '  Writing tests  ', subagents_running: true }
    expect(fleetRowExtras(slot)).toEqual({ runningSinceMs: Date.parse('2026-11-17T12:00:00Z'), lastMessage: 'Writing tests', subagentsRunning: true })
    expect(fleetRowExtras({ ...slot, running: false })).toEqual({ runningSinceMs: undefined, lastMessage: 'Writing tests', subagentsRunning: true })
    expect(fleetRowExtras(undefined)).toEqual({ runningSinceMs: undefined, lastMessage: undefined, subagentsRunning: false })
  })
})

describe('spendByModel', () => {
  const bucket = (costUsd: number, tokens: number) => ({ input: tokens, output: 0, cacheCreate: 0, cacheRead: 0, costUsd })
  const models = { a: bucket(4, 10), b: bucket(1, 50), c: bucket(2, 20), d: bucket(0, 0), e: bucket(0.5, 5), f: bucket(0.25, 1) }
  it('measures cost on a dollar-billed harness, largest first, dropping empty models', () => {
    expect(spendByModel(models, true, 10, 'other').map(r => [r.model, r.amount])).toEqual([['a', 4], ['c', 2], ['b', 1], ['e', 0.5], ['f', 0.25]])
  })
  it('measures tokens otherwise', () => {
    expect(spendByModel(models, false, 10, 'other').map(r => r.model)).toEqual(['b', 'c', 'a', 'e', 'f'])
  })
  it('folds the tail into one other row past the limit', () => {
    expect(spendByModel(models, true, 3, 'other')).toEqual([{ model: 'a', amount: 4 }, { model: 'c', amount: 2 }, { model: 'other', amount: 1.75 }])
    expect(spendByModel(undefined, true, 3, 'other')).toEqual([])
  })
})
