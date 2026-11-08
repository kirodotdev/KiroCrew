import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'
import { useEffect } from 'react'
import { useListKeyboardNav } from './useListKeyboardNav'

/**
 * Hover selects a row only on real pointer motion.
 *
 * Each keystroke in a picker's filter re-ranks the rows and resets the
 * selection to the best match. The browser then fires mouseover, mouseenter
 * and mousemove at the SAME coordinates on whichever row now sits under a
 * resting pointer. Selecting on those let the resting pointer overwrite the
 * reset, so Enter chose the row under the cursor instead of the top match.
 */

function Harness({ open = true, rows = ['a', 'b', 'c'], onChoose = () => {}, resetKey = 0 }: {
  open?: boolean
  rows?: string[]
  onChoose?: (i: number) => void
  resetKey?: number
}) {
  const { selected, setSelected, hoverSelect } = useListKeyboardNav({
    open,
    count: rows.length,
    onChoose,
    onClose: () => {},
  })
  // Hosts reset to the best match whenever the query re-ranks the rows.
  useEffect(() => { setSelected(0) }, [resetKey, setSelected])
  if (!open) return null
  return (
    <div role="listbox">
      {rows.map((r, i) => (
        <div
          key={r}
          role="option"
          aria-selected={i === selected}
          tabIndex={-1}
          onMouseMove={e => hoverSelect(i, e)}
        >
          {r}
        </div>
      ))}
    </div>
  )
}

afterEach(() => cleanup())

const selectedIndex = (options: HTMLElement[]) =>
  options.findIndex(o => o.getAttribute('aria-selected') === 'true')

describe('useListKeyboardNav hoverSelect', () => {
  it('selects the row under the pointer when the pointer moves', () => {
    const { getAllByRole } = render(<Harness />)
    const options = getAllByRole('option')
    fireEvent.mouseMove(options[2], { clientX: 10, clientY: 50 })
    fireEvent.mouseMove(options[2], { clientX: 11, clientY: 50 })
    expect(selectedIndex(getAllByRole('option'))).toBe(2)
  })

  it('keeps the reset to the top match when rows re-render under a resting pointer', () => {
    const onChoose = vi.fn()
    const { getAllByRole, rerender } = render(<Harness onChoose={onChoose} />)
    fireEvent.mouseMove(getAllByRole('option')[2], { clientX: 10, clientY: 50 })
    fireEvent.mouseMove(getAllByRole('option')[2], { clientX: 11, clientY: 50 })
    expect(selectedIndex(getAllByRole('option'))).toBe(2)

    // A keystroke re-ranks the rows and resets the selection to row 0. The
    // browser then fires mousemove at the unchanged position on the row that
    // landed under the pointer.
    rerender(<Harness onChoose={onChoose} rows={['c', 'a', 'b']} resetKey={1} />)
    expect(selectedIndex(getAllByRole('option'))).toBe(0)
    fireEvent.mouseMove(getAllByRole('option')[2], { clientX: 11, clientY: 50 })
    expect(selectedIndex(getAllByRole('option'))).toBe(0)

    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onChoose).toHaveBeenCalledWith(0, false)
  })

  it('counts motion that happened outside the rows', () => {
    const { getAllByRole } = render(<Harness />)
    fireEvent.mouseMove(document.body, { clientX: 300, clientY: 300 })
    fireEvent.mouseMove(getAllByRole('option')[1], { clientX: 10, clientY: 30 })
    expect(selectedIndex(getAllByRole('option'))).toBe(1)
  })

  it('treats the first event after opening as a record, not a move', () => {
    const { getAllByRole } = render(<Harness />)
    fireEvent.mouseMove(getAllByRole('option')[2], { clientX: 10, clientY: 50 })
    expect(selectedIndex(getAllByRole('option'))).toBe(0)
  })

  it('forgets the pointer trail when the surface closes', () => {
    const { getAllByRole, rerender } = render(<Harness />)
    fireEvent.mouseMove(document.body, { clientX: 300, clientY: 300 })
    rerender(<Harness open={false} />)
    rerender(<Harness open />)
    // Reopened under a resting pointer: the first event has nothing from this
    // open to compare against, so it must not select.
    fireEvent.mouseMove(getAllByRole('option')[2], { clientX: 10, clientY: 50 })
    expect(selectedIndex(getAllByRole('option'))).toBe(0)
  })
})
