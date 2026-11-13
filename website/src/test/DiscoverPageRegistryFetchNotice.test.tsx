/**
 * The store says which registry it could not fetch, and what fixes it.
 *
 * A registry whose index clone fails adds no rows to `GET /api/apps/registry`,
 * which still answers 200, so without this notice the store just shows fewer
 * apps and the source reads a count of 0. The listing carries `registryStatus`
 * for each such registry; the page renders one notice naming the remedy for its
 * cause (VPN for a host that did not resolve, known_hosts for an untrusted host
 * key, the credentials for a refused sign-in) and whether its apps are missing
 * or last synced. The real `useAppsData` runs here, so the listing's field is
 * read through the same normalization the page uses.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const refreshRegistries = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    refreshAppStore: vi.fn().mockResolvedValue({ ok: true }),
    refreshRegistries: (...a: unknown[]) => refreshRegistries(...a),
    updateRegistries: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    uninstallApp: vi.fn(),
    uninstallPreview: vi.fn().mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } }),
    installApp: vi.fn(),
    openApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="app-icon" />,
}))

import DiscoverPage from '../pages/apps/DiscoverPage'

const NOTICE = 'registry-fetch-notice'

function renderPage(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<DiscoverPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function listing(registryStatus: unknown[]) {
  return { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' }, registryStatus }
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  listApps.mockResolvedValue([])
  listRegistries.mockResolvedValue({ registries: [] })
})

describe('the registry fetch notice', () => {
  it('names the host and the VPN remedy when the registry host did not resolve', async () => {
    listRegistry.mockResolvedValue(listing([
      { name: 'internal-community', ok: false, cause: 'dns', host: 'git.example.com', cached: false },
    ]))
    renderPage()

    const notice = await screen.findByTestId(NOTICE)

    expect(notice).toHaveTextContent(
      'git.example.com could not be found for internal-community. '
      + 'Connect to your VPN or check your network, then try again.',
    )
    expect(notice).toHaveTextContent('Apps from internal-community are missing from the store.')
  })

  it('names known_hosts for an untrusted host key, and says the apps are last synced', async () => {
    listRegistry.mockResolvedValue(listing([
      { name: 'team', ok: false, cause: 'host_key', host: 'git.example.com', cached: true },
    ]))
    renderPage()

    const notice = await screen.findByTestId(NOTICE)

    expect(notice).toHaveTextContent('~/.ssh/known_hosts')
    expect(notice).toHaveTextContent('Run ssh git.example.com once in a terminal')
    expect(notice).toHaveTextContent('still showing the last-synced apps for these.')
  })

  it('names the credentials for a refused sign-in', async () => {
    listRegistry.mockResolvedValue(listing([
      { name: 'team', ok: false, cause: 'auth', host: 'git.example.com', cached: false },
    ]))
    renderPage()

    expect(await screen.findByTestId(NOTICE)).toHaveTextContent(
      'Renew the git credentials this machine uses for git.example.com',
    )
  })

  it('shows no notice when every registry fetched', async () => {
    listRegistry.mockResolvedValue(listing([]))
    renderPage()

    await waitFor(() => expect(listRegistry).toHaveBeenCalledTimes(1))
    await screen.findByRole('button', { name: 'Refresh the store' })
    expect(screen.queryByTestId(NOTICE)).toBeNull()
  })

  it('stays dismissed while the failure it reported stands', async () => {
    listRegistry.mockResolvedValue(listing([
      { name: 'internal-community', ok: false, cause: 'dns', host: 'git.example.com', cached: false },
    ]))
    renderPage()
    const notice = await screen.findByTestId(NOTICE)

    await userEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))

    await waitFor(() => expect(screen.queryByTestId(NOTICE)).toBeNull())
  })

  describe('beside the refresh report of this page', () => {
    const authFailure = { name: 'team', ok: false, cause: 'auth', host: 'git.example.com', cached: false }
    const SIGN_IN_REFUSED = 'Sign-in to git.example.com was refused for team.'

    it('stays on screen beside the report a failed refresh writes', async () => {
      refreshRegistries.mockResolvedValue({
        ok: false,
        failed: ['team'],
        results: [{ name: 'team', ok: false, reason: 'auth', host: 'git.example.com', cached: false }],
      })
      listRegistry.mockResolvedValue(listing([authFailure]))
      renderPage()
      await screen.findByTestId(NOTICE)
      const calls = listRegistry.mock.calls.length

      await userEvent.click(screen.getByRole('button', { name: 'Refresh the store' }))

      await waitFor(() => expect(listRegistry.mock.calls.length).toBeGreaterThan(calls))
      await waitFor(() => expect(screen.getAllByText(SIGN_IN_REFUSED, { exact: false })).toHaveLength(2))
      expect(screen.getByTestId(NOTICE)).toHaveTextContent(SIGN_IN_REFUSED)
    })

    it('still names the registry when a refresh request itself fails', async () => {
      refreshRegistries.mockRejectedValue(new Error('Failed to fetch'))
      listRegistry.mockResolvedValue(listing([
        { name: 'team', ok: false, cause: 'dns', host: 'git.example.com', cached: false },
      ]))
      renderPage()
      await screen.findByTestId(NOTICE)
      const calls = listRegistry.mock.calls.length

      await userEvent.click(screen.getByRole('button', { name: 'Refresh the store' }))

      await waitFor(() => expect(listRegistry.mock.calls.length).toBeGreaterThan(calls))
      expect(await screen.findByText('Failed to fetch')).toBeInTheDocument()
      expect(screen.getByTestId(NOTICE)).toHaveTextContent(
        'git.example.com could not be found for team. '
        + 'Connect to your VPN or check your network, then try again.',
      )
    })
  })
})
