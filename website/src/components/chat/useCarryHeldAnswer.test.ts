import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useCarryHeldAnswer, type FollowHandle } from './useCarryHeldAnswer'

/* Mate's live first text is held as one muted line, so its reply gets its
 * full height only when the line resolves at the end of the run, which the
 * follow core reads as idle growth and stops following. The hook carries a
 * reader who was following to the end of the resolved reply. */

function handle(following: boolean): FollowHandle & { scrollToBottom: ReturnType<typeof vi.fn> } {
  return { getFollow: () => following, scrollToBottom: vi.fn() }
}

describe('useCarryHeldAnswer', () => {
  it('re-pins a following reader once the held line resolves', () => {
    const list = handle(true)
    const { rerender } = renderHook(({ held }) => useCarryHeldAnswer(held, { current: list }), { initialProps: { held: true } })
    expect(list.scrollToBottom).not.toHaveBeenCalled()
    rerender({ held: false })
    expect(list.scrollToBottom).toHaveBeenCalledTimes(1)
    expect(list.scrollToBottom).toHaveBeenCalledWith('auto')
    // Once only: a later render with nothing held does not pin again.
    rerender({ held: false })
    expect(list.scrollToBottom).toHaveBeenCalledTimes(1)
  })

  it('leaves a reader who had scrolled up where they are', () => {
    const list = handle(false)
    const { rerender } = renderHook(({ held }) => useCarryHeldAnswer(held, { current: list }), { initialProps: { held: true } })
    rerender({ held: false })
    expect(list.scrollToBottom).not.toHaveBeenCalled()
  })

  it('does nothing in a chat that never held a line', () => {
    const list = handle(true)
    const { rerender } = renderHook(({ held }) => useCarryHeldAnswer(held, { current: list }), { initialProps: { held: false } })
    rerender({ held: false })
    expect(list.scrollToBottom).not.toHaveBeenCalled()
  })
})
