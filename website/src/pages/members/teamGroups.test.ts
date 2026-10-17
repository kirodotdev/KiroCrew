import { describe, expect, it } from 'vitest'
import type { CrewTeam, MemberRosterRow } from '../../api/client'
import type { ChatMessage } from '../../types'
import {
  NO_TEAM_ID,
  groupRosterByTeam,
  newestQuestionFirst,
  parseCollapsedTeams,
  serializeCollapsedTeams,
  teamMemberState,
  teamOfMember,
  unansweredQuestion,
  type OpenQuestion,
} from './teamGroups'

const row = (name: string): MemberRosterRow => ({ name, slug: name.toLowerCase(), slot_key: '', running: false })
const team = (id: string, name: string, members: string[]): CrewTeam => ({ id, name, members })
const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role,
  content,
  cls: role,
  ...extra,
})

describe('groupRosterByTeam', () => {
  it('with no teams is one anonymous group holding the whole roster, in roster order', () => {
    const rows = [row('Radar'), row('Fixer')]
    const groups = groupRosterByTeam([], rows)
    expect(groups).toHaveLength(1)
    expect(groups[0].team).toBeNull()
    expect(groups[0].id).toBe(NO_TEAM_ID)
    expect(groups[0].members.map((m) => m.name)).toEqual(['Radar', 'Fixer'])
  })

  it('groups in the teams\' stored order and keeps the ROSTER\'s order inside a group', () => {
    // The roster is sorted Scout, Radar, Fixer, Scribe; the team lists Radar before Scout.
    const rows = [row('Scout'), row('Radar'), row('Fixer'), row('Scribe')]
    const teams = [team('t1', 'Issue triage', ['Radar', 'Scout']), team('t2', 'Docs', ['Scribe'])]
    const groups = groupRosterByTeam(teams, rows)
    expect(groups.map((g) => g.id)).toEqual(['t1', 't2', NO_TEAM_ID])
    expect(groups[0].members.map((m) => m.name)).toEqual(['Scout', 'Radar'])
    expect(groups[1].members.map((m) => m.name)).toEqual(['Scribe'])
    expect(groups[2].members.map((m) => m.name)).toEqual(['Fixer'])
  })

  it('drops a team whose rows are all filtered out, and the No-team group when nothing is loose', () => {
    // Only Radar survived the roster filter; Docs has no visible row but
    // Scribe IS on the roster -- hidden by the filter, not gone.
    const rows = [row('Radar')]
    const all = [row('Radar'), row('Scribe')]
    const teams = [team('t1', 'Issue triage', ['Radar']), team('t2', 'Docs', ['Scribe'])]
    const groups = groupRosterByTeam(teams, rows, all)
    expect(groups.map((g) => g.id)).toEqual(['t1'])
  })

  it('keeps a genuinely empty team as a header with no rows, filtered or not', () => {
    // Release lists nobody; Docs lists only a deleted crewmate. Both are still
    // the user's teams and the header is the way back to their Edit dialog.
    const all = [row('Radar')]
    const teams = [team('t1', 'Release', []), team('t2', 'Docs', ['Ghost']), team('t3', 'Triage', ['Radar'])]
    expect(groupRosterByTeam(teams, all, all).map((g) => [g.id, g.members.length])).toEqual([
      ['t1', 0],
      ['t2', 0],
      ['t3', 1],
    ])
    // Under a filter that hides Radar, Triage is dropped (its crewmate exists)
    // while the empty teams stay: they have nothing a filter could be hiding.
    expect(groupRosterByTeam(teams, [], all).map((g) => g.id)).toEqual(['t1', 't2'])
  })

  it('ignores a listed name with no roster row and resolves a doubled name to the first team', () => {
    const rows = [row('Radar'), row('Fixer')]
    const teams = [team('t1', 'A', ['Radar', 'Ghost']), team('t2', 'B', ['Radar', 'Fixer'])]
    const groups = groupRosterByTeam(teams, rows)
    expect(groups.map((g) => [g.id, g.members.map((m) => m.name)])).toEqual([
      ['t1', ['Radar']],
      ['t2', ['Fixer']],
    ])
  })

  it('teamOfMember finds the listing team', () => {
    const teams = [team('t1', 'A', ['Radar'])]
    expect(teamOfMember(teams, 'Radar')?.id).toBe('t1')
    expect(teamOfMember(teams, 'Fixer')).toBeUndefined()
  })
})

describe('collapsed-team persistence', () => {
  it('round-trips a set and reads junk as nothing collapsed', () => {
    const set = new Set(['t1', NO_TEAM_ID])
    expect(parseCollapsedTeams(serializeCollapsedTeams(set))).toEqual(set)
    expect(parseCollapsedTeams(null)).toEqual(new Set())
    expect(parseCollapsedTeams('not json')).toEqual(new Set())
    expect(parseCollapsedTeams('{"a":1}')).toEqual(new Set())
    expect(parseCollapsedTeams('["t1", 3, null]')).toEqual(new Set(['t1']))
  })
})

describe('unansweredQuestion', () => {
  it('finds a trailing assistant question, stripping the option marker into chips', () => {
    const q = unansweredQuestion([
      msg('user', 'triage #12493'),
      msg('assistant', 'Looks like a duplicate of #12102. Close it?\n[OPTIONS: Close as duplicate | Keep it open]', { ts: '2026-09-22T10:00:00Z' }),
    ])
    expect(q).toEqual({
      text: 'Looks like a duplicate of #12102. Close it?',
      options: ['Close as duplicate', 'Keep it open'],
      ts: '2026-09-22T10:00:00Z',
    })
  })

  it('a bare question mark is a question too, with no chips', () => {
    const q = unansweredQuestion([msg('assistant', 'Should I keep digging for the real race?')])
    expect(q?.options).toEqual([])
    expect(q?.text).toBe('Should I keep digging for the real race?')
  })

  it('a later user reply answers it', () => {
    expect(
      unansweredQuestion([
        msg('assistant', 'Close it?\n[OPTIONS: Yes | No]'),
        msg('user', 'Yes'),
      ]),
    ).toBeNull()
  })

  it('a trailing statement is not a question', () => {
    expect(unansweredQuestion([msg('user', 'go'), msg('assistant', 'Done. PR #12470 is open.')])).toBeNull()
  })

  it('skips tool rows and a trailing Stop press when looking for the tail', () => {
    const q = unansweredQuestion([
      msg('assistant', 'Merge it as is?'),
      msg('tool', 'some tool output'),
      msg('system', '', { kind: 'stop_event' }),
    ])
    expect(q?.text).toBe('Merge it as is?')
  })

  it('a trailing system notice in the assistant role is not the crewmate\'s last word', () => {
    // The backend appends compaction / reload notices as assistant rows. Read
    // as the tail, "Conversation compacted." has no options and no question
    // mark, so the open question before it would vanish from "Needs you".
    const q = unansweredQuestion([
      msg('user', 'triage #12493'),
      msg('assistant', 'Close it?\n[OPTIONS: Close as duplicate | Keep it open]'),
      msg('assistant', 'Conversation compacted.', { kind: 'compaction' }),
      msg('assistant', 'Session reloaded.', { meta: { kind: 'session_reload' } }),
    ])
    expect(q?.text).toBe('Close it?')
    expect(q?.options).toEqual(['Close as duplicate', 'Keep it open'])
    // And a notice after the user's answer does not resurrect the question.
    expect(unansweredQuestion([msg('assistant', 'Close it?'), msg('user', 'Yes'), msg('assistant', 'Conversation compacted.', { kind: 'compaction' })])).toBeNull()
  })

  it('an empty chat has nothing waiting', () => {
    expect(unansweredQuestion([])).toBeNull()
  })
})

describe('teamMemberState', () => {
  it('ranks running over waiting over paused over idle', () => {
    expect(teamMemberState({ running: true, waiting: true, stopped: true })).toBe('running')
    expect(teamMemberState({ running: false, waiting: true, stopped: true })).toBe('waiting')
    expect(teamMemberState({ running: false, waiting: false, stopped: true })).toBe('paused')
    expect(teamMemberState({ running: false, waiting: false, stopped: false })).toBe('idle')
  })
})

describe('newestQuestionFirst', () => {
  const q = (ts: string): { question: OpenQuestion } => ({ question: { text: 'q', options: [], ts } })
  const none = { question: null }

  it('orders newest first and sinks rows without a usable timestamp to the end, whatever the input order', () => {
    const older = q('2026-09-22T10:00:00Z')
    const newer = q('2026-09-22T11:00:00Z')
    const unparseable = q('not a date')
    const rows = [none, older, unparseable, newer, none]
    // Ties keep input order (sort is stable), so the three sunk rows stay as they came.
    expect([...rows].sort(newestQuestionFirst)).toEqual([newer, older, none, unparseable, none])
    // Two timestamp-less rows are EQUAL, never NaN -- a NaN answer is not transitive.
    expect(newestQuestionFirst(none, unparseable)).toBe(0)
    expect(newestQuestionFirst(unparseable, none)).toBe(0)
    for (const a of rows) for (const b of rows) expect(Number.isNaN(newestQuestionFirst(a, b))).toBe(false)
  })
})
