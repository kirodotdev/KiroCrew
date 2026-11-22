/**
 * A cloud launch cancelled after its instance exists.
 *
 * The gateway saves the job `cancelled` at once and then spends minutes deleting
 * the stack, holding the provision step `active` until AWS answers. The card must
 * say the launch was cancelled (never "Setting up"), keep re-reading the job
 * while that step is active, and stop once the removal has finished or failed,
 * so the reader learns whether the instance is still billing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

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

const TAG = 'kc-c4nce1'
// Verbatim from `launch_job.py`'s rollback.
const REMOVING = `Removing ${TAG}…`
const REMOVED = `Removed ${TAG} after cancellation.`
const UNCONFIRMED_ERROR =
  `Cancelled, and the delete of EC2 stack ${TAG} was requested but did NOT confirm `
  + '(it may be DELETE_FAILED). Check your crews — it may still be running and billing.'
const RUNS_ON_GATEWAY = /This runs on the gateway, not in this tab/

/** The job as the gateway saves it, with the provision step in `state`. */
const cancelledJob = (state: 'active' | 'done' | 'failed', detail: string, error = '') => ({
  id: 'j-cancelled', tag: TAG, instance_id: 'i-0abc123456789def0',
  profile: '', region: 'us-east-1', provider_id: 'aws_ec2', size_key: 'balanced',
  status: 'cancelled' as const,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
    { key: 'provision', label: 'Create the instance', state, detail },
    { key: 'signin', label: 'Sign in to Kiro', state: 'skipped' as const },
    { key: 'connect', label: 'Connect', state: 'pending' as const },
  ],
  signin: null, signin_detected: false, error, created_at: 0, updated_at: 0,
})

/** One status read set to `job`, and the launches list answering the same job. */
const serve = (job: unknown) => {
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [job] } as never)
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(job as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] } as never)
  vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue(
    { identity: null, suggested_target: null, discovery: 'read' } as never,
  )
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
})
afterEach(() => { vi.useRealTimers() })

const openSetup = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'Add Crew' }))
  fireEvent.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
}

describe('a launch cancelled after its instance exists — the progress card', () => {
  it('says Cancelled, keeps re-reading while the removal runs, and stops once it is removed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    serve(cancelledJob('active', REMOVING))
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup()

    expect(await screen.findByText(REMOVING)).toBeInTheDocument()
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
    expect(screen.queryByText('Setting up')).toBeNull()
    // The removal does keep going on the gateway, so the footer still says so.
    expect(screen.getByText(RUNS_ON_GATEWAY)).toBeInTheDocument()

    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(cancelledJob('done', REMOVED) as never)
    await act(() => vi.advanceTimersByTimeAsync(3_100))
    expect(await screen.findByText(REMOVED)).toBeInTheDocument()
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
    expect(screen.queryByText('Setting up')).toBeNull()
    await waitFor(() => expect(screen.queryByText(RUNS_ON_GATEWAY)).toBeNull())

    // Nothing is left to happen to the job: no further reads.
    const settled = vi.mocked(api.cloudLaunchStatus).mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(api.cloudLaunchStatus).toHaveBeenCalledTimes(settled)
  })

  it('shows the error when the removal failed, under the Cancelled badge', async () => {
    serve(cancelledJob('failed', REMOVING, UNCONFIRMED_ERROR))
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup()

    expect(await screen.findByText(/did NOT confirm/)).toBeInTheDocument()
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
    expect(screen.queryByText('Setting up')).toBeNull()
    expect(screen.queryByText(RUNS_ON_GATEWAY)).toBeNull()
  })
})
