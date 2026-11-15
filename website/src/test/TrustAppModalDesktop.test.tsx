/**
 * The trust gate names the desktop windows an app's manifest declares before the
 * owner says yes.
 *
 * The desktop shell runs those windows over every other program while the app is
 * on, so the shapes it asked for (an overlay on every display, a hidden
 * background page, shortcuts) are listed with the grants. An app with no
 * `desktop` block shows no such box, and the Library builds the gate's target
 * from the manifest itself.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, renderHook, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string) => key,
}))

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { desktopRequestOf } from '../components/appstore/desktopRequest'
import { useAppActions } from '../pages/apps/useAppActions'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'desk-app',
  displayName: 'Desk App',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/desk-app',
  retryInstalls: true,
}

const COMPANION_DESKTOP = {
  windows: [
    { id: 'pet', entry: 'pet.mjs', kind: 'overlay', displays: 'all', openOnEnable: true },
    { id: 'brain', entry: 'brain.mjs', kind: 'background', openOnEnable: true },
    { id: 'panel', entry: 'panel.mjs', kind: 'panel' },
  ],
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('desktopRequestOf', () => {
  it('reads each declared shape, and nothing from a manifest without windows', () => {
    expect(desktopRequestOf({ desktop: COMPANION_DESKTOP })).toEqual({
      overlay: false, spansDisplays: true, panel: true, background: true, shortcuts: false,
    })
    expect(desktopRequestOf({
      desktop: {
        windows: [{ id: 'o', entry: 'o.mjs', kind: 'overlay' }],
        shortcuts: [{ id: 's', accelerator: 'Ctrl+Alt+K', window: 'o' }],
      },
    })).toEqual({ overlay: true, spansDisplays: false, panel: false, background: false, shortcuts: true })
    expect(desktopRequestOf({})).toBeUndefined()
    expect(desktopRequestOf({ desktop: { windows: [] } })).toBeUndefined()
    expect(desktopRequestOf(null)).toBeUndefined()
  })
})

describe('TrustAppModal desktop disclosure', () => {
  it('lists the declared shapes in their own box, and only those', () => {
    const dialog = renderModal({ ...BASE, desktop: desktopRequestOf({ desktop: COMPANION_DESKTOP }) })
    const box = within(dialog.getByTestId('trust-desktop-box'))
    expect(box.getByText(`${K}.desktop_heading`)).toBeTruthy()
    expect(box.getByText(`${K}.desktop_spans_displays`)).toBeTruthy()
    expect(box.getByText(`${K}.desktop_background`)).toBeTruthy()
    expect(box.getByText(`${K}.desktop_panel`)).toBeTruthy()
    expect(box.queryByText(`${K}.desktop_overlay`)).toBeNull()
    expect(box.queryByText(`${K}.desktop_shortcuts`)).toBeNull()
  })

  it('shows no such box for an app that declares no desktop window', () => {
    const dialog = renderModal(BASE)
    expect(dialog.queryByTestId('trust-desktop-box')).toBeNull()
  })
})

describe('useAppActions trust target', () => {
  function wrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    )
  }

  it('carries the desktop request from a registry row', () => {
    const row = {
      name: 'desk-app', displayName: 'Desk App', description: '', version: '1.0.0', author: '',
      repo: '', origin: 'registry', manifest: { desktop: COMPANION_DESKTOP },
    }
    const { result } = renderHook(
      () => useAppActions({
        apps: [] as never, browseApps: [row] as never, appsError: null, registryError: null,
        announceAppsChanged: () => {}, sources: [], registriesUnknown: false,
      }),
      { wrapper },
    )
    expect(result.current.trustTarget('desk-app').desktop?.spansDisplays).toBe(true)
  })
})
