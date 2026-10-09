import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { useLocation, useNavigate } from 'react-router-dom'

import { NavBadge } from '../App'
import { clearFeatureNewTag, deliverFeatureNewTag, featureNewTagRoute } from '../utils/featureNewTag'
import { renderWithProviders } from './helpers'

// The ghost flight is covered in featureNewTag.test.ts; here only the route matters.
vi.mock('../utils/featureNewTag', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/featureNewTag')>()),
  landFeatureGhost: vi.fn(),
}))

const ROUTE = '/members?member=default'

function Probe() {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <span data-testid="loc">{pathname + search}</span>
      <button type="button" onClick={() => navigate('/members')}>open</button>
    </>
  )
}

function renderAt(route: string) {
  renderWithProviders(
    <>
      <NavBadge navId="members" collapsed={false} appBadges={{}} />
      <Probe />
    </>,
    { route },
  )
}

beforeEach(() => {
  localStorage.clear()
  act(() => clearFeatureNewTag('members'))
})

describe('NavBadge New tag route', () => {
  it('only clears a tag set while the page is already open', () => {
    renderAt('/members')
    act(() => deliverFeatureNewTag('members', 'New', null, ROUTE))
    expect(screen.getByTestId('loc').textContent).toBe('/members')
    expect(featureNewTagRoute('members')).toBeNull()
  })

  it('opens the stored route when the user arrives at the page', () => {
    renderAt('/')
    act(() => deliverFeatureNewTag('members', 'New', null, ROUTE))
    act(() => screen.getByText('open').click())
    expect(screen.getByTestId('loc').textContent).toBe(ROUTE)
    expect(featureNewTagRoute('members')).toBeNull()
  })

  it('treats a page load onto the route as arriving', () => {
    act(() => deliverFeatureNewTag('members', 'New', null, ROUTE))
    renderAt('/members')
    expect(screen.getByTestId('loc').textContent).toBe(ROUTE)
  })

  it('keeps a link to a specific crewmate and only clears the tag', () => {
    act(() => deliverFeatureNewTag('members', 'New', null, ROUTE))
    renderAt('/members?member=helper')
    expect(screen.getByTestId('loc').textContent).toBe('/members?member=helper')
    expect(featureNewTagRoute('members')).toBeNull()
  })
})
