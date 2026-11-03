/**
 * Session cards on the board: a board element that stands for another session.
 * The model keeps and checks its session key, the conductor can add one, and on
 * the canvas the Link session tool places one from a picker; a double-click, Enter
 * or Open in its toolbar opens the session.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import BoardCanvas from '../pages/chat/page/board/BoardCanvas'
import { applyBoardAdd, emptyBoard, fitHeight, parseBoard, serializeBoard, summarizeBoard, type Board, type Shape } from '../pages/chat/page/board/boardModel'
import type { SessionRef } from '../pages/chat/page/SessionMention'

const SESSIONS: SessionRef[] = [
  { key: 'chat-1', title: 'Release planning', kind: 'chat', running: false, preview: 'We ship on Friday after the last review.' },
  { key: 'board-me', title: 'This board', kind: 'board', running: false },
  { key: 'doc-2', title: 'Pricing notes', kind: 'doc', running: true },
]
const card = (id: string, session: string, x = 0, y = 0): Shape => ({ id, kind: 'session', session, x, y, w: 288, h: 120, text: 'Old title', color: 'white' })

describe('session cards in the model', () => {
  it('keeps a card with its session, drops one without, and tells the conductor what it opens', () => {
    const raw = JSON.stringify({ shapes: [card('c1', 'chat-1'), { ...card('c2', 'x'), session: undefined }, { ...card('c3', 'x'), session: 'bad key!' }], connectors: [] })
    const b = parseBoard(raw)
    expect(b.shapes.map(s => s.id)).toEqual(['c1'])
    expect(b.shapes[0].session).toBe('chat-1')
    expect(parseBoard(serializeBoard(b)).shapes[0].session).toBe('chat-1')
    expect(summarizeBoard(b)).toContain('opens session chat-1')
    // Laid out in a section, a card keeps its own height rather than its text's.
    expect(fitHeight('session', 'a very long title '.repeat(20), 200)).toBe(120)
  })

  it('the conductor can add a card for a session; without a valid key it becomes a sticky', () => {
    const b = applyBoardAdd(emptyBoard(), { shapes: [{ kind: 'session', session: 'doc-2', text: 'Pricing notes' }, { kind: 'session', text: 'No key' }] })
    expect(b.shapes.map(s => [s.kind, s.session])).toEqual([['session', 'doc-2'], ['sticky', undefined]])
  })
})

describe('session cards on the canvas', () => {
  function Harness({ initial, onOpen }: { initial: Board; onOpen: (key: string) => void }) {
    const [board, setBoard] = useState(initial)
    ;(Harness as unknown as { last: Board }).last = board
    return (
      <div style={{ width: 1200, height: 800 }}>
        <BoardCanvas board={board} onChange={setBoard} overlay={[]} threads={{}} onAsk={vi.fn()} onFollowUp={vi.fn()} onOpenSession={onOpen}
          onReadAsk={vi.fn()} sessions={SESSIONS} selfKey="board-me" />
      </div>
    )
  }
  const latest = () => (Harness as unknown as { last: Board }).last
  const at = (x: number, y: number) => ({ clientX: x + 120, clientY: y + 100, button: 0, pointerId: 1 })

  it('the Link session tool opens a picker where you click, and the chosen session lands there as a card', () => {
    render(<Harness initial={emptyBoard()} onOpen={vi.fn()} />)
    fireEvent.click(screen.getByTestId('tool-session'))
    act(() => { fireEvent.pointerDown(screen.getByTestId('board-canvas'), at(400, 300)) })
    const search = screen.getByTestId('session-picker-search')
    // The board's own session is never offered.
    expect(screen.getAllByTestId('mention-item').map(i => i.getAttribute('data-key'))).toEqual(['chat-1', 'doc-2'])
    fireEvent.change(search, { target: { value: 'pri' } })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(screen.queryByTestId('session-picker')).toBeNull()
    const [made] = latest().shapes
    expect(made).toMatchObject({ kind: 'session', session: 'doc-2', text: 'Pricing notes' })
    // Centred on the click, on the grid.
    expect(made.x + made.w / 2).toBeCloseTo(400, -2)
    expect(made.y + made.h / 2).toBeCloseTo(300, -2)
  })

  it('a card draws the live session: title, kind, working, latest message; a gone one says so', () => {
    render(<Harness initial={{ ...emptyBoard(), shapes: [card('c1', 'chat-1'), card('c2', 'doc-2', 400), card('c3', 'gone', 800)] }} onOpen={vi.fn()} />)
    const titles = screen.getAllByTestId('session-card-title').map(t => t.textContent)
    expect(titles).toEqual(['Release planning', 'Pricing notes', 'Old title'])
    const metas = screen.getAllByTestId('session-card-meta').map(m => m.textContent)
    expect(metas[0]).toBe('Chat')
    expect(metas[1]).toContain('Working')
    expect(metas[2]).toBe('This session no longer exists')
    expect(screen.getByTestId('session-card-preview').textContent).toBe('We ship on Friday after the last review.')
  })

  it('a double-click, Enter, or Open in the toolbar opens the card\'s session, never a text editor', () => {
    const open = vi.fn()
    render(<Harness initial={{ ...emptyBoard(), shapes: [card('c1', 'chat-1')] }} onOpen={open} />)
    const canvas = screen.getByTestId('board-canvas')
    act(() => { fireEvent.doubleClick(canvas, at(100, 60)) })
    expect(open).toHaveBeenLastCalledWith('chat-1')
    expect(screen.queryByTestId('board-shape-editor')).toBeNull()
    act(() => { fireEvent.pointerDown(canvas, at(100, 60)) })
    act(() => { fireEvent.pointerUp(window, at(100, 60)) })
    act(() => { fireEvent.keyDown(canvas, { key: 'Enter' }) })
    expect(open).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByTestId('session-card-open'))
    expect(open).toHaveBeenCalledTimes(3)
    expect(screen.queryByTestId('board-shape-editor')).toBeNull()
  })
})
