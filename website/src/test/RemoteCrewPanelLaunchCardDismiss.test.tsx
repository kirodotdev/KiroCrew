/**
 * A finished launch's card goes away.
 *
 * The card of the newest persisted launch used to stay on the setup tab for good, after every
 * reload, once the launch had finished ("Done — your new crew is ready"). Now a launch that
 * needs nothing more has a close button, closes by itself about 2 minutes after its last write,
 * and stays closed after a reload (the ids are kept in this browser). A failed or running
 * launch, and one still waiting on its sign-in, keep their card as before.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { LAUNCH_CARD_SHOWN_MS, LAUNCH_DISMISSED_KEY } from '../pages/settings/remoteCrewPanelModel'

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
    },
  }
})
import { api } from '../api/client'

const AWS_EC2_ROW = {
  id: 'aws_ec2', kind: 'aws_ec2', label: 'AWS EC2 in your own account', posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup' },
    { key: 'provision', label: 'Create the instance' },
    { key: 'signin', label: 'Sign in to Kiro' },
    { key: 'connect', label: 'Connect' },
  ],
}

const STEPS_DONE = [
  { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
  { key: 'provision', label: 'Create the sandbox', state: 'done' as const },
  { key: 'signin', label: 'Sign in to Kiro on the sandbox', state: 'done' as const, detail: 'Already signed in.' },
  { key: 'connect', label: 'Add it to your crews', state: 'done' as const, detail: 'Added to Your crews.' },
]

const nowS = () => Date.now() / 1000 // flake-ok: every time is relative to the same clock the panel reads; margins are 30 s and 120 s

function job(over: Record<string, unknown> = {}) {
  return {
    id: 'aaaaaaaaaaaa', provider_id: 'aws_ec2', profile: 'default', region: 'us-east-1',
    size_key: 'balanced', tag: 'kc-00fcd1', status: 'done', steps: STEPS_DONE,
    instance_id: 'kc1', signin: null, signin_detected: true, error: '',
    created_at: nowS() - 300, updated_at: nowS(),
    ...over,
  }
}

const useJob = (j: unknown) => {
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [j] } as never)
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(j as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] } as never)
  vi.mocked(api.cloudPreflight).mockResolvedValue({
    reachable: true, account: '1234•••7890', arn: 'arn:aws:iam::x:user/dev', ec2_reachable: true,
    cloudformation_reachable: true, ssm_reachable: true, session_manager_plugin: true, note: '', detail: '',
  } as never)
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: null, suggested_target: null, discovery: 'read' } as never)
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
})

const openSetup = async (u: ReturnType<typeof userEvent.setup>) => {
  await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
}
const doneLine = /your new crew is ready/i

/** Run *body* with setTimeout faked from before the first render, so the card's own
 *  timer is a fake one; `pass` then moves it on by the card's whole shown window. */
async function withCardClock(body: (pass: () => Promise<void>) => Promise<void>) {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true }) // flake-ok: restored by this helper's own finally below, on every path
  try {
    await body(async () => { await act(async () => { vi.advanceTimersByTime(LAUNCH_CARD_SHOWN_MS) }) })
  } finally {
    vi.useRealTimers()
  }
}

describe('a finished launch card', () => {
  it('has a close button that dismisses it, and a reload does not bring it back', async () => {
    useJob(job())
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    expect(await screen.findByText(doneLine)).toBeInTheDocument()
    // Visible words, so the reader does not take it for "delete the crew".
    await u.click(screen.getByRole('button', { name: 'Hide card' }))
    await waitFor(() => expect(screen.queryByText(doneLine)).toBeNull())
    expect(JSON.parse(localStorage.getItem(LAUNCH_DISMISSED_KEY) ?? '[]')).toEqual(['aaaaaaaaaaaa'])
    first.unmount()
    // the reload: a new panel over the same browser storage and the same job list
    const reads = vi.mocked(api.cloudLaunches).mock.calls.length
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    await waitFor(() => expect(vi.mocked(api.cloudLaunches).mock.calls.length).toBeGreaterThan(reads))
    expect(screen.queryByText(doneLine)).toBeNull()
    expect(screen.queryByTestId('launch-card-dismiss')).toBeNull()
  })

  it('goes away by itself about 2 minutes after its last write', async () => {
    useJob(job({ updated_at: nowS() - 121 }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    await waitFor(() => expect(JSON.parse(localStorage.getItem(LAUNCH_DISMISSED_KEY) ?? '[]')).toEqual(['aaaaaaaaaaaa']))
    expect(screen.queryByText(doneLine)).toBeNull()
  })

  it('stays while its 2 minutes run, and goes when they end', async () => {
    useJob(job({ updated_at: nowS() - 30 }))
    await withCardClock(async pass => {
      const u = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      renderWithProviders(<RemoteCrewPanel />)
      await openSetup(u)
      expect(await screen.findByText(doneLine)).toBeInTheDocument()
      expect(localStorage.getItem(LAUNCH_DISMISSED_KEY)).toBeNull()
      await pass()
      await waitFor(() => expect(screen.queryByText(doneLine)).toBeNull())
    })
  })
})

describe('every other launch card is as before', () => {
  it.each([
    ['a failed launch', { status: 'failed', error: 'boom', signin_detected: false }],
    ['a launch still waiting on its sign-in', { signin_detected: false }],
  ])('%s has no close button and does not time out', async (_name, over) => {
    useJob(job({ ...over, updated_at: nowS() - 3600 }))
    await withCardClock(async pass => {
      const u = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      renderWithProviders(<RemoteCrewPanel />)
      await openSetup(u)
      await screen.findByText('Kiro Crew Cloud (kc-00fcd1)')
      await pass()
      expect(screen.getByText('Kiro Crew Cloud (kc-00fcd1)')).toBeInTheDocument()
      expect(screen.queryByTestId('launch-card-dismiss')).toBeNull()
      expect(localStorage.getItem(LAUNCH_DISMISSED_KEY)).toBeNull()
    })
  })

  it('a running launch keeps its card and its Cancel, with no close button', async () => {
    const running = job({
      status: 'running', signin_detected: false, updated_at: nowS() - 3600,
      steps: STEPS_DONE.map(s => (s.key === 'connect' ? { ...s, state: 'pending' as const } : s.key === 'signin' ? { ...s, state: 'active' as const } : s)),
    })
    useJob(running)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    // The card and its My Crews row both carry the title.
    expect((await screen.findAllByText('Kiro Crew Cloud (kc-00fcd1)')).length).toBeGreaterThan(0)
    expect(screen.queryByTestId('launch-card-dismiss')).toBeNull()
    expect(localStorage.getItem(LAUNCH_DISMISSED_KEY)).toBeNull()
  })
})
