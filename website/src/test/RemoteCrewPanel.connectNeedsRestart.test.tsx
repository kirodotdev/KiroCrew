import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
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
    },
  }
})
import { api } from '../api/client'

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
