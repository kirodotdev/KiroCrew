/**
 * Rescuing a cloud crew that was created WITHOUT a Kiro sign-in.
 *
 * The dead end these pin: a launch registered the instance, the card went green,
 * Connect was offered, and the remote dashboard then answered every chat with
 * "not logged in" — fixable only by running `kiro-cli login` in a terminal that
 * dashboard does not have. So the card must (a) not claim ready, (b) keep the
 * code reachable from BOTH tabs, and (c) be able to ask for a fresh one.
 *
 * The launch-form identity question (Personal vs Company SSO) is a separate,
 * already-shipped feature and is covered by its own tests; nothing about that
 * form belongs here.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

// The guarded helper, not `navigator.clipboard`: on a plain-HTTP dashboard the
// API is absent, and the point of one of these tests is that the card reports
// that rather than painting "Copied" regardless.
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
import { api, ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

const START_URL = 'https://amzn.awsapps.com/start'

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

/** The steps a launch leaves behind when it registered a crew but never got a
 *  sign-in confirmed: connect `done`, sign-in `skipped`. */
const STEPS_REGISTERED = [
  { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
  { key: 'provision', label: 'Create the instance', state: 'done' as const },
  {
    key: 'signin',
    label: 'Sign in to Kiro',
    state: 'skipped' as const,
    // Verbatim from `launch_job.py`. A fixture that invents the detail tests a
    // screen that does not ship.
    detail: 'Not signed in yet — finish it in the sign-in box below.',
  },
  {
    key: 'connect',
    label: 'Connect',
    state: 'done' as const,
    detail: 'Added to Your crews. Finish the Kiro sign-in before connecting.',
  },
]
const UNSIGNED_JOB = {
  id: 'j-unsigned', tag: 'kc-5e10bb', instance_id: 'i-0abc123456789def0',
  profile: '', region: 'us-east-1', provider_id: 'aws_ec2', size_key: 'balanced',
  status: 'done' as const, steps: STEPS_REGISTERED, signin: null,
  signin_detected: false, created_at: 0, updated_at: 0,
}
/** The same crew, launched through the company portal. */
const UNSIGNED_SSO_JOB = {
  ...UNSIGNED_JOB,
  login_target: { license: 'pro' as const, start_url: START_URL, region: 'us-east-1' },
}
const CLOUD_INSTANCE = {
  id: 'kc1',
  name: 'Kiro Crew Cloud (kc-5e10bb)',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: 'us-east-1',
  remote_port: 8765,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  provisioner_id: 'aws_ec2',
  status: { instance_id: 'kc1', state: 'disconnected' as const },
}

const withCode = (code: string, over: Record<string, unknown> = {}) => ({
  ...UNSIGNED_JOB,
  signin: { url: `${START_URL}/#/device?user_code=${code}`, code },
  ...over,
})

/**
 * Point BOTH job reads at the same fixture. The card polls
 * `cloudLaunchStatus` and the rows read `cloudLaunches`, and the panel
 * deliberately trusts the POLLED copy — so a mock that moved only one of them
 * silently reverts the state mid-test and the assertion passes or fails on
 * timing.
 */
const useJob = (job: unknown) => {
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
  useJob(UNSIGNED_JOB)
  vi.mocked(copyToClipboard).mockResolvedValue(true)
})

/** Open the setup tab, where the launch progress card lives. */
const openSetup = async (u: ReturnType<typeof userEvent.setup>) => {
  await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
}

describe('a crew created without a Kiro sign-in — the progress card', () => {
  it('does not report the launch as ready when the sign-in never confirmed', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByText(/Needs sign-in/i)).toHaveAttribute(
      'title',
      'This crew exists, but Kiro is not signed in on it yet — every chat on it will fail until the sign-in is finished.',
    )
    // And the footer must not say "your new instance is ready" under that badge.
    expect(screen.getByText(/not signed in on it yet/i)).toBeInTheDocument()
  })

  it('marks the held connect step as waiting rather than done', async () => {
    // A green tick beside "Finish the Kiro sign-in before connecting" reads as
    // done and not-done at the same time.
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const icon = await screen.findByTestId('step-waiting-connect')
    const row = icon.closest('li')
    expect(row).toHaveTextContent(/Added to Your crews\./i)
    expect(row?.querySelector('.text-ok')).toBeNull()
    // A warn-tinted hollow circle, deliberately NOT the key: the key marks the
    // step in the user's court; this step ran and is only held.
    expect(icon.classList.contains('text-warn')).toBe(true)
    expect(icon.classList.contains('lucide-circle')).toBe(true)
  })

  it('marks the unconfirmed sign-in step as waiting, not as not-started', async () => {
    // An empty circle beside the step the card is actively asking you to finish
    // reads as going backwards from "in progress".
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const icon = await screen.findByTestId('step-waiting-signin')
    expect(icon.classList.contains('text-warn')).toBe(true)
    // Not a swap: the connect step keeps its own waiting mark.
    expect(screen.getByTestId('step-waiting-connect')).toBeInTheDocument()
  })

  it('keeps the connect step waiting for the whole approval, not only once the job ends', async () => {
    // The approval is a non-terminal, registered job — most of the time a user
    // spends on this card — so gating the waiting icon on the job being over put
    // the green check back exactly when the step still says to finish first.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByTestId('step-waiting-connect')).toBeInTheDocument()
  })

  it('leaves a signed-in crew every step it earned', async () => {
    const signed = {
      ...UNSIGNED_JOB,
      signin_detected: true,
      steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'done' as const, detail: '' } : s)),
    }
    useJob(signed)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await screen.findByText(/Kiro Crew Cloud \(kc-5e10bb\)/i)
    expect(screen.queryByTestId('step-waiting-signin')).not.toBeInTheDocument()
    expect(screen.queryByTestId('step-waiting-connect')).not.toBeInTheDocument()
    expect(screen.queryByText(/Needs sign-in/i)).not.toBeInTheDocument()
  })
})

describe('the code box', () => {
  it('titles itself by what to do with the code', async () => {
    // "Sign in to Kiro" is already the step, the badge and the banner.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByTestId('signin-prompt')).toHaveTextContent(/Approve your sign-in code/i)
  })

  it('names the company SSO account when the crew was launched through one', async () => {
    // "Sign-in" means two things here — the Kiro sign-in that is the step, and
    // the SSO sign-in that carries it out. The reader had to assume they were the
    // same act; the box now says so.
    useJob({ ...UNSIGNED_SSO_JOB, signin: { url: `${START_URL}/#/device?user_code=OLD`, code: 'OLD' } })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByTestId('signin-prompt'))
      .toHaveTextContent(/Approve with your company SSO account/i)
  })

  it('keeps the identity-free title when no launch identity was recorded', async () => {
    // The Builder ID path, and every job launched before that field existed,
    // must not claim a company SSO account it never had.
    useJob(withCode('OLD'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const prompt = await screen.findByTestId('signin-prompt')
    expect(prompt).toHaveTextContent(/Approve your sign-in code/i)
    expect(prompt).not.toHaveTextContent(/company SSO account/i)
  })

  it('copies the code when the code itself is clicked', async () => {
    // The reader tried clicking the code to copy it; it was a `<code>` element.
    useJob(withCode('OLD-CODE'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const copy = await screen.findByTestId('signin-code-copy')
    expect(copy).toHaveAttribute('aria-label', expect.stringContaining('OLD-CODE'))
    await u.click(copy)
    expect(copyToClipboard).toHaveBeenCalledWith('OLD-CODE')
    await waitFor(() => expect(screen.getByText(/^Copied$/i)).toBeInTheDocument())
  })

  it('says a copy failed rather than reporting one that did not happen', async () => {
    // On a plain-HTTP dashboard `navigator.clipboard` is absent. A silent failure
    // sends the reader to paste nothing into the browser and blame the code.
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    useJob(withCode('OLD-CODE'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByTestId('signin-code-copy'))
    await waitFor(() => expect(screen.getByText(/Copy failed/i)).toBeInTheDocument())
    expect(screen.queryByText(/^Copied$/i)).not.toBeInTheDocument()
  })

  it('calls a stale code stale and offers a replacement', async () => {
    useJob(withCode('OLD-CODE'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const prompt = await screen.findByTestId('signin-prompt')
    expect(prompt).toHaveTextContent(/OLD-CODE/)
    expect(prompt).toHaveTextContent(/could not confirm the sign-in/i)
    expect(screen.getByRole('button', { name: /Start over with a new code/i })).toBeInTheDocument()
  })

  it('offers no replacement while the gateway is still polling the shown code', async () => {
    // Restarting mid-approval invalidates the code the user is typing.
    useJob(withCode('LIVE-CODE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByText(/LIVE-CODE/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Start over with a new code/i })).not.toBeInTheDocument()
  })

  it('fetches the pending prompt for a job that was already awaiting approval', async () => {
    // The prompt lives on the gateway: a job that reached `awaiting_signin`
    // before this tab opened has nothing to render until the dashboard asks.
    useJob({ ...UNSIGNED_JOB, status: 'awaiting_signin' as const, signin: null })
    vi.mocked(api.cloudLaunchSignin).mockResolvedValue(
      { signin: { url: `${START_URL}/#/device?user_code=FETCHED`, code: 'FETCHED' } } as never,
    )
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    // And it must not promise a browser tab: the click renders the code inline,
    // so "Open sign-in page" (with the external-link icon) described an outcome
    // that never happened. The real page link is the anchor beside a code.
    const fetch = await screen.findByRole('button', { name: /Show the sign-in code/i })
    expect(fetch).not.toHaveTextContent(/Open sign-in page/i)
    expect(screen.queryByRole('link', { name: /Open sign-in page/i })).not.toBeInTheDocument()
    await u.click(fetch)
    await waitFor(() => expect(api.cloudLaunchSignin).toHaveBeenCalledWith('j-unsigned'))
    // Fetching must not restart the login and retire a code already in a browser.
    expect(api.cloudLaunchSigninRestart).not.toHaveBeenCalled()
  })
})

describe('what Cancel destroys', () => {
  it('says a retry only stops the sign-in and keeps the crew', async () => {
    // The reader "would not dare click it" — the same word covered stopping a
    // sign-in and tearing down the instance that was already created.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const cancel = await screen.findByRole('button', {
      name: /Stop the sign-in on kc-5e10bb and keep the crew/i,
    })
    expect(cancel).toHaveTextContent(/Stop sign-in, keep the crew/i)
    // The crew exists: nothing here removes it.
    expect(cancel).not.toHaveTextContent(/remove/i)
    // And it is called a crew, the word the panel headers use. "Instance" here
    // read as a second, different thing the reader had to guess at — the panel
    // reserves that word for the EC2 machine (see `stamped_ec2_note`), so a
    // label naming the row must not borrow it.
    expect(cancel).not.toHaveTextContent(/\binstance\b/i)
  })

  it('says a launch that has not registered yet removes the crew', async () => {
    // Same button, opposite blast radius. Read off the same `isRegistered` test
    // the rest of the panel uses, so the two cannot drift.
    useJob({
      ...UNSIGNED_JOB,
      status: 'running' as const,
      signin: null,
      steps: [
        { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
        { key: 'provision', label: 'Create the instance', state: 'active' as const },
        { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
        { key: 'connect', label: 'Connect', state: 'pending' as const },
      ],
    })
    renderWithProviders(<RemoteCrewPanel />)

    const cancel = await screen.findByRole('button', {
      name: /Cancel setup of kc-5e10bb and remove the crew/i,
    })
    expect(cancel).toHaveTextContent(/Cancel and remove the crew/i)
    expect(cancel).not.toHaveTextContent(/keep the crew/i)
  })
})

describe('the outcome hint', () => {
  it('warns that replacing a code retires the one on screen', async () => {
    // Irreversible for a code the user may be typing into a browser right now.
    useJob(withCode('OLD-CODE'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    // One hint slot now serves the recheck primary AND the replace secondary, so
    // it must still warn that replacing retires the code -- an earlier UX finding.
    expect(await screen.findByTestId('signin-recovery-hint'))
      .toHaveTextContent(/Starting over with a new code retires the code above/i)
  })

  it('says a first sign-in starts one, since there is no code to resume', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const hint = await screen.findByTestId('signin-recovery-hint')
    expect(hint).toHaveTextContent(/Starts the sign-in on the crew and shows a new code here\./i)
    expect(hint).not.toHaveTextContent(/retires it/i)
  })

  it('offers no hint while the shown code is still being polled', async () => {
    // There is no recovery button in that state, so a hint would describe a
    // click the reader cannot make.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await screen.findByTestId('signin-prompt')
    expect(screen.queryByTestId('signin-recovery-hint')).not.toBeInTheDocument()
  })
})

describe('the restart, and the state before a code exists', () => {
  it('asks the gateway for a fresh code and shows the new one when it arrives', async () => {
    const fresh = withCode('NEW-CODE', { status: 'awaiting_signin' as const })
    vi.mocked(api.cloudLaunchSigninRestart).mockImplementation(async () => {
      vi.mocked(api.cloudLaunchStatus).mockResolvedValue(fresh as never)
      return fresh as never
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /Start sign-in/i }))
    await waitFor(() => expect(api.cloudLaunchSigninRestart).toHaveBeenCalledWith('j-unsigned'))
    expect(await screen.findByText(/NEW-CODE/)).toBeInTheDocument()
  })

  it('says the sign-in is starting, with no code button, before a code exists', async () => {
    // Reachable through the RESTART route only: the prompt block needs the
    // connect step done, which an initial launch has not reached yet.
    useJob({
      ...UNSIGNED_JOB,
      status: 'running' as const,
      signin: null,
      steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'active' as const, detail: '' } : s)),
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const prompt = await screen.findByTestId('signin-prompt')
    expect(prompt).toHaveTextContent(/Starting the Kiro sign-in on the crew\./i)
    expect(prompt).toHaveTextContent(/takes a few seconds/i)
    // Nothing to approve and nothing to replace: either button could not work.
    expect(screen.queryByRole('button', { name: /Start over with a new code|Show the sign-in code/i })).not.toBeInTheDocument()
    expect(screen.queryByTestId('signin-recovery-hint')).not.toBeInTheDocument()
  })
})

describe('Your crews — an unsigned cloud crew', () => {
  beforeEach(() => {
    vi.mocked(api.listInstances).mockResolvedValue(
      { active: true, warm_set_cap: 5, instances: [CLOUD_INSTANCE] } as never,
    )
  })

  it('holds Connect back and says why', async () => {
    // Connecting opens a remote dashboard whose chats all fail, and whose fix
    // needs a terminal it does not have.
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByRole('button', { name: /Connect after sign-in/i })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /^Connect$/i })).not.toBeInTheDocument()
    expect(screen.getByText(/Needs sign-in/i)).toBeInTheDocument()
  })

  it('names its crew and sits under the row header, not above every row', async () => {
    // Rendered above the name and titled "This crew", the block read as a
    // page-level warning banner about the whole panel — and with several rows it
    // attributed the sign-in to whichever crew the reader was looking at.
    renderWithProviders(<RemoteCrewPanel />)

    const prompt = await screen.findByTestId('signin-prompt')
    // No code yet on this row, so the title names the action, not a code the
    // reader has not been given -- and still names the crew.
    expect(prompt).toHaveTextContent(/Get a sign-in code for Kiro Crew Cloud \(kc-5e10bb\)/i)
    const header = screen.getByText('Kiro Crew Cloud (kc-5e10bb)')
    expect(header.compareDocumentPosition(prompt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Inside the row it belongs to, not a sibling of the whole list.
    expect(prompt.closest('[data-crew-id="kc1"]')).not.toBeNull()
  })

  it('starts the sign-in from the crew row, without going back to the setup tab', async () => {
    vi.mocked(api.cloudLaunchSigninRestart).mockResolvedValue(UNSIGNED_JOB as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const row = await screen.findByTestId('signin-prompt')
    expect(row).toHaveTextContent(/Starts the sign-in on the crew/i)
    await u.click(await screen.findByRole('button', { name: /Start sign-in/i }))
    await waitFor(() => expect(api.cloudLaunchSigninRestart).toHaveBeenCalledWith('j-unsigned'))
  })

  it('keeps the badge and the recovery controls after auto-connect', async () => {
    // Auto-connect is default-on, so an unsigned crew is routinely connected
    // already — and that is exactly when the chats fail, so hiding the badge on
    // `connected` removed the recovery from the only screen that offered it.
    vi.mocked(api.listInstances).mockResolvedValue(
      {
        active: true,
        warm_set_cap: 5,
        instances: [{ ...CLOUD_INSTANCE, status: { instance_id: 'kc1', state: 'connected' } }],
      } as never,
    )
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText(/Needs sign-in/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Start sign-in/i })).toBeInTheDocument()
  })

  it('leaves a signed-in crew alone', async () => {
    useJob({ ...UNSIGNED_JOB, signin_detected: true })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByRole('button', { name: /^Connect$/i })).toBeEnabled()
    expect(screen.queryByText(/Needs sign-in/i)).not.toBeInTheDocument()
    expect(screen.queryByTestId('signin-prompt')).not.toBeInTheDocument()
  })

  it('does not list a sign-in retry as a second instance being set up', async () => {
    // The retry is a non-terminal job on a crew that already exists; a "Setting
    // up" row for it reads as a second box being created — and billed.
    useJob({ ...UNSIGNED_JOB, status: 'awaiting_signin' as const })
    renderWithProviders(<RemoteCrewPanel />)

    // findAll, not find: an unsigned row names its crew twice on purpose — the
    // row header, and the sign-in block that must not be misattributed.
    expect(await screen.findAllByText(/Kiro Crew Cloud \(kc-5e10bb\)/i)).not.toHaveLength(0)
    expect(screen.queryByText(/Setting up/i)).not.toBeInTheDocument()
  })
})

describe('round 4 — the surfaces the reviewers could not tell apart', () => {
  const SSO_TARGET = { license: 'pro', start_url: 'https://amzn.awsapps.com/start', region: 'us-east-1' }

  it('reports a failed copy through the panel error surface, not a bare span', async () => {
    // A failure styled unlike every neighbouring error is the one the reader
    // skips. `PrereqRow` uses ErrorNotice for its copy failures; so does this.
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    const stale = {
      ...UNSIGNED_JOB,
      login_target: SSO_TARGET,
      signin: { url: 'https://amzn.awsapps.com/start/#/device?user_code=OLD', code: 'OLD' },
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [stale] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(stale as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByTestId('signin-code-copy'))
    const notice = await screen.findByTestId('signin-copy-error')
    expect(notice).toHaveTextContent(/Copy failed/i)
  })

  it('says what the fetch button produces, so it is not confused with a fresh sign-in', async () => {
    // This was the ONE recovery button whose hint was gated off, which is why
    // "Start sign-in" and "Show the sign-in code" looked interchangeable.
    const awaitingNoCode = {
      ...UNSIGNED_JOB,
      status: 'awaiting_signin' as const,
      login_target: SSO_TARGET,
      signin: null,
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingNoCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingNoCode as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const hint = await screen.findByTestId('signin-recovery-hint')
    expect(hint).toHaveTextContent(/already waiting on/i)
    expect(hint).toHaveTextContent(/does not start a new sign-in/i)
  })

  it('names the company approval as the Kiro sign-in itself, not a second one', async () => {
    const awaitingSso = {
      ...UNSIGNED_JOB,
      status: 'awaiting_signin' as const,
      login_target: SSO_TARGET,
      signin: { url: 'https://amzn.awsapps.com/start/#/device?user_code=LIVE', code: 'LIVE' },
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingSso] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingSso as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const prompt = await screen.findByTestId('signin-prompt')
    expect(prompt).toHaveTextContent(/That approval IS the Kiro sign-in/i)
  })

  it('keeps the plain hint for a launch with no company identity', async () => {
    // The Builder ID path has only one sign-in, so the "there is not a second
    // one" clause would be answering a question nobody asked.
    const awaitingPersonal = {
      ...UNSIGNED_JOB,
      status: 'awaiting_signin' as const,
      login_target: null,
      signin: { url: 'https://example/?user_code=LIVE', code: 'LIVE' },
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingPersonal] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingPersonal as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const prompt = await screen.findByTestId('signin-prompt')
    expect(prompt).not.toHaveTextContent(/company account/i)
  })
})

describe('a preserved code the user has already approved', () => {
  const STALE = {
    ...UNSIGNED_JOB,
    login_target: { license: 'pro', start_url: 'https://amzn.awsapps.com/start', region: 'us-east-1' },
    signin: { url: 'https://amzn.awsapps.com/start/#/device?user_code=OLD', code: 'OLD' },
  }

  const renderStale = async () => {
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [STALE] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(STALE as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    return u
  }

  it('offers a recheck that reaches the gateway probe, not only a replacement', async () => {
    // The re-probe lives behind this call. It was unreachable while the button
    // rendered only for `awaiting`, so the reader's sole option on a preserved
    // code was to replace it -- discarding the approval they had just given.
    const u = await renderStale()
    const recheck = await screen.findByRole('button', { name: /I approved it/i })
    await u.click(recheck)
    await waitFor(() => expect(api.cloudLaunchSignin).toHaveBeenCalledWith(STALE.id))
  })

  it('makes the recheck the primary and the replacement secondary', async () => {
    // Three peer actions with no primary is what the reader was choosing between.
    await renderStale()
    const recheck = await screen.findByRole('button', { name: /I approved it/i })
    const replace = await screen.findByRole('button', { name: /Start over with a new code/i })
    // The primary variant carries the accent background; the secondary does not.
    expect(recheck.className).not.toEqual(replace.className)
    expect(recheck.className).toMatch(/accent|primary/i)
  })

  it('says the recheck keeps the code, so it is tried before replacing it', async () => {
    await renderStale()
    const hint = await screen.findByTestId('signin-recovery-hint')
    expect(hint).toHaveTextContent(/keeping the code above/i)
    expect(hint).toHaveTextContent(/Starting over with a new code retires the code above/i)
  })

  it('keeps the code row to two actions and puts the recheck in its own row', async () => {
    // Copy chip, page link and the recheck primary as one flex row were three
    // peer actions -- the reader ranked them. The code's actions and the job's
    // action are now separate rows.
    await renderStale()
    const codeRow = await screen.findByTestId('signin-code-row')
    const actionRow = screen.getByTestId('signin-action-row')
    expect(codeRow).not.toBe(actionRow)
    expect(within(codeRow).getAllByRole('button')).toHaveLength(1) // the copy chip
    expect(within(codeRow).getAllByRole('link')).toHaveLength(1) // the page link
    expect(within(actionRow).getAllByRole('button')).toHaveLength(1) // the recheck
    expect(within(actionRow).getByRole('button')).toHaveAccessibleName(/I approved it/i)
  })

  it('offers no agent hand-off on a failed copy', async () => {
    // "Select the text and copy it manually" is the whole remedy; an agent cannot
    // supply a clipboard the browser refused, and beside a credential-like code
    // the button read as "no idea where my code would end up".
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await renderStale()
    const u = userEvent.setup()
    await u.click(await screen.findByTestId('signin-code-copy'))
    const notice = await screen.findByTestId('signin-copy-error')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('shares the verb between the row primary and the replace link', async () => {
    // Both call `onRestart`. "Start sign-in" on the row and "Get a new sign-in
    // code" in the hint read as two different operations; a reader recovering a
    // crew could not tell whether they did the same thing. Same verb, and the
    // link names only what differs: the code shown is replaced.
    await renderStale()
    const replace = await screen.findByRole('button', { name: /Start over with a new code/i })
    expect(replace).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Get a new/i })).not.toBeInTheDocument()
  })

  it('keeps the replacement in the hint sentence, not as a fourth peer action', async () => {
    // Four sibling actions with no ranking is what the reader was choosing
    // between: the code chip, the code's page link, the recheck primary, and a
    // second "Start over with a new code" button beside it.
    await renderStale()
    const hint = await screen.findByTestId('signin-recovery-hint')
    const replace = screen.getByTestId('signin-get-new-code')
    expect(hint).toContainElement(replace)
    // Still a real button with a real name, so it keeps its keyboard reach.
    expect(replace).toHaveAccessibleName(/Start over with a new code/i)

    // Beside the code chip and the code's own anchor, the row holds exactly one
    // action: the recheck primary.
    const prompt = screen.getByTestId('signin-prompt')
    const rowActions = Array.from(prompt.querySelectorAll('button, a'))
      .filter(node => !hint.contains(node))
      .filter(node => node.getAttribute('data-testid') !== 'signin-code-copy')
      .filter(node => !/Open sign-in page/i.test(node.textContent ?? ''))
    expect(rowActions).toHaveLength(1)
    expect(rowActions[0]).toHaveAccessibleName(/I approved it/i)
  })

  it('still restarts the sign-in from the hint link', async () => {
    // Demoting the action must not disconnect it: same `onRestart` handler.
    const u = await renderStale()
    await u.click(await screen.findByTestId('signin-get-new-code'))
    await waitFor(() => expect(api.cloudLaunchSigninRestart).toHaveBeenCalledWith(STALE.id))
  })

  it('still offers only the fetch when a job is awaiting with no code', async () => {
    // The other state this button serves must keep its own label.
    const awaitingNoCode = { ...STALE, status: 'awaiting_signin' as const, signin: null }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingNoCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingNoCode as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByRole('button', { name: /Show the sign-in code/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /I approved it/i })).not.toBeInTheDocument()
  })
})

describe('a restart on a crew that is already signed in', () => {
  it('reports nothing and refreshes, since the sign-in is what the reader wanted', async () => {
    // The route answers 409 `signin_already_complete` when another tab (or the
    // re-probe) confirmed the sign-in first. Painting that red told the reader
    // their crew had failed to sign in at the moment it succeeded.
    useJob(UNSIGNED_JOB)
    vi.mocked(api.cloudLaunchSigninRestart).mockRejectedValue(
      new ApiError(409, 'this crew is already signed in', JSON.stringify({ code: 'signin_already_complete' })),
    )
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /Start sign-in/i }))
    await waitFor(() => expect(api.cloudLaunchSigninRestart).toHaveBeenCalled())
    expect(screen.queryByText(/already signed in/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Unknown error/i)).not.toBeInTheDocument()
    // The list is re-read, so the badge and Connect catch up on their own.
    await waitFor(() => expect(api.cloudLaunches).toHaveBeenCalled())
  })

  it('still reports a restart that genuinely failed', async () => {
    useJob(UNSIGNED_JOB)
    vi.mocked(api.cloudLaunchSigninRestart).mockRejectedValue(new Error('gateway unreachable'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /Start sign-in/i }))
    expect(await screen.findByText(/gateway unreachable/i)).toBeInTheDocument()
  })
})

describe('the cancel that removes an instance asks first', () => {
  const LAUNCHING = {
    ...UNSIGNED_JOB,
    status: 'running' as const,
    instance_id: '',
    signin: null,
    steps: [
      { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
      { key: 'provision', label: 'Create the instance', state: 'active' as const },
      { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
      { key: 'connect', label: 'Connect', state: 'pending' as const },
    ],
  }

  it('says it will ask, with the ellipsis the panel uses for a confirm', async () => {
    // The reader could not tell from the label whether the click deletes at once
    // or asks first, and hesitated on the only exit from a billed launch. The
    // panel's own destructive controls end in an ellipsis (`Remove…`).
    useJob(LAUNCHING)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const first = (await screen.findAllByRole('button', { name: /remove the crew/i }))[0]
    expect(first).toHaveTextContent(/Cancel and remove the crew…$/)
  })

  it('arms, warns, and only then removes', async () => {
    // One unguarded click destroyed the crew being created, while the crew
    // row's Delete asks first -- so the reader could not tell whether this one
    // would, and "would not dare" press it.
    useJob(LAUNCHING)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const first = (await screen.findAllByRole('button', { name: /remove the crew/i }))[0]
    await u.click(first)
    expect(api.cloudLaunchCancel).not.toHaveBeenCalled()
    const warning = await screen.findByTestId('cancel-remove-warning')
    expect(warning).toHaveTextContent(/cannot be undone/i)

    // The operand is quoted per locale (destructiveConfirm pins it), so match the
    // verb and assert the instance name separately rather than pinning a glyph.
    const confirm = screen.getByRole('button', { name: /Yes, remove/i })
    expect(confirm).toHaveTextContent(/kc-5e10bb/)
    await u.click(confirm)
    await waitFor(() => expect(api.cloudLaunchCancel).toHaveBeenCalled())
  })

  it('lets the reader back out of the armed state', async () => {
    useJob(LAUNCHING)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click((await screen.findAllByRole('button', { name: /remove the crew/i }))[0])
    await u.click(await screen.findByRole('button', { name: /Keep setting up/i }))
    expect(screen.queryByTestId('cancel-remove-warning')).not.toBeInTheDocument()
    expect(api.cloudLaunchCancel).not.toHaveBeenCalled()
  })

  it('does not make stopping a sign-in ask twice', async () => {
    // That click keeps the crew and the crew row: a confirm there is ceremony,
    // and ceremony everywhere is what makes a real warning invisible.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /keep the crew/i }))
    await waitFor(() => expect(api.cloudLaunchCancel).toHaveBeenCalled())
    expect(screen.queryByTestId('cancel-remove-warning')).not.toBeInTheDocument()
  })
})

describe('a gateway failure in words the reader can act on', () => {
  const STALE_CODE = {
    ...UNSIGNED_JOB,
    signin: { url: 'https://amzn.awsapps.com/start/#/device?user_code=OLD-CODE', code: 'OLD-CODE', ports: [] },
  }

  const failWith = async (err: Error) => {
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [STALE_CODE] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(STALE_CODE as never)
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(err)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    await u.click(await screen.findByRole('button', { name: /I approved it/i }))
    return screen.findByTestId('signin-fetch-error')
  }

  it('turns a transport failure into a sentence', async () => {
    const notice = await failWith(new Error('TypeError: Failed to fetch'))
    // A whole sentence of its own, not a fragment dropped into another string.
    expect(notice).toHaveTextContent(/Couldn.t reach the gateway\. Check that it is running/i)
    expect(notice).not.toHaveTextContent(/Failed to fetch/i)
  })

  it('turns a gateway timeout into a sentence', async () => {
    const notice = await failWith(new Error('504 Gateway Timeout'))
    expect(notice).toHaveTextContent(/didn.t answer in time/i)
  })

  it('offers no agent hand-off when the gateway itself did not answer', async () => {
    // The agent chat is served by that same gateway, so the button would open a
    // chat that cannot load -- from the screen that just said the gateway is down.
    const notice = await failWith(new Error('TypeError: Failed to fetch'))
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('keeps the hand-off for a failure the agent can look into', async () => {
    const notice = await failWith(new Error('disk quota exceeded on the gateway'))
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('passes an unrecognised failure through rather than guessing', async () => {
    const notice = await failWith(new Error('disk quota exceeded on the gateway'))
    expect(notice).toHaveTextContent(/disk quota exceeded/i)
  })
})

describe('the two cancels do not look alike', () => {
  it('colours only the cancel that removes the instance', async () => {
    // Same plain button in the same slot for both, so reading the label was the
    // only way to tell them apart -- every time, before a click that may delete
    // the machine. The destructive one is the danger button.
    const launching = {
      ...UNSIGNED_JOB,
      status: 'running' as const,
      instance_id: '',
      signin: null,
      steps: [
        { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
        { key: 'provision', label: 'Create the instance', state: 'active' as const },
        { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
        { key: 'connect', label: 'Connect', state: 'pending' as const },
      ],
    }
    useJob(launching)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const removes = (await screen.findAllByRole('button', { name: /remove the crew/i }))[0]
    expect(removes.className).toMatch(/danger/)
  })

  it('leaves the sign-in cancel uncoloured, since the crew survives it', async () => {
    // Colouring both would make the danger tone meaningless: this click keeps the
    // instance and the crew row.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const keeps = await screen.findByRole('button', { name: /keep the crew/i })
    expect(keeps.className).not.toMatch(/danger/)
  })
})

describe('one in-progress job, one name', () => {
  it('gives the row pill and the card badge the same string', async () => {
    // The row said "Setting up" and the card said "Launching…" for the same
    // step-2 job, so the reader had two states to reconcile and no way to tell
    // whether they were the same one.
    const launching = {
      ...UNSIGNED_JOB,
      status: 'running' as const,
      signin: null,
      steps: [
        { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
        { key: 'provision', label: 'Create the instance', state: 'active' as const },
        { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
        { key: 'connect', label: 'Connect', state: 'pending' as const },
      ],
    }
    useJob(launching)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    // The card badge now reads the row pill's own string, and no surface says the
    // other name. (Both rendered together is asserted on the real SPA, where the
    // instances list has rows: harness frame 06 requires two matches.)
    expect(await screen.findByText('Setting up')).toBeInTheDocument()
    expect(screen.queryByText(/Launching/)).not.toBeInTheDocument()
  })
})

describe('the progress-card badge while the reader owes an approval', () => {
  it('says it is waiting for the sign-in, not that it is launching', async () => {
    // "Launching…" over a card whose whole body asks the reader to approve a code
    // says the machine is busy and there is nothing to do — so the reader waited
    // for a launch that was in fact waiting for them.
    useJob(withCode('LIVE', { status: 'awaiting_signin' as const }))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const badge = await screen.findByText('Waiting for your approval')
    // Blocked-on-you, in the same warn tone as the terminal unsigned badge, so
    // the two read as one condition seen at two moments.
    expect(badge.className).toMatch(/text-warn/)
    expect(screen.queryByText('Launching…')).not.toBeInTheDocument()
    expect(screen.queryByText('Setting up')).not.toBeInTheDocument()
  })

  it('keeps the in-progress badge for a launch that is genuinely still working', async () => {
    // The badge must not become a blanket warning: a running job with no code
    // owed is progress, and warning there would cry wolf.
    useJob({
      ...UNSIGNED_JOB,
      status: 'running' as const,
      signin: null,
      steps: [
        { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
        { key: 'provision', label: 'Create the instance', state: 'active' as const },
        { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
        { key: 'connect', label: 'Connect', state: 'pending' as const },
      ],
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    // "Setting up", the same string the row's pill uses: one job in progress must
    // not wear two names. The row and the card both render it here, so count.
    expect((await screen.findAllByText('Setting up')).length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByText('Launching…')).not.toBeInTheDocument()
    expect(screen.queryByText('Waiting for your approval')).not.toBeInTheDocument()
  })
})

describe('the step details these fixtures put on screen', () => {
  // The card renders `step.detail` verbatim, so a fixture that invents one tests
  // — and screenshots — a screen that does not ship. Both fixtures drifted once:
  // they still said "Added to your crews." after the backend had moved to
  // "Added to Your crews.". Pin them to their source instead.
  const read = (rel: string): string =>
    readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
  const details = STEPS_REGISTERED.flatMap(step =>
    'detail' in step && step.detail ? [step.detail] : [],
  )

  it('has details to check at all, so neither assertion below can pass vacuously', () => {
    expect(details.length).toBeGreaterThanOrEqual(2)
  })

  it('are copy launch_job.py actually writes', () => {
    const py = read('../../../src/kiro_crew/cloud/launch_job.py')
    for (const detail of details) {
      expect(py, `launch_job.py never writes ${JSON.stringify(detail)}`).toContain(detail)
    }
  })

  it('are the same strings the screenshot harness captures', () => {
    const harness = read('../../scripts/capture-remote-crew-signin-recovery.mjs')
    for (const detail of details) {
      expect(harness, `the harness fixture is missing ${JSON.stringify(detail)}`).toContain(detail)
    }
  })
})

describe('round 9 — the recheck tells the truth about success', () => {
  const STALE = {
    ...UNSIGNED_JOB,
    login_target: { license: 'pro', start_url: 'https://amzn.awsapps.com/start', region: 'us-east-1' },
    signin: { url: 'https://amzn.awsapps.com/start/#/device?user_code=OLD', code: 'OLD' },
  }

  it('does not paint a red error when the recheck finds the sign-in complete', async () => {
    // The gateway answers the recheck with HTTP 409 `signin_already_complete`
    // when the box IS signed in. That is the click's success, not a failure.
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(
      new ApiError(409, 'already signed in', JSON.stringify({ code: 'signin_already_complete' })),
    )
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [STALE] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(STALE as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /I approved it/i }))
    await waitFor(() => expect(api.cloudLaunchSignin).toHaveBeenCalledWith(STALE.id))
    // Give any error surface a tick to appear; it must not.
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByText(/already signed in/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/unknown error/i)).not.toBeInTheDocument()
  })

  it('answers a recheck that finds no approval yet with "not signed in yet", beside the button', async () => {
    // 409 `no_signin_pending` on a preserved code is the gateway saying the box
    // was re-probed and the approval has NOT landed. Painted as a red banner it
    // told a user who clicked a moment early that something broke; re-rendering
    // the identical screen told them nothing had run. Neither is an answer.
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(
      new ApiError(409, 'no sign-in pending', JSON.stringify({ code: 'no_signin_pending' })),
    )
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [STALE] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(STALE as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /I approved it/i }))
    const result = await screen.findByTestId('signin-recheck-result')
    expect(result).toHaveTextContent(/Not signed in yet/i)
    expect(result).toHaveAttribute('role', 'status')
    // Inside the prompt block, in the row with the button that produced it.
    expect(screen.getByTestId('signin-prompt')).toContainElement(result)
    expect(screen.getByTestId('signin-action-row')).toContainElement(result)
    expect(screen.queryByText(/no sign-in pending/i)).not.toBeInTheDocument()
    // The code stays: it may still be approved.
    expect(screen.getByTestId('signin-prompt')).toHaveTextContent(/Your code: OLD/)
  })

  it('shows an inline error and keeps the code when the sign-in probe is indeterminate', async () => {
    const body = {
      error: 'could not reach the crew to check its sign-in',
      code: 'signin_probe_failed',
    }
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(
      new ApiError(502, body.error, JSON.stringify(body)),
    )
    useJob(STALE)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /I approved it/i }))
    expect(api.cloudLaunchSignin).toHaveBeenCalledWith(STALE.id)
    const notice = await screen.findByTestId('signin-fetch-error')
    expect(notice).toHaveTextContent(/could not reach the crew to check its sign-in/i)
    expect(screen.getByTestId('signin-action-row')).toContainElement(notice)
    expect(screen.queryByTestId('signin-recheck-result')).not.toBeInTheDocument()
    expect(screen.getByTestId('signin-prompt')).toHaveTextContent(/Your code: OLD/)
  })

  it('reports a failed recheck next to the button, not only in the panel banner', async () => {
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new Error('gateway unreachable'))
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [STALE] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(STALE as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await u.click(await screen.findByRole('button', { name: /I approved it/i }))
    const notice = await screen.findByTestId('signin-fetch-error')
    // The transport's words are replaced by a sentence of their own.
    expect(notice).toHaveTextContent(/Couldn.t reach the gateway/i)
    expect(notice).not.toHaveTextContent(/gateway unreachable/i)
    expect(screen.getByTestId('signin-action-row')).toContainElement(notice)
    expect(screen.queryByTestId('signin-recheck-result')).not.toBeInTheDocument()
  })

  it('reports a failed code fetch next to the fetch button', async () => {
    const awaitingNoCode = { ...UNSIGNED_JOB, status: 'awaiting_signin' as const, signin: null }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingNoCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingNoCode as never)
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new Error('network'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    // The auto-fetch on mount already failed: the notice is beside the button.
    const notice = await screen.findByTestId('signin-fetch-error')
    expect(notice).toHaveTextContent(/Couldn.t get the sign-in code \(network\)/i)
    const prompt = screen.getByTestId('signin-prompt')
    expect(prompt).toContainElement(notice)
    expect(prompt).toContainElement(screen.getByRole('button', { name: /Show the sign-in code/i }))
  })

  it('shows the waiting badge on a running sign-in restart, not Launching', async () => {
    // A restart is a running job whose connect step is already done: it is
    // signing in, not launching, and the create step above it is ticked.
    const restarting = {
      ...UNSIGNED_JOB,
      status: 'running' as const,
      steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'active' as const, detail: '' } : s)),
      signin: null,
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [restarting] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(restarting as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await screen.findByText(/Kiro Crew Cloud \(kc-5e10bb\)/i)
    // No code yet: the badge must not say "your approval" before there is
    // anything to approve. It says the code is being fetched.
    // The prompt block shows the same words beside its spinner, so assert the
    // badge specifically: there are at least two matches and both are correct.
    // Once, not twice: the badge carries the fact and the card's inline line now
    // says what the badge cannot (how long to wait).
    expect(screen.getAllByText(/Getting your sign-in code/i)).toHaveLength(1)
    expect(screen.getByText(/takes a few seconds/i)).toBeInTheDocument()
    expect(screen.queryByText(/^Launching/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Waiting for your approval/i)).not.toBeInTheDocument()
  })

  it('says Waiting for your approval only once a code is on screen', async () => {
    const awaitingWithCode = {
      ...UNSIGNED_JOB,
      status: 'awaiting_signin' as const,
      steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'active' as const, detail: '' } : s)),
      signin: { url: 'https://x/?user_code=LIVE', code: 'LIVE' },
    }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingWithCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingWithCode as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await screen.findByText(/Kiro Crew Cloud \(kc-5e10bb\)/i)
    expect(screen.getByText(/Waiting for your approval/i)).toBeInTheDocument()
  })
})

describe('round 15 — one row at a time, and the prompt fetched for you', () => {
  it('fetches the pending prompt on mount for a job already awaiting sign-in', async () => {
    // The gateway holds the prompt; making the reader click to reveal it left
    // them unable to tell what decided shown vs hidden.
    const awaitingNoCode = { ...UNSIGNED_JOB, status: 'awaiting_signin' as const, signin: null }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingNoCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingNoCode as never)
    vi.mocked(api.cloudLaunchSignin).mockResolvedValue({
      signin: { url: 'https://x/?user_code=AUTO', code: 'AUTO', ports: [] },
    } as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await waitFor(() => expect(api.cloudLaunchSignin).toHaveBeenCalledWith(UNSIGNED_JOB.id))
    // Once only: a re-render must not re-fire it.
    await new Promise(r => setTimeout(r, 60))
    expect(vi.mocked(api.cloudLaunchSignin).mock.calls.length).toBe(1)
  })

  it('disables the sign-in button only on the row that was clicked', async () => {
    // The mutation object is shared by every row; its bare `isPending` made a
    // click on one instance grey out the button on every other unsigned one.
    const jobA = { ...UNSIGNED_JOB, id: 'job-a', tag: 'kc-aaaaaa', instance_id: 'i-aaa' }
    const jobB = { ...UNSIGNED_JOB, id: 'job-b', tag: 'kc-bbbbbb', instance_id: 'i-bbb' }
    const instA = { ...CLOUD_INSTANCE, id: 'inst-a', name: 'Kiro Crew Cloud (kc-aaaaaa)', ssm_target: 'i-aaa', status: { instance_id: 'inst-a', state: 'disconnected' as const } }
    const instB = { ...CLOUD_INSTANCE, id: 'inst-b', name: 'Kiro Crew Cloud (kc-bbbbbb)', ssm_target: 'i-bbb', status: { instance_id: 'inst-b', state: 'disconnected' as const } }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [jobA, jobB] } as never)
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [instA, instB] })
    // Never resolves: keeps the mutation pending while we look at the other row.
    vi.mocked(api.cloudLaunchSigninRestart).mockReturnValue(new Promise(() => {}) as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    // Select rows by their stable id, not by text: the tag appears in several
    // places on a row (name, aria labels), so a text query is ambiguous.
    await screen.findAllByRole('button', { name: /Start sign-in/i })
    const rowA = document.querySelector('[data-crew-id="inst-a"]') as HTMLElement
    const rowB = document.querySelector('[data-crew-id="inst-b"]') as HTMLElement
    expect(rowA).toBeTruthy(); expect(rowB).toBeTruthy()
    const startA = within(rowA).getByRole('button', { name: /Start sign-in/i })
    const startB = within(rowB).getByRole('button', { name: /Start sign-in/i })
    await u.click(startA)
    await waitFor(() => expect(startA).toBeDisabled())
    expect(startB).not.toBeDisabled()
  })
})

describe('round 25 — the badge and the button agree', () => {
  it('does not say "Getting your sign-in code" while the fetch button sits idle', async () => {
    // A badge that says wait beside a primary that says act is two instructions.
    // The preparing badge is for an in-flight fetch or restart only; an idle
    // no-code prompt shows the waiting badge, and the auto-fetch on mount makes
    // that idle window brief anyway. Here the fetch is made to fail so the
    // prompt stays idle without a code.
    const awaitingNoCode = { ...UNSIGNED_JOB, status: 'awaiting_signin' as const, signin: null }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingNoCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingNoCode as never)
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new Error('network'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    await screen.findByRole('button', { name: /Show the sign-in code/i })
    await waitFor(() => expect(api.cloudLaunchSignin).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText(/Getting your sign-in code/i)).not.toBeInTheDocument())
    expect(screen.getByText(/Waiting for your approval/i)).toBeInTheDocument()
  })

  it('titles an awaiting no-code prompt as a code that is ready, not one to get', async () => {
    // The gateway already holds a code here and the button SHOWS it; "Get a
    // sign-in code" over a button that says it starts nothing was a
    // contradiction. The title says the code exists; "Get" is reserved for the
    // terminal no-code state, where nothing does (the row test above).
    const awaitingNoCode = { ...UNSIGNED_JOB, status: 'awaiting_signin' as const, signin: null }
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [awaitingNoCode] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(awaitingNoCode as never)
    vi.mocked(api.cloudLaunchSignin).mockRejectedValue(new Error('network'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    const prompt = await screen.findByTestId('signin-prompt')
    expect(prompt).toHaveTextContent(/sign-in code .*is ready/i)
    expect(prompt).not.toHaveTextContent(/Get a sign-in code/i)
    // The subtitle may still tell the reader to approve a code in the browser;
    // the TITLE must not claim they have been shown one. Match the title forms only.
    expect(prompt).not.toHaveTextContent(/Approve (the sign-in code|with your company)/i)
  })
})

describe('the preparing state says one thing once', () => {
  const STARTING = {
    ...UNSIGNED_JOB,
    status: 'running' as const,
    signin: null,
    steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'active' as const, detail: '' } : s)),
  }

  it('does not repeat the badge in the card body', async () => {
    // Badge and inline line carried the identical string, which the reader read
    // as the same fact twice. The card keeps the badge and the line says what the
    // badge cannot: how long to wait.
    useJob(STARTING)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)

    expect(await screen.findByText(/Getting your sign-in code/i)).toBeInTheDocument()
    expect(screen.getAllByText(/Getting your sign-in code/i)).toHaveLength(1)
    expect(screen.getByText(/takes a few seconds/i)).toBeInTheDocument()
  })
})

describe('the code chip says what it does', () => {
  const STALE_CODE = {
    ...UNSIGNED_JOB,
    signin: { url: 'https://amzn.awsapps.com/start/#/device?user_code=OLD-CODE', code: 'OLD-CODE', ports: [] },
  }
  const renderChip = async () => {
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [STALE_CODE] } as never)
    vi.mocked(api.cloudLaunchStatus).mockResolvedValue(STALE_CODE as never)
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await openSetup(u)
    return u
  }

  it('shows the word Copy beside the icon, not the icon alone', async () => {
    // Every recovery goes through this chip, and the blind reader could only
    // guess from the glyph that clicking copies.
    await renderChip()
    const chip = await screen.findByTestId('signin-code-copy')
    expect(chip).toHaveTextContent(/Copy/i)
  })

  it('says Copied in the chip once the copy lands', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    const u = await renderChip()
    const chip = await screen.findByTestId('signin-code-copy')
    await u.click(chip)
    await waitFor(() => expect(chip).toHaveTextContent(/Copied/i))
  })

  it('keeps a failed copy out of the code row, so the page link cannot shift', async () => {
    // Rendered between the chip and the anchor, the notice pushed the link
    // sideways at the moment the reader was going for it.
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    const u = await renderChip()
    await u.click(await screen.findByTestId('signin-code-copy'))
    const notice = await screen.findByTestId('signin-copy-error')
    const row = screen.getByTestId('signin-code-row')
    expect(row).not.toContainElement(notice)
    expect(screen.getByTestId('signin-prompt')).toContainElement(notice)
    // The link is still in the row, after the chip.
    const inRow = within(row).getByRole('link', { name: /Open sign-in page/i })
    expect(inRow).toBeInTheDocument()
  })
})
