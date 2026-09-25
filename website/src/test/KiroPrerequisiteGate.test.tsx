import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { KiroPrerequisiteStatus } from '../api/client'
import KiroPrerequisiteGate, {
  acpBackendsRefetchInterval,
  asSentence,
  kiroPrerequisiteIsBlocking,
  kiroPrerequisiteRefetchInterval,
} from '../components/KiroPrerequisiteGate'
import { renderWithProviders } from './helpers'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn(),
}))

vi.mock('../api/client', async () => ({
  // The REAL ApiError, not a stand-in: the shared `setupMarkerError*` /
  // `agentChoiceSaved` helpers in `api/acpBackend` narrow with `instanceof`
  // against `api/apiError`, so an error the test builds has to be that same
  // class or every marker-write assertion silently reads `null`. `apiError` is
  // the side-effect-free split, so importing it here pulls in none of client's
  // transport graph.
  ApiError: (await import('../api/apiError')).ApiError,
  api: {
    kiroPrerequisite: vi.fn(),
    repairKiroPrerequisiteSpecs: vi.fn(),
    kirocrewConfig: vi.fn(),
    acpBackends: vi.fn(),
    acpBackendRecheck: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import { api, ApiError } from '../api/client'
import type { AcpBackendProbe } from '../api/client'
import { CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'

function probe(overrides: Partial<AcpBackendProbe> = {}): AcpBackendProbe {
  return {
    id: 'claude',
    policy_id: 'claude',
    selectable: true,
    independent_setup: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    ...overrides,
  }
}

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
    bundled_cli: false,
    setup_allowed: true,
    sandbox_unavailable: false,
    sandbox_backend_available: true,
    sandbox_failure_kind: '',
    sandbox_detail: '',
    sandbox_remedy: '',
    missing_agent_specs: [],
    agent_spec_repair_error: '',
    ...overrides,
  }
}

// The Pod remedy block is real nested YAML. Matched on the code element's exact
// text: RTL's default matcher collapses whitespace, which would hide a lost
// newline or indentation — exactly the defect that makes a paste invalid.
const POD_YAML = 'securityContext:\n  appArmorProfile:\n    type: Unconfined'
const podBlock = () =>
  screen.getByText((_, el) => el?.tagName === 'CODE' && el.textContent === POD_YAML)

describe('KiroPrerequisiteGate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // The gate remembers first-run completion in localStorage, so each case
    // must start from a clean slate or a prior test's completion would leak in
    // and silently bypass the setup assertions.
    localStorage.clear()
    // Default: Kiro CLI is the configured agent and no other harness is listed,
    // so every pre-existing case exercises the Kiro checks exactly as before.
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
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

  it('polls the harness probe at the server\'s cache rate, and only behind a setup screen', () => {
    const missing = probe({ installed: 'missing' })
    // The endpoint serves a 30s cache (backend_install.CACHE_TTL_SECONDS), the
    // same one Settings → Agent Backend polls at 30s; asking faster returns the
    // same bytes. The per-agent Check again is the route for someone who cannot wait.
    expect(acpBackendsRefetchInterval(status(), false, undefined)).toBe(30_000)
    expect(acpBackendsRefetchInterval(status(), true, missing)).toBe(30_000)
    // A configured agent that is usable opens the dashboard: nothing left to poll.
    expect(acpBackendsRefetchInterval(status(), true, probe())).toBe(false)
    // An ESTABLISHED install renders the dashboard whatever the probe says about
    // a configured agent, so a missing one must not be re-probed forever behind
    // a screen nobody is looking at — the first turn reports it, in context.
    expect(acpBackendsRefetchInterval(status({ initial_setup_complete: true }), true, missing)).toBe(false)
    expect(acpBackendsRefetchInterval(status({ ready: true }), false, undefined)).toBe(false)
    expect(acpBackendsRefetchInterval(status({ setup_allowed: false }), false, undefined)).toBe(false)
    expect(acpBackendsRefetchInterval(undefined, false, undefined)).toBe(false)
  })

  it('admits a KAS first run on kiro-cli ACP support, matching the marker', () => {
    // KAS is kiro-cli's relay: it completes on kiro-cli ACP support, the SAME
    // rule record_independent_backend_setup enforces before writing the marker.
    // A gateway that reports acp_supported opens the dashboard (nothing left to poll).
    const kas = probe({ id: 'kas', policy_id: 'kas', independent_setup: false })
    expect(acpBackendsRefetchInterval(status(), true, kas)).toBe(false)
    // A too-old kiro-cli (acp_supported false) refuses KAS — keep polling. This
    // is the case the marker also refuses, so the two cannot drift.
    expect(acpBackendsRefetchInterval(status({ acp_supported: false }), true, kas)).toBe(30_000)
    // KAS loads Kiro Crew's agent specs: a missing or rejected spec keeps the
    // gate on its repair card instead of opening the dashboard.
    expect(acpBackendsRefetchInterval(status({ missing_agent_specs: ['kirocrew-lite'] }), true, kas)).toBe(30_000)
    expect(acpBackendsRefetchInterval(status({ rejected_agent_specs: ['kirocrew'] }), true, kas)).toBe(30_000)
    // Only KAS takes the acp_supported rule: any other backend without
    // independent_setup keeps polling, exactly as the marker refuses it.
    const other = probe({ id: 'goose', policy_id: 'goose', independent_setup: false })
    expect(acpBackendsRefetchInterval(status(), true, other)).toBe(30_000)
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

    await screen.findByText(/Get a coding agent running on the/)
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

    fireEvent.click(await screen.findByRole('button', { name: 'Check sign-in again' }))

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

    expect(await screen.findByText(/Get a coding agent running on the/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check again for Kiro CLI' }))
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

    expect(await screen.findByText(/Get a coding agent running on the/)).toBeInTheDocument()
    expect((await screen.findAllByText(/Windows gateway host/)).length).toBeGreaterThan(0)

    const setupLink = screen.getByRole('link', { name: /Open Kiro CLI setup/ })
    expect(setupLink).toHaveAttribute('href', 'https://kiro.dev/cli/')
    expect(setupLink).toHaveAttribute('target', '_blank')
    expect(setupLink).toHaveAttribute('rel', expect.stringContaining('noopener'))
    expect(screen.queryByRole('button', { name: /Install Kiro CLI/ })).not.toBeInTheDocument()
    // No sign-in action exists at all — the user signs in with Kiro CLI.
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
  })

  it('pins its footer actions to the bottom of the scrolling scrim on a phone', async () => {
    // Same stacked layout as the onboarding chapters: without the pinned
    // footer the actions sit below the fold under the browser toolbar. The
    // Kiro-missing screen carries its Check again inside the Kiro CLI card and
    // has no footer, so this uses a screen that still has one: the sandbox
    // verdict, whose only action is the footer's retry.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
    }))
    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    const footer = await screen.findByTestId('gate-footer')
    const cls = footer.className.split(/\s+/)
    expect(cls).toEqual(expect.arrayContaining(['sticky', 'bottom-0', 'bg-card', 'sm:static']))
    expect(footer.className).toContain('env(safe-area-inset-bottom)')
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

    expect(await screen.findByText(/Kiro CLI is installed.*Finish signing in/))
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
    expect(screen.getByRole('button', { name: 'Check sign-in again' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(screen.queryByText(/unverified executable/)).not.toBeInTheDocument()
    // A PATH install: bare commands, nothing muted, no bundled-copy hint.
    expect(screen.queryByText(/desktop app.s own copy of Kiro CLI/)).not.toBeInTheDocument()
  })

  it('explains then mutes the shared bundled path', async () => {
    // A desktop install resolves the app's own kiro-cli, which is not on the
    // user's PATH, so both commands open with the same quoted absolute path and
    // differ only after `login`. Two full lines read as one command shown twice,
    // so the shared run is muted and the tail carries the weight; the copied
    // text is still the whole command. The hint saying what the path is applies
    // to both commands equally, so it appears ONCE before either path is read.
    const bin =
      "'/Applications/Kiro Crew.app/Contents/Resources/backend-dist/kiro-cli/kiro-cli-chat'"
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      bundled_cli: true,
      login_command: `${bin} login`,
      sso_login_command: `${bin} login --use-device-flow --license pro`,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText(/Kiro CLI is installed.*Finish signing in/)
    const codes = screen.getAllByText(
      (_, el) => el?.tagName === 'CODE' && (el.textContent ?? '').startsWith(bin),
    )
    expect(codes.map((c) => c.textContent)).toEqual([
      `${bin} login`,
      `${bin} login --use-device-flow --license pro`,
    ])
    for (const code of codes) {
      expect(code.querySelector('span.text-muted')?.textContent).toBe(`${bin} `)
    }
    const hints = screen.getAllByText(/desktop app.s own copy of Kiro CLI.*exactly as shown/)
    expect(hints).toHaveLength(1)
    expect(
      hints[0].compareDocumentPosition(codes[0]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('a copy that fails says so under the box instead of painting Copied', async () => {
    // Both clipboard paths can be denied (no API, execCommand refused). The
    // command is the only instruction on this screen, so a silent miss strands
    // the user; the failure renders through ErrorNotice and clears on success.
    const { copyToClipboard } = await import('../utils/clipboard')
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText(/Kiro CLI is installed.*Finish signing in/)
    const [button] = screen.getAllByRole('button', { name: /Copy command/ })
    fireEvent.click(button)
    const notice = await screen.findByTestId('kiro-gate-copy-failed')
    expect(notice.textContent).toMatch(/Copy failed/)
    expect(screen.queryByRole('button', { name: /Copied/ })).toBeNull()

    fireEvent.click(button)
    await waitFor(() => expect(screen.queryByTestId('kiro-gate-copy-failed')).toBeNull())
    expect(screen.getAllByRole('button', { name: /Copied/ }).length).toBeGreaterThan(0)
  })

  it('exposes no way to start a sign-in from the dashboard', async () => {
    // Kiro Crew does not authenticate for the user: there is no device-flow
    // trigger, no sign-in URL, and no device code surfaced anywhere.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText(/Kiro CLI is installed.*Finish signing in/)
    expect(api).not.toHaveProperty('loginKiroPrerequisite')
    expect(screen.queryByRole('link', { name: /Open Kiro sign-in page/ })).not.toBeInTheDocument()
    // The setup link is gone once the CLI is found. The Kiro card owns its
    // re-check, without a second page-level control.
    const buttons = screen.getAllByRole('button').map(b => b.textContent || '')
    expect(buttons.filter(t => /Check sign-in again/.test(t))).toHaveLength(1)
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

  it('swaps the ask-the-owner body for the re-auth remedy while the auth banner is up', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      setup_allowed: false,
    }))
    // The stale-owner banner element is the signal source: present at mount.
    const banner = document.createElement('div')
    banner.id = 'mc-session-expired'
    document.body.prepend(banner)
    try {
      renderWithProviders(
        <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
      )
      // One instruction, not two: eyebrow, headline, and body all name the
      // sign-in remedy instead of telling the viewer to ask someone else.
      expect(await screen.findByText(/Sign in again to continue/)).toBeInTheDocument()
      expect(screen.getByText(/Sign in required/)).toBeInTheDocument()
      expect(screen.getByText(/predates the configured owner/)).toBeInTheDocument()
      expect(screen.queryByText(/gateway owner needs to finish setup/)).not.toBeInTheDocument()
      expect(screen.queryByText(/Ask the .* owner to install/)).not.toBeInTheDocument()
      // "Check again" cannot succeed until sign-in, so the state carries no
      // retry affordance — the banner is the single action.
      expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
      // Clearing the banner flips the whole surface back to the setup copy.
      window.dispatchEvent(new CustomEvent('mc-auth-cleared'))
      expect(await screen.findByText(/Ask the .* owner to install/)).toBeInTheDocument()
      expect(screen.getByText(/gateway owner needs to finish setup/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
    } finally {
      banner.remove()
    }
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

  it('gates on a spec that is PRESENT but which kiro-cli refuses', async () => {
    // The gap the missing-specs card cannot cover: statting the file says it is
    // there, while kiro-cli drops it from its agent table, so Kiro Crew's agent
    // silently becomes kiro-cli's default one with none of its MCP servers.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: false,
      initial_setup_complete: true,
      repair_required: true,
      missing_agent_specs: [],
      rejected_agent_specs: ['kirocrew.json'],
      agent_spec_rejection_detail:
        'Error: Json supplied at /home/u/.kiro/agents/kirocrew.json is invalid: '
        + 'data did not match any variant of untagged enum Repr',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(
      await screen.findByText("Kiro CLI will not load Kiro Crew's agent specs"),
    ).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    // Exact match on the list entry: the reason below also contains the filename
    // as part of a full path, so a loose regex matches both nodes.
    expect(screen.getByText('kirocrew.json')).toBeInTheDocument()
    // kiro-cli's own words are what make the report actionable, so they are
    // surfaced verbatim rather than replaced with our own paraphrase.
    expect(screen.getByText(/data did not match any variant/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('does not promise that checking again rewrites a rejected spec', async () => {
    // The button deliberately does NOT rewrite a rejected spec: the file is on
    // disk, and regenerating it would discard a concurrent MCP toggle's
    // tools/allowedTools grant. So the copy must not imply a rewrite, must name
    // the control the user can actually see, and must point at the two real
    // remedies (update, or an explicit setup --clean).
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      rejected_agent_specs: ['kirocrew.json'],
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const note = await screen.findByText(/Check again asks Kiro CLI to load the specs again/)
    expect(note).toHaveTextContent('does not rewrite them')
    // The leading cause is a kiro-cli upgrade, which re-checking cannot fix, so
    // both remedies must be present as their own lines rather than buried.
    expect(screen.getByText(/Update Kiro Crew\./)).toBeInTheDocument()
    expect(screen.getByText(/Rewrite the specs from scratch/)).toBeInTheDocument()
    // The command must NOT come from a catalog value: a translator must not be
    // able to alter a string the user pastes into a shell.
    const command = screen.getByText('kirocrew setup --agent-only --clean')
    expect(command.tagName).toBe('CODE')
    // The label the copy names must be the label actually rendered.
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument()
  })

  it('shows the missing-specs card, not the rejected one, when a spec is absent', async () => {
    // One fault, one card. A spec that is absent cannot also be rejected, and
    // the absent case has a repair that definitely works.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      missing_agent_specs: ['kirocrew.json'],
      rejected_agent_specs: ['kirocrew-lite.json'],
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(
      await screen.findByText("Kiro Crew's agent specs are not installed"),
    ).toBeInTheDocument()
    expect(
      screen.queryByText("Kiro CLI will not load Kiro Crew's agent specs"),
    ).not.toBeInTheDocument()
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

  it('shows the probe diagnostic when the backend backstop degrades a probe exception to 200', async () => {
    // The backend's last-resort backstop reports an exception as a retryable
    // not-ready 200 body (never a 500), so `prerequisite` resolves and the
    // ApiError branch above never fires — this is the "Setup Check Unavailable
    // with no reason" symptom the diagnostic exists to fix.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      probe_error: 'OSError: probe wedged',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('We could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.getByText(/OSError: probe wedged/)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('names the probe exit status alongside the message when both are present', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      probe_error: 'toolbox: kiro-cli is not registered',
      probe_status: 127,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(
      await screen.findByText(/toolbox: kiro-cli is not registered \(exit 127\)/),
    ).toBeInTheDocument()
  })

  it('does not show a diagnostic screen when there is nothing to report', async () => {
    // No probe_error at all: the ordinary first-run screen renders as before.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Set up Kiro')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-gate-status-error')).not.toBeInTheDocument()
  })

  it('terminates an unpunctuated gateway error before the next sentence', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(401, 'Token required'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    // The gateway's message is the ErrorNotice (terminated as a sentence, since
    // it is read as one), and the retry hint is its own line beneath it.
    const alert = await screen.findByTestId('kiro-gate-status-error')
    expect(alert).toHaveAttribute('role', 'alert')
    expect(alert).toHaveTextContent('Token required.')
    expect(
      screen.getByText('Retry the gateway check before starting a session.'),
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

  it('names the container policy and both spellings of the AppArmor switch for a refused mount', async () => {
    // Issue #10765: a non-root Kubernetes pod granted both namespaces and its
    // runtime's default AppArmor profile then refused the launcher's first
    // mount. The probe now names that step, so the gate can say the fix is the
    // container's policy — not root, not CAP_SYS_ADMIN, not a sysctl — and
    // spell it for Docker and for a Pod.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'mount(MS_REC|MS_PRIVATE) on / failed with errno 13 (EACCES)',
      sandbox_remedy: 'mount_denied',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('How to fix')).toBeInTheDocument()
    expect(
      screen.getByText(/--security-opt apparmor=unconfined/),
    ).toBeInTheDocument()
    // Real nested YAML, so it drops into a Pod manifest as-is.
    expect(podBlock()).toBeInTheDocument()
    // Each block is captioned, so the reader knows which of the two is theirs.
    expect(screen.getByText('Docker')).toBeInTheDocument()
    expect(screen.getByText('Kubernetes Pod')).toBeInTheDocument()
    // Namespaces work here, so the generic "no OS-level sandbox" body would be
    // false; the container body names what actually refused.
    expect(screen.queryByText(/provides no OS-level sandbox/)).not.toBeInTheDocument()
    expect(screen.getByText(/grants the user and mount namespaces/)).toBeInTheDocument()
    // A host userns remedy would be the wrong fix for a pod that already grants them.
    expect(screen.queryByText('kirocrew service install')).not.toBeInTheDocument()
    expect(screen.queryByText(/sysctl/)).not.toBeInTheDocument()
    expect(screen.getByText('kirocrew doctor')).toBeInTheDocument()
  })

  it('copies each container spelling on its own', async () => {
    // Two blocks for one switch, because Docker and a Pod spell it differently;
    // each must paste as something usable by itself — the Pod block as the
    // whole nested YAML, newlines included.
    const { copyToClipboard } = await import('../utils/clipboard')
    vi.mocked(copyToClipboard).mockClear()
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'mount(MS_REC|MS_PRIVATE) on / failed with errno 13 (EACCES)',
      sandbox_remedy: 'mount_denied',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText('How to fix')
    fireEvent.click(podBlock().closest('button')!)
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith(POD_YAML),
    )
    const docker = screen.getByText(/--security-opt apparmor=unconfined/)
    fireEvent.click(docker.closest('button')!)
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenLastCalledWith(
        '--security-opt apparmor=unconfined --security-opt seccomp=kirocrew-seccomp.json',
      ),
    )
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

  it('cues the reader that the tall remedy column scrolls', async () => {
    // The mount_denied remedy overflows the panel's fixed height, so the footer
    // divider below reads as the end of the content unless the fold is marked.
    // jsdom does no layout, so the scroll geometry is stubbed on the region.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
      sandbox_detail: 'mount(MS_REC|MS_PRIVATE) on / failed with errno 13 (EACCES)',
      sandbox_remedy: 'mount_denied',
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    await screen.findByText('How to fix')
    const region = screen.getByTestId('gate-scroll-region')
    // Overflowing, scrolled to the top: only the bottom cue should show.
    Object.defineProperty(region, 'clientHeight', { configurable: true, get: () => 300 })
    Object.defineProperty(region, 'scrollHeight', { configurable: true, get: () => 640 })
    Object.defineProperty(region, 'scrollTop', { configurable: true, get: () => 0 })
    fireEvent.scroll(region)
    await waitFor(() => expect(screen.getByTestId('gate-scroll-cue-bottom')).toBeInTheDocument())
    expect(screen.queryByTestId('gate-scroll-cue-top')).not.toBeInTheDocument()

    // Scrolled to the very end: nothing is hidden below, so the bottom cue goes.
    Object.defineProperty(region, 'scrollTop', { configurable: true, get: () => 340 })
    fireEvent.scroll(region)
    await waitFor(() => expect(screen.queryByTestId('gate-scroll-cue-bottom')).not.toBeInTheDocument())
    expect(screen.getByTestId('gate-scroll-cue-top')).toBeInTheDocument()
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

describe('KiroPrerequisiteGate agent choice', () => {
  const codeBlock = (text: string) =>
    screen.queryByText((_, el) => el?.tagName === 'CODE' && el.textContent === text)
  const CURL = 'curl -fsSL https://cli.kiro.dev/install | bash'
  const IRM = "irm 'https://cli.kiro.dev/install.ps1' | iex"
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )
  // The gate renders the app while the config/probe reads are in flight, so a
  // "dashboard is shown" assertion only means something once they have landed.
  const settle = async () => {
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalled())
    await act(() => new Promise(resolve => setTimeout(resolve, 20)))
  }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('shows the one-line Kiro CLI installer for a Linux or macOS host only', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Linux' }))
    render()

    await screen.findByText(/Run this on the Linux gateway host to install it/)
    expect(codeBlock(CURL)).toBeInTheDocument()
    expect(codeBlock(IRM)).not.toBeInTheDocument()
    // Shown to copy, never run: there is still no install button.
    expect(screen.queryByRole('button', { name: /Install Kiro CLI/ })).not.toBeInTheDocument()
  })

  it('has no separate sign-in step: the Kiro CLI card carries sign-in once installed', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    const first = render()
    await screen.findByText(/Run this on the Linux gateway host/)
    expect(screen.queryByRole('heading', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(screen.queryByText('kiro-cli login')).not.toBeInTheDocument()
    first.unmount()

    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))
    render()
    const card = (await screen.findByRole('heading', { name: 'Get Kiro CLI' })).closest('div.card-glow')
    expect(card).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(within(card as HTMLElement).getByText('kiro-cli login')).toBeInTheDocument()
    expect(within(card as HTMLElement).queryByText(/cli\.kiro\.dev\/install/)).not.toBeInTheDocument()
    // The installed state leads the card, and is said once: not repeated in
    // the footer below the other-agents section.
    expect(within(card as HTMLElement).getByText(/Kiro CLI is installed/)).toBeInTheDocument()
    expect(screen.getAllByText(/Kiro CLI is installed/)).toHaveLength(1)
  })

  it('shows the PowerShell installer for a Windows host only', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Windows' }))
    render()

    await screen.findByText(/Run this on the Windows gateway host/)
    expect(codeBlock(IRM)).toBeInTheDocument()
    expect(codeBlock(CURL)).not.toBeInTheDocument()
  })

  it('shows both installers, labelled, when the platform is unknown', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'freebsd' }))
    render()

    await screen.findByText(/Run the command for the gateway host's platform/)
    expect(codeBlock(CURL)).toBeInTheDocument()
    expect(codeBlock(IRM)).toBeInTheDocument()
    // An unlisted platform (FreeBSD, an unnamed distro) still maps to one of
    // the two commands: the Unix label says it covers more than the two names.
    expect(screen.getByText('macOS and Linux (and other Unix-like systems)')).toBeInTheDocument()
    expect(screen.getByText('Windows (PowerShell)')).toBeInTheDocument()
  })

  it('keeps other agents collapsed behind a disclosure while Kiro CLI is the default', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    const toggle = await screen.findByRole('button', { name: 'Use other coding agents' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByRole('radio', { name: /Claude Code/ })).toBeChecked()
  })

  it('offers only backends explicitly eligible for independent setup', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe({ id: '', policy_id: 'kiro', independent_setup: false }),
        probe({ id: 'kas', policy_id: 'kas', independent_setup: false }),
        probe({ id: 'codex', policy_id: 'codex' }),
        probe({ id: 'goose', policy_id: 'goose', selectable: false }),
        probe({ id: 'future', policy_id: 'future-harness', independent_setup: false }),
      ],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const radios = await screen.findAllByRole('radio')
    expect(radios.map(r => r.closest('label')?.textContent)).toEqual([
      // Named exactly as Settings → Agent names it: a harness that panel has no
      // translated name for renders under the server's policy id here too, so
      // one agent is never called two things on two screens.
      expect.stringContaining('codex'),
    ])
    expect(screen.queryByText('future-harness')).not.toBeInTheDocument()
  })

  it('switches to an installed agent and opens the dashboard', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockImplementation(async () => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
      return {}
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))

    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('keeps a failed switch in the row of the agent it was attempted for, naming the save as what failed', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe(), probe({ id: 'codex', policy_id: 'codex' })],
    })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))
    const notice = await screen.findByTestId('other-agent-switch-error')
    // The save failed; the agent did not. The row still says the agent is ready,
    // and a notice that blamed the agent would leave the reader with two
    // contradicting lines to choose between.
    expect(notice).toHaveTextContent('Could not save the agent choice. Press Use Claude Code to try again.')
    expect(notice).not.toHaveTextContent('Could not switch to Claude Code')
    const detail = screen.getByTestId('other-agent-detail')
    expect(within(detail).getByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    // Inside the row, beside the Use button that triggered it, not at the
    // foot of the list under a different agent's line.
    expect(within(detail).getByTestId('other-agent-switch-error')).toBe(notice)
    expect(within(detail).getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(within(notice).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()

    // Another agent's row does not inherit a failure it never had.
    fireEvent.click(screen.getByRole('radio', { name: 'codex' }))
    expect(screen.getByRole('radio', { name: 'codex' })).toBeChecked()
    expect(screen.queryByTestId('other-agent-switch-error')).not.toBeInTheDocument()

    // Back on the attempted agent, the notice is still there, and its Use
    // button is the retry.
    fireEvent.click(screen.getByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByTestId('other-agent-switch-error')).toHaveTextContent('Could not save the agent choice.')
    vi.mocked(api.patchConfig).mockResolvedValue({})
    fireEvent.click(screen.getByRole('button', { name: /Use Claude Code/ }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledTimes(2))
    expect(api.patchConfig).toHaveBeenLastCalledWith('agent.acp_backend', 'claude')
    await waitFor(() => expect(screen.queryByTestId('other-agent-switch-error')).not.toBeInTheDocument())
  })

  it('shows a failed switch back to Kiro CLI beside the button that attempted it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()

    const fallback = await screen.findByRole('button', { name: 'Use Kiro CLI instead' })
    fireEvent.click(fallback)
    const notice = await screen.findByTestId('use-kiro-instead-error')
    expect(notice).toHaveTextContent('Could not switch back to Kiro CLI. Press Use Kiro CLI instead to try again.')
    expect(fallback.parentElement).toBe(notice.parentElement)
    // The agent rows carry no notice for a switch they did not attempt.
    expect(screen.queryByTestId('other-agent-switch-error')).not.toBeInTheDocument()
  })

  it('explains that the agent was saved when only setup-marker persistence failed and nothing else moved', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    // The config read keeps answering Kiro, so the gate cannot lift on the
    // saved choice; the server's own partial-success sentence stays on screen.
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(
      503,
      'Agent selection was saved, but setup completion could not be recorded. Try again.',
      JSON.stringify({ code: 'setup_marker_write_failed', config_saved: true }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))
    const notice = await screen.findByTestId('other-agent-switch-error')
    expect(notice).toHaveTextContent('Agent selection was saved, but setup completion could not be recorded. Try again.')
    expect(notice).not.toHaveTextContent('Could not save the agent choice')
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
  })

  it('opens the dashboard when the agent choice saved but the setup marker did not', async () => {
    // A 503 whose body says config_saved: the config on disk names the new
    // agent, so re-reading it is what lets a saved, usable agent through. Left
    // on the failure path alone, the gate would keep showing "Set up Kiro" and
    // every retry would reproduce the same 503.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockImplementation(async () => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
      throw new ApiError(
        503,
        'Agent selection was saved, but setup completion could not be recorded. Try again.',
        JSON.stringify({ code: 'setup_marker_write_failed', config_saved: true }),
      )
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))

    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(2)
    expect(api.patchConfig).toHaveBeenCalledTimes(1)
  })

  it('does not re-read the config after a failure that saved nothing', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(
      503,
      'Setup completion could not be recorded.',
      JSON.stringify({ code: 'setup_marker_write_failed', config_saved: false }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))
    await screen.findByTestId('other-agent-switch-error')
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('shows a missing agent\'s install command and will not switch to it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({
        installed: 'missing',
        missing_components: ['claude-agent-acp'],
        install_command: 'npm install -g @zed-industries/claude-agent-acp',
      })],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    expect(await screen.findByText('Install Claude Code on the gateway host.')).toBeInTheDocument()
    expect(codeBlock('npm install -g @zed-industries/claude-agent-acp')).toBeInTheDocument()
    // The command IS the answer. A "Missing: claude-agent-acp" line beside it
    // names a second thing, and a reader second-guesses which one to install.
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeDisabled()
  })

  it('names the missing components only when there is no install command to show', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', missing_components: ['claude-agent-acp'] })],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    expect(await screen.findByText('Missing: claude-agent-acp')).toBeInTheDocument()
  })

  it('opens the picked agent\'s detail directly under its own row', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe(),
        probe({ id: 'codex', policy_id: 'codex', installed: 'missing', install_command: 'npm install -g @zed-industries/codex-acp' }),
        probe({ id: 'goose', policy_id: 'goose', installed: 'missing' }),
      ],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    // The row that owns the detail is the one whose radio sits in the same
    // outline -- not whichever row happens to be last in the list.
    const owner = () => {
      const details = screen.getAllByTestId('other-agent-detail')
      expect(details).toHaveLength(1)
      return within(details[0].parentElement as HTMLElement).getByRole('radio')
    }
    expect(owner()).toHaveAccessibleName('Claude Code')

    fireEvent.click(screen.getByRole('radio', { name: 'codex' }))
    expect(owner()).toHaveAccessibleName('codex')
    expect(owner()).toBeChecked()
    expect(codeBlock('npm install -g @zed-industries/codex-acp')).toBeInTheDocument()
  })

  it('scopes the Kiro card\'s re-check to Kiro CLI, beside each agent\'s own Check again', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    render()

    const kiroCard = (await screen.findByRole('heading', { name: 'Get Kiro CLI' })).closest('div.card-glow') as HTMLElement
    // The footer line read as Kiro-only once other agents were on screen.
    expect(screen.queryByText(/is required on the Linux gateway host/)).not.toBeInTheDocument()
    expect(within(kiroCard).getByRole('button', { name: 'Check again for Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Use other coding agents' }))
    const detail = await screen.findByTestId('other-agent-detail')
    // Two re-check buttons can be on screen at once and they check different
    // things, so no two of them carry the same label: the Kiro card's names
    // Kiro CLI, the agent's sits inside the row that names the agent.
    expect(screen.getAllByRole('button', { name: /Check again/ })).toEqual([
      within(kiroCard).getByRole('button', { name: 'Check again for Kiro CLI' }),
      within(detail).getByRole('button', { name: 'Check again' }),
    ])
  })

  it('offers no Check again on an agent that is installed and ready', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe(), probe({ id: 'pi', policy_id: 'pi', installed: 'unknown' })],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const ready = await screen.findByTestId('other-agent-detail')
    expect(within(ready).getByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    expect(within(ready).getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(within(ready).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()

    // A failed check is still something a re-check can settle, so it keeps one.
    fireEvent.click(screen.getByRole('radio', { name: 'pi' }))
    const unverified = screen.getByTestId('other-agent-detail')
    const recheck = within(unverified).getByRole('button', { name: 'Check again' })
    expect(recheck).toBeEnabled()
    expect(screen.getByText('Check failed')).toBeInTheDocument()
    expect(within(unverified).getByText(
      'Kiro Crew could not confirm that pi is installed. Press Check again to verify the installation before using this agent. Setup stays open until your chosen agent is ready. You can switch agents here, or later in Settings > Agent Harness.',
    )).toBeInTheDocument()
    expect(unverified).not.toHaveTextContent('This host cannot provide that sandbox')
    const useAgent = within(unverified).getByRole('button', { name: /Use pi/ })
    expect(useAgent).toBeDisabled()
    fireEvent.click(useAgent)
    expect(api.patchConfig).not.toHaveBeenCalled()

    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe({ id: 'pi', policy_id: 'pi' }) })
    fireEvent.click(recheck)
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('pi'))
    expect(await screen.findByText('pi is installed and ready to use.')).toBeInTheDocument()
    expect(useAgent).toBeEnabled()
    expect(within(unverified).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
    // Checking a row is not choosing it: only Use may save the new agent.
    expect(api.patchConfig).not.toHaveBeenCalled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('names no single agent as the engine in the intro', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    render()

    expect(await screen.findByText(/Get a coding agent running on the/)).toBeInTheDocument()
    expect(screen.getByText('Linux gateway host').tagName).toBe('STRONG')
    expect(screen.queryByText(/agent engine/)).not.toBeInTheDocument()
  })

  it('gives a failed check and a not-yet-active install two different badges', async () => {
    // UX round 4: "Not verified" beside "Found, not active yet" read as the same
    // "it might be there" shrug, so a user could not tell whether their agent
    // needed a re-check or was confirmed and merely not picked up yet. One badge
    // says the CHECK failed; the other says the install is confirmed and not
    // active. Neither is an instruction: the detail line carries the remedy.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe({ installed: 'unknown' }),
        probe({ id: 'pi', policy_id: 'pi', installed: 'installed', restart_required: true }),
      ],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const failed = await screen.findByText('Check failed')
    const inactive = screen.getByText('Installed, not active yet')
    expect(failed).toBeInTheDocument()
    expect(inactive).toBeInTheDocument()
    expect(failed.textContent).not.toBe(inactive.textContent)
    // Neither badge borrows the other's key word: "installed" belongs to the
    // confirmed row only, "check" to the failed row only.
    expect(failed.textContent).not.toMatch(/installed/i)
    expect(inactive.textContent).not.toMatch(/check|verif/i)
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
    expect(screen.queryByText('Found, not active yet')).not.toBeInTheDocument()
  })

  it.each([
    ['', 'empty'],
    ['gateway', 'the non-owner redaction'],
    ['Unknown', 'the gateway\u2019s own unknown label'],
    ['freebsd', 'a raw sys.platform value'],
  ])('names no platform in the intro or eyebrow when the platform is %j (%s)', async (platform) => {
    // UX round 4: `status.platform || 'local'` printed "local gateway host" in
    // the intro while the Kiro card, a few lines down, admitted it did not know
    // the host's OS. An unrecognised platform now selects a platform-free intro
    // and eyebrow rather than a filler word.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform }))
    render()

    const intro = await screen.findByText(/Get a coding agent running on the/)
    expect(intro).toHaveTextContent(
      'Get a coding agent running on the gateway host, then the dashboard will open automatically.',
    )
    expect(screen.getByText('gateway host').tagName).toBe('STRONG')
    expect(intro).not.toHaveTextContent(/local|Linux|macOS|Windows|Unknown|freebsd/)
    // The eyebrow "SETUP -> Linux gateway" drops the platform the same way.
    const eyebrow = screen.getByText('Setup').parentElement
    expect(eyebrow).toHaveTextContent('Gateway host')
    expect(eyebrow).not.toHaveTextContent(/local|Linux|macOS|Windows|Unknown|freebsd/)
    expect(screen.queryByText(/local gateway/)).not.toBeInTheDocument()
    // The Kiro card still asks for the platform's command, as before.
    expect(screen.getByText(/Run the command for the gateway host's platform/)).toBeInTheDocument()
  })

  it('still names a recognised platform in the intro and eyebrow', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Linux' }))
    render()

    const intro = await screen.findByText(/Get a coding agent running on the/)
    expect(intro).toHaveTextContent(
      'Get a coding agent running on the Linux gateway host, then the dashboard will open automatically.',
    )
    expect(screen.getByText('Linux gateway host').tagName).toBe('STRONG')
    expect(screen.getByText('Setup').parentElement).toHaveTextContent('Linux gateway')
    expect(screen.queryByText('Gateway host')).not.toBeInTheDocument()
  })

  it('retries a failed agent recheck from its single scoped control', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    vi.mocked(api.acpBackendRecheck).mockRejectedValueOnce(new ApiError(500, 'Probe failed'))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    const notice = await screen.findByTestId('other-agent-recheck-error')
    expect(notice).toHaveTextContent('The check for Claude Code failed. Press Check again.')
    expect(within(notice).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(screen.queryByTestId('other-agent-recheck-error')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(api.acpBackendRecheck).toHaveBeenLastCalledWith('claude')
  })

  it('shows a saved recheck with failed setup persistence as a retryable partial success', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    vi.mocked(api.acpBackendRecheck).mockRejectedValueOnce(new ApiError(
      503,
      'Agent check completed, but setup completion could not be recorded. Press Check again.',
      JSON.stringify({ code: 'setup_marker_write_failed' }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    const notice = await screen.findByTestId('other-agent-recheck-error')
    expect(notice).toHaveTextContent('Agent check completed, but setup completion could not be recorded. Press Check again.')
    expect(within(screen.getByTestId('other-agent-detail')).getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('applies the fresh row from a saved-but-marker-failed recheck 503 while still showing the message', async () => {
    // The 503 carries the fresh probe row: the re-probe itself succeeded and only
    // the marker write did not. The row is applied exactly as on success (the agent
    // reads installed and ready), and the marker-write message still surfaces in
    // place of the generic recheck-failed line.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    vi.mocked(api.acpBackendRecheck).mockRejectedValueOnce(new ApiError(
      503,
      'Agent check completed, but setup completion could not be recorded. Press Check again.',
      JSON.stringify({ code: 'setup_marker_write_failed', backend: probe() }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(screen.getByTestId('other-agent-recheck-error')).toHaveTextContent(
      'Agent check completed, but setup completion could not be recorded. Press Check again.',
    )
  })

  it('re-checks one agent through the cache-dropping endpoint and applies the answer', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'installed', restart_required: true })],
    })
    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    // The badge is status only, so a reader scanning the list gets one fact per
    // row; the instruction lives in the detail, beside the button it names,
    // which also says WHERE a restart happens if it comes to that, in the same
    // term as every sibling line: the gateway host, not "this machine".
    expect(await screen.findByText('Installed, not active yet')).toBeInTheDocument()
    expect(screen.queryByText('Found, not active yet')).not.toBeInTheDocument()
    expect(screen.queryByText('Installed, press Check again')).not.toBeInTheDocument()
    expect(screen.getByText(
      'Claude Code is installed on the gateway host. Press Check again to pick it up. If this line is still here after that, restart Kiro Crew on that host.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/on this machine/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Restart needed/)).not.toBeInTheDocument()
    const detail = screen.getByTestId('other-agent-detail')
    fireEvent.click(within(detail).getByRole('button', { name: 'Check again' }))

    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
    expect(await screen.findByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
  })

  it.each(['Linux', 'macOS'] as const)('keeps a configured unverified agent in setup until Check again confirms installation on %s', async (platform) => {
    const unverified = probe({ installed: 'unknown' })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [unverified] })
    render()

    // Wait for the settled setup state, not the initial pending dashboard render.
    expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
    expect(screen.queryByText(/Finish installing it below/)).not.toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBeNull()
    expect(acpBackendsRefetchInterval(status({ platform }), true, unverified)).toBe(30_000)

    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: unverified })
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled())
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBeNull()

    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    await waitFor(() => expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBe('1'))
    expect(acpBackendsRefetchInterval(status({ platform }), true, probe())).toBe(false)
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it.each(['Linux', 'macOS'] as const)('opens the dashboard for a configured, installed non-Kiro agent with no Kiro CLI on %s', async (platform) => {
    // An operator using an installed Claude Code with no Kiro CLI must not be
    // held behind checks about an agent they do not run.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    await settle()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
  })

  it('disables a sandbox-blocked agent choice before saving when the host has no sandbox', async () => {
    const sandboxStatus = { sandbox_backend_available: false }
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(sandboxStatus))
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' }), probe()],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    const useAgent = screen.getByRole('button', { name: /Use codex/ })
    expect(useAgent).toBeDisabled()
    const detail = screen.getByTestId('other-agent-detail')
    // Plain words for what the block means, in place of the bare badge text
    // repeated as a heading: a reader who does not know what a sandbox is here
    // otherwise has a disabled button, a command, and no idea why.
    expect(within(detail).getByText(
      'Kiro Crew runs codex inside a sandbox that keeps your credentials out of its reach. This host cannot provide that sandbox right now, so codex stays unavailable until it can.',
    )).toBeInTheDocument()
    expect(within(detail).getByText(/Run this on the gateway host for a full prerequisite report/)).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Copy command' })).toHaveTextContent('kirocrew doctor')
    // Not a dead end: the row keeps its own Check again, so the one enabled
    // act is not copying a command. The verdict it re-takes lives on the
    // prerequisite status, so that is re-read alongside the harness probe.
    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe({ id: 'codex', policy_id: 'codex' }) })
    const recheck = within(detail).getByRole('button', { name: 'Check again' })
    expect(recheck).toBeEnabled()
    const statusReads = vi.mocked(api.kiroPrerequisite).mock.calls.length
    fireEvent.click(recheck)
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('codex'))
    await waitFor(() => expect(vi.mocked(api.kiroPrerequisite).mock.calls.length).toBeGreaterThan(statusReads))
    const selectedRow = screen.getByRole('radio', { name: 'codex' }).closest('label')
    expect(within(selectedRow!).getByText("Host can't sandbox")).toHaveClass('text-warn')
    expect(within(selectedRow!).queryByText('Installed')).not.toBeInTheDocument()
    expect(detail).not.toHaveTextContent('ready to use')
    fireEvent.click(useAgent)
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it('names the sandbox SETTING, not the host, when the host sandbox works but is turned off', async () => {
    // `sandbox_facts` reports `sandbox_backend_available: true` (the host can
    // build a sandbox) AND lists every enforced harness in
    // `sandbox_blocked_backends` when `agent.sandbox` is `off`. "This host
    // cannot provide that sandbox" is then false, and Check again / doctor
    // re-measure a host that is fine — the remedy is the setting alone.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: true,
      sandbox_blocked_backends: ['codex'],
    }))
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' }), probe()],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    const useAgent = screen.getByRole('button', { name: /Use codex/ })
    expect(useAgent).toBeDisabled()
    const detail = screen.getByTestId('other-agent-detail')
    expect(within(detail).getByText(
      "Kiro Crew runs codex inside a sandbox that keeps your credentials out of its reach, but the sandbox is turned off in Kiro Crew's settings (agent.sandbox). Turn it back on to use codex.",
    )).toBeInTheDocument()
    // Neither remedy for a host verdict is offered: nothing they measure is the cause.
    expect(within(detail).queryByText(/This host cannot provide that sandbox/)).not.toBeInTheDocument()
    expect(within(detail).queryByText(/Run this on the gateway host/)).not.toBeInTheDocument()
    expect(within(detail).queryByText('kirocrew doctor')).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
    // The badge says what the detail says: off, not unavailable.
    const selectedRow = screen.getByRole('radio', { name: 'codex' }).closest('label')
    expect(within(selectedRow!).getByText('Sandbox turned off')).toHaveClass('text-warn')
    expect(within(selectedRow!).queryByText("Host can't sandbox")).not.toBeInTheDocument()
    expect(within(selectedRow!).queryByText('Installed')).not.toBeInTheDocument()
    fireEvent.click(useAgent)
    expect(api.patchConfig).not.toHaveBeenCalled()

    // The setting blocks only the enforced harnesses the gateway listed: an
    // unlisted one is still installed, ready, and offered with no re-check.
    fireEvent.click(screen.getByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(screen.getByTestId('other-agent-detail')).toHaveTextContent('ready to use')
    expect(within(screen.getByTestId('other-agent-detail')).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
  })

  it('keeps the host verdict when the host has no sandbox, whatever the blocked list says', async () => {
    // Both flags at once is a host that cannot build a sandbox: the setting is
    // moot there, so the host-verdict copy and its remedies stay.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: false,
      sandbox_blocked_backends: ['codex'],
    }))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ id: 'codex', policy_id: 'codex' })] })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    const detail = screen.getByTestId('other-agent-detail')
    expect(within(detail).getByText(/This host cannot provide that sandbox/)).toBeInTheDocument()
    expect(within(detail).queryByText(/turned off in Kiro Crew's settings/)).not.toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Check again' })).toBeEnabled()
    const selectedRow = screen.getByRole('radio', { name: 'codex' }).closest('label')
    expect(within(selectedRow!).getByText("Host can't sandbox")).toBeInTheDocument()
    expect(within(selectedRow!).queryByText('Sandbox turned off')).not.toBeInTheDocument()
  })

  it('lets a non-enforced agent start on a backend-less host that permits unsandboxed exec', async () => {
    // Native Windows: no OS sandbox backend, but unsandboxed exec is the
    // platform default. Claude Code needs no Crew OS credential mask, so it is
    // usable and finishes setup even though `sandbox_backend_available` is false.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: false,
      unsandboxed_exec_permitted: true,
    }))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    const detail = screen.getByTestId('other-agent-detail')
    expect(detail).toHaveTextContent('ready to use')
    expect(within(detail).queryByText(/This host cannot provide that sandbox/)).not.toBeInTheDocument()
    const selectedRow = screen.getByRole('radio', { name: 'Claude Code' }).closest('label')
    expect(within(selectedRow!).getByText('Installed')).toBeInTheDocument()
    expect(within(selectedRow!).queryByText("Host can't sandbox")).not.toBeInTheDocument()
  })

  it('fails closed on an older gateway that omits the unsandboxed-exec field', async () => {
    // No `unsandboxed_exec_permitted` at all (older gateway): the client treats
    // its absence as "not permitted", so a backend-less host still blocks the
    // agent rather than admitting it on a field it never received.
    const older = status({ sandbox_backend_available: false })
    delete (older as { unsandboxed_exec_permitted?: boolean }).unsandboxed_exec_permitted
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(older)
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeDisabled()
    const selectedRow = screen.getByRole('radio', { name: 'Claude Code' }).closest('label')
    expect(within(selectedRow!).getByText("Host can't sandbox")).toBeInTheDocument()
    expect(within(selectedRow!).queryByText('Installed')).not.toBeInTheDocument()
  })

  it.each([
    { sandbox_backend_available: false },
    { sandbox_backend_available: true, sandbox_blocked_backends: ['codex'] },
  ])('offers Kiro fallback for a configured sandbox-blocked agent ($sandbox_backend_available, $sandbox_blocked_backends)', async (sandboxStatus) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(sandboxStatus))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'codex' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' })],
    })
    vi.mocked(api.patchConfig).mockResolvedValue({})
    render()

    const fallback = await screen.findByRole('button', { name: 'Use Kiro CLI instead' })
    expect(fallback).toBeEnabled()
    expect(screen.getByText(/is set to use codex, which isn't ready/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use codex/ })).toBeDisabled()
    fireEvent.click(fallback)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', ''))
  })

  it.each([false, undefined])('keeps configured Codex in setup without confirmed host sandbox capability (%s)', async (available) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'Windows',
      sandbox_backend_available: available,
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'codex' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    expect(acpBackendsRefetchInterval(status({ sandbox_backend_available: available }), true, probe())).toBe(30_000)
  })

  it.each(['codex', 'claude'] as const)('honors the effective-tier sandbox refusal list for %s', async (backend) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: true,
      sandbox_blocked_backends: ['codex'],
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: backend } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: backend, policy_id: backend })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded') !== null).toBe(backend === 'claude')
  })

  it('preserves ready Kiro CLI delegation on Windows without a Crew sandbox backend', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'Windows',
      installed: true,
      authenticated: true,
      ready: true,
      sandbox_backend_available: false,
    }))
    render()

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('keeps the sandbox remedy visible for an installed foreign harness', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Kiro CLI is installed but could not be verified' })).toBeInTheDocument()
  })

  it('requires Kiro CLI ACP support for KAS, which launches through kiro-cli acp', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      acp_supported: false,
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kas' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'kas', policy_id: 'kas', independent_setup: false })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Your Kiro CLI is out of date' })).toBeInTheDocument()
  })

  it('never tells a signed-out user their too-old Kiro CLI is signed in', async () => {
    // The ACP probe runs for a signed-out CLI too, so this card is reachable
    // with `authenticated: false`. Its copy must then hold for both readers:
    // installed and too old, with no claim about sign-in either way.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      acp_supported: false,
    }))
    render()

    expect(await screen.findByRole('heading', { name: 'Your Kiro CLI is out of date' })).toBeInTheDocument()
    expect(screen.getByText(
      'Kiro CLI is installed, but this version predates the acp command Kiro Crew launches every session through. Update it and this page continues on its own.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/signed in/i)).not.toBeInTheDocument()
  })

  it('says under the Update button that it runs the command on the host, with the command verbatim', async () => {
    // The button is not a copy action: the gateway's update_cli spawns the
    // CLI's own `update` on the host. The line under it says so, and names
    // the command in code type from the status, never from a catalog.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      acp_supported: false,
      update_command: 'kiro-cli update',
    }))
    render()

    const update = await screen.findByRole('button', { name: 'Update Kiro CLI' })
    const line = screen.getByTestId('kiro-gate-update-runs-on-host')
    expect(line).toHaveTextContent('Runs kiro-cli update on the gateway host for you.')
    const code = within(line).getByText('kiro-cli update')
    expect(code.tagName).toBe('CODE')
    // Directly under the button, in the pinned footer beside it.
    expect(update.nextElementSibling).toBe(line)
    expect(within(screen.getByTestId('gate-footer')).getByTestId('kiro-gate-update-runs-on-host')).toBe(line)
  })

  it('makes no run-it-for-you claim for the desktop app\'s bundled Kiro CLI, which the gateway will not self-update', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      acp_supported: false,
      bundled_cli: true,
    }))
    render()

    expect(await screen.findByRole('button', { name: 'Update Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-gate-update-runs-on-host')).not.toBeInTheDocument()
  })

  it('does not require Kiro CLI ACP support for an independent harness', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ acp_supported: false }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    await settle()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('does not bypass setup for an unclassified future backend', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'future' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'future', policy_id: 'future', independent_setup: false })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('does not let Kiro CLI\'s own screens hold a usable non-Kiro agent', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      acp_supported: false,
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'codex' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ id: 'codex', policy_id: 'codex' })] })
    render()

    await settle()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Kiro CLI update needed')).not.toBeInTheDocument()
  })

  it('keeps setup up for a configured agent that is not installed, with a way back to Kiro', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockResolvedValue({})
    render()

    expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use other coding agents' }))
      .toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Use Kiro CLI instead' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', ''))
  })

  it.each([false, true])('surfaces a config read failure without bypassing the Kiro checks (installed: %s)', async (installed) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed }))
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new ApiError(500, 'Config read failed'))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    const notice = await screen.findByTestId('kiro-gate-config-error')
    expect(notice).toHaveAttribute('role', 'alert')
    // First-run words: every sibling line on this screen says "coding agent",
    // and "agent backend" is the Developer tab's term for the same thing.
    expect(notice).toHaveTextContent('Could not check which coding agent is set up.')
    expect(notice).not.toHaveTextContent(/backend/i)
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    // Visible without expanding the alternative-agent picker. A usable probe
    // alone cannot bypass Kiro checks when the configured agent is unknown.
    expect(screen.getByRole('button', { name: 'Use other coding agents' }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    if (installed) {
      expect(codeBlock(CURL)).not.toBeInTheDocument()
      expect(screen.getByText('kiro-cli login')).toBeInTheDocument()
    } else {
      expect(codeBlock(CURL)).toBeInTheDocument()
      expect(screen.queryByText('kiro-cli login')).not.toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: installed ? 'Check sign-in again' : 'Check again for Kiro CLI' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(api.patchConfig).not.toHaveBeenCalled()

    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
    fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByTestId('kiro-gate-config-error')).not.toBeInTheDocument())
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('preserves the unsaved agent choice while retrying a config read failure', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new ApiError(500, 'Config read failed'))
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe(), probe({ id: 'codex', policy_id: 'codex' })],
    })
    render()

    const notice = await screen.findByTestId('kiro-gate-config-error')
    fireEvent.click(screen.getByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    expect(api.patchConfig).not.toHaveBeenCalled()

    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
    fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByTestId('kiro-gate-config-error')).not.toBeInTheDocument())
    expect(screen.getByRole('radio', { name: 'codex' })).toBeChecked()
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it.each(['', 'claude'])('surfaces a harness probe failure and keeps the Kiro checks (configured: %s)', async (configured) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: configured } })
    vi.mocked(api.acpBackends).mockRejectedValue(new ApiError(403, 'forbidden'))
    render()

    expect(await screen.findByText('Set up Kiro')).toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: 'Use other coding agents' })
    if (!configured) fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const notice = await screen.findByTestId('other-agents-probe-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Could not check the other agents on this host. You can choose one later in Settings > Agent Harness.')
    expect(within(notice).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/which isn't ready on this host/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use Kiro CLI instead' })).not.toBeInTheDocument()
    expect(api.patchConfig).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('reads neither the config nor the probe for a ready Kiro install', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))
    render()

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(api.kirocrewConfig).not.toHaveBeenCalled()
    expect(api.acpBackends).not.toHaveBeenCalled()
  })

  describe('persisting the first-run marker when the poll admits a configured agent', () => {
    // The poll is read-only: it can admit THIS browser but never writes the
    // durable `initial_setup_complete` marker. Without a write, every later
    // browser and every non-owner stays on first-run setup for good.
    const BACKENDS_KEY = ['acpBackends']
    const missing = () => probe({ installed: 'missing', install_command: 'npm install -g x' })
    // "Dashboard loaded" is painted while the probe is still in flight, so a
    // "did not fire" assertion means nothing until the probe has LANDED and
    // the effect has had a turn on it.
    const probeLanded = async (queryClient: QueryClient) => {
      await waitFor(() => expect(queryClient.getQueryState(BACKENDS_KEY)?.status).toBe('success'))
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
    }

    beforeEach(() => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
      vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    })

    it('fires the recheck POST exactly once for the configured agent when the poll flips to ready', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [missing()] })
      const { queryClient } = render()

      // Not installed yet: setup stays up and nothing is written.
      expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
      expect(api.acpBackendRecheck).not.toHaveBeenCalled()

      // The next poll sees the install. The gate admits AND persists.
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)

      // Re-polls and re-renders with the same verdict do not repeat it.
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] }))
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    })

    it('fires once when the configured agent is already installed on first load', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
    })

    it('fires again only on a new flip: the agent going away and coming back', async () => {
      // Not a repeat of the same verdict — the verdict dropped to "cannot
      // start" in between, and the marker may still be unwritten (the first
      // POST could have refused to write it for the same reason).
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))

      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [missing()] })
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)

      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(2))
    })

    it('does not fire when the marker is already written', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ initial_setup_complete: true }))
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await probeLanded(queryClient)
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).not.toHaveBeenCalled()
    })

    it('still admits this browser when the POST fails, and does not retry on its own', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(503, 'Marker write failed'))
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    })

    it('surfaces a failed marker write over the dashboard without blocking it', async () => {
      // The dashboard is shown (this browser is admitted), but the silent write
      // failure would leave the operator unaware that other browsers/non-owners
      // stay stuck on setup. It must be visible AND non-blocking.
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(
        503,
        'Agent selection was saved, but setup completion could not be recorded. Try again.',
        JSON.stringify({ code: 'setup_marker_write_failed' }),
      ))
      render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      const notice = await screen.findByTestId('kiro-gate-marker-write-error')
      expect(notice).toHaveAttribute('role', 'alert')
      // The server's own sentence is carried verbatim beneath the plain title.
      expect(notice).toHaveTextContent("Setup completion wasn't recorded")
      expect(notice).toHaveTextContent('setup completion could not be recorded')
      // The overlay wrapper is click-through so the app beneath stays operable;
      // only the notice card itself captures pointer events.
      const region = screen.getByTestId('kiro-gate-marker-write-error-region')
      expect(region.className).toContain('pointer-events-none')
      expect(notice.className).toContain('pointer-events-auto')
    })

    it('retries the marker write from the notice and clears it on success', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck)
        .mockRejectedValueOnce(new ApiError(503, 'Marker write failed'))
        .mockResolvedValue({ backend: probe() })
      render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      // Fired once automatically and failed; it must NOT retry on its own.
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      const notice = await screen.findByTestId('kiro-gate-marker-write-error')

      // The notice's own Retry re-POSTs for the configured backend...
      fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(2))
      expect(api.acpBackendRecheck).toHaveBeenLastCalledWith('claude')
      // ...and a success clears the notice, with the dashboard still up.
      await waitFor(() => expect(screen.queryByTestId('kiro-gate-marker-write-error')).toBeNull())
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    })

    it('does not re-fire the marker write on its own after it fails', async () => {
      // The effect writes one POST per flip: a failed write changes no
      // dependency, so there is no spin. Only the Retry button re-fires it.
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(503, 'Marker write failed'))
      const { queryClient } = render()

      await screen.findByTestId('kiro-gate-marker-write-error')
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      // Re-polls and re-renders with the same failed verdict do not repeat it.
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] }))
      await act(() => new Promise(resolve => setTimeout(resolve, 30)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
      // Still visible, still one POST.
      expect(screen.getByTestId('kiro-gate-marker-write-error')).toBeInTheDocument()
    })

    it('dismisses the marker-write notice without another POST', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(503, 'Marker write failed'))
      render()

      const notice = await screen.findByTestId('kiro-gate-marker-write-error')
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
      await waitFor(() => expect(screen.queryByTestId('kiro-gate-marker-write-error')).toBeNull())
      // Dismissing is not a retry: the dashboard stays up and no POST is sent.
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
    })

    it('does not fire for a viewer the status says cannot run setup', async () => {
      // A non-owner never gets a probe (the GET answers 403), but the status
      // bit is the client's own word for it and is honoured on its own too.
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ setup_allowed: false }))
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await probeLanded(queryClient)
      expect(api.acpBackendRecheck).not.toHaveBeenCalled()
    })
  })
})

describe('KiroPrerequisiteGate agent picker copy (UX round 5)', () => {
  const INTRO = /Already use another coding agent\?/
  const SWITCH_LATER = 'You can switch agents at any time in Settings > Agent Harness.'
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )
  const openPicker = async () => {
    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
  }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('does not tell the user to pick an agent above a list that offers none', async () => {
    // "Pick one that is installed on the gateway host" directly above "This
    // build offers no other coding agents" was an instruction and its own
    // refutation on consecutive lines.
    render()
    await openPicker()

    expect(await screen.findByText('This build offers no other coding agents.')).toBeInTheDocument()
    expect(screen.queryByText(INTRO)).not.toBeInTheDocument()
  })

  it('withholds the intro while the list is still being checked', async () => {
    // Nothing to pick from yet either; the intro arrives with the list.
    let resolveBackends: (v: { backends: AcpBackendProbe[] }) => void = () => {}
    vi.mocked(api.acpBackends).mockReturnValue(
      new Promise(resolve => { resolveBackends = resolve }),
    )
    render()
    await openPicker()

    expect(await screen.findByText('Checking which agents are installed…')).toBeInTheDocument()
    expect(screen.queryByText(INTRO)).not.toBeInTheDocument()

    await act(async () => { resolveBackends({ backends: [probe()] }) })
    expect(await screen.findByRole('radio', { name: 'Claude Code' })).toBeInTheDocument()
    expect(screen.getByText(INTRO)).toBeInTheDocument()
  })

  it('withholds the intro when the probe failed, leaving the notice its own pointer', async () => {
    vi.mocked(api.acpBackends).mockRejectedValue(new ApiError(403, 'forbidden'))
    render()
    await openPicker()

    const notice = await screen.findByTestId('other-agents-probe-error')
    expect(notice).toHaveTextContent('You can choose one later in Settings > Agent Harness.')
    expect(screen.queryByText(INTRO)).not.toBeInTheDocument()
  })

  it('keeps the intro above a list with agents to pick from', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()
    await openPicker()

    await screen.findByRole('radio', { name: 'Claude Code' })
    expect(screen.getByText(INTRO)).toHaveTextContent('Pick one that is installed on the gateway host.')
  })

  it('names the Use button, by its rendered label, as the retry for a failed agent save', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()
    await openPicker()

    const useButton = await screen.findByRole('button', { name: /Use Claude Code/ })
    fireEvent.click(useButton)
    const notice = await screen.findByTestId('other-agent-switch-error')
    // The retry it names is the control that exists in this row, spelled as
    // that control spells itself. No control on this screen is called Try again.
    const label = useButton.textContent?.trim()
    expect(label).toBe('Use Claude Code')
    expect(notice).toHaveTextContent(`Press ${label} to try again.`)
    expect(notice).not.toHaveTextContent(/\bTry again\./)
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('names the Use Kiro CLI instead button, by its rendered label, as the retry for a failed switch back', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()

    const fallback = await screen.findByRole('button', { name: 'Use Kiro CLI instead' })
    fireEvent.click(fallback)
    const notice = await screen.findByTestId('use-kiro-instead-error')
    expect(notice).toHaveTextContent(`Press ${fallback.textContent?.trim()} to try again.`)
    expect(notice).not.toHaveTextContent(/\bTry again\./)
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('says a failed re-check in the same words as the Check failed badge', async () => {
    // Badge "Check failed" + notice "Could not re-check pi" read as two
    // different problems on one row. One check, one failure, one vocabulary.
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'pi', policy_id: 'pi', installed: 'unknown' })],
    })
    vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(500, 'Probe failed'))
    render()
    await openPicker()

    const badge = await screen.findByText('Check failed')
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    const notice = await screen.findByTestId('other-agent-recheck-error')
    expect(notice).toHaveTextContent('The check for pi failed. Press Check again.')
    expect(notice).not.toHaveTextContent(/re-check|Could not/)
    // Same two words carry the verdict in both places.
    expect(badge.textContent).toMatch(/check/i)
    expect(badge.textContent).toMatch(/fail/i)
    expect(notice.textContent).toMatch(/check/i)
    expect(notice.textContent).toMatch(/fail/i)
    // The badge stays short and does not borrow the positive sibling's word.
    expect(badge.textContent).not.toMatch(/installed/i)
    expect(badge.textContent).not.toBe('Installed, not active yet')
  })

  it('says "switch agents later" once when the configured-agent notice already says it', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    render()

    await screen.findByText(/is set to use Claude Code, which isn't ready/)
    const lines = screen.getAllByText(SWITCH_LATER)
    expect(lines).toHaveLength(1)
    // The one that stays is the notice's, beside Use Kiro CLI instead.
    expect(lines[0].parentElement).toBe(
      screen.getByRole('button', { name: 'Use Kiro CLI instead' }).parentElement,
    )
  })

  it('keeps the list footer\'s "switch agents later" line when no configured-agent notice is up', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()
    await openPicker()

    await screen.findByRole('radio', { name: 'Claude Code' })
    expect(screen.getAllByText(SWITCH_LATER)).toHaveLength(1)
    expect(screen.queryByText(/which isn't ready on this host/)).not.toBeInTheDocument()
  })

  it('brings the footer line back once the notice leaves with a successful switch to Kiro CLI', async () => {
    // The notice and the footer swap rather than stack: while the configured
    // agent is not ready the notice carries the line; after Use Kiro CLI
    // instead saves, the notice is gone and the foot of the list says it.
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockImplementation(async () => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
      return {}
    })
    render()

    await screen.findByText(/is set to use Claude Code, which isn't ready/)
    expect(screen.getAllByText(SWITCH_LATER)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Use Kiro CLI instead' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', ''))
    await waitFor(() => expect(screen.queryByText(/which isn't ready on this host/)).not.toBeInTheDocument())
    // Still on setup (Kiro CLI is not installed), the list still open.
    expect(screen.getByRole('radio', { name: 'Claude Code' })).toBeInTheDocument()
    const lines = screen.getAllByText(SWITCH_LATER)
    expect(lines).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Use Kiro CLI instead' })).not.toBeInTheDocument()
  })

  describe('every locale quotes its own button labels in the retry notices', () => {
    // The English guard above reads the rendered button; the other catalogs
    // are only ever seen by a reader of that language, so the same drift —
    // a relabelled button leaving a notice pointing at a name that is no
    // longer on screen — is pinned per locale here. The generated
    // pseudolocale is excluded: its per-key mangling does not preserve
    // cross-key containment, and it regenerates from English anyway.
    const generated = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))
    const shipped = Object.keys(CATALOGS).filter(code => !generated.has(code))
    const gate = (code: string) => (
      (CATALOGS[code].translation as { components: { kiroPrerequisiteGate: Record<string, string> } })
        .components.kiroPrerequisiteGate
    )

    it('covers every shipped language', () => {
      expect(shipped).toEqual(expect.arrayContaining(['en', 'de', 'ja', 'zh-CN']))
      expect(shipped.length).toBeGreaterThanOrEqual(12)
    })

    it.each(shipped)('%s: the failed-save notice names that locale\'s Use {{name}} button', (code) => {
      const g = gate(code)
      expect(g.could_not_save_agent_choice).toContain(g.use_agent)
      expect(g.use_agent).toContain('{{name}}')
    })

    it.each(shipped)('%s: the failed switch-back notice names that locale\'s Use Kiro CLI instead button', (code) => {
      const g = gate(code)
      expect(g.could_not_switch_to_kiro).toContain(g.use_kiro_cli_instead)
    })

    it.each(shipped)('%s: the failed re-check notice names that locale\'s Check again button', (code) => {
      const g = gate(code)
      expect(g.agent_recheck_failed).toContain(g.check_again)
    })

    it.each(shipped)('%s: the two sandbox pills name different causes, and neither is the host screen\'s title', (code) => {
      // Same warn colour, opposite remedies: only the words tell them apart.
      const g = gate(code)
      expect(g.agent_host_cannot_sandbox).toBeTruthy()
      expect(g.agent_sandbox_turned_off).toBeTruthy()
      expect(g.agent_host_cannot_sandbox).not.toBe(g.agent_sandbox_turned_off)
      expect(g.agent_host_cannot_sandbox).not.toBe(g.sandbox_unavailable)
    })

    it.each(shipped)('%s: the update helper keeps the command slot in code tags', (code) => {
      // The command is interpolated from the gateway, never translated.
      expect(gate(code).update_kiro_cli_runs_on_host).toMatch(/<0>\{\{command\}\}<\/0>/)
    })
  })
})
