vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import MultiSelect from '../components/MultiSelect'

describe('MultiSelect option hint', () => {
  it('shows the hint beside a named row only, and still matches search by value', async () => {
    const user = userEvent.setup()
    render(
      <MultiSelect
        label="Selectable Models"
        options={[
          { value: 'claude-opus-5', label: 'Opus 5.5', hint: 'claude-opus-5' },
          { value: 'model-b', label: 'Model B' },
        ]}
        selected={new Set()}
        onToggle={vi.fn()}
        summary="Selected 0 / 2"
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Selectable Models' }))
    const opusLabel = screen.getByRole('checkbox', { name: 'Opus 5.5' }).closest('label') as HTMLElement
    expect(opusLabel.querySelector('[data-multi-select-hint]')?.textContent).toBe('claude-opus-5')
    const modelBLabel = screen.getByRole('checkbox', { name: 'Model B' }).closest('label') as HTMLElement
    expect(modelBLabel.querySelector('[data-multi-select-hint]')).toBeNull()

    await user.type(screen.getByRole('textbox', { name: 'Search…' }), 'claude-opus')
    expect(screen.getByRole('checkbox', { name: 'Opus 5.5' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Model B' })).toBeNull()
  })

  // The gateway clips a backend-sent label to 400 characters
  // (ADVERTISED_LABEL_MAX_CHARS), and the row must still not grow past one line
  // at that bound, so the label and description have to sit in truncating elements.
  it('keeps an at-bound label and description truncated, with the hint still shown', async () => {
    const user = userEvent.setup()
    const long = 'N'.repeat(400)
    const desc = 'D'.repeat(400)
    render(
      <MultiSelect
        label="Selectable Models"
        options={[{ value: 'claude-opus-5', label: long, hint: 'claude-opus-5', description: desc }]}
        selected={new Set()}
        onToggle={vi.fn()}
        summary="Selected 0 / 1"
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Selectable Models' }))
    const row = screen.getByRole('checkbox', { name: long }).closest('label') as HTMLElement
    expect(screen.getByText(long).className).toContain('truncate')
    expect(screen.getByText(desc).className).toContain('truncate')
    expect(row.querySelector('[data-multi-select-hint]')?.textContent).toBe('claude-opus-5')
  })
})
