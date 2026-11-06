import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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
