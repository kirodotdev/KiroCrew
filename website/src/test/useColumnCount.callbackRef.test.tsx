/**
 * useColumnCount attaches its ResizeObserver from a CALLBACK ref, not a
 * mount-time effect (#7193).
 *
 * The old effect read `ref.current` once on mount with `[minColWidth]` deps;
 * when the grid was rendered conditionally (behind query data or a view-mode
 * branch, e.g. ArtifactsPage's `view === 'grid'`), `ref.current` was null at
 * that moment, the effect returned early, and the observer never attached —
 * so the viewport seed was never corrected for the life of the component.
 * These tests pin the callback-ref contract: attach-after-mount corrects the
 * seed, detach disconnects, the SSR seed still holds, and a `minColWidth`
 * change re-measures with the current value.
 *
 * happy-dom has no layout (clientWidth is 0) and no ResizeObserver, so both
 * are stubbed here; the width each measure() sees is driven through a
 * controllable `clientWidth` getter on HTMLElement.prototype.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { renderToString } from 'react-dom/server'
import { useColumnCount } from '../hooks/useColumnCount'

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

let mockClientWidth = 0
// Captured from the SAME object the stub below is installed on: a descriptor
// read elsewhere on the chain would come back undefined and the restore in
// afterEach would silently take the delete branch.
const originalClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')

function Probe({ show, minColWidth = 300 }: { show: boolean; minColWidth?: number }) {
  const [ref, cols] = useColumnCount(minColWidth)
  return (
    <div>
      <span data-testid="cols">{cols}</span>
      {show ? <div data-testid="grid" ref={ref} /> : null}
    </div>
  )
}

describe('useColumnCount callback ref (#7193)', () => {
  beforeEach(() => {
    MockResizeObserver.instances = []
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true, writable: true })
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get: () => mockClientWidth,
    })
  })

  afterEach(() => {
    // Unstub BEFORE cleanup: the SSR test stubs `window` to undefined, and RTL
    // teardown must run against the real one.
    vi.unstubAllGlobals()
    cleanup()
    if (originalClientWidth) {
      Object.defineProperty(HTMLElement.prototype, 'clientWidth', originalClientWidth)
    } else {
      delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
    }
  })

  it('corrects the seed when the grid mounts AFTER the hook (conditional render flips on)', () => {
    // Seed at 1280px viewport: floor((1280 - 32) / 300) = 4.
    mockClientWidth = 950 // measured: floor(950 / 300) = 3
    const { rerender } = render(<Probe show={false} />)
    expect(screen.getByTestId('cols').textContent).toBe('4')
    // The defect: with a mount-time effect, nothing ever observed this element.
    expect(MockResizeObserver.instances).toHaveLength(0)

    rerender(<Probe show={true} />)
    // The callback ref fired on attach: measured immediately, then observed.
    expect(screen.getByTestId('cols').textContent).toBe('3')
    const ro = MockResizeObserver.instances.at(-1)!
    expect(ro.observed).toContain(screen.getByTestId('grid'))
  })

  it('disconnects the observer when the grid unmounts (conditional render flips off)', () => {
    mockClientWidth = 950
    const { rerender } = render(<Probe show={true} />)
    const ro = MockResizeObserver.instances.at(-1)!
    expect(ro.disconnected).toBe(false)

    rerender(<Probe show={false} />)
    expect(ro.disconnected).toBe(true)
  })

  it('re-measures with the current minColWidth when it changes', () => {
    mockClientWidth = 950
    const { rerender } = render(<Probe show={true} minColWidth={300} />)
    expect(screen.getByTestId('cols').textContent).toBe('3') // floor(950/300)

    rerender(<Probe show={true} minColWidth={450} />)
    // New callback identity -> React detached the old ref and attached the new
    // one, which measured with the new width: floor(950/450) = 2.
    expect(screen.getByTestId('cols').textContent).toBe('2')
    // The stale observer was disconnected, a fresh one attached.
    expect(MockResizeObserver.instances.at(0)!.disconnected).toBe(true)
    expect(MockResizeObserver.instances.at(-1)!.observed).toHaveLength(1)
  })

  it('keeps the seed when the attached element has no layout yet (clientWidth 0)', () => {
    // happy-dom/jsdom have no layout, and a real element can attach while
    // hidden: a measured width of 0 must not collapse the masonry to 1 column.
    mockClientWidth = 0
    render(<Probe show={true} />)
    expect(screen.getByTestId('cols').textContent).toBe('4') // viewport seed survives
    expect(MockResizeObserver.instances.at(-1)!.observed).toHaveLength(1) // still observing
  })

  it('keeps the SSR seed of 2 when window is undefined', () => {
    vi.stubGlobal('window', undefined)
    const html = renderToString(<Probe show={false} />)
    expect(html).toContain('>2</span>')
  })
})
