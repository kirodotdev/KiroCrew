import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../types'
import { createTranscriptVersioner } from './transcriptVersion'

/* The transcript version a memoized row keys its redraw on: it must move on
 * every change another row can read, and hold still only while a streaming
 * row's text grows or a row comes back as an equal copy. */

const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content, cls: '', ...extra })

const q1 = msg('user', 'q1', { ts: '2026-01-01T00:00:01Z' })
const a1 = msg('assistant', 'a1', { ts: '2026-01-01T00:00:02Z', meta: { mid: 'm1' } })

describe('createTranscriptVersioner', () => {
  it('starts at 1 and holds for the same list', () => {
    const v = createTranscriptVersioner()
    const list = [q1, a1]
    expect(v(list)).toBe(1)
    expect(v(list)).toBe(1)
  })

  it('holds while only a streaming row grows, wherever it sits', () => {
    const v = createTranscriptVersioner()
    const steer = msg('user', 'also this', { meta: { steer: true } })
    const start = v([q1, a1, msg('streaming', 'par')])
    expect(v([q1, a1, msg('streaming', 'part', { rawText: 'part' })])).toBe(start)
    // A steer can land after the streaming row; the row keeps growing in place.
    const withSteer = v([q1, a1, msg('streaming', 'part'), steer])
    expect(withSteer).toBe(start + 1)
    expect(v([q1, a1, msg('streaming', 'part two'), steer])).toBe(withSteer)
  })

  it('holds for a row re-minted as an equal copy', () => {
    const v = createTranscriptVersioner()
    const divider = () => msg('earlier-conversation', '', { meta: { count: 3, at: '2026-01-01T00:00:00Z' } })
    const start = v([divider(), q1, a1])
    expect(v([divider(), q1, a1])).toBe(start)
  })

  it('moves on every change another row can read', () => {
    const v = createTranscriptVersioner()
    let last = v([q1, a1, msg('streaming', 'part')])
    const moves = (list: ChatMessage[]) => {
      const next = v(list)
      expect(next).toBe(last + 1)
      last = next
    }
    // The streaming row settles into a reply: a role change.
    moves([q1, a1, msg('assistant', 'part')])
    // A row lands.
    moves([q1, a1, msg('assistant', 'part'), msg('user', 'q2')])
    // A settled row is edited in place: new content, then new meta.
    moves([q1, msg('assistant', 'a1 edited', { ts: a1.ts, meta: a1.meta }), msg('assistant', 'part'), msg('user', 'q2')])
    moves([q1, msg('assistant', 'a1 edited', { ts: a1.ts, meta: { mid: 'm1', turn_stats: { elapsed_ms: 5 } } }), msg('assistant', 'part'), msg('user', 'q2')])
    // A row leaves, and rows reorder.
    moves([q1, a1])
    moves([a1, q1])
    // Earlier history is prepended.
    moves([msg('user', 'q0'), a1, q1])
  })
})
