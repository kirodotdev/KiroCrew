import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import React from 'react'
import AppPage from './AppPage'

// While an installed app's metadata loads, the page shows only a spinner and a
// line of text. That line must be a status region, so a screen reader hears it.
const { getApp } = vi.hoisted(() => ({ getApp: vi.fn() }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ name: 'alpha' }),
  useNavigate: () => vi.fn(),
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../components/InstalledAppSurface', () => ({
  default: () => <div data-testid="app-surface" />,
}))

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AppPage loading line', () => {
  it('is announced as a status while the app loads', () => {
    getApp.mockReturnValue(new Promise(() => {}))
    render(<AppPage />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Loading app')
    // The spinner is decoration, so only the text is read out.
    expect(status.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
  })
})
