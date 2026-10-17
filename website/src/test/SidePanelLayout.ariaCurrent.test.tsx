/**
 * The side-panel rail (Settings, Customize, Developer) opens one section at a time and
 * paints the open one with a class. A class is invisible to assistive technology, so a
 * screen reader heard every row as the same button. The open tab's row carries
 * `aria-current="page"` (the rows navigate), and no other row does.
 *
 * The phone's root list is the other face of the same tabs, and it opens nothing yet:
 * `tab` resolves to the first tab there only so the pane has a default, so no row of
 * that list is the current page.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'shortcuts', label: 'Shortcuts', icon: null, group: 'System' },
  { key: 'about', label: 'About', icon: null, group: 'System' },
]

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <SidePanelLayout title="Settings" tabs={TABS} rememberKey="test-aria-current">
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

const row = (label: string) => screen.getByRole('button', { name: label })

describe('side-panel rail: the open tab is the current page', () => {
  beforeEach(() => { mobile = false; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('marks the open tab with aria-current="page"', () => {
    renderAt('?tab=shortcuts')
    expect(row('Shortcuts')).toHaveAttribute('aria-current', 'page')
  })

  it('marks no other row', () => {
    renderAt('?tab=shortcuts')
    expect(row('Overview')).not.toHaveAttribute('aria-current')
    expect(row('About')).not.toHaveAttribute('aria-current')
  })

  it('marks the first tab when the URL names none, because that pane is the one shown', () => {
    renderAt('')
    expect(screen.getByTestId('pane').textContent).toBe('overview')
    expect(row('Overview')).toHaveAttribute('aria-current', 'page')
  })

  it('moves the mark to the row that was opened', () => {
    renderAt('?tab=shortcuts')
    fireEvent.click(row('About'))
    expect(screen.getByTestId('pane').textContent).toBe('about')
    expect(row('About')).toHaveAttribute('aria-current', 'page')
    expect(row('Shortcuts')).not.toHaveAttribute('aria-current')
  })
})

describe('side-panel rail: the phone root list has no current page', () => {
  beforeEach(() => { mobile = true; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('marks none of its rows, although the first tab is the default', () => {
    renderAt('')
    expect(screen.queryByTestId('pane')).toBeNull()
    for (const t of TABS) expect(row(t.label)).not.toHaveAttribute('aria-current')
  })
})
