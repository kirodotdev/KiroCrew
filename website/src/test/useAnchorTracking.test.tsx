import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { useRef } from 'react'
import { useAnchorTracking } from '../hooks/useAnchorTracking'

/** Fake ResizeObserver: records what is observed and lets a test fire callbacks. */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  observed: Element[] = []
  disconnected = false
  constructor(private cb: ResizeObserverCallback) { FakeResizeObserver.instances.push(this) }
  observe(el: Element) { this.observed.push(el) }
  unobserve() {}
  disconnect() { this.disconnected = true }
  fire() { this.cb([], this as unknown as ResizeObserver) }
}

const originalRO = window.ResizeObserver
beforeEach(() => {
  FakeResizeObserver.instances = []
  window.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
})
afterEach(() => { window.ResizeObserver = originalRO })

function Host({ open, onRender }: { open: boolean; onRender: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useAnchorTracking(open, ref)
  onRender()
  return <section data-testid="outer"><div><div ref={ref} data-testid="anchor" /></div></section>
}

describe('useAnchorTracking', () => {
  it('observes the anchor and every ancestor below body while open', () => {
    const { getByTestId } = render(<Host open onRender={() => {}} />)
    const ro = FakeResizeObserver.instances[0]
    const anchor = getByTestId('anchor')
    expect(ro.observed[0]).toBe(anchor)
    expect(ro.observed).toContain(getByTestId('outer'))
    expect(ro.observed).not.toContain(document.body)
  })

  it('skips the first callback and re-renders once on each later one', () => {
    const onRender = vi.fn()
    render(<Host open onRender={onRender} />)
    const ro = FakeResizeObserver.instances[0]
    const base = onRender.mock.calls.length
    act(() => ro.fire())
    expect(onRender).toHaveBeenCalledTimes(base)
    act(() => ro.fire())
    expect(onRender).toHaveBeenCalledTimes(base + 1)
  })

  it('re-renders on a window resize after one animation frame', () => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })
    try {
      const onRender = vi.fn()
      render(<Host open onRender={onRender} />)
      act(() => { vi.advanceTimersToNextFrame() })
      const base = onRender.mock.calls.length
      act(() => {
        window.dispatchEvent(new Event('resize'))
        vi.advanceTimersToNextFrame()
      })
      expect(onRender).toHaveBeenCalledTimes(base + 1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('disconnects when the picker closes', () => {
    const { rerender } = render(<Host open onRender={() => {}} />)
    const ro = FakeResizeObserver.instances[0]
    rerender(<Host open={false} onRender={() => {}} />)
    expect(ro.disconnected).toBe(true)
  })

  it('observes nothing while closed', () => {
    render(<Host open={false} onRender={() => {}} />)
    expect(FakeResizeObserver.instances).toHaveLength(0)
  })
})
