/**
 * Dev Fleet "live?" badge (#10191).
 *
 * When the gateway runs from a packaged install it cannot confirm which checkout
 * it is executing, so the live row comes from the live-target pointer alone and
 * the payload says `live_verified: false`. The row then shows "live?" with the
 * reason as visible text (not a hover-only tooltip), never the plain "live" badge
 * that asserts it. A verified live row, or an older payload with no
 * `live_verified`, keeps the plain badge.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

// A fixed instant: the badge does not depend on row age.
const now = 1_760_000_000

function fleetWith(liveVerified?: boolean) {
  return {
    live_state_known: true,
    ...(liveVerified === undefined ? {} : { live_verified: liveVerified }),
    worktrees: [
      { name: 'main', is_main: true, is_live: false, is_staged: false, running: false, has_dist: true, behind: 0, last_updated_at: now, path: '/wt/main' },
      { name: 'feature-x', is_main: false, is_live: true, is_staged: false, running: false, has_dist: true, behind: 0, last_updated_at: now, path: '/wt/feature-x' },
    ],
  }
}

function serve(fleet: object) {
  vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(fleet), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
})

describe('DevFleetPage live badge', () => {
  it('shows "live?" and the visible reason when the live row comes from the pointer alone', async () => {
    serve(fleetWith(false))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => screen.getByText('live?'))
    const reason = screen.getByTestId('fleet-live-unverified-reason')
    expect(reason.textContent).toMatch(/can't confirm this checkout is running/)
    expect(screen.queryByText('live')).toBeNull()
  })

  it.each([[true], [undefined]])('shows the plain "live" badge when live_verified is %s', async (verified) => {
    serve(fleetWith(verified))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const badge = await waitFor(() => screen.getByText('live'))
    expect(badge.getAttribute('title')).toMatch(/runs from this checkout/)
    expect(screen.queryByText('live?')).toBeNull()
    expect(screen.queryByTestId('fleet-live-unverified-reason')).toBeNull()
  })
})
