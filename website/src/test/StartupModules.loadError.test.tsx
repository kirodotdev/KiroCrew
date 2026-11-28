import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import StartupModules from '../components/StartupModules'
import type { AppHostProps } from '../components/AppHost'
import type { InstalledApp } from '../components/appstore/types'
import { renderWithProviders } from './helpers'

vi.mock('../components/AppHost', () => ({
  default: ({ app }: AppHostProps) => <span>{app.name} started</span>,
}))
vi.mock('../lib/safeReload', () => ({ isSafeReload: () => false }))
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: () => false }))

const startupApp = {
  name: 'ghost',
  version: '1.0.0',
  enabled: true,
  startup: { state: 'admitted', entry: 'startup.mjs', reason: '' },
} as InstalledApp

afterEach(() => { vi.restoreAllMocks() })

describe('StartupModules app-list loading', () => {
  it('shows one load notice after retries fail and starts modules when Retry succeeds', async () => {
    const listApps = vi.spyOn(api, 'listApps').mockRejectedValue(new Error('apps unavailable'))
    renderWithProviders(<StartupModules />, { queryDefaults: { retry: 1, retryDelay: 0 } })

    const notice = await screen.findByTestId('startup-modules-load-error')
    expect(notice).toBeVisible()
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent('Startup modules could not load.')
    expect(screen.getAllByRole('status')).toHaveLength(1)

    listApps.mockResolvedValue([startupApp])
    fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('ghost started')).toBeVisible()
    expect(screen.queryByTestId('startup-module-errors')).not.toBeInTheDocument()
  })

  it('renders nothing after a successful empty list', async () => {
    vi.spyOn(api, 'listApps').mockResolvedValue([])
    const { container, queryClient } = renderWithProviders(<StartupModules />)

    await waitFor(() => expect(queryClient.getQueryState(['apps'])?.status).toBe('success'))
    expect(container).toBeEmptyDOMElement()
  })

  it('keeps cached modules without a load notice when a background refetch fails', async () => {
    const listApps = vi.spyOn(api, 'listApps').mockResolvedValue([startupApp])
    const { queryClient } = renderWithProviders(<StartupModules />)
    expect(await screen.findByText('ghost started')).toBeVisible()

    listApps.mockRejectedValue(new Error('apps unavailable'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['apps'] }) })

    expect(screen.getByText('ghost started')).toBeVisible()
    expect(screen.queryByTestId('startup-module-errors')).not.toBeInTheDocument()
  })
})
