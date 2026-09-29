/**
 * useRecallHistory — the composer-facing wrapper around `buildRecallHistory`.
 *
 * The merge itself is pinned by the builder's own tests. What is only observable
 * through the hook is the array IDENTITY, which ChatInput takes as a
 * `useCallback` dependency — and since the main chat and the grid pane mount that
 * same input, these are the properties that would drift if either composer grew
 * its own spelling of this.
 */
import { describe, expect, it } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useRecallHistory } from './useRecallHistory'

type Row = { role: string; content: string; meta?: { sendId?: string } }
const userRow = (content: string, sendId?: string): Row => ({ role: 'user', content, ...(sendId ? { meta: { sendId } } : {}) })

describe('useRecallHistory', () => {
  it('returns the previous array when a new messages reference carries the same prompts', () => {
    const { result, rerender } = renderHook(
      ({ messages }: { messages: Row[] }) => useRecallHistory(messages, 'slot-a'),
      { initialProps: { messages: [userRow('one')] } },
    )
    const first = result.current.history
    expect(first).toEqual(['one'])
    rerender({ messages: [userRow('one')] })
    expect(result.current.history).toBe(first)
  })

  it('returns a fresh array once the prompts themselves change', () => {
    const { result, rerender } = renderHook(
      ({ messages }: { messages: Row[] }) => useRecallHistory(messages, 'slot-a'),
      { initialProps: { messages: [userRow('one')] } },
    )
    const first = result.current.history
    rerender({ messages: [userRow('one'), userRow('two')] })
    expect(result.current.history).not.toBe(first)
    expect(result.current.history).toEqual(['one', 'two'])
  })

  it('does not hand one slot the array it built for another', () => {
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useRecallHistory([userRow('shared')], slot),
      { initialProps: { slot: 'slot-a' } },
    )
    const first = result.current.history
    // Matching length AND tail, so the element-wise compare alone would reuse it
    // and offer one conversation the other's prompt.
    rerender({ slot: 'slot-b' })
    expect(result.current.history).not.toBe(first)
  })

  it('offers an unlanded attempt at the tail, where the first ArrowUp lands', () => {
    const { result } = renderHook(() => useRecallHistory(
      [userRow('landed', 's-1')],
      'slot-a',
      [{ text: 'landed', sendId: 's-1' }, { text: 'lost', sendId: 's-2' }],
    ))
    expect(result.current.history).toEqual(['landed', 'lost'])
  })

  it('exposes the attempt behind each position, and null for a transcript prompt', () => {
    const { result } = renderHook(() => useRecallHistory(
      [userRow('landed', 's-1')],
      'slot-a',
      [{ text: 'lost', sendId: 's-2', files: ['a.txt'] }],
    ))
    const { history, entries } = result.current
    // Aligned with `history`, so ChatInput's index identifies the attempt without
    // matching text — the spelling a send persists differs from the composer's.
    expect(entries.map((e) => e.text)).toEqual(history)
    expect(entries[0].attempt).toBeNull()
    expect(entries[1].attempt?.sendId).toBe('s-2')
    expect(entries[1].attempt?.files).toEqual(['a.txt'])
  })
})
