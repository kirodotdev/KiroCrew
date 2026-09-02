/**
 * useSmoothStream: content that GROWS after the stream completes.
 *
 * Once a message finishes, the rAF drain loop stops itself (raf = 0 when
 * !streaming && caughtUp) and never restarts (deps [enabled, speed]). A later
 * content change while not streaming — a variant switch to a longer answer, or
 * a post-completion patch — was permanently truncated to the old emitLen. A
 * non-streaming content change should render instantly.
 */
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSmoothStream } from '../hooks/useSmoothStream'

describe('useSmoothStream grow-after-complete', () => {
  it('renders the full new content when it grows while not streaming', async () => {
    const { result, rerender } = renderHook(
      ({ content, streaming }) => useSmoothStream(content, streaming, true, 1),
      { initialProps: { content: 'ok', streaming: false } },
    )
    // Completed short message renders fully.
    expect(result.current).toBe('ok')

    // Variant switch to a longer, already-complete answer (streaming stays false).
    const longer = 'a much longer regenerated answer'
    await act(async () => {
      rerender({ content: longer, streaming: false })
      await Promise.resolve()
    })

    // Must show the full new content, not a truncation to the old length (2).
    expect(result.current).toBe(longer)
  })

  it('renders a post-completion shrink (variant switch to shorter) fully too', async () => {
    const { result, rerender } = renderHook(
      ({ content, streaming }) => useSmoothStream(content, streaming, true, 1),
      { initialProps: { content: 'a much longer first answer', streaming: false } },
    )
    expect(result.current).toBe('a much longer first answer')

    await act(async () => {
      rerender({ content: 'short', streaming: false })
      await Promise.resolve()
    })
    expect(result.current).toBe('short')
  })

  it('passthrough when disabled is unchanged', () => {
    const { result } = renderHook(() => useSmoothStream('anything', false, false, 1))
    expect(result.current).toBe('anything')
  })
})
