/**
 * The whiteboard: its data (parse, summary, connector geometry, the conductor's
 * additions), its turn, and the canvas driven through real pointer and key events.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import BoardCanvas from '../pages/chat/page/board/BoardCanvas'
import {
  BOARD_ID_PREFIX, applyBoardAdd, applyBoardEdit, boardAddsIn, boardEditsIn, conductorAccessOf, emptyBoard, isBoardId, labelScale, parseBoard, sectionMembers, serializeBoard, shapeAt, summarizeBoard, toSwatch, wheelPanStep, wheelZoomFactor,
  type Board, type Shape,
} from '../pages/chat/page/board/boardModel'
import { applyReplyChanges, applyReplyChecked, boardAddsFromMessages, boardChangesFromMessages, buildBoardTurn } from '../pages/chat/page/board/boardTurn'
import type { OverlayItem } from '../pages/chat/page/docOverlay'
import { deriveThreads } from '../pages/chat/page/docThreads'
import { fitDash, ovalDash } from '../pages/chat/page/board/dashFit'
import { darker, hexToHsv, hsvToHex, inkOn, luminance, toHex } from '../pages/chat/page/board/colorMath'
import { shapeLook } from '../pages/chat/page/board/shapeLook'
import { firstLine } from '../pages/chat/page/board/ConductorThread'
import { restack } from '../pages/chat/page/board/boardEditing'
import { connectorRoute, connectorSides, movedSegment, segmentDrag } from '../pages/chat/page/board/connectorGeometry'

const sticky = (id: string, x: number, y: number, text = ''): Shape => ({ id, kind: 'sticky', x, y, w: 200, h: 200, text, color: 'yellow' })

describe('board model', () => {
  it('knows a board id by its prefix', () => {
    expect(isBoardId(`${BOARD_ID_PREFIX}0123456789abcdef0123456789a`)).toBe(true)
    expect(isBoardId('0123456789abcdef0123456789abcdef')).toBe(false)
  })

  it('round-trips through its file, and drops what it cannot read', () => {
    const b: Board = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Idea')], connectors: [{ id: 'c', from: 'a', to: 'gone' }] }
    const back = parseBoard(serializeBoard(b))
    expect(back.shapes).toEqual(b.shapes)
    expect(back.connectors).toEqual([])
    expect(parseBoard('not json').shapes).toEqual([])
    expect(parseBoard('').kind).toBe('kirocrew-board')
  })

  it('a connector joins the facing sides at their midpoints, and is an elbow unless told otherwise', () => {
    const r = connectorRoute(sticky('a', 0, 0), sticky('b', 400, 0), {})
    expect(r.a).toEqual({ x: 200, y: 100 })
    expect(r.b).toEqual({ x: 400, y: 100 })
    // Level shapes: the elbow has no corner left, it is one straight run.
    expect(r.points).toEqual([{ x: 200, y: 100 }, { x: 400, y: 100 }])
    expect(connectorSides(sticky('a', 0, 0), sticky('b', 0, 400))).toEqual(['bottom', 'top'])
  })

  it('the drawn line stops short of an arrowhead\'s shape, while the route\'s ends stay on the sides', () => {
    const a = sticky('a', 0, 0)
    const b = sticky('b', 400, 0)
    const arrow = connectorRoute(a, b, { line: 'straight' })
    expect(arrow.d).toBe('M200,100 L391,100')
    expect(arrow.b).toEqual({ x: 400, y: 100 })
    expect(connectorRoute(a, b, { line: 'straight', arrows: 'none' }).d).toBe('M200,100 L400,100')
    expect(connectorRoute(a, b, { line: 'straight', arrows: 'both' }).d).toBe('M209,100 L391,100')
    expect(connectorRoute(a, b, { line: 'curve' }).d).toMatch(/ 391,100$/)
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
    const b: Board = {
      ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)],
      connectors: [{ id: 'c', from: 'a', to: 'b', fromSide: 'right', toSide: 'top', line: 'curve', color: 'green', dashed: true, arrows: 'both' }],
    }
    expect(parseBoard(serializeBoard(b)).connectors).toEqual(b.connectors)
    const odd = JSON.stringify({ ...b, connectors: [{ id: 'c', from: 'a', to: 'b', fromSide: 'middle', line: 'zigzag', color: 'neon', arrows: 3 }] })
    expect(parseBoard(odd).connectors).toEqual([{ id: 'c', from: 'a', to: 'b' }])
  })

  it('summarizes the board for the conductor', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Pricing'), sticky('b', 300, 0)], connectors: [{ id: 'c', from: 'a', to: 'b' }] }
    const s = summarizeBoard(b)
    expect(s).toContain('- a | sticky | yellow | at (0, 0) size 200x200 | "Pricing"')
    expect(s).toContain('- b | sticky | yellow | at (300, 0) size 200x200 | (no text)')
    expect(s).toContain('Connections:\n- a -> b')
  })

  it('a conductor adds a column under its header by default, connectors by ref, nothing twice', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Pricing')] }
    const next = applyBoardAdd(b, {
      shapes: [{ ref: 'n1', text: 'Free tier', near: 'a' }, { ref: 'n2', text: 'Pro tier', near: 'a', color: 'blue' }, { text: 'Pricing' }],
      connectors: [{ from: 'a', to: 'n1' }, { from: 'a', to: 'n2' }],
    })
    expect(next.shapes[0]).toEqual(b.shapes[0])
    expect(next.shapes.map(s => s.text)).toEqual(['Pricing', 'Free tier', 'Pro tier'])
    expect(next.shapes[1]).toMatchObject({ x: 0, y: 224 })
    expect(next.shapes[2].x).toBe(0)
    expect(next.shapes[2].y).toBeGreaterThanOrEqual(next.shapes[1].y + 200)
    expect(next.shapes[2].color).toBe('blue')
    expect(next.connectors.map(c => [c.from, c.to])).toEqual([['a', next.shapes[1].id], ['a', next.shapes[2].id]])
  })

  it('side right puts new shapes beside their neighbour, and no new shape lands on another', () => {
    const header = sticky('h', 0, 0, 'Doing')
    const inTheWay = sticky('w', 0, 240, 'Already here')
    const next = applyBoardAdd({ ...emptyBoard(), shapes: [header, inTheWay] }, {
      shapes: [{ text: 'Below', near: 'h' }, { text: 'Beside', near: 'h', side: 'right' }],
    })
    const below = next.shapes.find(s => s.text === 'Below')!
    const beside = next.shapes.find(s => s.text === 'Beside')!
    expect(below.y).toBeGreaterThan(inTheWay.y + inTheWay.h)
    expect(beside).toMatchObject({ x: 248, y: 0 })
    const clash = (a: Shape, c: Shape) => a.x < c.x + c.w && a.x + a.w > c.x && a.y < c.y + c.h && a.y + a.h > c.y
    for (const s of next.shapes) for (const t of next.shapes) if (s !== t) expect(clash(s, t)).toBe(false)
  })

  it('reads board-add blocks from a reply, skipping ones that are not JSON', () => {
    const text = 'Added two.\n```board-add\n{"shapes":[{"text":"x"}]}\n```\n```board-add\nnope\n```'
    expect(boardAddsIn(text)).toEqual([{ shapes: [{ text: 'x' }] }])
    expect(boardAddsFromMessages([{ role: 'assistant', content: text, cls: '', meta: { mid: 'm1' } }])).toEqual([{ key: 'm1', adds: [{ shapes: [{ text: 'x' }] }] }])
  })

  it('a conductor edit changes only the fields it names, and skips ids that are not on the board', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Old'), sticky('b', 300, 0, 'Keep')] }
    const next = applyBoardEdit(b, { shapes: [{ id: 'a', x: 40, text: 'New', color: 'green', kind: 'rect' }, { id: 'ghost', text: 'x' }] })
    expect(next.shapes[0]).toEqual({ ...b.shapes[0], x: 40, text: 'New', color: 'green', kind: 'rect' })
    expect(next.shapes[1]).toBe(b.shapes[1])
    expect(next.shapes).toHaveLength(2)
  })

  it('a bad value is ignored rather than written', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0)] }
    const next = applyBoardEdit(b, { shapes: [{ id: 'a', x: 'far' as unknown as number, color: 'neon' as unknown as 'blue', kind: 'blob' as unknown as 'rect', w: 2 }] })
    expect(next.shapes[0]).toEqual({ ...b.shapes[0], w: 24 })
  })

  it('deleting a shape takes its connectors and its asks; connectors can go on their own', () => {
    const b: Board = {
      ...emptyBoard(),
      shapes: [sticky('a', 0, 0), sticky('b', 300, 0), sticky('c', 600, 0)],
      connectors: [{ id: 'c1', from: 'a', to: 'b' }, { id: 'c2', from: 'b', to: 'c' }],
      asks: [{ id: 'q', shapeIds: ['a'] }],
    }
    expect(applyBoardEdit(b, { delete: ['a'] })).toMatchObject({ connectors: [{ id: 'c2' }], asks: [] })
    expect(applyBoardEdit(b, { deleteConnectors: [{ from: 'b', to: 'c' }] }).connectors.map(c => c.id)).toEqual(['c1'])
    expect(applyBoardEdit(b, { deleteConnectors: ['c1'] }).connectors.map(c => c.id)).toEqual(['c2'])
  })

  it('a reply\'s edits and additions are read together and applied edits first', () => {
    const text = 'Tidied.\n```board-edit\n{"shapes":[{"id":"a","text":"Pricing"}]}\n```\n```board-add\n{"shapes":[{"text":"Free","near":"a"}]}\n```'
    expect(boardEditsIn(text)).toEqual([{ shapes: [{ id: 'a', text: 'Pricing' }] }])
    const [change] = boardChangesFromMessages([{ role: 'assistant', content: text, cls: '', meta: { mid: 'r9' } }])
    expect(change.key).toBe('r9')
    const next = applyReplyChanges({ ...emptyBoard(), shapes: [sticky('a', 0, 0, 'pricng')] }, change)
    expect(next.shapes.map(s => s.text)).toEqual(['Pricing', 'Free'])
    expect(deriveThreads([
      { role: 'user', content: '[Doc ask q]\nfix\n\n---\nx', cls: '' },
      { role: 'assistant', content: text, cls: '' },
    ]).q.messages.at(-1)).toEqual({ who: 'conductor', text: 'Tidied.' })
  })

  it('the turn lets the conductor edit only when asked, and says one undo takes a reply back', () => {
    const turn = buildBoardTurn({ id: 'q', request: 'tidy', board: emptyBoard(), selected: [], followUp: false, docId: 'b0a4d1' })
    expect(turn).toContain('Change the board ONLY when the person asks you to')
    expect(turn).toContain('```board-edit')
    expect(turn).toContain('undone by a single undo')
    expect(turn).not.toContain('never move, edit or delete')
    // Status goes on the cards the work is about, in HTML it writes itself.
    expect(turn).toContain('data-place="corner"')
    expect(turn).toContain('ov-status ov-running')
    expect(turn).toContain('The HTML is yours to write')
  })

  it('its turn carries the board and selection, opens like a doc ask, and hides board-add from the answer', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Pricing')] }
    const turn = buildBoardTurn({ id: 'q1', request: 'brainstorm tiers', board: b, selected: ['a'], followUp: false, docId: 'b0a4d1', title: 'Launch' })
    expect(turn.startsWith('[Doc ask q1]\nbrainstorm tiers\n\n---\n')).toBe(true)
    expect(turn).toContain('They pointed at a ')
    expect(turn).toContain('"Pricing"')
    expect(turn).toContain('slug "doc-overlay-b0a4d1"')
    expect(turn).toContain('data-anchor="shape:<shape id>"')
    const threads = deriveThreads([
      { role: 'user', content: turn, cls: '' },
      { role: 'assistant', content: 'Three ideas.\n```board-add\n{"shapes":[]}\n```', cls: '' },
    ])
    expect(threads.q1.messages.at(-1)).toEqual({ who: 'conductor', text: 'Three ideas.' })
  })
})

describe('sections lay out their own cards', () => {
  const kanban = () => applyBoardAdd(emptyBoard(), {
    shapes: [
      { ref: 'todo', kind: 'section', text: 'To do' },
      { ref: 'done', kind: 'section', text: 'Done' },
      { kind: 'sticky', text: '#1 Short', in: 'todo' },
      { kind: 'sticky', text: '#2 A much longer card title that has to wrap onto several lines to fit', in: 'todo' },
      { kind: 'sticky', text: '#3 Third', in: 'todo' },
    ],
  })
  const overlapping = (a: Shape, b: Shape) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y

  it('cards added "in" a section stack inside it, never overlapping, and the section grows to fit', () => {
    const b = kanban()
    const [todo, done] = b.shapes.filter(s => s.kind === 'section')
    const cards = sectionMembers(b.shapes, todo)
    expect(cards.map(c => c.text.slice(0, 2))).toEqual(['#1', '#2', '#3'])
    for (const c of cards) {
      expect(c.x).toBeGreaterThanOrEqual(todo.x)
      expect(c.x + c.w).toBeLessThanOrEqual(todo.x + todo.w)
      expect(c.y + c.h).toBeLessThanOrEqual(todo.y + todo.h)
    }
    for (let i = 1; i < cards.length; i++) expect(overlapping(cards[i - 1], cards[i])).toBe(false)
    // Each card is as tall as its text: the long one is taller than the short one.
    expect(cards[1].h).toBeGreaterThan(cards[0].h)
    // Sections line up side by side and do not overlap.
    expect(overlapping(todo, done)).toBe(false)
    expect(done.y).toBe(todo.y)
  })

  it('"in" on an edit moves a card across columns, and both columns re-flow', () => {
    const b = kanban()
    const [todo, done] = b.shapes.filter(s => s.kind === 'section')
    const first = sectionMembers(b.shapes, todo)[0]
    const next = applyBoardEdit(b, { shapes: [{ id: first.id, in: done.id }] })
    const t = next.shapes.find(s => s.id === todo.id)!
    const d = next.shapes.find(s => s.id === done.id)!
    expect(sectionMembers(next.shapes, d).map(c => c.id)).toEqual([first.id])
    const left = sectionMembers(next.shapes, t)
    expect(left.map(c => c.text.slice(0, 2))).toEqual(['#2', '#3'])
    // The column closed the gap: its first card is back at the top.
    expect(left[0].y).toBe(sectionMembers(b.shapes, todo)[0].y)
    expect(summarizeBoard(next)).toContain(`contains: ${first.id}`)
  })

  it('a section is picked by its title tab, so a press on its body reaches the board', () => {
    const b = kanban()
    const todo = b.shapes.find(s => s.kind === 'section')!
    expect(shapeAt(b.shapes, { x: todo.x + 10, y: todo.y - 10 })?.id).toBe(todo.id)
    expect(shapeAt(b.shapes, { x: todo.x + 4, y: todo.y + todo.h - 4 })).toBeNull()
    const card = sectionMembers(b.shapes, todo)[0]
    expect(shapeAt(b.shapes, { x: card.x + 5, y: card.y + 5 })?.id).toBe(card.id)
  })
})

describe('the canvas', () => {
  function Harness({ initial, ...rest }: { initial: Board } & Partial<React.ComponentProps<typeof BoardCanvas>>) {
    const [board, setBoard] = useState(initial)
    ;(Harness as unknown as { last: Board }).last = board
    return (
      <div style={{ width: 1200, height: 800 }}>
        <BoardCanvas board={board} onChange={setBoard} overlay={[]} threads={{}} onAsk={vi.fn()} onFollowUp={vi.fn()} onOpenSession={vi.fn()} onReadAsk={vi.fn()} {...rest} />
      </div>
    )
  }
  const latest = () => (Harness as unknown as { last: Board }).last
  const canvas = () => screen.getByTestId('board-canvas')
  /** World = screen minus the starting view offset (120, 100) at zoom 1. */
  const at = (x: number, y: number) => ({ clientX: x + 120, clientY: y + 100, button: 0, pointerId: 1 })
  const press = (x: number, y: number, extra = {}) => act(() => { fireEvent.pointerDown(canvas(), { ...at(x, y), ...extra }) })
  const moveTo = (x: number, y: number, extra = {}) => act(() => { fireEvent.pointerMove(window, { ...at(x, y), ...extra }) })
  const release = (x: number, y: number) => act(() => { fireEvent.pointerUp(window, at(x, y)) })

  it('the dock\'s sticky tool drops a sticky where you click, ready to type into', () => {
    render(<Harness initial={emptyBoard()} />)
    expect(screen.getByText('A blank board')).toBeTruthy()
    fireEvent.click(screen.getByTestId('tool-sticky'))
    press(300, 300)
    release(300, 300)
    expect(latest().shapes).toHaveLength(1)
    // Centred on the click, then onto the 12px snap grid.
    expect(latest().shapes[0]).toMatchObject({ kind: 'sticky', x: 204, y: 204 })
    const editor = screen.getByTestId('board-shape-editor') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'An idea' } })
    act(() => { editor.blur() })
    expect(latest().shapes[0].text).toBe('An idea')
  })

  it('dragging with a shape tool sizes the new shape, live, as one undo step', () => {
    render(<Harness initial={emptyBoard()} />)
    fireEvent.click(screen.getByTestId('tool-rect'))
    press(100, 100)
    moveTo(190, 150)
    // It exists while you drag, so you can see what you are sizing; both corners snap.
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'rect', x: 96, y: 96, w: 96, h: 60 })])
    moveTo(50, 290)
    release(50, 290)
    // Dragging up-left of the start flips the box instead of going negative.
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'rect', x: 48, y: 96, w: 48, h: 192 })])
    expect(screen.getByTestId('board-shape-editor')).toBeTruthy()
    act(() => { (screen.getByTestId('board-shape-editor') as HTMLTextAreaElement).blur() })
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes).toHaveLength(0)
  })

  it('a conductor change committed through the canvas is undone by one Cmd+Z, whatever it changed', () => {
    const api: { current: { commit: (next: Board) => void } | null } = { current: null }
    const start: Board = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'One'), sticky('b', 300, 0, 'Two')] }
    render(<Harness initial={start} apiRef={api} />)
    const reply = applyReplyChanges(start, {
      edits: [{ shapes: [{ id: 'a', x: 500, text: 'Moved' }], delete: ['b'] }],
      adds: [{ shapes: [{ text: 'New' }, { text: 'Also new' }] }],
    })
    act(() => { api.current!.commit(reply) })
    expect(latest().shapes.map(s => s.text)).toEqual(['Moved', 'New', 'Also new'])
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest()).toEqual(start)
  })

  it('the text tool keeps the new text box: the press that makes it does not take focus away', () => {
    render(<Harness initial={emptyBoard()} />)
    fireEvent.click(screen.getByTestId('tool-text'))
    let notPrevented = true
    act(() => { notPrevented = fireEvent.pointerDown(canvas(), at(300, 300)) })
    // A browser would move focus to the canvas on this press and end the edit.
    expect(notPrevented).toBe(false)
    release(300, 300)
    expect(latest().shapes).toEqual([expect.objectContaining({ kind: 'text' })])
    expect(screen.getByTestId('board-shape-editor')).toBeTruthy()
  })

  it('dragging a shape moves it; Delete removes it; undo brings each step back', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Move me')] }} />)
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [section, card] }} />)
    press(10, -10)
    moveTo(106, 86)
    release(106, 86)
    expect(latest().shapes.find(s => s.id === 'sec')).toMatchObject({ x: 96, y: 96 })
    expect(latest().shapes.find(s => s.id === 'c')).toMatchObject({ x: 116, y: 116 })
  })

  it('a corner handle resizes; a sticky stays square', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.pointerDown(screen.getByTestId('handle-se'), at(200, 200)) })
    moveTo(290, 260)
    release(290, 260)
    // The dragged corner snaps to (288, 264); a sticky takes the larger side.
    expect(latest().shapes[0]).toMatchObject({ w: 288, h: 288 })
  })

  it('dragging a side of the selected shape moves only that side; a sticky stays square', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyBoard(), shapes: [rect, sticky('s', 600, 0)] }} />)
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} />)
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)] }} />)
    fireEvent.click(screen.getByTestId('tool-connector'))
    press(100, 100)
    moveTo(500, 100)
    release(500, 100)
    expect(latest().connectors).toEqual([expect.objectContaining({ from: 'a', to: 'b' })])
    expect(screen.getAllByTestId('board-connector')).toHaveLength(1)
  })

  it('the connect dots stay while you reach for one, and dragging from one links two shapes', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)] }} />)
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)] }} />)
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 100, 100), sticky('c', 1000, 0)], connectors: [] }} />)
    const ids = () => latest().shapes.map(s => s.id)
    act(() => { fireEvent.contextMenu(canvas(), at(50, 50)) })
    expect(screen.getByTestId('board-context-menu')).toBeTruthy()
    expect((screen.getByRole('menuitem', { name: /^Copy/ }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('menuitem', { name: /Bring forward/ }))
    // a overlapped b, so one step forward puts it over b; c is far away and unaffected.
    expect(ids()).toEqual(['b', 'a', 'c'])
    expect(screen.queryByTestId('board-context-menu')).toBeNull()
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.keyDown(canvas(), { key: 'L', code: 'KeyL', metaKey: true, shiftKey: true }) })
    expect(latest().shapes[0].locked).toBe(true)
    expect(parseBoard(serializeBoard(latest())).shapes[0].locked).toBe(true)
    // A press on it goes through: nothing is picked and a drag does not move it.
    press(100, 100)
    moveTo(300, 300)
    release(300, 300)
    expect(latest().shapes[0]).toMatchObject({ x: 0, y: 0 })
    expect(screen.queryByTestId('board-toolbar')).toBeNull()
    act(() => { fireEvent.contextMenu(canvas(), at(100, 100)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /Lock\/Unlock/ }))
    expect(latest().shapes[0].locked).toBeUndefined()
    // Unlock all objects frees every locked shape at once.
    act(() => { fireEvent.keyDown(canvas(), { key: 'L', code: 'KeyL', metaKey: true, shiftKey: true }) })
    act(() => { fireEvent.contextMenu(canvas(), at(600, 600)) })
    fireEvent.click(screen.getByRole('menuitem', { name: /Unlock all objects/ }))
    expect(latest().shapes[0].locked).toBeUndefined()
  })

  it('the menu\'s shortcuts work on the selection: brackets restack, Cmd+C then Cmd+V pastes a copy with its links', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 100, 100), sticky('c', 400, 0)], connectors: [{ id: 'k', from: 'a', to: 'c' }] }} />)
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [section] }} />)
    const label = () => screen.getByText('To do').parentElement as HTMLElement
    expect(label().style.transform).toBe('')
    fireEvent.click(screen.getByLabelText('Zoom out'))
    fireEvent.click(screen.getByLabelText('Zoom out'))
    fireEvent.click(screen.getByLabelText('Zoom out'))
    // 1 / 1.2^3 = 0.579 of the board's size: the label is drawn 1.728 times larger from its bottom-left corner.
    expect(label().style.transform).toBe('scale(1.728)')
    expect(label().style.transformOrigin).toBe('left bottom')
  })

  it('zooming out thins the dots by halves instead of packing them tighter', () => {
    render(<Harness initial={emptyBoard()} />)
    const gap = () => parseFloat(canvas().style.backgroundSize)
    expect(gap()).toBe(24)
    const seen: number[] = []
    for (let i = 0; i < 14; i++) {
      fireEvent.click(screen.getByLabelText('Zoom out'))
      seen.push(gap())
    }
    // Never tighter than 16px, and never looser than twice that.
    for (const g of seen) { expect(g).toBeGreaterThanOrEqual(16); expect(g).toBeLessThan(32.01) }
    // The gap fell, then sprang back at least once when every other dot dropped.
    expect(seen.some((g, i) => i > 0 && g > seen[i - 1])).toBe(true)
  })

  it('connectors are drawn over the shapes they cross, not under them', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0), sticky('c', 200, 0)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    const line = screen.getByTestId('board-connector')
    for (const shape of screen.getAllByTestId('board-shape')) {
      // Later in the document is higher, as shapes and the line share a stacking context.
      expect(shape.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })

  it('a selected connector shows its end handles and a toolbar that restyles it, one undo step each', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    expect(screen.getByTestId('board-connector').dataset.line).toBe('elbow')
    act(() => { fireEvent.pointerDown(screen.getByTestId('board-connector'), at(300, 100)) })
    expect(screen.getByTestId('conn-end-from')).toBeTruthy()
    expect(screen.getByTestId('conn-end-to')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Curved line'))
    fireEvent.click(screen.getByLabelText('Dashed line'))
    fireEvent.click(screen.getByLabelText('Arrows at both ends'))
    fireEvent.click(screen.getByLabelText('Line colour'))
    fireEvent.click(screen.getByLabelText('Line colour green'))
    expect(latest().connectors[0]).toMatchObject({ line: 'curve', dashed: true, arrows: 'both', color: 'green' })
    expect(screen.getByTestId('board-connector').dataset.line).toBe('curve')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().connectors[0]).toMatchObject({ line: 'curve', dashed: true, arrows: 'both' })
    expect(latest().connectors[0].color).toBeUndefined()
    fireEvent.click(screen.getByLabelText('Delete connector'))
    expect(latest().connectors).toEqual([])
  })

  it('a selected elbow has a handle per segment; dragging one reshapes the route as one undo step', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    act(() => { fireEvent.pointerDown(screen.getByTestId('board-connector'), at(300, 100)) })
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 400, 0), sticky('c', 400, 400)], connectors: [{ id: 'c1', from: 'a', to: 'b' }] }} />)
    act(() => { fireEvent.pointerDown(screen.getByTestId('board-connector'), at(300, 100)) })
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
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} />)
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
    fireEvent.click(screen.getByLabelText('Colour mint'))
    expect(latest().shapes[0].color).toBe('mint')
    expect(screen.queryByTestId('swatch-panel')).toBeNull()
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('swatch-panel')).toBeNull()
  })

  it('restacks a step only past a shape it overlaps, and to the front or back in one go', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('far', 2000, 0), sticky('b', 100, 100)] }
    const order = (x: Board) => x.shapes.map(s => s.id)
    expect(order(restack(b, new Set(['a']), 'forward'))).toEqual(['far', 'b', 'a'])
    expect(order(restack(b, new Set(['b']), 'backward'))).toEqual(['b', 'a', 'far'])
    expect(order(restack(b, new Set(['b']), 'back'))).toEqual(['b', 'a', 'far'])
    expect(order(restack(b, new Set(['a', 'far']), 'front'))).toEqual(['b', 'a', 'far'])
    // Already on top: forward changes nothing.
    expect(order(restack(b, new Set(['b']), 'forward'))).toEqual(['a', 'far', 'b'])
  })

  it('the toolbar sets a shape\'s fill mode, outline weight and kind, each one undo step', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyBoard(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Colour'))
    expect(screen.getByRole('radio', { name: 'Fill' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'No fill' }))
    expect(latest().shapes[0].fill).toBe('none')
    const shape = screen.getByTestId('board-shape')
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
    expect(screen.getByTestId('board-shape').style.borderWidth).toBe('6px')
    fireEvent.click(screen.getByRole('radio', { name: 'Dashed' }))
    expect(latest().shapes[0].border).toBe('dashed')
    // Drawn as SVG strokes: each side fitted with whole dashes, the corners solid.
    const outline = screen.getByTestId('dashed-outline')
    expect(outline.querySelectorAll('line')).toHaveLength(4)
    expect(screen.getByTestId('dashed-corners')).toBeTruthy()
    expect(screen.getByTestId('board-shape').style.borderStyle).toBe('solid')
    fireEvent.click(screen.getByLabelText('Border red'))
    expect(latest().shapes[0].borderColor).toBe('red')
    // The fill keeps its own colour.
    expect(latest().shapes[0].color).toBe('blue')
    fireEvent.click(screen.getByLabelText('Border'))
    fireEvent.click(screen.getByRole('radio', { name: /Match fill/ }))
    expect(latest().shapes[0].borderColor).toBeUndefined()
    // The panel stays open after a mode or "Match fill", like FigJam's.
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

  it('an older "clear" colour reads as black with no fill, and a conductor can set fill and weight', () => {
    const old = JSON.stringify({ ...emptyBoard(), shapes: [{ id: 'a', kind: 'rect', x: 0, y: 0, w: 100, h: 80, text: '', color: 'clear' }] })
    expect(parseBoard(old).shapes[0]).toMatchObject({ color: 'black', fill: 'none' })
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0)] }
    const added = applyBoardAdd(b, { shapes: [{ text: 'Box', kind: 'rect', color: 'teal', fill: 'transparent', weight: 'thin', border: 'dashed', borderColor: 'red' }] })
    expect(added.shapes[1]).toMatchObject({ color: 'teal', fill: 'transparent', weight: 'thin', border: 'dashed', borderColor: 'red' })
    const edited = applyBoardEdit(added, { shapes: [{ id: 'a', fill: 'none', weight: 'bogus' as unknown as 'thin' }] })
    expect(edited.shapes[0].fill).toBe('none')
    expect(edited.shapes[0].weight).toBeUndefined()
    // Round trip keeps the look; the default look is not written.
    expect(parseBoard(serializeBoard(edited)).shapes[1]).toMatchObject({ fill: 'transparent', weight: 'thin', border: 'dashed', borderColor: 'red' })
    expect('fill' in parseBoard(serializeBoard(b)).shapes[0]).toBe(false)
  })

  it('a shape\'s outline follows its fill unless it has a colour of its own, and can be dashed or gone', () => {
    const base = { color: 'blue' as const }
    expect(shapeLook(base)).toMatchObject({ fill: '#3FAAFF', border: '#0B78D6', dashed: false, width: 3.5, solid: true })
    expect(shapeLook({ ...base, fill: 'none' })).toMatchObject({ fill: 'transparent', border: '#3FAAFF' })
    expect(shapeLook({ color: 'black', fill: 'none' }).border).toBe('var(--text)')
    expect(shapeLook({ ...base, borderColor: 'red' }).border).toBe('#F2573C')
    expect(shapeLook({ ...base, border: 'dashed', weight: 'thin' })).toMatchObject({ dashed: true, width: 2 })
    expect(shapeLook({ ...base, border: 'none', borderColor: 'red' }).border).toBe('transparent')
  })

  it('dashed outlines are drawn for every kind of shape that has a border, and not for a solid one', () => {
    const shapes: Shape[] = [
      { ...sticky('r', 0, 0), kind: 'rect', border: 'dashed' },
      { ...sticky('e', 300, 0), kind: 'ellipse', border: 'dashed' },
      { ...sticky('s', 600, 0), kind: 'section', fill: 'none', border: 'dashed' },
      { ...sticky('d', 900, 0), kind: 'diamond', border: 'dashed' },
      { ...sticky('x', 1200, 0), kind: 'rect' },
    ]
    render(<Harness initial={{ ...emptyBoard(), shapes }} />)
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
    expect(shapeLook({ color: 'blue', hex: '#FFC83D' })).toMatchObject({ fill: '#FFC83D', border: darker('#FFC83D'), ink: '#1F2328' })
    expect(shapeLook({ color: 'blue', hex: '#1E2A5E' }).ink).toBe('#FFFFFF')
    // With no fill, the custom colour is the outline itself; a border colour of its own wins over both.
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', fill: 'none' }).border).toBe('#FFC83D')
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', borderHex: '#112233' }).border).toBe('#112233')
    expect(shapeLook({ color: 'blue', hex: '#FFC83D', borderColor: 'red' }).border).toBe('#F2573C')
  })

  it('the free colour picker sets a fill colour live, as one undo step, and a swatch swaps it out', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyBoard(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.click(screen.getByLabelText('Colour custom'))
    const hexField = screen.getByLabelText('Hex colour') as HTMLInputElement
    // Typing shows each valid colour at once...
    fireEvent.focus(hexField)
    fireEvent.change(hexField, { target: { value: '#FFC83D' } })
    expect(latest().shapes[0].hex).toBe('#FFC83D')
    expect(screen.getByTestId('board-shape').style.background.toLowerCase()).toBe('#ffc83d')
    fireEvent.change(hexField, { target: { value: '#12AB' } })
    expect(latest().shapes[0].hex).toBe('#FFC83D')
    fireEvent.change(hexField, { target: { value: '1e2a5e' } })
    expect(latest().shapes[0].hex).toBe('#1E2A5E')
    // ...and leaving the field records the whole edit as one undo step.
    fireEvent.blur(hexField)
    expect(parseBoard(serializeBoard(latest())).shapes[0].hex).toBe('#1E2A5E')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true }) })
    expect(latest().shapes[0].hex).toBeUndefined()
    expect(latest().shapes[0].color).toBe('blue')
    act(() => { fireEvent.keyDown(canvas(), { key: 'z', metaKey: true, shiftKey: true }) })
    expect(latest().shapes[0].hex).toBe('#1E2A5E')
    // A swatch swaps the custom colour out (the panel is still open).
    fireEvent.click(screen.getByLabelText('Colour mint'))
    expect(latest().shapes[0]).toMatchObject({ color: 'mint' })
    expect(latest().shapes[0].hex).toBeUndefined()
  })

  it('the square and the hue bar move the colour; the border has a free colour of its own', () => {
    const rect: Shape = { ...sticky('r', 0, 0), kind: 'rect', color: 'blue', hex: '#FF0000', w: 240, h: 120 }
    render(<Harness initial={{ ...emptyBoard(), shapes: [rect] }} />)
    press(100, 60)
    release(100, 60)
    fireEvent.click(screen.getByLabelText('Border'))
    fireEvent.click(screen.getByLabelText('Border custom'))
    fireEvent.change(screen.getByLabelText('Hex colour'), { target: { value: '#00AA55' } })
    fireEvent.blur(screen.getByLabelText('Hex colour'))
    expect(latest().shapes[0]).toMatchObject({ hex: '#FF0000', borderHex: '#00AA55' })
    // Picking "Match fill" gives the outline back to the fill's darker shade.
    fireEvent.click(screen.getByRole('radio', { name: /Match fill/ }))
    expect(latest().shapes[0].borderHex).toBeUndefined()
    expect(screen.getByTestId('custom-hue').getAttribute('aria-valuenow')).toBeTruthy()
    expect(screen.getByTestId('custom-square')).toBeTruthy()
  })

  it('a conductor can name a hex colour, and a swatch it names swaps a custom one out', () => {
    const b = { ...emptyBoard(), shapes: [sticky('a', 0, 0)] }
    const added = applyBoardAdd(b, { shapes: [{ text: 'Box', kind: 'rect', hex: '#abc', borderHex: 'nope' }] })
    expect(added.shapes[1]).toMatchObject({ hex: '#AABBCC' })
    expect(added.shapes[1].borderHex).toBeUndefined()
    const same = applyBoardEdit(added, { shapes: [{ id: added.shapes[1].id, text: 'Renamed' }] })
    expect(same.shapes[1].hex).toBe('#AABBCC')
    const swapped = applyBoardEdit(added, { shapes: [{ id: added.shapes[1].id, color: 'red' }] })
    expect(swapped.shapes[1]).toMatchObject({ color: 'red' })
    expect(swapped.shapes[1].hex).toBeUndefined()
    expect(parseBoard(serializeBoard(added)).shapes[1].hex).toBe('#AABBCC')
  })

  it('sections get no connect dots and are not joined by lines; the dots go once zoomed far out', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 100, w: 500, h: 300, text: 'To do', color: 'smoke' }
    render(<Harness initial={{ ...emptyBoard(), shapes: [section, sticky('a', 600, 100)] }} />)
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
    fireEvent.click(screen.getByLabelText('Zoom out'))
    fireEvent.click(screen.getByLabelText('Zoom out'))
    fireEvent.click(screen.getByLabelText('Zoom out'))
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    fireEvent.click(screen.getByLabelText('Zoom out'))
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
    fireEvent.click(screen.getByLabelText('Zoom in'))
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
  })

  it('a selected shape keeps its four connect dots, and they go with the selection', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} />)
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.pointerMove(canvas(), at(900, 900)) })
    expect(screen.getAllByTestId('connect-dot')).toHaveLength(4)
    act(() => { fireEvent.keyDown(canvas(), { key: 'Escape' }) })
    expect(screen.queryAllByTestId('connect-dot')).toHaveLength(0)
  })

  it('reads colours from a file or a conductor, and names an older palette\'s colours as their light kin', () => {
    expect(toSwatch('violet')).toBe('violet')
    expect(toSwatch('purple')).toBe('lavender')
    expect(toSwatch('pink')).toBe('blush')
    expect(toSwatch('gray')).toBe('smoke')
    expect(toSwatch('neon')).toBeUndefined()
    expect(toSwatch(3)).toBeUndefined()
    const old = JSON.stringify({ ...emptyBoard(), shapes: [{ id: 'a', kind: 'rect', x: 0, y: 0, w: 100, h: 80, text: '', color: 'purple' }] })
    expect(parseBoard(old).shapes[0].color).toBe('lavender')
  })

  it('marquee selects what it touches; the toolbar recolours the selection', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0), sticky('b', 300, 0), sticky('c', 0, 600)] }} />)
    press(-50, -50)
    moveTo(550, 250)
    release(550, 250)
    fireEvent.click(screen.getByLabelText('Colour'))
    fireEvent.click(screen.getByLabelText('Colour blue'))
    expect(latest().shapes.map(s => s.color)).toEqual(['blue', 'blue', 'yellow'])
  })

  it('C asks about the selection at once, without typing a "c"; with nothing selected it picks the Ask tool', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Pricing')] }} />)
    act(() => { fireEvent.keyDown(canvas(), { key: 'c' }) })
    // Nothing selected: the Ask tool is picked and the next click says where.
    expect(screen.queryByTestId('board-ask-input')).toBeNull()
    expect(screen.getByTestId('tool-ask').getAttribute('aria-pressed')).toBe('true')
    act(() => { fireEvent.keyDown(canvas(), { key: 'Escape' }) })
    press(100, 100)
    release(100, 100)
    let typed = true
    act(() => { typed = fireEvent.keyDown(canvas(), { key: 'c' }) })
    expect(typed).toBe(false)
    const input = screen.getByTestId('board-ask-input') as HTMLInputElement
    expect(input.value).toBe('')
    expect(input.placeholder).toBe('Ask the conductor about "Pricing"')
  })

  it('a section gets a goal from its toolbar, shows a chip on its label, and the goal clears again', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 600, h: 400, text: 'To do', color: 'smoke' }
    render(<Harness initial={{ ...emptyBoard(), shapes: [section, sticky('a', 40, 40, 'Fix login')] }} />)
    // A sticky has no goal button.
    press(100, 100)
    release(100, 100)
    expect(screen.queryByTestId('section-goal-button')).toBeNull()
    // A section is picked by its label.
    press(50, -10)
    release(50, -10)
    expect(screen.queryByTestId('section-goal-chip')).toBeNull()
    fireEvent.click(screen.getByTestId('section-goal-button'))
    expect((screen.getByTestId('section-goal-save') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByTestId('section-goal-text'), { target: { value: '  Keep checking these tasks  ' } })
    fireEvent.click(screen.getByRole('radio', { name: '30 min' }))
    fireEvent.click(screen.getByTestId('section-goal-save'))
    expect(latest().shapes.find(s => s.id === 'sec')?.goal).toEqual({ text: 'Keep checking these tasks', every: 30 })
    expect(latest().shapes.find(s => s.id === 'a')?.goal).toBeUndefined()
    expect(screen.getByTestId('section-goal-chip').textContent).toContain('Watching')
    // A watched section has a ring round its border, and it goes with the goal.
    expect(screen.getAllByTestId('watch-ring')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('section-goal-button'))
    fireEvent.click(screen.getByTestId('section-goal-clear'))
    expect(latest().shapes.find(s => s.id === 'sec')?.goal).toBeUndefined()
    expect(screen.queryByTestId('section-goal-chip')).toBeNull()
    expect(screen.queryByTestId('watch-ring')).toBeNull()
  })

  it('the toolbar protects a section from the conductor, a chip says so, and the board switch flips the default', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 600, h: 400, text: 'To do', color: 'smoke' }
    render(<Harness initial={{ ...emptyBoard(), shapes: [section] }} />)
    press(50, -10)
    release(50, -10)
    const button = () => screen.getByTestId('section-access-button')
    expect(button().getAttribute('aria-pressed')).toBe('false')
    expect(screen.queryByTestId('section-access-chip')).toBeNull()
    fireEvent.click(button())
    expect(latest().shapes[0].conductor).toBe('deny')
    expect(screen.getByTestId('section-access-chip').getAttribute('data-mark')).toBe('protected')
    // Opening it again goes back to following the board, and stores nothing.
    fireEvent.click(button())
    expect(latest().shapes[0].conductor).toBeUndefined()
    expect(screen.queryByTestId('section-access-chip')).toBeNull()
    // Closing the board makes an unmarked section protected; opening it is then marked.
    fireEvent.click(screen.getByTestId('board-conductor-mode'))
    expect(latest().conductorDefault).toBe('closed')
    expect(button().getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(button())
    expect(latest().shapes[0].conductor).toBe('allow')
    expect(screen.getByTestId('section-access-chip').getAttribute('data-mark')).toBe('open')
    fireEvent.click(screen.getByTestId('board-conductor-mode'))
    expect(latest().conductorDefault).toBeUndefined()
  })

  it('the conductor cannot change a protected section or what is in it, and is told so', () => {
    const sec = (id: string, x: number, conductor?: 'allow' | 'deny'): Shape => ({ id, kind: 'section', x, y: 0, w: 600, h: 400, text: id, color: 'smoke', ...(conductor ? { conductor } : {}) })
    const board: Board = {
      ...emptyBoard(),
      shapes: [sec('A', 0, 'deny'), sec('B', 800), sticky('a', 40, 40, 'Keep'), sticky('b', 840, 40, 'Change'), sticky('l', 0, 600, 'Loose')],
      connectors: [{ id: 'k', from: 'a', to: 'b' }],
    }
    const edit = { shapes: [{ id: 'a', text: 'Hacked' }, { id: 'b', text: 'Changed' }, { id: 'b2', text: 'Unknown' }, { id: 'l', in: 'A' }], delete: ['a', 'l'], arrange: ['A', 'B'], deleteConnectors: ['k'] }
    const { board: next, refused } = applyReplyChecked(board, { edits: [edit], adds: [] })
    expect(next.shapes.find(s => s.id === 'a')?.text).toBe('Keep')
    expect(next.shapes.find(s => s.id === 'b')?.text).toBe('Changed')
    expect(next.shapes.find(s => s.id === 'l')).toBeUndefined()
    expect(next.connectors).toHaveLength(1)
    expect(refused).toEqual(expect.arrayContaining(['a', 'l', 'A', 'a -> b']))
    // Additions: into a protected section no, into another yes; a new section and its cards are the conductor's own.
    const added = applyReplyChecked(board, {
      edits: [],
      adds: [{
        shapes: [
          { text: 'Into A', in: 'A' }, { text: 'Into B', in: 'B' },
          { ref: 'n', kind: 'section', text: 'New' }, { text: 'In new', in: 'n' },
          { text: 'Dropped at A', x: 100, y: 100 },
        ],
        connectors: [{ from: 'a', to: 'n' }],
      }],
    })
    const texts = added.board.shapes.map(s => s.text)
    expect(texts).toEqual(expect.arrayContaining(['Into B', 'New', 'In new']))
    expect(texts).not.toContain('Into A')
    expect(texts).not.toContain('Dropped at A')
    expect(added.board.connectors).toHaveLength(1)
    expect(added.refused).toEqual(expect.arrayContaining(['Into A', 'Dropped at A', 'a -> n']))
    // The conductor is told what is off limits, and the summary marks it.
    expect(summarizeBoard(board)).toMatch(/- a \| sticky[^\n]*\| protected/)
    expect(summarizeBoard(board)).not.toMatch(/- b \| sticky[^\n]*\| protected/)
    expect(buildBoardTurn({ id: 'q', request: 'x', board, selected: [], followUp: false, docId: 'd' })).toContain('Protected, off limits to you: A "A"')
  })

  it('on a closed board only opened sections can be changed, and the nearest setting decides', () => {
    const sec = (id: string, x: number, y: number, w: number, conductor?: 'allow' | 'deny'): Shape => ({ id, kind: 'section', x, y, w, h: w, text: id, color: 'smoke', ...(conductor ? { conductor } : {}) })
    const closed: Board = {
      ...emptyBoard(), conductorDefault: 'closed',
      shapes: [sec('open', 0, 0, 400, 'allow'), sec('shut', 800, 0, 400), sticky('o', 40, 40, 'In open'), sticky('s', 840, 40, 'In shut'), sticky('l', 0, 900, 'Loose')],
    }
    const { board: next, refused } = applyReplyChecked(closed, { edits: [{ shapes: [{ id: 'o', text: 'ok' }, { id: 's', text: 'no' }, { id: 'l', text: 'no' }] }], adds: [{ shapes: [{ text: 'New loose' }, { text: 'New in open', in: 'open' }] }] })
    expect(next.shapes.find(s => s.id === 'o')?.text).toBe('ok')
    expect(next.shapes.find(s => s.id === 's')?.text).toBe('In shut')
    expect(next.shapes.find(s => s.id === 'l')?.text).toBe('Loose')
    expect(next.shapes.map(s => s.text)).toEqual(expect.arrayContaining(['New in open']))
    expect(next.shapes.map(s => s.text)).not.toContain('New loose')
    expect(refused).toEqual(expect.arrayContaining(['s', 'l', 'New loose']))
    expect(buildBoardTurn({ id: 'q', request: 'x', board: closed, selected: [], followUp: false, docId: 'd' })).toContain('ONLY inside these sections, and what they hold: open "open"')
    // A section inside a protected one follows it, unless it says otherwise.
    const nested: Board = {
      ...emptyBoard(),
      shapes: [sec('outer', 0, 0, 800, 'deny'), sec('inner', 100, 100, 300, 'allow'), sticky('x', 120, 120, 'inner card'), sticky('y', 500, 500, 'outer card')],
    }
    expect(conductorAccessOf(nested, nested.shapes[2])).toBe('allow')
    expect(conductorAccessOf(nested, nested.shapes[3])).toBe('deny')
  })

  it('a section\'s protection and the board\'s default survive the file', () => {
    const raw = JSON.stringify({
      conductorDefault: 'closed',
      shapes: [
        { id: 'a', kind: 'section', x: 0, y: 0, w: 300, h: 300, text: 'A', color: 'smoke', conductor: 'deny' },
        { id: 'b', kind: 'section', x: 0, y: 0, w: 300, h: 300, text: 'B', color: 'smoke', conductor: 'maybe' },
        { id: 'c', kind: 'sticky', x: 0, y: 0, w: 200, h: 200, text: '', color: 'yellow', conductor: 'deny' },
      ],
    })
    const b = parseBoard(raw)
    expect(b.conductorDefault).toBe('closed')
    expect(b.shapes.map(s => s.conductor)).toEqual(['deny', undefined, undefined])
    expect(parseBoard(serializeBoard(b)).conductorDefault).toBe('closed')
    expect(parseBoard('{"shapes":[],"conductorDefault":"x"}').conductorDefault).toBeUndefined()
  })

  it('a goal survives the file, only on a section, and an empty one is no goal', () => {
    const raw = JSON.stringify({
      shapes: [
        { id: 's', kind: 'section', x: 0, y: 0, w: 300, h: 300, text: 'A', color: 'smoke', goal: { text: 'Watch', every: 5, session: 'w1' } },
        { id: 'e', kind: 'section', x: 400, y: 0, w: 300, h: 300, text: 'B', color: 'smoke', goal: { text: '  ', every: 5 } },
        { id: 'k', kind: 'sticky', x: 0, y: 0, w: 200, h: 200, text: '', color: 'yellow', goal: { text: 'Nope', every: 5 } },
        { id: 'o', kind: 'section', x: 800, y: 0, w: 300, h: 300, text: 'C', color: 'smoke', goal: { text: 'Odd', every: 7 } },
      ],
    })
    const b = parseBoard(raw)
    expect(b.shapes.map(s => s.goal)).toEqual([{ text: 'Watch', every: 5, session: 'w1' }, undefined, undefined, { text: 'Odd', every: 10 }])
    expect(parseBoard(serializeBoard(b)).shapes[0].goal).toEqual({ text: 'Watch', every: 5, session: 'w1' })
  })

  it('the toolbar\'s Ask opens the input beside the selection, and sending pins the conversation there', () => {
    const onAsk = vi.fn()
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0, 'Pricing')] }} onAsk={onAsk} />)
    press(100, 100)
    release(100, 100)
    fireEvent.click(screen.getByTestId('board-ask-button'))
    const box = screen.getByTestId('board-ask-input')
    expect((screen.getByTestId('board-ask-send') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(box, { target: { value: 'brainstorm tiers' } })
    expect((screen.getByTestId('board-ask-send') as HTMLButtonElement).disabled).toBe(false)
    act(() => { fireEvent.keyDown(box, { key: 'Enter' }) })
    // Its pin sits at the sticky's top-right corner, kept as an offset so it follows the sticky.
    expect(latest().asks).toEqual([{ id: expect.any(String), shapeIds: ['a'], at: { x: 200, y: 0 } }])
    expect(onAsk).toHaveBeenCalledWith(latest().asks[0].id, 'brainstorm tiers', latest())
    // It opens straight away, waiting on the conductor.
    expect(screen.getByTestId('conductor-card')).toBeTruthy()
    expect(screen.getByTestId('thread-waiting')).toBeTruthy()
    expect(screen.queryByTestId('board-ask-input')).toBeNull()
  })

  it('with the Ask tool, a click asks about what is there: a shape, else the section holding the point, else the board', () => {
    const onAsk = vi.fn()
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 600, h: 400, text: 'To do', color: 'smoke' }
    render(<Harness initial={{ ...emptyBoard(), shapes: [section, sticky('a', 40, 40, 'Fix login')] }} onAsk={onAsk} />)
    const askAt = (x: number, y: number, text: string) => {
      fireEvent.click(screen.getByTestId('tool-ask'))
      press(x, y)
      const input = screen.getByTestId('board-ask-input') as HTMLInputElement
      const hint = input.placeholder
      fireEvent.change(input, { target: { value: text } })
      act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
      return hint
    }
    expect(askAt(100, 100, 'on the card')).toBe('Ask the conductor about "Fix login"')
    expect(askAt(400, 300, 'in the section')).toBe('Ask the conductor about "To do"')
    expect(askAt(900, 600, 'on the board')).toBe('Ask the conductor about the whole board')
    expect(latest().asks.map(a => ({ shapeIds: a.shapeIds, at: a.at }))).toEqual([
      { shapeIds: ['a'], at: { x: 60, y: 60 } },
      // Kept relative to the section, so the pin moves with it.
      { shapeIds: ['sec'], at: { x: 400, y: 300 } },
      { shapeIds: [], at: { x: 900, y: 600 } },
    ])
    // The tool goes back to Select after each pin.
    expect(screen.getByTestId('tool-select').getAttribute('aria-pressed')).toBe('true')
    // A conversation about the whole board outlives deleting a shape; one about that shape does not.
    press(100, 100)
    release(100, 100)
    act(() => { fireEvent.keyDown(canvas(), { key: 'Delete' }) })
    expect(latest().asks.map(a => a.shapeIds)).toEqual([['sec'], []])
  })

  it('Esc or a press elsewhere puts away an unsent question', () => {
    const onAsk = vi.fn()
    render(<Harness initial={emptyBoard()} onAsk={onAsk} />)
    fireEvent.click(screen.getByTestId('tool-ask'))
    press(300, 300)
    act(() => { fireEvent.keyDown(screen.getByTestId('board-ask-input'), { key: 'Escape' }) })
    expect(screen.queryByTestId('board-ask-input')).toBeNull()
    fireEvent.click(screen.getByTestId('tool-ask'))
    press(300, 300)
    press(600, 600)
    expect(screen.queryByTestId('board-ask-input')).toBeNull()
    expect(onAsk).not.toHaveBeenCalled()
    expect(latest().asks).toEqual([])
  })

  it('an overlay item pinned to a shape moves with it; one with no shape waits in the tray', () => {
    // Built directly: sanitizing is pinned under jsdom in docOverlay.test.ts, and
    // DOMPurify does not run faithfully under this file's happy-dom.
    const overlay: OverlayItem[] = [
      { key: '0', anchor: 'shape:a', place: 'top', html: '<div class="ov-pill">2 of 3</div>' },
      { key: '1', anchor: null, place: 'margin', html: '<div>Board-wide</div>' },
    ]
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} overlay={overlay} />)
    const item = () => screen.getAllByTestId('board-overlay-item')[0]
    const before = item().style.left
    press(100, 100)
    moveTo(300, 100)
    release(300, 100)
    expect(item().style.left).not.toBe(before)
    expect(screen.getByTestId('board-overlay-tray').textContent).toContain('Board-wide')
  })

  it('an overlay item above or on the corner of a section starts above its label, not on it', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 100, w: 300, h: 200, text: 'To do', color: 'smoke' }
    const overlay: OverlayItem[] = [
      { key: '0', anchor: 'shape:sec', place: 'top', html: '<div class="ov-pill">3 of 7</div>' },
      { key: '1', anchor: 'shape:sec', place: 'corner', html: '<span class="ov-status">Doing</span>' },
      { key: '2', anchor: 'shape:sec', place: 'right', html: '<div>Side note</div>' },
    ]
    render(<Harness initial={{ ...emptyBoard(), shapes: [section, sticky('a', 400, 100)] }} overlay={overlay} />)
    const [top, corner, right] = screen.getAllByTestId('board-overlay-item')
    // The view starts at (120, 100), so the section's body begins at y = 200 on screen and its label strip at 170.
    expect(top.style.top).toBe('160px')
    expect(corner.style.top).toBe('164px')
    // Items beside it are placed against the body as before.
    expect(right.style.top).toBe('200px')
  })

  it('the overlay can be hidden and shown, the choice is remembered, and an overlay never catches a drag', () => {
    localStorage.removeItem('kirocrew.board.overlays')
    const overlay: OverlayItem[] = [
      { key: '0', anchor: 'shape:a', place: 'top', html: '<div class="ov-pill">2 of 3</div>' },
      { key: '1', anchor: null, place: 'margin', html: '<div>Board-wide</div>' },
    ]
    const { unmount } = render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} overlay={overlay} />)
    expect(screen.getAllByTestId('board-overlay-item')).toHaveLength(2)
    // Pointer events pass through the overlay to the board; only its links and buttons catch them.
    expect(screen.getAllByTestId('board-overlay-item')[0].className).toContain('pointer-events-none')
    expect(screen.getByTestId('board-overlay-tray').className).toContain('pointer-events-none')
    fireEvent.click(screen.getByLabelText('Hide overlays'))
    expect(screen.queryAllByTestId('board-overlay-item')).toHaveLength(0)
    expect(screen.queryByTestId('board-overlay-tray')).toBeNull()
    // The toggle says how many are hidden, and the shapes under them are still there to work on.
    expect(screen.getByTestId('board-overlay-toggle').textContent).toBe('2')
    expect(screen.getByTestId('board-overlay-toggle').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getAllByTestId('board-shape')).toHaveLength(1)
    // Remembered across a reload.
    unmount()
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} overlay={overlay} />)
    expect(screen.queryAllByTestId('board-overlay-item')).toHaveLength(0)
    fireEvent.click(screen.getByLabelText('Show overlays'))
    expect(screen.getAllByTestId('board-overlay-item')).toHaveLength(2)
    expect(localStorage.getItem('kirocrew.board.overlays')).toBe('on')
  })

  it('a corner badge sits on the shape\'s top-right corner and follows it', () => {
    const overlay: OverlayItem[] = [{ key: '0', anchor: 'shape:a', place: 'corner', html: '<span class="ov-status ov-running">Researching</span>' }]
    render(<Harness initial={{ ...emptyBoard(), shapes: [sticky('a', 0, 0)] }} overlay={overlay} />)
    const badge = () => screen.getByTestId('board-overlay-item')
    // The view starts at (120, 100); the corner is x + w - 6 across, y - 6 down.
    expect(badge().style.left).toBe('314px')
    expect(badge().style.top).toBe('94px')
    press(100, 100)
    moveTo(148, 100)
    release(148, 100)
    expect(badge().style.left).toBe('362px')
  })

  it('a conversation is a pin: hover shows the latest line, click opens the card, which replies and deletes', () => {
    const board: Board = { ...emptyBoard(), shapes: [sticky('a', 0, 0)], asks: [{ id: 'q', shapeIds: ['a'], at: { x: 200, y: 0 } }] }
    const threads = { q: { messages: [{ who: 'you' as const, text: 'Plan it' }, { who: 'conductor' as const, text: '**Here** is the plan.' }], pending: false, unread: true,
      workers: [{ key: 'w1', title: 'Research A', state: 'working' as const }] } }
    const onFollowUp = vi.fn()
    const onReadAsk = vi.fn()
    const onOpenSession = vi.fn()
    render(<Harness initial={board} threads={threads} onFollowUp={onFollowUp} onReadAsk={onReadAsk} onOpenSession={onOpenSession} />)
    const thread = screen.getByTestId('conductor-thread')
    expect(thread.dataset.state).toBe('unread')
    expect(screen.getByTestId('thread-unread')).toBeTruthy()
    // The pin's point is the sticky's top-right corner: view (120, 100) + (200, 0), lifted by its height so its tail marks the spot.
    expect(screen.getByTestId('board-thread').style.left).toBe('320px')
    expect(screen.getByTestId('board-thread').style.top).toBe('72px')
    act(() => { fireEvent.mouseEnter(screen.getByTestId('conductor-pin')) })
    expect(screen.getByTestId('conductor-preview').textContent).toContain('Here is the plan.')
    expect(screen.getByTestId('conductor-preview').textContent).toContain('1 reply')
    expect(screen.getByTestId('conductor-preview').textContent).toContain('1/1 working')
    act(() => { fireEvent.click(screen.getByTestId('conductor-pin')) })
    expect(onReadAsk).toHaveBeenCalledWith('q')
    // What it is about is outlined while it is open.
    expect(screen.getAllByTestId('ask-target')).toHaveLength(1)
    const card = screen.getByTestId('conductor-card')
    // Its header says what it is about.
    expect(card.textContent).toContain('this sticky')
    expect(screen.getAllByTestId('thread-msg').map(m => m.dataset.who)).toEqual(['you', 'conductor'])
    expect(screen.getAllByTestId('thread-msg')[1].querySelector('strong')?.textContent).toBe('Here')
    fireEvent.click(screen.getByTestId('thread-worker'))
    expect(onOpenSession).toHaveBeenCalledWith('w1')
    const reply = screen.getByTestId('thread-reply')
    fireEvent.change(reply, { target: { value: 'more detail' } })
    act(() => { fireEvent.keyDown(reply, { key: 'Enter' }) })
    expect(onFollowUp).toHaveBeenCalledWith('q', 'more detail')
    expect((reply as HTMLTextAreaElement).value).toBe('')
    // Esc closes it; the menu deletes the conversation.
    act(() => { fireEvent.keyDown(reply, { key: 'Escape' }) })
    expect(screen.queryByTestId('conductor-card')).toBeNull()
    expect(screen.queryAllByTestId('ask-target')).toHaveLength(0)
    act(() => { fireEvent.click(screen.getByTestId('conductor-pin')) })
    fireEvent.click(screen.getByLabelText('More'))
    fireEvent.click(screen.getByTestId('conductor-delete'))
    expect(latest().asks).toEqual([])
    expect(screen.queryByTestId('conductor-thread')).toBeNull()
  })

  it('the turn tells the conductor what was pointed at: shapes, a section and what it holds, or the board', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 600, h: 400, text: 'To do', color: 'smoke' }
    const b: Board = { ...emptyBoard(), shapes: [section, sticky('a', 40, 40, 'Fix login')] }
    const turn = (selected: string[]) => buildBoardTurn({ id: 'q', request: 'tidy this', board: b, selected, followUp: false, docId: 'b0a4d1' })
    expect(turn(['a'])).toContain('They pointed at a and wrote the request above.')
    expect(turn(['sec'])).toContain('They pointed at the section sec "To do" as a whole (it holds a)')
    expect(turn([])).toContain('They pointed at the whole board (no shape or section in particular)')
    expect(turn([])).toContain('pinned where they pointed')
  })

  it('what a conversation is about is tinted shape by shape, never boxed together with what it is not about', () => {
    const shapes = [sticky('a', 0, 0), sticky('b', 300, 0), sticky('c', 0, 300), sticky('other', 150, 150)]
    const board: Board = { ...emptyBoard(), shapes, asks: [{ id: 'q', shapeIds: ['a', 'b', 'c'], at: { x: 500, y: 0 } }] }
    render(<Harness initial={board} threads={{ q: { messages: [], pending: true, unread: false } }} />)
    expect(screen.queryAllByTestId('ask-target')).toHaveLength(0)
    act(() => { fireEvent.click(screen.getByTestId('conductor-pin')) })
    const marks = screen.getAllByTestId('ask-target')
    expect(marks).toHaveLength(3)
    // Each wash sits exactly on its shape (the view starts at (120, 100)), with no border.
    expect(marks[0].style.left).toBe('120px')
    expect(marks[0].style.top).toBe('100px')
    expect(marks[0].style.border).toBe('')
  })

  it('a run of messages from one side shares one header', () => {
    const board: Board = { ...emptyBoard(), shapes: [sticky('a', 0, 0, 'I need to create a kanban board of the open issues, by kind of task')], asks: [{ id: 'q', shapeIds: ['a'], at: { x: 200, y: 0 } }] }
    const threads = { q: { messages: [{ who: 'you' as const, text: 'Plan it' }, { who: 'conductor' as const, text: 'One.' }, { who: 'conductor' as const, text: 'Two.' }, { who: 'you' as const, text: 'Thanks' }], pending: false, unread: false } }
    render(<Harness initial={board} threads={threads} />)
    act(() => { fireEvent.click(screen.getByTestId('conductor-pin')) })
    expect(screen.getByTestId('conductor-card').textContent?.match(/Conductor/g)).toHaveLength(2) // the card title and one header
    expect(screen.getAllByTestId('thread-msg')).toHaveLength(4)
    // A long subject is cut at a word.
    expect(screen.getByTestId('thread-about').textContent).toBe('About "I need to create a kanban board of the…"')
  })

  it('the preview line drops markdown marks cleanly', () => {
    expect(firstLine('**Here** is `the` _plan_.')).toBe('Here is the plan.')
    expect(firstLine('\n\n# Title\n- a')).toBe('Title')
  })

  it('a pin follows its shape or section, and only one conversation is open at a time', () => {
    const section: Shape = { id: 'sec', kind: 'section', x: 0, y: 0, w: 400, h: 300, text: 'To do', color: 'smoke' }
    const board: Board = { ...emptyBoard(), shapes: [section, sticky('a', 600, 0)],
      asks: [{ id: 'q1', shapeIds: ['sec'], at: { x: 200, y: 150 } }, { id: 'q2', shapeIds: ['a'], at: { x: 200, y: 0 } }] }
    const threads = { q1: { messages: [], pending: true, unread: false }, q2: { messages: [], pending: false, unread: false } }
    render(<Harness initial={board} threads={threads} />)
    const pin = (id: string) => document.querySelector(`[data-ask="${id}"]`) as HTMLElement
    expect(pin('q1').style.left).toBe('320px')
    expect(screen.getAllByTestId('conductor-thread')[0].dataset.state).toBe('pending')
    // Dragging the section by its label carries its pin.
    press(50, -15)
    moveTo(150, -15)
    release(150, -15)
    expect(pin('q1').style.left).toBe('416px')
    act(() => { fireEvent.click(pin('q1').querySelector('[data-testid="conductor-pin"]')!) })
    act(() => { fireEvent.click(pin('q2').querySelector('[data-testid="conductor-pin"]')!) })
    expect(screen.getAllByTestId('conductor-card')).toHaveLength(1)
    expect(pin('q2').querySelector('[data-testid="conductor-card"]')).toBeTruthy()
  })

})
