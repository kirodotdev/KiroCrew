/**
 * The built-in launch form: an empty region field reads as empty and blocks
 * Launch, and a launch that went out cannot be repeated by a second click on the
 * same settings.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { launchGuardKey } from '../pages/settings/remoteCrewPanelModel'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

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

const PREFLIGHT_OK = {
  reachable: true, account: '1234•••7890', arn: 'arn:aws:iam::x:user/dev',
  ec2_reachable: true, cloudformation_reachable: true, ssm_reachable: true,
  session_manager_plugin: true, note: '', detail: '',
}
const AWS_EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup' },
    { key: 'provision', label: 'Create the instance' },
    { key: 'signin', label: 'Sign in to Kiro' },
    { key: 'connect', label: 'Connect' },
  ],
}
const RUNNING_JOB = {
  id: 'j-run', tag: 'kc-4d10', profile: '', region: 'us-east-1', size_key: 'light',
  provider_id: 'aws_ec2',
  status: 'running' as const, signin: null, created_at: 0, updated_at: 0,
  steps: [
    { key: 'preflight', label: 'Checked your AWS setup', state: 'done' as const },
    { key: 'provision', label: 'Created the instance', state: 'done' as const },
    { key: 'install', label: 'Installing Kiro Crew', state: 'active' as const },
    { key: 'connect', label: 'Connect', state: 'pending' as const },
  ],
}
const DONE_JOB = { ...RUNNING_JOB, status: 'done' as const, instance_id: 'i-0abc123456789def0' }
const REGION_REQUIRED = 'Enter an AWS Region, for example us-east-1.'

beforeEach(() => {
  vi.clearAllMocks()
  // The panel persists profile and region; a test that edits them must not leak.
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
  vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' },
  })
})

async function openSetup(u: ReturnType<typeof userEvent.setup>) {
  renderWithProviders(<RemoteCrewPanel />)
  await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
}

describe('RemoteCrewPanel launch form', () => {
  it('shows an emptied EC2 region as missing and does not launch until one is entered', async () => {
    const u = userEvent.setup()
    await openSetup(u)

    const region = await screen.findByRole('textbox', { name: 'Region' })
    expect(region).toHaveValue('us-east-1')
    expect(region).not.toHaveAttribute('aria-invalid', 'true')
    await u.clear(region)
    // The example text reads as an example, not as a value, and the field says it is missing.
    expect(region).toHaveAttribute('placeholder', 'e.g. us-east-1')
    expect(region).toHaveAttribute('aria-invalid', 'true')
    expect(region).toHaveAccessibleDescription(REGION_REQUIRED)
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    expect(launch).toBeDisabled()

    await u.type(region, 'eu-west-1')
    // Leaving the field re-runs the AWS check against the new region; Launch waits for it.
    await u.tab()
    await waitFor(() => expect(api.cloudPreflight).toHaveBeenLastCalledWith(undefined, 'eu-west-1'))
    await waitFor(() => expect(launch).not.toBeDisabled())
    expect(region).not.toHaveAttribute('aria-invalid', 'true')
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({
      provider_id: 'aws_ec2', profile: '', region: 'eu-west-1', size_key: 'balanced',
    }))
  })

  it('shows an empty Identity Center region as missing, not as us-east-1', async () => {
    vi.mocked(api.cloudIdentity).mockResolvedValue({
      identity: { account_type: 'IamIdentityCenter', start_url: 'https://example.awsapps.com/start' },
      suggested_target: { license: 'pro', start_url: 'https://example.awsapps.com/start', region: '' },
    })
    const u = userEvent.setup()
    await openSetup(u)

    await waitFor(() => expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked())
    const idcRegion = screen.getByRole('textbox', { name: /Identity Center region/i })
    expect(idcRegion).toHaveValue('')
    expect(idcRegion).toHaveAttribute('placeholder', 'e.g. us-east-1')
    expect(idcRegion).toHaveAttribute('aria-invalid', 'true')
    expect(idcRegion).toHaveAccessibleDescription(REGION_REQUIRED)
    expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled()

    await u.type(idcRegion, 'us-west-2')
    expect(idcRegion).not.toHaveAttribute('aria-invalid', 'true')
    await waitFor(() => expect(screen.getByRole('button', { name: /^Launch$/ })).not.toBeDisabled())
  })

  it('does not launch the same crew twice: Launch stays off until a setting changes or Launch another crew', async () => {
    let resolveLaunch: (job: typeof RUNNING_JOB) => void = () => {}
    vi.mocked(api.cloudLaunch).mockImplementation(() => new Promise(r => { resolveLaunch = r }))
    // The setup has finished, so only the same-settings guard holds Launch off.
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(DONE_JOB)
    const u = userEvent.setup()
    await openSetup(u)

    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    // Off while the request is in flight.
    expect(await screen.findByRole('button', { name: /Launching/ })).toBeDisabled()
    resolveLaunch(RUNNING_JOB)
    expect(await screen.findByText(/Launched\. Its progress is shown below/)).toBeInTheDocument()
    const again = screen.getByRole('button', { name: /^Launch$/ })
    expect(again).toBeDisabled()
    expect(api.cloudLaunch).toHaveBeenCalledTimes(1)

    // Changing a field makes it a different crew, so Launch comes back.
    const subnet = screen.getByRole('textbox', { name: 'Subnet ID (optional)' })
    await u.type(subnet, 'subnet-0123abcd')
    await waitFor(() => expect(again).not.toBeDisabled())
    // Back to the launched settings: off again.
    await u.clear(subnet)
    await waitFor(() => expect(again).toBeDisabled())

    // An identical second crew takes an explicit choice, and that choice launches it.
    await u.click(screen.getByRole('button', { name: 'Launch another crew' }))
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.cloudLaunch).mock.calls[1]).toEqual(vi.mocked(api.cloudLaunch).mock.calls[0])
  })

  it('offers no launch while a crew setup is still running, which the gateway would refuse', async () => {
    const u = userEvent.setup()
    await openSetup(u)

    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    expect(await screen.findByText(/A crew setup is running below/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Launch another crew' })).not.toBeInTheDocument()
    // A different crew must wait too: the gateway takes one setup at a time.
    await u.type(screen.getByRole('textbox', { name: 'Subnet ID (optional)' }), 'subnet-0123abcd')
    expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled()
    expect(api.cloudLaunch).toHaveBeenCalledTimes(1)
  })

  it('names the missing region in the check line instead of leaving a gap', async () => {
    const u = userEvent.setup()
    await openSetup(u)
    const region = await screen.findByRole('textbox', { name: 'Region' })
    await u.clear(region)
    await u.tab()
    expect(await screen.findByText('Checked against profile none set (AWS CLI default), with no region set.')).toBeInTheDocument()
  })
})

describe('launchGuardKey', () => {
  it('treats an omitted provider as the built-in one the gateway defaults to', () => {
    const body = { profile: '', region: 'us-east-1', size_key: 'balanced' }
    expect(launchGuardKey(body)).toBe(launchGuardKey({ provider_id: 'aws_ec2', ...body }))
    expect(launchGuardKey({ provider_id: 'other', ...body })).not.toBe(launchGuardKey(body))
  })
})
