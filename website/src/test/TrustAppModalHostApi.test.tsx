/**
 * The trust gate names the Kiro Crew endpoints an app declares in
 * `permissions.api` before the owner says yes.
 *
 * Only the entries outside the app's own namespace are listed: `/apps/<name>/...`
 * and `/api/apps/<name>/...` are admitted to the app without any declaration, so
 * naming them would ask the owner about nothing. An app that declares no other
 * path shows no such box, and the Library and the detail page build the gate's
 * target from the manifest the row or the install record carries.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, renderHook, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import TrustAppModal, { callsChat, hostApiPaths, type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { trustGrants } from '../components/appstore/trustGrants'
import { useAppActions } from '../pages/apps/useAppActions'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'crew-app',
  displayName: 'Crew App',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/crew-app',
  retryInstalls: true,
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('hostApiPaths', () => {
  it('keeps the entries outside the app namespace, in declared order, once each', () => {
    expect(hostApiPaths('crew-app', [
      '/apps/crew-app/api', '/apps/crew-app/api/*', '/api/apps/crew-app', '/api/apps/crew-app/*',
      '/api/chat', ' /api/source/pull-request ', '/api/chat', '', 7,
    ])).toEqual(['/api/chat', '/api/source/pull-request'])
  })

  it('reads a sibling app or a raw string prefix as outside the namespace', () => {
    expect(hostApiPaths('crew-app', ['/api/apps/crew-app-two', '/apps/crew-appx', '/api/apps/crew-app*']))
      .toEqual(['/api/apps/crew-app-two', '/apps/crew-appx', '/api/apps/crew-app*'])
  })

  it('reads anything but a list as no entries', () => {
    expect(hostApiPaths('crew-app', '/api/chat')).toEqual([])
    expect(hostApiPaths('crew-app', undefined)).toEqual([])
  })
})

describe('callsChat', () => {
  it('reads /api/chat and the paths below it, plain or exact, as chat', () => {
    expect(callsChat(['/api/chat'])).toBe(true)
    expect(callsChat(['/api/chat/slots/*'])).toBe(true)
    expect(callsChat(['=/api/chat'])).toBe(true)
  })

  it('reads a look-alike or another path as not chat', () => {
    expect(callsChat(['/api/chatx', '/api/source/pull-request', '/api/*'])).toBe(false)
    expect(callsChat([])).toBe(false)
  })
})

describe('TrustAppModal host endpoint disclosure', () => {
  it('lists each declared host path in its own box', () => {
    const dialog = renderModal({ ...BASE, api: ['/apps/crew-app/api/*', '/api/chat'] })
    const box = within(dialog.getByTestId('trust-app-host-api'))
    expect(box.getByText(`${K}.host_api_heading`)).toBeTruthy()
    // Says in plain words what the chat path lets the app do, before the paths.
    expect(box.getByText(`${K}.host_api_chat`)).toBeTruthy()
    expect(box.getByText(`${K}.host_api_paths`)).toBeTruthy()
    expect(box.getByText('/api/chat')).toBeTruthy()
    expect(box.queryByText('/apps/crew-app/api/*')).toBeNull()
    // The three-row ceiling is still there, unchanged.
    expect(dialog.getByText(`${K}.capability_note`)).toBeTruthy()
  })

  it('leaves the chat sentence out when no listed path is a chat path', () => {
    const dialog = renderModal({ ...BASE, api: ['/api/source/pull-request'] })
    const box = within(dialog.getByTestId('trust-app-host-api'))
    expect(box.getByText('/api/source/pull-request')).toBeTruthy()
    expect(box.queryByText(`${K}.host_api_chat`)).toBeNull()
  })

  it('shows no such box when every entry is in the app namespace', () => {
    const dialog = renderModal({ ...BASE, api: ['/apps/crew-app/api', '/api/apps/crew-app/*'] })
    expect(dialog.queryByTestId('trust-app-host-api')).toBeNull()
    expect(dialog.queryByText(`${K}.host_api_heading`)).toBeNull()
  })
})

describe('trustGrants', () => {
  // Every entry point that opens the gate (Library, detail page, local-source
  // switch) spreads this helper, so the declared paths reach each of them.
  it('carries the declared paths as the manifest lists them', () => {
    expect(trustGrants({ api: ['/api/chat'] }).api).toEqual(['/api/chat'])
    expect(trustGrants(undefined).api).toBeUndefined()
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

  function target(apps: unknown[], browseApps: unknown[]) {
    const { result, unmount } = renderHook(
      () => useAppActions({
        apps: apps as never, browseApps: browseApps as never, appsError: null, registryError: null,
        announceAppsChanged: () => {}, sources: [], registriesUnknown: false,
      }),
      { wrapper },
    )
    const value = result.current.trustTarget('crew-app')
    unmount()
    return value
  }

  it('carries the declared paths from a registry row', () => {
    const row = {
      name: 'crew-app', displayName: 'Crew App', description: '', version: '1.0.0', author: '',
      repo: '', origin: 'registry', manifest: { permissions: { api: ['/api/chat'] } },
    }
    expect(target([], [row]).api).toEqual(['/api/chat'])
  })

  it('carries the declared paths from an installed record', () => {
    const installed = {
      name: 'crew-app', displayName: 'Crew App', origin: 'registry',
      manifest: { permissions: { api: ['/api/source/pull-request'] } },
    }
    expect(target([installed], []).api).toEqual(['/api/source/pull-request'])
  })
})
