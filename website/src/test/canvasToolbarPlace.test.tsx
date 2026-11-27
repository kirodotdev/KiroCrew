/**
 * The selection toolbar's placement re-measures only when the measured widths
 * change, so swapping one toolbar for a narrower one settles instead of looping.
 */
import { afterEach, beforeEach, expect, it } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { emptyCanvas, type Canvas, type Shape } from '../components/canvas/canvasModel'

const shape = (id: string, kind: Shape['kind'], x: number): Shape =>
  ({ id, kind, x, y: 0, w: 200, h: 120, text: 'hello', color: kind === 'sticky' ? 'yellow' : 'sky' })

function Harness({ initial }: { initial: Canvas }) {
  const [board, setBoard] = useState(initial)
  return <div style={{ width: 1200, height: 800 }}><CanvasEditor canvas={board} onChange={setBoard} /></div>
}

const at = (x: number, y: number) => ({ clientX: x + 120, clientY: y + 100, button: 0, pointerId: 1 })

// jsdom has no layout: give the canvas a width and each toolbar its own, as a
// browser does, so the toolbar's placement has something to measure.
const saved: Record<string, PropertyDescriptor | undefined> = {}
const realObserver = globalThis.ResizeObserver
/**
 * A browser reports an observed element after it is laid out, outside React's
 * own update; `reportLayout` plays that report for every pending observation.
 */
let pending: (() => void)[] = []
class LayoutObserver {
  constructor(private readonly cb: ResizeObserverCallback) {}
  observe() { pending.push(() => this.cb([], this as unknown as ResizeObserver)) }
  unobserve() {}
  disconnect() {}
}
const reportLayout = () => { const due = pending; pending = []; for (const report of due) report() }
beforeEach(() => {
  pending = []
  globalThis.ResizeObserver = LayoutObserver as unknown as typeof ResizeObserver
  for (const key of ['clientWidth', 'offsetWidth']) saved[key] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key)
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get() { return 1200 } })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (this.dataset.testid !== 'canvas-toolbar-place') return 0
      return this.querySelector('[data-testid="canvas-text-toolbar"]') ? 317 : 370
    },
  })
})
afterEach(() => {
  globalThis.ResizeObserver = realObserver
  for (const key of ['clientWidth', 'offsetWidth']) {
    const d = saved[key]
    if (d) Object.defineProperty(HTMLElement.prototype, key, d)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[key]
  }
})

it('right-clicking a shape of another kind swaps the toolbar and opens the menu without an update loop', () => {
  render(<Harness initial={{ ...emptyCanvas(), shapes: [shape('a', 'rect', 0), shape('b', 'text', 400)] }} />)
  const canvas = screen.getByTestId('canvas-editor')
  act(() => { fireEvent.pointerDown(canvas, at(100, 60)) })
  act(() => { fireEvent.pointerUp(window, at(100, 60)) })
  act(() => { reportLayout() })
  expect(screen.getByTestId('canvas-toolbar')).toBeTruthy()
  // The text box is one line tall, so the click lands near its top.
  act(() => { fireEvent.contextMenu(canvas, at(500, 10)) })
  act(() => { reportLayout() })
  expect(screen.getByTestId('canvas-context-menu')).toBeTruthy()
  expect(screen.getByTestId('canvas-text-toolbar')).toBeTruthy()
})
