/**
 * DevFleetPage: the Release worktree action in the toolbar's More actions menu.
 *
 * Prune merged and the More actions trigger are the toolbar's two controls
 * (max-two-buttons-per-row counts the trigger); Refresh and Release worktree are
 * items in that labelled menu. The item's
 * accessible name is its visible label with the longer hint as its description
 * (WCAG 2.5.3), the route's worded result reaches the page, and the trigger shows
 * the action is still running after the menu closes.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from './DevFleetPage'

const FLEET = {
  worktrees: [{ name: 'main', is_main: true, running: false, has_dist: true, behind: 0 }],
}

/** Serve the fleet, and answer the create route with `create`. */
function mockFleet(create: () => Promise<Response>) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (u.includes('/create-release-worktree')) {
      expect(init?.method).toBe('POST')
      return create()
    }
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
}

/** Open the real Radix menu the way a mouse does: Radix opens on pointerdown. */
function pointerOpen(trigger: HTMLElement) {
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
}

async function openMore() {
  const trigger = await screen.findByTestId('fleet-toolbar-more')
  pointerOpen(trigger)
  return trigger
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
})

describe('DevFleetPage Release worktree', () => {
  it('keeps Prune merged as the one button and puts Refresh and Release worktree in the menu', async () => {
    mockFleet(() => new Promise<Response>(() => {}))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const trigger = await screen.findByTestId('fleet-toolbar-more')
    expect(trigger).toHaveAccessibleName('More actions')
    expect(screen.getByRole('button', { name: 'Prune merged' })).toBeInTheDocument()
    // Neither is a toolbar button, and neither is on screen until the menu opens.
    expect(screen.queryByRole('button', { name: /^Refresh/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Release worktree' })).toBeNull()
    expect(screen.queryByText('Release worktree')).toBeNull()

    pointerOpen(trigger)
    const item = await screen.findByRole('menuitem', { name: 'Release worktree' })
    expect(item).toHaveAttribute('title', 'Create a worktree at the newest stable release')
    expect(screen.getByRole('menuitem', { name: 'Refresh' })).toHaveAttribute('title', 'Refresh fleet')
  })

  it('creates the worktree and names it and its version', async () => {
    const fetchSpy = mockFleet(() => Promise.resolve(new Response(JSON.stringify({
      ok: true, path: '/wt/release-channel-stable', name: 'release-channel-stable',
      version: '0.10.0', ref: 'refs/tags/v0.10.0', oid: 'a'.repeat(40),
    }), { status: 200 })))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Release worktree' }))
    expect(await screen.findByText('Created release-channel-stable at 0.10.0')).toBeInTheDocument()
    expect(fetchSpy.mock.calls.some(([u]) => String(typeof u === 'string' ? u : (u as Request).url).includes('/create-release-worktree'))).toBe(true)
  })

  it('shows the route’s worded refusal', async () => {
    mockFleet(() => Promise.resolve(new Response(JSON.stringify({
      ok: false, code: 'unpublished', error: 'the remote advertises no stable release tag',
    }), { status: 409 })))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Release worktree' }))
    expect(await screen.findByText(/the remote advertises no stable release tag/)).toBeInTheDocument()
  })

  it('marks the trigger busy and disables the reopened item while the action runs', async () => {
    mockFleet(() => new Promise<Response>(() => {}))
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const trigger = await openMore()
    expect(trigger).toHaveAttribute('aria-busy', 'false')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Release worktree' }))
    await waitFor(() => expect(trigger).toHaveAttribute('aria-busy', 'true'))
    // The menu closed on select; reopened, the item names the running action.
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    pointerOpen(trigger)
    const busyItem = await screen.findByRole('menuitem', { name: 'Creating…' })
    expect(busyItem).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('menuitem', { name: 'Release worktree' })).toBeNull()
  })
})
