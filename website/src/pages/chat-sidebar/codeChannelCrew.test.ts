// A Slack code channel's crew in the folder view: which rows fold, and the
// conductor row's worst-first state line.
import { describe, it, expect } from 'vitest'
import { aggregateLine, codeChannelCrews, crewAttention, crewState, foldCodeChannelCrews, memberState, type CrewSignals } from './codeChannelCrew'
import type { WorkAggregate } from '../../types'
import type { Slot } from './types'

const CODE = { channel_name: 'fix-login', status: 'active' as const, web_url: 'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1' }
const row = (key: string, extra: Partial<Slot> = {}): Slot => ({ key, title: key, running: false, messages: 1, ...extra } as Slot)
const worker = (key: string, extra: Partial<Slot> = {}) => row(key, { parent: { slot: 'cond', key: 'cond' }, ...extra })

function signals(over: { running?: string[]; approvals?: Record<string, number>; unread?: string[] } = {}): CrewSignals {
  return {
    isRunning: s => (over.running ?? []).includes(s.key),
    subagentApprovals: s => over.approvals?.[s.key] ?? 0,
    isUnread: s => (over.unread ?? []).includes(s.key),
  }
}

describe('memberState: the precedence a row uses', () => {
  it.each([
    [{ pending_approval: true, running: true }, 'needs_you'],
    [{ needs_input: true }, 'needs_you'],
    // A worker's [OPTIONS:] are addressed to its conductor, not the user.
    [{ has_options: true }, 'idle'],
    [{ running: true, interrupted: true }, 'working'],
    [{ interrupted: true }, 'interrupted'],
    [{}, 'idle'],
  ] as const)('%o -> %s', (fields, expected) => {
    expect(memberState(row('a', fields as Partial<Slot>), signals())).toBe(expected)
  })

  it('a sub-agent waiting on its spawn approval is an owed decision', () => {
    expect(memberState(row('a'), signals({ approvals: { a: 1 } }))).toBe('needs_you')
  })

  it('the widened running signal counts as working', () => {
    expect(memberState(row('a'), signals({ running: ['a'] }))).toBe('working')
  })

  it('an armed loop or monitor does not hide an interrupted turn', () => {
    // The widened signal counts the loop as running; the row itself reads
    // "interrupted" because nothing is actually executing.
    expect(memberState(row('a', { interrupted: true }), signals({ running: ['a'] }))).toBe('interrupted')
  })
})

describe('crewState: worst first across the conductor and its workers', () => {
  const cond = row('cond', { code_channel: CODE })

  it('a hidden worker waiting on an approval wins, with what it last said', () => {
    const st = crewState(cond, [
      worker('w1', { running: true }),
      worker('w2', { pending_approval: true, last_message: 'approve git push' }),
    ], signals({ running: ['w1', 'cond'] }))
    expect(st).toEqual({ key: 'needs_you', count: 1, detail: 'approve git push' })
  })

  it('interrupted outranks working', () => {
    const st = crewState(cond, [worker('w1', { running: true }), worker('w2', { interrupted: true })], signals({ running: ['w1'] }))
    expect(st).toEqual({ key: 'interrupted', count: 1, detail: '' })
  })

  it('working counts every working member, the conductor included', () => {
    const st = crewState(cond, [worker('w1'), worker('w2'), worker('w3')], signals({ running: ['cond', 'w1', 'w3'] }))
    expect(st).toEqual({ key: 'working', count: 3, detail: '' })
  })

  it('an idle crew has no line of its own', () => {
    expect(crewState(cond, [worker('w1'), worker('w2')], signals())).toBeNull()
  })

  it('a question card or a sub-agent approval carries no detail', () => {
    expect(crewState(cond, [worker('w1', { needs_input: true, last_message: 'said before asking' })], signals()))
      .toEqual({ key: 'needs_you', count: 1, detail: '' })
    expect(crewState(cond, [worker('w1', { last_message: 'unrelated' })], signals({ approvals: { w1: 1 } })))
      .toEqual({ key: 'needs_you', count: 1, detail: '' })
  })

  it('a conductor whose own loop stalled keeps its own line over busy workers', () => {
    const stalled = row('cond', { code_channel: CODE, interrupted: true })
    expect(crewState(stalled, [worker('w1')], signals({ running: ['cond', 'w1'] }))).toBeNull()
  })

  it('the conductor keeps its own line when it is the most urgent member', () => {
    const own = row('cond', { code_channel: CODE, pending_approval: true })
    expect(crewState(own, [worker('w1')], signals({ running: ['w1'] }))).toBeNull()
  })

  it('a worker as urgent as the conductor takes the line, counting both', () => {
    const own = row('cond', { code_channel: CODE, pending_approval: true })
    expect(crewState(own, [worker('w1', { needs_input: true })], signals())).toMatchObject({ key: 'needs_you', count: 2 })
  })
})

describe('crewAttention: the dot beside the fold, from the slot list only', () => {
  it('names the most pressing thing a hidden worker wants', () => {
    expect(crewAttention([worker('w1')], signals())).toBeNull()
    expect(crewAttention([worker('w1', { pending_approval: true })], signals())).toBe('needs_you')
    expect(crewAttention([worker('w1', { needs_input: true })], signals())).toBe('needs_you')
    expect(crewAttention([worker('w1', { interrupted: true })], signals())).toBe('interrupted')
    expect(crewAttention([worker('w1')], signals({ unread: ['w1'] }))).toBe('unread')
    // Worst first across workers.
    expect(crewAttention([worker('w1', { interrupted: true }), worker('w2', { pending_approval: true })], signals({ unread: ['w1'] }))).toBe('needs_you')
    // Unread while still running is progress, not an ask.
    expect(crewAttention([worker('w1')], signals({ unread: ['w1'], running: ['w1'] }))).toBeNull()
  })
})

describe('codeChannelCrews: only code-channel conductors fold', () => {
  it('folds the direct workers of a code-channel conductor in the same rows', () => {
    const rows = [row('cond', { code_channel: CODE }), worker('w1'), worker('w2'), row('plain')]
    const crews = codeChannelCrews(rows)
    expect([...crews.keys()]).toEqual(['cond'])
    expect(crews.get('cond')!.map(s => s.key)).toEqual(['w1', 'w2'])
  })

  it('an ordinary parent keeps its children where they are', () => {
    const rows = [row('cond'), worker('w1')]
    expect(codeChannelCrews(rows).size).toBe(0)
  })

  it('a worker whose conductor is in another container is not folded', () => {
    expect(codeChannelCrews([worker('w1')]).size).toBe(0)
  })

  it('a peer row never folds and is never folded', () => {
    const rows = [row('cond', { code_channel: CODE }), worker('w1', { peer_id: 'peer-a' } as Partial<Slot>)]
    expect(codeChannelCrews(rows).size).toBe(0)
  })

  it('a worker\'s worker is not a direct worker', () => {
    const rows = [row('cond', { code_channel: CODE }), worker('w1'), row('w1a', { parent: { slot: 'w1', key: 'w1' } })]
    expect(codeChannelCrews(rows).get('cond')!.map(s => s.key)).toEqual(['w1'])
  })
})

describe('foldCodeChannelCrews', () => {
  const cond = row('cond', { code_channel: CODE })
  const w1 = worker('w1', { pending_approval: true, last_message: 'approve git push' })
  const w2 = worker('w2')
  const all = [cond, w1, w2]

  it('folds by default and reports the state while folded', () => {
    const fold = foldCodeChannelCrews(all, [cond, w1], [w2], { narrowed: false, expanded: new Set(), signals: signals() })
    expect(fold.fresh).toEqual([cond])
    expect(fold.stale).toEqual([])
    expect(fold.openWorkersOf(cond)).toBeNull()
    expect(fold.viewOf(cond)).toEqual({ workers: 2, expanded: false, state: { key: 'needs_you', count: 1, detail: 'approve git push' }, attention: 'needs_you' })
    expect(fold.viewOf(w2)).toBeUndefined()
  })

  it('open, the workers draw under the conductor and the row drops its crew line', () => {
    const fold = foldCodeChannelCrews(all, [cond, w1, w2], [], { narrowed: false, expanded: new Set(['cond']), signals: signals() })
    expect(fold.openWorkersOf(cond)).toEqual([w1, w2])
    expect(fold.viewOf(cond)).toEqual({ workers: 2, expanded: true, state: null, attention: null })
  })

  it('the open session and a pinned worker never fold, and do not count', () => {
    const fold = foldCodeChannelCrews(all, [cond, w1, w2], [], {
      narrowed: false, expanded: new Set(), signals: signals(), keepShown: w => w === w1,
    })
    expect(fold.fresh).toEqual([cond, w1])
    expect(fold.viewOf(cond)).toMatchObject({ workers: 1, state: null })
  })

  it('a crew whose only workers are kept on screen has no fold', () => {
    const fold = foldCodeChannelCrews(all, [cond, w1, w2], [], {
      narrowed: false, expanded: new Set(), signals: signals(), keepShown: () => true,
    })
    expect(fold.viewOf(cond)).toBeUndefined()
  })

  it('a narrowed list (search, filter) folds nothing', () => {
    const fold = foldCodeChannelCrews(all, [cond, w1, w2], [], { narrowed: true, expanded: new Set(), signals: signals() })
    expect(fold.fresh).toEqual([cond, w1, w2])
    expect(fold.viewOf(cond)).toBeUndefined()
  })

  it('a stale conductor does not fold its workers out of sight', () => {
    const fold = foldCodeChannelCrews(all, [w1, w2], [cond], { narrowed: false, expanded: new Set(), signals: signals() })
    expect(fold.fresh).toEqual([w1, w2])
  })
})

describe('work_aggregate present: the crew line reads the ledger summary too', () => {
  const agg = (state: WorkAggregate['state'], label = '', counts: WorkAggregate['counts'] = {}): WorkAggregate => ({ state, label, counts })
  const cond = (a?: WorkAggregate, extra: Partial<Slot> = {}) => row('cond', { code_channel: CODE, work_aggregate: a, ...extra })

  it.each([
    [agg('waiting', 'approve git push', { waiting: 2 }), { key: 'needs_you', count: 2, detail: 'approve git push' }],
    [agg('stuck', 'tests worker went quiet', { stuck: 1 }), { key: 'stuck', count: 1, detail: 'tests worker went quiet' }],
    [agg('working', '', { working: 3 }), { key: 'working', count: 3, detail: '' }],
    [agg('done', '', { done: 3, total: 3 }), { key: 'done', count: 3, total: 3, detail: '' }],
  ] as const)('%o reads as its own state', (a, expected) => {
    expect(aggregateLine(a)).toEqual(expected)
    expect(crewState(cond(a), [worker('w1')], signals())).toEqual(expected)
  })

  it('idle says nothing', () => {
    expect(aggregateLine(agg('idle'))).toBeNull()
    expect(crewState(cond(agg('idle')), [worker('w1')], signals())).toBeNull()
  })

  it('the board\'s state is trusted as it comes, never re-ranked against the slot list', () => {
    // The rows on screen look worse (interrupted) or busier (working): the
    // board already resolved the crew worst first, so its state stands.
    expect(crewState(cond(agg('working', 'w', { working: 2 })), [worker('w1', { interrupted: true })], signals())?.key).toBe('working')
    expect(crewState(cond(agg('done', '', { done: 2, total: 3 })), [worker('w1', { running: true })], signals({ running: ['w1'] }))?.key).toBe('done')
    expect(crewState(cond(agg('stuck')), [worker('w1')], signals())?.key).toBe('stuck')
    // Idle from the board: no crew line, even over a busy row on screen.
    expect(crewState(cond(agg('idle')), [worker('w1', { running: true })], signals({ running: ['w1'] }))).toBeNull()
  })

  it('a busy conductor still shows the board\'s line', () => {
    expect(crewState(cond(agg('done', '', { done: 3, total: 3 }), { running: true }), [], signals({ running: ['cond'] })))
      .toEqual({ key: 'done', count: 3, total: 3, detail: '' })
  })

  it('the label is rendered verbatim', () => {
    const label = '  #fix-login · 2 PRs open — [redacted]  '
    expect(aggregateLine(agg('working', label, { working: 1 }))?.detail).toBe(label)
  })

  it('INVARIANT: a hidden worker waiting on approval wins whatever the aggregate says, with its own context', () => {
    for (const state of ['waiting', 'stuck', 'working', 'done', 'idle'] as const) {
      const st = crewState(cond(agg(state, 'from the ledger')), [worker('w1', { pending_approval: true, last_message: 'approve git push' })], signals())
      expect(st).toEqual({ key: 'needs_you', count: 1, detail: 'approve git push' })
    }
  })

  it('the conductor\'s own approval keeps its own line', () => {
    expect(crewState(cond(agg('done', '', { done: 1, total: 1 }), { pending_approval: true }), [worker('w1')], signals())).toBeNull()
  })

  it('null reads as absent: the slot list alone', () => {
    expect(crewState(cond(null as unknown as WorkAggregate), [worker('w1', { interrupted: true })], signals())?.key).toBe('interrupted')
    expect(crewState(cond(null as unknown as WorkAggregate), [worker('w1')], signals())).toBeNull()
  })

  it('the conductor\'s own [OPTIONS:] ask under an armed loop keeps its own line', () => {
    // Widened running on (the loop), no turn running: its row reads "Waiting on you".
    const asking = cond(agg('working', '', { working: 2 }), { has_options: true })
    expect(crewState(asking, [worker('w1', { running: true })], signals({ running: ['cond', 'w1'] }))).toBeNull()
    // No armed automation: an [OPTIONS:] reply alone is not an ask, the board's line shows.
    expect(crewState(asking, [worker('w1')], signals())?.key).toBe('working')
  })

  it('the conductor\'s own interrupted turn keeps its own line over the aggregate', () => {
    for (const state of ['stuck', 'working', 'done'] as const) {
      expect(crewState(cond(agg(state), { interrupted: true }), [worker('w1')], signals({ running: ['cond'] }))).toBeNull()
    }
  })

  it('an interrupted worker the board does not yet call stuck still lights the dot', () => {
    const fold = foldCodeChannelCrews([cond(agg('done', '', { done: 1, total: 2 })), worker('w1', { interrupted: true })],
      [cond(agg('done', '', { done: 1, total: 2 })), worker('w1', { interrupted: true })], [], { narrowed: false, expanded: new Set(), signals: signals() })
    const view = fold.viewOf(fold.fresh[0])
    expect(view?.state?.key).toBe('done')
    expect(view?.attention).toBe('interrupted')
  })

  it('counts: a state reads at least one, and done never exceeds the total', () => {
    expect(aggregateLine(agg('stuck', '', { stuck: 0 }))?.count).toBe(1)
    expect(aggregateLine(agg('working', '', { working: 0 }))?.count).toBe(1)
    expect(aggregateLine(agg('done', '', { done: 5, total: 3 }))).toMatchObject({ count: 3, total: 3 })
  })

  it('a malformed aggregate falls back to the slot list (an interrupted worker shows)', () => {
    const bad = { state: 'exploded', label: 1, counts: null } as unknown as WorkAggregate
    expect(crewState(cond(bad), [worker('w1', { interrupted: true })], signals())).toEqual({ key: 'interrupted', count: 1, detail: '' })
  })

  it('a malformed aggregate costs the line, never a crash, and falls back to the slot list', () => {
    const bad = [
      { state: 'exploded', label: 1, counts: null },
      { state: 'done', label: null, counts: { done: -1, total: 'x' } },
      null,
      'nope',
    ] as unknown as WorkAggregate[]
    expect(aggregateLine(bad[0])).toBeNull()
    expect(aggregateLine(bad[1])).toEqual({ key: 'done', count: 0, total: undefined, detail: '' })
    expect(aggregateLine(bad[2])).toBeNull()
    expect(aggregateLine(bad[3])).toBeNull()
    expect(crewState(cond(bad[0]), [worker('w1', { pending_approval: true })], signals())?.key).toBe('needs_you')
  })
})

describe('work_aggregate absent: the slot list alone', () => {
  it('done and stuck simply do not show; every slot state still does', () => {
    const cond = row('cond', { code_channel: CODE })
    expect(crewState(cond, [worker('w1')], signals())).toBeNull()
    expect(crewState(cond, [worker('w1', { interrupted: true })], signals())?.key).toBe('interrupted')
    expect(crewState(cond, [worker('w1', { pending_approval: true, last_message: 'approve git push' })], signals()))
      .toEqual({ key: 'needs_you', count: 1, detail: 'approve git push' })
  })
})

describe('foldCodeChannelCrews with an aggregate and no worker rows', () => {
  it('the conductor still gets the aggregate\'s line, and no fold', () => {
    const cond = row('cond', { code_channel: CODE, work_aggregate: { state: 'done', label: '', counts: { done: 3, total: 3 } } })
    const fold = foldCodeChannelCrews([cond], [cond], [], { narrowed: false, expanded: new Set(), signals: signals() })
    expect(fold.viewOf(cond)).toEqual({ workers: 0, expanded: false, state: { key: 'done', count: 3, total: 3, detail: '' }, attention: null })
    expect(fold.openWorkersOf(cond)).toBeNull()
  })
})
