/**
 * The launch form's custom instance type: the list of types the Region offers is
 * read only once the section is opened, says that offered is not in stock, and a
 * picked type goes out as `instance_type` instead of the tier. Picking a tier
 * again drops it.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { __resetErrorJournalForTests } from '../utils/errorReport'

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
      cloudInstanceTypes: vi.fn(),
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
  id: 'aws_ec2', kind: 'aws_ec2', label: 'AWS EC2 in your own account', posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup' },
    { key: 'provision', label: 'Create the instance' },
    { key: 'connect', label: 'Connect' },
  ],
}
const RUNNING_JOB = {
  id: 'j-run', tag: 'kc-4d10', profile: '', region: 'us-east-1', size_key: 'balanced',
  provider_id: 'aws_ec2', status: 'running' as const, signin: null, created_at: 0, updated_at: 0,
  steps: [{ key: 'provision', label: 'Create the instance', state: 'active' as const }],
}
const TYPES = {
  region: 'us-east-1',
  instance_types: [
    { instance_type: 'c7g.2xlarge', vcpu: 8, memory_gib: 16, arch: 'arm64' as const, zones: ['us-east-1c'] },
    { instance_type: 'm7i.xlarge', vcpu: 4, memory_gib: 16, arch: 'x86_64' as const, zones: ['us-east-1a', 'us-east-1b'] },
    { instance_type: 't4g.xlarge', vcpu: 4, memory_gib: 16, arch: 'arm64' as const, zones: ['us-east-1a'] },
  ],
}
const CUSTOM = 'Custom instance type'

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
  vi.mocked(api.cloudInstanceTypes).mockResolvedValue(TYPES)
  vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' },
  })
})

async function openSetup(u: ReturnType<typeof userEvent.setup>) {
  renderWithProviders(<RemoteCrewPanel />)
  await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
  await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
}

describe('RemoteCrewPanel custom instance type', () => {
  it('reads the offered types only when opened, and names the 16 GB floor and that listed is not in stock', async () => {
    const u = userEvent.setup()
    await openSetup(u)

    const toggle = await screen.findByRole('button', { name: CUSTOM })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(api.cloudInstanceTypes).not.toHaveBeenCalled()

    await u.click(toggle)
    await waitFor(() => expect(api.cloudInstanceTypes).toHaveBeenCalledWith('', 'us-east-1'))
    expect(screen.getByText(/Only types with at least 16 GB memory are listed, the least Kiro Crew needs. Listed does not mean in stock/)).toBeInTheDocument()
    // The rows carry no price, so the price link is there before any pick.
    expect(screen.getByRole('link', { name: 'See on-demand prices' })).toHaveAttribute('href', 'https://aws.amazon.com/ec2/pricing/on-demand/')
    const list = await screen.findByRole('radiogroup', { name: CUSTOM })
    const rows = within(list).getAllByRole('radio')
    expect(rows).toHaveLength(3)
    expect(within(list).getByText('16 GB memory · 4 vCPU · Intel/AMD · zones offering it: 2')).toBeInTheDocument()

    const filter = screen.getByRole('searchbox', { name: 'Filter instance types' })
    expect(filter).toHaveAttribute('placeholder', 'Filter, e.g. m7i')
    await u.type(filter, 'm7i')
    expect(within(list).getAllByRole('radio')).toHaveLength(1)
  })

  it('launches the picked type instead of the tier, and a tier pick drops it', async () => {
    const u = userEvent.setup()
    await openSetup(u)
    await u.click(await screen.findByRole('button', { name: CUSTOM }))
    const list = await screen.findByRole('radiogroup', { name: CUSTOM })
    await u.click(within(list).getAllByRole('radio')[1])

    expect(within(list).getByRole('radio', { name: 'm7i.xlarge' })).toBeChecked()
    // No tier card reads as chosen while the custom type drives the launch.
    expect(screen.queryAllByRole('button', { pressed: true })).toHaveLength(0)

    const customJob = { ...RUNNING_JOB, size_key: 'custom', instance_type: 'm7i.xlarge' }
    vi.mocked(api.cloudLaunch).mockResolvedValue(customJob)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(customJob)
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({
      provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced', instance_type: 'm7i.xlarge',
    }))
    // The progress card names the type the launch runs, not a tier.
    expect(await screen.findByText('Custom type m7i.xlarge')).toBeInTheDocument()

    // Picking a tier card drops the custom type.
    await u.click(screen.getByRole('button', { name: 'Development · arm64' }))
    expect(screen.getByRole('radio', { name: 'm7i.xlarge' })).not.toBeChecked()
    expect(screen.getAllByRole('button', { pressed: true })).toHaveLength(1)
  })

  it('names the picked type on the toggle, so a collapsed section still shows what launches', async () => {
    const u = userEvent.setup()
    await openSetup(u)
    await u.click(await screen.findByRole('button', { name: CUSTOM }))
    await u.click(await screen.findByRole('radio', { name: 'm7i.xlarge' }))
    const toggle = screen.getByRole('button', { name: `${CUSTOM} m7i.xlarge` })
    await u.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('radiogroup', { name: CUSTOM })).not.toBeInTheDocument()
    expect(within(toggle).getByText('m7i.xlarge')).toBeInTheDocument()
  })

  it('offers and sends a custom type on the built-in lane only', async () => {
    // A second aws_ec2-kind lane draws the same form, but the gateway refuses
    // `instance_type` for any id but the built-in one.
    const GOV_ROW = { ...AWS_EC2_ROW, id: 'ec2_gov', label: 'AWS EC2 GovCloud' }
    vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW, GOV_ROW] })
    const u = userEvent.setup()
    await openSetup(u)
    await u.click(await screen.findByRole('button', { name: AWS_EC2_ROW.label }))
    await u.click(await screen.findByRole('button', { name: CUSTOM }))
    await u.click(await screen.findByRole('radio', { name: 'm7i.xlarge' }))

    await u.click(screen.getByRole('button', { name: GOV_ROW.label }))
    expect(screen.queryByRole('button', { name: new RegExp(CUSTOM) })).not.toBeInTheDocument()
    // The tier drives the launch again, so its card reads as chosen.
    expect(screen.getAllByRole('button', { pressed: true }).length).toBeGreaterThan(0)
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalled())
    expect(vi.mocked(api.cloudLaunch).mock.calls[0][0]).toEqual(expect.objectContaining({ provider_id: 'ec2_gov' }))
    expect(vi.mocked(api.cloudLaunch).mock.calls[0][0]).not.toHaveProperty('instance_type')
  })

  it('keeps the picked type across the hand-off to the agent', async () => {
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
    await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    await u.click(await screen.findByRole('button', { name: CUSTOM }))
    await u.click(await screen.findByRole('radio', { name: 'm7i.xlarge' }))
    // The agent hand-off navigates to chat, which unmounts the panel.
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
    await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    expect(await screen.findByRole('radio', { name: 'm7i.xlarge' })).toBeChecked()
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith(expect.objectContaining({ instance_type: 'm7i.xlarge' })))
  })

  it('names the failure when the list cannot be read', async () => {
    vi.mocked(api.cloudInstanceTypes).mockRejectedValue(new Error('AccessDenied'))
    const u = userEvent.setup()
    await openSetup(u)
    await u.click(await screen.findByRole('button', { name: CUSTOM }))
    expect(await screen.findByText('Could not read the instance types this Region offers: AccessDenied')).toBeInTheDocument()
  })
})
