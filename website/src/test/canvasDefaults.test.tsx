/**
 * A canvas's default styles: what each group keeps, how the file carries them,
 * and the editor setting, using and resetting them.
 */
import { describe, it, expect } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { newShapeStyle, readStyle, sharedStyle, withDefault } from '../components/canvas/canvasDefaults'
import { emptyCanvas, parseCanvas, serializeCanvas, type Canvas, type Shape } from '../components/canvas/canvasModel'

const shape = (id: string, kind: Shape['kind'], x: number, y: number, extra: Partial<Shape> = {}): Shape =>
  ({ id, kind, x, y, w: 200, h: 120, text: '', color: kind === 'sticky' ? 'yellow' : kind === 'text' ? 'smoke' : 'sky', ...extra })
const reread = (raw: string): Canvas => {
  const p = parseCanvas(raw)
  if (!p.ok) throw new Error('not a canvas')
  return p.canvas
}

describe('default styles', () => {
  it('each group keeps only the style fields it draws, and nothing built in', () => {
    const all = { color: 'red', hex: '#123456', fill: 'transparent', weight: 'thick', border: 'dashed', borderColor: 'blue', bold: true, strike: true, ink: 'violet', align: 'right', fontSize: 28 }
    expect(readStyle('shape', all)).toEqual({ color: 'red', hex: '#123456', fill: 'transparent', weight: 'thick', border: 'dashed', borderColor: 'blue', align: 'right', fontSize: 28 })
    expect(readStyle('sticky', all)).toEqual({ color: 'red', hex: '#123456', fill: 'transparent', align: 'right', fontSize: 28 })
    expect(readStyle('text', all)).toEqual({ ink: 'violet', bold: true, strike: true, align: 'right', fontSize: 28 })
    // The built-in look is no default at all.
    expect(readStyle('shape', { color: 'sky', fill: 'solid', weight: 'medium', align: 'center', fontSize: 16 })).toEqual({})
    expect(readStyle('sticky', { color: 'yellow' })).toEqual({})
    expect(readStyle('shape', { color: 'puce', fill: 'striped', fontSize: 'big' })).toEqual({})
  })

  it('a new object starts from its group default; a section and an unset group from the built-in look', () => {
    const d = withDefault(undefined, 'shape', { color: 'red', border: 'dashed' })
    expect(newShapeStyle(d, 'ellipse')).toEqual({ color: 'red', border: 'dashed' })
    expect(newShapeStyle(d, 'diamond')).toEqual({ color: 'red', border: 'dashed' })
    expect(newShapeStyle(d, 'sticky')).toEqual({ color: 'yellow' })
    expect(newShapeStyle(d, 'section')).toEqual({ color: 'smoke' })
    expect(withDefault(d, 'shape', null)).toBeUndefined()
    // Groups keep one order whichever was set first.
    expect(Object.keys(withDefault(withDefault(undefined, 'text', { bold: true }), 'shape', { color: 'red' })!)).toEqual(['shape', 'text'])
  })

  it('several objects set a default together only when they share one look', () => {
    const a = shape('a', 'rect', 0, 0, { color: 'red' })
    expect(sharedStyle([a, shape('b', 'ellipse', 0, 0, { color: 'red' })])).toEqual({ group: 'shape', style: { color: 'red' } })
    expect(sharedStyle([a, shape('b', 'rect', 0, 0, { color: 'blue' })])).toBeNull()
    expect(sharedStyle([a, shape('b', 'sticky', 0, 0)])).toBeNull()
    expect(sharedStyle([shape('s', 'section', 0, 0)])).toBeNull()
  })
})

describe('default styles in the file', () => {
  it('round-trip as one top-level field, and a canvas without them writes none', () => {
    const plain = serializeCanvas(emptyCanvas())
    expect(JSON.parse(plain)).not.toHaveProperty('defaultStyles')
    expect(reread(plain).defaults).toBeUndefined()
    const defaults = { shape: { color: 'red' as const, border: 'dashed' as const }, text: { bold: true } }
    const raw = serializeCanvas({ ...emptyCanvas(), defaults })
    expect(JSON.parse(raw).defaultStyles).toEqual(defaults)
    expect(reread(raw).defaults).toEqual(defaults)
  })

  it('drops values it cannot use, and a field with nothing usable is no defaults', () => {
    const raw = JSON.stringify({ nodes: [], edges: [], defaultStyles: { shape: { color: 'puce', fill: 'none' }, sticky: 'yellow', other: { color: 'red' } } })
    expect(reread(raw).defaults).toEqual({ shape: { fill: 'none' } })
    expect(reread(JSON.stringify({ nodes: [], defaultStyles: [1] })).defaults).toBeUndefined()
  })

  it('an imported file keeps its own defaults field until they change', () => {
    const top = { shape: { color: 'red', extra: 1 } }
    const doc = reread(JSON.stringify({ nodes: [], edges: [], defaultStyles: top, owner: 'x' }))
    expect(JSON.parse(serializeCanvas(doc))).toEqual({ nodes: [], edges: [], defaultStyles: top, owner: 'x' })
    const changed = JSON.parse(serializeCanvas({ ...doc, defaults: withDefault(doc.defaults, 'sticky', { color: 'mint' }) }))
    expect(changed.defaultStyles).toEqual({ shape: { color: 'red' }, sticky: { color: 'mint' } })
    expect(changed.owner).toBe('x')
    const { defaults: _d, ...reset } = doc
    void _d
    expect(JSON.parse(serializeCanvas(reset))).toEqual({ nodes: [], edges: [], owner: 'x' })
  })
})

describe('default styles in the editor', () => {
  function Harness({ initial }: { initial: Canvas }) {
    const [board, setBoard] = useState(initial)
    ;(Harness as unknown as { last: Canvas }).last = board
    return <div style={{ width: 1200, height: 800 }}><CanvasEditor canvas={board} onChange={setBoard} /></div>
  }
  const latest = () => (Harness as unknown as { last: Canvas }).last
  const canvas = () => screen.getByTestId('canvas-editor')
  const at = (x: number, y: number) => ({ clientX: x + 120, clientY: y + 100, button: 0, pointerId: 1 })
  const click = (x: number, y: number) => {
    act(() => { fireEvent.pointerDown(canvas(), at(x, y)) })
    act(() => { fireEvent.pointerUp(window, at(x, y)) })
  }
  const draw = (tool: string, x: number, y: number) => {
    fireEvent.click(screen.getByTestId(`tool-${tool}`))
    click(x, y)
    act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
  }
  const menuItem = (x: number, y: number, name: string) => {
    act(() => { fireEvent.contextMenu(canvas(), at(x, y)) })
    return screen.getByRole('menuitem', { name }) as HTMLButtonElement
  }

  it('Set as default style makes new shapes of the group start in that look, leaving the rest alone', () => {
    const red = shape('a', 'rect', 0, 0, { color: 'red', border: 'dashed', weight: 'thick' })
    const other = shape('b', 'rect', 400, 0)
    render(<Harness initial={{ ...emptyCanvas(), shapes: [red, other] }} />)
    click(100, 60)
    // The selection toolbar stays as it was: the default lives on the right-click menu.
    expect(screen.queryByRole('button', { name: /default/i })).toBeNull()
    fireEvent.click(menuItem(100, 60, 'Set as default style'))
    expect(latest().defaults).toEqual({ shape: { color: 'red', weight: 'thick', border: 'dashed' } })
    expect(latest().shapes[1]).toEqual(other)
    draw('ellipse', 300, 400)
    expect(latest().shapes[2]).toMatchObject({ kind: 'ellipse', color: 'red', border: 'dashed', weight: 'thick' })
    draw('sticky', 700, 400)
    expect(latest().shapes[3]).toMatchObject({ kind: 'sticky', color: 'yellow' })
    expect(latest().shapes[3].border).toBeUndefined()
  })

  it('the right-click menu sets and resets a default, each one undo step', () => {
    const bold = shape('t', 'text', 0, 0, { text: 'Hi', bold: true })
    render(<Harness initial={{ ...emptyCanvas(), shapes: [bold] }} />)
    act(() => { fireEvent.contextMenu(canvas(), at(50, 20)) })
    expect((screen.getByRole('menuitem', { name: 'Reset default style' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Set as default style' }))
    expect(latest().defaults).toEqual({ text: { bold: true } })
    act(() => { fireEvent.contextMenu(canvas(), at(50, 20)) })
    // Already the default: setting it again would change nothing.
    expect((screen.getByRole('menuitem', { name: 'Set as default style' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Reset default style' }))
    expect(latest().defaults).toBeUndefined()
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().defaults).toEqual({ text: { bold: true } })
  })

  it('a text box made with a large default size is tall enough for a line of it, by click or by drag', () => {
    render(<Harness initial={{ ...emptyCanvas(), defaults: { text: { fontSize: 64 } } }} />)
    // Read while each new box is still being edited: an empty text box goes when its edit ends.
    fireEvent.click(screen.getByTestId('tool-text'))
    click(300, 300)
    expect(latest().shapes[0]).toMatchObject({ kind: 'text', fontSize: 64 })
    expect(latest().shapes[0].h).toBeGreaterThanOrEqual(Math.ceil(64 * 1.3 + 8))
    expect(latest().shapes[0].w).toBeGreaterThanOrEqual(64 * 4)
    fireEvent.click(screen.getByTestId('tool-text'))
    act(() => { fireEvent.pointerDown(canvas(), at(600, 600)) })
    act(() => { fireEvent.pointerMove(window, at(660, 612)) })
    act(() => { fireEvent.pointerUp(window, at(660, 612)) })
    const dragged = latest().shapes[latest().shapes.length - 1]
    expect(dragged).toMatchObject({ kind: 'text', fontSize: 64 })
    expect(dragged.h).toBeGreaterThanOrEqual(Math.ceil(64 * 1.3 + 8))
  })

  it('the right-click menu scrolls inside a short canvas, so every item stays reachable', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [shape('a', 'rect', 0, 0)] }} />)
    act(() => { fireEvent.contextMenu(canvas(), at(50, 50)) })
    const menu = screen.getByTestId('canvas-context-menu')
    expect(menu.style.overflowY).toBe('auto')
    expect(menu.style.maxHeight).not.toBe('')
  })

  it('a section has no default style to set', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [shape('s', 'section', 0, 0)] }} />)
    act(() => { fireEvent.contextMenu(canvas(), at(50, -10)) })
    expect((screen.getByRole('menuitem', { name: 'Set as default style' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('menuitem', { name: 'Reset default style' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
