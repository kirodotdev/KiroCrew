import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { copyToClipboard } from '../utils/clipboard'
import { consumeChatHandoff, __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

// PARTIAL: the panel also reads the chain-refusal store from this module through
// useSyncExternalStore, so replacing the whole module would break rendering. Only the
// announce is spied, which is the one decision under test.
vi.mock('../lib/chainAnnounce', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/chainAnnounce')>()),
  announceChainedCrew: vi.fn(),
}))
import { announceChainedCrew } from '../lib/chainAnnounce'

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

/** Open a crew row's overflow menu — Edit / Stop / Start / Delete live there. */
async function openRowMenu(u: ReturnType<typeof userEvent.setup>, name: RegExp = /details for/i) {
  const toggle = await screen.findByRole('button', { name })
  if (toggle.getAttribute('aria-expanded') !== 'true') await u.click(toggle)
}

/** Open every crew row's details: the host, the notes and the other actions live there. */
async function expandRows() {
  for (const b of await screen.findAllByRole('button', { name: /^Show details for/ })) fireEvent.click(b)
}


const CLOUD_INSTANCE = {
  id: 'kc1',
  name: 'Kiro Crew Cloud (kc-3f9a)',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: '',
  aws_region: 'us-east-1',
  ssm_run_as: '',
  provisioner_id: 'aws_ec2',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  status: { instance_id: 'i-0abc123456789def0', state: 'connected' as const },
}
const MANUAL_INSTANCE = {
  id: 'm1',
  name: 'dev-box-1',
  connection_method: 'ssh' as const,
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
  status: { instance_id: 'm1', state: 'disconnected' as const },
}
const DONE_JOB = {
  id: 'j-done', tag: 'kc-3f9a', instance_id: 'i-0abc123456789def0', profile: '', region: 'us-east-1',
  provider_id: 'aws_ec2',
  size_key: 'balanced', status: 'done' as const, steps: [], signin: null, created_at: 0, updated_at: 0,
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
const PREFLIGHT_OK = {
  reachable: true, account: '1234•••7890', arn: 'arn:aws:iam::x:user/dev',
  ec2_reachable: true, cloudformation_reachable: true, ssm_reachable: true,
  session_manager_plugin: true, note: '', detail: '',
}

/** The one row the stock gateway offers. Its kind is drawn by the panel itself. */
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

// localStorage is cleared too: the panel now persists the AWS profile/region, so a
// test that seeds them would otherwise dictate what later tests probe.
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(copyToClipboard).mockResolvedValue(true)
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  __resetInstanceFailuresForTests()
  // Every test that reaches the setup tab needs this to settle before the
  // preflight is enabled (the preflight probes AWS, so it waits until the
  // selected provisioner is known to be the built-in one). The stock single-row
  // answer is the default; a test that cares overrides it.
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: "BuilderId" }, suggested_target: { license: "", start_url: "", region: "" } })
})

describe('RemoteCrewPanel', () => {
  it('never offers the plain-machine delete to a cloud crew while the launch history is still loading', async () => {
    // The row's cloud identity comes from cloudLaunches. If absent data were treated as
    // [], a real cloud crew would render as hand-added — and its trash button is a
    // single unconfirmed click that unregisters the instance while the EC2 stack keeps
    // running and billing, invisible to the dashboard.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    let releaseLaunches: (v: { jobs: typeof DONE_JOB[] }) => void = () => {}
    vi.mocked(api.cloudLaunches).mockReturnValue(
      new Promise(resolve => { releaseLaunches = resolve }) as ReturnType<typeof api.cloudLaunches>,
    )
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // While launches are in flight the list is not classified at all.
    expect(await screen.findByText(/Loading/i)).toBeInTheDocument()
    // No row at all yet — so no overflow menu, and nothing that could delete.
    expect(screen.queryByRole('button', { name: /More actions/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/does not manage this crew/i)).not.toBeInTheDocument()

    releaseLaunches({ jobs: [DONE_JOB] })

    // Once known, it is correctly a cloud row: Stop + the two-step Delete, no plain Remove.
    await expandRows()
    expect(await screen.findByText('Launched by Kiro Crew')).toBeInTheDocument()
    await openRowMenu(u)
    expect(screen.getByRole('button', { name: 'Stop Kiro Crew Cloud (kc-3f9a)' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Remove/i })).not.toBeInTheDocument()
  })

  it('keeps the device code reachable after navigating away and back', async () => {
    // activeLaunchId is component state, so a remount loses it. The awaiting-signin job
    // is still on the gateway, and its code is the only way to finish setup.
    const SIGNIN_JOB = {
      ...RUNNING_JOB,
      id: 'j-signin',
      status: 'awaiting_signin' as const,
      signin: { url: 'https://device.sso/verify', code: 'WXYZ-1234' },
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [SIGNIN_JOB] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(SIGNIN_JOB)
    const u = userEvent.setup()

    // A fresh mount: nothing was launched in this component's lifetime.
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    // Scoped to the setup card: the My Crews row behind the modal shows it too.
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText(/WXYZ-1234/)).toBeInTheDocument()
    expect(dialog.querySelector('a[href="https://device.sso/verify"]')).not.toBeNull()
    await waitFor(() => expect(api.cloudLaunchStatus).toHaveBeenCalledWith('j-signin'))
  })

  it('refreshes the crew list when a launch finishes, without waiting for a manual reload', async () => {
    // Switching tabs does not remount the panel, so nothing would invalidate the
    // instances cache and the brand-new crew would stay missing from My Crews.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [RUNNING_JOB] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue({ ...RUNNING_JOB, status: 'done' as const })
    renderWithProviders(<RemoteCrewPanel />)

    // listInstances is called once on mount, then again once the launch goes terminal.
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(1))
  })

  it('does not offer a one-click Remove to an SSM crew it cannot identify', async () => {
    // The CLI launcher registers real cloud crews over SSM, and those never produce a
    // launch job in this gateway's store — so an unmatched SSM row may well be a live
    // cloud crew. The plain one-click Remove would unregister a billing instance and
    // take away the only place the dashboard could still delete it.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })  // no job matches it
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // Not labelled as hand-added, because we cannot know that. The row is
    // EC2-stamped, so its caption agrees with the badge hint.
    await expandRows()
    expect(
      await screen.findByText(/Launched by the EC2 launcher\. Its instance may still be running and billing/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/does not manage this crew/i)).not.toBeInTheDocument()
    const row = screen.getByText(CLOUD_INSTANCE.name).closest('[data-crew-id]') as HTMLElement
    expect(within(row).getByText('EC2')).toBeInTheDocument()
    expect(within(row).getByText('SSM')).toBeInTheDocument()

    // The trash is confirm-gated, and the warning states what Remove does NOT do.
    await openRowMenu(u)
    await u.click(screen.getByRole('button', { name: /Remove Kiro Crew Cloud/i }))
    expect(await screen.findByText(/keeps running and billing/i)).toBeInTheDocument()
    expect(api.removeInstance).not.toHaveBeenCalled()
  })

  it('labels a chained crew\u2019s host as reported rather than as a target', async () => {
    // The host on a chained row arrives in the announcing pane's payload, which is
    // untrusted, and this gateway never dials it -- the forward rides the parent. Shown
    // bare, where every other row shows a verified target, an attacker-chosen string
    // borrows that authority: a compromised pane could make a row read like a
    // production database while reaching nothing at all.
    const chained = {
      ...MANUAL_INSTANCE,
      id: 'c1',
      name: 'Prod DB',
      ssh_host: 'prod-db.internal.example',
      via_instance_id: 'm1',
      via_remote_port: 53999,
      via_remote_id: 'c-2',
    }
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [MANUAL_INSTANCE, chained],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    // The value is kept -- only the parent knows which machine the crew is on, and
    // replacing it with the parent's host would state something false -- but it is
    // marked as the crew's own claim.
    await expandRows()
    expect(await screen.findByText(/prod-db\.internal\.example \(reported\)/i)).toBeInTheDocument()
    // The unchained row beside it still shows its host and port plainly, so the
    // label is a distinction and not a blanket hedge; the chained row shows no port.
    const plain = screen.getByRole('button', { name: 'Hide details for dev-box-1' }).closest('[data-crew-id]') as HTMLElement
    expect(within(plain).getByText('5476')).toBeInTheDocument()
    const chainedRow = screen.getByText(/prod-db\.internal\.example \(reported\)/i).closest('[data-crew-id]') as HTMLElement
    expect(within(chainedRow).queryByText('Port')).not.toBeInTheDocument()
  })

  it('treats an EC2-stamped SSH crew with no launch job as possibly cloud', async () => {
    // The EC2 stamp (`provisioner_id`) survives in the instance record even when
    // this gateway's store has no launch job for it — a carried-over config dir,
    // or a crew the CLI launcher registered. Calling it "added by you" would
    // invite a one-click Remove that unregisters a live, billing instance.
    const ec2Ssh = {
      ...MANUAL_INSTANCE,
      id: 'e1',
      name: 'gpu-box',
      ssh_host: 'gpu-box.internal',
      provisioner_id: 'aws_ec2',
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [ec2Ssh] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // The stamped caption agrees with the EC2 badge hint on the same row —
    // it was launched by the EC2 launcher — not the hedging "cannot tell" copy.
    await expandRows()
    expect(
      await screen.findByText(/Launched by the EC2 launcher\. Its instance may still be running and billing/i),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/cannot tell from here whether it launched this crew/i),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/Added by you/i)).not.toBeInTheDocument()

    // Remove is confirm-gated, and the warning states what Remove does NOT do.
    await openRowMenu(u, /details for gpu-box/i)
    await u.click(screen.getByRole('button', { name: /Remove gpu-box/i }))
    expect(await screen.findByText(/keeps running and billing/i)).toBeInTheDocument()
    expect(api.removeInstance).not.toHaveBeenCalled()
  })

  it('a connected fargate crew shows its chat API URL to copy, and nothing to open', async () => {
    // RULING: a fargate crew has no dashboard and no token. The one thing its
    // connect yields is the chat API's loopback URL, so the row offers that to
    // copy and offers no button that would point a browser at a JSON endpoint.
    const fargate = {
      ...MANUAL_INSTANCE,
      id: 'f1',
      name: 'fargate-crew',
      connection_method: 'fargate' as const,
      ssh_host: '',
      ssm_target: 'ecs:crew_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-0123456789',
      aws_region: 'us-west-2',
      remote_port: 8080,
      local_port: 7790,
      was_connected: true,
      status: {
        instance_id: 'f1',
        state: 'connected' as const,
        local_port: 7790,
        turn_url: 'http://127.0.0.1:7790/v1/chat/completions',
      },
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [fargate] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    await expandRows()
    const field = await screen.findByTestId('turn-url')
    expect(within(field).getByText('http://127.0.0.1:7790/v1/chat/completions')).toBeInTheDocument()
    expect(within(field).getByRole('button', { name: 'Copy the chat API URL of fargate-crew' })).toBeInTheDocument()
    // The row names the method and the ECS target it forwards to. The target
    // is shortened so the tail that tells two tasks in one cluster apart
    // survives the row's right-side truncation; the full target is on hover.
    expect(screen.getByText('Fargate')).toBeInTheDocument()
    const shownTarget = screen.getByText('ecs:crew_01234567\u2026-0123456789')
    expect(shownTarget).toHaveAttribute('title', fargate.ssm_target)
    // No open / dashboard affordance anywhere on the ROW (the page has other
    // buttons whose copy mentions opening the app; the row is what RULING 2
    // constrains).
    const row = field.closest('[data-crew-id="f1"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(within(row).queryByRole('button', { name: /open/i })).not.toBeInTheDocument()
    expect(within(row).queryByRole('link')).not.toBeInTheDocument()
    // A connected fargate row has no main action (nothing to open); Disconnect is in its details.
    expect(within(row).getByRole('button', { name: /Disconnect/i })).toBeInTheDocument()
    // A fargate row IS an AWS resource, so its caption states that plainly and
    // never hedges the way an unidentified SSM row does.
    expect(within(row).getByText(/An AWS Fargate task\./)).toBeInTheDocument()
    expect(within(row).queryByText(/cannot tell from here whether it launched this crew/)).not.toBeInTheDocument()
  })

  it('shows a failed turn URL copy and clears it after a successful retry', async () => {
    const fargate = {
      ...MANUAL_INSTANCE,
      id: 'f-copy',
      name: 'copy-crew',
      connection_method: 'fargate' as const,
      ssh_host: '',
      ssm_target: 'ecs:crew_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-0123456789',
      remote_port: 8080,
      local_port: 7790,
      was_connected: true,
      status: {
        instance_id: 'f-copy',
        state: 'connected' as const,
        local_port: 7790,
        turn_url: 'http://127.0.0.1:7790/v1/chat/completions',
      },
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [fargate] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await expandRows()
    const copyButton = await screen.findByRole('button', {
      name: 'Copy the chat API URL of copy-crew',
    })
    await u.click(copyButton)
    expect(await screen.findByTestId('turn-url-copy-error')).toBeInTheDocument()

    await u.click(copyButton)
    await waitFor(() => expect(screen.queryByTestId('turn-url-copy-error')).not.toBeInTheDocument())
  })

  it('a fargate crew that is not connected shows no chat API URL', async () => {
    // The URL is a property of the open forward, not of the record: with the
    // tunnel down there is no port behind it, so a stale URL would invite a
    // call that can only fail.
    const fargate = {
      ...MANUAL_INSTANCE,
      id: 'f2',
      name: 'fargate-idle',
      connection_method: 'fargate' as const,
      ssh_host: '',
      ssm_target: 'ecs:crew_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-0123456789',
      remote_port: 8080,
      status: { instance_id: 'f2', state: 'disconnected' as const },
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [fargate] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('fargate-idle')).toBeInTheDocument()
    expect(screen.queryByTestId('turn-url')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Connect$/i })).toBeInTheDocument()
  })

  it('a fargate row states it is an AWS task instead of hedging about AWS resources', async () => {
    // A fargate record IS an AWS resource by definition, so the caption names
    // it as a task and drops the unverified-cloud hedge, while keeping the same
    // Remove confirm step. Holds whether or not the forward is up; an idle row
    // is the cheapest fixture that reaches the caption.
    const fargate = {
      ...MANUAL_INSTANCE,
      id: 'f3',
      name: 'fargate-note',
      connection_method: 'fargate' as const,
      ssh_host: '',
      ssm_target: 'ecs:crew_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-0123456789',
      remote_port: 8080,
      status: { instance_id: 'f3', state: 'disconnected' as const },
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [fargate] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    const name = await screen.findByText('fargate-note')
    await expandRows()
    const row = name.closest('[data-crew-id="f3"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(within(row).getByText(/An AWS Fargate task\./)).toBeInTheDocument()
    expect(within(row).queryByText(/cannot tell from here whether it launched this crew/)).not.toBeInTheDocument()
  })

  it('still lists the crews when the gateway cannot do cloud provisioning at all', async () => {
    // A Windows gateway POSIX-gates the launch-history route, so the launches query
    // fails with 400 posix_host_required. Treating that as a load failure replaced
    // the whole list — hand-added SSH crews included — with a cloud-provisioning
    // error, leaving no way to connect, edit or remove anything the user had saved.
    // It is not a failure: it means this host cannot have launched a cloud crew.
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE, CLOUD_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockRejectedValue(
      new ApiError(
        400,
        'cloud provisioning requires a POSIX host (Linux/macOS); use WSL on Windows',
        JSON.stringify({
          error: 'cloud provisioning requires a POSIX host (Linux/macOS); use WSL on Windows',
          code: 'posix_host_required',
        }),
      ),
    )
    renderWithProviders(<RemoteCrewPanel />)

    // Both saved crews render, each with its own Connect button.
    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    expect(screen.getByText(/Kiro Crew Cloud/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Connect$/i })).toBeInTheDocument()
    // The POSIX message belongs on the Set-up tab, not over the crew list.
    expect(screen.queryByText(/requires a POSIX host/i)).not.toBeInTheDocument()

    // With no launch history, the SSM row must NOT be downgraded to "added by you":
    // the CLI launcher registers real cloud crews the same way. This row carries
    // the EC2 stamp, so it gets the stamped caption with the confirm step.
    await expandRows()
    expect(
      screen.getByText(/Launched by the EC2 launcher\. Its instance may still be running and billing/i),
    ).toBeInTheDocument()
  })

  it('labels SSM, confirmed EC2 over SSH, and plain SSH crews accurately', async () => {
    const legacyCloud = { ...CLOUD_INSTANCE, provisioner_id: undefined }
    const ec2Ssh = {
      ...MANUAL_INSTANCE,
      id: 'legacy-ec2',
      name: 'Legacy EC2',
      ssh_host: 'i-0feed123456789abc',
      provisioner_id: 'aws_ec2',
    }
    const ec2SshJob = {
      ...DONE_JOB,
      id: 'j-ssh',
      tag: 'kc-ssh',
      instance_id: ec2Ssh.ssh_host,
    }
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 10,
      instances: [legacyCloud, ec2Ssh, MANUAL_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB, ec2SshJob] })
    renderWithProviders(<RemoteCrewPanel />)

    const cloudRow = (await screen.findByText(legacyCloud.name)).closest('[data-crew-id]')
    const ec2SshRow = screen.getByText(ec2Ssh.name).closest('[data-crew-id]')
    const sshRow = screen.getByText(MANUAL_INSTANCE.name).closest('[data-crew-id]')

    expect(cloudRow).not.toBeNull()
    expect(ec2SshRow).not.toBeNull()
    expect(sshRow).not.toBeNull()
    expect(within(cloudRow as HTMLElement).getByText('EC2')).toBeInTheDocument()
    expect(within(cloudRow as HTMLElement).getByText('SSM')).toBeInTheDocument()
    expect(within(ec2SshRow as HTMLElement).getByText('EC2')).toBeInTheDocument()
    expect(within(ec2SshRow as HTMLElement).getByText('SSH')).toBeInTheDocument()
    expect(within(sshRow as HTMLElement).getByText('SSH')).toBeInTheDocument()
    expect(within(sshRow as HTMLElement).queryByText('EC2')).not.toBeInTheDocument()

    // The acronym badges explain themselves with matching hover titles and
    // accessible names.
    const ec2Badge = within(cloudRow as HTMLElement).getByText('EC2').closest('span')
    expect(ec2Badge).toHaveAttribute('title', expect.stringMatching(/EC2 launcher/))
    expect(ec2Badge).toHaveAttribute('aria-label', expect.stringMatching(/EC2 launcher/))
    expect(
      within(cloudRow as HTMLElement).getByText('SSM').closest('span'),
    ).toHaveAttribute('title', expect.stringMatching(/Session Manager/))
    expect(within(cloudRow as HTMLElement).getByText('SSM').closest('span')).toHaveAccessibleName(expect.stringMatching(/Session Manager/))
  })

  it('renames a configured crew and refreshes its visible label', async () => {
    let rows = [MANUAL_INSTANCE]
    vi.mocked(api.listInstances).mockImplementation(async () => ({
      active: true,
      warm_set_cap: 10,
      instances: rows,
    }))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.updateInstance).mockImplementation(async (_id, body) => {
      rows = [{ ...MANUAL_INSTANCE, name: String(body.name) }]
      return rows[0]
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /details for dev-box-1/i)
    await u.click(await screen.findByRole('button', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const name = form.getByRole('textbox', { name: /Name/i })
    const save = form.getByRole('button', { name: 'Save changes' })

    // The full record is on show and editable: renaming is Edit settings'
    // Name field, not a separate mode.
    expect(form.getByRole('textbox', { name: /SSH host/i })).toBeInTheDocument()
    await u.clear(name)
    expect(save).toBeDisabled()
    await u.type(name, 'Build box')
    expect(save).toBeEnabled()
    await u.click(save)

    await waitFor(() => expect(api.updateInstance).toHaveBeenCalledWith('m1', { name: 'Build box' }, expect.objectContaining({ signal: expect.anything() })))
    expect(await screen.findByText('Build box')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide details for Build box' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /details for dev-box-1/ })).not.toBeInTheDocument()
  })

  it('keeps the rename draft open and shows an API rejection', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 10,
      instances: [MANUAL_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.updateInstance).mockRejectedValue(new ApiError(409, 'name is already in use'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /details for dev-box-1/i)
    await u.click(await screen.findByRole('button', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const name = form.getByRole('textbox', { name: /Name/i })
    await u.clear(name)
    await u.type(name, 'Taken name')
    await u.click(form.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('name is already in use')).toBeInTheDocument()
    expect(form.getByRole('textbox', { name: /Name/i })).toHaveValue('Taken name')
  })

  it('restores a rename draft in the shared edit form after a route remount', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 10,
      instances: [MANUAL_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    const u = userEvent.setup()
    const first = renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /details for dev-box-1/i)
    await u.click(await screen.findByRole('button', { name: /Edit settings/i }))
    const form = within(await screen.findByRole('group', { name: /Edit dev-box-1/i }))
    const name = form.getByRole('textbox', { name: /Name/i })
    await u.clear(name)
    await u.type(name, 'Build box')

    first.unmount()
    renderWithProviders(<RemoteCrewPanel />, { store: first.store })

    const restored = within(
      await screen.findByRole('group', { name: /Edit dev-box-1/i }),
    )
    expect(restored.getByRole('textbox', { name: /Name/i })).toHaveValue('Build box')
    expect(restored.getByRole('textbox', { name: /SSH host/i })).toBeInTheDocument()
  })

  it('shows the install command the gateway reported, not a hardcoded macOS one', async () => {
    // The plugin must exist on the machine running the gateway, which may be Linux
    // while this dashboard is open on a Mac. A hardcoded `brew` line would be
    // unusable for every Linux host, so the remedy comes from the preflight.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue({
      ...PREFLIGHT_OK,
      session_manager_plugin: false,
      session_manager_plugin_command: 'sudo dnf install -y https://example.invalid/smp.rpm',
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    expect(await screen.findByText(/sudo dnf install -y/)).toBeInTheDocument()
    expect(screen.queryByText(/brew install/)).not.toBeInTheDocument()
  })

  it('offers no command when the gateway platform has no one-liner', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue({
      ...PREFLIGHT_OK,
      session_manager_plugin: false,
      session_manager_plugin_command: '',
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    // The localized "not installed" line still explains the gap…
    expect(await screen.findByText('Session Manager plugin')).toBeInTheDocument()
    // …but no Copy button appears with nothing to copy.
    expect(screen.queryByRole('button', { name: /Copy command/i })).not.toBeInTheDocument()
  })

  it('names the credential_process helper the AWS CLI could not find', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue({
      ...PREFLIGHT_OK,
      reachable: false,
      account: '',
      detail: "aws: [ERROR]: [Errno 2] No such file or directory: 'sso-cred-helper'",
      credential_helper_missing: 'sso-cred-helper',
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    expect(
      await screen.findByText(/^Give sso-cred-helper its full path in the credential_process line.*could not find it on the gateway’s PATH/s),
    ).toBeInTheDocument()
    // The CLI failure renders through ErrorNotice with the agent hand-off.
    const notice = screen.getByText(/^Give sso-cred-helper its full path/).closest('[role="alert"]') as HTMLElement
    expect(notice).not.toBeNull()
    expect(notice).toHaveTextContent(/No such file or directory: 'sso-cred-helper'/)
    expect(within(notice).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    // The sentence leads; the CLI's own error sits on its own smaller,
    // monospace line under it instead of running on in the same red paragraph.
    const lead = screen.getByText(/^Give sso-cred-helper its full path/)
    expect(lead).not.toHaveTextContent(/No such file or directory/)
    const raw = within(notice).getByText("aws: [ERROR]: [Errno 2] No such file or directory: 'sso-cred-helper'")
    expect(raw).not.toBe(lead)
    expect(raw.className).toMatch(/\bfont-mono\b/)
    // The row's error circle is the only failure mark: it blocks Launch.
    const row = notice.closest('li') as HTMLElement
    expect(row.querySelectorAll('.lucide-x')).toHaveLength(1)
    expect(notice.querySelector('.lucide-x, .lucide-triangle-alert')).toBeNull()
    // The plugin's install link belongs to the plugin row only.
    expect(within(row).queryByRole('link', { name: /install page/ })).toBeNull()
  })

  it('remembers the AWS profile across a remount and probes THAT account, not the default', async () => {
    // This panel unmounts when you visit another Settings section. Losing the
    // profile was worse than retyping: the committed value fell back to '', so the
    // next probe tested the AWS CLI default profile and reported unrelated expired
    // credentials — the exact confusion this checklist is supposed to prevent.
    localStorage.setItem('mc-cloud-profile', 'Admin')
    localStorage.setItem('mc-cloud-region', 'us-west-2')
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    // The field is repopulated…
    expect(await screen.findByLabelText(/AWS profile/i)).toHaveValue('Admin')
    // …and the FIRST probe already used it, rather than the default profile.
    await waitFor(() => expect(api.cloudPreflight).toHaveBeenCalledWith('Admin', 'us-west-2'))
    expect(await screen.findByText(/Checked against profile Admin in us-west-2/i)).toBeInTheDocument()
  })

  it('shows the Re-check button doing work instead of looking inert', async () => {
    // The re-check refetches an already-populated query, so the card's isLoading
    // spinner never fires and an unchanged result repaints identically — the click
    // looked like a no-op even though the probe really ran.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    // First call resolves; the second (the re-check) is held open so we can observe
    // the pending state.
    let releaseSecond: (v: typeof PREFLIGHT_OK) => void = () => {}
    vi.mocked(api.cloudPreflight)
      .mockResolvedValueOnce({ ...PREFLIGHT_OK, session_manager_plugin: false })
      .mockReturnValueOnce(
        new Promise(resolve => { releaseSecond = resolve }) as ReturnType<typeof api.cloudPreflight>,
      )
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    const recheck = (await screen.findAllByRole('button', { name: /Re-check/i }))[0]
    await u.click(recheck)

    // While in flight every re-check control reports progress and cannot be re-fired.
    const busy = await screen.findAllByRole('button', { name: /Checking/i })
    expect(busy.length).toBeGreaterThan(0)
    for (const b of busy) expect(b).toBeDisabled()

    releaseSecond({ ...PREFLIGHT_OK, session_manager_plugin: false })
    await waitFor(() => expect(screen.queryAllByRole('button', { name: /Checking/i })).toHaveLength(0))
    expect((await screen.findAllByRole('button', { name: /Re-check/i })).length).toBeGreaterThan(0)
  })

  it('puts the account inputs above the checks they produce, and names what was probed', async () => {
    // The verdict used to render above the profile/region inputs that produced it, so a
    // red "credentials expired" row gave no hint it had probed a different profile than
    // the reader had in mind. Cause must precede effect in the DOM, and the card must
    // say which identity it checked.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue({ ...PREFLIGHT_OK, account: '1234•••7890' })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    const profileInput = await screen.findByLabelText(/AWS profile/i)
    const credsRow = await screen.findByText('AWS credentials')
    // compareDocumentPosition: 4 = FOLLOWING — the row comes after the input.
    expect(profileInput.compareDocumentPosition(credsRow) & 4).toBeTruthy()
    // And the probed identity is stated in plain words, never as a profile named "none set".
    expect(await screen.findByText('Checked with your default AWS credentials in us-east-1.')).toBeInTheDocument()
    expect(screen.getByText('Using your default AWS credentials → account 1234•••7890.')).toBeInTheDocument()
  })

  it('promises only what the gateway actually delivers while a launch runs', async () => {
    // A restart terminalizes the job (reap_orphans marks it "Interrupted"), and no
    // completion notification is implemented — so the progress copy must not tell the
    // user they can quit the app or that they will be notified. Acting on either claim
    // costs them the setup.
    const SIGNIN_JOB = {
      ...RUNNING_JOB,
      status: 'awaiting_signin' as const,
      signin: { url: 'https://device.sso/verify', code: 'WXYZ-1234' },
    }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [SIGNIN_JOB] })
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(SIGNIN_JOB)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    const card = (await within(await screen.findByRole('dialog')).findByText(/WXYZ-1234/)).closest('div')?.parentElement
    expect(card).toBeTruthy()
    const page = document.body.textContent ?? ''
    expect(page).toMatch(/leave the page or switch crews and it keeps going/i)
    expect(page).not.toMatch(/quit the app/i)
    expect(page).not.toMatch(/get a notification/i)
  })

  it('offers Start so Stop is not a one-way door', async () => {
    // api.cloudStart existed and the route existed, but nothing in the UI called it:
    // a stopped crew had no dashboard path back to running while its EBS kept billing.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudStart).mockResolvedValue({ started: true } as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('button', { name: /^Start Kiro Crew Cloud/i }))
    await waitFor(() => expect(api.cloudStart).toHaveBeenCalledWith('kc-3f9a', expect.anything()))
  })

  it('still shows the device code when a finished launch never confirmed sign-in', async () => {
    // The gateway keeps job.signin precisely so the user can finish from the
    // dashboard; gating the block on status==='awaiting_signin' hid the code the
    // moment the job went terminal, making that promise a dead end.
    const job = { ...DONE_JOB, id: 'j-unconfirmed', signin: { code: 'WXYZ-9876', url: 'https://sign-in.example/device' } }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [job] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(job as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    expect(await screen.findByText(/WXYZ-9876/)).toBeInTheDocument()
    expect(screen.getByText(/could not confirm the sign-in/i)).toBeInTheDocument()
  })

  it('shows progress on the button that was clicked', async () => {
    // The busy key interpolated the whole {tag, coords} variables object, producing
    // "stop:[object Object]" — a key no row matched, so the label never changed.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    let release: (v: unknown) => void = () => {}
    vi.mocked(api.cloudStop).mockReturnValue(new Promise(r => { release = r }) as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('button', { name: /^Stop Kiro Crew Cloud/i }))
    // While in flight the clicked button reports progress rather than still saying "Stop".
    await waitFor(() => expect(screen.getByRole('button', { name: /^Stop Kiro Crew Cloud/i })).toHaveTextContent('…'))
    release({ ok: true })
  })

  it('shows a Deleting… state after the delete is accepted, instead of leaving the row untouched', async () => {
    // The DELETE endpoint only *requests* the teardown (cleanup: "pending"); the row is
    // dropped minutes later by the gateway once AWS confirms. Without a pending state the
    // row reappeared unchanged after the click and looked like nothing happened.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudDestroy).mockResolvedValue({ cleanup: 'pending' } as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('button', { name: /^Delete Kiro Crew Cloud/i }))
    await u.click(await screen.findByRole('button', { name: /^Confirm deleting/i }))
    await waitFor(() => expect(api.cloudDestroy).toHaveBeenCalledWith('kc-3f9a', expect.anything()))
    // The row now reflects the in-flight teardown and cannot be re-triggered.
    const deleting = await screen.findByRole('button', { name: /Deleting…/i })
    expect(deleting).toBeDisabled()
  })

  it('shows the enable CTA when the feature is disabled (403)', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'instances feature is disabled', JSON.stringify({ error: 'instances feature is disabled', code: 'instances_disabled' })))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByText('Remote Crew is off')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Enable remote crew management/i })).toBeInTheDocument()
  })

  it('does not flash the tabbed UI before showing the disabled state', async () => {
    // Bug: the panel rendered the full form (tabs, crew list) during the initial
    // query, then jittered to the "off" card once the 403 arrived. Fix: show a
    // neutral loading card until the enabled/disabled state is determined.
    let rejectInstances: (e: Error) => void = () => {}
    vi.mocked(api.listInstances).mockReturnValue(
      new Promise((_resolve, reject) => { rejectInstances = reject }) as ReturnType<typeof api.listInstances>,
    )
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    // While loading: a spinner, no sections, no Add Crew.
    expect(screen.getByText(/Loading/i)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'My Crews' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add Crew' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Enable remote crew management/i })).not.toBeInTheDocument()

    // After the 403 resolves: transitions directly to the disabled card.
    rejectInstances(new ApiError(403, 'instances feature is disabled', JSON.stringify({ error: 'instances feature is disabled', code: 'instances_disabled' })))
    expect(await screen.findByText('Remote Crew is off')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'My Crews' })).not.toBeInTheDocument()
  })

  it('distinguishes cloud crews from hand-added machines, and shows an in-progress launch', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE, MANUAL_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB, RUNNING_JOB] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // Cloud row carries the cloud attribution + a Stop control; manual row does not.
    await expandRows()
    expect(await screen.findByText('Launched by Kiro Crew')).toBeInTheDocument()
    expect(screen.getByText(/does not manage this crew/i)).toBeInTheDocument()
    await openRowMenu(u, /details for Kiro Crew Cloud/i)
    expect(screen.getByRole('button', { name: 'Stop Kiro Crew Cloud (kc-3f9a)' })).toBeInTheDocument()

    // The still-launching job is a "Setting up" row whose status opens its steps and the note.
    await u.click(screen.getByRole('button', { name: /Setting up · 3 of 4/ }))
    expect(screen.getByText(/Launching Kiro Crew Cloud/)).toBeInTheDocument()
    expect(screen.getByText(/Keeps running if you leave this page/i)).toBeInTheDocument()
  })

  it('enables Launch only once the AWS prerequisites pass', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue({ ...PREFLIGHT_OK, session_manager_plugin: false })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    // Prereq checklist rendered; a missing plugin blocks Launch.
    expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
    expect(screen.getByText('Session Manager plugin')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled())
    expect(screen.getByText('Fix “Session Manager plugin” above to enable this.')).toBeInTheDocument()
  })

  it('renders each size card headlined by its interpolated sub-agent count', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    // The sub-agent count is the headline the size choice turns on, so it must be
    // the real number: a var-name mismatch renders the raw `{{n}}` placeholder.
    expect(await screen.findByText(/~3 parallel sub-agents/)).toBeInTheDocument()
    expect(screen.getByText(/~6 parallel sub-agents/)).toBeInTheDocument()
    expect(screen.getByText(/~12 parallel sub-agents/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('{{')
  })

  it('shows the error and a retry when the crew list fails to load', async () => {
    // A failed load must not render "no crews yet" — that reads as "your crews
    // are gone" when the list simply did not come back.
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(500, 'gateway exploded'))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText(/gateway exploded/i)).toBeInTheDocument()
    expect(screen.queryByText(/No crews yet/i)).not.toBeInTheDocument()
    // A retry sits with the error, in addition to the header's refresh control.
    expect(screen.getAllByRole('button', { name: /Refresh/i }).length).toBeGreaterThan(1)
  })

  it('drops the retry when the load failed because the session no longer authenticates', async () => {
    // Retrying replays the same rejected credential, so the button could only
    // reproduce the error. Re-auth happens through the page-top banner instead,
    // and only the header's own refresh control remains.
    const denial = new ApiError(403, 'Session expired. Run kirocrew token …')
    ;(denial as unknown as { authRequired: boolean }).authRequired = true
    vi.mocked(api.listInstances).mockRejectedValue(denial)
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText(/kirocrew token/i)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Refresh/i }).length).toBe(1)
  })

  it('warns that a restart is required when the feature is on but not active', async () => {
    // active:false means the flag was set after the gateway started, so Connect
    // would 503. The user needs to be told to restart, not offered a dead action.
    vi.mocked(api.listInstances).mockResolvedValue({
      active: false, warm_set_cap: 5, instances: [CLOUD_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByRole('status')).toHaveTextContent(/restart/i)
  })

  it('offers selectable x86_64 tiers once the disclosure is expanded', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    // Collapsed: the arm64 ladder only.
    expect(screen.queryByText(/m7i\.2xlarge/)).not.toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: /Smaller and x86_64 sizes/i }))

    // Expanded: the disclosure must deliver real, selectable tiers — not just a
    // sentence describing sizes the user cannot pick.
    expect(await screen.findByText(/t3\.xlarge/)).toBeInTheDocument()
    expect(screen.getByText(/m7i\.2xlarge/)).toBeInTheDocument()
    expect(screen.getByText(/m7i\.4xlarge/)).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /Development · x86_64/i }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Development · x86_64/i })).toHaveAttribute('aria-pressed', 'true'),
    )
  })

  it('launches a cloud crew when prerequisites pass and shows the progress card', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    // The list resolved to the built-in row, so the body names it (see the seam tests).
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({ provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced' }))
    // Progress card polls the job and renders its steps.
    expect(await screen.findByText('Installing Kiro Crew')).toBeInTheDocument()
  })

  it('sends a typed subnet ID with the launch', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    await u.type(await screen.findByRole('textbox', { name: 'Subnet ID (optional)' }), ' subnet-0123abcd ')
    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({
      provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced', subnet_id: 'subnet-0123abcd',
    }))
  })

  it('preselects the inherited Identity Center sign-in, gates launch on the region, and sends the target', async () => {
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
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

    // The organization's portal is preselected and named; the form asks only for the region.
    const idc = await screen.findByRole('radio', { name: /Company SSO/i })
    await waitFor(() => expect(idc).toBeChecked())
    expect(screen.getByText(/Preselected from this computer's Kiro sign-in/)).toBeInTheDocument()
    const url = screen.getByRole('textbox', { name: /Identity Center start URL/i })
    expect(url).toHaveValue('https://example.awsapps.com/start')
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    // Region missing: the launch is refused up front, never sent as Builder ID.
    expect(launch).toBeDisabled()
    expect(screen.getByText(/Enter the Identity Center start URL and region/)).toBeInTheDocument()

    await u.type(screen.getByRole('textbox', { name: /Identity Center region/i }), 'us-east-1')
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() =>
      expect(api.cloudLaunch).toHaveBeenCalledWith({
        provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced',
        login_target: { license: 'pro', start_url: 'https://example.awsapps.com/start', region: 'us-east-1' },
      }),
    )
  })

  it('accepts any safe portal URL the backend accepts, scheme-less or on another domain', async () => {
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
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    const url = await screen.findByRole('textbox', { name: /Identity Center start URL/i })
    await waitFor(() => expect(url).toHaveValue('https://example.awsapps.com/start'))
    await u.type(screen.getByRole('textbox', { name: /Identity Center region/i }), 'us-gov-west-1')
    const launch = screen.getByRole('button', { name: /^Launch$/ })

    // The form mirrors normalize_start_url: a GovCloud portal pasted without a
    // scheme is a valid target, not a format the user has to guess at.
    await u.clear(url)
    await u.type(url, 'example.awsapps-us-gov.com/start')
    await waitFor(() => expect(launch).not.toBeDisabled())
    // A shell metacharacter is the one shape the form does refuse up front.
    await u.type(url, ';id')
    await waitFor(() => expect(launch).toBeDisabled())
  })

  it('lets the user override the inherited identity back to Builder ID, which sends no target', async () => {
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
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    await waitFor(() => expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked())
    await u.click(screen.getByRole('radio', { name: /^Builder ID$/i }))
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() =>
      expect(api.cloudLaunch).toHaveBeenCalledWith({ provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced' }),
    )
  })

  it('keeps Launch disabled while the inherited identity is still being read', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    let resolveIdentity: (v: Awaited<ReturnType<typeof api.cloudIdentity>>) => void = () => {}
    vi.mocked(api.cloudIdentity).mockReturnValue(new Promise((r) => { resolveIdentity = r }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    // Prerequisites are satisfied, but the identity read is pending: the
    // Builder ID default is a placeholder, so Launch must NOT be clickable —
    // a click here would send no target and ignore the preselection that
    // lands a moment later.
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    await screen.findByText(/Reading this computer's Kiro sign-in/i)
    expect(launch).toBeDisabled()
    resolveIdentity({
      identity: { account_type: 'IamIdentityCenter', start_url: 'https://example.awsapps.com/start' },
      suggested_target: { license: 'pro', start_url: 'https://example.awsapps.com/start', region: '' },
    })
    // Resolved: the preselection landed, and the gate is now the region field.
    await waitFor(() => expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked())
    expect(launch).toBeDisabled()
    await u.type(screen.getByRole('textbox', { name: /Identity Center region/i }), 'us-east-1')
    await waitFor(() => expect(launch).not.toBeDisabled())
  })

  it('lets an explicit user choice override the wait for the inherited identity', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudIdentity).mockReturnValue(new Promise(() => {})) // never resolves
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    expect(launch).toBeDisabled()
    await u.click(screen.getByRole('radio', { name: /^Builder ID$/i }))
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() =>
      expect(api.cloudLaunch).toHaveBeenCalledWith({ provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced' }),
    )
  })

  it('surfaces an identity lookup failure inline and waits for an explicit choice before launch', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudIdentity).mockRejectedValue(new Error('kiro-cli whoami timed out'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    // The failure is shown as a short human cause (never the raw exception
    // text) and offers the agent hand-off: the form keeps what the user typed,
    // so the navigation loses nothing.
    const notice = await screen.findByText(/request to read this computer's Kiro sign-in failed/i)
    expect(notice.textContent).not.toMatch(/whoami timed out/)
    const alert = notice.closest('[role="alert"]') as HTMLElement
    expect(within(alert).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    // Nothing is known about this computer's sign-in, so NO radio renders
    // checked and Launch waits for the user to pick: a checked Builder ID
    // beside a gate that says "choose" would read as a choice already made,
    // and an Identity Center user whose whoami failed must not be launched as
    // Builder ID by a default they never confirmed.
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    expect(launch).toBeDisabled()
    expect(screen.getByText(/Choose the crew's Kiro identity to launch/i)).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /^Builder ID$/i })).not.toBeChecked()
    expect(screen.getByRole('radio', { name: /Company SSO/i })).not.toBeChecked()
    await u.click(screen.getByRole('radio', { name: /^Builder ID$/i }))
    expect(screen.getByRole('radio', { name: /^Builder ID$/i })).toBeChecked()
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() =>
      expect(api.cloudLaunch).toHaveBeenCalledWith({ provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced' }),
    )
  })

  it('treats a server-reported unknown discovery as no preselection and waits for a choice', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(RUNNING_JOB)
    // whoami on the launching computer could not answer: the server suggests
    // nothing rather than a Builder ID default that would read as a fact.
    vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: null, suggested_target: null, discovery: 'unknown' })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    expect((await screen.findByText(/kiro-cli whoami did not answer/i)).closest('[role="status"]')).not.toBeNull() // amber, not red
    const launch = screen.getByRole('button', { name: /^Launch$/ })
    expect(launch).toBeDisabled()
    expect(screen.getByText(/Choose the crew's Kiro identity to launch/i)).toBeInTheDocument()
    // The user picks Identity Center by hand: the usual field gate applies.
    await u.click(screen.getByRole('radio', { name: /Company SSO/i }))
    expect(launch).toBeDisabled()
    await u.type(screen.getByRole('textbox', { name: /start URL/i }), 'https://example.awsapps.com/start')
    await u.type(screen.getByRole('textbox', { name: /Identity Center region/i }), 'us-east-1')
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() =>
      expect(api.cloudLaunch).toHaveBeenCalledWith({
        provider_id: 'aws_ec2', profile: '', region: 'us-east-1', size_key: 'balanced',
        login_target: { license: 'pro', start_url: 'https://example.awsapps.com/start', region: 'us-east-1' },
      }),
    )
  })

  it('names the Identity Center cause when the sign-in was read but its portal was not', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    // The server read an Identity Center sign-in but no portal address: the
    // user IS an Identity Center user, so the notice must say so rather than
    // hedge, and no radio may render checked as if Builder ID were a fact.
    vi.mocked(api.cloudIdentity).mockResolvedValue({
      identity: { account_type: 'IamIdentityCenter' }, suggested_target: null, discovery: 'unknown',
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
    const notice = await screen.findByText(/signed in through Identity Center, but its portal address could not be read/i)
    expect(notice.textContent).not.toMatch(/did not answer/)
    expect(screen.getByRole('radio', { name: /^Builder ID$/i })).not.toBeChecked()
    expect(screen.getByRole('radio', { name: /Company SSO/i })).not.toBeChecked()
    // The "preselected from this computer" hint belongs to a READ identity only.
    expect(screen.queryByText(/Preselected from this computer's Kiro sign-in/i)).toBeNull()
    expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled()
    await u.click(screen.getByRole('radio', { name: /Company SSO/i }))
    expect(screen.getByRole('radio', { name: /Company SSO/i })).toBeChecked()
    expect(screen.getByRole('textbox', { name: /start URL/i })).toBeInTheDocument()
  })

  describe('the chained-crew announce and a crew with no dashboard', () => {
    // A fargate crew serves a turn API on its forwarded port and nothing else -- no
    // dashboard, no token -- so announcing it to a host makes that host persist a row
    // promising a tab nothing can serve, and the host writes the row BEFORE it tries to
    // connect it, so the phantom survives until a human presses Remove. The announce gate
    // used to check only `local_port`, which a fargate connect does supply.
    const FARGATE = {
      id: 'f9',
      name: 'fargate-crew',
      connection_method: 'fargate' as const,
      ssh_host: '',
      ssm_target: 'ecs:crew_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-0123456789',
      aws_region: 'us-west-2',
      remote_port: 8080,
      local_port: 0,
      status: { instance_id: 'f9', state: 'disconnected' as const },
    }
    const SSH = {
      id: 's9',
      name: 'ssh-crew',
      connection_method: 'ssh' as const,
      ssh_host: 's9-alias',
      remote_port: 5476,
      local_port: 0,
      status: { instance_id: 's9', state: 'disconnected' as const },
    }
    // The control that matters most. `usesSsmTransport` is true for BOTH ssm and
    // fargate, so an ssh-only control cannot tell "gated on having a dashboard" from
    // "gated on not being SSM-family" -- an ssm crew DOES have a dashboard, so it must
    // still be announced, and only that distinguishes the two rules.
    const SSM = {
      id: 'm9',
      name: 'ssm-crew',
      connection_method: 'ssm' as const,
      ssh_host: '',
      ssm_target: 'i-0123456789abcdef0',
      aws_region: 'us-west-2',
      remote_port: 5476,
      local_port: 0,
      status: { instance_id: 'm9', state: 'disconnected' as const },
    }
    // A crew THIS dashboard already reaches through another crew. Announcing it asks the
    // host for a third hop, and the host cannot refuse: it counts hops in its own
    // registry, where our hop to this crew is invisible, so it accepts the add and
    // commits the row. Our own gateway is the only one that can see the extra hop, and
    // it does -- the mint answers `chain_too_deep` -- but the row is already persisted
    // by then and the refusal relay leaves it behind.
    const CHAINED = {
      id: 'x9',
      name: 'chained-crew',
      connection_method: 'ssh' as const,
      ssh_host: 'x9-alias',
      remote_port: 5476,
      local_port: 0,
      via_instance_id: 's9',
      via_remote_id: 'x9-there',
      via_remote_port: 53710,
      status: { instance_id: 'x9', state: 'disconnected' as const },
    }

    async function connect(rowName: string, localPort: number) {
      vi.mocked(api.connectInstance).mockResolvedValue({
        state: 'connected',
        local_port: localPort,
        token: 'tok',
      } as unknown as Awaited<ReturnType<typeof api.connectInstance>>)
      const row = screen.getByText(rowName).closest('[data-crew-id]') as HTMLElement
      expect(row).not.toBeNull()
      await userEvent.setup().click(within(row).getByRole('button', { name: /Connect/i }))
      await waitFor(() => expect(api.connectInstance).toHaveBeenCalled())
    }

    it('announces an ssh crew but never one that has no dashboard to embed', async () => {
      vi.mocked(api.listInstances).mockResolvedValue({
        active: true,
        warm_set_cap: 5,
        instances: [FARGATE, SSH, SSM, CHAINED],
      } as unknown as Awaited<ReturnType<typeof api.listInstances>>)
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      renderWithProviders(<RemoteCrewPanel />)
      await screen.findByText('fargate-crew')

      // The fargate connect SUCCEEDS and carries a port -- the exact shape that passed
      // the old gate -- and must still produce no announcement.
      await connect('fargate-crew', 7790)
      expect(announceChainedCrew).not.toHaveBeenCalled()

      // The control: an ssh crew on the same page, same flow, IS announced. Without this
      // the case above would pass just as well if the announce were broken outright.
      vi.mocked(api.connectInstance).mockClear()
      await connect('ssh-crew', 53701)
      await waitFor(() => expect(announceChainedCrew).toHaveBeenCalledTimes(1))
      expect(vi.mocked(announceChainedCrew).mock.calls[0][0]).toMatchObject({
        id: 's9',
        port: 53701,
      })

      // The discriminating control: an SSM crew shares `usesSsmTransport` with fargate
      // but DOES have a dashboard, so it must still be announced. A gate keyed on SSM
      // transport instead of on the dashboard predicate passes both cases above and
      // fails only here.
      vi.mocked(api.connectInstance).mockClear()
      await connect('ssm-crew', 53702)
      await waitFor(() => expect(announceChainedCrew).toHaveBeenCalledTimes(2))
      expect(vi.mocked(announceChainedCrew).mock.calls[1][0]).toMatchObject({
        id: 'm9',
        port: 53702,
      })

      // The depth cap, from the only end that can see it. This crew has a dashboard and
      // a port, so it passes both gates above; only `via_instance_id` separates it, and
      // announcing it leaves the host holding a row its own connect can never satisfy.
      vi.mocked(api.connectInstance).mockClear()
      await connect('chained-crew', 53703)
      await waitFor(() => expect(api.connectInstance).toHaveBeenCalled())
      expect(announceChainedCrew).toHaveBeenCalledTimes(2)
    })
  })

  describe('agent hand-off from the diagnosis note', () => {
    // These live HERE, on the panel SettingsPage actually renders. The same
    // surfaces exist on the unreachable `InstancesPanel`, whose only importers are
    // test files — a hand-off wired there would pass its tests and reach nobody.
    const BROKEN = {
      id: 'c1', name: 'Nimbus', connection_method: 'ssh', ssh_host: 'nimbus-alias',
      remote_port: 5476,
      status: { instance_id: 'c1', state: 'error', error: 'Remote dashboard did not answer' },
    }
    const DIAGNOSED = {
      instance_id: 'c1',
      state: 'error',
      error: 'Remote dashboard did not answer',
      diagnosis: {
        code: 'remote_down', ok: false, reason: 'Remote dashboard down',
        probes: [{ name: 'ssh', ok: true }, { name: 'remote_dashboard', ok: false }],
      },
    }
    /** The hand-off ON THE DIAGNOSIS NOTE. A broken row now carries its own
     *  "Ask the agent" link next to `status.error` (StatusBadge renders it through
     *  ErrorNotice), so the note's button must be picked by its container — the
     *  row's link would send the bare message without the ladder. */
    // The diagnosis note is the shared ErrorNotice (role="alert") since #8749;
    // this helper still looked for the role="status" box #8729 was written
    // against, so `closest` returned null and every hand-off case failed.
    const noteAgentButton = () =>
      within(screen.getByTestId('remote-crew-diagnosis'))
        .getByRole('button', { name: /agent/i })

    it('hands the diagnosis to the agent with the ladder code and probe chain', async () => {
      // A diagnosis that names the broken link and then leaves the user with
      // nothing to do about it is the dead end this change exists to remove. The
      // prompt must carry the verdict CODE and the probes, not the `id: reason`
      // string rendered on screen.
      sessionStorage.clear()
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [BROKEN] } as never)
      ;vi.mocked(api.instanceStatus).mockResolvedValue(DIAGNOSED as never)
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await screen.findByText('Nimbus')
      await openRowMenu(u)
      await u.click(await screen.findByRole('button', { name: /Diagnose Nimbus/i }))
      await screen.findByText(/c1: Remote dashboard down/i)
      await u.click(noteAgentButton())

      const prompt = consumeChatHandoff() || ''
      expect(prompt).toContain('remote_down')
      expect(prompt).toContain('ssh=ok -> remote_dashboard=FAILED')
      expect(prompt).toContain('Nimbus')
    })

    it('drops the diagnosis note and the remove warning once the crew is removed (Bug Bash row 53)', async () => {
      sessionStorage.clear()
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [BROKEN] } as never)
      ;vi.mocked(api.instanceStatus).mockResolvedValue(DIAGNOSED as never)
      ;vi.mocked(api.disconnectInstance).mockResolvedValue({} as never)
      ;vi.mocked(api.removeInstance).mockResolvedValue({} as never)
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)
      await screen.findByText('Nimbus')
      await openRowMenu(u)
      await u.click(await screen.findByRole('button', { name: /Diagnose Nimbus/i }))
      await screen.findByText(/c1: Remote dashboard down/i)
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
      // The row's Remove asks first; the confirm button carries the same name.
      await u.click(await screen.findByRole('button', { name: /Remove Nimbus/ }))
      await screen.findByText(/Remove only takes it off this list/)
      await u.click(await screen.findByRole('button', { name: /Remove Nimbus/ }))
      await waitFor(() => expect(api.removeInstance).toHaveBeenCalled())
      await waitFor(() => expect(screen.queryByText(/c1: Remote dashboard down/i)).toBeNull())
      expect(screen.queryByText(/Remove only takes it off this list/)).toBeNull()
    })

    it('keeps the typed add-form values across that hand-off', async () => {
      // The navigation unmounts this whole panel, the add form included, and a
      // first-time user has just typed the crew by hand. The values are held in the
      // store on every form change rather than by the button, so an exit the button
      // knows nothing about still costs nothing.
      //
      // The SAME store is passed to the remount: that is what an in-app navigation
      // is. A fresh store would model a full page reload, which this deliberately
      // does not cover.
      sessionStorage.clear()
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [BROKEN] } as never)
      ;vi.mocked(api.instanceStatus).mockResolvedValue(DIAGNOSED as never)
      const u = userEvent.setup()
      const first = renderWithProviders(<RemoteCrewPanel />)

      await screen.findByText('Nimbus')
      await u.click(screen.getByRole('button', { name: 'Add Crew' }))
      await u.click(screen.getByRole('button', { name: /Connect a machine you have/ }))
      await u.type(screen.getByPlaceholderText('Crew 1'), 'Cirrus')
      await u.click(screen.getByRole('button', { name: 'Close' }))
      await u.click(await screen.findByRole('button', { name: /Diagnose Nimbus/i }))
      await screen.findByText(/c1: Remote dashboard down/i)
      await u.click(noteAgentButton())
      first.unmount()

      renderWithProviders(<RemoteCrewPanel />, { store: first.store })
      await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
      await u.click(screen.getByRole('button', { name: /Connect a machine you have/ }))
      await waitFor(() =>
        expect(screen.getByPlaceholderText('Crew 1')).toHaveValue('Cirrus'),
      )
    })

    it('keeps an unsaved crew EDIT across that hand-off, and re-opens its form', async () => {
      // Held values nobody re-mounts are the same loss with an extra step, so the
      // row has to re-open on the way back — not merely retain the text somewhere.
      sessionStorage.clear()
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [BROKEN] } as never)
      ;vi.mocked(api.instanceStatus).mockResolvedValue(DIAGNOSED as never)
      const u = userEvent.setup()
      const first = renderWithProviders(<RemoteCrewPanel />)

      await screen.findByText('Nimbus')
      await openRowMenu(u)
      await u.click(await screen.findByRole('button', { name: /Edit settings/i }))
      const host = (await screen.findByLabelText(/SSH host/i, { selector: '#edit-instance-c1-ssh-host' })) as HTMLInputElement
      await u.clear(host)
      await u.type(host, 'nimbus-fixed')
      await openRowMenu(u)
      await u.click(await screen.findByRole('button', { name: /Diagnose Nimbus/i }))
      await screen.findByText(/c1: Remote dashboard down/i)
      await u.click(noteAgentButton())
      first.unmount()

      renderWithProviders(<RemoteCrewPanel />, { store: first.store })
      await waitFor(() =>
        expect(screen.getByLabelText(/SSH host/i, { selector: '#edit-instance-c1-ssh-host' }))
          .toHaveValue('nimbus-fixed'),
      )
    })

    it('measures a restored edit against the record it was OPENED on, not the live one', async () => {
      // The baseline travels with the values as the same object it was captured
      // from. Re-reading the live record on the way back would read a change
      // someone else made during the hand-off as the user's own edit, and write
      // back a field the user never touched.
      // The record must CARRY a ttl for this to discriminate: against a record with
      // no `ttl` key at all, the form's empty string differs from `undefined` and
      // would be sent whatever baseline is used — the test would pass for the wrong
      // reason and then fail for it too.
      const WITH_TTL = { ...BROKEN, ttl: '4h' }
      sessionStorage.clear()
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [WITH_TTL] } as never)
      ;vi.mocked(api.instanceStatus).mockResolvedValue(DIAGNOSED as never)
      const u = userEvent.setup()
      const first = renderWithProviders(<RemoteCrewPanel />)

      await screen.findByText('Nimbus')
      await openRowMenu(u)
      await u.click(await screen.findByRole('button', { name: /Edit settings/i }))
      const host = (await screen.findByLabelText(/SSH host/i, { selector: '#edit-instance-c1-ssh-host' })) as HTMLInputElement
      await u.clear(host)
      await u.type(host, 'nimbus-fixed')
      await openRowMenu(u)
      await u.click(await screen.findByRole('button', { name: /Diagnose Nimbus/i }))
      await screen.findByText(/c1: Remote dashboard down/i)
      await u.click(noteAgentButton())
      first.unmount()

      // Someone else moved the TTL while the user was in the chat.
      ;vi.mocked(api.listInstances).mockResolvedValue(
        { active: true, warm_set_cap: 5, instances: [{ ...WITH_TTL, ttl: '9h' }] } as never,
      )
      renderWithProviders(<RemoteCrewPanel />, { store: first.store })
      const back = (await screen.findByLabelText(/SSH host/i, { selector: '#edit-instance-c1-ssh-host' })) as HTMLInputElement
      await waitFor(() => expect(back).toHaveValue('nimbus-fixed'))
      await u.click(screen.getByRole('button', { name: /Save changes/i }))

      // Only the field the user actually typed is written. `ttl` is absent, so the
      // concurrent change stands.
      await waitFor(() => expect(api.updateInstance).toHaveBeenCalled())
      const body = vi.mocked(api.updateInstance).mock.calls[0]?.[1] as Record<string, unknown>
      expect(body).toMatchObject({ ssh_host: 'nimbus-fixed' })
      expect(body).not.toHaveProperty('ttl')
    })
  })
})
