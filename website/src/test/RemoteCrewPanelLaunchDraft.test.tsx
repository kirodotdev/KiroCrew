/**
 * The launch form's unlaunched subnet and Kiro sign-in choice survive the agent
 * hand-off. "Ask the agent" navigates to chat and unmounts the panel, so these
 * tests unmount it and render it again against the same store, which is what
 * that navigation does.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import reducer, { dropLaunchedCrewDraft, setCrewLaunchDraft } from '../store/instancesSlice'

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

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } })
})

describe('RemoteCrewPanel launch draft', () => {
  it('keeps a typed subnet and Identity Center entry across the hand-off, over the inherited sign-in', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudIdentity).mockResolvedValue({
      identity: { account_type: 'IamIdentityCenter', start_url: 'https://example.awsapps.com/start' },
      suggested_target: { license: 'pro', start_url: 'https://example.awsapps.com/start', region: '' },
    })
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    await u.type(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' }), 'subnet-0123abcd')
    const url = await screen.findByRole('textbox', { name: /Identity Center start URL/i })
    await waitFor(() => expect(url).toHaveValue('https://example.awsapps.com/start'))
    await u.clear(url)
    await u.type(url, 'https://other.awsapps.com/start')
    await u.type(screen.getByRole('textbox', { name: /Identity Center region/i }), 'eu-west-1')
    // The agent hand-off navigates to chat, which unmounts the panel.
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    expect(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' })).toHaveValue('subnet-0123abcd')
    expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked()
    // The inherited portal arrives after mount and must not replace the typed one.
    await waitFor(() => expect(api.cloudIdentity).toHaveBeenCalled())
    expect(screen.getByRole('textbox', { name: /Identity Center start URL/i })).toHaveValue('https://other.awsapps.com/start')
    expect(screen.getByRole('textbox', { name: /Identity Center region/i })).toHaveValue('eu-west-1')

    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({
      provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced', subnet_id: 'subnet-0123abcd',
      login_target: { license: 'pro', start_url: 'https://other.awsapps.com/start', region: 'eu-west-1' },
    }))
  })

  it('keeps an explicit Builder ID choice across the hand-off', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudIdentity).mockResolvedValue({
      identity: { account_type: 'IamIdentityCenter', start_url: 'https://example.awsapps.com/start' },
      suggested_target: { license: 'pro', start_url: 'https://example.awsapps.com/start', region: '' },
    })
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    await waitFor(() => expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked())
    await u.click(screen.getByRole('radio', { name: /Builder ID/i }))
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    await waitFor(() => expect(api.cloudIdentity).toHaveBeenCalled())
    expect(await screen.findByRole('radio', { name: /Builder ID/i })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Company SSO/i })).not.toBeChecked()
  })

  it('drops the subnet and sign-in drafts once a launch has used them', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    await u.type(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' }), 'subnet-0123abcd')
    await u.click(screen.getByRole('radio', { name: /Company SSO/i }))
    await u.type(screen.getByRole('textbox', { name: /Identity Center start URL/i }), 'https://other.awsapps.com/start')
    await u.type(screen.getByRole('textbox', { name: /Identity Center region/i }), 'eu-west-1')
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    expect(await screen.findByText('Installing Kiro Crew')).toBeInTheDocument()
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    expect(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' })).toHaveValue('')
    // Back to this computer's own sign-in (Builder ID in the default mock).
    await waitFor(() => expect(screen.getByRole('radio', { name: /Builder ID/i })).toBeChecked())
    expect(screen.queryByRole('textbox', { name: /Identity Center start URL/i })).toBeNull()
  })

  it('says the kept choice is the reader’s own when this computer’s sign-in could not be read', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudIdentity).mockRejectedValue(new Error('identity lookup failed'))
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    expect(await screen.findByText(/Choose one below to enable Launch/)).toBeInTheDocument()
    await u.click(screen.getByRole('radio', { name: /Company SSO/i }))
    // Once picked, the notice no longer asks for a choice that is on screen.
    expect(screen.queryByText(/Choose one below to enable Launch/)).toBeNull()
    expect(screen.getByText(/The new crew will sign in with the option you picked below\./)).toBeInTheDocument()
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    const notice = (await screen.findByText(/The new crew will sign in with the option you picked below\./)).closest('[role="alert"]') as HTMLElement
    expect(notice).not.toHaveTextContent(/Choose one below/)
    expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked()
  })

  it('keeps an edit typed while the launch request is in flight', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    let finish: (job: typeof RUNNING_JOB) => void = () => {}
    vi.mocked(api.cloudLaunch).mockReturnValue(new Promise(resolve => { finish = resolve }) as never)
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    const subnet = await screen.findByRole('textbox', { name: 'Subnet ID (optional)' })
    await u.type(subnet, 'subnet-aaaa')
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalled())
    // The next launch's subnet, typed before this one answers.
    await u.clear(subnet)
    await u.type(subnet, 'subnet-bbbb')
    finish(RUNNING_JOB)
    expect(await screen.findByText('Installing Kiro Crew')).toBeInTheDocument()
    first.unmount()

    renderWithProviders(<RemoteCrewPanel />, { store: first.store })
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    expect(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' })).toHaveValue('subnet-bbbb')
  })

  it('keeps the draft in memory only: nothing in browser storage, and a fresh tab starts empty', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    await u.type(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' }), 'subnet-0123abcd')
    await u.click(screen.getByRole('radio', { name: /Company SSO/i }))
    await u.type(screen.getByRole('textbox', { name: /Identity Center start URL/i }), 'https://other.awsapps.com/start')
    first.unmount()
    for (const storage of [window.localStorage, window.sessionStorage]) {
      expect(Object.keys(storage).filter(k => /subnet-0123abcd|other\.awsapps/.test(storage.getItem(k) ?? ''))).toEqual([])
    }

    // A reload or a new tab gets a new store.
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    expect(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' })).toHaveValue('')
    await waitFor(() => expect(screen.getByRole('radio', { name: /Builder ID/i })).toBeChecked())
  })
})

describe('instancesSlice launch draft', () => {
  const a = { subnet_id: 'subnet-a', identity: { mode: 'identity_center' as const, start_url: 'https://a.awsapps.com/start', region: 'us-east-1' } }

  it('drops the held draft only when it is the one the launch was sent with', () => {
    const held = reducer(undefined, setCrewLaunchDraft(a))
    expect(reducer(held, dropLaunchedCrewDraft({ ...a, identity: { ...a.identity } })).crewForms.launch).toBeNull()
    expect(reducer(held, dropLaunchedCrewDraft({ ...a, subnet_id: 'subnet-b' })).crewForms.launch).toEqual(a)
    expect(reducer(held, dropLaunchedCrewDraft({ ...a, identity: { ...a.identity, region: 'eu-west-1' } })).crewForms.launch).toEqual(a)
    expect(reducer(held, dropLaunchedCrewDraft({ subnet_id: 'subnet-a', identity: null })).crewForms.launch).toEqual(a)
  })

  it('drops nothing for a launch sent with no draft while one is held', () => {
    const held = reducer(undefined, setCrewLaunchDraft(a))
    expect(reducer(held, dropLaunchedCrewDraft(null)).crewForms.launch).toEqual(a)
    expect(reducer(reducer(undefined, setCrewLaunchDraft(null)), dropLaunchedCrewDraft(null)).crewForms.launch).toBeNull()
  })
})
