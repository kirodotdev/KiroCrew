import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { TagListEditor } from '../pages/settings/SlackPanel'

// The id-list editor shared by the channel settings panels names its add box
// after the list it adds to. Its placeholder is a sample id, which is not a
// name and vanishes once text is typed.

describe('TagListEditor add box name', () => {
  it('names the add box with the field label', () => {
    render(<TagListEditor label="Allowed users" values={[]} placeholder="U0123456789" onChange={vi.fn()} />)
    expect(screen.getByRole('textbox', { name: 'Allowed users' })).toBeInTheDocument()
  })

  it('keeps two editors on one page apart by name', () => {
    render(<>
      <TagListEditor label="Allowed users" values={[]} placeholder="U0123456789" onChange={vi.fn()} />
      <TagListEditor label="Allowed channels" values={[]} placeholder="C0123456789" onChange={vi.fn()} />
    </>)
    expect(screen.getByRole('textbox', { name: 'Allowed users' })).toHaveAttribute('placeholder', 'U0123456789')
    expect(screen.getByRole('textbox', { name: 'Allowed channels' })).toHaveAttribute('placeholder', 'C0123456789')
  })
})

// Typed-but-not-added text lives only in the editor, so a parent guarding
// unsaved edits (the SDK card's restart hold) is told when it appears and goes.
describe('TagListEditor onPendingChange', () => {
  it('reports typed text, its clearing, and the Add that empties the box, once per change', () => {
    const onPending = vi.fn()
    const onChange = vi.fn()
    render(<TagListEditor label="Allowed users" values={[]} placeholder="U0123456789" onChange={onChange} onPendingChange={onPending} />)
    const box = screen.getByRole('textbox', { name: 'Allowed users' })
    expect(onPending).not.toHaveBeenCalled()

    fireEvent.change(box, { target: { value: '   ' } })
    expect(onPending).not.toHaveBeenCalled()
    fireEvent.change(box, { target: { value: 'U1' } })
    fireEvent.change(box, { target: { value: 'U12' } })
    expect(onPending.mock.calls).toEqual([[true]])
    fireEvent.change(box, { target: { value: '' } })
    expect(onPending.mock.calls).toEqual([[true], [false]])

    fireEvent.change(box, { target: { value: 'U3' } })
    fireEvent.click(screen.getByRole('button', { name: /add/i }))
    expect(onChange).toHaveBeenCalledWith(['U3'])
    expect(onPending.mock.calls).toEqual([[true], [false], [true], [false]])
  })

  it('keeps reporting pending while a rejected ID stays in the box, and releases on unmount', () => {
    const onPending = vi.fn()
    const { unmount } = render(
      <TagListEditor label="Allowed users" values={[]} placeholder="U0123456789" onChange={vi.fn()} validate={() => false} onPendingChange={onPending} />,
    )
    fireEvent.change(screen.getByRole('textbox', { name: 'Allowed users' }), { target: { value: 'bad' } })
    fireEvent.click(screen.getByRole('button', { name: /add/i }))
    expect(onPending.mock.calls).toEqual([[true]])
    unmount()
    expect(onPending.mock.calls).toEqual([[true], [false]])
  })

  it('works without the callback', () => {
    render(<TagListEditor label="Allowed users" values={[]} placeholder="U0123456789" onChange={vi.fn()} />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Allowed users' }), { target: { value: 'U1' } })
    expect(screen.getByRole('button', { name: /add/i })).toBeEnabled()
  })
})
