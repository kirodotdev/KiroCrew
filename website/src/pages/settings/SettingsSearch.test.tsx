import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'

import SettingsSearch from './SettingsSearch'

/**
 * The in-page settings search: typing filters SETTINGS_REGISTRY, activation
 * hands off to useSettingHighlight via a FRESH path URL
 * (/settings/<tab>[/<sub>]?highlight=<id>), so stale params from the previous
 * tab can never block the target panel from mounting.
 *
 * Queries pin against long-stable registry entries (the Display tab's
 * zoom-level toggle) rather than enumerating the registry, so routine
 * regeneration does not churn this file.
 */

/** Renders the live location so assertions read what activation wrote. */
function ParamsProbe() {
  const location = useLocation()
  return (
    <>
      <div data-testid="pathname">{location.pathname}</div>
      <div data-testid="params">{new URLSearchParams(location.search).toString()}</div>
    </>
  )
}

function setup(initialEntry = '/settings?tab=chat&channel=slack') {
  // A QueryClient because the search reads `['dashboardConfig']` to learn whether a
  // governance-gated entry may be offered at all. The cases below are about ranking
  // and activation and query nothing governed, so an unresolved read is fine here;
  // `settingsSearchGovernance.test.ts` owns the offered/withheld behaviour.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SettingsSearch />
        <ParamsProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const input = () => screen.getByRole('combobox')

describe('SettingsSearch', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows matching rows for a query against a stable registry entry', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'zoom' } })
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(screen.getAllByRole('option').length).toBeGreaterThan(0)
    expect(screen.getByText('Zoom Level')).toBeInTheDocument()
  })

  it('activation writes a fresh path URL: tab segment + highlight, dropping stale params', () => {
    setup('/settings?tab=chat&channel=slack')
    fireEvent.change(input(), { target: { value: 'zoom' } })
    fireEvent.mouseDown(screen.getByText('Zoom Level'))
    expect(screen.getByTestId('pathname').textContent).toBe('/settings/display')
    const params = new URLSearchParams(screen.getByTestId('params').textContent ?? '')
    expect(params.get('highlight')).toBe('display.zoom-level')
    // The stale legacy params from the previous URL must not ride along.
    expect(params.get('tab')).toBeNull()
    expect(params.get('channel')).toBeNull()
    // Input clears and the dropdown closes after activation.
    expect(input()).toHaveValue('')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('carries an entry\u2019s own params into the deep link — as the second path segment', () => {
    setup()
    fireEvent.change(input(), { target: { value: 'auto-approve stays on' } })
    fireEvent.mouseDown(screen.getByText('How long auto-approve stays on'))
    // The registry entry carries the legacy `section` key; settingsRoute
    // mints it as the second PATH segment, so the navigation shell's level
    // test sees the drill-in (one back bar, not two).
    expect(screen.getByTestId('pathname').textContent).toBe('/settings/security/approval')
    const params = new URLSearchParams(screen.getByTestId('params').textContent ?? '')
    expect(params.get('sub')).toBeNull()
    expect(params.get('section')).toBeNull()
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

  it('still offers the governed entry when the ceiling read FAILED', async () => {
    // This component resolves the governance answer itself
    // (`!isSuccess || data?.decisions_enabled === true`), so the case belongs here
    // rather than against a restatement of that expression. A failed read is not a
    // denial: the Decisions card this row navigates to renders the read-failed notice,
    // so withholding the row would report the setting as absent on a host that merely
    // could not check.
    vi.spyOn(api, 'dashboardConfig').mockRejectedValue(new Error('offline'))
    setup()
    fireEvent.change(input(), { target: { value: 'Decisions' } })
    await waitFor(() => {
      expect(
        screen.getAllByRole('option').some(o => /Decisions/i.test(o.textContent ?? '')),
      ).toBe(true)
    })
  })
})
