/**
 * The built-in launch form: an empty region field reads as empty and blocks
 * Launch, a launch that went out cannot be repeated by a second click on the
 * same settings, and the checklist says when it answers for other values than
 * the ones typed.
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
const STALE = 'The profile or Region changed since this check. Re-check to update.'
const RETRY = 'Fix the problem shown below, or change the Subnet ID above, then choose Launch to try again.'
const CHECK_CREWS = 'The launch did not finish. Check Your crews before launching again.'
const CREW_LISTED = 'The launch did not finish. Its crew is listed in Your crews, where you can fix or delete it.'
const LAUNCHED_ID = 'i-0abc123456789def0'
// The registry id is derived from the crew name; the EC2 id the job carries is
// the crew's SSM target. Distinct here, as they are in a real registry.
const LIVE_CREW = {
  id: 'kiro-crew-cloud-kc-4d10', name: 'Kiro Crew Cloud (kc-4d10)', connection_method: 'ssm' as const, ssm_target: LAUNCHED_ID,
  ssh_host: '', aws_profile: '', aws_region: 'us-east-1', ssm_run_as: '', remote_port: 7777, local_port: 0,
  ttl: '20h', remote_bin: '', was_connected: false,
  status: { instance_id: 'kiro-crew-cloud-kc-4d10', state: 'disconnected' as const },
}
const FAILED_AT_PREFLIGHT = [
  { key: 'preflight', label: 'Check your AWS setup', state: 'failed' as const },
  { key: 'provision', label: 'Create the instance', state: 'pending' as const },
  { key: 'install', label: 'Install Kiro Crew', state: 'pending' as const },
  { key: 'connect', label: 'Connect', state: 'pending' as const },
]
const FAILED_AFTER_REGISTER = [
  { key: 'preflight', label: 'Checked your AWS setup', state: 'done' as const },
  { key: 'provision', label: 'Created the instance', state: 'done' as const },
  { key: 'signin', label: 'Sign in to Kiro', state: 'failed' as const },
  { key: 'connect', label: 'Connected', state: 'done' as const },
]
const FAILED_AT_REGISTER = [
  { key: 'preflight', label: 'Checked your AWS setup', state: 'done' as const },
  { key: 'provision', label: 'Created the instance', state: 'done' as const },
  { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
  { key: 'connect', label: 'Connect', state: 'failed' as const },
]
const FAILED_AT_PROVISION = [
  { key: 'preflight', label: 'Checked your AWS setup', state: 'done' as const },
  { key: 'provision', label: 'Create the instance', state: 'failed' as const },
  { key: 'install', label: 'Install Kiro Crew', state: 'pending' as const },
  { key: 'connect', label: 'Connect', state: 'pending' as const },
]

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
  await u.click(await screen.findByRole('radio', { name: /Set up a new one/i }))
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

  it('after a launch that failed before provisioning, says it failed and offers Launch as the retry', async () => {
    // Stopped at the AWS check: nothing was created, so repeating the settings is safe.
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue({ ...RUNNING_JOB, status: 'failed' as const, steps: FAILED_AT_PREFLIGHT })
    const u = userEvent.setup()
    await openSetup(u)

    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)

    expect(await screen.findByText(RETRY)).toBeInTheDocument()
    expect(screen.getAllByRole('alert').some(a => a.textContent?.includes(RETRY))).toBe(true)
    expect(screen.queryByText(/Launched\. Its progress is shown below/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Launch another crew' })).not.toBeInTheDocument()
    // Launch is the retry: the reader may fix the cause outside the form or change the subnet first.
    const retry = screen.getByRole('button', { name: /^Launch$/ })
    expect(retry).not.toBeDisabled()
    await u.type(screen.getByRole('textbox', { name: 'Subnet ID (optional)' }), 'subnet-0123abcd')
    await u.click(retry)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.cloudLaunch).mock.calls[1][0]).toMatchObject({ subnet_id: 'subnet-0123abcd' })
  })

  // From provisioning on a stack may still exist: a refused sign-in leaves a
  // registered crew, and a failed provision is rolled back best-effort with no
  // field saying whether the rollback confirmed.
  // "Listed in Your crews" is said only while the live list holds the instance:
  // the job is a snapshot, so a registered crew deleted since gets the neutral line.
  it.each([
    ['sign-in, with its registered crew in the live list', { instance_id: LAUNCHED_ID, steps: FAILED_AFTER_REGISTER }, [LIVE_CREW], CREW_LISTED],
    ['sign-in, with its registered crew deleted since', { instance_id: LAUNCHED_ID, steps: FAILED_AFTER_REGISTER }, [], CHECK_CREWS],
    ['registration, with an instance that never registered', { instance_id: LAUNCHED_ID, steps: FAILED_AT_REGISTER }, [LIVE_CREW], CHECK_CREWS],
    ['provisioning, with no instance returned', { steps: FAILED_AT_PROVISION }, [], CHECK_CREWS],
  ])('after a launch that failed during %s, says it failed but keeps the same-settings guard', async (_case, failed, live, line) => {
    // The list holds the crew only once the launch has gone out, as the gateway's would.
    vi.mocked(api.listInstances).mockImplementation(async () => ({
      active: true, warm_set_cap: 5, instances: vi.mocked(api.cloudLaunch).mock.calls.length ? live : [],
    }) as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue({ ...RUNNING_JOB, status: 'failed' as const, ...failed })
    const u = userEvent.setup()
    await openSetup(u)

    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)

    expect(await screen.findByText(line)).toBeInTheDocument()
    expect(screen.getAllByRole('alert').some(a => a.textContent?.includes(line))).toBe(true)
    expect(screen.queryByText(line === CREW_LISTED ? CHECK_CREWS : CREW_LISTED)).not.toBeInTheDocument()
    expect(screen.queryByText(/Launched\. Its progress is shown below/)).not.toBeInTheDocument()
    expect(screen.queryByText(RETRY)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Launch another crew' })).toBeInTheDocument()
    // A different subnet is a different crew, so Launch comes back for it.
    await u.type(screen.getByRole('textbox', { name: 'Subnet ID (optional)' }), 'subnet-0123abcd')
    await waitFor(() => expect(screen.getByRole('button', { name: /^Launch$/ })).not.toBeDisabled())
    expect(api.cloudLaunch).toHaveBeenCalledTimes(1)
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
    expect(await screen.findByText('Checked with your default AWS credentials, with no region set.')).toBeInTheDocument()
  })

  it('marks the checklist out of date while the typed profile differs from the checked one', async () => {
    const u = userEvent.setup()
    await openSetup(u)
    const list = (await screen.findByText('Session Manager plugin')).closest('ul') as HTMLElement
    const greyMarks = () => list.querySelectorAll('li > span.bg-border-strong').length
    expect(greyMarks()).toBe(0)
    expect(screen.queryByText(STALE)).toBeNull()

    // Typing runs no check, so the rows still answer for the default credentials: their
    // marks turn grey and the line above says so, while the rows' buttons stay usable.
    await u.type(screen.getByRole('textbox', { name: 'AWS profile' }), 'Admin')
    expect(greyMarks()).toBe(5)
    expect(screen.getByText(STALE)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Re-check/ })).toBeEnabled()
    expect(api.cloudPreflight).not.toHaveBeenCalledWith('Admin', expect.anything())

    // Leaving the field checks the typed profile, and the mark goes away.
    await u.tab()
    await waitFor(() => expect(api.cloudPreflight).toHaveBeenLastCalledWith('Admin', 'us-east-1'))
    expect(await screen.findByText('Checked against profile Admin in us-east-1.')).toBeInTheDocument()
    expect(screen.queryByText(STALE)).toBeNull()
  })
})

describe('launchGuardKey', () => {
  it('treats an omitted provider as the built-in one the gateway defaults to', () => {
    const body = { profile: '', region: 'us-east-1', size_key: 'balanced' }
    expect(launchGuardKey(body)).toBe(launchGuardKey({ provider_id: 'aws_ec2', ...body }))
    expect(launchGuardKey({ provider_id: 'other', ...body })).not.toBe(launchGuardKey(body))
  })
})
