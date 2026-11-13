/**
 * The trust gate names `permissions.workerAutoApprove` before the owner says yes.
 *
 * The grant lets the app's own background sessions approve their tool calls with
 * nobody watching, so it gets its own box outside the three-row ceiling, like
 * `sessionApproval`. An app that does not declare it shows no such box, and the
 * Library builds the gate's target from the manifest's literal `true` only.
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

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
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

describe('TrustAppModal worker auto-approve disclosure', () => {
  it('names the grant in its own box when the app declares it', () => {
    const dialog = renderModal({ ...BASE, workerAutoApprove: true })
    expect(dialog.getByText(`${K}.worker_auto_approve_heading`)).toBeTruthy()
    expect(dialog.getByText(`${K}.worker_auto_approve_desc`)).toBeTruthy()
    // The three-row ceiling is still there, unchanged.
    expect(dialog.getByText(`${K}.capability_note`)).toBeTruthy()
  })

  it('shows no such box for an app that does not declare it', () => {
    const dialog = renderModal(BASE)
    expect(dialog.queryByText(`${K}.worker_auto_approve_heading`)).toBeNull()
    expect(dialog.queryByText(`${K}.worker_auto_approve_desc`)).toBeNull()
  })
})

describe('TrustAppModal user-session reach disclosure', () => {
  it('names each declared verb the gateway knows, and only those', () => {
    const dialog = renderModal({ ...BASE, reachableByUserSessions: ['send', 'close'] })
    expect(dialog.getByText(`${K}.user_session_reach_heading`)).toBeTruthy()
    expect(dialog.getByText(`${K}.user_session_reach_send`)).toBeTruthy()
    expect(dialog.queryByText(`${K}.user_session_reach_read`)).toBeNull()
    expect(dialog.getByText(`${K}.user_session_reach_note`)).toBeTruthy()
  })

  it('shows no such box when nothing it knows is declared', () => {
    const dialog = renderModal({ ...BASE, reachableByUserSessions: ['close'] })
    expect(dialog.queryByText(`${K}.user_session_reach_heading`)).toBeNull()
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

  it('carries the declaration from a registry row, and only a literal true', () => {
    const row = (value: unknown) => ({
      name: 'crew-app', displayName: 'Crew App', description: '', version: '1.0.0', author: '',
      repo: '', origin: 'registry', manifest: { permissions: { workerAutoApprove: value } },
    })
    for (const [value, expected] of [[true, true], ['true', false], [undefined, false]] as const) {
      const { result, unmount } = renderHook(
        () => useAppActions({
          apps: [] as never, browseApps: [row(value)] as never, appsError: null, registryError: null,
          announceAppsChanged: () => {}, sources: [], registriesUnknown: false,
        }),
        { wrapper },
      )
      expect(result.current.trustTarget('crew-app').workerAutoApprove).toBe(expected)
      unmount()
    }
  })
})
