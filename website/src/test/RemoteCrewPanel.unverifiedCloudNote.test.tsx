import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

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
import { api } from '../api/client'

// A crew added by hand over Session Manager: an SSM target, no EC2 stamp, and no
// launch job in this gateway's store. The dashboard cannot tell it from a launch
// made before crews were stamped, so its row keeps the hedged caption (issue #34682).
const BYO_SSM_INSTANCE = {
  id: 'b1',
  name: 'my-ec2',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: 'test',
  aws_region: 'us-east-1',
  ssm_run_as: 'ec2-user',
  provisioner_id: '',
  remote_port: 5476,
  local_port: 7801,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  status: { instance_id: 'b1', state: 'connected' as const, local_port: 7801, remote_port: 5476 },
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

describe('RemoteCrewPanel — the note on an SSM crew with no launch record', () => {
  it('says what the note means and what Remove does', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [BYO_SSM_INSTANCE] })
    renderWithProviders(<RemoteCrewPanel />)

    const row = (await screen.findByText('my-ec2')).closest('[data-crew-id="b1"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(
      within(row).getByText(
        'Kiro Crew cannot tell from here whether it launched this crew or you added it. Remove only takes it off this list and leaves the machine as it is; stop or delete the machine yourself if you no longer need it.',
      ),
    ).toBeInTheDocument()
  })
})
