import { describe, expect, it } from 'vitest'
import reducer, { hydrateSlotMessages, sseChatMessage } from './chatSlice'
import { streamFlush } from './streamFlush'

// A split-view pane's one-time hydrate can land after the live chunks of a turn
// it is already showing (#29707): the page then carries the same reply as a
// trailing `streaming` row folded up to `seq`.
const slot = 'split-pane'
const gen = 'g1'
const ts = '2026-11-04T16:53:10Z'
const piece = (seq: number) => `fake slow chunk ${seq - 1} `
const text = (upTo: number) => Array.from({ length: upTo }, (_, i) => piece(i + 1)).join('')
// The socket's per-frame flush, as production lands chunks: each part carries its seq.
const flush = (seqs: number[], thinking?: string) => streamFlush({
  slots: [{
    slot,
    thinking,
    content: seqs.length ? { text: seqs.map(piece).join(''), seq: seqs.at(-1), gen, parts: seqs.map(seq => ({ seq, text: piece(seq) })) } : undefined,
  }],
  subagents: [],
  recency: [],
})
const page = (seq: number) => hydrateSlotMessages({
  slot,
  messages: [
    { role: 'user', content: 'hello', cls: 'msg msg-u', ts, meta: { mid: 'u1' } },
    { role: 'streaming', content: text(seq), cls: 'msg msg-a', seq, gen },
  ],
  bounded: true, hasMore: false, total: 2, running: true,
})
const sent = () => reducer(reducer(undefined, { type: '@@INIT' }), sseChatMessage({ slot, role: 'user', content: 'hello', ts, meta: { mid: 'u1' } }))
const rows = (state: ReturnType<typeof sent>) => state.slotMessages[slot].map(m => [m.role, m.content])

describe('pane hydrate over a live stream', () => {
  it('draws the reply once, below its reasoning, when the page holds the live chunks', () => {
    let state = reducer(sent(), flush([1], 'pondering'))
    state = reducer(state, page(1))
    state = reducer(state, flush([1, 2]))
    expect(rows(state)).toEqual([['user', 'hello'], ['thinking', 'pondering'], ['streaming', text(2)]])
  })

  it('drops a chunk the page already holds when it lands after the page', () => {
    let state = reducer(sent(), page(1))
    state = reducer(state, flush([1, 2]))
    expect(rows(state)).toEqual([['user', 'hello'], ['streaming', text(2)]])
  })

  it('keeps a live copy that is ahead of the page', () => {
    // The page cannot vouch for the live row's extra chunk, so the live row is
    // kept whole; the end-of-turn warm reconciles the two.
    const state = reducer(reducer(sent(), flush([1, 2])), page(1))
    expect(rows(state).at(-1)).toEqual(['streaming', text(2)])
  })
})
