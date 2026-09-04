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
import { act, screen, waitFor, within } from '@testing-library/react'
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
    // The real class carries the raw response body so a caller can read the
    // structured `code` the human message collapses away — the panel branches
    // on it to tell an unsupported-platform refusal from a load failure.
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    // Mirrors the real predicate: the panel drops its refresh button only for an
    // auth denial, so the mock must distinguish one from any other ApiError.
    isAuthExpiredError: (e: unknown) =>
      e instanceof ApiError && (e as { authRequired?: boolean }).authRequired === true,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      updateInstance: vi.fn(),
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
  // The launch form's size now persists here (like its sibling profile/region), and
  // the hand-off channel is a queue nothing drains, so without this a value left by
  // one case leaks into the next.
  localStorage.clear()
  sessionStorage.clear()
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


/** Open a crew row's overflow menu — Edit / Stop / Start / Delete / Remove live there. */
async function openRowMenu(u: ReturnType<typeof setup>, name: RegExp = /More actions/i) {
  await u.click(await screen.findByRole('button', { name }))
}

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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Remove dev-box-1' }))
    // Every remove is confirm-gated: the menu label ends in an ellipsis because a
    // second step follows, and the record has no undo.
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Remove dev-box-1' }))
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Diagnose dev-box-1' }))
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Diagnose dev-box-1' }))
    await waitFor(() => expect(api.instanceStatus).toHaveBeenCalledWith('m1', true))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('surfaces a failed diagnose', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.instanceStatus).mockRejectedValue(new Error('probe blew up'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Diagnose dev-box-1' }))
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Remove Kiro Crew Cloud/ }))
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Stop Kiro Crew Cloud/ }))
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Start Kiro Crew Cloud/ }))
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

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Delete Kiro Crew Cloud/ }))
    expect(await screen.findByText(/terminates the EC2 instance/i)).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /^Confirm deleting/ }))
    expect(await screen.findByRole('button', { name: /Deleting/, hidden: true })).toBeDisabled()

    rows = []
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/No instances yet/i, undefined, { timeout: 5_000 })).toBeInTheDocument()
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

    const enable = await screen.findByRole('button', { name: /Enable remote instance management/ })
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

    await u.click(await screen.findByRole('button', { name: /Enable remote instance management/ }))
    expect(await screen.findByText(/config is locked/, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('treats a non-403 failure as a load error, not a disabled feature', async () => {
    // Only a 403 mentioning "disabled" is the gate. Any other failure must keep
    // the panel intact so the user is not told to enable something already on.
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'owner only'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('owner only', undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Enable remote instance management/ })).not.toBeInTheDocument()
    await u.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(1))
  })
})

describe('RemoteCrewPanel — editing a crew', () => {
  it('saves an edited host and port to the crew that was already configured', async () => {
    // Correcting a crew used to mean deleting it and adding it back, which threw
    // away the record (and its connect history) along with the typo.
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.updateInstance).mockResolvedValue({ ...MANUAL_INSTANCE, ssh_host: 'dev-box-2' })
    // The host changed, so the save reconnects on the new coordinates.
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'm1', state: 'connected', local_port: 7999, token: 't' })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))

    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const host = form.getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'dev-box-2')
    const port = form.getByRole('textbox', { name: /Remote port/i })
    await u.clear(port)
    await u.type(port, '7999')
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() =>
      expect(api.updateInstance).toHaveBeenCalledWith(
        'm1',
        expect.objectContaining({ ssh_host: 'dev-box-2', remote_port: 7999 }),
      ),
    )
    // The form closes on success rather than leaving a stale copy of the row open.
    await waitFor(() => expect(screen.queryByRole('button', { name: /Save changes/i })).not.toBeInTheDocument())
  })

  it('surfaces a rejected save instead of closing as if it worked', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.updateInstance).mockRejectedValue(new ApiError(400, 'invalid ssh_host'))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    expect(await screen.findByText(/invalid ssh_host/, undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(form.getByRole('button', { name: /Save changes/i })).toBeInTheDocument()
  })

  it('actually clears an optional field the user emptied', async () => {
    // A PATCH is partial, so an omitted key means "leave as-is": emptying the
    // remote binary path has to travel as an explicit clear, or the crew keeps
    // launching through a path that no longer appears anywhere in the form.
    // (An SSM crew's profile/region are frozen instead — see the cloud-identity
    // test — because those ADDRESS the machine rather than describe it.)
    const withBin = { ...MANUAL_INSTANCE, remote_bin: '/opt/old/bin/kirocrew' }
    vi.mocked(api.listInstances).mockResolvedValue(list([withBin]))
    vi.mocked(api.updateInstance).mockResolvedValue({ ...withBin, remote_bin: '' })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    await u.clear(form.getByRole('textbox', { name: /Remote kirocrew path/i }))
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() =>
      expect(api.updateInstance).toHaveBeenCalledWith('m1', expect.objectContaining({ remote_bin: '' })),
    )
  })
  it('closes the tunnel on a transport change and leaves reconnecting to the user', async () => {
    // Any automatic reconnect races the user's own Disconnect, so a saved
    // transport change deliberately stops at "tunnel closed".
    const live = { ...MANUAL_INSTANCE, was_connected: true, status: { instance_id: 'm1', state: 'connected' as const, local_port: 7777 } }
    vi.mocked(api.listInstances).mockResolvedValue(list([live]))
    vi.mocked(api.updateInstance).mockResolvedValue({ ...live, remote_port: 7999 })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const port = form.getByRole('textbox', { name: /Remote port/i })
    await u.clear(port)
    await u.type(port, '7999')
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() =>
      expect(api.updateInstance).toHaveBeenCalledWith('m1', expect.objectContaining({ remote_port: 7999 })),
    )
    expect(api.connectInstance).not.toHaveBeenCalled()
  })

  it('will not let an edit rewrite the identity a cloud crew is tracked by', async () => {
    // A cloud crew is matched to its EC2 stack through its SSM target. Editing
    // that away would strand a billing machine the dashboard can no longer stop
    // or delete, and Remove would then unregister it silently.
    vi.mocked(api.listInstances).mockResolvedValue(list([CLOUD_INSTANCE]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.updateInstance).mockResolvedValue(CLOUD_INSTANCE)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for Kiro Crew Cloud/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit Kiro Crew Cloud/i }))
    // Everything stop/start/delete addresses the machine BY is frozen, not just
    // the instance id: a different profile or region points those calls at
    // another AWS account and leaves the real instance running.
    for (const name of [/SSM target/i, /AWS profile/i, /AWS region/i]) {
      expect(form.getByRole('textbox', { name })).toHaveAttribute('readonly')
    }
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
    const body = vi.mocked(api.updateInstance).mock.calls[0][1]
    for (const key of ['ssm_target', 'connection_method', 'aws_profile', 'aws_region']) {
      expect(body).not.toHaveProperty(key)
    }
  })

  it('refuses to save a port or lifetime the tunnel could never use', async () => {
    // Coercing an unparseable value would persist a port the user never chose,
    // or a TTL the token minter cannot read — so Save is gated instead.
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))

    const port = form.getByRole('textbox', { name: /Remote port/i })
    await u.clear(port)
    await u.type(port, 'abc')
    expect(await form.findByText(/between 1 and 65535/i)).toBeInTheDocument()
    expect(form.getByRole('button', { name: /Save changes/i })).toBeDisabled()

    await u.clear(port)
    await u.type(port, '70000')
    expect(form.getByRole('button', { name: /Save changes/i })).toBeDisabled()

    await u.clear(port)
    await u.type(port, '7999')
    const ttl = form.getByRole('textbox', { name: /Token TTL/i })
    await u.clear(ttl)
    await u.type(ttl, 'forever')
    expect(await form.findByText(/like 20h or 30m/i)).toBeInTheDocument()
    expect(form.getByRole('button', { name: /Save changes/i })).toBeDisabled()

    // Numeric but past the minters' four-digit bound: it would save and then
    // fail at the next connect rather than at the edit that caused it.
    await u.clear(ttl)
    await u.type(ttl, '99999h')
    expect(form.getByRole('button', { name: /Save changes/i })).toBeDisabled()

    // A valid pair re-enables it, and nothing was submitted meanwhile.
    await u.clear(ttl)
    await u.type(ttl, '30m')
    expect(form.getByRole('button', { name: /Save changes/i })).toBeEnabled()
    expect(api.updateInstance).not.toHaveBeenCalled()
  })

  it('sends only the fields the user changed, so a concurrent edit is not reverted', async () => {
    // The form is seeded once and can be minutes old. Sending every field would
    // make the later of two saves overwrite the earlier one's corrections — a
    // partial update carrying the whole record is a full overwrite in disguise.
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    vi.mocked(api.updateInstance).mockResolvedValue({ ...MANUAL_INSTANCE, remote_port: 7999 })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const port = form.getByRole('textbox', { name: /Remote port/i })
    await u.clear(port)
    await u.type(port, '7999')
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
    const body = vi.mocked(api.updateInstance).mock.calls[0][1]
    expect(body).toEqual({ remote_port: 7999 })
  })

  it('diffs against the record the form opened with, not a newer poll', async () => {
    // `inst` is fed by the instances poll. Diffing against the LIVE record would
    // compare the user's stale field values with someone else's newer ones, so a
    // change made from the CLI mid-edit would be overwritten by a field the user
    // never touched.
    let rows: InstanceView[] = [MANUAL_INSTANCE]
    vi.mocked(api.listInstances).mockImplementation(async () => list(rows))
    vi.mocked(api.updateInstance).mockResolvedValue(MANUAL_INSTANCE)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))

    // Someone else changes the host while this form sits open; the poll picks it up.
    rows = [{ ...MANUAL_INSTANCE, ssh_host: 'dev-box-moved' }]
    await act(async () => {
      await u.click(screen.getByRole('button', { name: 'Refresh' }))
    })

    // The user edits only the TTL. The externally-changed host is a machine
    // coordinate, so the form withholds Save until the user adopts the current
    // record — and the baseline diff must STILL hold afterwards: adopting the
    // record is not the same as sending it.
    const ttl = form.getByRole('textbox', { name: /Token TTL/i })
    await u.clear(ttl)
    await u.type(ttl, '4h')
    await u.click(await screen.findByRole('button', { name: /Apply my edits to the instance as it is now/i }))
    await u.click(await screen.findByRole('button', { name: /Save changes/i }))

    await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
    const body = vi.mocked(api.updateInstance).mock.calls[0][1]
    expect(body).toEqual({ ttl: '4h' })
    expect(body).not.toHaveProperty('ssh_host')
  })

  it('keeps the AWS profile editable on an SSM crew it cannot correlate to a stack', async () => {
    // Freezing the addressing fields is only load-bearing where lifecycle actions
    // exist. An uncorrelated crew is offered none, so a freeze there would protect
    // nothing and would remove the only way to fix its AWS profile.
    const ssm = {
      ...MANUAL_INSTANCE,
      connection_method: 'ssm' as const,
      ssm_target: 'i-0abc123456789def0',
      aws_profile: 'Stale',
      aws_region: 'us-west-2',
    }
    vi.mocked(api.listInstances).mockResolvedValue(list([ssm]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.updateInstance).mockResolvedValue({ ...ssm, aws_profile: 'Fixed' })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const profile = form.getByRole('textbox', { name: /AWS profile/i })
    expect(profile).not.toHaveAttribute('readonly')
    await u.clear(profile)
    await u.type(profile, 'Fixed')
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() =>
      expect(api.updateInstance).toHaveBeenCalledWith('m1', expect.objectContaining({ aws_profile: 'Fixed' })),
    )
  })

  it('shows a frozen field as frozen instead of letting the user type into nothing', async () => {
    // Identical styling on a read-only input invites a click, a few keystrokes,
    // and a late discovery that nothing landed.
    vi.mocked(api.listInstances).mockResolvedValue(list([CLOUD_INSTANCE]))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for Kiro Crew Cloud/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit Kiro Crew Cloud/i }))
    const target = form.getByRole('textbox', { name: /SSM target/i })
    expect(target).toHaveAttribute('aria-readonly', 'true')
    expect(target.className).toMatch(/cursor-not-allowed/)
  })

  it('lets the user back out of an armed destructive confirm', async () => {
    // While armed, the overflow menu is hidden — so without an exit a mis-click
    // on "Remove…" leaves the row showing nothing but a button with no undo.
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE]))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Remove dev-box-1' }))
    expect(await screen.findByRole('button', { name: 'Remove dev-box-1' })).toBeInTheDocument()

    // Two controls, not three: the primary action stands down while the row is
    // asking a destructive question (blocking `max-two-buttons-per-row`).
    expect(screen.queryByRole('button', { name: /^Connect$/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Disconnect$/ })).not.toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: /^Cancel$/ }))

    // Disarmed: the confirm is gone, nothing was removed, and the menu is back.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Remove dev-box-1' })).not.toBeInTheDocument(),
    )
    expect(api.removeInstance).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /More actions/i })).toBeInTheDocument()
  })

  it('refuses to swap an open edit form out from under unsaved work', async () => {
    // The form state is keyed by a single editingId, so opening another row would
    // unmount it and lose typed host/port corrections with no undo.
    const second = { ...MANUAL_INSTANCE, id: 'm2', name: 'dev-box-2', ssh_host: 'dev-box-2', remote_port: 7788 }
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE, second]))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const host = form.getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'dev-box-1-corrected')

    // Trying to edit the other crew is refused, with the reason stated.
    await openRowMenu(u, /More actions for dev-box-2/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))

    expect(await screen.findByText(/Save or cancel the open edit/i)).toBeInTheDocument()
    // The first form is still open, still holding the typed value.
    expect(screen.getByRole('group', { name: /Edit dev-box-1/i })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: /Edit dev-box-2/i })).not.toBeInTheDocument()
    expect((host as HTMLInputElement).value).toBe('dev-box-1-corrected')

    // The refusal must render at the row that was clicked, not once at the bottom
    // of the card: the menu closes on select, so a message the user has to scroll
    // to find is indistinguishable from the click having done nothing.
    const refusal = screen.getByRole('alert')
    expect(refusal).toHaveTextContent(/Save or cancel the open edit/i)
    expect(refusal.closest('[data-crew-id]')?.getAttribute('data-crew-id')).toBe('m2')
  })

  it('keeps a typed edit across a switch to the setup tab and back', async () => {
    // The crew list unmounts when the setup tab opens, so an edit whose only home
    // was the form's own state was silently reverted to the stored values on the
    // way back. A guard can only refuse the exits it enumerates; the draft lives
    // in the panel instead, so it survives the unmount rather than being defended
    // from it one exit at a time.
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const host = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
      .getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'dev-box-1-corrected')

    await u.click(screen.getByRole('button', { name: /Set up a new one/i }))
    expect(screen.queryByRole('group', { name: /Edit dev-box-1/i })).not.toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: /Your instances|Instances/i }))
    const reopened = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    expect((reopened.getByRole('textbox', { name: /SSH host/i }) as HTMLInputElement).value).toBe(
      'dev-box-1-corrected',
    )
  })

  it('keeps a restored draft anchored to the record it was typed against', async () => {
    // Preserving the draft across an unmount reintroduced the very hazard the
    // immutable baseline exists to prevent: on remount the baseline was re-derived
    // from the CURRENT `inst`, so a CLI change the poll picked up meanwhile became
    // a difference against the stale draft and would have been written back. The
    // baseline travels WITH the draft, so a field the user never touched is still
    // not a difference.
    let rows: InstanceView[] = [MANUAL_INSTANCE]
    vi.mocked(api.listInstances).mockImplementation(async () => list(rows))
    vi.mocked(api.updateInstance).mockResolvedValue(MANUAL_INSTANCE)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const host = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
      .getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'dev-box-1-corrected')

    // Someone else changes the port and the poll picks it up, THEN the form is
    // unmounted and restored. The order matters: the newer record is already in
    // hand when the form remounts, which is exactly when a re-derived baseline
    // would silently adopt it.
    rows = [{ ...MANUAL_INSTANCE, remote_port: 7999 }]
    await act(async () => {
      await u.click(screen.getByRole('button', { name: 'Refresh' }))
    })
    await u.click(screen.getByRole('button', { name: /Set up a new one/i }))
    await u.click(screen.getByRole('button', { name: /Your instances/i }))
    await screen.findByRole('group', { name: /Edit dev-box-1/i })

    // The port moved externally, which is a machine coordinate, so the save is
    // withheld until the user adopts the current record. The point of the test
    // survives that: adopting must not turn the stale port into a write.
    await u.click(await screen.findByRole('button', { name: /Apply my edits to the instance as it is now/i }))
    // Adopting the record remounts the form (it re-seeds from the merged draft), so
    // the old scope is detached — re-query rather than reusing it.
    const afterRebase = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    expect((afterRebase.getByRole('textbox', { name: /SSH host/i }) as HTMLInputElement).value)
      .toBe('dev-box-1-corrected')
    await u.click(afterRebase.getByRole('button', { name: /Save changes/i }))
    await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
    const body = vi.mocked(api.updateInstance).mock.calls[0][1]
    expect(body).toEqual({ ssh_host: 'dev-box-1-corrected' })
    // The port the user never touched must not be written back to its old value.
    expect(body).not.toHaveProperty('remote_port')
  })

  it('discards the draft when the user cancels the edit', async () => {
    // Preserving work must not mean it can never be dropped: Cancel is the user
    // choosing to discard, so reopening the row must show the stored record.
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const host = form.getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'typed-then-cancelled')
    await u.click(form.getByRole('button', { name: /^Cancel$/i }))

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const again = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    expect((again.getByRole('textbox', { name: /SSH host/i }) as HTMLInputElement).value).toBe(
      MANUAL_INSTANCE.ssh_host,
    )
  })

  it('drops the warm pane when a save tore its tunnel down, and keeps it when it did not', async () => {
    // A warm entry is an iframe holding the OLD local port and token. If the edit
    // tore the tunnel down, reconnecting cannot revive it — it would reuse a
    // credential the new tunnel never issued and sit on 403.
    vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED_MANUAL]))
    vi.mocked(api.updateInstance).mockResolvedValue({
      ...CONNECTED_MANUAL, ssh_host: 'moved', status: { state: 'disconnected' },
    } as never)
    const u = setup()
    const store = storeWithWarm('m1')
    renderWithProviders(<RemoteCrewPanel />, { store })

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const host = form.getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'moved')
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() => expect(store.getState().instances.warm).not.toHaveProperty('m1'))
  })

  it('keeps the warm pane when the save left the tunnel connected', async () => {
    // The counterpart: a name-or-ttl-only edit does not tear anything down, so
    // dropping the pane would make the user reload a working crew for nothing.
    vi.mocked(api.listInstances).mockResolvedValue(list([CONNECTED_MANUAL]))
    vi.mocked(api.updateInstance).mockResolvedValue({
      ...CONNECTED_MANUAL, name: 'renamed',
    } as never)
    const u = setup()
    const store = storeWithWarm('m1')
    renderWithProviders(<RemoteCrewPanel />, { store })

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const name = form.getByRole('textbox', { name: /^Name$/i })
    await u.clear(name)
    await u.type(name, 'renamed')
    await u.click(form.getByRole('button', { name: /Save changes/i }))

    await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
    expect(store.getState().instances.warm).toHaveProperty('m1')
  })

  it('refuses to save a draft onto a record that moved under it, until the user adopts it', async () => {
    // A live id is not proof of a live RECORD: a crew removed and recreated under
    // the same derived id never leaves the list, and a concurrent CLI edit moves the
    // record without touching its id. Those two want opposite outcomes — keep the
    // typing vs. never write it to this machine — and are indistinguishable here, so
    // the form refuses to guess: it names what moved and withholds Save.
    let rows: InstanceView[] = [MANUAL_INSTANCE]
    vi.mocked(api.listInstances).mockImplementation(async () => list(rows))
    vi.mocked(api.updateInstance).mockResolvedValue(MANUAL_INSTANCE)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const host = form.getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'dev-box-1-corrected')

    // Same id, different machine behind it.
    rows = [{ ...MANUAL_INSTANCE, remote_port: 7999, ssh_host: 'someone-elses-box' }]
    await act(async () => {
      await u.click(screen.getByRole('button', { name: 'Refresh' }))
    })

    expect(await screen.findByText(/changed outside this form/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save changes/i })).not.toBeInTheDocument()
    expect(api.updateInstance).not.toHaveBeenCalled()

    // The typed work is still there — it is withheld, not discarded.
    expect((host as HTMLInputElement).value).toBe('dev-box-1-corrected')

    // Adopting the current record restores Save, and the request is a partial update
    // against THAT record: the port the user never touched is not written back.
    await u.click(screen.getByRole('button', { name: /Apply my edits to the instance as it is now/i }))
    await u.click(await screen.findByRole('button', { name: /Save changes/i }))
    await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
    const body = vi.mocked(api.updateInstance).mock.calls[0][1]
    expect(body).toEqual({ ssh_host: 'dev-box-1-corrected' })
    expect(body).not.toHaveProperty('remote_port')
  })

  it('discards a draft whose crew no longer exists', async () => {
    // The draft outliving its FORM is the point; outliving its CREW is not. Ids
    // derive from the name, so a crew added after a removal can land on the same
    // id — remounting a stale draft on a different machine and letting Save
    // overwrite settings the user never typed.
    let rows: InstanceView[] = [MANUAL_INSTANCE]
    vi.mocked(api.listInstances).mockImplementation(async () => list(rows))
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const host = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
      .getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'typed-before-removal')

    // The crew goes away (this row's Remove, a CLI removal, or a cloud Delete).
    rows = []
    await act(async () => {
      await u.click(screen.getByRole('button', { name: 'Refresh' }))
    })
    await waitFor(() =>
      expect(screen.queryByRole('group', { name: /Edit dev-box-1/i })).not.toBeInTheDocument(),
    )

    // A crew that lands on the same id gets a CLEAN form, not the dead draft.
    rows = [MANUAL_INSTANCE]
    await act(async () => {
      await u.click(screen.getByRole('button', { name: 'Refresh' }))
    })
    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const reopened = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    expect((reopened.getByRole('textbox', { name: /SSH host/i }) as HTMLInputElement).value).toBe(
      MANUAL_INSTANCE.ssh_host,
    )
  })

  it('clears the blocked-edit warning once the open edit is saved', async () => {
    // The warning outliving the form it refers to instructs the user about a state
    // that no longer exists.
    const second = { ...MANUAL_INSTANCE, id: 'm2', name: 'dev-box-2', ssh_host: 'dev-box-2', remote_port: 7788 }
    vi.mocked(api.listInstances).mockResolvedValue(list([MANUAL_INSTANCE, second]))
    vi.mocked(api.updateInstance).mockResolvedValue({ ...MANUAL_INSTANCE, ssh_host: 'fixed' } as never)
    const u = setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for dev-box-1/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const host = form.getByRole('textbox', { name: /SSH host/i })
    await u.clear(host)
    await u.type(host, 'fixed')

    await openRowMenu(u, /More actions for dev-box-2/i)
    await u.click(await screen.findByRole('menuitem', { name: /Edit settings/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Save or cancel the open edit/i)

    await u.click(form.getByRole('button', { name: /Save changes/i }))
    await waitFor(() => expect(screen.queryByText(/Save or cancel the open edit/i)).not.toBeInTheDocument())
  })
})
