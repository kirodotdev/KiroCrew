import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel, splitSpotStartHint, splitSweepError, splitSweepRemedy, sweepRemediesFromError } from '../pages/settings/RemoteCrewPanel'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    // Mirrors the real one: the raw response body travels with the error, which
    // is the only place a refused destroy's sweep remedies can arrive.
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

/** Open a crew row's overflow menu — Edit / Stop / Start / Delete live there. */
async function openRowMenu(u: ReturnType<typeof userEvent.setup>, name: RegExp = /More actions/i) {
  await u.click(await screen.findByRole('button', { name }))
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
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // While launches are in flight the list is not classified at all.
    expect(await screen.findByText(/Loading/i)).toBeInTheDocument()
    // No row at all yet — so no overflow menu, and nothing that could delete.
    expect(screen.queryByRole('button', { name: /More actions/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/does not manage this machine/i)).not.toBeInTheDocument()

    releaseLaunches({ jobs: [DONE_JOB] })

    // Once known, it is correctly a cloud row: Stop + the two-step Delete, no plain Remove.
    expect(await screen.findByText('Launched by Kiro Crew')).toBeInTheDocument()
    await openRowMenu(u)
    expect(screen.getByRole('menuitem', { name: 'Stop Kiro Crew Cloud (kc-3f9a)' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /^Remove/i })).not.toBeInTheDocument()
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
    await openRowMenu(u)
    await u.click(screen.getByRole('menuitem', { name: /Remove Kiro Crew Cloud/i }))
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

  it('offers Start so Stop is not a one-way door', async () => {
    // api.cloudStart existed and the route existed, but nothing in the UI called it:
    // a stopped crew had no dashboard path back to running while its EBS kept billing.
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudStart).mockResolvedValue({ started: true } as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Start Kiro Crew Cloud/i }))
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
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))

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
    await u.click(await screen.findByRole('menuitem', { name: /^Stop Kiro Crew Cloud/i }))
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
    vi.mocked(api.cloudDestroy).mockResolvedValue({ cleanup: 'pending' })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Delete Kiro Crew Cloud/i }))
    await u.click(await screen.findByRole('button', { name: /^Confirm deleting/i }))
    await waitFor(() => expect(api.cloudDestroy).toHaveBeenCalledWith('kc-3f9a', expect.anything()))
    // The row now reflects the in-flight teardown and cannot be re-triggered.
    const deleting = await screen.findByRole('button', { name: /Deleting…/i })
    expect(deleting).toBeDisabled()
    // A clean teardown says nothing extra: no leftover-work banner on the
    // on-demand delete every user does, or the real one stops being read.
    expect(screen.queryByText(/aws ec2 cancel-spot-instance-requests/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Spot request/i)).not.toBeInTheDocument()
  })

  it('surfaces the warnings a successful destroy returns, instead of dropping them', async () => {
    // The gateway answers 200 with `warnings` when the delete was ACCEPTED but its
    // Spot sweep left something live — a persistent request keeps handing out
    // replacement instances outside the stack, billing with nothing tracking them.
    // The panel used to read only the error path, so the row went "Deleting…" and
    // the user was never told. Each line arrives self-contained: prose + ids, then
    // the runnable aws command LAST (ec2.grade_spot_sweep guarantees that order).
    const remedy =
      'aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1 --region us-east-1'
    const warning =
      "Could NOT cancel this tag's persistent Spot request(s): sir-1 AccessDenied " +
      `Cancel them yourself or EC2 keeps launching replacements: ${remedy}`
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudDestroy).mockResolvedValue({ cleanup: 'pending', warnings: [warning] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Delete Kiro Crew Cloud/i }))
    await u.click(await screen.findByRole('button', { name: /^Confirm deleting/i }))

    // The prose keeps the ids the user needs to recognize the leftover request…
    const prose = await screen.findByText(/Could NOT cancel this tag's persistent Spot request\(s\): sir-1/)
    expect(prose).toBeInTheDocument()
    // …and the command is CODE, not wrapped prose: this is a command the user has
    // to run because we could not, and a mistyped --spot-instance-request-ids
    // leaves the request live, handing out billing instances. So it must be
    // selectable as one unit, and copyable in one click.
    const code = screen.getByText(remedy)
    expect(code.tagName).toBe('CODE')
    expect(screen.getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
    // The delete itself succeeded, so this is leftover work, not a failed action:
    // it is announced as a status, not as an alert, and the row still shows the
    // teardown it did start.
    expect(code.closest('[role="status"]')).not.toBeNull()
    expect(await screen.findByRole('button', { name: /Deleting…/i })).toBeInTheDocument()
  })

  it('refuses to show a blocked destroy as an in-flight one, and keeps its remedy', async () => {
    // The gateway REFUSES the delete (409) when it could not cancel the crew's
    // persistent Spot request: deleting would terminate the box, re-open the
    // request and let EC2 launch a replacement outside the stack. So the row must
    // NOT go to "Deleting…" — the stack is still there — and the refusal's
    // remedy, which arrives in the error body rather than a 200 payload, must
    // still reach the user: it is the command that unblocks the teardown.
    const remedy =
      'aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1 --region us-east-1'
    const warning =
      "Could NOT cancel this tag's persistent Spot request(s): sir-1 AccessDenied " +
      `Cancel them yourself or EC2 keeps launching replacements: ${remedy}`
    const message =
      "'kc-3f9a' was NOT deleted: its persistent Spot request could not be cancelled. " +
      'The crew, its instance and its disk are untouched.'
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudDestroy).mockRejectedValue(
      new ApiError(409, message, JSON.stringify({
        error: message, code: 'spot_sweep_blocked_destroy', warnings: [warning],
      })),
    )
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Delete Kiro Crew Cloud/i }))
    await u.click(await screen.findByRole('button', { name: /^Confirm deleting/i }))

    expect(await screen.findByText(/was NOT deleted/)).toBeInTheDocument()
    // The command is code with a copy button, exactly as on the accepted path.
    const code = await screen.findByText(remedy)
    expect(code.tagName).toBe('CODE')
    expect(screen.getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
    // Nothing is being torn down, so nothing may claim to be.
    expect(screen.queryByRole('button', { name: /Deleting…/i })).not.toBeInTheDocument()
  })

  it('gives a notice the neutral note treatment instead of the warning block', async () => {
    // "Could not check — nothing proves it either way" and "this is STILL billing
    // and only you can stop it" are not the same claim. Rendering both in the amber
    // block spent the loud treatment on the line with no known consequence, which
    // is exactly how the loud one stops being read. Notices get the panel's neutral
    // note treatment (the same one `diagNote` uses); warnings keep the amber block.
    const remedy = 'aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1'
    const warning = `Could NOT cancel this tag's persistent Spot request(s): sir-1 Cancel them yourself: ${remedy}`
    const notice =
      'Could not check for a leftover Spot request (no permission) — and with no ' +
      'stack left there is nothing to prove it either way; check the EC2 console ' +
      'if this tag ever ran --spot.'
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB] })
    vi.mocked(api.cloudDestroy).mockResolvedValue({ cleanup: 'pending', warnings: [warning], notices: [notice] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u)
    await u.click(await screen.findByRole('menuitem', { name: /^Delete Kiro Crew Cloud/i }))
    await u.click(await screen.findByRole('button', { name: /^Confirm deleting/i }))

    const noticeBlock = (await screen.findByText(notice)).closest('[role="status"]')
    const warnBlock = screen.getByText(remedy).closest('[role="status"]')
    expect(noticeBlock).not.toBeNull()
    expect(warnBlock).not.toBeNull()
    // Two separate blocks, and only the warning wears the warn tone.
    expect(noticeBlock).not.toBe(warnBlock)
    expect(warnBlock?.className).toContain('warn')
    expect(noticeBlock?.className).not.toContain('warn')
  })

  it('shows the enable CTA when the feature is disabled (403)', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(new ApiError(403, 'instances feature is disabled'))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
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

    // While loading: a spinner, no tabs, no form.
    expect(screen.getByText(/Loading/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Your crews/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Set up a new one/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Enable remote crew management/i })).not.toBeInTheDocument()

    // After the 403 resolves: transitions directly to the disabled card.
    rejectInstances(new ApiError(403, 'instances feature is disabled'))
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Your crews/i })).not.toBeInTheDocument()
  })

  it('distinguishes cloud crews from hand-added machines, and shows an in-progress launch', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE, MANUAL_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [DONE_JOB, RUNNING_JOB] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // Cloud row carries the cloud attribution + a Stop control; manual row does not.
    expect(await screen.findByText('Launched by Kiro Crew')).toBeInTheDocument()
    expect(screen.getByText(/does not manage this machine/i)).toBeInTheDocument()
    await openRowMenu(u, /More actions for Kiro Crew Cloud/i)
    expect(screen.getByRole('menuitem', { name: 'Stop Kiro Crew Cloud (kc-3f9a)' })).toBeInTheDocument()

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

describe('splitSweepRemedy', () => {
  // The gateway flattens each leftover-work line to "prose … runnable command",
  // with the command LAST by contract (ec2.grade_spot_sweep). The split has to
  // honour that contract rather than take the first thing that looks like a
  // command: the prose in front of it is a raw AWS error nobody controls.
  const command =
    'aws ec2 terminate-instances --instance-ids i-0orphan --profile dev --region eu-west-1'

  it('peels the trailing command off the prose', () => {
    expect(splitSweepRemedy(`They keep billing until you do: ${command}`)).toEqual({
      prose: 'They keep billing until you do:',
      command,
    })
  })

  it('is not fooled by an AWS error that quotes the API call', () => {
    // A denial names the call it refused ("…not authorized to run aws ec2
    // terminate-instances…"), which a first-match split takes for the start of
    // the command — putting half the error inside the code block and handing the
    // user a "command" that is not one.
    const line =
      'Could NOT terminate the Spot instance(s): i-0orphan ' +
      'AccessDenied: not authorized to run aws ec2 terminate-instances on i-0orphan. ' +
      command
    const { prose, command: cmd } = splitSweepRemedy(line)
    expect(cmd).toBe(command)
    expect(prose).toContain('AccessDenied')
    expect(prose.endsWith('.')).toBe(true)
  })

  it('leaves an informational line whole', () => {
    const note = 'Agent sessions cannot sweep Spot requests — run destroy from a terminal.'
    expect(splitSweepRemedy(note)).toEqual({ prose: note, command: '' })
  })
})

describe('splitSweepError', () => {
  // Inside the prose the grader hands over, one stretch is AWS's own text and the
  // rest is ours. It is also the longest, so at equal weight it buries what the
  // user can act on — hence the muted treatment, hence this split.
  it('separates the raw AWS failure from the line we wrote', () => {
    const { summary, awsError } = splitSweepError(
      "Could NOT cancel this tag's persistent Spot request(s): sir-1 " +
        'ec2:CancelSpotInstanceRequests failed: An error occurred (UnauthorizedOperation) ' +
        'when calling the CancelSpotInstanceRequests operation. ' +
        'Cancel them yourself or EC2 keeps launching replacements:',
    )
    expect(summary).toBe("Could NOT cancel this tag's persistent Spot request(s): sir-1")
    expect(awsError.startsWith('ec2:CancelSpotInstanceRequests failed:')).toBe(true)
  })

  it('mutes nothing when the line carries no AWS error', () => {
    // The agent-session refusal is our own sentence, not AWS's; so is a summary
    // that had no error to report. Nothing to fade there.
    const line = 'Could NOT check for a leftover persistent Spot request — the lookup was denied.'
    expect(splitSweepError(line)).toEqual({ summary: line, awsError: '' })
  })
})

describe('splitSpotStartHint', () => {
  // The gateway can only reach the panel through the error STRING, so it appends
  // ec2.SPOT_START_FAILURE_HINT to it behind a single newline (handlers_cloud).
  // Rendered raw that puts "Do NOT destroy the instance to 'fix' this" in the red
  // banner, beside a Delete button that deletes the volume the interruption
  // preserved. The split is on that newline alone — no English is load-bearing.
  const awsError =
    'ec2:StartInstances failed: An error occurred (IncorrectSpotRequestState) when calling ' +
    'the StartInstances operation: Only Amazon EC2 can restart an interrupted stopped ' +
    'Spot Instance.'
  const flattened =
    `${awsError}\n` +
    'This crew was launched with --spot, so the most likely cause is an EC2 INTERRUPTION ' +
    'stop, not a broken instance. ' +
    'Your data is intact: an interruption stops the instance, it does not terminate it, so ' +
    'the root volume (and ~/.kiro/crew on it) is untouched. ' +
    "Do NOT destroy the instance to 'fix' this — destroy deletes that volume and everything " +
    'on it. Wait for the auto-resume, or check `kirocrew cloud status`.'

  it('keeps the AWS failure as the error and gives the hint its own lines', () => {
    const { error, hint } = splitSpotStartHint(flattened)
    expect(error).toBe(awsError)
    // Every sentence is a line, and each one still ends as a sentence.
    expect(hint.every(l => l.endsWith('.'))).toBe(true)
    expect(hint[0].startsWith('This crew was launched with --spot')).toBe(true)
    // The line that has to be impossible to miss stands on its own.
    expect(hint).toContain(
      "Do NOT destroy the instance to 'fix' this — destroy deletes that volume and everything on it.",
    )
    // Paths and commands are not sentence ends, so they are not split apart.
    expect(hint.some(l => l.includes('~/.kiro/crew'))).toBe(true)
    expect(hint.some(l => l.includes('`kirocrew cloud status`'))).toBe(true)
  })

  it('leaves every other failure exactly as it arrived', () => {
    // No newline, no hint: the gateway only ever adds one when it has one to add.
    expect(splitSpotStartHint(awsError)).toEqual({ error: awsError, hint: [] })
  })

  it('splits on the delimiter, not on the hint wording', () => {
    // The hint is translatable prose and gets reworded; the seam must survive
    // that, so nothing English is matched. Any text after the newline is hint.
    const { error, hint } = splitSpotStartHint(`${awsError}\nCeci est une note. Voilà.`)
    expect(error).toBe(awsError)
    expect(hint).toEqual(['Ceci est une note.', 'Voilà.'])
  })
})

describe('sweepRemediesFromError', () => {
  it('recovers the remedies a refused destroy carries in its body', () => {
    const warnings = ['Could NOT cancel …: sir-1 aws ec2 cancel-spot-instance-requests …']
    expect(sweepRemediesFromError(new ApiError(409, 'nope', JSON.stringify({ warnings })))).toEqual(warnings)
  })

  it('stays empty for every other failure', () => {
    // No remedy block on an ordinary 500, a non-JSON body, or a plain Error —
    // and a `warnings` array is trusted for its strings only.
    expect(sweepRemediesFromError(new ApiError(500, 'boom', 'not json'))).toEqual([])
    expect(sweepRemediesFromError(new ApiError(500, 'boom', '{"error":"boom"}'))).toEqual([])
    expect(sweepRemediesFromError(new ApiError(409, 'x', '{"warnings":[1,"ok"]}'))).toEqual(['ok'])
    expect(sweepRemediesFromError(new Error('boom'))).toEqual([])
  })
})
