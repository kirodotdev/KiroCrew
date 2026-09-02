// Feature: chat-virtualizer — row keys are unique across the display list.
//
// `virtualKeyFor` keys a `single` on `msgKey` alone (`row-<ts>`), and a coarse
// OS clock can stamp two rows appended in one tick with the same `ts` — the
// hazard `msgIdentityKey` closes for group leads but singles were exposed to.
// Duplicate keys reach React as duplicate siblings (one row is silently
// dropped from the DOM — the "content disappears" symptom) and share one
// HeightCache slot (each re-measure of either row reprices the other). The
// same collision arrives via an overlapping older page whose rows lack the
// `meta.mid` the prepend dedup keys on.
//
// `uniqueRowKeys` closes this at the list level: the FIRST occurrence keeps
// the bare `virtualKeyFor` key — so the non-colliding common case is
// byte-identical and every persisted height / scroll anchor keyed before this
// pass survives — and each later duplicate gets an occurrence suffix.

import { describe, it, expect } from 'vitest'
import { uniqueRowKeys, virtualKeyFor } from '../pages/ChatPage'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const msgKey = (m: ChatMessage): string => (m.meta?.clientTs as string | undefined) || m.ts || ''

function single(ts: string, idx: number, role = 'user'): DisplayItem {
  return { kind: 'single', msg: { role, content: `c${idx}`, ts } as ChatMessage, idx }
}

describe('uniqueRowKeys', () => {
  it('is byte-identical to virtualKeyFor when there is no collision', () => {
    const items = [single('100', 0), single('200', 1), single('300', 2)]
    const keys = uniqueRowKeys(items, msgKey)
    expect(keys).toEqual(items.map((it, i) => virtualKeyFor(it, i, msgKey)))
  })

  it('tie-breaks same-tick ts collisions; the first occurrence keeps the bare key', () => {
    const items = [single('100', 0), single('100', 1, 'assistant'), single('200', 2)]
    const keys = uniqueRowKeys(items, msgKey)
    expect(keys[0]).toBe(virtualKeyFor(items[0], 0, msgKey))
    expect(keys[1]).not.toBe(keys[0])
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('suffixes every later duplicate distinctly in a multi-way collision', () => {
    const items = [single('100', 0), single('100', 1), single('100', 2)]
    const keys = uniqueRowKeys(items, msgKey)
    expect(new Set(keys).size).toBe(3)
    expect(keys[0]).toBe(virtualKeyFor(items[0], 0, msgKey))
  })

  it('dedupes a turn against a loose single sharing the lead message ts', () => {
    // A turn inherits its LEAD single's key (deliberate — regroup stability),
    // so a loose single with the same ts elsewhere in the list collides with
    // the turn row. Both must still reach React distinct.
    const lead = single('100', 0)
    const turn: DisplayItem = { kind: 'turn', items: [lead], complete: true }
    const items: DisplayItem[] = [turn, single('100', 1)]
    const keys = uniqueRowKeys(items, msgKey)
    expect(new Set(keys).size).toBe(2)
    expect(keys[0]).toBe(virtualKeyFor(turn, 0, msgKey))
  })

  it('does not let the ~#N suffix collide with a NATURAL key spelling the same string', () => {
    // msgKey passes through arbitrary meta (clientTs), so a natural key can
    // literally spell `row-100~#1` — the string the tie-break would mint for
    // the second `row-100`. All three must still reach React distinct.
    const natural: DisplayItem = {
      kind: 'single',
      msg: { role: 'user', content: 'n', ts: '999', meta: { clientTs: '100~#1' } } as ChatMessage,
      idx: 0,
    }
    const items = [natural, single('100', 1), single('100', 2)]
    const keys = uniqueRowKeys(items, msgKey)
    expect(new Set(keys).size).toBe(3)
  })

  it('is deterministic for a given list order', () => {
    const items = [single('100', 0), single('100', 1), single('200', 2), single('200', 3)]
    expect(uniqueRowKeys(items, msgKey)).toEqual(uniqueRowKeys(items, msgKey))
  })
})
