/**
 * The page reloads when the Kiro Space account it loaded under changes: a
 * status read that names another account (or a sign-out, or a sign-in), a
 * listing or detail answer read for another account, and its own Sign out.
 * The r7 probes F1, F2, F3 and F5 are here with their assertions flipped.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import '../i18n/all'

const api = vi.hoisted(() => ({
  getMarketRegistries: vi.fn(),
  setMarketRegistries: vi.fn(),
  marketSignIn: vi.fn(),
  marketSignOut: vi.fn(),
  listMarketListings: vi.fn(),
  getMarketListing: vi.fn(),
  prepareMarketInstall: vi.fn(),
  confirmMarketInstall: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

import { newQueryClient } from '../api/queryClient'
import MarketRegistryManager from '../components/appstore/MarketRegistryManager'
import { MarketStoreSections } from '../components/appstore/MarketListingRows'
import {
  MARKET_RELOAD_AT_KEY, MARKET_STATUS_POLL_MS, marketPage, resetMarketAccountsForTests,
} from '../components/appstore/marketAccount'
import MarketListingPage from '../pages/apps/MarketListingPage'

/** These waits sit at the end of a CHAINED load: the store-list query
 *  (`getMarketRegistries`) must answer before the listings query
 *  (`listMarketListings` / `getMarketListing`) is enabled, and only then does
 *  the asserted text render. On a loaded coverage shard that chain can pass
 *  Testing Library's one-second default, so each wait names its own deadline. */
const STORE_CHAIN_TIMEOUT_MS = 5000
const STORE_CHAIN = { timeout: STORE_CHAIN_TIMEOUT_MS }


const OFFER = { id: 'team-store', label: 'Team store', web: 'https://store.example' }
const ALICE = { state: 'signed_in', account: 'alice' }
const BOB = { state: 'signed_in', account: 'bob' }
const OUT = { state: 'needs_sign_in', account: '' }
const UNKNOWN = { state: 'unknown', account: '' }
const regs = (status: object) => ({ offers: [OFFER], added: ['team-store'], status: { 'team-store': status } })
const ROW = {
  registry: 'team-store', ref: 'alice/private-a', name: 'private-a', displayName: 'Private A',
  summary: 'only alice sees this', category: 'productivity', tags: [], publisher: 'alice',
  latestVersion: 2, latestSemver: '1.1.0', trust: 'COMMUNITY', official: false, canPublish: false,
  rating: { status: 'absent' }, ratingCount: { status: 'ok', value: 0 }, downloads: { status: 'absent' },
  installed: false, installedVersion: null, nameTaken: false,
}
const listA = { ok: true, registry: 'team-store', account: 'alice', rows: [ROW], truncated: false, status: ALICE }
const listB = { ok: true, registry: 'team-store', account: 'bob', rows: [{ ...ROW, displayName: 'Private B' }], truncated: false, status: BOB }
const listOut = { ok: true, registry: 'team-store', account: '', rows: [], truncated: false, status: OUT }

let reload: ReturnType<typeof vi.spyOn>

function mount(client = newQueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<><MarketRegistryManager /><MarketStoreSections /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

async function rereadStatus(client: ReturnType<typeof newQueryClient>) {
  await act(async () => { await client.invalidateQueries({ queryKey: ['market-registries'] }) })
}

/** A fixed wall-clock time of the last account-change reload, for the hold tests. */
const RELOADED_AT = Date.UTC(2026, 0, 1)

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  resetMarketAccountsForTests()
  window.sessionStorage.removeItem(MARKET_RELOAD_AT_KEY)
  reload = vi.spyOn(marketPage, 'reload').mockImplementation(() => {})
})
afterEach(() => {
  reload.mockRestore()
  vi.useRealTimers()
})

describe('the page reloads when the store account changes', () => {
  it('does not reload while the status keeps naming the same account, or is unknown', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    await rereadStatus(client)
    api.getMarketRegistries.mockResolvedValue(regs(UNKNOWN))
    await rereadStatus(client)
    expect(reload).not.toHaveBeenCalled()
  })

  it.each([
    ['another account', BOB],
    ['a sign-out', OUT],
  ])('reloads when a status read shows %s', async (_name, status) => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(status))
    await rereadStatus(client)
    await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
  })

  it('reloads when a page loaded signed out sees a sign-in', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(OUT))
    api.listMarketListings.mockResolvedValue(listOut)
    const client = mount()
    expect(await screen.findByRole('button', { name: 'Sign in' }, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    await rereadStatus(client)
    await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
  })

  it('discards a listing answer read for another account and reloads', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listB)
    mount()
    await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
    expect(screen.queryByText('Private B')).toBeNull()
  })

  it('discards a detail answer read for another account and reloads (r7 F3)', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.getMarketListing.mockResolvedValue({ ok: true, registry: 'team-store', account: '', listing: null, status: OUT })
    render(
      <QueryClientProvider client={newQueryClient()}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/alice/private-a']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
    expect(screen.queryByText('Private A')).toBeNull()
  })

  it('reloads as soon as its own Sign out returns, before any late read can show (r7 F1)', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    api.marketSignOut.mockResolvedValue({ ok: true, registry: 'team-store', state: 'signed_out' })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }, STORE_CHAIN))
    await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
    expect(api.marketSignOut).toHaveBeenCalledWith('team-store')
  })

  it('re-reads the status every 30 s while visible and on focus, and reloads on a CLI switch (r7 F2)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.getMarketRegistries.mockResolvedValue(regs(ALICE))
      api.listMarketListings.mockResolvedValue(listA)
      mount()
      expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
      // Focus reads it again at once, stale or not.
      const reads = api.getMarketRegistries.mock.calls.length
      await act(async () => { focusManager.setFocused(false); focusManager.setFocused(true) })
      await waitFor(() => expect(api.getMarketRegistries.mock.calls.length).toBeGreaterThan(reads), STORE_CHAIN)
      const before = api.getMarketRegistries.mock.calls.length
      await act(async () => { vi.advanceTimersByTime(MARKET_STATUS_POLL_MS) })
      await waitFor(() => expect(api.getMarketRegistries.mock.calls.length).toBeGreaterThan(before), STORE_CHAIN)
      expect(reload).not.toHaveBeenCalled()
      // The CLI now names bob: the next poll reloads the page.
      api.getMarketRegistries.mockResolvedValue(regs(BOB))
      await act(async () => { vi.advanceTimersByTime(MARKET_STATUS_POLL_MS) })
      await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
    } finally {
      focusManager.setFocused(undefined)
      vi.useRealTimers()
    }
  })

  it('reloads with an Install dialog open when the status shows another account (r7 F5)', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    api.prepareMarketInstall.mockResolvedValue({
      ok: true, stagingId: 'S'.repeat(22),
      confirm: {
        app: { ref: 'alice/private-a', version: 2, appVersion: '1.1.0', publisher: 'alice', sha256Prefix: 'abcdef012345' },
        components: [], size: 2048, trustLabel: 'Community',
        permissions: { notice: 'n', commands: [], network: [], files: [], background: [], mcpServers: [], agents: [] },
      },
    })
    api.confirmMarketInstall.mockResolvedValue({ ok: true, discarded: true })
    const client = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }, STORE_CHAIN))
    expect(await screen.findByTestId('market-install-confirm', {}, STORE_CHAIN)).toBeInTheDocument()
    // Prepare names the account and the store the page loaded under.
    expect(api.prepareMarketInstall).toHaveBeenCalledWith('alice/private-a', 'alice', 'team-store')
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await rereadStatus(client)
    await waitFor(() => expect(reload).toHaveBeenCalled(), STORE_CHAIN)
    expect(api.confirmMarketInstall).not.toHaveBeenCalledWith('S'.repeat(22), true)
  })

  it('reloads at most once when the status and the listing answers keep disagreeing (r8 G1)', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listOut)
    for (let load = 1; load <= 3; load++) {
      const { unmount } = render(
        <QueryClientProvider client={newQueryClient()}>
          <MemoryRouter initialEntries={['/apps']}>
            <Routes><Route path="/apps" element={<><MarketRegistryManager /><MarketStoreSections /></>} /></Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      )
      if (load === 1) {
        await waitFor(() => expect(reload).toHaveBeenCalledTimes(1), STORE_CHAIN)
      } else {
        // Held: no reload, no rows, the status line.
        expect(await screen.findByTestId('market-store-held', {}, STORE_CHAIN)).toHaveTextContent('alice')
        // The read that held the store threw for the account change: no error
        // notice with its code, and the held line stays.
        await waitFor(() => expect(api.listMarketListings).toHaveBeenCalledTimes(load), STORE_CHAIN)
        await act(async () => { await Promise.resolve() })
        expect(screen.queryByRole('alert')).toBeNull()
        expect(screen.getByTestId('market-store-held')).toBeInTheDocument()
      }
      unmount()
      resetMarketAccountsForTests() // what the real reload does; sessionStorage stays
    }
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('hides the rows and Install while the shared status is unknown (r8 G3)', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(UNKNOWN))
    await rereadStatus(client)
    await waitFor(() => expect(screen.queryByText('Private A')).not.toBeInTheDocument(), STORE_CHAIN)
    expect(screen.queryByRole('button', { name: 'Install' })).not.toBeInTheDocument()
    expect(screen.getByTestId('market-store-held')).toBeInTheDocument()
    expect(reload).not.toHaveBeenCalled()
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    await rereadStatus(client)
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
  })

  it('a held store shows the status the hold read, never the old account (r9 m-3)', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await rereadStatus(client)
    const line = await screen.findByTestId('market-store-held', {}, STORE_CHAIN)
    await waitFor(() => expect(line).toHaveTextContent('bob'), STORE_CHAIN)
    expect(line).not.toHaveTextContent('alice')
    expect(screen.getByTestId('market-registry-manager')).toHaveTextContent('bob')
    expect(screen.getByTestId('market-registry-manager')).not.toHaveTextContent('alice')
    expect(reload).not.toHaveBeenCalled()
  })

  it('a held store shows its rows again when the loaded account comes back', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await rereadStatus(client)
    await screen.findByTestId('market-store-held', {}, STORE_CHAIN)
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    await rereadStatus(client)
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('market-store-held')).toBeNull()
    expect(screen.getByRole('button', { name: 'Install' })).toBeInTheDocument()
    expect(reload).not.toHaveBeenCalled()
  })

  it('a late listing answer for the loaded account does not release a held store', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await rereadStatus(client)
    await screen.findByTestId('market-store-held', {}, STORE_CHAIN)
    // A listing read from before the switch still names alice.
    const calls = api.listMarketListings.mock.calls.length
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh' })) })
    await waitFor(() => expect(api.listMarketListings.mock.calls.length).toBeGreaterThan(calls), STORE_CHAIN)
    await waitFor(() => expect(client.getQueryState(['market-listings', 'team-store'])?.fetchStatus).toBe('idle'), STORE_CHAIN)
    expect(screen.getByTestId('market-store-held')).toHaveTextContent('bob')
    expect(screen.queryByText('Private A')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
  })

  it('a Refresh on a held store keeps the held line, with no account-change error', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await rereadStatus(client)
    await screen.findByTestId('market-store-held', {}, STORE_CHAIN)
    // The page first saw alice, so a listing read for bob holds and throws.
    api.listMarketListings.mockResolvedValue(listB)
    const calls = api.listMarketListings.mock.calls.length
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh' })) })
    await waitFor(() => expect(api.listMarketListings.mock.calls.length).toBeGreaterThan(calls), STORE_CHAIN)
    await waitFor(() => expect(client.getQueryState(['market-listings', 'team-store'])?.fetchStatus).toBe('idle'), STORE_CHAIN)
    expect(screen.queryByText(/market_account_changed/)).toBeNull()
    expect(screen.getByTestId('market-store-held')).toHaveTextContent('bob')
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('the listing page hides a held or unknown store (r9 m-1)', () => {
  const detailA = { ok: true, registry: 'team-store', account: 'alice', listing: ROW, status: ALICE }

  function mountDetail() {
    const client = newQueryClient()
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/alice/private-a']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    return client
  }

  it('hides the listing and Install while the shared status is unknown (r9 P1)', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.getMarketListing.mockResolvedValue(detailA)
    const client = mountDetail()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(UNKNOWN))
    await rereadStatus(client)
    await waitFor(() => expect(screen.queryByText('Private A')).toBeNull(), STORE_CHAIN)
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
    expect(screen.getByTestId('market-store-held')).toBeInTheDocument()
    expect(reload).not.toHaveBeenCalled()
  })

  it('hides the listing and Install when held within 10 s of a reload, naming the new account (r9 P2)', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.getMarketListing.mockResolvedValue(detailA)
    const client = mountDetail()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await rereadStatus(client)
    await waitFor(() => expect(screen.queryByText('Private A')).toBeNull(), STORE_CHAIN)
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
    expect(screen.getByTestId('market-store-held')).toHaveTextContent('bob')
    expect(reload).not.toHaveBeenCalled()
  })

  it('shows the status line, not the raw error, when the held detail read itself is refused', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.getMarketListing.mockResolvedValue({ ...detailA, account: 'bob', listing: { ...ROW, displayName: 'Private B' }, status: BOB })
    mountDetail()
    expect(await screen.findByTestId('market-store-held', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByText('market_account_changed')).toBeNull()
    expect(screen.queryByText('Private B')).toBeNull()
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('review fixes on the store surfaces', () => {
  const OFFER_2 = { id: 'other-store', label: 'Other store', web: '' }
  const twoRegs = (a: object, b: object) => ({
    offers: [OFFER, OFFER_2], added: ['team-store', 'other-store'],
    status: { 'team-store': a, 'other-store': b },
  })
  const listOther = { ...listA, registry: 'other-store', rows: [{ ...ROW, registry: 'other-store', displayName: 'Other A' }] }

  it('holds every store whose account changed in one status read, not only the first', async () => {
    vi.setSystemTime(RELOADED_AT + 1_000)
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(RELOADED_AT))
    api.getMarketRegistries.mockResolvedValue(twoRegs(ALICE, ALICE))
    api.listMarketListings.mockImplementation((id: string) => Promise.resolve(id === 'team-store' ? listA : listOther))
    const client = mount()
    expect(await screen.findByText('Private A', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(await screen.findByText('Other A', {}, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(twoRegs(BOB, BOB))
    await rereadStatus(client)
    await waitFor(() => expect(screen.getAllByTestId('market-store-held')).toHaveLength(2), STORE_CHAIN)
    expect(screen.queryByText('Private A')).toBeNull()
    expect(screen.queryByText('Other A')).toBeNull()
    expect(reload).not.toHaveBeenCalled()
  })

  it('shows the failed store-status read in Sources instead of hiding the block', async () => {
    api.getMarketRegistries.mockRejectedValue(new Error('gateway did not answer'))
    mount(new QueryClient({ defaultOptions: { queries: { retry: false } } }))
    const block = await screen.findByTestId('market-registry-manager', {}, STORE_CHAIN)
    expect(block).toHaveTextContent('gateway did not answer')
  })

  it('keeps a no_access store row to two controls: Sign in and a More actions menu', async () => {
    api.getMarketRegistries.mockResolvedValue(regs({ state: 'no_access', account: '' }))
    api.listMarketListings.mockResolvedValue({ ...listOut, status: { state: 'no_access', account: '' } })
    mount()
    const row = await screen.findByTestId('market-store-team-store', {}, STORE_CHAIN)
    await waitFor(() => expect(row.querySelectorAll('button')).toHaveLength(2), STORE_CHAIN)
    expect(row).toHaveTextContent('Sign in')
    expect(row.querySelector('button[aria-label="More actions"]')).not.toBeNull()
  })

  it('signs in from the store card on the Discover page and shows the code there', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(OUT))
    api.listMarketListings.mockResolvedValue(listOut)
    api.marketSignIn.mockResolvedValue({ ok: true, registry: 'team-store', state: 'pending', code: 'WXYZ', approvalPage: 'https://store.example/approve' })
    mount()
    const card = await screen.findByTestId('market-signin-card', {}, STORE_CHAIN)
    const button = card.querySelector('button')
    expect(button).not.toBeNull()
    fireEvent.click(button!)
    await waitFor(() => expect(api.marketSignIn).toHaveBeenCalledWith('team-store'), STORE_CHAIN)
    await waitFor(() => expect(card).toHaveTextContent('WXYZ'), STORE_CHAIN)
  })
})
