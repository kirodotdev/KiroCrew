import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useSearchParams } from 'react-router-dom'

import SettingsSearch from './SettingsSearch'

/**
 * The in-page settings search: typing filters SETTINGS_REGISTRY, activation
 * hands off to useSettingHighlight via a FRESH set of search params (tab +
 * entry params + highlight), so stale params from the previous tab can never
 * block the target panel from mounting.
 *
 * Queries pin against long-stable registry entries (the Display tab's
 * zoom-level toggle) rather than enumerating the registry, so routine
 * regeneration does not churn this file.
 */

/** Renders the live search params so assertions read what activation wrote. */
function ParamsProbe() {
  const [params] = useSearchParams()
  return <div data-testid="params">{params.toString()}</div>
}

function setup(initialEntry = '/settings?tab=chat&channel=slack') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SettingsSearch />
      <ParamsProbe />
    </MemoryRouter>,
  )
}

const input = () => screen.getByRole('combobox')

describe('SettingsSearch', () => {
  it('shows matching rows for a query against a stable registry entry', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'zoom' } })
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(screen.getAllByRole('option').length).toBeGreaterThan(0)
    expect(screen.getByText('Zoom Level')).toBeInTheDocument()
  })

  it('activation writes fresh params: tab + highlight, dropping stale ones', () => {
    setup('/settings?tab=chat&channel=slack')
    fireEvent.change(input(), { target: { value: 'zoom' } })
    fireEvent.mouseDown(screen.getByText('Zoom Level'))
    const params = new URLSearchParams(screen.getByTestId('params').textContent ?? '')
    expect(params.get('tab')).toBe('display')
    expect(params.get('highlight')).toBe('display.zoom-level')
    // The stale channel param from the previous tab must not ride along.
    expect(params.get('channel')).toBeNull()
    // Input clears and the dropdown closes after activation.
    expect(input()).toHaveValue('')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('carries an entry\u2019s own params into the deep link', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'auto-approve stays on' } })
    fireEvent.mouseDown(screen.getByText('How long auto-approve stays on'))
    const params = new URLSearchParams(screen.getByTestId('params').textContent ?? '')
    expect(params.get('tab')).toBe('security')
    expect(params.get('section')).toBe('approval')
    expect(params.get('highlight')).toBe('security.how-long-auto-approve-stays-on')
  })

  it('ranks the yolo duration setting first for its keyword query', () => {
    // Not just present but TOP: selection resets to row 0 on each keystroke,
    // so Enter takes rows[0]. "Your Role" (y-o-l-o as a scattered label
    // subsequence) used to outrank the entry carrying the literal keyword,
    // sending Enter to an unrelated Chat setting.
    setup()
    fireEvent.change(input(), { target: { value: 'yolo' } })
    const rows = screen.getAllByRole('option')
    expect(rows[0]).toHaveTextContent('How long auto-approve stays on')
    expect(rows[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('Escape closes the dropdown and keeps the typed query', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'zoom' } })
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    fireEvent.keyDown(input(), { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(input()).toHaveValue('zoom')
  })

  it('shows the no-results row for a nonsense query', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'zzqqxxnothing' } })
    expect(screen.getByText('No matching settings')).toBeInTheDocument()
    expect(screen.queryByRole('option')).not.toBeInTheDocument()
  })

  it('arrow keys move aria-selected through the options', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'model' } })
    const before = screen.getAllByRole('option')
    expect(before.length).toBeGreaterThan(1)
    expect(before[0]).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(input(), { key: 'ArrowDown' })
    const after = screen.getAllByRole('option')
    expect(after[0]).toHaveAttribute('aria-selected', 'false')
    expect(after[1]).toHaveAttribute('aria-selected', 'true')
  })
})
