import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { KiroPrerequisiteStatus } from '../api/client'
import KiroPrerequisiteGate, {
  asSentence,
  kiroPrerequisiteRefetchInterval,
} from '../components/KiroPrerequisiteGate'
import { useKiroSessionReady } from '../providers/KiroReadinessContext'
import { renderWithProviders } from './helpers'

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
    installKiroPrerequisite: vi.fn(),
    loginKiroPrerequisite: vi.fn(),
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
    can_auto_install: true,
    can_login: true,
    repair_required: false,
    docs_url: 'https://kiro.dev/docs/cli/installation/',
    setup_allowed: true,
    operation: {
      kind: '',
      status: 'idle',
      message: '',
      detail: '',
      url: '',
      error: '',
    },
    ...overrides,
  }
}

function SessionReadinessProbe() {
  const ready = useKiroSessionReady()
  return <div>{ready ? 'Sessions ready' : 'Sessions paused'}</div>
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
    expect(kiroPrerequisiteRefetchInterval(status({
      operation: {
        kind: 'login',
        status: 'running',
        message: '',
        detail: '',
        url: '',
        error: '',
      },
    }))).toBe(1_000)
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
        <SessionReadinessProbe />
      </KiroPrerequisiteGate>,
    )

    // The dashboard mounts before the check resolves, so await the
    // resolution-specific signal (sessions unpausing), not the dashboard itself.
    expect(await screen.findByText('Sessions ready')).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
  })

  it('installs on the named gateway host and unlocks device login', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Windows' }))
    vi.mocked(api.installKiroPrerequisite).mockResolvedValue(status({
      platform: 'Windows',
      installed: true,
      operation: {
        kind: 'install',
        status: 'succeeded',
        message: 'Kiro CLI is installed.',
        detail: '',
        url: '',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/Kiro Crew uses Kiro CLI/)).toBeInTheDocument()
    expect((await screen.findAllByText(/Windows gateway host/)).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Install Kiro CLI' }))
    await waitFor(() => expect(api.installKiroPrerequisite).toHaveBeenCalledOnce())
    expect(await screen.findByRole('button', { name: 'Sign in to Kiro' })).toBeEnabled()
  })

  it('offers sign-in for an already-installed CLI regardless of install source', async () => {
    // A user-owned / self-updated / toolbox Kiro CLI that runs is installed and
    // sign-in ready — no "unverified executable" dead end, no repair prompt.
    // The mock reproduces the exact OLD rejected-provenance status
    // (can_login:false + repair_required:true): under the pre-change gate this
    // rendered a button-less "Reinstall" dead end; the new "runs" contract must
    // ignore both fields and still offer an enabled Sign-in — so this fails on
    // revert of the can_login/repair_required gate removals.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      can_auto_install: false,
      can_login: false,
      repair_required: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const loginButton = await screen.findByRole('button', { name: 'Sign in to Kiro' })
    expect(loginButton).toBeEnabled()
    expect(screen.queryByText(/unverified executable/)).not.toBeInTheDocument()
    expect(screen.queryByText('rm -- ~/.local/bin/kiro-cli')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Installed' })).toBeDisabled()
  })

  it('shows the secure device URL and advances when login becomes ready', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))
    vi.mocked(api.loginKiroPrerequisite).mockResolvedValue(status({
      installed: true,
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Open the sign-in page.',
        detail: 'Enter code ABCD-EFGH',
        url: 'https://view.awsapps.com/start/',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in to Kiro' }))
    const link = await screen.findByRole('link', { name: /Open Kiro sign-in page/ })
    expect(link).toHaveAttribute('href', 'https://view.awsapps.com/start/')
    expect(screen.getByText(/ABCD-EFGH/)).toBeInTheDocument()
  })

  it('does not render a login link when browser URL parsing rejects it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Open the sign-in page.',
        detail: 'Enter code ABCD-EFGH',
        url: 'https://evil.example\\@view.awsapps.com/start',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/ABCD-EFGH/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Open Kiro sign-in page/ })).not.toBeInTheDocument()
  })

  it('shows non-owners a redacted owner-setup state', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      can_auto_install: false,
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/gateway owner needs to finish setup/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Install Kiro CLI' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('lets a non-owner observe owner completion without reloading', async () => {
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(status({
        platform: 'gateway',
        can_auto_install: false,
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

  it('keeps an established dashboard navigable during Kiro reauthentication', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      initial_setup_complete: true,
    }))
    vi.mocked(api.loginKiroPrerequisite).mockResolvedValue(status({
      installed: true,
      initial_setup_complete: true,
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Open the sign-in page.',
        detail: 'Enter code ABCD-EFGH',
        url: 'https://view.awsapps.com/start/',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate>
        <div>Dashboard loaded</div>
        <SessionReadinessProbe />
      </KiroPrerequisiteGate>,
    )

    // Await the banner (the resolved-status signal) — the dashboard is mounted
    // from the first render now, so it is not a resolution signal.
    expect(await screen.findByText('Kiro Crew needs Kiro sign-in.')).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.getByText('Sessions paused')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveClass('pointer-events-none')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in to Kiro' }))
    await waitFor(() => expect(api.loginKiroPrerequisite).toHaveBeenCalledOnce())
    expect(await screen.findByText(/ABCD-EFGH/)).toBeInTheDocument()
  })

  it('offers a copyable terminal sign-in command in the re-auth banner', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    // happy-dom's navigator.clipboard is getter-only; defineProperty replaces it.
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      ready: false,
      initial_setup_complete: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('kiro-cli login')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /copy sign-in command/i }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('kiro-cli login'))
    // The retry control reads as a post-sign-in re-check, not a failed-probe retry.
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
  })

  it('keeps an established non-owner dashboard open while the owner reconnects', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      initial_setup_complete: true,
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/gateway owner needs to restore Kiro access/))
      .toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
  })

  it('fails open when connected to a gateway without the new endpoint', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(404, 'HTTP 404'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('mounts the dashboard immediately while the first check is pending', async () => {
    // The bug: the pending state rendered the full-screen SETUP shell ("Your
    // crew is almost ready.") for the whole first round trip — which is slow
    // because the gateway probe shells out to kiro-cli twice. A returning user
    // saw the first-run setup screen flash and vanish.
    //
    // Kiro readiness gates starting a TURN, not using the dashboard, so an
    // unresolved check must not withhold the app: mount it with sessions paused
    // and let the resolved status add the reauth banner / unpause / (only for a
    // confirmed first run) show setup.
    let resolveStatus: (value: KiroPrerequisiteStatus) => void = () => {}
    vi.mocked(api.kiroPrerequisite).mockReturnValue(
      new Promise<KiroPrerequisiteStatus>(resolve => { resolveStatus = resolve }),
    )

    renderWithProviders(
      <KiroPrerequisiteGate>
        <div>Dashboard loaded</div>
        <SessionReadinessProbe />
      </KiroPrerequisiteGate>,
    )

    // No waiting screen and no setup chrome — the app itself is already up.
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Your crew is almost ready.')).not.toBeInTheDocument()
    expect(screen.queryByText('One quick setup')).not.toBeInTheDocument()
    // Sessions stay paused until the gateway confirms readiness.
    expect(screen.getByText('Sessions paused')).toBeInTheDocument()

    resolveStatus(status({ installed: true, authenticated: true, ready: true }))
    expect(await screen.findByText('Sessions ready')).toBeInTheDocument()
  })

  it('adds the reauth banner when a pending check resolves to signed-out', async () => {
    let resolveStatus: (value: KiroPrerequisiteStatus) => void = () => {}
    vi.mocked(api.kiroPrerequisite).mockReturnValue(
      new Promise<KiroPrerequisiteStatus>(resolve => { resolveStatus = resolve }),
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()

    resolveStatus(status({ installed: true, initial_setup_complete: true }))

    expect(await screen.findByText('Kiro Crew needs Kiro sign-in.')).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
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
    // so the gate rendered full-screen setup-branded chrome at a user who has
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

  it('treats an unusable status body the same as an error for a returning user', async () => {
    // Both no-usable-status paths (error, and a settled-but-falsy body) share one
    // guard, so a returning user never gets first-run chrome from either.
    localStorage.setItem('kirocrew:kiro-setup-complete', '1')
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      null as unknown as KiroPrerequisiteStatus,
    )

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
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
      <KiroPrerequisiteGate>
        <div>Dashboard loaded</div>
        <SessionReadinessProbe />
      </KiroPrerequisiteGate>,
    )

    // Wait for the resolved status to be applied (sessions unpause), then assert
    // the completion bit was recorded — the dashboard renders before resolution.
    expect(await screen.findByText('Sessions ready')).toBeInTheDocument()
    expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBe('1')
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
})
