/**
 * The Ultra Mode Status view's pure helpers: the Overview's goal and "Done when"
 * lines, the milestone state rule, the Recap rule (the session summary only when
 * it is about goal progress, else the milestone sentence) and the List's tree depth.
 */
import { describe, it, expect } from 'vitest'
import type { CrewWorker } from '../../../types'
import type { WorkBoardItem } from '../../../api/crewBoard'
import type { SessionSummary } from '../../../types/sessionSummary'
import { CREW_BOARD_EXPANDED_KEY, crewAncestors, crewRowOpen, crewTree, doneWhen, goalScope, goalSentence, milestoneSentence, milestoneState, readCrewBoardExpanded, summaryRecap, visibleCrewRows, type CrewTreeNode } from './UltraCrewPanel'

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

  it('compares stamps as instants, so an older decision in another offset does not answer a newer report', () => {
    // 06:00-07:00 is 13:00Z, an hour AFTER the 12:00Z decision; as strings it sorts before.
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', decided_at: '2026-11-02T12:00:00+00:00', last_report_at: '2026-11-02T06:00:00-07:00' }), false)).toBe('needs_you')
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', decided_at: '2026-11-02T14:00:00+00:00', last_report_at: '2026-11-02T06:00:00-07:00' }), false)).toBe('waiting')
    // A decision stamp that does not parse answers nothing.
    expect(milestoneState(item({ status: 'blocked', reason: 'needs_human', decided_at: 'not a date', last_report_at: '2026-11-02T06:00:00Z' }), false)).toBe('needs_you')
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
  })

  it('quiet: the board stale flag on a chat that is still there, not stuck', () => {
    expect(milestoneState(item({ status: 'progress', stale: true, ...R }), false)).toBe('quiet')
    expect(milestoneState(item({ status: 'progress', stale: true, alive: 'closed' }), false)).toBe('stuck')
    // No report yet, even past the window and with no worker bound (the board
    // reads that as closed): Not started, never stuck.
    expect(milestoneState(item({ status: null, stale: true }), false)).toBe('next')
    expect(milestoneState(item({ status: null, stale: true, alive: 'closed' }), false)).toBe('next')
    expect(milestoneState(item({ status: 'blocked', ...ANSWERED }), false)).toBe('waiting')
    expect(milestoneState(item({ status: 'question', outstanding: false, ...ANSWERED }), false)).toBe('waiting')
  })

  it('stuck: a closed session on an open report, or an orphaned board', () => {
    expect(milestoneState(item({ status: 'progress', alive: 'closed' }), false)).toBe('stuck')
    expect(milestoneState(item({ status: 'blocked', alive: 'closed', ...R }), false)).toBe('stuck')
    expect(milestoneState(item({ status: 'progress', orphaned: true, ...R }), false)).toBe('stuck')
    // An orphaned board (conductor tab closed) flags every open item: a done
    // report or an unstarted item is not stuck.
    expect(milestoneState(item({ status: 'done', orphaned: true, ...R }), false)).toBe('done')
    expect(milestoneState(item({ status: null, orphaned: true }), false)).toBe('next')
    expect(milestoneState(item({ status: 'done', stale: true, ...R }), false)).toBe('done')
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
    expect(milestoneSentence(5, 6, [], [], ['Supplies list'])).toBe('5 of 6 done; Supplies list is stuck.')
    expect(milestoneSentence(4, 6, [], ['Poem'], ['Supplies list'], ['Drinks menu'])).toBe('4 of 6 done; Supplies list is stuck; Drinks menu has gone quiet; Poem is running.')
  })
})

describe('crewTree', () => {
  const root = 'root'
  const g = (w: CrewWorker, group: CrewWorker['group']): CrewWorker => ({ ...w, group })
  const flat = (nodes: CrewTreeNode[]) => visibleCrewRows(nodes, () => true).map(n => `${n.w.key}@${n.depth}`)

  it('indents a member under its lead only when the lead is in the same list', () => {
    expect(flat(crewTree([worker('lead', root), worker('child', 'lead')], root))).toEqual(['lead@0', 'child@1'])
    expect(flat(crewTree([worker('child', 'lead')], root))).toEqual(['child@0'])
  })

  it('keeps ONE tree: a finished lead keeps its unfinished crew under it, and the reverse', () => {
    const nodes = crewTree([g(worker('lead', root), 'done'), worker('kid', 'lead'), worker('sub', root), g(worker('old', 'sub'), 'done')], root)
    expect(flat(nodes)).toEqual(['lead@0', 'kid@1', 'sub@0', 'old@1'])
    expect(nodes.map(n => n.allDone)).toEqual([false, false])
    expect(crewTree([g(worker('a', root), 'done'), g(worker('b', 'a'), 'done')], root)[0]).toMatchObject({ allDone: true, size: 2 })
  })

  it('counts each subtree: children, waiting on an approval, running', () => {
    const [lead] = crewTree([
      worker('lead', root), worker('sub', 'lead'), g(worker('ask', 'sub'), 'approval'), worker('run', 'sub'), g(worker('wait', 'lead'), 'waiting'),
    ], root)
    expect(lead.kids.map(k => k.w.key)).toEqual(['sub', 'wait'])
    expect(lead).toMatchObject({ needsYou: 1, running: 2, size: 5 })
    expect(lead.kids[0]).toMatchObject({ needsYou: 1, running: 1, size: 3 })
  })

  it('is cycle safe', () => {
    expect(flat(crewTree([worker('a', 'b'), worker('b', 'a')], root))).toEqual(['a@0', 'b@1'])
  })

  it('hides the crew of a folded row, and opens top-level rows by default', () => {
    const nodes = crewTree([worker('lead', root), worker('sub', 'lead'), worker('deep', 'sub')], root)
    expect(visibleCrewRows(nodes, n => crewRowOpen({}, n)).map(n => n.w.key)).toEqual(['lead', 'sub'])
    expect(visibleCrewRows(nodes, n => crewRowOpen({ sub: true }, n)).map(n => n.w.key)).toEqual(['lead', 'sub', 'deep'])
    expect(visibleCrewRows(nodes, n => crewRowOpen({ lead: false, sub: true }, n)).map(n => n.w.key)).toEqual(['lead'])
    // A lead on the way to the member on screen defaults to open; a stored choice still wins.
    expect(crewRowOpen({}, nodes[0].kids[0], true)).toBe(true)
    expect(crewRowOpen({ sub: false }, nodes[0].kids[0], true)).toBe(false)
  })

  it('names the leads above a member', () => {
    const nodes = crewTree([worker('lead', root), worker('sub', 'lead'), worker('deep', 'sub'), worker('other', root)], root)
    expect([...crewAncestors(nodes, 'deep')]).toEqual(['lead', 'sub'])
    expect([...crewAncestors(nodes, 'nobody')]).toEqual([])
    expect([...crewAncestors(nodes, null)]).toEqual([])
  })
})

describe('readCrewBoardExpanded', () => {
  it('reads its own key and drops what it cannot use', () => {
    localStorage.setItem(CREW_BOARD_EXPANDED_KEY, JSON.stringify({ a: true, b: false, c: 'yes', '': true }))
    expect(readCrewBoardExpanded()).toEqual({ a: true, b: false })
    localStorage.setItem(CREW_BOARD_EXPANDED_KEY, '[1]')
    expect(readCrewBoardExpanded()).toEqual({})
    localStorage.setItem(CREW_BOARD_EXPANDED_KEY, '{oops')
    expect(readCrewBoardExpanded()).toEqual({})
    localStorage.removeItem(CREW_BOARD_EXPANDED_KEY)
    expect(readCrewBoardExpanded()).toEqual({})
    expect(CREW_BOARD_EXPANDED_KEY).not.toBe('mc-sidebar-conductor-expanded')
  })
})
