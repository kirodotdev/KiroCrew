/**
 * A canvas text box's height follows its text: measured on screen, written to
 * the file only when the box itself is edited.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { emptyCanvas, type Canvas, type Shape } from '../components/canvas/canvasModel'

/**
 * jsdom lays nothing out, so the measuring element gets a stand-in layout: each
 * character half the font size wide, lines wrapping at the box's inner width.
 */
function lineLayout(this: HTMLElement): number {
  if (this.getAttribute('aria-hidden') !== 'true' || this.style.visibility !== 'hidden') return 0
  const px = parseFloat(this.style.fontSize)
  const inner = parseFloat(this.style.width) - 4
  const perLine = Math.max(1, Math.floor(inner / (px / 2)))
  const lines = (this.textContent ?? '').split('\n').reduce((n, p) => n + Math.max(1, Math.ceil(p.length / perLine)), 0)
  return lines * px * 1.3 + 8
}

/** 20px text, one line: 20 * 1.3 + 8. */
const LINE = 34
const ROW = 26
const text = (id: string, x: number, y: number, body: string, h = 40): Shape => ({ id, kind: 'text', x, y, w: 220, h, text: body, color: 'smoke' })
const sticky = (id: string, x: number, y: number, body = ''): Shape => ({ id, kind: 'sticky', x, y, w: 200, h: 200, text: body, color: 'yellow' })

let last: Canvas
function Harness({ initial, onSave }: { initial: Canvas; onSave?: (c: Canvas) => void }) {
  const [board, setBoard] = useState(initial)
  last = board
  return (
    <div style={{ width: 1200, height: 800 }}>
      <CanvasEditor canvas={board} onChange={c => { onSave?.(c); setBoard(c) }} />
    </div>
  )
}
const canvas = () => screen.getByTestId('canvas-editor')
const at = (x: number, y: number) => ({ clientX: x + 120, clientY: y + 100, button: 0, pointerId: 1 })
const press = (x: number, y: number) => act(() => { fireEvent.pointerDown(canvas(), at(x, y)) })
const moveTo = (x: number, y: number) => act(() => { fireEvent.pointerMove(window, at(x, y)) })
const release = (x: number, y: number) => act(() => { fireEvent.pointerUp(window, at(x, y)) })
const shown = (id: string) => (screen.getAllByTestId('canvas-shape').find(el => el.dataset.shapeId === id) as HTMLElement)
/** The selection ring: the one element drawn with the accent's 3px border. */
const ring = () => [...document.querySelectorAll<HTMLElement>('div')].find(el => el.style.border.startsWith('3px solid')) ?? null
const undo = () => act(() => { fireEvent.keyDown(canvas(), { key: 'z', ctrlKey: true }) })

describe('a canvas text box fits its height to its text', () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(lineLayout)
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('a loaded box shows at its text\'s height, and that correction saves nothing', () => {
    const onSave = vi.fn()
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'Hello', 400)] }} onSave={onSave} />)
    expect(shown('t').style.height).toBe(`${LINE}px`)
    // The selection ring follows the fitted height (9px gap each side), not the stored 400.
    press(10, 10)
    release(10, 10)
    expect(ring()?.style.height).toBe(`${LINE + 18}px`)
    expect(onSave).not.toHaveBeenCalled()
    expect(last.shapes[0].h).toBe(400)
    // A press below the text, inside the stored height, is not on the box.
    press(10, 200)
    release(10, 200)
    expect(ring()).toBeNull()
  })

  it('grows as text wraps or lines are added, shrinks as text goes, and the edit is one undo step', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'Hi', 40)] }} />)
    press(10, 10)
    release(10, 10)
    act(() => { fireEvent.keyDown(canvas(), { key: 'Enter' }) })
    const field = () => screen.getByTestId('canvas-shape-editor')
    // 216px inner width at 10px a character: 21 a line, so 50 characters take three.
    fireEvent.change(field(), { target: { value: 'x'.repeat(50) } })
    expect(last.shapes[0].h).toBe(LINE + 2 * ROW)
    fireEvent.change(field(), { target: { value: `${'x'.repeat(50)}\n` } })
    expect(last.shapes[0].h).toBe(LINE + 3 * ROW)
    fireEvent.change(field(), { target: { value: 'short' } })
    expect(last.shapes[0].h).toBe(LINE)
    fireEvent.change(field(), { target: { value: 'x'.repeat(30) } })
    act(() => { (field() as HTMLTextAreaElement).blur() })
    expect(last.shapes[0]).toMatchObject({ text: 'x'.repeat(30), h: LINE + ROW })
    undo()
    expect(last.shapes[0]).toMatchObject({ text: 'Hi', h: 40 })
  })

  it('its handles set the width only, and the height refits to it', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'x'.repeat(30))] }} />)
    press(10, 10)
    release(10, 10)
    expect(screen.queryByTestId('handle-n')).toBeNull()
    expect(screen.queryByTestId('handle-s')).toBeNull()
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-e'), at(220, 20)) })
    moveTo(520, 300)
    release(520, 300)
    expect(last.shapes[0]).toMatchObject({ x: 0, y: 0, w: 516, h: LINE })
    // A corner also leaves the height to the text.
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-se'), at(516, LINE)) })
    moveTo(216, 400)
    release(216, 400)
    expect(last.shapes[0]).toMatchObject({ y: 0, w: 216, h: LINE + ROW })
  })

  it('a box the edit does not touch keeps its stored height in the file, through undo too', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'Hello', 400), sticky('s', 600, 0)] }} />)
    press(700, 100)
    moveTo(760, 100)
    release(760, 100)
    expect(last.shapes.find(s => s.id === 's')).toMatchObject({ x: 660 })
    expect(last.shapes.find(s => s.id === 't')?.h).toBe(400)
    undo()
    expect(last.shapes.find(s => s.id === 't')?.h).toBe(400)
    // Moving the box itself is an edit of it: the file takes its real height.
    press(10, 10)
    moveTo(70, 10)
    release(70, 10)
    expect(last.shapes.find(s => s.id === 't')).toMatchObject({ x: 60, h: LINE })
  })

  it('unlocking every shape leaves a text box it did not change at its stored height', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'Hello', 400), { ...sticky('s', 600, 0), locked: true }] }} />)
    act(() => { fireEvent.keyDown(canvas(), { code: 'KeyL', ctrlKey: true, shiftKey: true, altKey: true }) })
    expect(last.shapes.find(s => s.id === 's')?.locked).toBeUndefined()
    expect(last.shapes.find(s => s.id === 't')?.h).toBe(400)
  })

  it('shapes and stickies keep their own size, however much text they hold', () => {
    const rect: Shape = { ...sticky('r', 0, 300, 'x'.repeat(400)), kind: 'rect', w: 220, h: 130 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('s', 0, 0, 'x'.repeat(400)), rect] }} />)
    expect(shown('s').style.height).toBe('200px')
    expect(shown('r').style.height).toBe('130px')
  })

  it.each(['text', 'sticky', 'rect', 'ellipse', 'diamond'] as const)('editing a %s keeps its displayed typography', kind => {
    const shape: Shape = { ...sticky('a', 0, 0, 'First line\nSecond line'), kind, fontSize: 32, bold: true, strike: true }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [shape] }} />)
    const shown = within(screen.getByTestId('canvas-shape')).getByText('First line Second line')
    const lineHeight = kind === 'text' ? '1.3' : '1.35'
    expect(shown.style.lineHeight).toBe(lineHeight)
    press(20, 20)
    release(20, 20)
    act(() => { fireEvent.keyDown(canvas(), { key: 'Enter' }) })
    const editor = screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement
    expect(editor.style.lineHeight).toBe(shown.style.lineHeight)
    expect(editor.style.lineHeight).toBe(lineHeight)
    expect(editor.style.fontFamily).toBe('inherit')
    expect(editor.style.fontSize).toBe(shown.style.fontSize)
    expect(editor.style.fontWeight).toBe(shown.style.fontWeight)
    expect(editor.style.textDecoration).toBe(shown.style.textDecoration)
  })
})

describe('a canvas mounted with no layout fits its text boxes once it is shown', () => {
  /** Whether the canvas sits under a `display: none` ancestor: nothing measures, and it has no width. */
  let hidden = true
  let reports: (() => void)[] = []
  const realObserver = globalThis.ResizeObserver
  /** Reports each observed element when the test says the browser laid it out. */
  class LayoutObserver {
    constructor(private readonly cb: ResizeObserverCallback) {}
    observe() { reports.push(() => this.cb([], this as unknown as ResizeObserver)) }
    unobserve() {}
    disconnect() { reports = [] }
  }
  const show = () => act(() => { hidden = false; for (const report of [...reports]) report() })
  beforeEach(() => {
    hidden = true
    reports = []
    globalThis.ResizeObserver = LayoutObserver as unknown as typeof ResizeObserver
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return hidden ? 0 : lineLayout.call(this)
    })
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => (hidden ? 0 : 1200))
  })
  afterEach(() => {
    globalThis.ResizeObserver = realObserver
    vi.restoreAllMocks()
  })

  it('shows a wrapped box at its stored height while hidden, at its text\'s height once shown, and saves nothing', () => {
    const onSave = vi.fn()
    // 50 characters at 21 a line take three lines; the stored height says 90.
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'x'.repeat(50), 90)] }} onSave={onSave} />)
    expect(shown('t').style.height).toBe('90px')
    show()
    expect(shown('t').style.height).toBe(`${LINE + 2 * ROW}px`)
    expect(last.shapes[0].h).toBe(90)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('a report with an unchanged width measures nothing again', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text('t', 0, 0, 'x'.repeat(50), 90)] }} />)
    act(() => { for (const report of [...reports]) report() })
    expect(shown('t').style.height).toBe('90px')
  })
})
