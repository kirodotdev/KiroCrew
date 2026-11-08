import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

// The panel reads its instance list from this client. When management is on but
// the gateway has NOT been restarted yet, the list reports `active: false`: the
// "not active yet" banner shows and connecting cannot work until the restart.
// The Connect button must say so, not look fully clickable (issue #23692).
vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    isAuthExpiredError: (e: unknown) =>
      e instanceof ApiError && (e as { authRequired?: boolean }).authRequired === true,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      updateInstance: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudIdentity: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
      restartGateway: vi.fn(),
    },
  }
})
import { api, ApiError } from '../api/client'

// One hand-added, disconnected crew — the cheapest row that shows a Connect
// button. Its connect state is the only thing under test here.
const MANUAL_INSTANCE = {
  id: 'm1',
  name: 'dev-box-1',
  connection_method: 'ssh' as const,
  ssm_target: '',
  ssh_host: 'dev-box-1',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'm1', state: 'disconnected' as const },
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' },
    suggested_target: { license: '', start_url: '', region: '' },
  })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
})

describe('RemoteCrewPanel — Connect while the gateway needs a restart', () => {
  it('disables Connect and says why when management is on but not active yet', async () => {
    // active:false is exactly the state that shows the "not active yet" banner.
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    const connect = screen.getByRole('button', { name: /^Connect$/i })
    // The two signals must agree: the banner says "not yet", so the button must
    // not look fully clickable. A reason is attached so the muting is explained.
    expect(connect).toBeDisabled()
    expect(connect).toHaveAttribute('title', expect.stringMatching(/restart/i))
    // A `title` on a disabled button is not keyboard- or screen-reader-reachable,
    // so the button also points at the restart banner via aria-describedby — and
    // that banner must actually be present on the page for the reference to
    // resolve to something a screen reader can read.
    const bannerId = connect.getAttribute('aria-describedby')
    expect(bannerId).toBeTruthy()
    expect(document.getElementById(bannerId as string)).toHaveTextContent(/restart/i)
  })

  it('leaves Connect enabled once the gateway is active', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    const connect = screen.getByRole('button', { name: /^Connect$/i })
    expect(connect).toBeEnabled()
    // No banner to describe once active, so the reference is dropped rather than
    // left dangling at an element that is no longer on the page.
    expect(connect).not.toHaveAttribute('aria-describedby')
  })
})

// Turning management on asks for a gateway restart (issue #32097). The banner
// offers it in place, and clears by itself once the restarted gateway is back.
describe('RemoteCrewPanel — the not-active-yet banner', () => {
  it('restarts the gateway from the banner, behind the shared two-step confirm', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockResolvedValue({ ok: true, status: 'restarting' })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    expect(api.restartGateway).not.toHaveBeenCalled()
    await u.click(restart)
    await waitFor(() => expect(restart).toHaveTextContent(/Restarting/))
    expect(api.restartGateway).toHaveBeenCalledTimes(1)
    expect(restart).toBeDisabled()
  })

  it('shows a refused restart and leaves the button ready for another press', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockRejectedValue(new ApiError(409, 'The restart launcher cannot run here.'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    await u.click(restart)
    expect(await screen.findByTestId('remote-crew-restart-failed')).toHaveTextContent('The restart launcher cannot run here.')
    expect(restart).toBeEnabled()
    expect(restart).not.toHaveTextContent(/Restarting/)
  })

  it('shows a restart request that never reached the gateway, and leaves the button ready', async () => {
    // The gateway answers before it restarts, so a fetch that rejects means the
    // restart was never accepted, not that it is under way.
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockRejectedValue(new TypeError('Failed to fetch'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    await u.click(restart)
    expect(await screen.findByTestId('remote-crew-restart-failed')).toHaveTextContent('Restart failed')
    expect(restart).toBeEnabled()
    expect(restart).not.toHaveTextContent(/Restarting/)
  })

  it('clears once the socket reconnects to a gateway that is active, with no Refresh', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    const { queryClient } = renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByTestId('remote-crew-restart-gateway')).toBeInTheDocument()

    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    // The reconnect's re-read of the crew list, which useWebSocket.frameTrace.test.ts
    // pins on both reconnect paths.
    void queryClient.invalidateQueries({ queryKey: ['instances'], exact: true })

    await waitFor(() => expect(screen.queryByTestId('remote-crew-restart-gateway')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^Connect$/i })).toBeEnabled()
  })
})
