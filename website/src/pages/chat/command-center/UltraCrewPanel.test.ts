/**
 * The Ultra Mode Status view's pure helpers: the Overview's goal and "Done when"
 * lines, the milestone state rule, the Recap rule (the session summary only when
 * it is about goal progress, else the milestone sentence) and the List's tree depth.
 */
import { describe, it, expect } from 'vitest'
import type { CrewWorker } from '../../../types'
import type { WorkBoardItem } from '../../../api/crewBoard'
import type { SessionSummary } from '../../../types/sessionSummary'
import { depthWithin, doneWhen, goalScope, goalSentence, milestoneSentence, milestoneState, summaryRecap } from './UltraCrewPanel'

const item = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_1', title: 'Menu', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: null, summary: '', artifacts: {}, pr: null, last_report_at: null,
  created_at: '2026-10-30T00:00:00Z', closed_at: null, decided_at: null, orphaned: false, stale: false,
  acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle', events: [], ...over,
})

const worker = (key: string, by: string): CrewWorker => ({
  key, title: key, agent: 'kirocrew-worker', status: 'idle', group: 'working', last_activity: '', ledger_item: '',
  created_by: by, dispatched_by: by,
})

const summary = (line: string): SessionSummary => ({
  enabled: true, stale: false, constraints: [], generated_at: 1, user_turns: 3, last_activity: null,
  intents: [{ title: 'Launch', initial_intent: '', progress: [line], next_steps: [], ranges: [[1, 3]], status: 'active', verified: null, state: 'in-progress', last_touched_turn: 3, origin_turn: 1 }],
})

describe('Overview', () => {
  it('takes the first sentence of the goal and drops parentheticals', () => {
    expect(goalSentence('Plan a small tea shop launch (UI review demo). Then more.')).toBe('Plan a small tea shop launch.')
  })

  it('cuts a long goal on a word boundary, never mid-word', () => {
    const out = goalSentence('Plan a small tea shop launch for next month with a drinks menu and an opening poem and a price list', 40)
    expect(out.endsWith('…')).toBe(true)
    expect(out.slice(0, -1).split(' ').every(w => 'Plan a small tea shop launch for next month with'.split(' ').includes(w))).toBe(true)
  })

  it('reads a stated scope and nothing else', () => {
    expect(goalScope('Fix the login. Scope: backend only, no test deletions.')).toBe('backend only, no test deletions')
    expect(goalScope('Fix the login.')).toBe('')
  })

  it('merges one checkable acceptance kind into one sentence', () => {
    expect(doneWhen([item({ acceptance: { kind: 'pr_merged' } }), item({ acceptance: { kind: 'pr_merged' } })])).toBe('Done when all 2 PRs are merged.')
    expect(doneWhen([item({ acceptance: { kind: 'file' } }), item({})])).toBe('Done when 1 file check passes and you approve the rest.')
  })

  it('falls back to the approval sentence when nothing can be merged', () => {
    expect(doneWhen([item({})])).toBe('Done when you approve the result.')
    expect(doneWhen([item({ acceptance: { kind: 'file' } }), item({ acceptance: { kind: 'pr_checks' } })])).toBe('Done when you approve the result.')
    expect(doneWhen([])).toBe('Done when you approve the result.')
  })
})

describe('Done when, the rest', () => {
  it('drops "you approve the rest" once the other items are finished', () => {
    expect(doneWhen([item({ acceptance: { kind: 'file' } }), item({ state: 'accepted' })])).toBe('Done when 1 file check passes.')
  })
})

describe('Done when, file kinds', () => {
  it('names a file check without saying written, so an absent-file bar is not inverted', () => {
    const line = doneWhen([item({ acceptance: { kind: 'file', path: 'obsolete.txt', exists: false } as never })])
    expect(line).toBe('Done when 1 file check passes.')
  })
})

describe('milestoneState', () => {
  const R = { last_report_at: '2026-10-30T01:00:00Z' }
  const ANSWERED = { decided_at: '2026-10-30T01:05:00Z', last_report_at: '2026-10-30T01:00:00Z' }

  it('needs you: a pending approval, even above an earlier done report', () => {
    expect(milestoneState(item({ status: 'done' }), true)).toBe('needs_you')
  })

  it('needs you: a blocked or question report whose reason names a person', () => {
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', ...R }), false)).toBe('needs_you')
    expect(milestoneState(item({ status: 'question', reason: 'approval', outstanding: true, ...R }), false)).toBe('needs_you')
    // Once the conductor answered, it is no longer on the person.
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', ...ANSWERED }), false)).toBe('waiting')
  })

  it('needs you ends once the person answered through the composer (a steer)', () => {
    const report = { id: 'e1', ts: '2026-10-30T01:00:00Z', item_id: 'it_1', kind: 'report' as const, status: 'blocked' as const, text: '' }
    const steer = { id: 'e2', ts: '2026-10-30T01:02:00Z', item_id: 'it_1', kind: 'steer' as const, status: null, text: '' }
    // A question: the board's `outstanding` already counts the steer.
    expect(milestoneState(item({ status: 'question', reason: 'needs_human', outstanding: false, ...R }), false)).toBe('waiting')
    expect(milestoneState(item({ status: 'question', reason: 'needs_human', outstanding: false, alive: 'running', ...R }), false)).toBe('running')
    // A blocked report: a steer after the newest report answers it.
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', events: [report, steer], ...R }), false)).toBe('waiting')
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', events: [steer, { ...report, ts: '2026-10-30T01:03:00Z' }], ...R }), false)).toBe('needs_you')
  })

  it('waiting: a question or blocked report the conductor still owns is not needs you', () => {
    expect(milestoneState(item({ status: 'question', outstanding: true, ...R }), false)).toBe('waiting')
    expect(milestoneState(item({ status: 'blocked', ...R }), false)).toBe('waiting')
  })

  it('waiting: an idle session after progress or an answered report', () => {
    expect(milestoneState(item({ status: 'progress', ...R }), false)).toBe('waiting')
    expect(milestoneState(item({ status: 'blocked', ...ANSWERED }), false)).toBe('waiting')
    expect(milestoneState(item({ status: 'question', outstanding: false, ...ANSWERED }), false)).toBe('waiting')
  })

  it('stalled: a closed session on an open report, or the board stale / orphaned flag', () => {
    expect(milestoneState(item({ status: 'progress', alive: 'closed' }), false)).toBe('stalled')
    expect(milestoneState(item({ status: 'blocked', alive: 'closed', ...R }), false)).toBe('stalled')
    expect(milestoneState(item({ status: 'progress', stale: true, ...R }), false)).toBe('stalled')
    expect(milestoneState(item({ status: 'progress', orphaned: true, ...R }), false)).toBe('stalled')
  })

  it('running: a live turn, even after a done report or an answered one', () => {
    expect(milestoneState(item({ status: 'done', alive: 'running' }), false)).toBe('running')
    expect(milestoneState(item({ status: 'progress', alive: 'running' }), false)).toBe('running')
    expect(milestoneState(item({ status: 'blocked', alive: 'running', ...ANSWERED }), false)).toBe('running')
  })

  it('done and next: as before', () => {
    expect(milestoneState(item({ state: 'accepted' }), false)).toBe('done')
    expect(milestoneState(item({ status: 'done' }), false)).toBe('done')
    expect(milestoneState(item({ status: 'question', outstanding: false, terminal: true, state: 'abandoned' }), false)).toBe('done')
    expect(milestoneState(item({ status: null }), false)).toBe('next')
    expect(milestoneState(item({ status: null, alive: 'closed' }), false)).toBe('next')
  })
})

describe('Recap', () => {
  const titles = ['Supplies list', 'Drinks menu']

  it('uses the summary line when it is about a milestone', () => {
    expect(summaryRecap(summary('The drinks menu is ready and the supplies list waits for a save.'), titles)).toBe('The drinks menu is ready and the supplies list waits for a save.')
  })

  it('rejects bookkeeping and lines that name no milestone', () => {
    expect(summaryRecap(summary('Tasks used: 8 of 20. The open-task limit was raised.'), titles)).toBe('')
    expect(summaryRecap(summary('Waiting for the person.'), titles)).toBe('')
    // A stale summary may name a state the milestones have moved past.
    expect(summaryRecap({ ...summary('The drinks menu is ready and the supplies list waits for a save.'), stale: true }, titles)).toBe('')
    expect(summaryRecap({ ...summary('The drinks menu is ready.'), enabled: false }, titles)).toBe('')
  })

  it('builds the milestone sentence', () => {
    expect(milestoneSentence(4, 8, ['Disk check', 'Decor team'], ['Launch-day checklist'])).toBe('4 of 8 done; Disk check and Decor team wait on you; Launch-day checklist is running.')
    expect(milestoneSentence(5, 6, [], [], ['Supplies list'])).toBe('5 of 6 done; Supplies list has stalled.')
  })
})

describe('depthWithin', () => {
  it('indents a member under its lead only when the lead is in the same list', () => {
    const root = 'root'
    const lead = worker('lead', root)
    const child = worker('child', 'lead')
    expect(depthWithin([lead, child], root).get('child')).toBe(1)
    expect(depthWithin([child], root).get('child')).toBe(0)
  })
})
