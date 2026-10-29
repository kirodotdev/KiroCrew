/**
 * `useTranscriptReactions`: the page-level wrapper over `useChatReactions`.
 * It forwards the active slot (null -> undefined), passes the per-message
 * toggle through, and surfaces a reaction error only on the message AND slot
 * it happened on — a fork copies a message's `mid`, so a `mid` match alone
 * would show slot A's error on the copy in slot B.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactionError } from '../hooks/useChatReactions'

const hookState = {
  reactions: [],
  loading: false,
  loadFailed: false,
  reactionsFor: vi.fn(),
  toggleReaction: vi.fn(),
  error: null as ReactionError | null,
  clearError: vi.fn(),
}
const useChatReactions = vi.fn((_slot: string | undefined) => hookState)

vi.mock('../hooks/useChatReactions', () => ({
  useChatReactions: (slot: string | undefined) => useChatReactions(slot),
}))

import { useTranscriptReactions } from '../pages/chat/page/transcriptReactions'

beforeEach(() => {
  vi.clearAllMocks()
  hookState.error = null
  hookState.loadFailed = false
})

describe('useTranscriptReactions', () => {
  it('passes the active slot to useChatReactions, and undefined when there is none', () => {
    renderHook(() => useTranscriptReactions('slot-a'))
    expect(useChatReactions).toHaveBeenLastCalledWith('slot-a')
    renderHook(() => useTranscriptReactions(null))
    expect(useChatReactions).toHaveBeenLastCalledWith(undefined)
  })

  it('forwards a per-message toggle to toggleReaction', () => {
    hookState.toggleReaction.mockResolvedValue(undefined)
    const { result } = renderHook(() => useTranscriptReactions('slot-a'))
    result.current.handleToggleReactionForMessage('m1', '👍')
    expect(hookState.toggleReaction).toHaveBeenCalledWith('m1', '👍')
  })

  it('consumes a failed toggle so it never surfaces as an unhandled rejection', async () => {
    // The row components call this through a `(emoji) => void` prop and never
    // await it. A rejection that escaped here would be unhandled in the
    // browser; the hook's `error` state is what the user sees instead.
    const unhandled: unknown[] = []
    const onUnhandled = (reason: unknown) => { unhandled.push(reason) }
    process.on('unhandledRejection', onUnhandled)
    try {
      hookState.toggleReaction.mockRejectedValue(new Error('zzz-toggle-failed'))
      const { result } = renderHook(() => useTranscriptReactions('slot-a'))
      expect(result.current.handleToggleReactionForMessage('m1', '👍')).toBeUndefined()
      // Give Node two macrotask turns to report an unhandled rejection.
      await new Promise(r => setTimeout(r, 0))
      await new Promise(r => setTimeout(r, 0))
      expect(hookState.toggleReaction).toHaveBeenCalledWith('m1', '👍')
      expect(unhandled).toEqual([])
    } finally {
      process.off('unhandledRejection', onUnhandled)
    }
  })

  it('surfaces an error only when both the slot and the mid match', () => {
    hookState.error = { slot: 'slot-a', mid: 'm1', emoji: '👍', kind: 'limit' }
    const inA = renderHook(() => useTranscriptReactions('slot-a')).result.current
    expect(inA.reactionErrorFor('m1')).toEqual(hookState.error)
    expect(inA.reactionErrorFor('m2')).toBeNull()
    // Same mid in a fork (different slot): no error shown there.
    const inB = renderHook(() => useTranscriptReactions('slot-b')).result.current
    expect(inB.reactionErrorFor('m1')).toBeNull()
  })

  it('reports no error when there is none, and exposes load failure and clearError', () => {
    hookState.loadFailed = true
    const { result } = renderHook(() => useTranscriptReactions('slot-a'))
    expect(result.current.reactionErrorFor('m1')).toBeNull()
    expect(result.current.reactionsLoadFailed).toBe(true)
    expect(result.current.clearReactionError).toBe(hookState.clearError)
  })
})
