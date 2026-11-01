/**
 * useKeyboardLift reads the viewport at most once per animation frame: iOS
 * fires a resize and a scroll event on every frame of the keyboard animation.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useKeyboardLift } from './useKeyboardLift'

type Listener = () => void

describe('useKeyboardLift', () => {
  const originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
  afterEach(() => {
    if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
    else delete (window as { visualViewport?: VisualViewport }).visualViewport
    document.body.replaceChildren()
  })

  it('folds a burst of viewport events into one read per frame', async () => {
    const listeners: Record<string, Listener[]> = {}
    let reads = 0
    const vv = {
      get height() { reads += 1; return 400 },
      offsetTop: 0,
      addEventListener: (t: string, l: Listener) => { (listeners[t] ||= []).push(l) },
      removeEventListener: () => {},
    }
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
    const input = document.createElement('textarea')
    document.body.append(input)
    input.focus()
    const ref = { current: document.body }
    const { result } = renderHook(() => useKeyboardLift(ref, true))
    reads = 0

    act(() => {
      for (let i = 0; i < 10; i++) for (const t of ['resize', 'scroll']) (listeners[t] || []).forEach(l => l())
    })
    expect(reads).toBe(0)
    await act(() => new Promise<void>(r => requestAnimationFrame(() => r())))
    expect(reads).toBe(1)
    expect(result.current).toBe(document.documentElement.clientHeight > 400 ? document.documentElement.clientHeight - 400 : 0)
  })
})
