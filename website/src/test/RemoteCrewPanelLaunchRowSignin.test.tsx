/**
 * A launch waiting on the reader's Kiro sign-in, seen from My Crews.
 *
 * The dead end this pins: the row said "Setting up · 3 of 4" with a spinner on
 * "Sign in to Kiro", and the code the reader had to approve was only in the
 * Add Crew modal. Nothing on the row said the launch was waiting for them.
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
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})
import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

const AWS_EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [],
}

const WAITING_JOB = {
  id: 'j-wait', tag: 'kc-7a21cd', instance_id: 'i-0def456789abc0123',
  profile: '', region: 'us-east-1', provider_id: 'aws_ec2', size_key: 'balanced',
  status: 'awaiting_signin' as const,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
    { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'done' as const },
    { key: 'signin', label: 'Sign in to Kiro', state: 'active' as const },
    { key: 'connect', label: 'Connect', state: 'pending' as const },
  ],
  signin: { url: 'https://view.awsapps.com/start/#/device?user_code=WXYZ-1234', code: 'WXYZ-1234' },
  signin_detected: false, created_at: 0, updated_at: 0,
}

const useJob = (job: unknown) => {
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [job] } as never)
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(job as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] } as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: null, suggested_target: null, discovery: 'read' } as never)
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
  vi.mocked(api.cloudLaunchSignin).mockResolvedValue({ signin: WAITING_JOB.signin } as never)
  useJob(WAITING_JOB)
})

describe('a launch waiting for the Kiro sign-in, on its My Crews row', () => {
  it('shows the code, copy and the sign-in link without opening the row', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    const row = await screen.findByTestId('setting-up-row')

    // The status says whose turn it is, not "Setting up".
    expect(within(row).getByTestId('setting-up-status')).toHaveTextContent('Waiting for you to sign in · 3 of 4')
    expect(within(row).getByTestId('setting-up-status')).toHaveAttribute('aria-expanded', 'false')

    const block = await within(row).findByTestId('signin-prompt')
    expect(within(block).getByRole('link', { name: /Open sign-in page/ })).toHaveAttribute('href', WAITING_JOB.signin.url)
    await u.click(within(block).getByTestId('signin-code-copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('WXYZ-1234')
  })

  it('announces the code once in a polite status region', async () => {
    renderWithProviders(<RemoteCrewPanel />)
    const row = await screen.findByTestId('setting-up-row')
    const live = within(row).getByTestId('setting-up-signin-announce')
    expect(live).toHaveAttribute('role', 'status')
    expect(live).toHaveTextContent(/is waiting for you to sign in\. Your code is WXYZ-1234\./)
  })

  it('marks the sign-in step as waiting on the reader, not spinning', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    const row = await screen.findByTestId('setting-up-row')
    await u.click(within(row).getByTestId('setting-up-status'))
    expect(within(row).getByTestId('setting-up-step-waiting-signin')).toBeInTheDocument()
  })

  it('keeps the plain setting-up row while the machine is still being created', async () => {
    useJob({
      ...WAITING_JOB,
      status: 'running',
      signin: null,
      steps: WAITING_JOB.steps.map(s => (s.key === 'provision' ? { ...s, state: 'active' } : s.key === 'signin' ? { ...s, state: 'pending' } : s)),
    })
    renderWithProviders(<RemoteCrewPanel />)
    const row = await screen.findByTestId('setting-up-row')
    await waitFor(() => expect(within(row).getByTestId('setting-up-status')).toHaveTextContent('Setting up · 2 of 4'))
    expect(within(row).queryByTestId('signin-prompt')).toBeNull()
    expect(api.cloudLaunchSignin).not.toHaveBeenCalled()
  })
})
