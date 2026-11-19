/**
 * Launching a cloud crew without the Kiro sign-in.
 *
 * A crew on another agent harness works without a Kiro sign-in, so the launch
 * form offers the sign-in as a default-on choice, the progress card offers
 * Skip for now while the sign-in runs, and a skipped sign-in reads as a choice:
 * a skipped step and no "Needs sign-in" badge.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(async () => true),
  copyWithOutcome: vi.fn(async () => 'copied'),
}))

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
      cloudIdentity: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudLaunchSigninRestart: vi.fn(),
      cloudLaunchSigninSkip: vi.fn(),
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
  id: 'aws_ec2', kind: 'aws_ec2', label: 'AWS EC2 in your own account', posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup' },
    { key: 'provision', label: 'Create the instance' },
    { key: 'signin', label: 'Sign in to Kiro' },
    { key: 'connect', label: 'Connect' },
  ],
}
const IID = 'i-0abc123456789def0'
const base = {
  id: 'j-1', tag: 'kc-5e10bb', instance_id: IID, profile: '', region: 'us-east-1',
  provider_id: 'aws_ec2', size_key: 'balanced', signin_detected: false, created_at: 0, updated_at: 0,
}
const SIGNING_IN = {
  ...base,
  status: 'awaiting_signin' as const,
  signin: { url: 'https://view.awsapps.com/start/#/device?user_code=AB-12', code: 'AB-12' },
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
    { key: 'provision', label: 'Create the instance', state: 'done' as const },
    { key: 'signin', label: 'Sign in to Kiro', state: 'active' as const },
    { key: 'connect', label: 'Connect', state: 'pending' as const },
  ],
}
const SKIPPED_DONE = {
  ...base,
  status: 'done' as const,
  signin: null,
  signin_skipped: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
    { key: 'provision', label: 'Create the instance', state: 'done' as const },
    {
      key: 'signin', label: 'Sign in to Kiro', state: 'skipped' as const,
      detail: 'Skipped.',
    },
    { key: 'connect', label: 'Connect', state: 'done' as const, detail: 'Added to My Crews.' },
  ],
}
const CLOUD_INSTANCE = {
  id: 'kc1', name: 'Kiro Crew Cloud (kc-5e10bb)', connection_method: 'ssm' as const, ssm_target: IID,
  ssh_host: '', ssm_run_as: '', aws_profile: '', aws_region: 'us-east-1', remote_port: 8765,
  local_port: 0, ttl: '20h', remote_bin: '', was_connected: false, provisioner_id: 'aws_ec2',
  status: { instance_id: 'kc1', state: 'disconnected' as const },
}

const useJob = (job: unknown) => {
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: job ? [job] : [] } as never)
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(job as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] } as never)
  vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' },
  } as never)
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
  vi.mocked(api.cloudLaunch).mockResolvedValue(SIGNING_IN as never)
  vi.mocked(api.cloudLaunchSignin).mockResolvedValue({ signin: SIGNING_IN.signin } as never)
  vi.mocked(api.cloudLaunchSigninSkip).mockResolvedValue({ ...SIGNING_IN, status: 'running' } as never)
  useJob(null)
})

const openSetup = async (u: ReturnType<typeof userEvent.setup>) => {
  await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
  await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
}

describe('the launch form', () => {
  it('keeps sign-in off across a remount and launches with skip_signin', async () => {
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const toggle = await screen.findByRole('checkbox', { name: /Sign in to Kiro on the new crew/ })
    expect(first.store.getState().instances.crewForms.launch).toBeNull()
    await u.click(toggle)
    expect(toggle).not.toBeChecked()
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await openSetup(u)
    expect(await screen.findByRole('checkbox', { name: /Sign in to Kiro on the new crew/ })).not.toBeChecked()

    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({
      provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced', skip_signin: true,
    }))
    await waitFor(() => expect(first.store.getState().instances.crewForms.launch).toBeNull())
  })

  it('signs in by default, and launches with skip_signin when the choice is off', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const toggle = await screen.findByRole('checkbox', { name: /Sign in to Kiro on the new crew/ })
    expect(toggle).toBeChecked()
    expect(screen.getByRole('radio', { name: /Builder ID/i })).toBeInTheDocument()

    await u.click(toggle)
    expect(toggle).not.toBeChecked()
    // No identity to choose for a sign-in that will not happen.
    expect(screen.queryByRole('radio', { name: /Builder ID/i })).toBeNull()

    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({
      provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced', skip_signin: true,
    }))
  })
})

describe('the progress card', () => {
  it('offers Skip for now while the sign-in runs, and asks the gateway to skip', async () => {
    useJob(SIGNING_IN)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const dialog = await screen.findByRole('dialog')
    await u.click(await within(dialog).findByTestId('signin-skip'))
    await waitFor(() => expect(api.cloudLaunchSigninSkip).toHaveBeenCalledWith('j-1'))
  })

  it('shows a skipped sign-in as skipped, with one line on where to sign in later', async () => {
    useJob(SKIPPED_DONE)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByTestId('step-skipped-signin')).toBeInTheDocument()
    expect(within(dialog).getAllByText(/sign in later/i)).toHaveLength(1)
    expect(within(dialog).getByTestId('signin-skipped-note')).toHaveTextContent(
      "You can sign in later in the crew's Settings → Kiro Account.",
    )
    expect(within(dialog).queryByText(/Needs sign-in/)).toBeNull()
    expect(within(dialog).queryByTestId('signin-prompt')).toBeNull()
    expect(within(dialog).queryByTestId('signin-skip')).toBeNull()
  })
})

describe('the crew row', () => {
  const withCrew = () =>
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] } as never)

  it('reads as ready: no Needs sign-in, no sign-in box, and no probe', async () => {
    useJob(SKIPPED_DONE)
    withCrew()
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText(CLOUD_INSTANCE.name)).toBeInTheDocument()
    await waitFor(() => expect(api.cloudLaunches).toHaveBeenCalled())
    expect(screen.queryByText(/Needs sign-in/)).toBeNull()
    expect(screen.queryByTestId('signin-prompt')).toBeNull()
    expect(screen.queryByText(/sign in later/i)).toBeNull()
    expect(api.cloudLaunchSignin).not.toHaveBeenCalled()
  })

  it('shows the newest skipped launch, not an older unsigned one, on a reused stack', async () => {
    // The first launch on this stack ended registered but unsigned, its code
    // kept. The owner then re-launched onto the same stack with sign-in off.
    const OLDER_UNSIGNED = {
      ...SKIPPED_DONE,
      id: 'j-0',
      signin_skipped: false,
      signin: { url: 'https://view.awsapps.com/start/#/device?user_code=OLD-99', code: 'OLD-99' },
      steps: SKIPPED_DONE.steps.map(st => (st.key === 'signin' ? { key: st.key, label: st.label, state: 'failed' as const } : st)),
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [SKIPPED_DONE, OLDER_UNSIGNED] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(SKIPPED_DONE as never)
    withCrew()
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText(CLOUD_INSTANCE.name)).toBeInTheDocument()
    await waitFor(() => expect(api.cloudLaunches).toHaveBeenCalled())
    expect(screen.queryByTestId('signin-prompt')).toBeNull()
    expect(screen.queryByText(/Needs sign-in/)).toBeNull()
    expect(screen.queryByText(/OLD-99/)).toBeNull()
    expect(api.cloudLaunchSignin).not.toHaveBeenCalledWith('j-0')
  })
})
