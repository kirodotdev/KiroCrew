/**
 * useContainerWidth attaches its ResizeObserver from a CALLBACK ref, not a
 * mount-time effect.
 *
 * The old shape read `ref.current` once in a mount-only effect; an element
 * rendered conditionally (behind query data or a collapsed branch) after that
 * first commit was never observed, so the width stayed null for the life of
 * the component — latent only while every caller attaches unconditionally.
 * These tests pin the callback-ref contract: attach-after-mount observes,
 * detach disconnects, and a zero-width report does not evict the
 * "assume wide" null.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import { useContainerWidth } from '../hooks/useContainerWidth'

class MockResizeObserver {
  static instances: MockResizeObserver[] = []
  observed: Element[] = []
  disconnected = false
  constructor(public callback: ResizeObserverCallback) {
    MockResizeObserver.instances.push(this)
  }
  observe(el: Element) {
    this.observed.push(el)
  }
  unobserve() {}
  disconnect() {
    this.disconnected = true
  }
}

/** Drive one observer callback with a content-box width, as layout would. */
function fire(ro: MockResizeObserver, width: number) {
  act(() => {
    ro.callback(
      [{ contentRect: { width } } as ResizeObserverEntry],
      ro as unknown as ResizeObserver,
    )
  })
}

function Probe({ show }: { show: boolean }) {
  const [ref, width] = useContainerWidth<HTMLDivElement>()
  return (
    <div>
      <span data-testid="width">{width === null ? 'null' : width}</span>
      {show ? <div data-testid="box" ref={ref} /> : null}
    </div>
  )
}

describe('useContainerWidth callback ref', () => {
  beforeEach(() => {
    MockResizeObserver.instances = []
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    cleanup()
  })

  it('observes an element that mounts AFTER the hook (conditional render flips on)', () => {
    const { rerender } = render(<Probe show={false} />)
    expect(screen.getByTestId('width').textContent).toBe('null')
    // The defect shape: a mount-time effect saw null once and never returned.
    expect(MockResizeObserver.instances).toHaveLength(0)

    rerender(<Probe show={true} />)
    const ro = MockResizeObserver.instances.at(-1)!
    expect(ro.observed).toContain(screen.getByTestId('box'))

    fire(ro, 640)
    expect(screen.getByTestId('width').textContent).toBe('640')
  })

  it('disconnects the observer when the element unmounts', () => {
    const { rerender } = render(<Probe show={true} />)
    const ro = MockResizeObserver.instances.at(-1)!
    expect(ro.disconnected).toBe(false)

    rerender(<Probe show={false} />)
    expect(ro.disconnected).toBe(true)
  })

  it('keeps null ("assume wide") when the observer reports a zero width', () => {
    render(<Probe show={true} />)
    const ro = MockResizeObserver.instances.at(-1)!
    fire(ro, 0)
    expect(screen.getByTestId('width').textContent).toBe('null')
    // A later real layout still lands.
    fire(ro, 512)
    expect(screen.getByTestId('width').textContent).toBe('512')
  })
})
