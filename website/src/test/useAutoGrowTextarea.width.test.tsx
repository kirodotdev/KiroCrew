import React, { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'

/* The hook's width path (#9979): a window resize or a pane folding beside the
 * field changes the column the text wraps in with no value change, so the
 * value-keyed measure never runs. A ResizeObserver covers it — and because
 * `measure` writes the height that observer also sees, the callback must bail
 * unless the WIDTH moved. happy-dom has neither ResizeObserver nor layout, so
 * both are stubbed: the observer is fired by hand, and the element's box is
 * given by getters the test moves. */

function Box({ value }: { value: string }) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useAutoGrowTextarea(ref, value, 200)
  return <textarea ref={ref} data-testid="ta" aria-label="auto-grow probe" />
}

type Instance = { cb: ResizeObserverCallback; targets: Set<Element> }

function stubResizeObserver() {
  const instances = new Set<Instance>()
  const RO = class {
    cb: ResizeObserverCallback
    targets = new Set<Element>()
    constructor(cb: ResizeObserverCallback) { this.cb = cb; instances.add(this) }
    observe(el: Element) { this.targets.add(el) }
    unobserve(el: Element) { this.targets.delete(el) }
    disconnect() { instances.delete(this) }
  } as unknown as typeof ResizeObserver
  vi.stubGlobal('ResizeObserver', RO)
  return instances
}

/** Give the textarea a layout box: `measure` declines an element with none. */
function layOut(el: HTMLTextAreaElement, box: { width: number; scrollHeight: number }) {
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => box.width })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => box.scrollHeight })
  Object.defineProperty(el, 'offsetParent', { configurable: true, get: () => document.body })
}

afterEach(() => { vi.unstubAllGlobals() })

describe('useAutoGrowTextarea — width changes', () => {
  it('re-measures when the width changes at an unchanged value, and not otherwise', () => {
    const instances = stubResizeObserver()
    const { getByTestId } = render(<Box value="two lines of text" />)
    const ta = getByTestId('ta') as HTMLTextAreaElement
    const mine = [...instances].filter((i) => i.targets.has(ta))
    expect(mine.length).toBe(1)
    const fire = () => { for (const i of mine) i.cb([], {} as ResizeObserver) }

    // Mount measured nothing: happy-dom gives the element no box.
    expect(ta.style.height).toBe('')
    const box = { width: 400, scrollHeight: 60 }
    layOut(ta, box)

    // The observer baselined the width at mount (0), so the first notification
    // at 400 IS a width change: one measurement, no value change.
    fire()
    expect(ta.style.height).toBe('60px')

    // Height moved but width did not (the measure's own write): no re-measure,
    // so a taller scrollHeight is NOT picked up here...
    box.scrollHeight = 90
    fire()
    expect(ta.style.height).toBe('60px')

    // ...until the width moves: the column narrowed, the text wraps taller.
    box.width = 200
    fire()
    expect(ta.style.height).toBe('90px')
  })

  it('caps at maxH and switches the overflow on, like the value path', () => {
    const instances = stubResizeObserver()
    const { getByTestId } = render(<Box value="long" />)
    const ta = getByTestId('ta') as HTMLTextAreaElement
    const mine = [...instances].filter((i) => i.targets.has(ta))
    layOut(ta, { width: 120, scrollHeight: 500 })
    for (const i of mine) i.cb([], {} as ResizeObserver)
    expect(ta.style.height).toBe('200px')
    expect(ta.style.overflowY).toBe('auto')
  })

  it('disconnects the observer on unmount', () => {
    const instances = stubResizeObserver()
    const { unmount } = render(<Box value="x" />)
    expect(instances.size).toBe(1)
    unmount()
    expect(instances.size).toBe(0)
  })
})
