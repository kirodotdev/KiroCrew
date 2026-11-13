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
    const bottom = document.documentElement.clientHeight
    document.body.getBoundingClientRect = () => DOMRect.fromRect({ x: 0, y: 0, width: 800, height: bottom })
    const { result } = renderHook(() => useKeyboardLift(ref, true))
    reads = 0

    act(() => {
      for (let i = 0; i < 10; i++) for (const t of ['resize', 'scroll']) (listeners[t] || []).forEach(l => l())
    })
    expect(reads).toBe(0)
    await act(() => new Promise<void>(r => requestAnimationFrame(() => r())))
    expect(reads).toBe(1)
    expect(result.current).toBe(bottom > 400 ? bottom - 400 : 0)
  })

  /** A box whose bottom edge is `bottom`; the lift is padding inside it, so the edge stays. */
  function liftedBox(bottom: number) {
    const box = document.createElement('div')
    box.getBoundingClientRect = () => DOMRect.fromRect({ x: 0, y: bottom - 80, width: 800, height: 80 })
    const input = document.createElement('textarea')
    box.append(input)
    document.body.append(box)
    input.focus()
    return box
  }
  function installViewport(height: number, offsetTop: number) {
    const listeners: Record<string, Listener[]> = {}
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: {
        height, offsetTop, scale: 1,
        addEventListener: (t: string, l: Listener) => { (listeners[t] ||= []).push(l) },
        removeEventListener: () => {},
      },
    })
    return () => act(() => { for (const t of ['resize', 'scroll']) (listeners[t] || []).forEach(l => l()) })
  }
  const nextFrame = () => act(() => new Promise<void>(r => requestAnimationFrame(() => r())))

  it('lifts a box that sits above the layout bottom only by the part the keyboard covers', async () => {
    // Layout 1024 tall, a 300px docked terminal below the box, a 400px keyboard.
    installViewport(624, 0)
    const ref = { current: liftedBox(724) }
    const { result } = renderHook(() => useKeyboardLift(ref, true))
    await nextFrame()
    expect(result.current).toBe(100)
  })

  it('re-measures to the same lift instead of adding it up', async () => {
    const fire = installViewport(600, 0)
    const ref = { current: liftedBox(1024) }
    const { result } = renderHook(() => useKeyboardLift(ref, true))
    await nextFrame()
    expect(result.current).toBe(424)
    fire()
    await nextFrame()
    expect(result.current).toBe(424)
  })

  it('re-measures a box when a lift below it drops (focus moves from the terminal to the composer)', async () => {
    // Layout 1024 tall, a 424px keyboard. The terminal (300px) sits at the layout
    // bottom; the composer sits on top of it, pushed up further by the terminal's lift.
    installViewport(600, 0)
    const terminal = liftedBox(1024)
    let terminalLift = 0
    const composer = liftedBox(0)
    composer.getBoundingClientRect = () => {
      const bottom = 1024 - 300 - terminalLift
      return DOMRect.fromRect({ x: 0, y: bottom - 80, width: 800, height: 80 })
    }
    const terminalRef = { current: terminal }
    const composerRef = { current: composer }
    act(() => { terminal.querySelector('textarea')!.focus() })
    const { result } = renderHook(() => {
      terminalLift = useKeyboardLift(terminalRef, true)
      return { terminal: terminalLift, composer: useKeyboardLift(composerRef, true) }
    })
    await nextFrame()
    expect(result.current).toEqual({ terminal: 424, composer: 0 })

    act(() => { composer.querySelector('textarea')!.focus() })
    await nextFrame()
    await nextFrame()
    expect(result.current).toEqual({ terminal: 0, composer: 124 })
  })
})
