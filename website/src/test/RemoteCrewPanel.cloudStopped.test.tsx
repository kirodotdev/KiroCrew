import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'

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
    isAuthExpiredError: () => false,
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

const NAME = 'Kiro Crew Cloud (kc-3f9a)'
// What the gateway reports for a crew whose instance was stopped: the last SSM
// failure is still on the status, and the row must not show it.
const STOPPED_CREW = {
  id: 'kc1',
  name: NAME,
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: '',
  aws_region: 'us-east-1',
  ssm_run_as: '',
  provisioner_id: 'aws_ec2',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  cloud_stopped: true,
  status: {
    instance_id: 'kc1',
    state: 'error' as const,
    error: 'An error occurred (TargetNotConnected) when calling the StartSession operation',
  },
}
const DONE_JOB = {
  id: 'j-done', tag: 'kc-3f9a', instance_id: 'i-0abc123456789def0', profile: '', region: 'us-east-1',
  provider_id: 'aws_ec2',
  size_key: 'balanced', status: 'done' as const, steps: [], signin: null, created_at: 0, updated_at: 0,
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  __resetInstanceFailuresForTests()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(DONE_JOB as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } } as never)
})

describe('a cloud crew stopped from its menu', () => {
  it('reads as Stopped with Start as its button, not as a red connection error', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [STOPPED_CREW] })
    vi.mocked(api.cloudStart).mockResolvedValue({ started: true } as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const row = (await screen.findByText(NAME)).closest('[data-crew-id]') as HTMLElement
    expect(within(row).getByText('Stopped')).toBeInTheDocument()
    expect(within(row).getByText(/You stopped this crew's cloud machine/)).toBeInTheDocument()
    expect(within(row).queryByText(/TargetNotConnected/)).not.toBeInTheDocument()
    expect(within(row).queryByText('Error')).not.toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /^Connect/ })).not.toBeInTheDocument()

    await u.click(within(row).getByRole('button', { name: `Start ${NAME}` }))
    await waitFor(() => expect(api.cloudStart).toHaveBeenCalledWith('kc-3f9a', expect.anything()))

    await u.click(within(row).getByRole('button', { name: /More actions/i }))
    expect(screen.queryByRole('menuitem', { name: `Stop ${NAME}` })).not.toBeInTheDocument()
  })

  it('still offers Stop on a running cloud crew', async () => {
    const running = { ...STOPPED_CREW, cloud_stopped: false, status: { instance_id: 'kc1', state: 'connected' as const } }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [running] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const row = (await screen.findByText(NAME)).closest('[data-crew-id]') as HTMLElement
    expect(within(row).queryByText(/You stopped this crew's cloud machine/)).not.toBeInTheDocument()
    await u.click(within(row).getByRole('button', { name: /More actions/i }))
    expect(screen.getByRole('menuitem', { name: `Stop ${NAME}` })).toBeInTheDocument()
  })
})
