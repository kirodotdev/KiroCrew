import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { KiroPrerequisiteStatus } from '../api/client'
import KiroPrerequisiteGate, {
  asSentence,
  kiroPrerequisiteIsBlocking,
  kiroPrerequisiteRefetchInterval,
} from '../components/KiroPrerequisiteGate'
import { renderWithProviders } from './helpers'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(undefined),
  copyCode: vi.fn(),
}))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    body: string

    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  },
  api: {
    kiroPrerequisite: vi.fn(),
    repairKiroPrerequisiteSpecs: vi.fn(),
  },
}))

import { api, ApiError } from '../api/client'

function status(overrides: Partial<KiroPrerequisiteStatus> = {}): KiroPrerequisiteStatus {
  return {
    platform: 'Linux',
    installed: false,
    authenticated: false,
    ready: false,
    initial_setup_complete: false,
    repair_required: false,
    docs_url: 'https://kiro.dev/cli/',
    login_command: 'kiro-cli login',
    sso_login_command: 'kiro-cli login --use-device-flow --license pro',
    setup_allowed: true,
    sandbox_unavailable: false,
    sandbox_failure_kind: '',
    sandbox_detail: '',
    sandbox_remedy: '',
    missing_agent_specs: [],
    agent_spec_repair_error: '',
    ...overrides,
  }
}

describe('KiroPrerequisiteGate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // The gate remembers first-run completion in localStorage, so each case
    // must start from a clean slate or a prior test's completion would leak in
    // and silently bypass the setup assertions.
    localStorage.clear()
  })

  it('keeps a slow readiness poll after setup so later sign-out is detected', () => {
    expect(kiroPrerequisiteRefetchInterval(status({ ready: true }))).toBe(30_000)
    expect(kiroPrerequisiteRefetchInterval(status({ initial_setup_complete: true }))).toBe(30_000)
  })

  it('polls the host faster while the first-run gate blocks the dashboard', () => {
    // The gate is what the user stares at while they install Kiro CLI from
    // kiro.dev and sign in. Neither step touches the gateway, so the gate has to
    // keep asking or it can never lift on its own.
    expect(kiroPrerequisiteIsBlocking(status())).toBe(true)
    expect(kiroPrerequisiteRefetchInterval(status())).toBe(5_000)

    // Not blocking: ready, a returning user, and a non-owner each have their own
    // screen and must not drive a host probe every 5s.
    expect(kiroPrerequisiteIsBlocking(status({ ready: true }))).toBe(false)
    expect(kiroPrerequisiteIsBlocking(status({ initial_setup_complete: true }))).toBe(false)
    expect(kiroPrerequisiteIsBlocking(status({ setup_allowed: false }))).toBe(false)
    expect(kiroPrerequisiteIsBlocking(undefined)).toBe(false)
  })

  it('forces a real host probe on the blocking gate, not a latched read', async () => {
    // The status endpoint serves the boot-time LATCH unless refresh is set, so a
    // poll that omits it can never observe a CLI the user just installed. Driven
    // by an explicit refetch rather than by waiting out the interval, so the
    // assertion is about the force decision and not about elapsed time.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())

    const rendered = renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText(/Kiro Crew uses Kiro CLI/)
    // Cold mount has no cached status, so it reads the latch.
    expect(vi.mocked(api.kiroPrerequisite).mock.calls[0][0]).toBe(false)

    await rendered.queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] })

    // Every later fetch sees a cached status that reports the gate as blocking, so
    // it probes the host — as 'auto', the coalesced mode, NOT the human 'explicit'.
    await waitFor(() => expect(
      vi.mocked(api.kiroPrerequisite).mock.calls.some(([refresh]) => refresh === 'auto'),
    ).toBe(true))
    expect(vi.mocked(api.kiroPrerequisite).mock.calls.some(([r]) => r === 'explicit'))
      .toBe(false)
  })

  it('sends the human Check again as the uncoalesced explicit mode', async () => {
    // A button that can be answered from a cache looks broken, so the click must
    // be distinguishable from the automatic poll at the API boundary.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: /Check again/ }))

    await waitFor(() => expect(
      vi.mocked(api.kiroPrerequisite).mock.calls.some(([refresh]) => refresh === 'explicit'),
    ).toBe(true))
  })

  it('lifts the gate as soon as detection reports a signed-in CLI', async () => {
    // "Guard until sign-in succeeds" in one assertion: the same mounted gate goes
    // from blocking to rendering the app on a later poll, with no reload.
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({
        installed: true,
        authenticated: true,
        ready: true,
        initial_setup_complete: true,
      }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/Kiro Crew uses Kiro CLI/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('renders the application immediately when Kiro is ready', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate>
        <div>Dashboard loaded</div>
      </KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
  })

  it('sends the user to Kiro CLI setup instead of installing anything', async () => {
    // Kiro Crew does not install Kiro CLI. A missing CLI must offer a link to
    // Kiro's own setup page and NO install action of any kind, so there is
    // nothing for the user to press that would download and run a script.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Windows' }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/Kiro Crew uses Kiro CLI/)).toBeInTheDocument()
    expect((await screen.findAllByText(/Windows gateway host/)).length).toBeGreaterThan(0)

    const setupLink = screen.getByRole('link', { name: /Open Kiro CLI setup/ })
    expect(setupLink).toHaveAttribute('href', 'https://kiro.dev/cli/')
    expect(setupLink).toHaveAttribute('target', '_blank')
    expect(setupLink).toHaveAttribute('rel', expect.stringContaining('noopener'))
    expect(screen.queryByRole('button', { name: /Install Kiro CLI/ })).not.toBeInTheDocument()
    // No sign-in action exists at all — the user signs in with Kiro CLI.
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
  })

  it('tells an installed-but-signed-out CLI to sign in via Kiro CLI', async () => {
    // Any Kiro CLI that runs is usable regardless of install source, so this
    // state must show the sign-in instruction and the exact command — and no
    // action that would sign the user in on their behalf.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/Sign in with Kiro CLI on the gateway host/))
      .toBeInTheDocument()
    // The command is rendered verbatim so it can be copied and typed.
    expect(screen.getByText('kiro-cli login').tagName).toBe('CODE')
    // Both tiers are offered, each under a label naming which account it signs
    // into. The sign-in page presents a free Builder ID as a peer of
    // organization SSO, so a gate that showed only the bare command would let an
    // SSO user authenticate into the wrong tier and discover it later as
    // missing models.
    expect(screen.getByText('kiro-cli login --use-device-flow --license pro').tagName)
      .toBe('CODE')
    expect(screen.getByText(/Personal account/)).toBeInTheDocument()
    expect(screen.getByText(/Organization SSO/)).toBeInTheDocument()
    // The only action is a re-check.
    expect(screen.getByRole('button', { name: /Check again/ })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(screen.queryByText(/unverified executable/)).not.toBeInTheDocument()
  })

  it('exposes no way to start a sign-in from the dashboard', async () => {
    // Kiro Crew does not authenticate for the user: there is no device-flow
    // trigger, no sign-in URL, and no device code surfaced anywhere.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText(/Sign in with Kiro CLI on the gateway host/)
    expect(api).not.toHaveProperty('loginKiroPrerequisite')
    expect(screen.queryByRole('link', { name: /Open Kiro sign-in page/ })).not.toBeInTheDocument()
    // Only the two step cards' own affordances: the setup link is gone once the
    // CLI is found, leaving exactly one control — Check again.
    const buttons = screen.getAllByRole('button').map(b => b.textContent || '')
    expect(buttons.filter(t => /Check again/.test(t))).toHaveLength(1)
  })

  it('shows non-owners a redacted owner-setup state', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/gateway owner needs to finish setup/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Open Kiro CLI setup/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('lets a non-owner observe owner completion without reloading', async () => {
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(status({
        platform: 'gateway',
        setup_allowed: false,
      }))
      .mockResolvedValueOnce(status({
        platform: 'gateway',
        installed: true,
        authenticated: true,
        ready: true,
        initial_setup_complete: true,
        setup_allowed: false,
      }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('keeps cached readiness mounted after a transient refetch failure', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))
    const rendered = renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()

    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))
    await rendered.queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] })

    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('We could not check Kiro CLI.')).not.toBeInTheDocument()
  })

  it('blocks an ESTABLISHED install when the agent specs are missing', async () => {
    // The one condition that hijacks an established install, and deliberately
    // so: `initial_setup_complete` normally short-circuits to the app because
    // readiness is a latch that can be stale. A missing spec is not a latch —
    // it is two stat calls made while answering this request — and it means
    // kiro-cli fails EVERY session/set_mode, so without this screen the install
    // has no affordance anywhere to repair itself.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: false,
      initial_setup_complete: true,
      repair_required: true,
      missing_agent_specs: ['kirocrew.json', 'kirocrew-lite.json'],
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText("Kiro Crew's agent specs are not installed")).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    // Names the actual files, so the user can see what to look for on disk.
    expect(screen.getByText(/kirocrew\.json/)).toBeInTheDocument()
    expect(screen.getByText(/kirocrew-lite\.json/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('points a terminal diagnoser past the app-not-running dead end', async () => {
    // `kiro-cli diagnostic` is the first command anyone reaches for and it
    // refuses with "Kiro CLI app is not running" until the app is launched,
    // which reads as the cause and is not.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const hint = await screen.findByText(/kiro-cli diagnostic reports nothing/)
    expect(hint).toHaveTextContent('kiro-cli launch')
    expect(hint).toHaveTextContent('not the cause')
  })

  it('surfaces a failed repair verbatim instead of a generic failure', async () => {
    // The swallowed boot exception is what made the original report
    // undiagnosable; this text names the failing install step.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
      agent_spec_repair_error: 'FileNotFoundError: no shipped defaults.json',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('The repair attempt failed')).toBeInTheDocument()
    expect(
      screen.getByText('FileNotFoundError: no shipped defaults.json'),
    ).toBeInTheDocument()
  })

  it('repairs via POST, never by re-reading the status GET', async () => {
    // The gateway's CSRF check and SEL audit are both method-scoped, so the
    // write cannot hang off the status GET: a SameSite=Lax cookie rides a
    // top-level cross-site GET, and a GET leaves no audit record.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
    }))
    vi.mocked(api.repairKiroPrerequisiteSpecs).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
      initial_setup_complete: true,
      missing_agent_specs: [],
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const repair = await screen.findByRole('button', { name: 'Check again' })
    // Every status read stays a free, side-effect-free poll.
    expect(vi.mocked(api.kiroPrerequisite)).toHaveBeenCalledWith(false)
    expect(vi.mocked(api.kiroPrerequisite)).not.toHaveBeenCalledWith(true)
    fireEvent.click(repair)

    await waitFor(() => {
      expect(vi.mocked(api.repairKiroPrerequisiteSpecs)).toHaveBeenCalledTimes(1)
    })
    // The POST response IS the post-repair snapshot, so the app unblocks without
    // waiting for the next poll.
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('shows a failed repair returned by the POST', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
    }))
    vi.mocked(api.repairKiroPrerequisiteSpecs).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
      agent_spec_repair_error: 'FileNotFoundError: no shipped defaults.json',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))

    // role="alert" so a screen reader hears it: this appears in place, with no
    // route change to announce.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The repair attempt failed')
    expect(alert).toHaveTextContent('FileNotFoundError: no shipped defaults.json')
  })

  it('surfaces a rejected repair POST rather than failing silently', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
    }))
    vi.mocked(api.repairKiroPrerequisiteSpecs).mockRejectedValue(
      new ApiError(403, 'dashboard owner required'),
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('dashboard owner required')
  })

  it('leaves a healthy install untouched by the spec check', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
      initial_setup_complete: true,
      missing_agent_specs: [],
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Agent specs missing')).not.toBeInTheDocument()
  })

  it('tolerates a gateway older than the agent-spec fields', async () => {
    // A partial payload (older gateway, or a fixture built before the field)
    // must not crash the gate on `.length` of undefined.
    const legacy = status({ installed: true, authenticated: true, initial_setup_complete: true })
    delete (legacy as { missing_agent_specs?: unknown }).missing_agent_specs
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(legacy)

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('shows NO sign-in chrome when an established install is signed out', async () => {
    // The dashboard does not guide the user to sign in. A signed-out CLI is
    // reported by the turn itself (an actionable `kiro-cli login` error card in
    // the transcript), so a persistent banner would nag every surface for a
    // state the dashboard cannot even keep current.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      ready: false,
      initial_setup_complete: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Kiro Crew needs Kiro sign-in.')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(screen.queryByText('kiro-cli login')).not.toBeInTheDocument()
    // Nothing is paused: no gate chrome of any kind renders over the app.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
  })

  it('leaves an established non-owner dashboard completely unblocked', async () => {
    // `initial_setup_complete` short-circuits before the non-owner branch: a
    // signed-out established install shows no chrome to ANY user. The
    // owner-restore screen is reserved for a genuine first run (below).
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      initial_setup_complete: true,
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('The gateway owner needs to finish setup.'))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
  })

  it('still shows the owner-restore screen to a non-owner on a genuine first run', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      initial_setup_complete: false,
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('The gateway owner needs to finish setup.'))
      .toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('fails open when connected to a gateway without the new endpoint', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(404, 'HTTP 404'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('mounts the dashboard immediately while the first check is pending', async () => {
    // The pending state must not render the full-screen SETUP shell ("Your
    // crew is almost ready.") for the whole first round trip — that round trip
    // is slow because the gateway probe shells out to kiro-cli twice, so a
    // returning user would see the first-run setup screen flash and vanish.
    //
    // Kiro readiness gates nothing in the dashboard, so an unresolved check must
    // not withhold OR degrade the app: mount it fully usable and let only a
    // confirmed first-run status show setup.
    let resolveStatus: (value: KiroPrerequisiteStatus) => void = () => {}
    vi.mocked(api.kiroPrerequisite).mockReturnValue(
      new Promise<KiroPrerequisiteStatus>(resolve => { resolveStatus = resolve }),
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    // No waiting screen and no setup chrome — the app itself is already up.
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Your crew is almost ready.')).not.toBeInTheDocument()
    expect(screen.queryByText('One quick setup')).not.toBeInTheDocument()

    resolveStatus(status({ installed: true, authenticated: true, ready: true }))
    await waitFor(() => expect(screen.getByText('Dashboard loaded')).toBeInTheDocument())
  })

  it('adds NO chrome when a pending check resolves to signed-out', async () => {
    let resolveStatus: (value: KiroPrerequisiteStatus) => void = () => {}
    vi.mocked(api.kiroPrerequisite).mockReturnValue(
      new Promise<KiroPrerequisiteStatus>(resolve => { resolveStatus = resolve }),
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()

    resolveStatus(status({ installed: true, initial_setup_complete: true }))

    await waitFor(() => expect(api.kiroPrerequisite).toHaveBeenCalled())
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Kiro Crew needs Kiro sign-in.')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText('Your crew is almost ready.')).not.toBeInTheDocument()
  })

  it('never shows setup chrome to a genuine-first-run user until confirmed', async () => {
    // The setup gate is reachable ONLY from a resolved status that actually says
    // first-run. While unresolved, even a true first-time user sees the app
    // rather than a setup screen that might turn out to be wrong.
    let resolveStatus: (value: KiroPrerequisiteStatus) => void = () => {}
    vi.mocked(api.kiroPrerequisite).mockReturnValue(
      new Promise<KiroPrerequisiteStatus>(resolve => { resolveStatus = resolve }),
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()

    resolveStatus(status())

    expect(await screen.findByText('Set up Kiro')).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('keeps setup visible and offers retry for a live gateway error', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('We could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.getByText(/Probe failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('terminates an unpunctuated gateway error before the next sentence', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(401, 'Token required'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(
      await screen.findByText('Token required. Retry the gateway check before starting a session.'),
    ).toBeInTheDocument()
  })

  it('keeps a space between the retry icon and its label', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const retry = await screen.findByRole('button', { name: 'Try again' })
    expect(retry.textContent).toBe(' Try again')
  })

  it('punctuates only when the message needs it', () => {
    expect(asSentence('Token required')).toBe('Token required.')
    expect(asSentence('The gateway returned an unexpected error.'))
      .toBe('The gateway returned an unexpected error.')
    expect(asSentence('Is the gateway running?')).toBe('Is the gateway running?')
    expect(asSentence('  Token required  ')).toBe('Token required.')
    expect(asSentence('')).toBe('')
  })

  it('remembers a returning user across a cold start with an erroring gateway', async () => {
    // Second flash path, independent of the pending one: on a cold load (empty
    // React Query cache) a gateway error has no `prerequisite` to fall back on,
    // so the gate would render full-screen setup-branded chrome at a user who has
    // completed setup. The client remembers first-run completion locally, so a
    // returning user gets the dashboard plus a reauth banner instead.
    localStorage.setItem('kirocrew:kiro-setup-complete', '1')
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Your crew is almost ready.')).not.toBeInTheDocument()
    expect(screen.queryByText('We could not check Kiro CLI.')).not.toBeInTheDocument()
  })

  it('leaves a returning user fully unblocked when the status is unusable', async () => {
    // An unreachable status check is not evidence the CLI is broken, and the
    // turn reports the truth either way — so a returning user keeps a clean,
    // fully usable dashboard rather than a "could not check" banner.
    localStorage.setItem('kirocrew:kiro-setup-complete', '1')
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      null as unknown as KiroPrerequisiteStatus,
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Could not check Kiro CLI.')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText('Your crew is almost ready.')).not.toBeInTheDocument()
  })

  it('still surfaces an unusable status body to a first-run user', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      null as unknown as KiroPrerequisiteStatus,
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('We could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.getByText(/returned no prerequisite status/)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('records first-run completion so later cold starts skip setup chrome', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
      initial_setup_complete: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    await waitFor(() =>
      expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBe('1'),
    )
  })

  it('still gates a genuine first run when no prior completion is remembered', async () => {
    // The remembered bit must not become a blanket bypass: a true first-run
    // user (nothing in storage) still gets the full setup gate.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Set up Kiro')).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('reports an unbuildable sandbox as its own state, not a missing CLI', async () => {
    // Verification runs the CLI INSIDE the sandbox, so a host that cannot build
    // one fails verification with the binary present and signed in. Rendering
    // "Install Kiro CLI" here would be false and its button could not help, so
    // this names the real cause and offers only a retry.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(
      await screen.findByText('Kiro CLI is installed but could not be verified'),
    ).toBeInTheDocument()
    expect(screen.getByText(/provides no OS-level sandbox/)).toBeInTheDocument()
    // The technical reason names the failing step, so it is shown verbatim.
    expect(
      screen.getByText('unshare(CLONE_NEWNS) failed with errno 1 (EPERM)'),
    ).toBeInTheDocument()
    // No install/sign-in dead ends, and the dashboard stays withheld.
    expect(screen.queryByRole('button', { name: 'Install Kiro CLI' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('tells a transient sandbox failure apart from a host verdict', async () => {
    // The remedies diverge: retry versus change the host. Advising someone to
    // disable their own isolation over a momentary EAGAIN is the outcome this
    // wording exists to prevent.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'transient',
      sandbox_detail: 'fork failed with errno 11 (EAGAIN)',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/temporary resource limit/)).toBeInTheDocument()
    expect(screen.getByText(/do not disable the sandbox/)).toBeInTheDocument()
    // The aside must not contradict the headline by still saying "Install Kiro CLI".
    expect(screen.queryByText(/Install Kiro CLI, sign in once/)).not.toBeInTheDocument()
    expect(screen.queryByText(/provides no OS-level sandbox/)).not.toBeInTheDocument()
    // A momentary failure identifies nothing to reconfigure, so the host
    // remedies must stay hidden — they would be advice to break a working setup.
    expect(screen.queryByText('How to fix')).not.toBeInTheDocument()
    expect(screen.queryByText('kirocrew service install')).not.toBeInTheDocument()
  })

  it('offers the cap remedy on a transient verdict without telling the user to act now', async () => {
    // `user.max_user_namespaces` exhaustion surfaces as ENOSPC, which is
    // indistinguishable from momentary pressure, so a host with the cap set to 0
    // is reported transient forever and is never cached. Suppressing the remedy
    // here left exactly that host with a retry button and no way out.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'transient',
      sandbox_detail: 'unshare(CLONE_NEWUSER) failed with errno 28 (ENOSPC)',
      sandbox_remedy: 'max_user_namespaces',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    // Conditional framing, so it never reads as "reconfigure a host that is fine".
    expect(await screen.findByText('If this keeps happening')).toBeInTheDocument()
    expect(screen.queryByText('How to fix')).not.toBeInTheDocument()
    expect(screen.getByText(/per-user cap/)).toBeInTheDocument()
    // Both the explanation and the copyable command name the sysctl.
    expect(screen.getAllByText(/user\.max_user_namespaces/).length).toBeGreaterThan(1)
    // The transient body still leads, and still must not push a disable.
    expect(screen.getByText(/temporary resource limit/)).toBeInTheDocument()
    expect(screen.getByText(/do not disable the sandbox/)).toBeInTheDocument()
  })

  it('names the AppArmor mechanism and the command that fixes it', async () => {
    // Issue #1660: the screen used to show `errno 1 (EPERM)` and a retry button.
    // The probe already knew this was Ubuntu's restricted-profile restriction —
    // NEWUSER succeeded and NEWNS was denied — and that the fix is the narrow
    // AppArmor profile `service install` writes.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
      sandbox_remedy: 'apparmor_userns',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('How to fix')).toBeInTheDocument()
    expect(screen.getByText('kirocrew service install')).toBeInTheDocument()
    // `aa-exec -p` must NOT be offered: entering a named profile is not
    // permitted for an unconfined user and aa-exec execs unconfined instead of
    // failing, so the command looks applied and changes nothing.
    expect(screen.queryByText(/aa-exec/)).not.toBeInTheDocument()
    // The generic "this host provides no OS-level sandbox" line is FALSE here:
    // user namespaces work, the kernel denied the second step.
    expect(screen.queryByText(/provides no OS-level sandbox/)).not.toBeInTheDocument()
    expect(screen.getByText(/allows user namespaces/)).toBeInTheDocument()
    // The reporter's explicit ask: tell me to run doctor.
    expect(screen.getByText('kirocrew doctor')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Linux sandbox guide/ })).toHaveAttribute(
      'href',
      expect.stringContaining('docs/guides/install.md'),
    )
  })

  it('copies a command to the clipboard when its block is clicked', async () => {
    // The command has to be retyped on the gateway host and one typo restarts
    // the loop, so the whole block is the copy target rather than a small glyph.
    const { copyToClipboard } = await import('../utils/clipboard')
    vi.mocked(copyToClipboard).mockClear()
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
      sandbox_remedy: 'apparmor_userns',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const command = await screen.findByText('kirocrew service install')
    const button = command.closest('button')
    expect(button).not.toBeNull()
    fireEvent.click(button!)

    // Read out of the DOM, so what is copied is exactly what is shown — not a
    // duplicated prop that could drift from the rendered text.
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith('kirocrew service install'),
    )
  })

  it('offers exactly one AppArmor command, and never aa-exec', async () => {
    // systemd is what attaches the profile, so the service is the only path that
    // applies it. An `aa-exec -p` alternative is worse than none: entering a
    // named profile needs privilege, and aa-exec execs unconfined rather than
    // failing, so it reads as applied while changing nothing.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
      sandbox_remedy: 'apparmor_userns',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const command = await screen.findByText('kirocrew service install')
    const list = command.closest('ul, ol')
    expect(list).not.toBeNull()
    expect(list!.querySelectorAll('li')).toHaveLength(1)
    expect(screen.queryByText(/aa-exec/)).not.toBeInTheDocument()
    // No numerals: a single item must not read as step one of several.
    expect(screen.queryByText('1.')).not.toBeInTheDocument()
  })

  it('still points at doctor when the mechanism is unknown', async () => {
    // An unclassified failure has no command to offer, but a dead end with a
    // retry button was the original complaint. The diagnostic pointer is the
    // floor, not the bonus — and the "How to fix" heading must NOT appear over a
    // section holding only a diagnostic, or it promises a fix it cannot deliver.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 5 (EIO)',
      sandbox_remedy: '',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('kirocrew doctor')).toBeInTheDocument()
    expect(screen.queryByText('How to fix')).not.toBeInTheDocument()
    expect(screen.queryByText('kirocrew service install')).not.toBeInTheDocument()
    // Unclassified means the generic body is the honest one.
    expect(screen.getByText(/provides no OS-level sandbox/)).toBeInTheDocument()
  })

  it('keeps the retry button out of the scrolling region', async () => {
    // The remedy content overflows the panel's fixed height, and a primary
    // action bisected by the panel edge reads as a rendering defect rather than
    // a scroll cue. Pinning it below the scroll region keeps it whole at any
    // content length or locale.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
      sandbox_remedy: 'apparmor_userns',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const button = await screen.findByRole('button', { name: 'Check again' })
    // The scrim also scrolls, so target the inner content column specifically —
    // that is the one whose overflow was clipping the button.
    const column = document.querySelector('div.flex-1.overflow-y-auto')
    expect(column).not.toBeNull()
    expect(column!.contains(button)).toBe(false)
  })

  it('offers no host remedy when a foreign sandbox is the cause', async () => {
    // This host's sandbox is fine — it just cannot nest. Sending the user to
    // change a sysctl would be a fix for a problem they do not have.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'foreign_sandbox',
      sandbox_detail: 'sandbox-exec probe failed',
      sandbox_remedy: '',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/Another sandbox already confines/)).toBeInTheDocument()
    expect(screen.queryByText('How to fix')).not.toBeInTheDocument()
  })

  it('never withholds the dashboard from a ready install over a sandbox flag', async () => {
    // Precedence guard: `ready` wins. A working install must never be hijacked
    // by this screen.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(
      screen.queryByText('Kiro CLI is installed but could not be verified'),
    ).not.toBeInTheDocument()
  })

  it('leaves an established install alone rather than hijacking it', async () => {
    // Deliberate scope: this screen replaces the first-run screen that would
    // otherwise lie. A returning user keeps their dashboard, and the per-turn
    // error card carries the sandbox failure in context — which is specific now
    // that the probe names the failing step.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      initial_setup_complete: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })
})
