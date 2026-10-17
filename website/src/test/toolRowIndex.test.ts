import { describe, it, expect } from 'vitest'
import { selectToolRowIndex, lookupLogEntry, denySiblingContent } from '../pages/chat/toolRowIndex'
import type { ChatMessage, ToolActivity } from '../types'

// Per-slot tool-row index contract: ONE build per (messages, toolLog)
// identity pair, no matter how many mounted rows consult it per dispatch —
// this is what converts ToolCallLine's per-row O(messages + toolLog) selector
// scans into O(1) lookups.

const toolMsg = (id: string | undefined, content: string, i: number): ChatMessage =>
  ({ role: 'tool', content, ts: String(i), meta: id ? { tool_call_id: id } : {} })

const permMsg = (id: string, resolved: string | undefined, i: number): ChatMessage =>
  ({ role: 'permission', content: 'perm', ts: String(i), meta: { tool_call_id: id, ...(resolved ? { resolved } : {}) } })

const logEntry = (id: string | undefined, text: string, ts: number): ToolActivity =>
  ({ type: 'tool', text, ts, tool_call_id: id })

describe('toolRowIndex — build count', () => {
  it('builds once per (messages, toolLog) identity pair, not per row', () => {
    const msgs = [toolMsg('a', '🔧 one', 0), toolMsg('b', '🔧 two', 1)]
    const log = [logEntry('a', 'one', 0), logEntry('b', 'two', 1)]
    const before = selectToolRowIndex.recomputations()
    // Three mounted rows each running their selector on the same dispatch:
    const i1 = selectToolRowIndex(msgs, log)
    const i2 = selectToolRowIndex(msgs, log)
    const i3 = selectToolRowIndex(msgs, log)
    expect(selectToolRowIndex.recomputations()).toBe(before + 1)
    expect(i2).toBe(i1)
    expect(i3).toBe(i1)
    // A toolLog identity change rebuilds exactly once:
    const log2 = log.slice()
    selectToolRowIndex(msgs, log2)
    selectToolRowIndex(msgs, log2)
    expect(selectToolRowIndex.recomputations()).toBe(before + 2)
    // A messages identity change rebuilds exactly once:
    const msgs2 = msgs.slice()
    selectToolRowIndex(msgs2, log2)
    selectToolRowIndex(msgs2, log2)
    expect(selectToolRowIndex.recomputations()).toBe(before + 3)
  })
})

describe('toolRowIndex — lookup semantics', () => {
  it('logById resolves the NEWEST entry per tool_call_id', () => {
    const older = logEntry('a', 'first frame', 0)
    const newer = logEntry('a', 'second frame', 1)
    const index = selectToolRowIndex([], [older, newer])
    expect(lookupLogEntry(index, 'a', 'irrelevant')).toBe(newer)
    expect(lookupLogEntry(index, 'missing', 'irrelevant')).toBeUndefined()
  })

  it('id-less rows fall back to the newest id-bearing entry whose text is a label substring', () => {
    const e1 = logEntry('x', 'Running: ls', 0)
    const e2 = logEntry('y', 'Running: cat', 1)
    const index = selectToolRowIndex([], [e1, e2])
    expect(lookupLogEntry(index, undefined, '🔧 Running: ls -la')).toBe(e1)
    expect(lookupLogEntry(index, undefined, 'no match here')).toBeUndefined()
    // Cached second lookup returns the same entry.
    expect(lookupLogEntry(index, undefined, '🔧 Running: ls -la')).toBe(e1)
  })

  it('pendingPermIds tracks unresolved permissions; lastPermById the newest decision', () => {
    const msgs = [
      permMsg('a', undefined, 0),          // unresolved → pending
      permMsg('b', 'rejected', 1),         // resolved
      permMsg('b', 'approved', 2),         // NEWEST decision for b
    ]
    const index = selectToolRowIndex(msgs, [])
    expect(index.pendingPermIds.has('a')).toBe(true)
    expect(index.pendingPermIds.has('b')).toBe(false)
    expect(index.lastPermById.get('b')?.meta?.resolved).toBe('approved')
  })

  it('denySiblingContent finds a 🚫 sibling above the pill and stops at the pill itself', () => {
    const own = toolMsg('a', '🔧 Running: rm', 0)
    const deny = toolMsg('a', '🚫 Running: rm — Blocked by security policy: no', 1)
    const withDeny = selectToolRowIndex([own, deny], [])
    expect(denySiblingContent(withDeny, 'a', own)).toContain('🚫')
    // A 🚫 row BELOW the pill (earlier in the transcript) belongs to an
    // earlier call and must not mark this pill blocked.
    const laterOwn = toolMsg('a', '🔧 Running: rm', 2)
    const denyBelow = selectToolRowIndex([deny, laterOwn], [])
    expect(denySiblingContent(denyBelow, 'a', laterOwn)).toBe('')
    expect(denySiblingContent(withDeny, undefined, own)).toBe('')
  })

  it('lastToolMsg is the transcript-newest tool message', () => {
    const a = toolMsg('a', '🔧 one', 0)
    const b = toolMsg('b', '🔧 two', 1)
    const index = selectToolRowIndex([a, { role: 'assistant', content: 'x', ts: '2' }, b], [])
    expect(index.lastToolMsg).toBe(b)
  })
})
