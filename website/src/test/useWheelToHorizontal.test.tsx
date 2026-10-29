import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { useWheelToHorizontal, WHEEL_LINE_PX } from '../hooks/useWheelToHorizontal'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

interface Geometry {
  clientWidth: number
  scrollWidth: number
  scrollLeft: number
}

function stubGeometry(el: HTMLElement, g: Geometry): { get: () => number } {
  let left = g.scrollLeft
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => g.clientWidth })
  Object.defineProperty(el, 'scrollWidth', { configurable: true, get: () => g.scrollWidth })
  Object.defineProperty(el, 'scrollLeft', {
    configurable: true,
    get: () => left,
    set: (v: number) => { left = v },
  })
  return { get: () => left }
}

function Strip({ tick, which = 'a' }: { tick?: number, which?: 'a' | 'b' }) {
  const ref = useWheelToHorizontal<HTMLDivElement>()
  return (
    <div data-testid="wrap" data-tick={tick}>
      {which === 'a'
        ? <div key="a" data-testid="a" ref={ref} />
        : <div key="b" data-testid="b" ref={ref} />}
    </div>
  )
}

function wheel(init: WheelEventInit): WheelEvent {
  return new WheelEvent('wheel', { bubbles: true, cancelable: true, ...init })
}

function setup(g: Geometry = { clientWidth: 100, scrollWidth: 400, scrollLeft: 100 }) {
  const view = render(<Strip />)
  const node = view.getByTestId('a')
  const pos = stubGeometry(node, g)
  return { view, node, pos }
}

describe('useWheelToHorizontal', () => {
  it('moves a pixel event by exactly deltaY and cancels it', () => {
    const { node, pos } = setup()
    const e = wheel({ deltaY: 40, deltaMode: 0 })
    node.dispatchEvent(e)
    expect(pos.get()).toBe(140)
    expect(e.defaultPrevented).toBe(true)
  })

  it('moves a line event by deltaY times WHEEL_LINE_PX', () => {
    const { node, pos } = setup()
    node.dispatchEvent(wheel({ deltaY: 3, deltaMode: 1 }))
    expect(WHEEL_LINE_PX).toBe(16)
    expect(pos.get()).toBe(148)
  })

  it('moves a page event by deltaY times clientWidth', () => {
    const { node, pos } = setup()
    node.dispatchEvent(wheel({ deltaY: 1, deltaMode: 2 }))
    expect(pos.get()).toBe(200)
  })

  it('moves backwards for a negative deltaY', () => {
    const { node, pos } = setup()
    node.dispatchEvent(wheel({ deltaY: -40 }))
    expect(pos.get()).toBe(60)
  })

  it('clamps to the legal range and still cancels a partial move', () => {
    const { node, pos } = setup({ clientWidth: 100, scrollWidth: 400, scrollLeft: 290 })
    const down = wheel({ deltaY: 40 })
    node.dispatchEvent(down)
    expect(pos.get()).toBe(300)
    expect(down.defaultPrevented).toBe(true)
    const up = wheel({ deltaY: -1000 })
    node.dispatchEvent(up)
    expect(pos.get()).toBe(0)
    expect(up.defaultPrevented).toBe(true)
  })

  it('does not cancel a downward event at the end', () => {
    const { node, pos } = setup({ clientWidth: 100, scrollWidth: 400, scrollLeft: 300 })
    const down = wheel({ deltaY: 40 })
    node.dispatchEvent(down)
    expect(pos.get()).toBe(300)
    expect(down.defaultPrevented).toBe(false)
  })

  it('does not cancel an upward event at the start', () => {
    const { node, pos } = setup({ clientWidth: 100, scrollWidth: 400, scrollLeft: 0 })
    const up = wheel({ deltaY: -40 })
    node.dispatchEvent(up)
    expect(pos.get()).toBe(0)
    expect(up.defaultPrevented).toBe(false)
  })

  it('ignores an event with a nonzero deltaX and does not cancel it', () => {
    const { node, pos } = setup()
    const e = wheel({ deltaY: 40, deltaX: 1 })
    node.dispatchEvent(e)
    expect(pos.get()).toBe(100)
    expect(e.defaultPrevented).toBe(false)
  })

  it('ignores a zero deltaY', () => {
    const { node, pos } = setup()
    const e = wheel({ deltaY: 0 })
    node.dispatchEvent(e)
    expect(pos.get()).toBe(100)
    expect(e.defaultPrevented).toBe(false)
  })

  it.each([
    ['shiftKey'],
    ['ctrlKey'],
    ['altKey'],
    ['metaKey'],
  ] as const)('ignores a %s-modified event', key => {
    const { node, pos } = setup()
    const e = wheel({ deltaY: 40 })
    // happy-dom's WheelEvent ignores modifier keys in its init dict, so set
    // the flag on the instance. The precondition below proves it took.
    Object.defineProperty(e, key, { configurable: true, value: true })
    expect(e[key]).toBe(true)
    node.dispatchEvent(e)
    expect(pos.get()).toBe(100)
    expect(e.defaultPrevented).toBe(false)
  })

  it('ignores an event an earlier listener already cancelled', () => {
    const { view, node, pos } = setup()
    // A capture listener on an ancestor runs before the node's own listener.
    view.getByTestId('wrap').addEventListener('wheel', ev => ev.preventDefault(), { capture: true })
    node.dispatchEvent(wheel({ deltaY: 40 }))
    expect(pos.get()).toBe(100)
  })

  it('ignores a scroller that does not overflow', () => {
    const { node, pos } = setup({ clientWidth: 100, scrollWidth: 100, scrollLeft: 0 })
    const e = wheel({ deltaY: 40 })
    node.dispatchEvent(e)
    expect(pos.get()).toBe(0)
    expect(e.defaultPrevented).toBe(false)
  })

  it('does not stack listeners across re-renders', () => {
    const view = render(<Strip tick={0} />)
    const node = view.getByTestId('a')
    const pos = stubGeometry(node, { clientWidth: 100, scrollWidth: 400, scrollLeft: 100 })
    const adds = vi.spyOn(node, 'addEventListener')
    const removes = vi.spyOn(node, 'removeEventListener')
    for (let i = 1; i <= 5; i++) view.rerender(<Strip tick={i} />)
    expect(view.getByTestId('wrap').dataset.tick).toBe('5')
    // Stacked listeners would hide behind the defaultPrevented guard (the
    // first cancels, the rest bail), so count the net wheel listeners too.
    const net = adds.mock.calls.filter(c => c[0] === 'wheel').length
      - removes.mock.calls.filter(c => c[0] === 'wheel').length
    expect(net).toBe(0)
    node.dispatchEvent(wheel({ deltaY: 10 }))
    expect(pos.get()).toBe(110)
  })

  it('removes the listener on unmount', () => {
    const { view, node } = setup()
    const spy = vi.spyOn(node, 'removeEventListener')
    view.unmount()
    expect(spy).toHaveBeenCalledWith('wheel', expect.any(Function))
  })

  it('detaches from the old node when the ref moves to a new one', () => {
    const view = render(<Strip which="a" />)
    const oldNode = view.getByTestId('a')
    const oldPos = stubGeometry(oldNode, { clientWidth: 100, scrollWidth: 400, scrollLeft: 100 })
    view.rerender(<Strip which="b" />)
    const newNode = view.getByTestId('b')
    const newPos = stubGeometry(newNode, { clientWidth: 100, scrollWidth: 400, scrollLeft: 100 })
    const stale = wheel({ deltaY: 40 })
    oldNode.dispatchEvent(stale)
    expect(oldPos.get()).toBe(100)
    expect(stale.defaultPrevented).toBe(false)
    newNode.dispatchEvent(wheel({ deltaY: 40 }))
    expect(newPos.get()).toBe(140)
  })
})
