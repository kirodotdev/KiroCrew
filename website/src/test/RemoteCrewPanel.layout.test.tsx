import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

// Settings > Remote Crew (#38790): My Crews and Shared with me, one Add Crew
// modal for both ways of adding a Crew, rows with one main action and a
// disclosure for the rest, a clickable "Setting up" status, and one Settings
// section.
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
      instancesVersions: vi.fn(),
      relayViewerCrews: vi.fn(),
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
import { api, ApiError } from '../api/client'

const ssh = (over: Record<string, unknown>) => ({
  id: 'm1', name: 'dev-box', connection_method: 'ssh' as const, ssh_host: 'dev-box.internal',
  remote_port: 5476, local_port: 0, ttl: '20h', remote_bin: '', was_connected: false,
  status: { instance_id: 'm1', state: 'disconnected' as const },
  ...over,
})
const CONNECTED = ssh({ id: 'c1', name: 'build-farm', status: { instance_id: 'c1', state: 'connected', local_port: 7801 } })
const FAILING = ssh({ id: 'e1', name: 'gpu-box', status: { instance_id: 'e1', state: 'error', error: 'ssh: connection refused' } })
const IDLE = ssh({ id: 'd1', name: 'idle-box' })
const UNSIGNED = ssh({
  id: 's1', name: 'Cloud crew', connection_method: 'ssm', ssh_host: '', ssm_target: 'i-0abc1234', aws_region: 'us-east-1',
  provisioner_id: 'aws_ec2', status: { instance_id: 's1', state: 'disconnected' },
})
const UNSIGNED_JOB = {
  id: 'j-unsigned', tag: 'kc-aaaa', instance_id: 'i-0abc1234', profile: '', region: 'us-east-1', size_key: 'balanced',
  provider_id: 'aws_ec2', status: 'done' as const, signin: null, signin_detected: false, created_at: 0, updated_at: 0,
  steps: [{ key: 'connect', label: 'Connect', state: 'done' as const }],
}
const RUNNING_JOB = {
  id: 'j-run', tag: 'kc-4d10', profile: '', region: 'us-east-1', size_key: 'light', provider_id: 'aws_ec2',
  status: 'running' as const, signin: null, created_at: 0, updated_at: 0,
  steps: [
    { key: 'preflight', label: 'Check AWS access', state: 'done' as const },
    { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'active' as const },
    { key: 'signin', label: 'Sign in', state: 'pending' as const },
    { key: 'connect', label: 'Connect', state: 'pending' as const },
  ],
}

const list = (instances: unknown[]) => ({ active: true, warm_set_cap: 5, instances }) as never
const row = (name: string) =>
  screen.getByRole('button', { name: new RegExp(`details for ${name}$`) }).closest('[data-crew-id]') as HTMLElement

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({
    provisioners: [{ id: 'aws_ec2', kind: 'aws_ec2', label: 'AWS EC2 in your own account', posix_only: true, steps: [] }],
  } as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' },
    suggested_target: { license: '', start_url: '', region: '' },
    discovery: 'read',
  })
  vi.mocked(api.cloudPreflight).mockResolvedValue({
    reachable: true, account: '1234', arn: 'arn', ec2_reachable: true, cloudformation_reachable: true,
    ssm_reachable: true, session_manager_plugin: true, note: '', detail: '',
  } as never)
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB as never)
  vi.mocked(api.instancesVersions).mockResolvedValue({ local_version: '1.0.0', instances: [] } as never)
  vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED]))
})
afterEach(() => {
  delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
})

describe('Remote Crew sections', () => {
  it('lists My Crews and the Crews Shared with me as two sections, then Settings', async () => {
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(async () => ({ ok: true })) }
    vi.mocked(api.relayViewerCrews).mockResolvedValue({
      crews: [{
        endpointId: 'ep-1', name: 'Platform Team', crewType: 'TEAM', presence: 'CONNECTED',
        relayConnectionStatus: 'CONNECTED', crewUrl: 'https://ep-1.relay.example',
      }],
    } as never)
    renderWithProviders(<RemoteCrewPanel />)

    const mine = await screen.findByRole('region', { name: 'My Crews' })
    expect(await within(mine).findByText('build-farm')).toBeInTheDocument()
    const shared = await screen.findByRole('region', { name: 'Shared with me' })
    expect(await within(shared).findByText('Platform Team')).toBeInTheDocument()
    expect(within(shared).queryByText('build-farm')).not.toBeInTheDocument()
    expect(within(mine).queryByText('Platform Team')).not.toBeInTheDocument()

    // Auto-connect and the default destination share one Settings section.
    const settings = screen.getByRole('region', { name: 'Settings' })
    expect(within(settings).getByRole('switch', { name: /Auto-connect crews/i })).toBeInTheDocument()
    expect(within(settings).getByTestId('default-destination-crew')).toBeInTheDocument()
    expect(within(mine).queryByTestId('default-destination-crew')).not.toBeInTheDocument()
  })
})

describe('Add Crew modal', () => {
  it('offers the two ways to add a Crew, each reaching today\'s form', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // No add or launch form on the page itself.
    await screen.findByText('build-farm')
    expect(screen.queryByRole('button', { name: 'Add remote crew' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Before you start/i)).not.toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: 'Add Crew' }))
    const dialog = screen.getByRole('dialog', { name: 'Add a Crew' })
    await u.click(within(dialog).getByRole('button', { name: /Set up a new Crew/ }))
    // The launch checklist and form.
    expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Launch$/ })).toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: /Back/ }))
    await u.click(screen.getByRole('button', { name: /Connect a machine you have/ }))
    // The add-a-machine form.
    expect(await screen.findByRole('button', { name: 'Add remote crew' })).toBeInTheDocument()
    expect(screen.getByLabelText('SSH host / alias')).toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('shows a launch in progress as a Setting up row of My Crews, whose status opens the steps and Cancel', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [RUNNING_JOB] } as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const mine = await screen.findByRole('region', { name: 'My Crews' })
    const status = await within(mine).findByRole('button', { name: /Setting up · 2 of 4/ })
    expect(status).toHaveAttribute('aria-expanded', 'false')
    expect(within(mine).getByText('EC2 · SSM')).toBeInTheDocument()
    expect(within(mine).queryByText('Check AWS access')).not.toBeInTheDocument()
    expect(within(mine).queryByRole('button', { name: /Cancel setup of/ })).not.toBeInTheDocument()

    await u.click(status)
    expect(status).toHaveAttribute('aria-expanded', 'true')
    expect(within(mine).getByText('Check AWS access')).toBeInTheDocument()
    expect(within(mine).getByText('Create the instance and install Kiro Crew')).toBeInTheDocument()
    expect(within(mine).getByText(/Keeps running if you leave this page/)).toBeInTheDocument()
    expect(within(mine).getByRole('button', { name: 'Cancel setup of kc-4d10 and remove the crew' })).toBeInTheDocument()
  })
})

describe('Crew rows', () => {
  it('shows name, status and source collapsed, and the rest behind its chevron', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const r = await waitFor(() => row('build-farm'))
    const chevron = within(r).getByRole('button', { name: 'Show details for build-farm' })
    expect(chevron).toHaveAttribute('aria-expanded', 'false')
    expect(within(r).getByText('Connected')).toBeInTheDocument()
    expect(within(r).getByText('SSH')).toBeInTheDocument()
    expect(within(r).queryByText('dev-box.internal')).not.toBeInTheDocument()
    expect(within(r).queryByRole('switch')).not.toBeInTheDocument()
    expect(within(r).queryByRole('button', { name: /Edit settings/ })).not.toBeInTheDocument()
    // No overflow menu: the details hold its actions.
    expect(within(r).queryByRole('button', { name: /More actions/ })).not.toBeInTheDocument()

    await u.click(chevron)
    expect(within(r).getByRole('button', { name: 'Hide details for build-farm' })).toHaveAttribute('aria-expanded', 'true')
    const details = within(r).getByTestId('crew-row-details')
    expect(within(details).getByText('Host')).toBeInTheDocument()
    expect(within(details).getByText('dev-box.internal')).toBeInTheDocument()
    expect(within(details).getByText('Port')).toBeInTheDocument()
    expect(within(details).getByText('5476')).toBeInTheDocument()
    expect(within(details).getByText('Managed by')).toBeInTheDocument()
    expect(within(details).getByRole('switch', { name: 'build-farm enabled' })).toBeInTheDocument()
    expect(within(details).getByRole('button', { name: 'Diagnose build-farm' })).toBeInTheDocument()
    expect(within(details).getByRole('button', { name: /Edit settings/ })).toBeInTheDocument()
    expect(within(details).getByRole('button', { name: 'Remove build-farm' })).toBeInTheDocument()
  })

  it('gives each state one main action: Open, Diagnose, Connect, Sign in, Update', async () => {
    const UPDATABLE = ssh({ id: 'u1', name: 'dev-box-2', status: { instance_id: 'u1', state: 'connected', local_port: 7802 } })
    vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED, FAILING, IDLE, UNSIGNED, UPDATABLE]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [UNSIGNED_JOB] } as never)
    // The box says it is signed out, so the row is badged.
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new ApiError(409, 'no sign-in pending', JSON.stringify({ code: 'no_signin_pending' })))
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '1.2.0',
      instances: [
        { id: 'c1', version: '1.2.0', behind: false, updatable: true },
        { id: 'u1', version: '1.0.0', behind: true, updatable: true },
      ],
    } as never)
    const u = userEvent.setup()
    const { store } = renderWithProviders(<RemoteCrewPanel />)

    await waitFor(() => row('build-farm'))
    const header = (name: string) => row(name).firstElementChild as HTMLElement

    // Connected: Open switches to the crew as the header switcher does.
    await u.click(within(header('build-farm')).getByRole('button', { name: 'Open build-farm' }))
    expect(store.getState().instances.activeId).toBe('c1')

    // Can't connect: tagged, and Diagnose is the action.
    expect(within(header('gpu-box')).getByText("Can't connect")).toBeInTheDocument()
    expect(within(header('gpu-box')).getByRole('button', { name: 'Diagnose gpu-box' })).toBeInTheDocument()
    expect(within(header('gpu-box')).queryByRole('button', { name: /^Connect/ })).not.toBeInTheDocument()

    // Disconnected with nothing wrong: Connect.
    expect(within(header('idle-box')).getByRole('button', { name: /^Connect$/ })).toBeInTheDocument()

    // Needs sign-in: tagged, and Sign in.
    expect(within(header('Cloud crew')).getByText('Needs sign-in')).toBeInTheDocument()
    expect(within(header('Cloud crew')).getByRole('button', { name: 'Sign in on Cloud crew' })).toBeInTheDocument()
    expect(within(header('Cloud crew')).getByText('EC2')).toBeInTheDocument()
    expect(within(header('Cloud crew')).getByText('SSM')).toBeInTheDocument()

    // Update available: the version tag, and the Update control instead of Open.
    expect(await within(header('dev-box-2')).findByTestId('remote-behind-badge')).toBeInTheDocument()
    expect(within(header('dev-box-2')).getByTestId('remote-update-control')).toBeInTheDocument()
    expect(within(header('dev-box-2')).queryByRole('button', { name: 'Open dev-box-2' })).not.toBeInTheDocument()

    // Exactly one main action per row header.
    for (const name of ['build-farm', 'gpu-box', 'idle-box', 'Cloud crew']) {
      const buttons = within(header(name)).getAllByRole('button').filter(b => !/details for/.test(b.getAttribute('aria-label') ?? ''))
      expect(buttons, name).toHaveLength(1)
    }
  })

  it('offers Start, not Sign in, on a stopped crew that never finished signing in', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([{ ...UNSIGNED, cloud_stopped: true }]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [UNSIGNED_JOB] } as never)
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new ApiError(409, 'no sign-in pending', JSON.stringify({ code: 'no_signin_pending' })))
    renderWithProviders(<RemoteCrewPanel />)
    await waitFor(() => row('Cloud crew'))
    const header = row('Cloud crew').firstElementChild as HTMLElement
    expect(await within(header).findByRole('button', { name: /Start/ })).toBeInTheDocument()
    expect(within(header).queryByRole('button', { name: 'Sign in on Cloud crew' })).not.toBeInTheDocument()
  })

  it('keeps Open on a current crew whose update finished, with the outcome beside it', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED]))
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '1.2.0',
      instances: [{ id: 'c1', version: '1.2.0', behind: false, updatable: true }],
    } as never)
    const job = {
      state: 'succeeded' as const, phase: 'done' as const, started_at: 1, finished_at: 2,
      elapsed_secs: 40, error: '', version_before: '1.0.0', version_after: '1.2.0',
    }
    const { queryClient } = renderWithProviders(<RemoteCrewPanel />)
    await waitFor(() => row('build-farm'))
    act(() => { queryClient.setQueryData(['instances', 'update-job', 'c1'], { job }) })
    const header = row('build-farm').firstElementChild as HTMLElement
    expect(await within(header).findByText('build-farm now runs 1.2.0.')).toBeInTheDocument()
    expect(within(header).getByRole('button', { name: 'Open build-farm' })).toBeInTheDocument()
  })
})
