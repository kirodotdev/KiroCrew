// AppPopoutControl is the host-owned pop-out control an app page carries in its
// top-trailing corner, mirroring the chat title bar's: a dimmed icon to pop the
// app out, an accent "Popped out" chip (focus) while it is out, and Return in
// the same spot inside the pop-out window. AppPopoutHost decides WHICH routes
// carry it.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const popouts = vi.hoisted(() => {
  const listeners = new Set<() => void>()
  const state = { set: new Set<string>() }
  return {
    state,
    listeners,
    setPopped(routes: string[]) { state.set = new Set(routes); listeners.forEach(l => l()) },
    openPopout: vi.fn(),
    focusPopout: vi.fn(),
    returnSelfToMain: vi.fn(),
  }
})
vi.mock('../utils/appPopout', () => ({
  subscribe: (l: () => void) => { popouts.listeners.add(l); return () => popouts.listeners.delete(l) },
  getSnapshot: () => popouts.state.set,
  openPopout: popouts.openPopout,
  focusPopout: popouts.focusPopout,
  returnSelfToMain: popouts.returnSelfToMain,
}))
// The host's "is this an app route" answer comes from the builtin registry;
// pin a small one so the test does not depend on which pages the bundle ships.
vi.mock('../apps/builtinRegistry', () => ({
  hasBuiltinComponent: (route: string) => route === '/dev-fleet' || route === '/aws-control',
}))

import AppPopoutControl from '../components/AppPopoutControl'
import AppPopoutHost from '../components/AppPopoutHost'

beforeEach(() => {
  popouts.setPopped([])
  popouts.openPopout.mockClear()
  popouts.focusPopout.mockClear()
  popouts.returnSelfToMain.mockClear()
})

describe('AppPopoutControl', () => {
  it('offers to pop the app out, and opens its window on click', () => {
    renderWithProviders(<AppPopoutControl route="/dev-fleet" />)
    const btn = screen.getByRole('button', { name: 'Pop out app to its own window' })
    fireEvent.click(btn)
    expect(popouts.openPopout).toHaveBeenCalledWith('/dev-fleet', undefined)
    expect(popouts.focusPopout).not.toHaveBeenCalled()
  })

  it('turns into the "Popped out" chip while the app is out, which focuses that window', () => {
    renderWithProviders(<AppPopoutControl route="/dev-fleet" />)
    act(() => popouts.setPopped(['/dev-fleet']))
    expect(screen.queryByRole('button', { name: 'Pop out app to its own window' })).toBeNull()
    const chip = screen.getByRole('button', { name: 'Focus popped-out window' })
    expect(chip).toHaveTextContent('Popped out')
    fireEvent.click(chip)
    expect(popouts.focusPopout).toHaveBeenCalledWith('/dev-fleet')
    expect(popouts.openPopout).not.toHaveBeenCalled()
  })

  it('only reads its OWN route out of the live set', () => {
    renderWithProviders(<AppPopoutControl route="/dev-fleet" />)
    act(() => popouts.setPopped(['/aws-control']))
    expect(screen.getByRole('button', { name: 'Pop out app to its own window' })).toBeInTheDocument()
  })

  it('carries Return inside the pop-out window, never the pop-out icon', () => {
    popouts.setPopped(['/dev-fleet'])
    renderWithProviders(<AppPopoutControl route="/dev-fleet" popout />)
    expect(screen.queryByRole('button', { name: 'Pop out app to its own window' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Focus popped-out window' })).toBeNull()
    const ret = screen.getByRole('button', { name: 'Return to main window' })
    expect(ret).toHaveTextContent('Return')
    fireEvent.click(ret)
    expect(popouts.returnSelfToMain).toHaveBeenCalledTimes(1)
  })

  it('is a zero-height sticky row whose only hit target is the control', () => {
    renderWithProviders(<AppPopoutControl route="/dev-fleet" />)
    const row = screen.getByTestId('app-popout-control')
    expect(row.className).toContain('sticky')
    expect(row.className).toContain('h-0')
    expect(row.className).toContain('pointer-events-none')
    expect(row.className).toContain('focus-caption-reserve')
    expect(row.querySelector('.pointer-events-auto')).not.toBeNull()
  })
})

describe('AppPopoutHost', () => {
  it.each([
    ['/dev-fleet', '/dev-fleet'],
    ['/aws-control/usage', '/aws-control'],
    ['/apps/my-tool', '/apps/my-tool'],
  ])('mounts the control on the app page %s, keyed by its route %s, opening at that page', (path, route) => {
    renderWithProviders(<AppPopoutHost />, { route: path })
    fireEvent.click(screen.getByRole('button', { name: 'Pop out app to its own window' }))
    expect(popouts.openPopout).toHaveBeenCalledWith(route, path)
  })

  it('opens the window at the page AND query the user is on, still keyed by the app route', () => {
    renderWithProviders(<AppPopoutHost />, { route: '/aws-control/usage?tab=x' })
    fireEvent.click(screen.getByRole('button', { name: 'Pop out app to its own window' }))
    expect(popouts.openPopout).toHaveBeenCalledWith('/aws-control', '/aws-control/usage?tab=x')
  })

  it.each(['/chat', '/settings', '/apps', '/apps/library', '/apps/detail/my-tool', '/not-an-app'])(
    'renders nothing on %s',
    (path) => {
      renderWithProviders(<AppPopoutHost />, { route: path })
      expect(screen.queryByTestId('app-popout-control')).toBeNull()
    },
  )
})
