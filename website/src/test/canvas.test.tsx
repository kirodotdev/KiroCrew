/**
 * The canvas artifact's editor: its data (parse, connector geometry, edits) and
 * the editor driven through real pointer and key events.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import CanvasEditor, { noteTilt } from '../components/canvas/CanvasEditor'
import {
  boundsOf, emptyCanvas, labelScale, newId, parseCanvas, pointBounds, sectionMembers, serializeCanvas, shapeAt, wheelPanStep, wheelZoomFactor,
  type Canvas, type Shape,
} from '../components/canvas/canvasModel'
import { fitDash, ovalDash } from '../components/canvas/dashFit'
import { darker, hexToHsv, hsvToHex, inkOn, luminance, toHex } from '../components/canvas/colorMath'
import { shapeLook } from '../components/canvas/shapeLook'
import { copyShapes, pasteClip, restack } from '../components/canvas/canvasEditing'
import { STICKER_EDGE, STICKER_EDGE_W } from '../components/canvas/canvasPaint'
import { connectorRoute, connectorSides, movedSegment, segmentDrag } from '../components/canvas/connectorGeometry'

const sticky = (id: string, x: number, y: number, text = ''): Shape => ({ id, kind: 'sticky', x, y, w: 200, h: 200, text, color: 'yellow' })
/** A canvas document read back, failing the test when it is not one. */
const reread = (raw: string): Canvas => {
  const p = parseCanvas(raw)
  if (!p.ok) throw new Error('not a canvas')
  return p.canvas
}

/** One step from the zoom menu: open it from its trigger, then pick the item. */
function zoomStep(label: string) {
  act(() => { fireEvent.pointerDown(screen.getByTestId('canvas-zoom-menu'), { button: 0, ctrlKey: false, pointerType: 'mouse' }) })
  act(() => { fireEvent.click(screen.getByRole('menuitem', { name: label })) })
}

describe('canvas model', () => {
  it('ids never repeat, even made by the thousand in one go as a paste does', () => {
    const ids = new Set(Array.from({ length: 200_000 }, () => newId()))
    expect(ids.size).toBe(200_000)
    // A paste of thousands of shapes, in one synchronous loop.
    const many: Canvas = { ...emptyCanvas(), shapes: Array.from({ length: 5_000 }, (_, i) => sticky(`p${i}`, i, 0)) }
    const { canvas: pasted, ids: fresh } = pasteClip(emptyCanvas(), copyShapes(many, new Set(many.shapes.map(s => s.id)))!)
    expect(new Set(fresh).size).toBe(5_000)
    expect(new Set(pasted.shapes.map(s => s.id)).size).toBe(5_000)
  })

  it('a file that repeats an id keeps both shapes, under different ids', () => {
    const raw = JSON.stringify({ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('a', 300, 0)], connectors: [{ id: 'c', from: 'a', to: 'a' }, { id: 'c', from: 'a', to: 'a' }] })
    const back = reread(raw)
    expect(back.shapes).toHaveLength(2)
    expect(new Set(back.shapes.map(s => s.id)).size).toBe(2)
    expect(new Set(back.connectors.map(c => c.id)).size).toBe(2)
  })

  it('round-trips through its file, and drops what it cannot read', () => {
    const b: Canvas = { ...emptyCanvas(), shapes: [sticky('a', 0, 0, 'Idea')], connectors: [{ id: 'c', from: 'a', to: 'gone' }] }
    const back = reread(serializeCanvas(b))
    expect(back.shapes).toEqual(b.shapes)
    expect(back.connectors).toEqual([])
    // A shape it cannot read is dropped, the rest of the canvas kept.
    const odd = JSON.stringify({ ...b, shapes: [...b.shapes, { id: 'x', kind: 'hexagon' }, { kind: 'sticky' }] })
    expect(reread(odd).shapes.map(s => s.id)).toEqual(['a'])
  })

  it('a connector joins the facing sides at their midpoints, and is an elbow unless told otherwise', () => {
    const r = connectorRoute(sticky('a', 0, 0), sticky('b', 400, 0), {})
    expect(r.a).toEqual({ x: 200, y: 100 })
    expect(r.b).toEqual({ x: 400, y: 100 })
    // Level shapes: the elbow has no corner left, it is one straight run.
    expect(r.points).toEqual([{ x: 200, y: 100 }, { x: 400, y: 100 }])
    expect(connectorSides(sticky('a', 0, 0), sticky('b', 0, 400))).toEqual(['bottom', 'top'])
  })

  it('the drawn line starts and stops clear of its shapes, further for an arrowhead, while the route\'s ends stay on the sides', () => {
    const a = sticky('a', 0, 0)
    const b = sticky('b', 400, 0)
    const arrow = connectorRoute(a, b, { line: 'straight' })
    // 12 clear of each sticker's edge, and 9 more where an arrowhead sits.
    expect(arrow.d).toBe('M212,100 L379,100')
    expect(arrow.b).toEqual({ x: 400, y: 100 })
    expect(connectorRoute(a, b, { line: 'straight', arrows: 'none' }).d).toBe('M212,100 L388,100')
    expect(connectorRoute(a, b, { line: 'straight', arrows: 'both' }).d).toBe('M221,100 L379,100')
    expect(connectorRoute(a, b, { line: 'curve' }).d).toMatch(/ 379,100$/)
  })

  it('a connector whose stored route collapses to one point still draws, rather than crashing the canvas', () => {
    // Touching shapes joined right-to-left, with the corner a file brought in.
    const a: Shape = { ...sticky('a', 0, 0), kind: 'rect' }
    const b: Shape = { ...sticky('b', 200, 0), kind: 'rect' }
    const r = connectorRoute(a, b, { fromSide: 'right', toSide: 'left', route: [{ x: 200, y: 100 }] })
    expect(r.d).toMatch(/^M/)
  })

  it('an elbow from a side to a side turns once, with a rounded corner', () => {
    const a: Shape = { ...sticky('a', 0, 0), w: 200, h: 100 }
    const b: Shape = { ...sticky('b', 400, 200), w: 200, h: 100 }
    const r = connectorRoute(a, b, { fromSide: 'right', toSide: 'top' })
    expect(r.points).toEqual([{ x: 200, y: 50 }, { x: 500, y: 50 }, { x: 500, y: 200 }])
    expect(r.d).toContain('Q500,50')
    // Back to back (a's left side to b's right side): it goes round, between them, never through.
    const back = connectorRoute(a, b, { fromSide: 'left', toSide: 'right' })
    expect(back.points).toEqual([
      { x: 0, y: 50 }, { x: -24, y: 50 }, { x: -24, y: 150 }, { x: 624, y: 150 }, { x: 624, y: 250 }, { x: 600, y: 250 },
    ])
    expect(connectorRoute(a, b, { line: 'straight' }).d).toMatch(/^M[\d.]+,[\d.]+ L[\d.]+,[\d.]+$/)
    expect(connectorRoute(a, b, { line: 'curve' }).d).toContain(' C')
  })

  it('dragging an elbow segment reshapes it; an end segment keeps a stub on its shape and adds a jog', () => {
    const a: Shape = { ...sticky('a', 0, 0), w: 200, h: 100 }
    const b: Shape = { ...sticky('b', 400, 200), w: 200, h: 100 }
    const r = connectorRoute(a, b, { fromSide: 'right', toSide: 'top' })
    // The first segment touches a: a 24px stub stays, the segment past it moves down to 120.
    const first = segmentDrag(r.points, 0, ...r.sides)
    expect(first.axis).toBe('y')
    const route = movedSegment(first, 120)
    const shaped = connectorRoute(a, b, { fromSide: 'right', toSide: 'top', route })
    expect(shaped.points).toEqual([{ x: 200, y: 50 }, { x: 224, y: 50 }, { x: 224, y: 120 }, { x: 500, y: 120 }, { x: 500, y: 200 }])
    // After b moves, the shaped route still meets it squarely.
    const moved = connectorRoute(a, { ...b, x: 460 }, { fromSide: 'right', toSide: 'top', route })
    expect(moved.points[moved.points.length - 2]).toEqual({ x: 560, y: 120 })
    // A middle segment just slides.
    const back = connectorRoute(a, b, { fromSide: 'left', toSide: 'right' })
    const mid = segmentDrag(back.points, 2, ...back.sides)
    expect(movedSegment(mid, 400)).toEqual([{ x: -24, y: 50 }, { x: -24, y: 400 }, { x: 624, y: 400 }, { x: 624, y: 250 }])
  })

  it('keeps a connector\'s sides and style through its file, and drops values it does not know', () => {
    const b: Canvas = {
      ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)],
      connectors: [{ id: 'c', from: 'a', to: 'b', fromSide: 'right', toSide: 'top', line: 'curve', color: 'green', dashed: true, arrows: 'both' }],
    }
    expect(reread(serializeCanvas(b)).connectors).toEqual(b.connectors)
    const odd = JSON.stringify({ ...b, connectors: [{ id: 'c', from: 'a', to: 'b', fromSide: 'middle', line: 'zigzag', color: 'neon', arrows: 3 }] })
    expect(reread(odd).connectors).toEqual([{ id: 'c', from: 'a', to: 'b' }])
  })

  it('reads an empty document as an empty canvas, and refuses one that is not a canvas', () => {
    expect(parseCanvas('')).toEqual({ ok: true, canvas: emptyCanvas() })
    expect(parseCanvas('not json').ok).toBe(false)
    expect(parseCanvas('[]').ok).toBe(false)
    expect(parseCanvas(JSON.stringify({ kind: 'kirocrew-board', shapes: [] })).ok).toBe(false)
    expect(reread(serializeCanvas(emptyCanvas()))).toEqual({ kind: 'kirocrew-canvas', schema: 1, shapes: [], connectors: [] })
  })

  it('a section is picked by its title tab, so a press on its body reaches the canvas', () => {
    const sec: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 400, h: 400, text: 'To do', color: 'smoke' }
    const card = sticky('a', 40, 40)
    const shapes = [sec, card]
    expect(shapeAt(shapes, { x: 10, y: -10 })?.id).toBe('sec')
    expect(shapeAt(shapes, { x: 4, y: 396 })).toBeNull()
    expect(shapeAt(shapes, { x: 45, y: 45 })?.id).toBe('a')
    expect(sectionMembers(shapes, sec).map(s => s.id)).toEqual(['a'])
  })
})

describe('the canvas', () => {
  function Harness({ initial, ...rest }: { initial: Canvas } & Partial<React.ComponentProps<typeof CanvasEditor>>) {
    const [board, setBoard] = useState(initial)
    ;(Harness as unknown as { last: Canvas }).last = board
    return (
      <div style={{ width: 1200, height: 800 }}>
        <CanvasEditor canvas={board} onChange={setBoard} {...rest} />
      </div>
    )
  }
  const latest = () => (Harness as unknown as { last: Canvas }).last
  const canvas = () => screen.getByTestId('canvas-editor')
  /** World = screen minus the starting view offset (120, 100) at zoom 1. */
  const at = (x: number, y: number) => ({ clientX: x + 120, clientY: y + 100, button: 0, pointerId: 1 })
  const press = (x: number, y: number, extra = {}) => act(() => { fireEvent.pointerDown(canvas(), { ...at(x, y), ...extra }) })
  const moveTo = (x: number, y: number, extra = {}) => act(() => { fireEvent.pointerMove(window, { ...at(x, y), ...extra }) })
  const release = (x: number, y: number) => act(() => { fireEvent.pointerUp(window, at(x, y)) })

  it('Space released outside the canvas does not leave the next drawing gesture in pan mode', () => {
    render(<Harness initial={emptyCanvas()} />)
    fireEvent.click(screen.getByTestId('tool-rect'))
    fireEvent.keyDown(canvas(), { key: ' ' })
    expect(canvas().style.cursor).toBe('grab')
    fireEvent.blur(canvas(), { relatedTarget: document.body })
    fireEvent.keyUp(document.body, { key: ' ' })
    press(100, 100)
    moveTo(190, 150)
    release(190, 150)
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'rect', x: 96, y: 96, w: 96, h: 60 })])
  })

  it('a desktop split canvas keeps the dock capped and scrollable, with zoom off its row', () => {
    render(<Harness initial={emptyCanvas()} />)
    const dock = screen.getByTestId('canvas-dock').className.split(' ')
    expect(dock).toContain('max-w-[calc(100%-1.5rem)]')
    expect(dock).toContain('overflow-x-auto')
    expect(dock).not.toContain('md:max-w-none')
    expect(dock).not.toContain('md:overflow-visible')
    const zoom = screen.getByTestId('canvas-zoom').className.split(' ')
    expect(zoom).toContain('top-3')
    expect(zoom).not.toContain('md:bottom-5')
    expect(zoom).not.toContain('md:top-auto')
  })

  it('the dock\'s sticky tool drops a sticky where you click, ready to type into', () => {
    render(<Harness initial={emptyCanvas()} />)
    expect(screen.getByText('A blank canvas')).toBeTruthy()
    fireEvent.click(screen.getByTestId('tool-sticky'))
    press(300, 300)
    release(300, 300)
    expect(latest().shapes).toHaveLength(1)
    // Centred on the click, then onto the 12px snap grid.
    expect(latest().shapes[0]).toMatchObject({ kind: 'sticky', x: 204, y: 204 })
    const editor = screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'An idea' } })
    act(() => { editor.blur() })
    expect(latest().shapes[0].text).toBe('An idea')
  })

  it('dragging with a shape tool sizes the new shape, live, as one undo step', () => {
    render(<Harness initial={emptyCanvas()} />)
    fireEvent.click(screen.getByTestId('tool-rect'))
    press(100, 100)
    moveTo(190, 150)
    // It exists while you drag, so you can see what you are sizing; both corners snap.
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'rect', x: 96, y: 96, w: 96, h: 60 })])
    moveTo(50, 290)
    release(50, 290)
    // Dragging up-left of the start flips the box instead of going negative.
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'rect', x: 48, y: 96, w: 48, h: 192 })])
    expect(screen.getByTestId('canvas-shape-editor')).toBeTruthy()
    act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes).toHaveLength(0)
  })

  it('the text tool keeps the new text box: the press that makes it does not take focus away', () => {
    render(<Harness initial={emptyCanvas()} />)
    fireEvent.click(screen.getByTestId('tool-text'))
    let notPrevented = true
    act(() => { notPrevented = fireEvent.pointerDown(canvas(), at(300, 300)) })
    // A browser would move focus to the canvas on this press and end the edit.
    expect(notPrevented).toBe(false)
    release(300, 300)
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'text' })])
    expect(screen.getByTestId('canvas-shape-editor')).toBeTruthy()
  })

  it('dragging a shape moves it; Delete removes it; undo brings each step back', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0, 'Move me')] }} />)
    press(100, 100)
    moveTo(160, 140)
    release(160, 140)
    // A 60 by 40 drag lands on the nearest 12px snap step.
    expect(latest().shapes[0]).toMatchObject({ x: 60, y: 36 })
    act(() => { fireEvent.keyDown(canvas(), { key: 'Delete' }) })
    expect(latest().shapes).toHaveLength(0)
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0]).toMatchObject({ x: 60, y: 36 })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0]).toMatchObject({ x: 0, y: 0 })
  })

  it('dragging a section by its title carries the cards inside it', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 300, h: 400, text: 'To do', color: 'smoke' }
    const card: Shape = { ...sticky('c', 20, 20, 'Card'), w: 260, h: 80 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [section, card] }} />)
    press(10, -10)
    moveTo(106, 86)
    release(106, 86)
    expect(latest().shapes.find(s => s.id === 'sec')).toMatchObject({ x: 96, y: 96 })
    expect(latest().shapes.find(s => s.id === 'c')).toMatchObject({ x: 116, y: 116 })
  })

  it('a corner handle resizes; a sticky stays square', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-se'), at(200, 200)) })
    moveTo(290, 260)
    release(290, 260)
    // The dragged corner snaps to (288, 264); a sticky takes the larger side.
    expect(latest().shapes[0]).toMatchObject({ w: 288, h: 288 })
  })

  it('a text box left empty leaves no undo step behind, so Undo never brings back an empty box', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    fireEvent.click(screen.getByTestId('tool-text'))
    press(600, 300)
    release(600, 300)
    act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
    expect(latest().shapes.map(s => s.kind)).toEqual(['sticky'])
    // Nothing to undo: the abandoned box left the canvas as it was.
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes.map(s => s.kind)).toEqual(['sticky'])
  })

  it('a drag that ends an empty text box\'s edit undoes to a canvas without that box', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    fireEvent.click(screen.getByTestId('tool-text'))
    press(600, 300)
    release(600, 300)
    expect(latest().shapes.map(s => s.kind)).toEqual(['sticky', 'text'])
    // The press on the sticky ends the edit, which removes the empty box.
    press(100, 100)
    moveTo(140, 100)
    release(140, 100)
    expect(latest().shapes).toEqual([expect.objectContaining({ id: 'a', x: 36, y: 0 })])
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', ctrlKey: true }) })
    expect(latest().shapes).toEqual([expect.objectContaining({ id: 'a', x: 0, y: 0 })])
  })

  it('a shape drawn while an emptied text box is still in edit undoes in one step to a canvas without that box', () => {
    const text: Shape = { id: 't', kind: 'text', x: 0, y: 0, w: 220, h: 40, text: 'Hello', color: 'smoke' }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text] }} />)
    press(10, 10)
    release(10, 10)
    // The sticky tool from its key, then Enter edits the selected box: the edit
    // is still open when the next press lands, since nothing took focus.
    act(() => { fireEvent.keyDown(canvas(), { key: 's' }) })
    act(() => { fireEvent.keyDown(canvas(), { key: 'Enter' }) })
    fireEvent.change(screen.getByTestId('canvas-shape-editor'), { target: { value: '' } })
    press(300, 300)
    moveTo(450, 450)
    release(450, 450)
    expect(latest().shapes.map(s => s.kind)).toEqual(['sticky'])
    act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', ctrlKey: true }) })
    expect(latest().shapes).toEqual([])
    // The step before that is the emptying of the box: undo brings its text back.
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', ctrlKey: true }) })
    expect(latest().shapes).toEqual([expect.objectContaining({ id: 't', text: 'Hello' })])
  })

  it('pressing a resize handle and letting go without moving records no undo step, so redo survives', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    act(() => { fireEvent.keyDown(canvas(), { key: 'd', metaKey: true }) })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes).toHaveLength(1)
    press(100, 60)
    release(100, 60)
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-e'), at(240, 60)) })
    release(240, 60)
    // The duplicate undone above can still be redone: the still press changed nothing.
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true, shiftKey: true }) })
    expect(latest().shapes).toHaveLength(2)
  })

  it('dragging a side of the selected shape moves only that side; a sticky stays square', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect, sticky('s', 600, 0)] }} />)
    press(100, 60)
    release(100, 60)
    expect(screen.getByTestId('handle-e').style.cursor).toBe('ew-resize')
    expect(screen.getByTestId('handle-n').style.cursor).toBe('ns-resize')
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-e'), at(240, 60)) })
    moveTo(340, 90)
    release(340, 90)
    expect(latest().shapes[0]).toMatchObject({ x: 0, y: 0, w: 336, h: 120 })
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-n'), at(100, 0)) })
    moveTo(100, -50)
    release(100, -50)
    expect(latest().shapes[0]).toMatchObject({ x: 0, y: -48, w: 336, h: 168 })
    // A sticky's side drag widens it and its height follows, so it stays square.
    press(700, 100)
    release(700, 100)
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-e'), at(800, 100)) })
    moveTo(860, 100)
    release(860, 100)
    expect(latest().shapes[1]).toMatchObject({ x: 600, y: 0, w: 264, h: 264 })
  })

  it('holding Alt drags, resizes and draws off the grid', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    moveTo(160, 140, { altKey: true })
    release(160, 140)
    expect(latest().shapes[0]).toMatchObject({ x: 60, y: 40 })
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-se'), at(260, 240)) })
    moveTo(310, 250, { altKey: true })
    release(310, 250)
    expect(latest().shapes[0]).toMatchObject({ w: 250, h: 250 })
    fireEvent.click(screen.getByTestId('tool-rect'))
    press(500, 500)
    moveTo(570, 530, { altKey: true })
    release(570, 530)
    expect(latest().shapes[1]).toMatchObject({ kind: 'rect', x: 500, y: 500, w: 70, h: 30 })
  })

  it('the connector tool links two shapes, and the link follows the shapes', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)] }} />)
    fireEvent.click(screen.getByTestId('tool-connector'))
    press(100, 100)
    moveTo(500, 100)
    release(500, 100)
    expect(latest().connectors).toEqual([expect.objectContaining({ from: 'a', to: 'b' })])
    expect(screen.getAllByTestId('canvas-connector')).toHaveLength(1)
  })

  it('the connect dots stay while you reach for one, and dragging from one links two shapes', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)] }} />)
    const hover = (x: number, y: number) => act(() => { fireEvent.pointerMove(canvas(), at(x, y)) })
    hover(100, 100)
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    // Off the shape's right edge, on the way to its right dot: still hovered.
    hover(sticky('a', 0, 0).w + 14, 100)
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    const dot = screen.getAllByTestId('connect-dot')[1]
    act(() => { fireEvent.pointerMove(dot, at(sticky('a', 0, 0).w + 14, 100)) })
    act(() => { fireEvent.pointerDown(dot, at(sticky('a', 0, 0).w + 14, 100)) })
    moveTo(500, 100)
    release(500, 100)
    // It leaves from the dot it was dragged from; dropped on b's body, b's side faces a.
    expect(latest().connectors).toEqual([{ id: expect.any(String), from: 'a', to: 'b', fromSide: 'right' }])
    // Well clear of every shape, the dots go.
    hover(250, 400)
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
  })

  it('a connector dropped near a side\'s dot pins that side, even just outside the shape', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)] }} />)
    act(() => { fireEvent.pointerMove(canvas(), at(100, 100)) })
    const bottom = screen.getAllByTestId('connect-dot')[2]
    act(() => { fireEvent.pointerDown(bottom, at(100, 214)) })
    // Over b's top dot, 10px above b: its side dots show, the top one snapped.
    moveTo(500, -10)
    expect(screen.getAllByTestId('connect-target')).toHaveLength(4)
    expect(screen.getAllByTestId('connect-target').filter(d => d.dataset.snapped)).toHaveLength(1)
    release(500, -10)
    expect(latest().connectors).toEqual([{ id: expect.any(String), from: 'a', to: 'b', fromSide: 'bottom', toSide: 'top' }])
    expect(screen.queryAllByTestId('connect-target')).toHaveLength(0)
  })

  it('a wheel notch zooms by about a fifth and a pinch follows gently, whichever way it is clamped', () => {
    expect(wheelZoomFactor(0)).toBe(1)
    expect(wheelZoomFactor(4)).toBeCloseTo(0.98, 2)
    expect(wheelZoomFactor(120)).toBeCloseTo(Math.exp(-0.2), 5)
    expect(wheelZoomFactor(-120)).toBeCloseTo(Math.exp(0.2), 5)
    expect(wheelZoomFactor(-4) * wheelZoomFactor(4)).toBeCloseTo(1, 10)
  })

  it('scrolling pans 60% further than the raw delta, and a line-based wheel is read in lines', () => {
    expect(wheelPanStep(10, 0)).toBe(16)
    expect(wheelPanStep(-10, 0)).toBe(-16)
    expect(wheelPanStep(3, 1)).toBeCloseTo(76.8, 5)
    expect(wheelPanStep(1, 2)).toBe(640)
  })

  it('right-click opens the menu on a shape; its order, copy, paste and delete act on that shape', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 100, 100), sticky('c', 1000, 0)], connectors: [] }} />)
    const ids = () => latest().shapes.map(s => s.id)
    act(() => { fireEvent.contextMenu(canvas(), at(50, 50)) })
    expect(screen.getByTestId('canvas-context-menu')).toBeTruthy()
    expect((screen.getByRole('menuitem', { name: /^Copy/ }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('menuitem', { name: /Bring forward/ }))
    // a overlapped b, so one step forward puts it over b; c is far away and unaffected.
    expect(ids()).toEqual(['b', 'a', 'c'])
    expect(screen.queryByTestId('canvas-context-menu')).toBeNull()
    act(() => { fireEvent.contextMenu(canvas(), at(50, 50)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /Send to back/ }))
    expect(ids()).toEqual(['a', 'b', 'c'])
    act(() => { fireEvent.contextMenu(canvas(), at(50, 50)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /^Copy/ }))
    // Paste lands where the menu was opened: on empty board at (600, 600).
    act(() => { fireEvent.contextMenu(canvas(), at(600, 600)) })
    expect((screen.getByRole('menuitem', { name: /^Delete/ }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('menuitem', { name: /^Paste/ }))
    expect(latest().shapes).toHaveLength(4)
    expect(latest().shapes[3]).toMatchObject({ x: 600, y: 600, color: 'yellow' })
    expect(latest().shapes[3].id).not.toBe('a')
    act(() => { fireEvent.contextMenu(canvas(), at(1100, 100)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /^Delete/ }))
    expect(ids()).not.toContain('c')
  })

  it('a locked shape cannot be picked or moved, yet a right-click unlocks it', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.keyDown(canvas(), { key: 'L', code: 'KeyL', metaKey: true, shiftKey: true }) })
    expect(latest().shapes[0].locked).toBe(true)
    expect(reread(serializeCanvas(latest())).shapes[0].locked).toBe(true)
    // A press on it goes through: nothing is picked and a drag does not move it.
    press(100, 100)
    moveTo(300, 300)
    release(300, 300)
    expect(latest().shapes[0]).toMatchObject({ x: 0, y: 0 })
    expect(screen.queryByTestId('canvas-toolbar')).toBeNull()
    act(() => { fireEvent.contextMenu(canvas(), at(100, 100)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /Lock\/Unlock/ }))
    expect(latest().shapes[0].locked).toBeUndefined()
    // Unlock all objects frees every locked shape at once.
    act(() => { fireEvent.keyDown(canvas(), { key: 'L', code: 'KeyL', metaKey: true, shiftKey: true }) })
    act(() => { fireEvent.contextMenu(canvas(), at(600, 600)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /Unlock all objects/ }))
    expect(latest().shapes[0].locked).toBeUndefined()
  })

  it('a right-click on a locked shape offers only Lock/Unlock, so its menu cannot delete or restack it', () => {
    const onChange = vi.fn()
    const initial: Canvas = { ...emptyCanvas(), shapes: [sticky('a', 0, 0), { ...sticky('b', 100, 100), locked: true }] }
    render(<Harness initial={initial} onChange={onChange} />)
    act(() => { fireEvent.contextMenu(canvas(), at(250, 250)) })
    const item = (name: RegExp) => screen.getByRole('menuitem', { name }) as HTMLButtonElement
    for (const name of [/^Copy/, /^Delete/, /Bring to front/, /Bring forward/, /Send backward/, /Send to back/]) expect(item(name).disabled).toBe(true)
    expect(item(/Lock\/Unlock/).disabled).toBe(false)
    expect(item(/Unlock all objects/).disabled).toBe(false)
    fireEvent.click(item(/^Delete/))
    fireEvent.click(item(/Send to back/))
    expect(onChange).not.toHaveBeenCalled()
    expect(latest()).toBe(initial)
  })

  it('an undo that locks a selected shape again lets go of it, so it cannot be resized, edited or deleted', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.keyDown(canvas(), { key: 'L', code: 'KeyL', metaKey: true, shiftKey: true }) })
    act(() => { fireEvent.contextMenu(canvas(), at(100, 100)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /Lock\/Unlock/ }))
    expect(screen.getByTestId('canvas-toolbar')).toBeTruthy()
    // Undoing the unlock locks it again while it is still picked.
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0].locked).toBe(true)
    expect(screen.queryByTestId('canvas-toolbar')).toBeNull()
    expect(document.querySelector('[data-handle]')).toBeNull()
    act(() => { fireEvent.keyDown(canvas(), { key: 'Backspace' }) })
    act(() => { fireEvent.keyDown(canvas(), { key: 'Enter' }) })
    expect(latest().shapes).toHaveLength(1)
    expect(screen.queryByTestId('canvas-shape-editor')).toBeNull()
  })

  it('opening a second toolbar panel closes the first, so panels never pile up', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Colour'))
    expect(screen.getAllByTestId('swatch-panel')).toHaveLength(1)
    // The press lands inside the toolbar, whose wrapper stops it reaching the canvas.
    const border = screen.getByLabelText('Border')
    fireEvent.pointerDown(border)
    fireEvent.click(border)
    expect(screen.getAllByTestId('swatch-panel')).toHaveLength(1)
    expect(screen.getByLabelText('Colour').getAttribute('aria-expanded')).toBe('false')
    expect(border.getAttribute('aria-expanded')).toBe('true')
  })

  it('zoom is one menu, its trigger reading the zoom, with steps, 100% and fit inside', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    expect(within(screen.getByTestId('canvas-zoom')).getAllByRole('button')).toHaveLength(1)
    const trigger = screen.getByTestId('canvas-zoom-menu')
    expect(trigger.textContent).toContain('100%')
    zoomStep('Zoom in')
    expect(trigger.textContent).toContain('120%')
    zoomStep('Reset zoom to 100%')
    expect(trigger.textContent).toContain('100%')
    zoomStep('Fit to canvas')
    expect(screen.getByTestId('canvas-zoom-menu').textContent).toMatch(/%/)
  })

  it('on a phone the selection toolbar sits across the top of the canvas, and colour rows wrap to fit', () => {
    const real = window.matchMedia
    window.matchMedia = ((q: string) => ({
      matches: q.includes('max-width'), media: q, onchange: null,
      addEventListener: () => undefined, removeEventListener: () => undefined, addListener: () => undefined, removeListener: () => undefined, dispatchEvent: () => false,
    })) as typeof window.matchMedia
    try {
      const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
      render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
      press(100, 60)
      release(100, 60)
      const place = screen.getByTestId('canvas-toolbar-place')
      expect(place.style.left).toBe('12px')
      expect(place.style.right).toBe('12px')
      expect(place.style.transform).toBe('')
      fireEvent.click(screen.getByLabelText('Colour'))
      const rows = [...screen.getByTestId('swatch-panel').children].filter(c => c.className.includes('gap-2'))
      expect(rows.every(r => r.className.includes('flex-wrap'))).toBe(true)
    } finally {
      window.matchMedia = real
    }
  })

  it('a canvas far too big to spread into Math.min still has bounds', () => {
    const many = Array.from({ length: 200_000 }, (_, i) => ({ ...sticky(`s${i}`, i, -i), w: 10, h: 10 }))
    expect(boundsOf(many)).toEqual({ x: 0, y: -199_999, w: 200_009, h: 200_009 })
    expect(pointBounds(many.map(s => ({ x: s.x, y: s.y })))).toEqual({ x: 0, y: -199_999, w: 199_999, h: 199_999 })
    expect(boundsOf([])).toBeNull()
    expect(pointBounds([])).toBeNull()
  })

  it('Space and Enter on a toolbar button press the button, not the canvas shortcuts', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    const button = within(screen.getByTestId('canvas-toolbar')).getAllByRole('button')[0]
    act(() => { button.focus() })
    // Not prevented: the browser's own activation of the button still happens.
    expect(fireEvent.keyDown(button, { key: ' ' })).toBe(true)
    expect(fireEvent.keyDown(button, { key: 'Enter' })).toBe(true)
    expect(screen.queryByTestId('canvas-shape-editor')).toBeNull()
    // A chord still reaches the canvas from a toolbar button.
    act(() => { fireEvent.keyDown(button, { key: 'd', metaKey: true }) })
    expect(latest().shapes).toHaveLength(2)
  })

  it.each(['same shape', 'another shape'])('a dismissed colour drag stays separate from the next drag on %s', target => {
    const a: Shape = { ...sticky('a', 0, 0), kind: 'rect', hex: '#FF0000', w: 240, h: 120 }
    const b: Shape = { ...a, id: 'b', x: 400 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [a, b] }} />)
    press(100, 60)
    release(100, 60)
    const openSquare = () => {
      fireEvent.click(screen.getByLabelText('Colour'))
      const custom = screen.getByLabelText('Colour: custom colour')
      if (custom.getAttribute('aria-expanded') === 'false') fireEvent.click(custom)
      const square = screen.getByTestId('custom-square')
      square.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) })
      return square
    }
    const first = openSquare()
    fireEvent.pointerDown(first, { clientX: 50, clientY: 50, pointerId: 1 })
    expect(latest().shapes.map(s => s.hex)).toEqual(['#804040', '#FF0000'])
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('custom-square')).toBeNull()
    fireEvent.pointerUp(window, { clientX: 50, clientY: 50, pointerId: 1 })

    if (target === 'another shape') { press(500, 60); release(500, 60) }
    const second = openSquare()
    fireEvent.pointerDown(second, { clientX: 100, clientY: 75, pointerId: 2 })
    fireEvent.pointerUp(second, { clientX: 100, clientY: 75, pointerId: 2 })
    expect(latest().shapes.map(s => s.hex)).toEqual(target === 'same shape' ? ['#400000', '#FF0000'] : ['#804040', '#400000'])

    fireEvent.keyDown(canvas(), { key: 'z', metaKey: true })
    expect(latest().shapes.map(s => s.hex)).toEqual(['#804040', '#FF0000'])
    fireEvent.keyDown(canvas(), { key: 'z', metaKey: true })
    expect(latest().shapes.map(s => s.hex)).toEqual(['#FF0000', '#FF0000'])
  })

  it('a colour typed and then dismissed with Escape is its own undo step, so later undos never jump past newer edits', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.click(screen.getByLabelText('Colour: custom colour'))
    const hexField = screen.getByLabelText('Hex colour') as HTMLInputElement
    fireEvent.focus(hexField)
    fireEvent.change(hexField, { target: { value: '#112233' } })
    // Dismissed mid-gesture: the colour shows, but no final value was ever sent.
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    expect(latest().shapes[0].hex).toBe('#112233')
    act(() => { fireEvent.keyDown(canvas(), { key: 'd', metaKey: true }) })
    expect(latest().shapes).toHaveLength(2)
    // Undo takes back the duplicate, then the colour; nothing is skipped or lost.
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes).toHaveLength(1)
    expect(latest().shapes[0].hex).toBe('#112233')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0].hex).toBeUndefined()
  })

  it('the menu\'s shortcuts work on the selection: brackets restack, Cmd+C then Cmd+V pastes a copy with its links', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 100, 100), sticky('c', 400, 0)], connectors: [{ id: 'k', from: 'a', to: 'c' }] }} />)
    press(50, 50)
    release(50, 50)
    act(() => { fireEvent.keyDown(canvas(), { key: ']', code: 'BracketRight' }) })
    expect(latest().shapes.map(s => s.id)).toEqual(['b', 'c', 'a'])
    act(() => { fireEvent.keyDown(canvas(), { key: '[', code: 'BracketLeft' }) })
    expect(latest().shapes.map(s => s.id)).toEqual(['a', 'b', 'c'])
    act(() => { fireEvent.keyDown(canvas(), { key: 'a', code: 'KeyA', metaKey: true }) })
    act(() => { fireEvent.keyDown(canvas(), { key: 'c', code: 'KeyC', metaKey: true }) })
    act(() => { fireEvent.keyDown(canvas(), { key: 'v', code: 'KeyV', metaKey: true }) })
    expect(latest().shapes).toHaveLength(6)
    // Pasted beside the originals, with the link between a and c copied between their copies.
    expect(latest().shapes[3]).toMatchObject({ x: 24, y: 24 })
    expect(latest().connectors).toHaveLength(2)
    const copy = latest().connectors[1]
    expect([copy.from, copy.to]).toEqual([latest().shapes[3].id, latest().shapes[5].id])
  })

  it('a section label keeps its on-screen size as you zoom out, and its grab area grows with it', () => {
    expect(labelScale(1)).toBe(1)
    expect(labelScale(2)).toBe(1)
    expect(labelScale(0.5)).toBe(2)
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 300, h: 200, text: 'To do', color: 'smoke' }
    // At 100% the tab is 30 high above the section; zoomed out to 25% it is 4 times that, so it can still be grabbed.
    expect(shapeAt([section], { x: 200, y: -100 }, { zoom: 1 })).toBeNull()
    expect(shapeAt([section], { x: 200, y: -100 }, { zoom: 0.25 })?.id).toBe('sec')
    expect(shapeAt([section], { x: 200, y: -125 }, { zoom: 0.25 })).toBeNull()
    render(<Harness initial={{ ...emptyCanvas(), shapes: [section] }} />)
    const label = () => screen.getByText('To do') as HTMLElement
    expect(label().style.transform).toBe('')
    zoomStep('Zoom out')
    zoomStep('Zoom out')
    zoomStep('Zoom out')
    // 1 / 1.2^3 = 0.579 of the board's size: the label is drawn 1.728 times larger from its bottom-left corner.
    expect(label().style.transform).toBe('scale(1.728)')
    expect(label().style.transformOrigin).toBe('left bottom')
  })

  it('zooming out thins the dots by halves instead of packing them tighter', () => {
    render(<Harness initial={emptyCanvas()} />)
    const gap = () => parseFloat(canvas().style.backgroundSize)
    expect(gap()).toBe(24)
    const seen: number[] = []
    for (let i = 0; i < 14; i++) {
      zoomStep('Zoom out')
      seen.push(gap())
    }
    // Never tighter than 16px, and never looser than twice that.
    for (const g of seen) { expect(g).toBeGreaterThanOrEqual(16); expect(g).toBeLessThan(32.01) }
    // The gap fell, then sprang back at least once when every other dot dropped.
    expect(seen.some((g, i) => i > 0 && g > seen[i - 1])).toBe(true)
  })

  it('connectors are drawn over the shapes they cross, not under them', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0), sticky('c', 200, 0)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    const line = screen.getByTestId('canvas-connector')
    for (const shape of screen.getAllByTestId('canvas-shape')) {
      // Later in the document is higher, as shapes and the line share a stacking context.
      expect(shape.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })

  it('shapes are stickers: a white edge by default, a picked colour paints the edge, and only dashes make an outline', () => {
    const rect: Shape = { id: 'r', kind: 'rect', x: 0, y: 0, w: 200, h: 120, text: '', color: 'blue' }
    const red: Shape = { ...rect, id: 'o', x: 400, borderColor: 'red' }
    const dashed: Shape = { ...rect, id: 'd', x: 800, border: 'dashed' }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect, red, dashed], connectors: [{ id: 'c1', from: 'r', to: 'o' }] }} />)
    const [plain, edged, outlined] = screen.getAllByTestId('canvas-shape')
    expect(plain.style.boxShadow).toContain(`${STICKER_EDGE_W}px ${STICKER_EDGE}`)
    expect(plain.style.border).toBe('')
    expect(edged.style.boxShadow).toContain(`${STICKER_EDGE_W}px #F2573C`)
    expect(outlined.style.boxShadow).toBe('')
    expect(outlined.style.border).toContain('solid')
    // A line carries no halo: it stops short of the shapes instead.
    const paths = screen.getByTestId('canvas-connector').querySelectorAll('path')
    expect([...paths].some(p => p.getAttribute('stroke') === STICKER_EDGE)).toBe(false)
  })

  it('a text box gets its own toolbar: colour, size, bold, strikethrough and alignment, each kept in the file', () => {
    const text: Shape = { id: 't', kind: 'text', x: 0, y: 0, w: 220, h: 40, text: 'Hello', color: 'smoke' }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [text] }} />)
    press(20, 20)
    release(20, 20)
    expect(screen.getByTestId('canvas-text-toolbar')).toBeTruthy()
    expect(screen.queryByTestId('canvas-toolbar')).toBeNull()
    fireEvent.click(screen.getByLabelText('Text size'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Huge' }))
    // The box grows to hold a line at the new size.
    expect(latest().shapes[0]).toMatchObject({ fontSize: 64 })
    expect(latest().shapes[0].h).toBeGreaterThanOrEqual(64)
    fireEvent.click(screen.getByLabelText('Bold'))
    fireEvent.click(screen.getByLabelText('Strikethrough'))
    fireEvent.click(screen.getByLabelText('Alignment'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Align right' }))
    expect(latest().shapes[0]).toMatchObject({ bold: true, strike: true, align: 'right' })
    const shown = screen.getByText('Hello') as HTMLElement
    expect(shown.style.textAlign).toBe('right')
    expect(shown.style.fontWeight).toBe('700')
    expect(reread(serializeCanvas(latest())).shapes[0]).toMatchObject({ fontSize: 64, bold: true, strike: true, align: 'right' })
  })

  it('a shape shows text alignment and size only once it has text, and centres its text by default', () => {
    const empty: Shape = { id: 'r', kind: 'rect', x: 0, y: 0, w: 240, h: 120, text: '', color: 'blue' }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [empty] }} />)
    press(100, 60)
    release(100, 60)
    expect(screen.queryByLabelText('Alignment')).toBeNull()
    expect(screen.queryByLabelText('Text size')).toBeNull()
    act(() => { fireEvent.keyDown(canvas(), { key: 'Enter' }) })
    fireEvent.change(screen.getByTestId('canvas-shape-editor'), { target: { value: 'Ship it' } })
    act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
    expect((screen.getByText('Ship it') as HTMLElement).style.textAlign).toBe('center')
    fireEvent.click(screen.getByLabelText('Alignment'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Align left' }))
    expect(latest().shapes[0].align).toBe('left')
    // Back to the kind's default drops the field.
    fireEvent.click(screen.getByLabelText('Alignment'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Align centre' }))
    expect(latest().shapes[0].align).toBeUndefined()
    fireEvent.click(screen.getByLabelText('Text size'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Large' }))
    // A shape keeps its size; only its text changes.
    expect(latest().shapes[0]).toMatchObject({ fontSize: 28, w: 240, h: 120 })
  })

  it('a sticky is taped, dog-eared paper with a steady lean that straightens while it is typed in', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('n1', 0, 0, 'Idea')] }} />)
    const note = screen.getByTestId('canvas-shape')
    expect(screen.getByTestId('sticky-tape')).toBeTruthy()
    expect(note.style.boxShadow).toBe('')
    expect(note.style.transform).toBe(`rotate(${noteTilt('n1')}deg)`)
    expect(Math.abs(noteTilt('n1'))).toBeLessThanOrEqual(3)
    expect(noteTilt('n1')).toBe(noteTilt('n1'))
    act(() => { fireEvent.doubleClick(canvas(), at(50, 50)) })
    expect(screen.getByTestId('canvas-shape').style.transform).toBe('')
    // Only the drawing leans: no border picker for paper notes.
    act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
    expect(screen.queryByLabelText('Border')).toBeNull()
  })

  it('a dock tool shows its name and key in a tooltip after the pointer rests on it, and only that one', () => {
    vi.useFakeTimers()
    try {
      render(<Harness initial={emptyCanvas()} />)
      fireEvent.mouseEnter(screen.getByTestId('tool-text'))
      expect(screen.queryByTestId('canvas-tool-tip')).toBeNull()
      act(() => { vi.advanceTimersByTime(600) })
      const tips = screen.getAllByTestId('canvas-tool-tip')
      expect(tips).toHaveLength(1)
      expect(tips[0].textContent).toContain('Text')
      expect(tips[0].textContent).toContain('T')
      expect(canvas().contains(tips[0])).toBe(false)
      fireEvent.mouseLeave(screen.getByTestId('tool-text'))
      expect(screen.queryByTestId('canvas-tool-tip')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a selected connector shows its end handles and a toolbar that restyles it, one undo step each', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    expect(screen.getByTestId('canvas-connector').dataset.line).toBe('elbow')
    act(() => { fireEvent.pointerDown(screen.getByTestId('canvas-connector'), at(300, 100)) })
    expect(screen.getByTestId('conn-end-from')).toBeTruthy()
    expect(screen.getByTestId('conn-end-to')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Curved line'))
    fireEvent.click(screen.getByLabelText('Dashed line'))
    fireEvent.click(screen.getByLabelText('Arrows at both ends'))
    fireEvent.click(screen.getByLabelText('Line colour'))
    fireEvent.click(screen.getByLabelText('Line colour: Green'))
    expect(latest().connectors[0]).toMatchObject({ line: 'curve', dashed: true, arrows: 'both', color: 'green' })
    expect(screen.getByTestId('canvas-connector').dataset.line).toBe('curve')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().connectors[0]).toMatchObject({ line: 'curve', dashed: true, arrows: 'both' })
    expect(latest().connectors[0].color).toBeUndefined()
    fireEvent.click(screen.getByLabelText('Delete connector'))
    expect(latest().connectors).toEqual([])
  })

  it('a selected elbow has a handle per segment; dragging one reshapes the route as one undo step', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    act(() => { fireEvent.pointerDown(screen.getByTestId('canvas-connector'), at(300, 100)) })
    const segs = screen.getAllByTestId('conn-seg')
    expect(segs).toHaveLength(1)
    act(() => { fireEvent.pointerDown(segs[0], at(300, 100)) })
    moveTo(300, 203)
    release(300, 203)
    // A straight run between facing sides becomes a step down, with a stub kept on each shape.
    expect(latest().connectors[0].route).toEqual([{ x: 224, y: 100 }, { x: 224, y: 204 }, { x: 376, y: 204 }, { x: 376, y: 100 }])
    expect(screen.getAllByTestId('conn-seg').length).toBeGreaterThan(1)
    // Switching the line type drops the hand-made route.
    fireEvent.click(screen.getByLabelText('Straight line'))
    expect(latest().connectors[0].route).toBeUndefined()
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().connectors[0]).toEqual({ id: 'c1', from: 'a', to: 'b' })
  })

  it('dragging a connector\'s end handle re-attaches it to another shape\'s side', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0), sticky('c', 400, 400)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    act(() => { fireEvent.pointerDown(screen.getByTestId('canvas-connector'), at(300, 100)) })
    act(() => { fireEvent.pointerDown(screen.getByTestId('conn-end-to'), at(400, 100)) })
    moveTo(500, 404)
    release(500, 404)
    expect(latest().connectors).toEqual([{ id: 'c1', from: 'a', to: 'c', toSide: 'top' }])
    // An end dropped on empty board leaves the connector as it was.
    act(() => { fireEvent.pointerDown(screen.getByTestId('conn-end-to'), at(500, 400)) })
    moveTo(1200, 1200)
    release(1200, 1200)
    expect(latest().connectors).toEqual([{ id: 'c1', from: 'a', to: 'c', toSide: 'top' }])
  })

  it('the colour picker opens FigJam\'s two rows, and closes on a pick or Escape', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    expect(screen.queryByTestId('swatch-panel')).toBeNull()
    fireEvent.click(screen.getByLabelText('Colour'))
    const panel = screen.getByTestId('swatch-panel')
    // The fill modes, then the strong colours over the light ones.
    const rows = panel.children
    expect(rows).toHaveLength(3)
    expect(rows[0].children).toHaveLength(3)
    expect(rows[1].children).toHaveLength(11)
    // The light colours, then the free colour picker.
    expect(rows[2].children).toHaveLength(11)
    fireEvent.click(screen.getByLabelText('Colour: Mint'))
    expect(latest().shapes[0].color).toBe('mint')
    expect(screen.queryByTestId('swatch-panel')).toBeNull()
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('swatch-panel')).toBeNull()
  })

  it('restacks a step only past a shape it overlaps, and to the front or back in one go', () => {
    const b = { ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('far', 2000, 0), sticky('b', 100, 100)] }
    const order = (x: Canvas) => x.shapes.map(s => s.id)
    expect(order(restack(b, new Set(['a']), 'forward'))).toEqual(['far', 'b', 'a'])
    expect(order(restack(b, new Set(['b']), 'backward'))).toEqual(['b', 'a', 'far'])
    expect(order(restack(b, new Set(['b']), 'back'))).toEqual(['b', 'a', 'far'])
    expect(order(restack(b, new Set(['a', 'far']), 'front'))).toEqual(['b', 'a', 'far'])
    // Already on top: forward changes nothing.
    expect(order(restack(b, new Set(['b']), 'forward'))).toEqual(['a', 'far', 'b'])
  })

  it('the toolbar sets a shape\'s fill mode, outline weight and kind, each one undo step', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Colour'))
    expect(screen.getByRole('radio', { name: 'Fill' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'No fill' }))
    expect(latest().shapes[0].fill).toBe('none')
    const shape = screen.getByTestId('canvas-shape')
    expect(shape.style.background).toContain('transparent')
    fireEvent.click(screen.getByRole('radio', { name: 'Transparent' }))
    expect(latest().shapes[0].fill).toBe('transparent')
    // Back to solid drops the field, so older files stay as they were.
    fireEvent.click(screen.getByRole('radio', { name: 'Fill' }))
    expect(latest().shapes[0].fill).toBeUndefined()
    // The border has its own picker: style, colour and weight.
    fireEvent.click(screen.getByLabelText('Border'))
    expect(screen.getByRole('radio', { name: 'Solid' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'Thick' }))
    expect(latest().shapes[0].weight).toBe('thick')
    // On a sticker the weight is the edge's: thick is 8 wide.
    expect(screen.getByTestId('canvas-shape').style.boxShadow).toContain('8px')
    fireEvent.click(screen.getByRole('radio', { name: 'Dashed' }))
    expect(latest().shapes[0].border).toBe('dashed')
    // Drawn as SVG strokes: each side fitted with whole dashes, the corners solid.
    const outline = screen.getByTestId('dashed-outline')
    expect(outline.querySelectorAll('line')).toHaveLength(4)
    expect(screen.getByTestId('dashed-corners')).toBeTruthy()
    expect(screen.getByTestId('canvas-shape').style.borderStyle).toBe('solid')
    fireEvent.click(screen.getByLabelText('Border: Red'))
    expect(latest().shapes[0].borderColor).toBe('red')
    // The fill keeps its own colour.
    expect(latest().shapes[0].color).toBe('blue')
    fireEvent.click(screen.getByLabelText('Border'))
    fireEvent.click(screen.getByRole('radio', { name: 'White (default)' }))
    expect(latest().shapes[0].borderColor).toBeUndefined()
    // The panel stays open after a mode or the default, like FigJam's.
    fireEvent.click(screen.getByRole('radio', { name: 'None' }))
    expect(latest().shapes[0].border).toBe('none')
    fireEvent.click(screen.getByRole('radio', { name: 'Solid' }))
    expect(latest().shapes[0].border).toBeUndefined()
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByLabelText('Shape'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Ellipse' }))
    expect(latest().shapes[0]).toMatchObject({ kind: 'ellipse', x: 0, y: 0, w: 240, h: 120, color: 'blue', weight: 'thick' })
    fireEvent.click(screen.getByLabelText('Shape'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Sticky note' }))
    expect(latest().shapes[0]).toMatchObject({ kind: 'sticky', w: 240, h: 240 })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0].kind).toBe('ellipse')
  })

  it('a solid shape is a sticker whose edge takes the border colour and weight; only dashes bring an outline back', () => {
    const base = { color: 'blue' as const }
    expect(shapeLook(base)).toMatchObject({ fill: '#3FAAFF', border: 'transparent', width: 0, dashed: false, solid: true, sticker: true, edge: '#FFFFFF', edgeWidth: 5 })
    // The border colour and weight paint the edge, black as black; none drops the edge but keeps the lift.
    expect(shapeLook({ ...base, weight: 'thick', borderColor: 'black' })).toMatchObject({ sticker: true, edge: '#1E1E1E', edgeWidth: 8 })
    expect(shapeLook({ ...base, border: 'none' })).toMatchObject({ sticker: true, edgeWidth: 0 })
    // Dashes are an outline, in a deeper shade of the fill.
    expect(shapeLook({ ...base, border: 'dashed', weight: 'thick' })).toMatchObject({ border: '#0B78D6', width: 6, sticker: false })
    // An unfilled or see-through shape keeps its outline: an edge around nothing reads as a hole.
    expect(shapeLook({ ...base, fill: 'transparent' }).sticker).toBe(false)
    expect(shapeLook({ ...base, fill: 'none' })).toMatchObject({ fill: 'transparent', border: '#3FAAFF' })
    expect(shapeLook({ color: 'black', fill: 'none' }).border).toBe('var(--text)')
    expect(shapeLook({ ...base, border: 'dashed', borderColor: 'red' }).border).toBe('#F2573C')
    expect(shapeLook({ ...base, border: 'dashed', weight: 'thin' })).toMatchObject({ dashed: true, width: 2 })
    expect(shapeLook({ ...base, fill: 'none', border: 'none', borderColor: 'red' }).border).toBe('transparent')
  })

  it('dashed outlines are drawn for every kind of shape that has a border, and not for a solid one', () => {
    const shapes: Shape[] = [
      { ...sticky('r', 0, 0), kind: 'rect', border: 'dashed' },
      { ...sticky('e', 300, 0), kind: 'ellipse', border: 'dashed' },
      { ...sticky('s', 600, 0), kind: 'section', fill: 'none', border: 'dashed' },
      { ...sticky('d', 900, 0), kind: 'diamond', border: 'dashed' },
      { ...sticky('x', 1200, 0), kind: 'rect' },
    ]
    render(<Harness initial={{ ...emptyCanvas(), shapes }} />)
    // Sections draw first, then the rectangle, the ellipse and the diamond.
    const outlines = screen.getAllByTestId('dashed-outline')
    expect(outlines).toHaveLength(4)
    expect(outlines.map(o => o.querySelectorAll('line').length)).toEqual([4, 4, 0, 4])
    expect(outlines[2].querySelector('ellipse')).toBeTruthy()
    // The diamond's own polygon is only the fill; its corners get a disc each so none is left open.
    expect(document.querySelector('[data-shape-id="d"] polygon')!.getAttribute('stroke')).toBe('none')
    expect(outlines[3].querySelectorAll('circle')).toHaveLength(4)
  })

  it('dashes fit whole to each side, so a corner always lands on a dash', () => {
    // A 200-unit side at width 3.5: 19.25 dash, 10.5 gap -> 7 dashes and 6 gaps exactly fill it.
    const fit = fitDash(200, 3.5)!.split(' ').map(Number)
    expect(fit[1]).toBeCloseTo(10.5, 10)
    expect(7 * fit[0] + 6 * fit[1]).toBeCloseTo(200, 8)
    // Whatever the length, a whole number of dashes and gaps spans it end to end.
    for (const len of [37, 90, 123.4, 480, 1001]) {
      const [d, g] = fitDash(len, 3.5)!.split(' ').map(Number)
      const n = Math.round((len + g) / (d + g))
      expect(n * d + (n - 1) * g).toBeCloseTo(len, 8)
      expect(d).toBeGreaterThan(g)
    }
    // Too short for two dashes: solid.
    expect(fitDash(20, 3.5)).toBeUndefined()
    const [pd, pg] = ovalDash(100, 60, 3.5)!.split(' ').map(Number)
    const perimeter = Math.PI * (3 * 160 - Math.sqrt((3 * 100 + 60) * (100 + 3 * 60)))
    expect(Math.round(perimeter / (pd + pg)) * (pd + pg)).toBeCloseTo(perimeter, 6)
  })

  it('colour maths: hex in many spellings, a round trip through HSV, a darker border, readable ink', () => {
    expect(toHex('#abc')).toBe('#AABBCC')
    expect(toHex('3fAAff')).toBe('#3FAAFF')
    expect(toHex('#12345')).toBeUndefined()
    expect(toHex('blue')).toBeUndefined()
    expect(toHex(7)).toBeUndefined()
    for (const hex of ['#3FAAFF', '#F2573C', '#000000', '#FFFFFF', '#808080', '#874FFF']) expect(hsvToHex(hexToHsv(hex))).toBe(hex)
    // Darker keeps the hue and loses brightness; white and black stay in range.
    const d = hexToHsv(darker('#FFC83D'))
    expect(d.h).toBeCloseTo(hexToHsv('#FFC83D').h, 0)
    expect(d.v).toBeCloseTo(hexToHsv('#FFC83D').v * 0.78, 1)
    expect(luminance(darker('#FFC83D'))).toBeLessThan(luminance('#FFC83D'))
    expect(darker('#FFFFFF')).toBe('#C7C7C7')
    expect(darker('#000000')).toBe('#000000')
    expect(inkOn('#FFC83D')).toBe('#1F2328')
    expect(inkOn('#1E1E1E')).toBe('#FFFFFF')
    expect(inkOn('#874FFF')).toBe('#FFFFFF')
  })

  it('a custom fill colour picks its own darker border and a readable ink', () => {
    expect(shapeLook({ color: 'blue', hex: '#FFC83D' })).toMatchObject({ fill: '#FFC83D', sticker: true, ink: '#1F2328' })
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', border: 'dashed' }).border).toBe(darker('#FFC83D'))
    expect(shapeLook({ color: 'blue', hex: '#1E2A5E' }).ink).toBe('#FFFFFF')
    // With no fill, the custom colour is the outline itself; a border colour of its own wins over both.
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', fill: 'none' }).border).toBe('#FFC83D')
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', borderHex: '#112233' }).edge).toBe('#112233')
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', borderColor: 'red' }).edge).toBe('#F2573C')
  })

  it('the free colour picker sets a fill colour live, as one undo step, and a swatch swaps it out', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.click(screen.getByLabelText('Colour: custom colour'))
    const hexField = screen.getByLabelText('Hex colour') as HTMLInputElement
    // Typing shows each valid colour at once...
    fireEvent.focus(hexField)
    fireEvent.change(hexField, { target: { value: '#FFC83D' } })
    expect(latest().shapes[0].hex).toBe('#FFC83D')
    expect(screen.getByTestId('canvas-shape').style.background.toLowerCase()).toBe('#ffc83d')
    fireEvent.change(hexField, { target: { value: '#12AB' } })
    expect(latest().shapes[0].hex).toBe('#FFC83D')
    fireEvent.change(hexField, { target: { value: '1e2a5e' } })
    expect(latest().shapes[0].hex).toBe('#1E2A5E')
    // ...and leaving the field records the whole edit as one undo step.
    fireEvent.blur(hexField)
    expect(reread(serializeCanvas(latest())).shapes[0].hex).toBe('#1E2A5E')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0].hex).toBeUndefined()
    expect(latest().shapes[0].color).toBe('blue')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true, shiftKey: true }) })
    expect(latest().shapes[0].hex).toBe('#1E2A5E')
    // A swatch swaps the custom colour out (the panel is still open).
    fireEvent.click(screen.getByLabelText('Colour: Mint'))
    expect(latest().shapes[0]).toMatchObject({ color: 'mint' })
    expect(latest().shapes[0].hex).toBeUndefined()
  })

  it('the square and the hue bar move the colour; the border has a free colour of its own', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', hex: '#FF0000', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Border'))
    fireEvent.click(screen.getByLabelText('Border: custom colour'))
    fireEvent.change(screen.getByLabelText('Hex colour'), { target: { value: '#00AA55' } })
    fireEvent.blur(screen.getByLabelText('Hex colour'))
    expect(latest().shapes[0]).toMatchObject({ hex: '#FF0000', borderHex: '#00AA55' })
    // Picking the default gives the sticker its white edge back.
    fireEvent.click(screen.getByRole('radio', { name: 'White (default)' }))
    expect(latest().shapes[0].borderHex).toBeUndefined()
    expect(screen.getByTestId('custom-hue').getAttribute('aria-valuenow')).toBeTruthy()
    expect(screen.getByTestId('custom-square')).toBeTruthy()
  })

  it('sections get no connect dots and are not joined by lines; the dots go once zoomed far out', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 100, w: 500, h: 300, text: 'To do', color: 'smoke' }
    render(<Harness initial={{ ...emptyCanvas(), shapes: [section, sticky('a', 600, 100)] }} />)
    const hover = (x: number, y: number) => act(() => { fireEvent.pointerMove(canvas(), at(x, y)) })
    // Hovering or selecting a section (by its label) shows no dots.
    hover(100, 80)
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
    press(100, 80)
    release(100, 80)
    hover(900, 900)
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
    // A shape beside it has them, and a line dragged from there passes over the section without joining it.
    press(700, 200)
    release(700, 200)
    hover(900, 900)
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    act(() => { fireEvent.pointerDown(screen.getAllByTestId('connect-dot')[3], at(586, 200)) })
    moveTo(200, 250)
    release(200, 250)
    expect(latest().connectors).toEqual([])
    // The connector tool does not start a line from a section's label either.
    fireEvent.click(screen.getByTestId('tool-connector'))
    press(100, 80)
    moveTo(700, 200)
    release(700, 200)
    expect(latest().connectors).toEqual([])
    // Zoomed out to just under half, the dots are gone from a shape that has them at 100%.
    fireEvent.click(screen.getByTestId('tool-select'))
    press(700, 200)
    release(700, 200)
    zoomStep('Zoom out')
    zoomStep('Zoom out')
    zoomStep('Zoom out')
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    zoomStep('Zoom out')
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
    zoomStep('Zoom in')
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
  })

  it('a selected shape keeps its four connect dots, and they go with the selection', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.pointerMove(canvas(), at(900, 900)) })
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    act(() => { fireEvent.keyDown(canvas(), { key: 'Escape' }) })
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
  })

  it('marquee selects what it touches; the toolbar recolours the selection', () => {
    render(<Harness initial={{ ...emptyCanvas(), shapes: [sticky('a', 0, 0), sticky('b', 300, 0), sticky('c', 0, 600)] }} />)
    press(-50, -50)
    moveTo(550, 250)
    release(550, 250)
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.click(screen.getByLabelText('Colour: Blue'))
    expect(latest().shapes.map(s => s.color)).toEqual(['blue', 'blue', 'yellow'])
  })

})
