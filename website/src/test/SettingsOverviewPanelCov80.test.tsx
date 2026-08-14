// OverviewPanel is the settings-shell adapter for the standalone overview page:
// it must delegate, not re-implement.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('../pages/OverviewPage', () => ({
  default: () => <div data-testid="overview-page" />,
}))

import { OverviewPanel } from '../pages/settings/OverviewPanel'

describe('OverviewPanel', () => {
  it('renders the overview page as-is', () => {
    render(<OverviewPanel />)
    expect(screen.getByTestId('overview-page')).toBeInTheDocument()
  })

  it('adds no wrapper of its own around it', () => {
    const { container } = render(<OverviewPanel />)
    expect(container.firstElementChild).toBe(screen.getByTestId('overview-page'))
  })
})
