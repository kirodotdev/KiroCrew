/**
 * RemoteCrewPanel — coverage for the paths the behavioural suite leaves alone:
 * the instance CRUD mutations (connect / disconnect / remove / diagnose) and
 * every mutation's failure branch, the copy-to-clipboard affordances and their
 * 1.5s revert, the enable-the-feature gate, the sign-in fetch when a job carries
 * no prompt, a failed launch's card, and the pruning of a finished teardown.
 *
 * Kept separate from RemoteCrewPanel.test.tsx so the behavioural file stays a
 * readable record of the defects it was written for.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createTestStore, renderWithProviders } from './helpers'
import type {
  CloudPreflight,
  InstanceView,
  InstanceTunnelStatus,
  LaunchJob,
} from '../api/client'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})
// The real helper falls back to a textarea + execCommand when the Clipboard API
// is absent, and happy-dom implements neither — the fallback would reject and
// surface as an unhandled rejection, which reddens CI with a green summary.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(undefined),
  copyCode: vi.fn().mockResolvedValue(undefined),
}))

import { api, ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

const CLOUD_INSTANCE: InstanceView = {
  id: 'kc1',
  name: 'Kiro Crew Cloud (kc-3f9a)',
  connection_method: 'ssm',
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: 'Admin',
  aws_region: 'us-west-2',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  status: { instance_id: 'i-0abc123456789def0', state: 'connected' },
}
const MANUAL_INSTANCE: InstanceView = {
  id: 'm1',
  name: 'dev-box-1',
  connection_method: 'ssh',
  ssm_target: '',
  ssh_host: 'dev-box-1',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'm1', state: 'disconnected' },
}
const CONNECTED_MANUAL: InstanceView = {
  ...MANUAL_INSTANCE,
  status: { instance_id: 'm1', state: 'connected' },
}
const DONE_JOB: LaunchJob = {
  id: 'j-done',
  tag: 'kc-3f9a',
  instance_id: 'i-0abc123456789def0',
  profile: 'Admin',
  region: 'us-west-2',
  size_key: 'balanced',
  status: 'done',
  steps: [],
  signin: null,
  created_at: 0,
  updated_at: 0,
}
const RUNNING_JOB: LaunchJob = {
  id: 'j-run',
  tag: 'kc-4d10',
  profile: '',
  region: 'us-east-1',
  size_key: 'light',
  status: 'running',
  signin: null,
  created_at: 0,
  updated_at: 0,
  steps: [
    { key: 'preflight', label: 'Checked your AWS setup', state: 'done' },
    { key: 'provision', label: 'Created the instance', state: 'active' },
    { key: 'connect', label: 'Connect', state: 'pending' },
  ],
}
const PREFLIGHT_OK: CloudPreflight = {
  reachable: true,
  account: '1234•••7890',
  arn: 'arn:aws:iam::x:user/dev',
  ec2_reachable: true,
  cloudformation_reachable: true,
  ssm_reachable: true,
  session_manager_plugin: true,
  note: '',
  detail: '',
}

const list = (instances: InstanceView[], extra: { active?: boolean; warm_set_cap?: number } = {}) => ({
  active: extra.active ?? true,
  warm_set_cap: extra.warm_set_cap ?? 5,
  instances,
})

/** A store whose instances slice already holds a warm connection for `id`. */
function storeWithWarm(id: string) {
  const base = createTestStore().getState()
  return createTestStore({
    ...base,
    instances: {
      ...base.instances,
      warm: { [id]: { port: 41234, token: 'stub' } },
      mru: [id],
      activeId: id,
    },
  })
}

const setup = () => userEvent.setup({ advanceTimers: (ms: number) => { vi.advanceTimersByTime(ms) } })

/** Open the "Set up a new one" tab. */
async function openSetupTab(u: ReturnType<typeof setup>) {
  await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // The copy affordances schedule a 1.5s revert. Without fake timers that
  // callback fires after teardown and throws as an unhandled error.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
  // The status query is enabled by the mere existence of a persisted job, so a
  // test that only cares about the crew list still polls it — an unmocked
  // resolve returns undefined, which React Query rejects noisily.
  vi.mocked(api.cloudLaunchStatus).mockResolvedValue(DONE_JOB)
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('RemoteCrewPanel — instance actions', () => {
  it('reports progress on the clicked Connect, then explains a connect that did not finish', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    let release: (v: InstanceTunnelStatus) => void = () => {}
    vi.mocked(api.connectInstance).mockReturnValue(
      new Promise<InstanceTunnelStatus>(r => { release = r }) as ReturnType<typeof api.connectInstance>,
    )
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Connect' }))
    // The busy key is per-instance, so the clicked row is the one that changes.
    expect(await screen.findByRole('button', { name: /Connecting/ })).toBeInTheDocument()

    // A resolved-but-not-connected tunnel with no error of its own falls back to
    // the "try Diagnose" hint rather than showing an empty banner.
    release({ instance_id: 'm1', state: 'error' })
    expect(await screen.findByText(/Connection did not complete/i, undefined, { timeout: 5_000 })).toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(/Connection did not complete/i)).not.toBeInTheDocument()
  })

  it('names the instance and the reason when Connect fails outright', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.connectInstance).mockRejectedValue(new ApiError(502, 'ssm tunnel refused'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Connect' }))
    expect(await screen.findByText(/Connect to m1 failed: ssm tunnel refused/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('drops the warm connection from the store when a crew is disconnected', async () => {
    // The warm entry holds the loopback port for a mounted pane. Leaving it
    // behind would keep a dead iframe in the switcher after the tunnel is gone.
    vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED_MANUAL]))
    vi.mocked(api.disconnectInstance).mockResolvedValue({ disconnected: 'm1', was_connected: true })
    const u = setup()
    const store = storeWithWarm('m1')
    renderWithProviders(<RemoteCrewPanel />, { store })

    await u.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(api.disconnectInstance).toHaveBeenCalledWith('m1'))
    await waitFor(() => expect(store.getState().instances.warm).not.toHaveProperty('m1'))
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('surfaces a failed disconnect instead of leaving the row looking disconnected', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED_MANUAL]))
    vi.mocked(api.disconnectInstance).mockRejectedValue(new Error('socket already gone'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText(/Disconnect of m1 failed: socket already gone/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('removes a hand-added machine even when the pre-emptive disconnect fails', async () => {
    // Remove disconnects first and swallows that failure on purpose: a tunnel
    // that is already dead must not block unregistering the machine.
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.disconnectInstance).mockRejectedValue(new Error('not connected'))
    vi.mocked(api.removeInstance).mockResolvedValue({ removed: 'm1' } as never)
    const u = setup()
    const store = storeWithWarm('m1')
    renderWithProviders(<RemoteCrewPanel />, { store })

    await u.click(await screen.findByRole('button', { name: 'Remove dev-box-1' }))
    await waitFor(() => expect(api.removeInstance).toHaveBeenCalledWith('m1'))
    await waitFor(() => expect(store.getState().instances.warm).not.toHaveProperty('m1'))
  })

  it('surfaces a failed remove', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.disconnectInstance).mockResolvedValue({ disconnected: 'm1', was_connected: false })
    vi.mocked(api.removeInstance).mockRejectedValue(new ApiError(409, 'registry is locked'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Remove dev-box-1' }))
    expect(await screen.findByText(/Remove of m1 failed: registry is locked/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('shows the diagnosis reason as a dismissible note', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.instanceStatus).mockResolvedValue({
      instance_id: 'm1',
      state: 'error',
      diagnosis: { code: 'ssh_unreachable', ok: false, reason: 'host did not answer', probes: [] },
    } as never)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Diagnose dev-box-1' }))
    expect(await screen.findByText(/m1: host did not answer/, undefined, { timeout: 5_000 })).toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: 'Dismiss diagnosis' }))
    expect(screen.queryByText(/m1: host did not answer/)).not.toBeInTheDocument()
  })

  it('says nothing when a diagnosis comes back clean', async () => {
    // A healthy probe has no reason and no error, so there is nothing to report —
    // a note reading "m1: undefined" would be worse than silence.
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.instanceStatus).mockResolvedValue({ instance_id: 'm1', state: 'connected' } as never)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Diagnose dev-box-1' }))
    await waitFor(() => expect(api.instanceStatus).toHaveBeenCalledWith('m1', true))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('surfaces a failed diagnose', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.instanceStatus).mockRejectedValue(new Error('probe blew up'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Diagnose dev-box-1' }))
    expect(await screen.findByText(/Diagnose of m1 failed: probe blew up/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('actually unregisters an unverifiable SSM crew once the removal is confirmed', async () => {
    // The confirm step exists because Remove leaves a real cloud instance
    // running; it must still complete when the user goes through with it.
    vi.mocked(api.listInstances).mockResolvedValue(list([CLOUD_INSTANCE]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.disconnectInstance).mockResolvedValue({ disconnected: 'kc1', was_connected: true })
    vi.mocked(api.removeInstance).mockResolvedValue({ removed: 'kc1' } as never)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Remove Kiro Crew Cloud/ }))
    expect(await screen.findByText(/keeps running and billing/i)).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /Remove Kiro Crew Cloud/ }))
    await waitFor(() => expect(api.removeInstance).toHaveBeenCalledWith('kc1'))
  })

  it('carries the crew AWS coordinates into a lifecycle call, and reports a failed Stop', async () => {
    // A crew launched under a non-default profile/region is invisible to the
    // gateway defaults, so the coordinates come off the row itself.
    vi.mocked(api.listInstances).mockResolvedValue(list([CLOUD_INSTANCE]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudStop).mockRejectedValue(new ApiError(404, 'stack kc-3f9a not found'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /^Stop Kiro Crew Cloud/ }))
    await waitFor(() =>
      expect(api.cloudStop).toHaveBeenCalledWith('kc-3f9a', {
        profile: 'Admin',
        region: 'us-west-2',
        instanceId: 'i-0abc123456789def0',
      }),
    )
    expect(await screen.findByText(/stack kc-3f9a not found/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('falls back to a generic message when a rejection is not an Error at all', async () => {
    // Nothing guarantees a thrown value is an Error; interpolating it blindly
    // would print "[object Object]" into the banner.
    vi.mocked(api.listInstances).mockResolvedValue(list([CLOUD_INSTANCE]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudStart).mockRejectedValue('nope')
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /^Start Kiro Crew Cloud/ }))
    expect(await screen.findByText('unknown error', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('clears the Deleting… state once the teardown drops the row', async () => {
    // The pending tag is pruned off the instance list, not a timer: the row (and
    // its Deleting… button) must disappear on its own when AWS confirms.
    let rows: InstanceView[] = [CLOUD_INSTANCE]
    vi.mocked(api.listInstances).mockImplementation(async () => list(rows, { warm_set_cap: 0 }))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudDestroy).mockResolvedValue({ ok: true })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    // An absent cap falls back to the default rather than advertising "up to 0".
    expect(await screen.findByText(/Up to 5 stay warm at once/i)).toBeInTheDocument()

    await u.click(await screen.findByRole('button', { name: /^Delete Kiro Crew Cloud/ }))
    expect(await screen.findByText(/terminates the EC2 instance/i)).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /^Confirm deleting/ }))
    expect(await screen.findByRole('button', { name: /Deleting/, hidden: true })).toBeDisabled()

    rows = []
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/No crews yet/i, undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Deleting/ })).not.toBeInTheDocument()
  })

  it('reports a cancel that the gateway refuses', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [RUNNING_JOB] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchCancel).mockRejectedValue(new ApiError(409, 'too late to cancel'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Cancel setup of kc-4d10' }))
    expect(await screen.findByText(/too late to cancel/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('marks the in-progress row as cancelling while the request is open', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [RUNNING_JOB] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    let release: (v: LaunchJob) => void = () => {}
    vi.mocked(api.cloudLaunchCancel).mockReturnValue(
      new Promise<LaunchJob>(r => { release = r }) as ReturnType<typeof api.cloudLaunchCancel>,
    )
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    const cancel = await screen.findByRole('button', { name: 'Cancel setup of kc-4d10' })
    await u.click(cancel)
    await waitFor(() => expect(cancel).toBeDisabled())
    expect(cancel).toHaveTextContent(/Cancelling/)
    release({ ...RUNNING_JOB, status: 'cancelled' })
  })
})

describe('RemoteCrewPanel — AWS prerequisites', () => {
  it('marks Copied on the install command, then reverts on its own', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudPreflight).mockResolvedValue({
      ...PREFLIGHT_OK,
      session_manager_plugin: false,
      session_manager_plugin_command: 'sudo dnf install -y https://example.invalid/smp.rpm',
    })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    await u.click(await screen.findByRole('button', { name: /Copy command/ }))
    expect(copyToClipboard).toHaveBeenCalledWith('sudo dnf install -y https://example.invalid/smp.rpm')
    expect(await screen.findByRole('button', { name: /Copied/ })).toBeInTheDocument()

    // The confirmation is transient, so the button becomes usable again.
    await act(async () => { vi.advanceTimersByTime(1_600) })
    expect(await screen.findByRole('button', { name: /Copy command/ })).toBeInTheDocument()
  })

  it('fetches the IAM policy on demand and confirms the copy', async () => {
    // The policy is not shipped to the browser: a missing-permission user asks
    // the gateway for it at the moment they need to paste it.
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudPreflight).mockResolvedValue({ ...PREFLIGHT_OK, cloudformation_reachable: false })
    vi.mocked(api.cloudIamPolicy).mockResolvedValue({ policy: '{"Version":"2012-10-17"}' })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    await u.click(await screen.findByRole('button', { name: /Copy policy JSON/ }))
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('{"Version":"2012-10-17"}'))
    expect(await screen.findByRole('button', { name: /Copied/ })).toBeInTheDocument()
  })

  it('surfaces a failure to fetch the IAM policy rather than silently copying nothing', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudPreflight).mockResolvedValue({ ...PREFLIGHT_OK, cloudformation_reachable: false })
    vi.mocked(api.cloudIamPolicy).mockRejectedValue(new ApiError(500, 'policy render failed'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    await u.click(await screen.findByRole('button', { name: /Copy policy JSON/ }))
    expect(await screen.findByText(/policy render failed/, undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('re-probes the region the user just typed, not the remembered one', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)
    await waitFor(() => expect(api.cloudPreflight).toHaveBeenCalledWith(undefined, 'us-east-1'))

    const regionInput = await screen.findByLabelText('Region')
    await u.clear(regionInput)
    await u.type(regionInput, 'eu-west-1')
    await u.tab()  // blur commits the value the probe runs against

    await waitFor(() => expect(api.cloudPreflight).toHaveBeenCalledWith(undefined, 'eu-west-1'))
    expect(await screen.findByText(/in eu-west-1/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('explains an unreachable account and keeps Launch out of reach', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudPreflight).mockResolvedValue({
      ...PREFLIGHT_OK,
      reachable: false,
      account: '',
      ec2_reachable: false,
      cloudformation_reachable: false,
      ssm_reachable: false,
      session_manager_plugin: false,
      detail: 'Credentials for this profile have expired.',
    })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    expect(await screen.findByText('Credentials for this profile have expired.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch' })).toBeDisabled())
    expect(screen.getByText(/Finish the AWS setup above/i)).toBeInTheDocument()
  })

  it('offers a re-check when the preflight request itself fails', async () => {
    // No preflight object at all means the checklist cannot render — the panel
    // must still say why and give the user a way to try again.
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudPreflight).mockRejectedValue(new ApiError(500, 'sts call timed out'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    expect(await screen.findByText(/sts call timed out/, undefined, { timeout: 5_000 })).toBeInTheDocument()
    const recheck = await screen.findByRole('button', { name: /Re-check/ })
    await u.click(recheck)
    await waitFor(() => expect(vi.mocked(api.cloudPreflight).mock.calls.length).toBeGreaterThan(1))
  })
})

describe('RemoteCrewPanel — launching', () => {
  it('surfaces a rejected launch', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunch).mockRejectedValue(new ApiError(500, 'no capacity for m7g.2xlarge'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    const launch = await screen.findByRole('button', { name: 'Launch' })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    expect(await screen.findByText(/no capacity for m7g.2xlarge/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('fetches the device-code prompt when the job is waiting but carries none', async () => {
    // The prompt lives on the gateway; a job that reached awaiting_signin before
    // this tab existed has nothing to render until the dashboard asks for it.
    const waiting: LaunchJob = { ...RUNNING_JOB, status: 'awaiting_signin', signin: null }
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [waiting] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(waiting)
    vi.mocked(api.cloudLaunchSignin).mockResolvedValue({
      signin: { url: 'https://device.sso/verify', code: 'ABCD-9999' },
    })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    await u.click(await screen.findByRole('button', { name: /Open sign-in page/ }))
    await waitFor(() => expect(api.cloudLaunchSignin).toHaveBeenCalledWith('j-run'))
  })

  it('surfaces a refused sign-in fetch', async () => {
    const waiting: LaunchJob = { ...RUNNING_JOB, status: 'awaiting_signin', signin: null }
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [waiting] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(waiting)
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new ApiError(409, 'no pending sign-in'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    await u.click(await screen.findByRole('button', { name: /Open sign-in page/ }))
    expect(await screen.findByText(/no pending sign-in/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('shows a failed launch with its step detail and no dead Cancel', async () => {
    // A terminal job cannot be cancelled, and the step that broke carries the
    // only server-authored explanation the user will get.
    const failed: LaunchJob = {
      ...RUNNING_JOB,
      id: 'j-failed',
      status: 'failed',
      error: 'CloudFormation rolled the stack back',
      steps: [
        { key: 'preflight', label: 'Checked your AWS setup', state: 'done' },
        { key: 'provision', label: 'Created the instance', state: 'failed', detail: 'InsufficientInstanceCapacity' },
        { key: 'connect', label: 'Connect', state: 'pending' },
      ],
    }
    vi.mocked(api.listInstances).mockResolvedValue(list([]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [failed] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(failed)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetupTab(u)

    expect(await screen.findByText('Launch failed')).toBeInTheDocument()
    expect(screen.getByText('InsufficientInstanceCapacity')).toBeInTheDocument()
    expect(screen.getByText('CloudFormation rolled the stack back')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Cancel setup of/ })).not.toBeInTheDocument()
  })
})

describe('RemoteCrewPanel — disabled feature gate', () => {
  it('enables the feature, reports progress, then asks for a restart', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'instances feature is disabled'))
    let release: (v: unknown) => void = () => {}
    vi.mocked(api.patchConfig).mockReturnValue(new Promise(r => { release = r }) as never)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    const enable = await screen.findByRole('button', { name: /Enable remote crew management/ })
    await u.click(enable)
    await waitFor(() => expect(screen.getByRole('button', { name: /Enabling/ })).toBeDisabled())
    expect(api.patchConfig).toHaveBeenCalledWith('instances.enabled', true)

    release({ ok: true })
    // The flag is on but the running gateway never opened its tunnels, so a
    // restart is the next required step rather than an optional suggestion.
    expect(await screen.findByRole('status', undefined, { timeout: 5_000 })).toHaveTextContent(/Restart the gateway/i)
  })

  it('surfaces a failure to write the config', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'instances feature is disabled'))
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(423, 'config is locked'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Enable remote crew management/ }))
    expect(await screen.findByText(/config is locked/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('treats a non-403 failure as a load error, not a disabled feature', async () => {
    // Only a 403 mentioning "disabled" is the gate. Any other failure must keep
    // the panel intact so the user is not told to enable something already on.
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'owner only'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('owner only', undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Enable remote crew management/ })).not.toBeInTheDocument()
    await u.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(1))
  })
})
