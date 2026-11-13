/**
 * `permissions.webhookSource` is shown wherever the owner decides about an app.
 *
 * The trust gate names it in its own box with the source id a security policy uses
 * (the app's own name), the detail page's permissions card shows the same id, and the
 * Library builds the gate's target from the manifest's literal `true` only. An app
 * that does not declare it shows neither box.
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
import { WebhookSourceBox } from '../pages/AppDetailGrantBoxes'
import { useAppActions } from '../pages/apps/useAppActions'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'crew-hooked',
  displayName: 'Crew Hooked',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/crew-hooked',
  retryInstalls: true,
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('TrustAppModal webhook source disclosure', () => {
  it('names the source id in its own box when the app declares it', () => {
    const dialog = renderModal({ ...BASE, webhookSource: true })
    expect(dialog.getByText(`${K}.webhook_source_heading`)).toBeTruthy()
    expect(dialog.getByText(`${K}.webhook_source_desc crew-hooked`)).toBeTruthy()
  })

  it('shows no such box for an app that does not declare it', () => {
    const dialog = renderModal(BASE)
    expect(dialog.queryByText(`${K}.webhook_source_heading`)).toBeNull()
  })
})

describe('WebhookSourceBox', () => {
  it('shows the source id a policy names', () => {
    render(<WebhookSourceBox source="crew-hooked" />)
    expect(screen.getByTestId('webhook-source')).toHaveTextContent('pages.appDetailPage.webhook_source_desc')
    expect(screen.getByTestId('webhook-source-id')).toHaveTextContent('crew-hooked')
  })
})

describe('useAppActions trust target webhook source', () => {
  function wrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    )
  }

  it('carries the declaration from a registry row and an installed app, only a literal true', () => {
    const manifest = (value: unknown) => ({ permissions: { webhookSource: value } })
    for (const [value, expected] of [[true, true], ['true', false], [undefined, false]] as const) {
      const row = {
        name: 'crew-hooked', displayName: 'Crew Hooked', description: '', version: '1.0.0', author: '',
        repo: '', origin: 'registry', manifest: manifest(value),
      }
      for (const lists of [{ apps: [], browseApps: [row] }, { apps: [row], browseApps: [] }]) {
        const { result, unmount } = renderHook(
          () => useAppActions({
            ...lists, appsError: null, registryError: null,
            announceAppsChanged: () => {}, sources: [], registriesUnknown: false,
          } as never),
          { wrapper },
        )
        expect(result.current.trustTarget('crew-hooked').webhookSource).toBe(expected)
        unmount()
      }
    }
  })
})
