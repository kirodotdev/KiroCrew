/**
 * `groupDisplayItems` + `applyRunningState` — the transcript grouping pass, split
 * out of ChatPage so the `slotRunning` flag stops re-running an O(N) pass.
 *
 * Grouping decides what the user actually SEES, so these tests pin the semantics
 * of the split rather than its performance: the same messages must produce the
 * same items, and the running flag must land on exactly one element.
 */
import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

const msg = (role: string, content = ''): ChatMessage =>
  ({ role, content, cls: '' } as ChatMessage)

/** A turn long enough to collapse: needs a working step and > 2 items. */
const workingTurn = () => [msg('assistant', 'a'), msg('tool', 't'), msg('assistant', 'b')]

/** A sub-agent completion event SubagentCompletionCard can parse. */
const COMPLETION = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: map the picker',
  '',
  'done',
].join('\n')

const isTurn = (d: DisplayItem): d is { kind: 'turn'; items: never[]; complete: boolean } =>
  d.kind === 'turn'

describe('groupDisplayItems', () => {
  it('drops permission messages and unparseable subagent injections from the transcript', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u'), msg('permission', 'p'), msg('subagent', 's'), msg('assistant', 'a'),
    ])
    const singles = turns.filter(t => t.kind === 'single')
    expect(singles.map(t => (t as { msg: ChatMessage }).msg.role)).toEqual(['user', 'assistant'])
  })

  it('keeps a subagent completion the card can render, and opens a turn on it', () => {
    // The event IS the next turn's input, so the agent's reply must group BELOW
    // the card rather than beside it.
    const completion = msg('subagent', COMPLETION)
    const { turns } = groupDisplayItems([
      msg('user', 'u'), ...workingTurn(), completion, ...workingTurn(),
    ])
    const singles = turns.filter(t => t.kind === 'single') as { msg: ChatMessage }[]
    expect(singles.map(t => t.msg.role)).toEqual(['user', 'subagent'])
    // Two collapsible turns: one under the user message, one under the card.
    expect(turns.filter(isTurn)).toHaveLength(2)
  })

  it('preserves the original message index on singles', () => {
    // idx must be the index into the INPUT array, not into the filtered output —
    // callers map display rows back to messages with it.
    const { turns } = groupDisplayItems([msg('permission'), msg('user', 'u')])
    const single = turns.find(t => t.kind === 'single') as { idx: number }
    expect(single.idx).toBe(1)
  })

  it('opens a new turn on a user message', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'first'), ...workingTurn(), msg('user', 'second'), ...workingTurn(),
    ])
    const users = turns.filter(t => t.kind === 'single' && (t as { msg: ChatMessage }).msg.role === 'user')
    expect(users).toHaveLength(2)
  })

  it('opens a new turn on a nudge, same as a user message', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u'), ...workingTurn(), msg('nudge', 'keep going'), ...workingTurn(),
    ])
    // Two collapsed turns, one per prompt — the nudge must not be swallowed into
    // the previous turn's step group.
    expect(turns.filter(isTurn)).toHaveLength(2)
  })

  it('marks every NON-trailing turn complete regardless of running state', () => {
    const { turns, trailingTurnIdx } = groupDisplayItems([
      msg('user', 'u1'), ...workingTurn(), msg('user', 'u2'), ...workingTurn(),
    ])
    const allTurns = turns.filter(isTurn)
    expect(allTurns).toHaveLength(2)
    expect(allTurns[0].complete).toBe(true)
    // The last one is the trailing turn, and grouping always emits it complete.
    expect(trailingTurnIdx).toBeGreaterThanOrEqual(0)
    expect(turns[trailingTurnIdx]).toBe(allTurns[1])
  })

  it('reports trailingTurnIdx = -1 when the trailing group does not collapse', () => {
    // Two items only — below the > 2 threshold, so flushTurn spreads them as
    // loose items and there is no `complete` flag for the running state to touch.
    const { turns, trailingTurnIdx } = groupDisplayItems([msg('user', 'u'), msg('assistant', 'a')])
    expect(trailingTurnIdx).toBe(-1)
    expect(turns.every(t => !isTurn(t))).toBe(true)
  })

  it('reports trailingTurnIdx = -1 for an empty list', () => {
    expect(groupDisplayItems([])).toEqual({ turns: [], trailingTurnIdx: -1 })
  })

  it('does not collapse a turn with no working steps', () => {
    const { turns } = groupDisplayItems([msg('user', 'a'), msg('user', 'b'), msg('user', 'c')])
    expect(turns.filter(isTurn)).toHaveLength(0)
  })
})

describe('applyRunningState', () => {
  const grouped = () => groupDisplayItems([msg('user', 'u'), ...workingTurn()])

  it('returns the grouped array UNCHANGED by identity when not running', () => {
    const g = grouped()
    // Identity matters: a new array here would cascade into the display-index
    // maps and the virtualizer, which is the cost this split exists to avoid.
    expect(applyRunningState(g, false)).toBe(g.turns)
  })

  it('marks the trailing turn incomplete while running', () => {
    const g = grouped()
    const out = applyRunningState(g, true)
    expect(out[g.trailingTurnIdx]).toMatchObject({ kind: 'turn', complete: false })
  })

  it('leaves every other element identity-stable while running', () => {
    const g = groupDisplayItems([msg('user', 'u1'), ...workingTurn(), msg('user', 'u2'), ...workingTurn()])
    const out = applyRunningState(g, true)
    for (let i = 0; i < out.length; i++) {
      if (i === g.trailingTurnIdx) continue
      expect(out[i]).toBe(g.turns[i])
    }
  })

  it('does not mutate the grouped input', () => {
    const g = grouped()
    const trailingBefore = g.turns[g.trailingTurnIdx]
    applyRunningState(g, true)
    expect(g.turns[g.trailingTurnIdx]).toBe(trailingBefore)
    expect((trailingBefore as { complete: boolean }).complete).toBe(true)
  })

  it('is a no-op when running but nothing collapsed', () => {
    const g = groupDisplayItems([msg('user', 'u'), msg('assistant', 'a')])
    expect(applyRunningState(g, true)).toBe(g.turns)
  })

  it('reproduces the pre-split behaviour: trailing complete === !slotRunning', () => {
    // Grouping always emits `complete: true` and this function applies the flag,
    // so the trailing turn's `complete` must equal `!slotRunning` in both
    // directions.
    const g = grouped()
    for (const slotRunning of [true, false]) {
      const out = applyRunningState(g, slotRunning)
      const trailing = out[g.trailingTurnIdx] as { complete: boolean }
      expect(trailing.complete).toBe(!slotRunning)
    }
  })
})
