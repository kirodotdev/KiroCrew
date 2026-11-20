/**
 * The desktop side-panel rail (Settings, Customize, Developer) scrolls its rows in an
 * `overflow-y-auto` box with no side padding, and `overflow-y: auto` clips the x axis
 * too. The global focus ring sits 2px OUTSIDE a focused element, so on a full-width
 * row the box cut the ring's left and right sides off and only its top and bottom
 * showed. Each row draws its ring inside its own edge instead, the same inset the
 * side-panel tab chips and the Crew schedule rows use.
 *
 * jsdom does not lay out or paint, so this pins the class that keeps the ring inside
 * the row; the screenshots on the change show the painted result.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'developer', label: 'Developer', icon: null, group: 'System', tile: '#7c3aed' },
  { key: 'about', label: 'About', icon: null, group: 'System', dot: true },
]

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <SidePanelLayout title="Settings" tabs={TABS} rememberKey="test-rail-focus-ring">
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('side-panel rail: a focused row keeps its whole focus ring', () => {
  beforeEach(() => { mobile = false; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('draws every row\'s ring inside the row, where the scrolling list cannot clip it', () => {
    renderAt('?tab=developer')
    const rows = within(screen.getByTestId('side-panel-tab-list')).getAllByRole('button')
    expect(rows.map(r => r.textContent)).toEqual(['Overview', 'Developer', 'About'])
    for (const r of rows) expect(r).toHaveClass('focus-visible:outline-offset-[-2px]')
  })

  it('keeps the inset on the open row, beside the fill that marks it open', () => {
    renderAt('?tab=developer')
    const open = within(screen.getByTestId('side-panel-tab-list')).getByRole('button', { name: 'Developer' })
    expect(open).toHaveAttribute('aria-current', 'page')
    expect(open).toHaveClass('bg-accent-subtle', 'focus-visible:outline-offset-[-2px]')
  })
})
