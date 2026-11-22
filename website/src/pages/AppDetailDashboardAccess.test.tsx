/**
 * The "Dashboard access" card: hidden when there is nothing to say, a confirmed grant,
 * a restart offered after the grant, the "needs dashboard access" state for an app whose
 * last token request was refused, and the platform reason in place of the control.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getAppDashboardAccess: vi.fn(),
  grantAppDashboardAccess: vi.fn(),
  restartAppForDashboardAccess: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

const confirmMock = vi.hoisted(() => vi.fn())
vi.mock('../components/ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: confirmMock, cancel: vi.fn(), confirmDialog: null, confirmOpen: false }),
}))

import AppDetailDashboardAccess from './AppDetailDashboardAccess'

const OFFER = {
  app: 'tunnel',
  registry: 'crew-tunnels',
  source: 'https://example.invalid/tunnel.git',
  version: '0.9.4',
  text:
    'Allow tunnel 0.9.4 (registry crew-tunnels) to use this dashboard on your behalf? ' +
    'The app can show the dashboard page with your theme, preferences and chat list, and stop a running chat turn. ' +
    'It can use its own backend. ' +
    'It cannot read chat messages, send them, change settings, credentials, apps, the terminal or approvals.',
}
const OFFERED = { granted: false, offer: OFFER, platform_supported: true, needs_access: false }

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.clearAllMocks()
  confirmMock.mockResolvedValue(true)
  api.getAppDashboardAccess.mockResolvedValue(OFFERED)
  api.grantAppDashboardAccess.mockResolvedValue({ ...OFFERED, granted: true, offer: OFFER, restart_required: true })
  api.restartAppForDashboardAccess.mockResolvedValue({ ...OFFERED, granted: true, restarted: true })
})

describe('AppDetailDashboardAccess', () => {
  it('renders nothing when no grant is offered, held or missed', async () => {
    api.getAppDashboardAccess.mockResolvedValue({ granted: false, offer: null, platform_supported: true, needs_access: false })
    const { container } = render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    await waitFor(() => expect(api.getAppDashboardAccess).toHaveBeenCalledWith('tunnel'))
    expect(container.textContent).toBe('')
  })

  it('confirms with the offer text, grants, then offers a restart', async () => {
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    fireEvent.click(await screen.findByRole('button', { name: 'Allow dashboard access' }))
    await waitFor(() => expect(api.grantAppDashboardAccess).toHaveBeenCalledWith('tunnel'))
    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ body: OFFER.text }))
    expect(confirmMock.mock.calls[0][0].body).toContain('stop a running chat turn')
    expect(confirmMock.mock.calls[0][0].body).toContain('cannot read chat messages, send them, change settings, credentials, apps, the terminal or approvals')
    expect(confirmMock.mock.calls[0][0].body).not.toContain('run agents as you')
    expect(await screen.findByText('Dashboard access is on for this app.')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Restart app' }))
    await waitFor(() => expect(api.restartAppForDashboardAccess).toHaveBeenCalledWith('tunnel'))
    expect(await screen.findByRole('status')).toHaveTextContent('App restarted.')
  })

  it('says on the card what the access can do and that turning the app off removes it', async () => {
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    const scope = await screen.findByTestId('dashboard-access-scope')
    expect(scope).toHaveTextContent('show the dashboard page with your theme, preferences and chat list, and stop a running chat turn')
    expect(scope).toHaveTextContent('cannot read chat messages, send them, change settings, credentials, apps, the terminal or approvals')
    expect(scope).not.toHaveTextContent('run agents as you')
    expect(scope).toHaveTextContent('Turning the app off removes this access.')
  })

  it('shows the same scope line while the access is on', async () => {
    api.getAppDashboardAccess.mockResolvedValue({ ...OFFERED, granted: true })
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    expect(await screen.findByTestId('dashboard-access-scope')).toHaveTextContent('Turning the app off removes this access.')
  })

  it('renders a failed load, with the agent hand-off, instead of nothing', async () => {
    api.getAppDashboardAccess.mockRejectedValue(new Error('boom'))
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    expect(await screen.findByTestId('dashboard-access-load-failed')).toHaveTextContent(
      'Could not load the dashboard access for this app.',
    )
    expect(screen.queryByRole('button', { name: 'Allow dashboard access' })).toBeNull()
  })

  it('grants nothing when the owner declines the confirm', async () => {
    confirmMock.mockResolvedValue(false)
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    fireEvent.click(await screen.findByRole('button', { name: 'Allow dashboard access' }))
    await waitFor(() => expect(confirmMock).toHaveBeenCalled())
    expect(api.grantAppDashboardAccess).not.toHaveBeenCalled()
  })

  it('shows the needs-access state for an app whose last mint was refused', async () => {
    api.getAppDashboardAccess.mockResolvedValue({ ...OFFERED, needs_access: true })
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    expect(await screen.findByTestId('dashboard-access-needed')).toHaveTextContent('asked for dashboard access and has none')
    expect(screen.getByRole('button', { name: 'Allow dashboard access' })).toBeEnabled()
  })

  it('names the platform reason, with no control, where the sandbox cannot bind the access', async () => {
    api.getAppDashboardAccess.mockResolvedValue({ granted: false, offer: null, platform_supported: false, needs_access: true })
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    expect(await screen.findByTestId('dashboard-access-unsupported')).toHaveTextContent('cannot be given on this computer')
    expect(screen.queryByRole('button', { name: 'Allow dashboard access' })).toBeNull()
    expect(screen.queryByTestId('dashboard-access-needed')).toBeNull()
  })

  it('reports a restart that did not bring the backend back', async () => {
    api.restartAppForDashboardAccess.mockResolvedValue({ ...OFFERED, granted: true, restarted: false })
    render(wrap(<AppDetailDashboardAccess appName="tunnel" />))
    fireEvent.click(await screen.findByRole('button', { name: 'Allow dashboard access' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Restart app' }))
    expect(await screen.findByText(/did not start again/)).toBeInTheDocument()
  })
})
