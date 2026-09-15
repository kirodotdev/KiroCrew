vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import MultiSelect from '../components/MultiSelect'

describe('MultiSelect bulk actions', () => {
  it('filters by all searchable fields and clears the filter when closed', async () => {
    const user = userEvent.setup()
    render(
      <MultiSelect
        label="Selectable Models"
        options={[
          { value: 'model-a', label: 'Model A', description: 'small context' },
          { value: 'model-b', label: 'Model B', description: 'flagship context' },
        ]}
        selected={new Set()}
        onToggle={vi.fn()}
        summary="Selected 0 / 2"
      />,
    )

    const trigger = screen.getByRole('button', { name: 'Selectable Models' })
    await user.click(trigger)
    const search = screen.getByRole('textbox', { name: 'Search…' })
    await user.type(search, 'flagship')
    expect(screen.getByRole('checkbox', { name: 'Model B' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Model A' })).toBeNull()

    await user.clear(search)
    await user.type(search, 'missing')
    expect(screen.getByText('No matches')).toBeInTheDocument()

    await user.click(trigger)
    await user.click(trigger)
    expect(screen.getByRole('checkbox', { name: 'Model A' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Model B' })).toBeInTheDocument()
  })

  it('moves through option rows by keyboard and ignores locked rows', async () => {
    const user = userEvent.setup()
    const onToggle = vi.fn()
    render(
      <MultiSelect
        label="Selectable Models"
        options={[
          { value: 'auto', label: 'auto', locked: true },
          { value: 'model-a', label: 'model-a' },
          { value: 'model-b', label: 'model-b' },
        ]}
        selected={new Set(['auto'])}
        onToggle={onToggle}
        summary="Selected 1 / 3"
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Selectable Models' }))
    const search = screen.getByRole('textbox', { name: 'Search…' })
    const autoRow = screen.getByRole('checkbox', { name: 'auto' }).closest('label') as HTMLElement
    const modelARow = screen.getByRole('checkbox', { name: 'model-a' }).closest('label') as HTMLElement
    const modelBRow = screen.getByRole('checkbox', { name: 'model-b' }).closest('label') as HTMLElement

    fireEvent.keyDown(search, { key: 'ArrowDown' })
    expect(autoRow).toHaveFocus()
    fireEvent.keyDown(autoRow, { key: 'Enter' })
    expect(onToggle).not.toHaveBeenCalled()

    fireEvent.keyDown(autoRow, { key: 'ArrowDown' })
    expect(modelARow).toHaveFocus()
    fireEvent.keyDown(modelARow, { key: ' ' })
    expect(onToggle).toHaveBeenCalledWith('model-a', true)

    fireEvent.keyDown(modelARow, { key: 'End' })
    expect(modelBRow).toHaveFocus()
    fireEvent.keyDown(modelBRow, { key: 'Home' })
    expect(autoRow).toHaveFocus()
    fireEvent.keyDown(autoRow, { key: 'ArrowUp' })
    expect(search).toHaveFocus()
  })

  it('keeps bulk actions keyboard reachable beside search without toggling rows', async () => {
    const user = userEvent.setup()
    const onToggle = vi.fn()
    const selectAll = vi.fn()
    const deselectAll = vi.fn()
    render(
      <MultiSelect
        label="Selectable Models"
        options={[
          { value: 'auto', label: 'auto', locked: true },
          { value: 'model-a', label: 'model-a' },
        ]}
        selected={new Set(['auto'])}
        onToggle={onToggle}
        bulkActions={[
          { label: 'Select all', onSelect: selectAll },
          { label: 'Deselect all', onSelect: deselectAll },
        ]}
        summary="Selected 1 / 2"
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Selectable Models' }))
    const search = screen.getByRole('textbox', { name: 'Search…' })
    expect(search).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Select all' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(selectAll).toHaveBeenCalledOnce()
    expect(onToggle).not.toHaveBeenCalled()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Deselect all' })).toHaveFocus()
    await user.keyboard(' ')
    expect(deselectAll).toHaveBeenCalledOnce()
    expect(onToggle).not.toHaveBeenCalled()
  })
})
