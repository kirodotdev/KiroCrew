/**
 * Dev Fleet "Restore installed build".
 *
 * While the live-target pointer names a checkout (`live_target_pinned`), the page
 * shows a persistent control (no dismiss) that asks for confirmation and then posts
 * to the GATEWAY route `/api/apps/dev-fleet/restore-installed`. It is hidden when
 * nothing is pinned, and a staged-only answer (no drivable service) surfaces the
 * notice without entering the restart overlay.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from './DevFleetPage'

const now = 1_760_000_000

function fleetWith(extra: object) {
  return {
    live_state_known: true,
    manual_restart: 'kirocrew restart',
    worktrees: [
      { name: 'main', is_main: true, is_live: false, is_staged: false, running: false, has_dist: true, behind: 0, last_updated_at: now, path: '/wt/main' },
      { name: 'feature-x', is_main: false, is_live: true, is_staged: false, running: false, has_dist: true, behind: 0, last_updated_at: now, path: '/wt/feature-x' },
    ],
    ...extra,
  }
}

function serve(fleet: object, restore?: object) {
  const posts: { url: string; body: string }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((url, opts) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (u.includes('/restore-installed') && opts?.method === 'POST') {
      // The page asks for a dry-run plan first, so its confirm text matches the restart.
      if (String(opts.body).includes('dry_run')) {
        const restart = (fleet as { gateway_service_active?: boolean }).gateway_service_active ? 'service' : 'manual'
        return Promise.resolve(new Response(JSON.stringify({ ok: true, dry_run: true, plan: { restart } }), { status: 200 }))
      }
      posts.push({ url: u, body: String(opts.body) })
      return Promise.resolve(new Response(JSON.stringify(restore ?? {}), { status: 200 }))
    }
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(fleet), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
  return posts
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
})

describe('DevFleetPage Restore installed build', () => {
  it('is hidden while nothing is pinned', async () => {
    serve(fleetWith({ live_target_pinned: false }))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => screen.getByText('feature-x'))
    expect(screen.queryByTestId('restore-installed-build')).toBeNull()
  })

  it('is persistent while pinned, confirms, and posts to the gateway route', async () => {
    const posts = serve(
      fleetWith({ live_target_pinned: true, gateway_service_active: false }),
      { ok: true, restored: true, staged_only: true, notice: 'The live target is cleared.' },
    )
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const note = await screen.findByTestId('restore-installed-build')
    expect(note).toHaveTextContent('not the build installed on this machine')
    // Persistent: no dismiss control, unlike the Switch back banner.
    expect(within(note).queryByLabelText('Dismiss')).toBeNull()

    fireEvent.click(within(note).getByRole('button', { name: 'Restore installed build' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/open a terminal \(not one inside a worktree folder\)/)).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Restore installed build' }))
    })

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toBe('/api/apps/dev-fleet/restore-installed')
    expect(JSON.parse(posts[0].body)).toEqual({})
    expect(screen.queryByText('Restarting — reconnecting…')).toBeNull()
    // The instruction outlives the toast until dismissed.
    const pending = await screen.findByTestId('restore-installed-pending')
    expect(pending).toHaveTextContent('The live target is cleared.')
    fireEvent.click(within(pending).getByLabelText('Dismiss'))
    expect(screen.queryByTestId('restore-installed-pending')).toBeNull()
  })

  it('picks the confirm text from the server plan, not from the service flag', async () => {
    // A foreground gateway reports gateway_service_active, but the plan says manual.
    vi.spyOn(globalThis, 'fetch').mockImplementation((url, opts) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/restore-installed') && opts?.method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, dry_run: true, plan: { restart: 'manual' } }), { status: 200 }))
      }
      if (u.includes('/fleet')) {
        return Promise.resolve(new Response(JSON.stringify(fleetWith({ live_target_pinned: true, gateway_service_active: true })), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const note = await screen.findByTestId('restore-installed-build')
    fireEvent.click(within(note).getByRole('button', { name: 'Restore installed build' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/open a terminal \(not one inside a worktree folder\)/)).toBeInTheDocument()
  })

  it('stays available when worktree discovery fails but the gateway is pinned', async () => {
    serve({ worktrees: [], error: 'no git', live_target_pinned: true })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const note = await screen.findByTestId('restore-installed-build')
    expect(within(note).getByRole('button', { name: 'Restore installed build' })).toBeEnabled()
  })

  it('posts nothing when the confirm is cancelled', async () => {
    const posts = serve(fleetWith({ live_target_pinned: true, gateway_service_active: true }))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const note = await screen.findByTestId('restore-installed-build')
    fireEvent.click(within(note).getByRole('button', { name: 'Restore installed build' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/restarts the dashboard on the build installed/)).toBeInTheDocument()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' })) })
    expect(posts).toHaveLength(0)
  })

  it('shows the refusal from the gateway in the persistent error notice', async () => {
    serve(
      fleetWith({ live_target_pinned: true, gateway_service_active: true }),
      { ok: false, code: 'restore_no_installer_record', error: 'no record of the installed build' },
    )
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const note = await screen.findByTestId('restore-installed-build')
    fireEvent.click(within(note).getByRole('button', { name: 'Restore installed build' }))
    const dialog = await screen.findByRole('dialog')
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Restore installed build' }))
    })
    const err = await screen.findByTestId('gateway-restart-error')
    expect(err).toHaveTextContent('no record of the installed build')
  })
})
