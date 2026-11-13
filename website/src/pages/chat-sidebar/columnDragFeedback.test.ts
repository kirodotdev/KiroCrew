import { describe, it, expect, vi } from 'vitest'
import type { DragEvent } from 'react'
import { columnDropMark, columnDropSide, draggedColumnId, endColumnDrag, markColumnDropSide, startColumnDrag } from './columnDragFeedback'

const ORDER = ['a', 'b', 'c', 'd']

/** What the column's `drop` handler does with the same three inputs. */
function reorder(order: string[], dragged: string, target: string): string[] {
  const ids = order.filter(id => id !== dragged)
  ids.splice(order.indexOf(target), 0, dragged)
  return ids
}

function strip(): { strip: HTMLElement; cols: Record<string, HTMLElement> } {
  const el = document.createElement('div')
  const cols: Record<string, HTMLElement> = {}
  for (const id of ORDER) {
    const col = document.createElement('div')
    col.dataset.boardColumn = id
    el.appendChild(col)
    cols[id] = col
  }
  return { strip: el, cols }
}

function dragEvent(setDragImage?: unknown): DragEvent<HTMLElement> {
  return { clientX: 30, clientY: 12, dataTransfer: { setDragImage } } as unknown as DragEvent<HTMLElement>
}

describe('columnDropSide', () => {
  it('names the side the drop handler actually puts the column on', () => {
    for (const dragged of ORDER) {
      for (const target of ORDER) {
        const side = columnDropSide(ORDER, dragged, target)
        if (dragged === target) {
          expect(side).toBeNull()
          continue
        }
        const after = reorder(ORDER, dragged, target)
        const expected = after.indexOf(dragged) > after.indexOf(target) ? 'after' : 'before'
        expect(side, `${dragged} over ${target}`).toBe(expected)
      }
    }
  })

  it('reaches both ends of the board', () => {
    expect(columnDropSide(ORDER, 'b', 'd')).toBe('after')
    expect(reorder(ORDER, 'b', 'd').at(-1)).toBe('b')
    expect(columnDropSide(ORDER, 'c', 'a')).toBe('before')
    expect(reorder(ORDER, 'c', 'a')[0]).toBe('c')
  })

  it('shows nothing for a column that is not on the board', () => {
    expect(columnDropSide(ORDER, 'gone', 'a')).toBeNull()
    expect(columnDropSide(ORDER, 'a', 'gone')).toBeNull()
  })
})

describe('column drag marks', () => {
  it('marks the source, records it on the strip, and lifts the whole column as the image', () => {
    const { strip: s, cols } = strip()
    const setDragImage = vi.fn()
    startColumnDrag(dragEvent(setDragImage), cols.b, 'b')
    expect(cols.b.dataset.colDrag).toBe('source')
    expect(s.dataset.colDragging).toBe('b')
    expect(draggedColumnId(cols.d)).toBe('b')
    expect(setDragImage).toHaveBeenCalledWith(cols.b, 30, 12)
  })

  it('still marks the drag where setDragImage is missing or throws', () => {
    const { cols } = strip()
    startColumnDrag(dragEvent(undefined), cols.a, 'a')
    expect(cols.a.dataset.colDrag).toBe('source')
    startColumnDrag(dragEvent(() => { throw new Error('Not implemented.') }), cols.c, 'c')
    expect(cols.c.dataset.colDrag).toBe('source')
  })

  it('sets and clears the drop side on one column', () => {
    const { cols } = strip()
    markColumnDropSide(cols.c, 'after')
    expect(columnDropMark(cols.c)).toBe('after')
    markColumnDropSide(cols.c, null)
    expect(columnDropMark(cols.c)).toBeNull()
  })

  it('clears every mark in the strip when the drag ends', () => {
    const { strip: s, cols } = strip()
    startColumnDrag(dragEvent(vi.fn()), cols.a, 'a')
    markColumnDropSide(cols.c, 'after')
    markColumnDropSide(cols.d, 'after')
    endColumnDrag(cols.a)
    expect(s.dataset.colDragging).toBeUndefined()
    expect(s.querySelectorAll('[data-col-drag], [data-col-drop-before], [data-col-drop-after]')).toHaveLength(0)
  })
})
