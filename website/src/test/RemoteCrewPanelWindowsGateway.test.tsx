/**
 * The Set-up tab on a Windows gateway (#32150). The dashboard launcher refuses
 * there with 400 `posix_host_required` before any AWS call, so the tab says that
 * once, with the ways forward, and never blames the AWS credentials it did not read.
 */
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { REMOTE_CREW_WINDOWS_GUIDE_URL } from '../pages/settings/RemoteCrewWindowsNotice'
import { __resetErrorJournalForTests } from '../utils/errorReport'

const platformEnv = vi.hoisted(() => ({ value: 'other' as string }))
vi.mock('../hooks/useGatewayPlatform', async importOriginal => ({
  ...(await importOriginal<typeof import('../hooks/useGatewayPlatform')>()),
  useGatewayPlatform: () => platformEnv.value,
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

const POSIX_ERROR = 'cloud provisioning requires a POSIX host (Linux/macOS); use WSL on Windows'
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
  confirm_before_launch: '',
}

async function openSetup() {
  const u = userEvent.setup()
  renderWithProviders(<RemoteCrewPanel />)
  await u.click(await screen.findByRole('radio', { name: /Set up a new one/i }))
  return screen.findByRole('heading', { name: /Cloud crews can’t launch from this Windows dashboard/i })
}

function expectNoCredentialBlame() {
  expect(screen.queryByText(/Could not reach AWS/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/credentials were not checked/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/requires a POSIX host/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/Finish the AWS setup above/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Launch$/i })).not.toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  platformEnv.value = 'other'
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } })
  vi.mocked(api.cloudPreflight).mockRejectedValue(
    new ApiError(400, POSIX_ERROR, JSON.stringify({ error: POSIX_ERROR, code: 'posix_host_required' })),
  )
})

describe('RemoteCrewPanel Set-up tab on a Windows gateway', () => {
  it('says once that the launcher needs Linux or macOS when the preflight answers posix_host_required', async () => {
    const heading = await openSetup()
    const notice = heading.closest('section') as HTMLElement
    expect(notice).not.toBeNull()
    expect(notice).toHaveTextContent(/Your AWS profile wasn’t used, so it isn’t the problem/)
    expect(notice).toHaveTextContent(/kirocrew cloud launch/)
    expect(notice).toHaveTextContent(/does not add kirocrew to PATH/)
    expect(notice).toHaveTextContent(/administrator rights and a restart/)
    const guide = screen.getByRole('link', { name: /Read the Windows guide/i })
    expect(guide).toHaveAttribute('href', REMOTE_CREW_WINDOWS_GUIDE_URL)
    // The rejection learned it, so the sentence is the shared error surface, with
    // its hand-off, inside the notice rather than a second error above it.
    const alert = within(notice).getByRole('alert')
    expect(alert).toHaveTextContent(/Your AWS profile wasn’t used, so it isn’t the problem/)
    expect(within(alert).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Ask the agent/i })).toHaveLength(1)
    expectNoCredentialBlame()
  })

  it('runs no AWS check at all when the gateway is known to be Windows', async () => {
    platformEnv.value = 'windows'
    const heading = await openSetup()
    expect(api.cloudPreflight).not.toHaveBeenCalled()
    // Nothing was rejected, so there is no error surface to hand off.
    expect(within(heading.closest('section') as HTMLElement).queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    expectNoCredentialBlame()
  })

  it('keeps the AWS checklist on a POSIX gateway', async () => {
    vi.mocked(api.cloudPreflight).mockResolvedValue({
      reachable: true, account: '1234•••7890', arn: 'arn:aws:iam::x:user/dev',
      ec2_reachable: true, cloudformation_reachable: true, ssm_reachable: true,
      session_manager_plugin: true, note: '', detail: '',
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('radio', { name: /Set up a new one/i }))
    expect(await screen.findByRole('button', { name: /^Launch$/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /Windows dashboard/i })).not.toBeInTheDocument()
  })
})
