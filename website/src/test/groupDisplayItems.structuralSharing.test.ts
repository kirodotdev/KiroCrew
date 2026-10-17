import { describe, it, expect } from 'vitest'
import { createTurnGrouper, groupDisplayItems } from '../pages/chat/groupDisplayItems'
import type { ChatMessage } from '../types'

// Structural-sharing contract for createTurnGrouper: a streaming flush that
// only changes the TAIL message must hand back the previous objects — by
// reference — for every settled turn, so memo(TurnBlock) and the
// mergeTurnThinking [turn.items] memo bail out. Only the trailing turn may
// carry a new identity.

const msg = (role: string, content: string, i: number): ChatMessage =>
  ({ role, content, ts: String(i) })

/** Two settled turns + a trailing turn ending in a streaming row. Each turn
 *  has >2 non-opener items with working steps, so flushTurn wraps it. */
function buildMessages(): ChatMessage[] {
  return [
    msg('user', 'first question', 0),
    msg('assistant', 'thinking about it', 1),
    msg('tool', '🔧 Running: ls', 2),
    msg('assistant', 'first answer', 3),
    msg('user', 'second question', 4),
    msg('assistant', 'working', 5),
    msg('tool', '🔧 Running: cat', 6),
    msg('assistant', 'second answer', 7),
    msg('user', 'third question', 8),
    msg('assistant', 'partial', 9),
    msg('tool', '🔧 Running: grep', 10),
    msg('streaming', 'streaming tail', 11),
  ]
}

describe('createTurnGrouper — structural sharing', () => {
  it('groups identically to the pure groupDisplayItems', () => {
    const messages = buildMessages()
    expect(createTurnGrouper()(messages)).toEqual(groupDisplayItems(messages))
  })

  it('returns the same result object for the same messages array', () => {
    const grouper = createTurnGrouper()
    const messages = buildMessages()
    const r1 = grouper(messages)
    expect(grouper(messages)).toBe(r1)
  })

  it('appending a chunk to the trailing turn keeps every settled turn BY REFERENCE and rebuilds only the trailing turn', () => {
    const grouper = createTurnGrouper()
    const messages1 = buildMessages()
    const r1 = grouper(messages1)
    // Shape check: 3 turn openers + 3 wrapped turns, trailing turn last.
    expect(r1.turns.length).toBe(6)
    expect(r1.trailingTurnIdx).toBe(5)

    // Streaming flush: same array shape, only the tail message object replaced.
    const messages2 = messages1.slice()
    messages2[messages2.length - 1] = { ...messages2[messages2.length - 1], content: 'streaming tail grew' }
    const r2 = grouper(messages2)

    expect(r2.turns.length).toBe(r1.turns.length)
    for (let i = 0; i < r2.turns.length - 1; i++) {
      expect(r2.turns[i]).toBe(r1.turns[i]) // identity, not equality
    }
    const trailing1 = r1.turns[5]
    const trailing2 = r2.turns[5]
    expect(trailing2).not.toBe(trailing1)
    if (trailing2.kind !== 'turn' || trailing1.kind !== 'turn') throw new Error('trailing element must be a turn')
    expect(trailing2.items[trailing2.items.length - 1]).toMatchObject({ kind: 'single', msg: { content: 'streaming tail grew' } })
  })

  it('a new messages array with unchanged element references returns the previous result object', () => {
    const grouper = createTurnGrouper()
    const messages1 = buildMessages()
    const r1 = grouper(messages1)
    const r2 = grouper(messages1.slice())
    expect(r2).toBe(r1)
  })

  it('appending a NEW message to the trailing turn still shares all settled turns', () => {
    const grouper = createTurnGrouper()
    const messages1 = buildMessages()
    const r1 = grouper(messages1)
    const messages2 = [...messages1, msg('tool', '🔧 Running: tail', 12)]
    const r2 = grouper(messages2)
    for (let i = 0; i < r2.turns.length - 1; i++) {
      expect(r2.turns[i]).toBe(r1.turns[i])
    }
    expect(r2.turns[5]).not.toBe(r1.turns[5])
  })

  it('two groupers do not share cache state', () => {
    const a = createTurnGrouper()
    const b = createTurnGrouper()
    const messages = buildMessages()
    const ra = a(messages)
    const rb = b(messages)
    expect(ra).toEqual(rb)
    expect(ra).not.toBe(rb) // independent closures build independent results
  })
})

// Field-exhaustiveness gate for the purity invariant: the reconcile's equality
// helpers compare a FIXED set of fields per variant, so a field added to the
// grouper's output without extending the comparison would be silently frozen
// on settled turns. Pin the runtime key sets of produced objects to exactly
// what sameTurnItem/sameDisplayItem read — an added field fails here by name
// instead of shipping stale UI.
describe('reconcile field exhaustiveness', () => {
  it('produced TurnItem/DisplayItem variants carry only fields the reconcile compares', () => {
    const msgs: ChatMessage[] = [
      { role: 'user', content: 'q', ts: '1' },
      { role: 'assistant', content: 'a', ts: '2' },
      { role: 'tool', content: '🔧 Running: ls', ts: '3' },
      { role: 'tool', content: '🔧 Running: cat', ts: '4' },
      { role: 'assistant', content: 'done', ts: '5' },
      { role: 'user', content: 'next', ts: '6' },
    ]
    const { turns } = groupDisplayItems(msgs)
    const expectedKeys: Record<string, string[]> = {
      // sameDisplayItem: kind + complete + items (recursing per item);
      // sameTurnItem singles: kind + msg + idx; groups: kind + msgs + startIdx.
      'display:turn': ['kind', 'items', 'complete'],
      'display:single': ['kind', 'msg', 'idx'],
      'display:group': ['kind', 'msgs', 'startIdx'],
      'turn:single': ['kind', 'msg', 'idx'],
      'turn:group': ['kind', 'msgs', 'startIdx'],
    }
    const seen = new Set<string>()
    for (const it of turns) {
      const tag = `display:${it.kind}`
      expect(Object.keys(it).sort(), `${tag} keys drifted — extend sameDisplayItem`).toEqual([...expectedKeys[tag]].sort())
      seen.add(tag)
      if (it.kind === 'turn') {
        for (const ti of it.items) {
          const ttag = `turn:${ti.kind}`
          expect(Object.keys(ti).sort(), `${ttag} keys drifted — extend sameTurnItem`).toEqual([...expectedKeys[ttag]].sort())
          seen.add(ttag)
        }
      }
    }
    // The fixture must exercise the variants it gates, or the pin is hollow.
    // group variants are absent: the raw pass skips permission messages (the
    // pinned ApprovalBar owns them), so the grouper currently never emits
    // kind:'group' — the comparators' group branches are defensive. If a
    // GROUPABLE role becomes reachable, add it here and to expectedKeys.
    for (const required of ['display:turn', 'display:single', 'turn:single']) {
      expect(seen.has(required), `fixture no longer produces ${required}`).toBe(true)
    }
  })
})
