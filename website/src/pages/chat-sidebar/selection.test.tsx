/**
 * The Sessions sidebar's batch selection, at its interface: which presses pick rows,
 * which stay the row's own, and which rows can be picked at all.
 */
import { useRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { sessionRowIdentity } from './rowIdentity'
import { rowsBetween, selectionGesture, useSessionSelection } from './selection'
import type { Slot } from './types'

const local = (key: string, over: Partial<Slot> = {}): Slot => ({ key, title: key, running: false, ...over }) as Slot
const peer = (key: string): Slot => local(key, { peer_id: 'inst-a', row_identity: `inst-a:${key}` })

const NO_KEYS = { shiftKey: false, metaKey: false, ctrlKey: false, altKey: false }

describe('selectionGesture', () => {
  it('reads Shift as a range, and a plain press as a toggle only while rows are picked', () => {
    expect(selectionGesture({ ...NO_KEYS, shiftKey: true }, false)).toBe('range')
    expect(selectionGesture({ ...NO_KEYS, shiftKey: true }, true)).toBe('range')
    expect(selectionGesture(NO_KEYS, true)).toBe('toggle')
    expect(selectionGesture(NO_KEYS, false)).toBeNull()
  })

  // docs/decisions/2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md:
  // the tab modifier keeps its one meaning on a session row, picked rows or not.
  it.each(['metaKey', 'ctrlKey', 'altKey'] as const)('leaves a %s press to the row', modifier => {
    expect(selectionGesture({ ...NO_KEYS, [modifier]: true }, true)).toBeNull()
    expect(selectionGesture({ ...NO_KEYS, [modifier]: true, shiftKey: true }, true)).toBeNull()
  })
})

describe('rowsBetween', () => {
  const order = ['a', 'b', 'c', 'd']
  it('takes both ends, in the drawn order, whichever end was pressed first', () => {
    expect(rowsBetween(order, 'b', 'd')).toEqual(['b', 'c', 'd'])
    expect(rowsBetween(order, 'd', 'b')).toEqual(['b', 'c', 'd'])
    expect(rowsBetween(order, 'c', 'c')).toEqual(['c'])
  })
  it('is empty when an end is not drawn', () => {
    expect(rowsBetween(order, 'x', 'c')).toEqual([])
    expect(rowsBetween(order, 'a', 'x')).toEqual([])
  })
})

/** Rows as the lanes draw them: the row marker, the row's own click and key
 *  handlers, an inner control, and a collapsed folder's `inert` wrapper. The clear
 *  button stands for the bar: it is mounted only while rows are picked. */
function Harness({ rows, activeSlot = null, onOpen, collapsed = [], dragging = false }: {
  rows: Slot[]
  activeSlot?: string | null
  onOpen: (key: string) => void
  collapsed?: string[]
  dragging?: boolean
}) {
  const rootRef = useRef<HTMLDivElement>(null)
  const selection = useSessionSelection({ rows, activeSlot, rootRef, dragging })
  return (
    <div ref={rootRef} data-testid="root" {...selection.rootProps}>
      {rows.map(s => (
        <div key={sessionRowIdentity(s)} {...(collapsed.includes(s.key) ? { inert: '' } : {})}>
          <div role="button" tabIndex={0} data-session-row={sessionRowIdentity(s)}
            data-testid={`row-${sessionRowIdentity(s)}`}
            onClick={() => onOpen(`click:${s.key}`)}
            onDoubleClick={() => onOpen(`rename:${s.key}`)}
            onKeyDown={e => { if (e.key === 'Enter') onOpen(`enter:${s.key}`) }}>
            <span data-testid={`title-${s.key}`}>{s.title}</span>
            <button type="button" data-testid={`menu-${s.key}`} onClick={e => { e.stopPropagation(); onOpen(`menu:${s.key}`) }}>menu</button>
            <input aria-label="Rename" data-testid={`rename-${s.key}`} onClick={e => e.stopPropagation()} />
          </div>
        </div>
      ))}
      <output data-testid="picked">{[...selection.selected].sort().join(',')}</output>
      <button type="button" data-testid="menu-select-c" onClick={() => selection.menu.toggle('c')}>select c</button>
      {selection.selecting && <button type="button" data-testid="clear" onClick={selection.clear}>clear</button>}
    </div>
  )
}

const ROWS = [local('a'), local('b'), local('c'), local('d')]
const row = (id: string) => screen.getByTestId(`row-${id}`)
const picked = () => screen.getByTestId('picked').textContent

describe('useSessionSelection', () => {
  it('picks from the open session to the Shift-clicked row, without opening it', () => {
    const onOpen = vi.fn()
    render(<Harness rows={ROWS} activeSlot="b" onOpen={onOpen} />)
    fireEvent.click(row('d'), { shiftKey: true })
    expect(picked()).toBe('b,c,d')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('picks only the Shift-clicked row when the open session is not drawn', () => {
    render(<Harness rows={ROWS} activeSlot="gone" onOpen={vi.fn()} />)
    fireEvent.click(row('c'), { shiftKey: true })
    expect(picked()).toBe('c')
  })

  it('re-aims a range from its anchor, and adds it to the rows toggled before', () => {
    render(<Harness rows={ROWS} onOpen={vi.fn()} />)
    fireEvent.click(row('b'), { shiftKey: true })
    fireEvent.click(row('d'), { shiftKey: true })
    expect(picked()).toBe('b,c,d')
    // The same anchor, a nearer end: the range shrinks instead of growing.
    fireEvent.click(row('c'), { shiftKey: true })
    expect(picked()).toBe('b,c')
    // A toggle moves the anchor and keeps what is picked.
    fireEvent.click(row('a'))
    fireEvent.click(row('a'), { shiftKey: true })
    expect(picked()).toBe('a,b,c')
  })

  it('toggles on a plain click only while rows are picked', () => {
    const onOpen = vi.fn()
    render(<Harness rows={ROWS} onOpen={onOpen} />)
    fireEvent.click(row('a'))
    expect(onOpen).toHaveBeenCalledWith('click:a')
    expect(picked()).toBe('')

    fireEvent.click(row('b'), { shiftKey: true })
    onOpen.mockClear()
    fireEvent.click(row('d'))
    fireEvent.click(row('b'))
    expect(picked()).toBe('d')
    expect(onOpen).not.toHaveBeenCalled()
    // Dropping the last picked row ends the selection: the next click opens.
    fireEvent.click(row('d'))
    fireEvent.click(row('a'))
    expect(onOpen).toHaveBeenCalledWith('click:a')
  })

  it('leaves the tab modifier, the row\'s inner controls and a double click on a picked list alone', () => {
    const onOpen = vi.fn()
    render(<Harness rows={ROWS} onOpen={onOpen} />)
    fireEvent.click(row('b'), { shiftKey: true })
    fireEvent.click(row('c'), { metaKey: true })
    fireEvent.click(row('c'), { ctrlKey: true })
    fireEvent.click(screen.getByTestId('menu-c'))
    fireEvent.click(screen.getByTestId('rename-c'))
    expect(onOpen.mock.calls.map(c => c[0])).toEqual(['click:c', 'click:c', 'menu:c'])
    expect(picked()).toBe('b')
    // No rename opens over a selection.
    fireEvent.doubleClick(screen.getByTestId('title-c'))
    expect(onOpen).not.toHaveBeenCalledWith('rename:c')
  })

  it('toggles the focused row on Enter and Space, and drops the selection on Escape', () => {
    const onOpen = vi.fn()
    render(<Harness rows={ROWS} onOpen={onOpen} />)
    fireEvent.keyDown(row('a'), { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledWith('enter:a')

    fireEvent.click(row('b'), { shiftKey: true })
    onOpen.mockClear()
    fireEvent.keyDown(row('c'), { key: 'Enter' })
    fireEvent.keyDown(row('d'), { key: ' ' })
    expect(picked()).toBe('b,c,d')
    expect(onOpen).not.toHaveBeenCalled()
    // A text box inside the sidebar keeps its own Escape.
    fireEvent.keyDown(screen.getByTestId('rename-a'), { key: 'Escape' })
    expect(picked()).toBe('b,c,d')
    fireEvent.keyDown(row('c'), { key: 'Escape' })
    expect(picked()).toBe('')
  })

  it('never picks a row another machine or another page owns', () => {
    const onOpen = vi.fn()
    const member = local('m', { mode: 'member', agent: 'bob' })
    render(<Harness rows={[local('a'), peer('a'), member, local('z')]} onOpen={onOpen} />)
    fireEvent.click(row('a'), { shiftKey: true })
    fireEvent.click(row('z'), { shiftKey: true })
    // The peer row shares the local row's raw key: the range reads identities.
    expect(picked()).toBe('a,z')
    fireEvent.click(row('inst-a:a'))
    fireEvent.click(row('m'))
    expect(onOpen.mock.calls.map(c => c[0])).toEqual(['click:a', 'click:m'])
    expect(picked()).toBe('a,z')
  })

  it('drops a picked row the list no longer shows, for good', () => {
    const { rerender } = render(<Harness rows={ROWS} onOpen={vi.fn()} />)
    fireEvent.click(row('a'), { shiftKey: true })
    fireEvent.click(row('c'), { shiftKey: true })
    rerender(<Harness rows={[local('a'), local('d')]} onOpen={vi.fn()} />)
    expect(picked()).toBe('a')
    rerender(<Harness rows={[local('d')]} onOpen={vi.fn()} />)
    expect(picked()).toBe('')
    // A search that is cleared brings the rows back, not the selection.
    rerender(<Harness rows={ROWS} onOpen={vi.fn()} />)
    expect(picked()).toBe('')
  })

  it('never reaches a row in a collapsed folder: not by a range, and not once it collapses', () => {
    const { rerender } = render(<Harness rows={ROWS} collapsed={['b']} onOpen={vi.fn()} />)
    fireEvent.click(row('a'), { shiftKey: true })
    fireEvent.click(row('d'), { shiftKey: true })
    expect(picked()).toBe('a,c,d')
    rerender(<Harness rows={ROWS} collapsed={['b', 'c']} onOpen={vi.fn()} />)
    expect(picked()).toBe('a,d')
  })

  it('takes a double click as one toggle, and opens nothing with its second click', () => {
    const onOpen = vi.fn()
    render(<Harness rows={ROWS} onOpen={onOpen} />)
    fireEvent.click(row('b'), { shiftKey: true })
    fireEvent.click(row('c'), { detail: 1 })
    fireEvent.click(row('c'), { detail: 2 })
    expect(picked()).toBe('b,c')
    // The toggle that ends the selection: its second click must not open the row.
    fireEvent.click(row('b'), { detail: 1 })
    fireEvent.click(row('c'), { detail: 1 })
    fireEvent.click(row('c'), { detail: 2 })
    fireEvent.doubleClick(screen.getByTestId('title-c'))
    expect(picked()).toBe('')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('toggles once for a held key, and leaves Escape to a drag in flight', () => {
    const { rerender } = render(<Harness rows={ROWS} onOpen={vi.fn()} />)
    fireEvent.click(row('b'), { shiftKey: true })
    fireEvent.keyDown(row('c'), { key: 'Enter' })
    fireEvent.keyDown(row('c'), { key: 'Enter', repeat: true })
    expect(picked()).toBe('b,c')
    rerender(<Harness rows={ROWS} dragging onOpen={vi.fn()} />)
    fireEvent.keyDown(row('c'), { key: 'Escape' })
    expect(picked()).toBe('b,c')
  })

  it('hands focus to a row when the selection ends under a control that leaves with it', () => {
    render(<Harness rows={ROWS} onOpen={vi.fn()} />)
    fireEvent.click(row('b'), { shiftKey: true })
    const clear = screen.getByTestId('clear')
    clear.focus()
    fireEvent.click(clear)
    expect(document.activeElement).toBe(row('a'))
  })

  it('leaves focus where it is when the selection ends from a row', () => {
    render(<Harness rows={ROWS} onOpen={vi.fn()} />)
    fireEvent.click(row('b'), { shiftKey: true })
    row('c').focus()
    fireEvent.keyDown(row('c'), { key: 'Escape' })
    expect(document.activeElement).toBe(row('c'))
  })

  it('toggles one row from the row menu', () => {
    render(<Harness rows={ROWS} onOpen={vi.fn()} />)
    fireEvent.click(screen.getByTestId('menu-select-c'))
    expect(picked()).toBe('c')
    fireEvent.click(screen.getByTestId('menu-select-c'))
    expect(picked()).toBe('')
  })
})
