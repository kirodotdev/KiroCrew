/**
 * `orderTurnsNewestFirst`: the row reorder behind the top-down chat layout.
 * Blocks (an opener row plus the rows after it) come out newest first; rows
 * inside a block keep their order, and the row objects are the inputs.
 */
import { describe, it, expect } from 'vitest'
import { groupDisplayItems } from '../pages/chat/groupDisplayItems'
import { opensTurnBlock, orderTurnsNewestFirst } from '../pages/chat/orderTurnsNewestFirst'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

const msg = (role: string, content = '', extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content, cls: '', ...extra } as ChatMessage)

const workingTurn = (tag: string) => [msg('assistant', `${tag}-a`), msg('tool', `${tag}-t`), msg('assistant', `${tag}-b`)]

const COMPLETION = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: map the picker',
  '',
  'done',
].join('\n')

/** First message content of a row, to describe an order compactly. */
const label = (d: DisplayItem): string => {
  if (d.kind === 'single') return d.msg.content
  if (d.kind === 'group') return d.msgs[0].content
  const f = d.items[0]
  return `turn(${f.kind === 'single' ? f.msg.content : f.msgs[0].content})`
}

describe('orderTurnsNewestFirst', () => {
  it('returns no rows and headBlockEnd -1 for an empty list', () => {
    expect(orderTurnsNewestFirst<DisplayItem>([])).toEqual({ rows: [], headBlockEnd: -1, chrono: [] })
  })

  it('leaves a single block in place, with the head block ending at the last row', () => {
    const { turns } = groupDisplayItems([msg('user', 'u1'), ...workingTurn('r1')])
    const out = orderTurnsNewestFirst(turns)
    expect(out.rows).toEqual(turns)
    expect(out.headBlockEnd).toBe(turns.length - 1)
  })

  it('puts the newest turn first and keeps prompt-then-reply inside each turn', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u1'), msg('assistant', 'a1'),
      msg('user', 'u2'), ...workingTurn('r2'),
      msg('user', 'u3'), msg('assistant', 'a3'),
    ])
    const out = orderTurnsNewestFirst(turns)
    expect(out.rows.map(label)).toEqual(['u3', 'a3', 'u2', 'turn(r2-a)', 'u1', 'a1'])
    expect(out.headBlockEnd).toBe(1)
    // Same objects, not copies: keyed memo and height caches survive.
    for (const r of out.rows) expect(turns).toContain(r)
  })

  it('treats leading non-opener rows as the oldest block', () => {
    const { turns } = groupDisplayItems([
      msg('assistant', 'orphan-reply'), msg('user', 'u1'), msg('assistant', 'a1'),
    ])
    expect(orderTurnsNewestFirst(turns).rows.map(label)).toEqual(['u1', 'a1', 'orphan-reply'])
  })

  it('keeps a compaction card inside the block it landed in', () => {
    const card = msg('assistant', 'compacted', { kind: 'compaction' } as Partial<ChatMessage>)
    const { turns } = groupDisplayItems([
      msg('user', 'u1'), msg('assistant', 'a1'), card,
      msg('user', 'u2'), msg('assistant', 'a2'),
    ])
    expect(orderTurnsNewestFirst(turns).rows.map(label)).toEqual(['u2', 'a2', 'u1', 'a1', 'compacted'])
  })

  it('opens a block on a cron delivery, as the grouper opens a turn on it', () => {
    const cron = msg('inject', 'cron-fired', { meta: { injectKind: 'cron' } } as Partial<ChatMessage>)
    const { turns } = groupDisplayItems([
      msg('user', 'u1'), msg('assistant', 'a1'), cron, msg('assistant', 'cron-reply'),
      msg('user', 'u2'), msg('assistant', 'a2'),
    ])
    // The cron delivery is the prompt of the turn below it (opensDisplayTurn),
    // so its reply sits under it and the block sorts between u2 and u1.
    const out = orderTurnsNewestFirst(turns)
    expect(out.rows.map(label)).toEqual(['u2', 'a2', 'cron-fired', 'cron-reply', 'u1', 'a1'])
    expect(out.headBlockEnd).toBe(1)
  })

  it('keeps a plain inject row inside the block it follows', () => {
    // An inject with no stamped kind dispatches nothing, so it opens no turn.
    const note = msg('inject', 'note')
    const { turns } = groupDisplayItems([
      msg('user', 'u1'), msg('assistant', 'a1'), note, msg('assistant', 'a1b'),
      msg('user', 'u2'), msg('assistant', 'a2'),
    ])
    const out = orderTurnsNewestFirst(turns)
    expect(out.rows.map(label)).toEqual(['u2', 'a2', 'u1', 'turn(a1)'])
    const tail = out.rows[3]
    expect(tail.kind === 'turn' && tail.items.map(i => (i.kind === 'single' ? i.msg.content : ''))).toEqual(['a1', 'note', 'a1b'])
  })

  it('opens a block on a nudge, like a user prompt', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u1'), msg('assistant', 'a1'), msg('nudge', 'n1'), msg('assistant', 'a2'),
    ])
    const out = orderTurnsNewestFirst(turns)
    expect(out.rows.map(label)).toEqual(['n1', 'a2', 'u1', 'a1'])
    expect(out.headBlockEnd).toBe(1)
  })

  it('opens a block on a steered bubble, so the continuation sits under it', () => {
    // The grouper splits on the steered user row like any user row, so the
    // reorder does too: the steer and the reply it redirected lead the list.
    const steered = msg('user', 'steer', { meta: { steer: true } } as Partial<ChatMessage>)
    const { turns } = groupDisplayItems([
      msg('user', 'u1'), msg('assistant', 'a1'), steered, msg('streaming', 'live'),
    ])
    const out = orderTurnsNewestFirst(turns)
    expect(out.rows.map(label)).toEqual(['steer', 'live', 'u1', 'a1'])
    expect(out.rows[out.headBlockEnd]).toBe(turns[turns.length - 1])
  })

  it('keeps an interim fold with its prompt, even though it holds completion openers', () => {
    const synthesis = msg('inject', 'synth', { meta: { injectKind: 'synthesis' } } as Partial<ChatMessage>)
    const { turns } = groupDisplayItems([
      msg('user', 'u0'), msg('assistant', 'a0'),
      msg('user', 'u1'), ...workingTurn('w1'),
      msg('subagent', COMPLETION), ...workingTurn('w2'),
      synthesis, msg('assistant', 'answer'),
    ])
    const interim = turns.find(t => t.kind === 'turn' && t.interim)
    expect(interim).toBeDefined()
    expect(opensTurnBlock(interim!)).toBe(false)
    const out = orderTurnsNewestFirst(turns)
    // u1, its interim fold and the synthesis answer are ONE block, ahead of u0.
    const u1 = out.rows.findIndex(r => label(r) === 'u1')
    const u0 = out.rows.findIndex(r => label(r) === 'u0')
    expect(u1).toBe(0)
    expect(out.rows.indexOf(interim!)).toBeGreaterThan(u1)
    expect(out.rows.indexOf(interim!)).toBeLessThan(u0)
    expect(out.rows.slice(u0).map(label)).toEqual(['u0', 'a0'])
    expect(out.headBlockEnd).toBe(u0 - 1)
  })

  it('accepts a caller-supplied opener test for another row shape', () => {
    const out = orderTurnsNewestFirst(['P1', 'r', 'P2', 'r2', 'r3'], s => s.startsWith('P'))
    expect(out).toEqual({ rows: ['P2', 'r2', 'r3', 'P1', 'r'], headBlockEnd: 2, chrono: [2, 3, 4, 0, 1] })
  })

  it('maps every row back to its chronological index, a leading orphan block included', () => {
    const items = ['orphan', 'P1', 'r1', 'P2']
    const out = orderTurnsNewestFirst(items, s => s.startsWith('P'))
    expect(out.rows).toEqual(['P2', 'P1', 'r1', 'orphan'])
    expect(out.chrono).toEqual([3, 1, 2, 0])
    out.rows.forEach((r, i) => expect(items[out.chrono[i]]).toBe(r))
  })
})
