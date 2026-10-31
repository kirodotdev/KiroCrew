import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import EditorBindingCard from '../components/editorBindings/EditorBindingCard'
import type { BindingCardView } from '../apps/editorBindingContributions'

const rows = [
  { key: 'a', path: '/p/A.java', line: 2, sameFile: true, primary: 'A.run()', secondary: 'void run()' },
  { key: 'b', path: '/p/B.java', line: 7, sameFile: false, primary: 'B.java:7', secondary: '' },
  { key: 'c', path: '/p/C.java', line: 9, character: 4, sameFile: false, primary: 'C.java:9', secondary: 'class C' },
]
const picker: BindingCardView = { status: 'ready', title: 'Definitions', rows, markdown: '' }

describe('EditorBindingCard', () => {
  it('names the binding while loading and closes on Escape', () => {
    const onClose = vi.fn()
    render(<EditorBindingCard view={{ status: 'loading', title: 'Go to definition' }} onOpen={vi.fn()} onClose={onClose} />)
    expect(screen.getByRole('status').textContent).toContain('Go to definition')
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('lists the rows with a count, filters them, and opens the keyboard pick', () => {
    const onOpen = vi.fn()
    render(<EditorBindingCard view={picker} onOpen={onOpen} onClose={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(3)
    expect(screen.getByText('3')).toBeTruthy()
    const filter = screen.getByRole('combobox')
    expect(document.activeElement).toBe(filter)
    fireEvent.change(filter, { target: { value: 'c' } })
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['C.java:9class C'])
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledWith({ path: '/p/C.java', line: 9, character: 4 }, false)
  })

  it('moves with the arrow keys and opens a clicked row in the same file', () => {
    const onOpen = vi.fn()
    render(<EditorBindingCard view={picker} onOpen={onOpen} onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'ArrowDown' })
    fireEvent.keyDown(dialog, { key: 'Enter' })
    expect(onOpen).toHaveBeenLastCalledWith({ path: '/p/B.java', line: 7, character: undefined }, false)
    fireEvent.click(screen.getByText('A.run()'))
    expect(onOpen).toHaveBeenLastCalledWith({ path: '/p/A.java', line: 2, character: undefined }, true)
  })

  it('shows markdown with no filter and says when a filter matches nothing', () => {
    const { rerender } = render(<EditorBindingCard view={{ status: 'ready', title: 'Symbol', rows: [], markdown: '**int** count' }} onOpen={vi.fn()} onClose={vi.fn()} />)
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.getByText('int').tagName).toBe('STRONG')
    rerender(<EditorBindingCard view={picker} onOpen={vi.fn()} onClose={vi.fn()} />)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzz' } })
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByText('Nothing found')).toBeTruthy()
  })
})
