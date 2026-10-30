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
  it('puts waiting on the person above an earlier done report', () => {
    expect(milestoneState(item({ status: 'done' }), true)).toBe('blocked')
  })

  it('reads a session working again as running', () => {
    expect(milestoneState(item({ status: 'done', alive: 'running' }), false)).toBe('running')
  })

  it('maps the other states', () => {
    expect(milestoneState(item({ state: 'accepted' }), false)).toBe('done')
    expect(milestoneState(item({ status: 'done' }), false)).toBe('done')
    expect(milestoneState(item({ status: 'question', outstanding: true }), false)).toBe('blocked')
    expect(milestoneState(item({ status: 'blocked', last_report_at: '2026-10-30T01:00:00Z' }), false)).toBe('blocked')
    expect(milestoneState(item({ status: null, alive: 'closed' }), false)).toBe('next')
  })

  it('stops waiting on the person once the conductor answered', () => {
    expect(milestoneState(item({ status: 'question', outstanding: false, decided_at: '2026-10-30T01:05:00Z', last_report_at: '2026-10-30T01:00:00Z', alive: 'running' }), false)).toBe('running')
    expect(milestoneState(item({ status: 'blocked', decided_at: '2026-10-30T01:05:00Z', last_report_at: '2026-10-30T01:00:00Z' }), false)).toBe('running')
  })

  it('does not read a report left by a closed session as running', () => {
    expect(milestoneState(item({ status: 'progress', alive: 'closed' }), false)).toBe('blocked')
  })

  it('reads a closed item as finished whatever its last report said', () => {
    expect(milestoneState(item({ status: 'question', outstanding: false, terminal: true, state: 'abandoned' }), false)).toBe('done')
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
    expect(milestoneSentence(5, 6, [], [], ['Supplies list'])).toBe('5 of 6 done; Supplies list is blocked.')
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
