import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { usePaneForwardPending } from './paneForward'

type Answer = Record<string, 'up' | 'down'> | null

function installBridge() {
  const answers: Array<{ ports: number[]; resolve: (answer: Answer) => void }> = []
  const forwardPorts = vi.fn(
    (ports: number[]) => new Promise<Answer>(resolve => { answers.push({ ports, resolve }) }),
  )
  window.remoteCrewPaneAPI = { forwardPorts }
  return { forwardPorts, answers }
}

afterEach(() => { delete window.remoteCrewPaneAPI })

describe('usePaneForwardPending', () => {
  it('never holds a pane without the desktop bridge', () => {
    const { result } = renderHook(() => usePaneForwardPending([7778]))
    expect(result.current(7778)).toBe(false)
  })

  it('holds each new port until the bridge has answered for it', async () => {
    const { forwardPorts, answers } = installBridge()
    const { result, rerender } = renderHook(({ ports }) => usePaneForwardPending(ports), {
      initialProps: { ports: [7778] },
    })
    expect(result.current(7778)).toBe(true)
    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    await act(async () => { answers[0].resolve({ 7778: 'up' }) })
    expect(result.current(7778)).toBe(false)

    // A second pane is held on its own; the first stays loaded.
    rerender({ ports: [7778, 7779] })
    expect(result.current(7779)).toBe(true)
    expect(result.current(7778)).toBe(false)
    await waitFor(() => expect(forwardPorts).toHaveBeenLastCalledWith([7778, 7779]))
    await act(async () => { answers[answers.length - 1].resolve({ 7778: 'up', 7779: 'up' }) })
    expect(result.current(7779)).toBe(false)
  })

  it('holds a port shown again, and an answer about its earlier forward does not release it', async () => {
    const { answers } = installBridge()
    const { result, rerender } = renderHook(({ ports }) => usePaneForwardPending(ports), {
      initialProps: { ports: [7778] },
    })
    await waitFor(() => expect(answers).toHaveLength(1))
    rerender({ ports: [] })
    await waitFor(() => expect(answers).toHaveLength(2))
    rerender({ ports: [7778] })
    await waitFor(() => expect(answers).toHaveLength(3))
    expect(result.current(7778)).toBe(true)

    // The first request's late answer belongs to a forward that was dropped.
    await act(async () => { answers[0].resolve({ 7778: 'down' }) })
    expect(result.current(7778)).toBe(true)
    await act(async () => { answers[2].resolve({ 7778: 'up' }) })
    expect(result.current(7778)).toBe(false)
  })

  it('releases a port whose request the shell refused', async () => {
    const forwardPorts = vi.fn(() => Promise.reject(new Error('restricted')))
    window.remoteCrewPaneAPI = { forwardPorts }
    const { result } = renderHook(() => usePaneForwardPending([7778]))
    await waitFor(() => expect(result.current(7778)).toBe(false))
  })
})
