import { describe, it, expect } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import CrewDashboardLoading from './CrewDashboardLoading'

/**
 * The one loading treatment the Dashboard tab draws for all three serial waits
 * (lazy chunk, read, mint). These guard the contract the three old placeholders
 * used to split between them: the first-phase test still looks for the
 * `crew-dashboard-loading` testid, and a reader needs one named status region
 * rather than three separate announcements.
 */
describe('CrewDashboardLoading', () => {
  it('keeps the crew-dashboard-loading testid the loading branch is found by', () => {
    const view = renderWithProviders(<CrewDashboardLoading />)
    expect(view.getByTestId('crew-dashboard-loading')).toBeTruthy()
  })

  it('names the wait once for a screen reader and hides the decorative boxes', () => {
    const view = renderWithProviders(<CrewDashboardLoading />)
    const status = view.getByRole('status')
    // One label, not the three separate sentences the old phases announced.
    expect(status.getAttribute('aria-label')).toBe('Loading the dashboard')
    // The skeleton boxes are decoration and must not be read out.
    expect(status.querySelector('[aria-hidden]')).toBeTruthy()
  })

  it('draws no explanatory copy — the skeleton previews the panel instead', () => {
    const view = renderWithProviders(<CrewDashboardLoading />)
    expect(view.queryByText(/rendering|drawing|loading the dashboard/i)).toBeNull()
  })
})
