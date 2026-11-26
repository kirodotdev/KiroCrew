/**
 * Settings -> Remote Crew -> My Crews: a crew row's own gateway writes are
 * refused while the dashboard is offline. Connect, Disconnect, the on/off
 * switch, Start, Stop and the armed Delete / Remove dim, say why on the control
 * itself, and send no request; arming a confirm still works, since it writes
 * nothing. Online the same row's writes go through.
 *
 * The panel runs real against a mocked api, so a refused click is proven by
 * the api mock never being called.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createConnectedTestStore, renderWithProviders } from './helpers'
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

const OFFLINE_TITLE = 'Gateway offline — reconnect to manage remote crews'
const offlineName = (label: string) => `${label} disabled — gateway offline`

const HAND = 'build box'
// A machine the user added by hand, not connected: Connect is its main action.
const HAND_CREW = {
  id: 'hb1',
  name: HAND,
  connection_method: 'ssh' as const,
  ssh_host: 'build.example',
  ssm_target: '',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  provisioner_id: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'hb1', state: 'disconnected' as const },
}

const CLOUD = 'Kiro Crew Cloud (kc-3f9a)'
// A running crew this gateway launched, correlated to its launch by tag.
const CLOUD_CREW = {
  id: 'kc1',
  name: CLOUD,
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
  status: { instance_id: 'kc1', state: 'connected' as const },
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
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [HAND_CREW, CLOUD_CREW] })
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(DONE_JOB as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } } as never)
})

async function openRow(name: string): Promise<HTMLElement> {
  const row = (await screen.findByText(name)).closest('[data-crew-id]') as HTMLElement
  fireEvent.click(within(row).getByRole('button', { name: `Show details for ${name}` }))
  return row
}

/** Dimmed with the offline reason in its tooltip, and a click sends nothing. */
function expectRefused(button: HTMLElement) {
  expect(button).toBeDisabled()
  expect(button).toHaveAttribute('title', OFFLINE_TITLE)
  fireEvent.click(button)
}

describe('a crew row while the gateway is offline', () => {
  it('refuses Connect, the switch and the armed Remove on a hand-added crew', async () => {
    renderWithProviders(<RemoteCrewPanel />)
    const row = await openRow(HAND)

    expectRefused(within(row).getByRole('button', { name: offlineName('Connect') }))
    const toggle = within(row).getByRole('switch', { name: offlineName(`${HAND} enabled`) })
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(toggle.parentElement).toHaveAttribute('title', OFFLINE_TITLE)
    fireEvent.click(toggle)

    // Arming the confirm writes nothing, so it stays live; the confirm refuses.
    fireEvent.click(within(row).getByRole('button', { name: `Remove ${HAND}` }))
    expectRefused(within(row).getByRole('button', { name: offlineName(`Remove ${HAND}`) }))

    // A mutation calls the api after its own `onMutate` settles, so let any
    // started one run before asserting nothing was sent.
    await act(async () => {})
    expect(api.connectInstance).not.toHaveBeenCalled()
    expect(api.updateInstance).not.toHaveBeenCalled()
    expect(api.disconnectInstance).not.toHaveBeenCalled()
    expect(api.removeInstance).not.toHaveBeenCalled()
  })

  it('refuses Disconnect, Stop, Start and the armed Delete on a cloud crew', async () => {
    renderWithProviders(<RemoteCrewPanel />)
    const row = await openRow(CLOUD)

    expectRefused(within(row).getByRole('button', { name: offlineName('Disconnect') }))
    expectRefused(within(row).getByRole('button', { name: offlineName(`Stop ${CLOUD}`) }))
    expectRefused(within(row).getByRole('button', { name: offlineName(`Start ${CLOUD}`) }))

    fireEvent.click(within(row).getByRole('button', { name: `Delete ${CLOUD}` }))
    expectRefused(within(row).getByRole('button', { name: offlineName(`Confirm deleting “${CLOUD}”`) }))

    await act(async () => {})
    expect(api.disconnectInstance).not.toHaveBeenCalled()
    expect(api.cloudStop).not.toHaveBeenCalled()
    expect(api.cloudStart).not.toHaveBeenCalled()
    expect(api.cloudDestroy).not.toHaveBeenCalled()
  })
})

describe('a crew row while the gateway is connected', () => {
  it('connects and disconnects as before', async () => {
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'hb1', state: 'connected' } as never)
    vi.mocked(api.disconnectInstance).mockResolvedValue({} as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />, { store: createConnectedTestStore() })

    const hand = (await screen.findByText(HAND)).closest('[data-crew-id]') as HTMLElement
    const connect = within(hand).getByRole('button', { name: 'Connect' })
    expect(connect).not.toHaveAttribute('title')
    await u.click(connect)
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('hb1'))

    const cloud = await openRow(CLOUD)
    await u.click(within(cloud).getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(api.disconnectInstance).toHaveBeenCalledWith('kc1'))
  })
})
