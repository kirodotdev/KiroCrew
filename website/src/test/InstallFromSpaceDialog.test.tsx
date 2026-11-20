/**
 * InstallFromSpaceDialog and the listing page's install hand-off.
 *
 * - Each install refusal code reads as its own copy; an unknown code falls back
 *   to the error's message.
 * - A failed prepare or confirm shows that copy, and a failed confirm leaves
 *   Install off: the staging id is single use.
 * - Closing after prepare discards the staging; a prepare that answers after the
 *   dialog closed discards it too.
 * - The listing page's Back goes to /apps, and a confirmed install closes the
 *   dialog and tells the rest of the dashboard the app list changed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import '../i18n/all'

const api = vi.hoisted(() => ({
  getMarketRegistries: vi.fn(),
  getMarketListing: vi.fn(),
  prepareMarketInstall: vi.fn(),
  confirmMarketInstall: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

import { ApiError } from '../api/apiError'
import InstallFromSpaceDialog, { installErrorKey } from '../components/appstore/InstallFromSpaceDialog'
import { resetMarketAccountsForTests } from '../components/appstore/marketAccount'
import type { MarketListingRow } from '../components/appstore/types'

/** Waits that follow the store-list query and then the listing query: a loaded
 *  coverage shard can pass Testing Library's one-second default on that chain. */
const STORE_CHAIN = { timeout: 5000 }

const OFFER = { id: 'team-store', label: 'Team store', web: 'https://store.example' }
const SIGNED_IN = { state: 'signed_in', account: 'a' }
const STAGING = 'S'.repeat(22)

function row(extra: Partial<MarketListingRow> = {}): MarketListingRow {
  return {
    registry: 'team-store', ref: 'acme/board', name: 'board', displayName: 'Board',
    summary: 'A board', category: 'productivity', tags: [], publisher: 'acme',
    latestVersion: 2, latestSemver: '1.1.0', trust: 'COMMUNITY', official: false, canPublish: false,
    rating: { status: 'absent' }, ratingCount: { status: 'ok', value: 0 }, downloads: { status: 'absent' },
    installed: false, installedVersion: null, nameTaken: false, ...extra,
  }
}

function prepared(components: { kind: string, name: string }[] = []) {
  return {
    ok: true, stagingId: STAGING,
    confirm: {
      app: { ref: 'acme/board', version: 2, appVersion: '', publisher: 'acme', sha256Prefix: 'abcdef012345' },
      components, size: 2048, trustLabel: 'Community',
      permissions: { notice: 'Declared by the publisher.', commands: [], network: [], files: [], background: [], mcpServers: [], agents: [] },
    },
  }
}

function refusal(code: string): ApiError {
  return new ApiError(409, 'refused', JSON.stringify({ ok: false, code, error: 'refused' }))
}

function withClient(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  resetMarketAccountsForTests()
  api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': SIGNED_IN } })
  api.confirmMarketInstall.mockResolvedValue({ ok: true, name: 'board' })
})

describe('installErrorKey', () => {
  it.each([
    ['market_registry_unavailable', 'err_registry_unavailable'],
    ['market_registry_changed', 'err_registry_unavailable'],
    ['market_signed_out', 'err_signed_out'],
    ['market_account_changed', 'err_account_changed'],
    ['market_api_refused', 'err_refused'],
    ['market_api_unreachable', 'err_unreachable'],
    ['market_unavailable', 'err_unreachable'],
  ])('maps %s to its copy', (code, key) => {
    expect(installErrorKey(code)).toBe(`components.appstore.marketStore.${key}`)
  })

  it('has no copy for a code it does not know', () => {
    expect(installErrorKey('something_else')).toBe('')
  })
})

describe('the install dialog', () => {
  it('shows the refusal copy when prepare fails, and offers no Install', async () => {
    api.prepareMarketInstall.mockRejectedValue(refusal('market_signed_out'))
    withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={() => {}} />)
    expect(await screen.findByText('Sign in to Kiro Space first.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
  })

  it("shows the error's own message for a code it does not know", async () => {
    api.prepareMarketInstall.mockRejectedValue(new Error('disk full'))
    withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={() => {}} />)
    expect(await screen.findByText('disk full')).toBeInTheDocument()
  })

  it('lists the bundled parts and shows the version number when there is no app version', async () => {
    api.prepareMarketInstall.mockResolvedValue(prepared([{ kind: 'skill', name: 'notes' }]))
    withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={() => {}} />)
    expect(await screen.findByText('skill: notes')).toBeInTheDocument()
    expect(screen.getByTestId('market-install-confirm')).toHaveTextContent('2')
  })

  it("says what still holds the app back, in the dashboard's own words, not the server's notice", async () => {
    api.prepareMarketInstall.mockResolvedValue(prepared())
    withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={() => {}} />)
    const notice = await screen.findByTestId('market-permissions-notice')
    expect(notice).toHaveTextContent('The app installs switched off and runs nothing until you turn it on.')
    expect(notice.className).not.toContain('text-muted')
    expect(screen.queryByText('Declared by the publisher.')).toBeNull()
  })

  it('shows a failed confirm and keeps Install off, since the staging id is single use', async () => {
    api.prepareMarketInstall.mockResolvedValue(prepared())
    api.confirmMarketInstall.mockRejectedValue(refusal('market_api_refused'))
    const onInstalled = vi.fn()
    withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={onInstalled} />)
    await screen.findByTestId('market-install-confirm')
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    expect(await screen.findByText('Kiro Space refused this install.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
    expect(onInstalled).not.toHaveBeenCalled()
  })

  it('discards the staging when it closes without Install', async () => {
    api.prepareMarketInstall.mockResolvedValue(prepared())
    const view = withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={() => {}} />)
    await screen.findByTestId('market-install-confirm')
    view.unmount()
    expect(api.confirmMarketInstall).toHaveBeenCalledWith(STAGING, false)
  })

  it('discards a staging that prepare answers after the dialog closed', async () => {
    let answer: (v: unknown) => void = () => {}
    api.prepareMarketInstall.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const view = withClient(<InstallFromSpaceDialog row={row()} onClose={() => {}} onInstalled={() => {}} />)
    await waitFor(() => expect(api.prepareMarketInstall).toHaveBeenCalled())
    view.unmount()
    answer(prepared())
    await waitFor(() => expect(api.confirmMarketInstall).toHaveBeenCalledWith(STAGING, false))
  })
})

describe('the listing page', () => {
  function Where() {
    return <div data-testid="where">{useLocation().pathname}</div>
  }

  async function renderPage() {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketListing.mockResolvedValue({
      ok: true, registry: 'team-store', account: 'a', listing: row(), status: SIGNED_IN,
    })
    withClient(
      <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
        <Routes>
          <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByRole('heading', { name: 'Board' }, STORE_CHAIN)
  }

  it('goes back to the App Store from Back', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Back to the App Store/ }))
    expect(screen.getByTestId('where')).toHaveTextContent('/apps')
  })

  it('closes the dialog and says the app list changed after a confirmed install', async () => {
    api.prepareMarketInstall.mockResolvedValue(prepared())
    const changed = vi.fn()
    window.addEventListener('mc:apps-changed', changed)
    try {
      await renderPage()
      fireEvent.click(screen.getByRole('button', { name: 'Install' }))
      await screen.findByTestId('market-install-confirm', {}, STORE_CHAIN)
      fireEvent.click(screen.getAllByRole('button', { name: 'Install' }).at(-1)!)
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), STORE_CHAIN)
      expect(api.confirmMarketInstall).toHaveBeenCalledWith(STAGING, true)
      expect(changed).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc:apps-changed', changed)
    }
  })
})
