// chat-core P5-e: the per-message key every virtualized host rows by. Pinned
// because a key that drifts between renders throws away the row's cached
// height, DOM node and scroll anchor — and the array index, the obvious
// fallback, does exactly that on every history prepend.
import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useStableMessageKey } from '../chat-core/transcript/useStableMessageKey'
import type { ChatMessage } from '../types'

const msg = (over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role: 'assistant', content: 'x', cls: '', ...over }) as ChatMessage

describe('useStableMessageKey', () => {
  it('prefers the optimistic client timestamp, then the server timestamp', () => {
    const { result } = renderHook(() => useStableMessageKey())
    expect(result.current(msg({ ts: 'srv', meta: { clientTs: 'cli' } }))).toBe('cli')
    expect(result.current(msg({ ts: 'srv', meta: {} }))).toBe('srv')
    expect(result.current(msg({ ts: 'srv' }))).toBe('srv')
  })

  it('mints an id once per message object when neither timestamp exists, and remembers it', () => {
    const { result, rerender } = renderHook(() => useStableMessageKey())
    const a = msg()
    const b = msg()
    const ka = result.current(a)
    const kb = result.current(b)
    expect(ka).not.toBe(kb)
    // Same object -> same key, across calls and across renders (the WeakMap
    // and the callback identity both survive a re-render).
    expect(result.current(a)).toBe(ka)
    const before = result.current
    rerender()
    expect(result.current).toBe(before)
    expect(result.current(a)).toBe(ka)
    expect(result.current(b)).toBe(kb)
  })

  it('never keys by position: a prepend leaves every existing key untouched', () => {
    const { result } = renderHook(() => useStableMessageKey())
    const rows = [msg(), msg({ ts: 't1' }), msg()]
    const keys = rows.map(result.current)
    const prepended = [msg(), msg(), ...rows]
    expect(prepended.slice(2).map(result.current)).toEqual(keys)
  })
})
