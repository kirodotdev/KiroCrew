import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
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
import { api, ApiError } from '../api/client'

const CLOUD_INSTANCE = {
  id: 'kc1',
  name: 'Kiro Crew Cloud (kc-3f9a)',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: '',
  aws_region: 'us-east-1',
  ssm_run_as: '',
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
  size_key: 'balanced', status: 'done' as const, steps: [], signin: null, created_at: 0, updated_at: 0,
}
const RUNNING_JOB = {
  id: 'j-run', tag: 'kc-4d10', profile: '', region: 'us-east-1', size_key: 'light',
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

// localStorage is cleared too: the panel now persists the AWS profile/region, so a
// test that seeds them would otherwise dictate what later tests probe.
beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
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
    renderWithProviders(<RemoteCrewPanel />)

    // While launches are in flight the list is not classified at all.
    expect(await screen.findByText(/Loading/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Remove/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/does not manage this machine/i)).not.toBeInTheDocument()

    releaseLaunches({ jobs: [DONE_JOB] })

    // Once known, it is correctly a cloud row: Stop + the two-step Delete, no plain Remove.
    expect(await screen.findByText('Launched by Kiro Crew')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop Kiro Crew Cloud (kc-3f9a)' })).toBeInTheDocument()
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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

    expect(await screen.findByText(/WXYZ-1234/)).toBeInTheDocument()
    expect(document.querySelector('a[href="https://device.sso/verify"]')).not.toBeNull()
    await waitFor(() => expect(api.cloudLaunchStatus).toHaveBeenCalledWith('j-signin'))
  })

  it('refreshes the crew list when a launch finishes, without waiting for a manual reload', async () => {
    // Switching tabs does not remount the panel, so nothing would invalidate the
    // instances cache and the brand-new crew would stay missing from Your crews.
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

    // Not labelled as hand-added, because we cannot know that.
    expect(await screen.findByText(/cannot verify whether this machine has AWS resources/i)).toBeInTheDocument()
    expect(screen.queryByText(/does not manage this machine/i)).not.toBeInTheDocument()

    // The trash is confirm-gated, and the warning states what Remove does NOT do.
    await u.click(screen.getByRole('button', { name: /Remove Kiro Crew Cloud/i }))
    expect(await screen.findByText(/keeps running and billing/i)).toBeInTheDocument()
    expect(api.removeInstance).not.toHaveBeenCalled()
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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

    // The localized "not installed" line still explains the gap…
    expect(await screen.findByText(/Session Manager plugin/i)).toBeInTheDocument()
    // …but no Copy button appears with nothing to copy.
    expect(screen.queryByRole('button', { name: /Copy command/i })).not.toBeInTheDocument()
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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

    const profileInput = await screen.findByLabelText(/AWS profile/i)
    const credsRow = await screen.findByText(/Credentials/i)
    // compareDocumentPosition: 4 = FOLLOWING — the row comes after the input.
    expect(profileInput.compareDocumentPosition(credsRow) & 4).toBeTruthy()

    // And the probed identity is stated, not left implicit.
    expect(await screen.findByText(/Checked against profile .* in us-east-1/i)).toBeInTheDocument()
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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

    const card = (await screen.findByText(/WXYZ-1234/)).closest('div')?.parentElement
    expect(card).toBeTruthy()
    const page = document.body.textContent ?? ''
    expect(page).toMatch(/leave the page or switch crews and it keeps going/i)
    expect(page).not.toMatch(/quit the app/i)
    expect(page).not.toMatch(/get a notification/i)
  })

  it('shows the enable CTA when the feature is disabled (403)', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'instances feature is disabled'))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Enable remote crew management/i })).toBeInTheDocument()
  })

  it('distinguishes cloud crews from hand-added machines, and shows an in-progress launch', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE, MANUAL_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB, RUNNING_JOB] })
    renderWithProviders(<RemoteCrewPanel />)

    // Cloud row carries the cloud attribution + a Stop control; manual row does not.
    expect(await screen.findByText('Launched by Kiro Crew')).toBeInTheDocument()
    expect(screen.getByText(/does not manage this machine/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop Kiro Crew Cloud (kc-3f9a)' })).toBeInTheDocument()

    // The still-launching job shows a "Setting up" row with step progress + the note.
    expect(screen.getByText(/Setting up/)).toBeInTheDocument()
    expect(screen.getByText(/Step 3 of 4/)).toBeInTheDocument()
    expect(screen.getByText(/Keeps running if you leave this page/i)).toBeInTheDocument()
  })

  it('enables Launch only once the AWS prerequisites pass', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue({ ...PREFLIGHT_OK, session_manager_plugin: false })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    // Prereq checklist rendered; a missing plugin blocks Launch.
    expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
    expect(screen.getByText(/Session Manager plugin/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: /^Launch$/ })).toBeDisabled())
    expect(screen.getByText(/Finish the AWS setup above/i)).toBeInTheDocument()
  })

  it('renders each size card headlined by its interpolated sub-agent count', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
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

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
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

    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    const launch = await screen.findByRole('button', { name: /^Launch$/ })
    await waitFor(() => expect(launch).not.toBeDisabled())
    await u.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledWith({ profile: '', region: 'us-east-1', size_key: 'balanced' }))
    // Progress card polls the job and renders its steps.
    expect(await screen.findByText('Installing Kiro Crew')).toBeInTheDocument()
  })
})
